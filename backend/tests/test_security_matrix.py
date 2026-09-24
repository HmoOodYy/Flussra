"""
Security matrix tests — Foundation Lockdown Phase 1.

Verifies backend access-boundary invariants for Current Payroll, Review, and Ledger.

Design notes:
  - Uses session_client + auth_token (session-scoped) so all tests share the same DB state.
  - A module-level fixture creates one period per branch (opened immediately so the
    Draft slot is freed), then stores those period_ids for reuse.
  - Tests that need blocked-user access just try the endpoint and expect 403.
  - User/role creation uses function-unique usernames to avoid collisions.
  - Period pay tests use PAYTEST branch where BONUS is active.

Matrix:
  1.  ODA user cannot access payroll period list/detail/lines/summary.
  2.  Driver role + SpecificBranch cannot access payroll period list/detail/lines/summary.
  3.  SpecificBranch user without payroll.view/payroll.entry cannot read Current Payroll.
  4.  SpecificBranch user with payroll.view can read only allowed branch.
  5.  SpecificBranch user cannot access other branch.
  6.  AllCompanyBranches operational user can access company branches.
  7.  Driver/ODA cannot mutate draft lines or period status.
  8.  Review list/detail require review/payroll access (not just branch access).
  9.  Driver/ODA cannot access review list/detail/decide.
  10. Final-lines require payroll read permission.
  11. Final-lines reject non-Locked/non-Archived periods.
  12. Driver/ODA cannot access final-lines.
  13. Manual ADJUSTMENT period-pay create/update is rejected.
  15. Driver role with SpecificBranch (not only ODA) is specifically blocked.
"""

import datetime
import itertools
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _sqla_text
from sqlalchemy.ext.asyncio import create_async_engine
from app.payroll_setup.policy import assign_setup, create_draft, create_setup, publish_version

# ---------------------------------------------------------------------------
# Unique username counter — keeps each test's users separate
# ---------------------------------------------------------------------------

_counter = itertools.count(1)


def _uid() -> str:
    return f"sm{next(_counter):04d}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(
    client: httpx.AsyncClient,
    username: str,
    password: str = "TestPass1234!",
    company_code: str = "DEMO",
) -> str:
    resp = await client.post("/auth/login", json={
        "username": username,
        "password": password,
        "company_code": company_code,
    })
    assert resp.status_code == 200, f"Login failed for {username!r}: {resp.text}"
    return resp.json()["access_token"]


async def _create_user(
    client: httpx.AsyncClient,
    token: str,
    username: str,
) -> dict:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": username,
            "password": "TestPass1234!",
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 201, f"Create user {username!r} failed: {resp.text}"
    return resp.json()


async def _get_branch_id(
    client: httpx.AsyncClient,
    token: str,
    branch_code: str,
) -> int:
    resp = await client.get("/core/branches", headers=_hdr(token))
    assert resp.status_code == 200
    branch = next((b for b in resp.json() if b["branch_code"] == branch_code), None)
    assert branch is not None, f"Branch {branch_code!r} not found"
    return branch["branch_id"]


async def _get_driver_role_id(client: httpx.AsyncClient, token: str) -> int:
    resp = await client.get("/admin/company-roles", headers=_hdr(token))
    assert resp.status_code == 200
    role = next((r for r in resp.json() if r["role_code"] == "DRIVER"), None)
    assert role is not None, "DRIVER company role not found"
    return role["company_role_id"]


async def _create_role_with_perms(
    client: httpx.AsyncClient,
    token: str,
    role_name: str,
    perms: list[str],
) -> int:
    cr = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=_hdr(token),
    )
    assert cr.status_code == 201, f"Create role {role_name!r} failed: {cr.text}"
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=_hdr(token),
        )
        assert pr.status_code == 200, f"Set permissions failed: {pr.text}"
    return role_id


async def _assign_role(
    client: httpx.AsyncClient,
    token: str,
    user_id: int,
    role_id: int,
    scope: str = "AllCompanyBranches",
    branch_id: int | None = None,
) -> None:
    payload: dict = {"company_role_id": role_id, "scope_type": scope}
    if branch_id is not None:
        payload["branch_id"] = branch_id
    resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=payload,
        headers=_hdr(token),
    )
    assert resp.status_code == 201, f"Assign role failed: {resp.text}"


async def _create_driver(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    suffix: str,
) -> int:
    resp = await client.post(
        "/core/drivers",
        json={
            "branch_id": branch_id,
            "full_name": f"SM Driver {suffix}",
            "driver_code": f"SMD-{suffix}",
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 201, f"Create driver failed: {resp.text}"
    return resp.json()["driver_id"]


async def _cancel_branch_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    """Cancel Draft and Open periods for a branch so fixtures can create fresh ones."""
    for status in ("Draft", "Open"):
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": branch_id, "status": status},
            headers=_hdr(token),
        )
        if resp.status_code != 200:
            continue
        for p in resp.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"},
                headers=_hdr(token),
            )


async def _create_and_open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str,
    end: str,
    *,
    db_conn=None,
) -> int:
    """Insert an Open period directly (CP-1D: POST requires existing Open; PATCH→Open blocked)."""
    assert db_conn is not None, "_create_and_open_period requires db_conn after CP-1D"
    code = f"SM-{branch_id}-{start}"
    # Cancel any stale non-SM Open/InReview/Returned periods left by other test modules
    # so the one-Open-per-branch partial unique index doesn't block the SM INSERT.
    await db_conn.execute(
        _sqla_text(
            "UPDATE payroll.payrollperiods SET status = 'Cancelled', currentreturnreviewitemid = NULL "
            "WHERE branchid = :bid AND status IN ('Open','Draft','InReview','Returned') "
            "  AND periodcode NOT LIKE 'SM-%'"
        ),
        {"bid": branch_id},
    )
    row = (await db_conn.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": code,
         "start": datetime.date.fromisoformat(start),
         "end": datetime.date.fromisoformat(end)},
    )).mappings().first()
    if row is None:
        row = (await db_conn.execute(
            _sqla_text(
                "SELECT payrollperiodid FROM payroll.payrollperiods "
                "WHERE branchid = :bid AND periodcode = :code"
            ),
            {"bid": branch_id, "code": code},
        )).mappings().first()
    return row["payrollperiodid"]


# ---------------------------------------------------------------------------
# Session-scoped setup fixtures for the security matrix
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="module")
async def sm_hq_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    return await _get_branch_id(session_client, auth_token, "HQ")


@pytest_asyncio.fixture(scope="module")
async def sm_paytest_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    return await _get_branch_id(session_client, auth_token, "PAYTEST")


@pytest_asyncio.fixture(scope="module")
async def sm_driver_role_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    return await _get_driver_role_id(session_client, auth_token)


@pytest_asyncio.fixture(scope="module")
async def sm_hq_period_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    sm_hq_id: int,
    session_db_conn,
) -> int:
    """One Open HQ period for all security matrix tests that need a period reference."""
    return await _create_and_open_period(
        session_client, auth_token, sm_hq_id,
        "2096-01-06", "2096-01-12",
        db_conn=session_db_conn,
    )


@pytest_asyncio.fixture(scope="module")
async def sm_paytest_period_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    sm_paytest_id: int,
    session_db_conn,
) -> int:
    """One Open PAYTEST period for security matrix tests."""
    return await _create_and_open_period(
        session_client, auth_token, sm_paytest_id,
        "2096-01-13", "2096-01-19",
        db_conn=session_db_conn,
    )


@pytest_asyncio.fixture(scope="module")
async def sm_paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    sm_paytest_id: int,
) -> int:
    """One driver on PAYTEST branch for period-pay tests."""
    return await _create_driver(session_client, auth_token, sm_paytest_id, "SM-PAY")


@pytest_asyncio.fixture(scope="module")
async def sm_hq_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    sm_hq_id: int,
) -> int:
    """One driver on HQ branch for mutation tests."""
    return await _create_driver(session_client, auth_token, sm_hq_id, "SM-HQ")


@pytest_asyncio.fixture(scope="module")
async def sm_bonus_activated(
    session_client: httpx.AsyncClient,
    auth_token: str,
    sm_paytest_id: int,
) -> None:
    """Activate BONUS for PAYTEST branch (effectivefrom=today, effectiveto=NULL covers all future dates)."""
    items_resp = await session_client.get(
        f"/settings/branches/{sm_paytest_id}/pay-items",
        headers=_hdr(auth_token),
    )
    assert items_resp.status_code == 200
    for item in items_resp.json():
        if item["pay_item_code"] == "BONUS":
            await session_client.patch(
                f"/settings/branches/{sm_paytest_id}/pay-items/{item['pay_item_id']}",
                json={"is_active": True},
                headers=_hdr(auth_token),
            )
            return


@pytest_asyncio.fixture(scope="module")
async def sm_adjustment_activated(
    session_client: httpx.AsyncClient,
    auth_token: str,
    sm_paytest_id: int,
) -> None:
    """Activate the supported non-Bonus Period Pay item for route-security tests."""
    items_resp = await session_client.get(
        f"/settings/branches/{sm_paytest_id}/pay-items",
        headers=_hdr(auth_token),
    )
    assert items_resp.status_code == 200
    item = next(
        (i for i in items_resp.json() if i["pay_item_code"] == "ADJUSTMENT"),
        None,
    )
    assert item is not None, "ADJUSTMENT pay item not found"
    if not item.get("is_active", False):
        resp = await session_client.patch(
            f"/settings/branches/{sm_paytest_id}/pay-items/{item['pay_item_id']}",
            json={"is_active": True},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Helper: create ODA user assigned to a branch (ODA requires branch_id)
# ---------------------------------------------------------------------------

async def _make_oda_user(
    client: httpx.AsyncClient,
    token: str,
    driver_role_id: int,
    branch_id: int,
) -> str:
    """Create user with ODA scope, return token. ODA requires branch_id."""
    uname = f"sm_oda_{_uid()}"
    user = await _create_user(client, token, uname)
    await _assign_role(
        client, token, user["user_id"], driver_role_id,
        scope="OwnDriverDataOnly", branch_id=branch_id,
    )
    return await _login(client, uname)


async def _make_driver_sb_user(
    client: httpx.AsyncClient,
    token: str,
    driver_role_id: int,
    branch_id: int,
) -> str:
    """Create user with DRIVER role + SpecificBranch scope, return token."""
    uname = f"sm_drvsb_{_uid()}"
    user = await _create_user(client, token, uname)
    await _assign_role(
        client, token, user["user_id"], driver_role_id,
        scope="SpecificBranch", branch_id=branch_id,
    )
    return await _login(client, uname)


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSecurityMatrix:

    # -----------------------------------------------------------------------
    # 1. ODA user cannot access payroll period list/detail/lines/summary
    # -----------------------------------------------------------------------

    async def test_1_oda_blocked_period_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
    ):
        """ODA-scope user is blocked from payroll period list."""
        tok = await _make_oda_user(session_client, auth_token, sm_driver_role_id, sm_hq_id)
        resp = await session_client.get("/payroll/periods", headers=_hdr(tok))
        assert resp.status_code == 403, (
            f"ODA user must be blocked from period list; got {resp.status_code}"
        )

    async def test_1_oda_blocked_period_detail(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
    ):
        """ODA-scope user is blocked from payroll period detail."""
        tok = await _make_oda_user(session_client, auth_token, sm_driver_role_id, sm_hq_id)
        resp = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}", headers=_hdr(tok)
        )
        assert resp.status_code == 403, (
            f"ODA user must be blocked from period detail; got {resp.status_code}"
        )

    async def test_1_oda_blocked_period_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
    ):
        """ODA-scope user is blocked from payroll period draft lines."""
        tok = await _make_oda_user(session_client, auth_token, sm_driver_role_id, sm_hq_id)
        resp = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}/lines", headers=_hdr(tok)
        )
        assert resp.status_code == 403

    async def test_1_oda_blocked_period_summary(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
    ):
        """ODA-scope user is blocked from payroll period draft summary."""
        tok = await _make_oda_user(session_client, auth_token, sm_driver_role_id, sm_hq_id)
        resp = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}/lines/summary", headers=_hdr(tok)
        )
        assert resp.status_code == 403

    # -----------------------------------------------------------------------
    # 2 & 15. Driver role + SpecificBranch blocked (key regression test)
    # -----------------------------------------------------------------------

    async def test_2_driver_sb_blocked_period_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
    ):
        """Driver role + SpecificBranch is blocked from period list.

        This is the critical gap: _get_oda_own_driver_id missed DRIVER+SpecificBranch.
        _require_not_driver_role catches it by checking rolecode='DRIVER'.
        """
        tok = await _make_driver_sb_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.get("/payroll/periods", headers=_hdr(tok))
        assert resp.status_code == 403, (
            f"Driver+SpecificBranch must be blocked from period list; got {resp.status_code}"
        )

    async def test_2_driver_sb_blocked_period_detail(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
    ):
        """Driver role + SpecificBranch is blocked from period detail."""
        tok = await _make_driver_sb_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}", headers=_hdr(tok)
        )
        assert resp.status_code == 403

    async def test_2_driver_sb_blocked_period_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
    ):
        """Driver role + SpecificBranch is blocked from period draft lines."""
        tok = await _make_driver_sb_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}/lines", headers=_hdr(tok)
        )
        assert resp.status_code == 403

    async def test_2_driver_sb_blocked_period_summary(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
    ):
        """Driver role + SpecificBranch is blocked from period draft summary."""
        tok = await _make_driver_sb_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}/lines/summary", headers=_hdr(tok)
        )
        assert resp.status_code == 403

    # -----------------------------------------------------------------------
    # 3. SpecificBranch without payroll.view/entry is blocked
    # -----------------------------------------------------------------------

    async def test_3_no_payroll_perm_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_hq_period_id: int,
    ):
        """SpecificBranch user with only drivers.view cannot read Current Payroll."""
        no_payroll = await _create_role_with_perms(
            session_client, auth_token, f"SM3NoPay-{_uid()}", ["drivers.view"]
        )
        uname = f"sm3_{_uid()}"
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(
            session_client, auth_token, user["user_id"], no_payroll,
            scope="SpecificBranch", branch_id=sm_hq_id,
        )
        tok = await _login(session_client, uname)

        # List blocked
        resp = await session_client.get("/payroll/periods", headers=_hdr(tok))
        assert resp.status_code == 403, (
            f"No-payroll user blocked from period list; got {resp.status_code}"
        )

        # Detail blocked
        resp2 = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}", headers=_hdr(tok)
        )
        assert resp2.status_code == 403, (
            f"No-payroll user blocked from period detail; got {resp2.status_code}"
        )

    # -----------------------------------------------------------------------
    # 4. SpecificBranch with payroll.view can read allowed branch
    # -----------------------------------------------------------------------

    async def test_4_payroll_view_can_read_own_branch(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_hq_period_id: int,
    ):
        """SpecificBranch user with payroll.view can access their branch's periods."""
        viewer = await _create_role_with_perms(
            session_client, auth_token, f"SM4View-{_uid()}", ["payroll.view"]
        )
        uname = f"sm4_{_uid()}"
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(
            session_client, auth_token, user["user_id"], viewer,
            scope="SpecificBranch", branch_id=sm_hq_id,
        )
        tok = await _login(session_client, uname)

        resp = await session_client.get("/payroll/periods", headers=_hdr(tok))
        assert resp.status_code == 200, f"payroll.view must access list; got {resp.status_code}"
        # Only their branch should appear
        for p in resp.json():
            assert p["branch_id"] == sm_hq_id, f"Period from other branch leaked: {p}"

        resp2 = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}", headers=_hdr(tok)
        )
        assert resp2.status_code == 200

    # -----------------------------------------------------------------------
    # 5. SpecificBranch cannot access other branch
    # -----------------------------------------------------------------------

    async def test_5_specificbranch_cannot_access_other_branch(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_paytest_id: int,
        sm_paytest_period_id: int,
    ):
        """SpecificBranch/HQ user cannot access PAYTEST periods."""
        viewer = await _create_role_with_perms(
            session_client, auth_token, f"SM5HQView-{_uid()}", ["payroll.view"]
        )
        uname = f"sm5_{_uid()}"
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(
            session_client, auth_token, user["user_id"], viewer,
            scope="SpecificBranch", branch_id=sm_hq_id,
        )
        tok = await _login(session_client, uname)

        resp = await session_client.get(
            f"/payroll/periods/{sm_paytest_period_id}", headers=_hdr(tok)
        )
        assert resp.status_code == 403, (
            f"SpecificBranch user must not access other branch; got {resp.status_code}"
        )

    # -----------------------------------------------------------------------
    # 6. AllCompanyBranches operational user can access all branches
    # -----------------------------------------------------------------------

    async def test_6_allbranches_can_access_all(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_paytest_id: int,
        sm_hq_period_id: int,
        sm_paytest_period_id: int,
    ):
        """AllCompanyBranches user with payroll.view can access any branch's periods."""
        all_viewer = await _create_role_with_perms(
            session_client, auth_token, f"SM6AllView-{_uid()}", ["payroll.view"]
        )
        uname = f"sm6_{_uid()}"
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(
            session_client, auth_token, user["user_id"], all_viewer,
            scope="AllCompanyBranches",
        )
        tok = await _login(session_client, uname)

        r1 = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}", headers=_hdr(tok)
        )
        assert r1.status_code == 200, f"AllCompanyBranches blocked from HQ period: {r1.text}"

        r2 = await session_client.get(
            f"/payroll/periods/{sm_paytest_period_id}", headers=_hdr(tok)
        )
        assert r2.status_code == 200, (
            f"AllCompanyBranches blocked from PAYTEST period: {r2.text}"
        )

    # -----------------------------------------------------------------------
    # 7. Driver/ODA cannot mutate draft lines or period status
    # -----------------------------------------------------------------------

    async def test_7_driver_sb_cannot_add_draft_line(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
        sm_hq_driver_id: int,
    ):
        """Driver+SpecificBranch cannot add draft lines."""
        tok = await _make_driver_sb_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.post(
            f"/payroll/periods/{sm_hq_period_id}/lines",
            json={
                "driver_id": sm_hq_driver_id,
                "work_date": "2096-01-08",
                "line_type": "HOURS",
                "quantity": "8.00",
                "rate_amount": "10.00",
            },
            headers=_hdr(tok),
        )
        assert resp.status_code == 403, (
            f"Driver+SpecificBranch must not add draft lines; got {resp.status_code}"
        )

    async def test_7_driver_sb_cannot_change_period_status(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
    ):
        """Driver+SpecificBranch cannot change period status."""
        tok = await _make_driver_sb_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.patch(
            f"/payroll/periods/{sm_hq_period_id}/status",
            json={"status": "InReview"},
            headers=_hdr(tok),
        )
        assert resp.status_code == 403, (
            f"Driver+SpecificBranch must not change period status; got {resp.status_code}"
        )

    async def test_7_oda_cannot_add_draft_line(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
        sm_hq_driver_id: int,
    ):
        """ODA user cannot add draft lines."""
        tok = await _make_oda_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.post(
            f"/payroll/periods/{sm_hq_period_id}/lines",
            json={
                "driver_id": sm_hq_driver_id,
                "work_date": "2096-01-08",
                "line_type": "HOURS",
                "quantity": "8.00",
                "rate_amount": "10.00",
            },
            headers=_hdr(tok),
        )
        assert resp.status_code == 403

    # -----------------------------------------------------------------------
    # 8. Review list/detail require payroll/review access
    # -----------------------------------------------------------------------

    async def test_8_no_payroll_perm_blocked_from_review(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
    ):
        """SpecificBranch user with only drivers.view cannot read review list."""
        no_perm = await _create_role_with_perms(
            session_client, auth_token, f"SM8NoPerm-{_uid()}", ["drivers.view"]
        )
        uname = f"sm8_{_uid()}"
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(
            session_client, auth_token, user["user_id"], no_perm,
            scope="SpecificBranch", branch_id=sm_hq_id,
        )
        tok = await _login(session_client, uname)

        resp = await session_client.get("/review/items", headers=_hdr(tok))
        assert resp.status_code == 403, (
            f"drivers.view-only user must be blocked from review; got {resp.status_code}"
        )

    async def test_8_payroll_view_can_read_review(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
    ):
        """SpecificBranch user with payroll.view can read review list."""
        viewer = await _create_role_with_perms(
            session_client, auth_token, f"SM8View-{_uid()}", ["payroll.view"]
        )
        uname = f"sm8b_{_uid()}"
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(
            session_client, auth_token, user["user_id"], viewer,
            scope="SpecificBranch", branch_id=sm_hq_id,
        )
        tok = await _login(session_client, uname)

        resp = await session_client.get("/review/items", headers=_hdr(tok))
        assert resp.status_code == 200, (
            f"payroll.view user must access review list; got {resp.status_code}"
        )

    # -----------------------------------------------------------------------
    # 9. Driver/ODA cannot access review list/detail/decide
    # -----------------------------------------------------------------------

    async def test_9_oda_blocked_from_review_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
    ):
        """ODA-scope user cannot access review list."""
        tok = await _make_oda_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.get("/review/items", headers=_hdr(tok))
        assert resp.status_code == 403

    async def test_9_driver_sb_blocked_from_review_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
    ):
        """Driver+SpecificBranch cannot access review list."""
        tok = await _make_driver_sb_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.get("/review/items", headers=_hdr(tok))
        assert resp.status_code == 403

    # -----------------------------------------------------------------------
    # 10. Final-lines require payroll read permission
    # -----------------------------------------------------------------------

    async def test_10_no_payroll_perm_blocked_from_final_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_hq_period_id: int,
    ):
        """SpecificBranch user with only drivers.view cannot access final-lines.

        The driver-role block fires first for DRIVER-role users, but this test
        uses a custom role with 'drivers.view' only (not rolecode='DRIVER').
        It gets past the driver block but fails the payroll permission check.
        """
        no_perm = await _create_role_with_perms(
            session_client, auth_token, f"SM10NoPerm-{_uid()}", ["drivers.view"]
        )
        uname = f"sm10_{_uid()}"
        user = await _create_user(session_client, auth_token, uname)
        await _assign_role(
            session_client, auth_token, user["user_id"], no_perm,
            scope="SpecificBranch", branch_id=sm_hq_id,
        )
        tok = await _login(session_client, uname)

        resp = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}/final-lines", headers=_hdr(tok)
        )
        assert resp.status_code == 403, (
            f"No-payroll user must be blocked from final-lines; got {resp.status_code}"
        )

    # -----------------------------------------------------------------------
    # 11. Final-lines reject non-Locked/non-Archived periods
    # -----------------------------------------------------------------------

    async def test_11_final_lines_reject_draft_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        direct_db,
    ):
        """Final-lines returns 422 for a Draft period."""
        # Insert a Draft period or find an existing one (Draft slot is unique per branch).
        try:
            row = (await direct_db.execute(
                _sqla_text("""
                    INSERT INTO payroll.payrollperiods
                        (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                    VALUES (1, :bid, 'Draft', 'SM-DRAFT-2097', 'SM Draft 2097', 'Week', :start, :end)
                    RETURNING payrollperiodid
                """),
                {"bid": sm_hq_id,
                 "start": datetime.date(2097, 1, 6),
                 "end": datetime.date(2097, 1, 12)},
            )).mappings().first()
        except Exception:
            await direct_db.rollback()
            row = None
        if row is None:
            row = (await direct_db.execute(
                _sqla_text(
                    "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE companyid = 1 AND branchid = :bid AND status = 'Draft' LIMIT 1"
                ),
                {"bid": sm_hq_id},
            )).mappings().first()
        pid = row["payrollperiodid"]

        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422, (
            f"Draft period must return 422 for final-lines; got {resp.status_code}"
        )

    async def test_11_final_lines_reject_open_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_period_id: int,
    ):
        """Final-lines returns 422 for an Open period."""
        resp = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}/final-lines",
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422, (
            f"Open period must return 422 for final-lines; got {resp.status_code}"
        )

    # -----------------------------------------------------------------------
    # 12. Driver/ODA cannot access final-lines
    # -----------------------------------------------------------------------

    async def test_12_driver_sb_blocked_from_final_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
    ):
        """Driver+SpecificBranch cannot access final-lines."""
        tok = await _make_driver_sb_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}/final-lines", headers=_hdr(tok)
        )
        assert resp.status_code == 403

    async def test_12_oda_blocked_from_final_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_driver_role_id: int,
        sm_hq_period_id: int,
    ):
        """ODA-scope user cannot access final-lines."""
        tok = await _make_oda_user(
            session_client, auth_token, sm_driver_role_id, sm_hq_id
        )
        resp = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}/final-lines", headers=_hdr(tok)
        )
        assert resp.status_code == 403

    # -----------------------------------------------------------------------
    # 12b. Reachable legacy Period Pay route: ODA, permission, and branch scope
    # -----------------------------------------------------------------------

    async def test_12b_period_pay_oda_cannot_read_or_mutate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_paytest_id: int,
        sm_paytest_driver_id: int,
        sm_paytest_period_id: int,
        sm_driver_role_id: int,
        sm_adjustment_activated: None,
    ):
        """ODA is blocked across GET/POST/PATCH/DELETE without data leakage."""
        created = await session_client.post(
            f"/payroll/periods/{sm_paytest_period_id}/period-pay",
            json={
                "driver_id": sm_paytest_driver_id,
                "line_type": "ADJUSTMENT",
                "amount": "51.00",
            },
            headers=_hdr(auth_token),
        )
        assert created.status_code == 201, created.text
        line_id = created.json()["draft_line_id"]

        oda_token = await _make_oda_user(
            session_client, auth_token, sm_driver_role_id, sm_paytest_id
        )
        headers = _hdr(oda_token)
        attempts = [
            await session_client.get(
                f"/payroll/periods/{sm_paytest_period_id}/period-pay", headers=headers
            ),
            await session_client.post(
                f"/payroll/periods/{sm_paytest_period_id}/period-pay",
                json={
                    "driver_id": sm_paytest_driver_id,
                    "line_type": "ADJUSTMENT",
                    "amount": "52.00",
                },
                headers=headers,
            ),
            await session_client.patch(
                f"/payroll/periods/{sm_paytest_period_id}/period-pay/{line_id}",
                json={"notes": "ODA must not update"},
                headers=headers,
            ),
            await session_client.delete(
                f"/payroll/periods/{sm_paytest_period_id}/period-pay/{line_id}",
                headers=headers,
            ),
        ]
        for response in attempts:
            assert response.status_code == 403, response.text
            assert str(line_id) not in response.text

        visible = await session_client.get(
            f"/payroll/periods/{sm_paytest_period_id}/period-pay",
            headers=_hdr(auth_token),
        )
        assert visible.status_code == 200
        current = next(x for x in visible.json() if x["draft_line_id"] == line_id)
        assert current["calculated_amount"] == "51.0000"
        assert current["status"] == "Active"

    async def test_12b_period_pay_all_company_can_read_and_write(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_paytest_id: int,
        sm_paytest_driver_id: int,
        sm_paytest_period_id: int,
        sm_adjustment_activated: None,
    ):
        """An AllCompanyBranches user with payroll permissions can use the route."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, f"SM Period Pay AllCompany {_uid()}",
            ["payroll.view", "payroll.entry"],
        )
        user = await _create_user(session_client, auth_token, f"sm_pp_all_{_uid()}")
        await _assign_role(session_client, auth_token, user["user_id"], role_id)
        token = await _login(session_client, user["username"])

        read = await session_client.get(
            f"/payroll/periods/{sm_paytest_period_id}/period-pay",
            headers=_hdr(token),
        )
        assert read.status_code == 200, read.text
        write = await session_client.post(
            f"/payroll/periods/{sm_paytest_period_id}/period-pay",
            json={
                "driver_id": sm_paytest_driver_id,
                "line_type": "ADJUSTMENT",
                "amount": "53.00",
            },
            headers=_hdr(token),
        )
        assert write.status_code == 201, write.text

    async def test_12b_period_pay_requires_payroll_entry(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_paytest_id: int,
        sm_paytest_driver_id: int,
        sm_paytest_period_id: int,
        sm_adjustment_activated: None,
    ):
        """Read permission alone does not authorize a Period Pay mutation."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, f"SM Period Pay ViewOnly {_uid()}",
            ["payroll.view"],
        )
        user = await _create_user(session_client, auth_token, f"sm_pp_view_{_uid()}")
        await _assign_role(session_client, auth_token, user["user_id"], role_id)
        token = await _login(session_client, user["username"])

        response = await session_client.post(
            f"/payroll/periods/{sm_paytest_period_id}/period-pay",
            json={
                "driver_id": sm_paytest_driver_id,
                "line_type": "ADJUSTMENT",
                "amount": "54.00",
            },
            headers=_hdr(token),
        )
        assert response.status_code == 403, response.text

    async def test_12b_period_pay_specific_branch_cannot_cross_branch(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_paytest_id: int,
        sm_hq_period_id: int,
        sm_hq_driver_id: int,
        direct_db,
    ):
        """SpecificBranch scope protects all four legacy Period Pay operations."""
        from sqlalchemy import text as _text

        result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid,
                     workdate, linetype, linescope, quantity, calculatedamount,
                     sourcetype, status, needsmanagerreview, addedbyuserid)
                VALUES
                    (1, :bid, :pid, :did, NULL, 'ADJUSTMENT', 'Period', 1,
                     61.00, 'Manual', 'Active', FALSE,
                     (SELECT userid FROM sec.users WHERE username = 'admin' LIMIT 1))
                RETURNING draftlineid
            """),
            {"bid": sm_hq_id, "pid": sm_hq_period_id, "did": sm_hq_driver_id},
        )
        line_id = result.scalar_one()

        role_id = await _create_role_with_perms(
            session_client, auth_token, f"SM Period Pay Branch {_uid()}",
            ["payroll.view", "payroll.entry"],
        )
        user = await _create_user(session_client, auth_token, f"sm_pp_branch_{_uid()}")
        await _assign_role(
            session_client, auth_token, user["user_id"], role_id,
            scope="SpecificBranch", branch_id=sm_paytest_id,
        )
        token = await _login(session_client, user["username"])
        headers = _hdr(token)

        attempts = [
            await session_client.get(
                f"/payroll/periods/{sm_hq_period_id}/period-pay", headers=headers
            ),
            await session_client.post(
                f"/payroll/periods/{sm_hq_period_id}/period-pay",
                json={
                    "driver_id": sm_hq_driver_id,
                    "line_type": "ADJUSTMENT",
                    "amount": "62.00",
                },
                headers=headers,
            ),
            await session_client.patch(
                f"/payroll/periods/{sm_hq_period_id}/period-pay/{line_id}",
                json={"notes": "cross-branch"}, headers=headers,
            ),
            await session_client.delete(
                f"/payroll/periods/{sm_hq_period_id}/period-pay/{line_id}",
                headers=headers,
            ),
        ]
        for response in attempts:
            assert response.status_code in (403, 404), response.text

        row = (await direct_db.execute(
            _text("SELECT status, calculatedamount FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
            {"lid": line_id},
        )).mappings().one()
        assert row["status"] == "Active"
        assert str(row["calculatedamount"]) == "61.0000"

    # -----------------------------------------------------------------------
    # 13. Manual ADJUSTMENT period-pay is rejected
    # -----------------------------------------------------------------------

    async def test_13_adjustment_period_pay_supported_canonical(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_paytest_id: int,
        sm_paytest_driver_id: int,
        sm_paytest_period_id: int,
    ):
        """Active canonical ADJUSTMENT remains supported on the legacy route."""
        resp = await session_client.post(
            f"/payroll/periods/{sm_paytest_period_id}/period-pay",
            json={
                "driver_id": sm_paytest_driver_id,
                "line_type": "ADJUSTMENT",
                "amount": "50.00",
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["line_type"] == "ADJUSTMENT"

    async def test_13_adjustment_period_pay_supported_legacy(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_paytest_id: int,
        sm_paytest_driver_id: int,
        sm_paytest_period_id: int,
    ):
        """The legacy Adjustment spelling normalizes to canonical ADJUSTMENT."""
        resp = await session_client.post(
            f"/payroll/periods/{sm_paytest_period_id}/period-pay",
            json={
                "driver_id": sm_paytest_driver_id,
                "line_type": "Adjustment",
                "amount": "50.00",
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["line_type"] == "ADJUSTMENT"

    async def test_13_bonus_period_pay_is_retired(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_paytest_id: int,
        sm_paytest_driver_id: int,
        sm_paytest_period_id: int,
        sm_bonus_activated: None,
    ):
        """BONUS must use the canonical Bonus Events API, not period-pay."""
        resp = await session_client.post(
            f"/payroll/periods/{sm_paytest_period_id}/period-pay",
            json={
                "driver_id": sm_paytest_driver_id,
                "line_type": "BONUS",
                "amount": "100.00",
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422, (
            f"BONUS period-pay must be rejected; got {resp.status_code}: {resp.text}"
        )
        assert "bonus events" in resp.json()["detail"].lower()

    # -----------------------------------------------------------------------
    # 11b. Final-lines accepted for Locked period
    #      Runs AFTER test_13 so sm_paytest_period_id is still Open here;
    #      we advance it through the full lifecycle to Locked.
    # -----------------------------------------------------------------------

    async def test_11b_final_lines_accepted_for_locked_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_paytest_id: int,
        sm_paytest_driver_id: int,
        sm_paytest_period_id: int,
        sm_bonus_activated: None,
        direct_db,
    ):
        """Final-lines returns 200 for a Locked period.

        Advances sm_paytest_period_id: Open→InReview→Approved→Locked.
        Must run after test_13_* (which rely on the period being Open).
        """
        from sqlalchemy import text as _sqla_text
        pid = sm_paytest_period_id

        # Ensure no stale InReview period blocks the submit (CP-1B slot conflict).
        # InReview→Cancelled is blocked by CP-1A via API; use direct DB.
        await direct_db.execute(
            _sqla_text("""
                UPDATE payroll.payrollperiods
                SET    status = 'Cancelled', currentreturnreviewitemid = NULL
                WHERE  branchid = :bid AND status = 'InReview'
                  AND  payrollperiodid != :pid
            """),
            {"bid": sm_paytest_id, "pid": pid},
        )
        await direct_db.commit()

        # Add a draft line so the period is non-empty for finalization.
        # Use DailyNote (informational, no rate required) to avoid rate setup dependencies.
        line_r = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id": sm_paytest_driver_id,
                "work_date": "2096-01-15",
                "line_type": "DailyNote",
                "quantity": 1,
                "notes": "filler",
            },
            headers=_hdr(auth_token),
        )
        assert line_r.status_code == 201, f"Add line failed: {line_r.text}"

        # Open → InReview
        ri = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=_hdr(auth_token),
        )
        assert ri.status_code == 200, f"Cannot submit for review: {ri.text}"

        # Approve the review item
        items = await session_client.get(
            "/review/items",
            params={"branch_id": sm_paytest_id},
            headers=_hdr(auth_token),
        )
        period_item = next(
            (it for it in items.json() if it.get("entity_id") == str(pid)),
            None,
        )
        if period_item:
            await session_client.post(
                f"/review/items/{period_item['review_item_id']}/decide",
                json={"decision": "Approved", "reason": "SM11b test"},
                headers=_hdr(auth_token),
            )

        # Finalize (Approved → Locked)
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=_hdr(auth_token),
        )
        assert fin.status_code == 200, f"Cannot finalize: {fin.text}"

        # Final-lines on Locked period must return 200
        resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200, (
            f"Locked period must return 200 for final-lines; got {resp.status_code}: {resp.text}"
        )

    # -----------------------------------------------------------------------
    # 15. The pre-seeded branch_user (PAYROLL_VIEWER_CO + SpecificBranch=HQ)
    #     can read HQ data but cannot write (no payroll.entry).
    # -----------------------------------------------------------------------

    async def test_15_conftest_branch_user_read_only(
        self,
        session_client: httpx.AsyncClient,
        sm_hq_period_id: int,
        sm_hq_id: int,
    ):
        """
        The conftest 'branch_user' has PAYROLL_VIEWER_CO company role (payroll.view)
        + SpecificBranch=HQ scope.  Verify it can read HQ payroll data but cannot
        create or transition periods (no payroll.entry).
        """
        resp = await session_client.post("/auth/login", json={
            "username": "branch_user",
            "password": "TestPass123!",
            "company_code": "DEMO",
        })
        assert resp.status_code == 200, f"Login failed: {resp.text}"
        tok = resp.json()["access_token"]

        # Period list on HQ: allowed (payroll.view passes driver check + permission)
        r1 = await session_client.get(
            "/payroll/periods",
            params={"branch_id": sm_hq_id},
            headers=_hdr(tok),
        )
        assert r1.status_code == 200, (
            f"branch_user (PAYROLL_VIEWER_CO+SpecificBranch=HQ) must be able to list HQ periods; "
            f"got {r1.status_code}: {r1.text}"
        )

        # Period detail on HQ: allowed
        r2 = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}", headers=_hdr(tok)
        )
        assert r2.status_code == 200, (
            f"branch_user must be able to read HQ period detail; got {r2.status_code}"
        )

        # Review list: allowed (payroll.view satisfies review read gate)
        r3 = await session_client.get("/review/items", headers=_hdr(tok))
        assert r3.status_code == 200, (
            f"branch_user must be able to list review items; got {r3.status_code}"
        )

        # Candidate preview requires payroll.period.create, which this read-only
        # role does not have.
        r4 = await session_client.get(
            f"/payroll/branches/{sm_hq_id}/period-candidates",
            params={"mode": "OPEN_CREATION"},
            headers=_hdr(tok),
        )
        assert r4.status_code == 403, (
            f"branch_user must be blocked from creating periods (no payroll.entry); "
            f"got {r4.status_code}"
        )
        assert "permission" in r4.json()["detail"].lower(), (
            f"Write denial must cite missing permission; got: {r4.json()['detail']}"
        )


# ---------------------------------------------------------------------------
# Phase 1.3 — P0 #1: legacy ODA rows (companyroleId IS NULL) are blocked
# ---------------------------------------------------------------------------

class TestLegacyODABlock:
    """
    Verify that _require_not_driver_role catches legacy sec.userbranchroles rows
    where companyroleId IS NULL and scopetype = 'OwnDriverDataOnly'.

    These rows cannot be created via the API (the API always sets a companyroleId),
    so they are injected directly via the direct_db fixture.
    """

    async def test_legacy_oda_null_companyrole_blocked_from_period_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        sm_hq_period_id: int,
        direct_db,
    ):
        """
        A user with a legacy ODA row (companyroleId IS NULL, scopetype='OwnDriverDataOnly')
        must be blocked from all payroll operational endpoints with 403.

        Note: OwnDriverDataOnly scope MUST have a branchid (DB constraint).
        The "legacy" aspect is that companyroleId IS NULL — not branchid IS NULL.
        """
        from sqlalchemy import text as _text

        uname = _uid()
        user_resp = await session_client.post(
            "/admin/users",
            json={"username": uname, "display_name": uname,
                  "password": "TestPass1234!", "is_active": True,
                  "can_login": True, "must_change_password": False},
            headers=_hdr(auth_token),
        )
        assert user_resp.status_code == 201, f"Create user: {user_resp.text}"

        # Inject a legacy ODA row: companyroleId IS NULL, scopetype='OwnDriverDataOnly'
        # branchid must be set (DB constraint); the legacy aspect is NULL companyroleId.
        # roleid is set to the global PAYROLL_ADMIN role so the user can log in —
        # the real legacy case is roleid set (old global role) + companyroleId NOT YET backfilled.
        await direct_db.execute(
            _text("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, companyroleid, scopetype, isactive)
                SELECT u.userid, u.companyid, :bid, r.roleid, NULL,
                       'OwnDriverDataOnly', TRUE
                FROM sec.users u, sec.roles r
                WHERE u.username = :uname AND r.rolecode = 'PAYROLL_ADMIN'
                ON CONFLICT DO NOTHING
            """),
            {"uname": uname, "bid": sm_hq_id},
        )

        tok = await _login(session_client, uname)

        # Period list must be 403 — legacy ODA row should be caught by LEFT JOIN check
        r1 = await session_client.get(
            "/payroll/periods",
            params={"branch_id": sm_hq_id},
            headers=_hdr(tok),
        )
        assert r1.status_code == 403, (
            f"Legacy ODA user (companyroleId=NULL) must be blocked from period list; "
            f"got {r1.status_code}: {r1.text}"
        )
        assert "driver" in r1.json()["detail"].lower(), (
            f"403 detail must mention driver; got: {r1.json()['detail']}"
        )

        # Period detail must also be 403
        r2 = await session_client.get(
            f"/payroll/periods/{sm_hq_period_id}",
            headers=_hdr(tok),
        )
        assert r2.status_code == 403, (
            f"Legacy ODA user must be blocked from period detail; got {r2.status_code}"
        )

        # Review list must also be 403
        r3 = await session_client.get("/review/items", headers=_hdr(tok))
        assert r3.status_code == 403, (
            f"Legacy ODA user must be blocked from review list; got {r3.status_code}"
        )

    async def test_legacy_oda_user_cannot_create_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        sm_hq_id: int,
        direct_db,
    ):
        """
        A legacy ODA user cannot create a payroll period even if they somehow
        have payroll.entry assigned (driver hard-block runs before permission check).
        """
        from sqlalchemy import text as _text

        uname = _uid()
        user_resp = await session_client.post(
            "/admin/users",
            json={"username": uname, "display_name": uname,
                  "password": "TestPass1234!", "is_active": True,
                  "can_login": True, "must_change_password": False},
            headers=_hdr(auth_token),
        )
        assert user_resp.status_code == 201

        # Give the user a PAYROLL_ADMIN role (payroll.entry) AND a legacy ODA row
        await direct_db.execute(
            _text("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, companyroleid, scopetype, isactive)
                SELECT u.userid, u.companyid, NULL, r.roleid, NULL,
                       'AllCompanyBranches', TRUE
                FROM sec.users u, sec.roles r
                WHERE u.username = :uname AND r.rolecode = 'PAYROLL_ADMIN'
                ON CONFLICT DO NOTHING
            """),
            {"uname": uname},
        )
        await direct_db.execute(
            _text("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, companyroleid, scopetype, isactive)
                SELECT u.userid, u.companyid, :bid, r.roleid, NULL,
                       'OwnDriverDataOnly', TRUE
                FROM sec.users u, sec.roles r
                WHERE u.username = :uname AND r.rolecode = 'PAYROLL_ADMIN'
                ON CONFLICT DO NOTHING
            """),
            {"uname": uname, "bid": sm_hq_id},
        )

        tok = await _login(session_client, uname)

        r = await session_client.get(
            f"/payroll/branches/{sm_hq_id}/period-candidates",
            params={"mode": "OPEN_CREATION"},
            headers=_hdr(tok),
        )
        assert r.status_code == 403, (
            f"Legacy ODA user must be blocked from candidate creation even with payroll.entry; "
            f"got {r.status_code}: {r.text}"
        )
        assert "driver" in r.json()["detail"].lower(), (
            f"403 detail must mention driver; got: {r.json()['detail']}"
        )

    async def test_operational_user_can_create_open_candidate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
        test_database_url: str,
    ):
        """
        An operational role without ODA/DRIVER or Setup permissions can create
        an Open period on its assigned branch through the candidate workflow.
        """
        uname = _uid()
        # Grant both payroll.period.create (create gate, migration 0030) and
        # payroll.entry (entry/open/submit gate), but no payroll_setup.* grant.
        op_role_id = await _create_role_with_perms(
            session_client, auth_token,
            f"SM_ENTRY_{uname}",
            ["payroll.period.create", "payroll.entry"],
        )
        user = await _create_user(session_client, auth_token, uname)
        uid = user["user_id"]
        # Assign role BEFORE login — login gate requires at least one active role
        await _assign_role(session_client, auth_token, uid, op_role_id)
        me = await session_client.post("/auth/login", json={
            "username": uname, "password": "TestPass1234!", "company_code": "DEMO",
        })
        assert me.status_code == 200, f"Login failed: {me.text}"
        tok = me.json()["access_token"]

        branch_code = f"SM-CAND-{uname}"
        branch_id = (await direct_db.execute(
            _sqla_text("""
                INSERT INTO core.branches
                    (companyid, branchcode, branchname, status, isdefault)
                SELECT companyid, :code, :name, 'Active', FALSE
                FROM core.companies WHERE companycode = 'DEMO'
                RETURNING branchid
            """),
            {"code": branch_code, "name": f"Candidate gate {uname}"},
        )).scalar_one()
        anchor = datetime.date(2099, 4, 1)
        engine = create_async_engine(test_database_url, echo=False)
        try:
            async with engine.begin() as db:
                admin_id = (await db.execute(_sqla_text("""
                    SELECT userid FROM sec.users
                    WHERE companyid = 1 AND username = 'admin'
                """))).scalar_one()
                setup_id = await create_setup(
                    1, admin_id, branch_code, "Operational candidate gate", db,
                )
                draft_id = await create_draft(
                    1, admin_id, setup_id, db,
                    payroll_frequency="Week", anchor_start_date=anchor,
                    normal_days_off_mask=0,
                )
                await publish_version(1, admin_id, setup_id, draft_id, anchor, db)
                await assign_setup(1, admin_id, branch_id, setup_id, anchor, db)
        finally:
            await engine.dispose()

        preview = await session_client.get(
            f"/payroll/branches/{branch_id}/period-candidates",
            params={"mode": "OPEN_CREATION"},
            headers=_hdr(tok),
        )
        assert preview.status_code == 200, preview.text
        selected = preview.json()["selected"]
        assert selected["creatable"] is True

        created = await session_client.post(
            f"/payroll/branches/{branch_id}/period-creations",
            json={"candidate_key": selected["candidate_key"]},
            headers=_hdr(tok),
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "Open"
        assert created.json()["branch_id"] == branch_id
