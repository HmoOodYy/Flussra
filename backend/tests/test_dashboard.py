"""
Dashboard D1 tests — permission-aware dashboard foundation.

Test matrix:
  A. Access control
     1.  payroll.entry admin can access dashboard (regression — existing user).
     2.  review.decide-only user can access dashboard and gets review_queue section.
     3.  payrates.view-only user can access dashboard and gets rates_health section.
     4.  setup.manage-only user can access dashboard and gets setup_health section.
     5.  Driver/ODA user cannot access operational dashboard → 403.
     6.  payroll.view SpecificBranch user can access dashboard.

  B. Section gating
     7.  review.decide user: sections_available includes review_queue, NOT payroll_ops.
     8.  payroll.finalize user: sections_available includes approved_periods.
     9.  payroll.finalize user sees approved_periods list when Approved periods exist.
     10. Non-finalize user (payroll.view) does NOT see approved_periods section.
     11. payrates.view user: sections_available includes rates_health.
     12. setup.manage user: sees setup_health section and setup_warnings list.

  C. Branch scope
     13. AllCompanyBranches admin sees multiple branches in branch_summaries.
     14. SpecificBranch payroll.view user sees only their branch in branch_summaries.
     15. review.decide user gets review counts (branch-scoped).

  D. Regression / field presence
     16. All original DashboardResponse fields present for admin.
     17. payroll.view only user: pending_transfers=None, pending_rates=None.
     18. Admin (all perms): pending_transfers is int, transfers section available.
"""
import itertools
import pytest
import pytest_asyncio
import httpx
from uuid import uuid4

_counter = itertools.count(500)


def _u() -> str:
    return f"db{next(_counter):04d}"


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, username, password="TestPass1234!"):
    r = await client.post("/auth/login", json={
        "username": username, "password": password, "company_code": "DEMO",
    })
    assert r.status_code == 200, f"Login failed for {username!r}: {r.text}"
    return r.json()["access_token"]


async def _create_user(client, admin_token, username):
    r = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": username,
            "password": "TestPass1234!",
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=_hdr(admin_token),
    )
    assert r.status_code == 201, f"Create user {username!r}: {r.text}"
    return r.json()["user_id"]


async def _create_company_role(client, admin_token, role_name, perm_codes: list[str]) -> int:
    """Create a company role and set its permissions. Return company_role_id."""
    # 1. Create the role (role_code auto-generated)
    r = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=_hdr(admin_token),
    )
    assert r.status_code == 201, f"Create company role {role_name!r}: {r.text}"
    cr_id = r.json()["company_role_id"]

    # 2. Set permissions (PUT replaces all)
    r2 = await client.put(
        f"/admin/company-roles/{cr_id}/permissions",
        json={"permission_codes": perm_codes},
        headers=_hdr(admin_token),
    )
    assert r2.status_code == 200, f"Set perms for role {cr_id}: {r2.text}"
    return cr_id


async def _assign_company_role(
    client, admin_token, user_id, company_role_id, scope_type, branch_id=None
):
    """Assign a company role to a user via the new-path endpoint."""
    payload: dict = {"company_role_id": company_role_id, "scope_type": scope_type}
    if branch_id is not None:
        payload["branch_id"] = branch_id
    r = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=payload,
        headers=_hdr(admin_token),
    )
    assert r.status_code in (200, 201), f"Assign company role: {r.text}"


async def _make_user_with_perms(
    client,
    admin_token: str,
    perm_codes: list[str],
    scope_type: str,
    branch_id: int | None = None,
) -> tuple[str, str]:
    """
    Create a user, give them a fresh company role with exactly `perm_codes`.
    Returns (username, token).
    """
    username = _u()
    uid = await _create_user(client, admin_token, username)
    cr_id = await _create_company_role(
        client, admin_token, f"TestRole_{username}", perm_codes
    )
    await _assign_company_role(client, admin_token, uid, cr_id, scope_type, branch_id)
    token = await _login(client, username)
    return username, token


# ---------------------------------------------------------------------------
# A. Access control
# ---------------------------------------------------------------------------

class TestDashboardAccess:

    async def test_payroll_entry_admin_can_access_dashboard(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Regression: existing admin (payroll.entry + all perms) gets 200."""
        r = await session_client.get("/dashboard", headers=_hdr(auth_token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "sections_available" in data
        assert "payroll_ops" in data["sections_available"]

    async def test_review_decide_only_user_can_access_dashboard(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """User with only review.decide gets 200 and sees review_queue section."""
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["review.decide"], "SpecificBranch", hq_branch_id,
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "review_queue" in data["sections_available"]

    async def test_payrates_view_only_user_can_access_dashboard(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """User with only payrates.view gets 200 and sees rates_health section."""
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["payrates.view"], "SpecificBranch", hq_branch_id,
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "rates_health" in data["sections_available"]
        assert "payroll_ops" not in data["sections_available"]

    async def test_setup_manage_only_user_can_access_dashboard(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """User with only setup.manage (AllCompanyBranches) gets 200 and setup_health."""
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["setup.manage"], "AllCompanyBranches",
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "setup_health" in data["sections_available"]

    async def test_driver_oda_user_cannot_access_dashboard(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """ODA driver user gets 403 from the operational dashboard."""
        username = _u()
        uid = await _create_user(session_client, auth_token, username)

        # Find the DRIVER company role
        cr_resp = await session_client.get("/admin/company-roles", headers=_hdr(auth_token))
        assert cr_resp.status_code == 200
        driver_cr = next((r for r in cr_resp.json() if r["role_code"] == "DRIVER"), None)
        assert driver_cr, "DRIVER company role not found"

        await _assign_company_role(
            session_client, auth_token, uid,
            driver_cr["company_role_id"], "OwnDriverDataOnly", hq_branch_id,
        )
        token = await _login(session_client, username)

        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 403, r.text

    async def test_payroll_view_specific_branch_user_can_access_dashboard(
        self,
        session_client: httpx.AsyncClient,
        branch_user_token: str,
    ):
        """branch_user (payroll.view, SpecificBranch HQ) can access dashboard."""
        r = await session_client.get("/dashboard", headers=_hdr(branch_user_token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "payroll_ops" in data["sections_available"]


# ---------------------------------------------------------------------------
# B. Section gating
# ---------------------------------------------------------------------------

class TestSectionGating:

    async def test_review_only_user_no_payroll_ops_section(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """review.decide-only user: review_queue in sections, payroll_ops NOT."""
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["review.decide"], "SpecificBranch", hq_branch_id,
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "review_queue" in data["sections_available"]
        assert "payroll_ops" not in data["sections_available"]

    async def test_finalize_user_gets_approved_periods_section(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """payroll.finalize user gets approved_periods section in sections_available."""
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["payroll.finalize", "payroll.view"], "AllCompanyBranches",
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "approved_periods" in data["sections_available"]
        assert isinstance(data["approved_periods"], list)

    async def test_finalize_user_sees_approved_periods_when_present(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
    ):
        """
        Admin creates a Draft period, then uses direct_db to set it to Approved
        (bypassing the multi-step review workflow) to prove that the dashboard
        approved_periods list includes it for a payroll.finalize user.
        Cleans up by cancelling the period via direct_db.
        """
        from sqlalchemy import text as _text

        branch_id = int((await direct_db.execute(
            _text("""
                INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
                VALUES (1, :code, 'Dashboard isolated', 'Active', FALSE)
                RETURNING branchid
            """), {"code": f"DASH_{uuid4().hex}"}
        )).scalar_one())

        # The legacy POST period factory requires one existing Open period.
        # Seed that prerequisite explicitly; this test exercises dashboard
        # visibility of an Approved period.
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname,
                     periodtype, startdate, enddate)
                VALUES
                    (1, :bid, 'Open', :code, 'Dashboard Open Seed',
                     'Week', '2055-01-25', '2055-01-31')
            """),
            {"bid": branch_id, "code": f"DASH-OPEN-{_u()}"},
        )

        pid = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname,
                     periodtype, startdate, enddate)
                VALUES
                    (1, :bid, 'Draft', :code, 'Dashboard Draft Seed',
                     'Week', '2055-02-01', '2055-02-07')
                RETURNING payrollperiodid
            """),
            {"bid": branch_id, "code": f"DASH-DRAFT-{_u()}"},
        )).scalar_one()

        try:
            # Set status directly to Approved via AUTOCOMMIT direct_db connection.
            await direct_db.execute(
                _text("UPDATE payroll.payrollperiods SET status='Approved' WHERE payrollperiodid=:pid"),
                {"pid": pid},
            )

            _, fin_token = await _make_user_with_perms(
                session_client, auth_token,
                ["payroll.finalize", "payroll.view"], "AllCompanyBranches",
            )
            dash_r = await session_client.get("/dashboard", headers=_hdr(fin_token))
            assert dash_r.status_code == 200, dash_r.text
            data = dash_r.json()
            assert "approved_periods" in data["sections_available"]
            period_ids = [p["period_id"] for p in data["approved_periods"]]
            assert pid in period_ids, (
                f"Period {pid} not in approved_periods: {period_ids}"
            )

        finally:
            # Cancel the period so it does not linger in the test DB
            await direct_db.execute(
                _text("UPDATE payroll.payrollperiods SET status='Cancelled' WHERE payrollperiodid=:pid"),
                {"pid": pid},
            )
            await direct_db.execute(
                _text("UPDATE payroll.payrollperiods SET status='Cancelled' "
                      "WHERE branchid=:bid AND status='Open'"),
                {"bid": branch_id},
            )

    async def test_non_finalize_user_no_approved_periods_section(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """payroll.view only user does NOT see approved_periods section."""
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["payroll.view"], "SpecificBranch", hq_branch_id,
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "approved_periods" not in data["sections_available"]

    async def test_payrates_view_user_gets_rates_health_section(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """payrates.view user: rates_health in sections; pending_rates is an int."""
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["payrates.view"], "SpecificBranch", hq_branch_id,
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "rates_health" in data["sections_available"]
        assert data["pending_rates"] is not None
        assert isinstance(data["pending_rates"], int)

    async def test_setup_manage_user_sees_setup_health_and_warnings(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """setup.manage user sees setup_health section; setup_warnings is a list."""
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["setup.manage"], "AllCompanyBranches",
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "setup_health" in data["sections_available"]
        assert isinstance(data["setup_warnings"], list)

    async def test_setup_manage_user_no_payroll_ops(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """setup.manage only user does NOT see payroll_ops section."""
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["setup.manage"], "AllCompanyBranches",
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "payroll_ops" not in data["sections_available"]


# ---------------------------------------------------------------------------
# C. Branch scope
# ---------------------------------------------------------------------------

class TestBranchScope:

    async def test_all_branches_admin_sees_multiple_branch_summaries(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """AllCompanyBranches admin sees both HQ and PAYTEST in branch_summaries."""
        r = await session_client.get("/dashboard", headers=_hdr(auth_token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["scope"] == "AllCompanyBranches"
        assert len(data["branch_summaries"]) >= 2

    async def test_specific_branch_user_sees_only_their_branch(
        self,
        session_client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        """SpecificBranch user (HQ only) sees only HQ in branch_summaries."""
        r = await session_client.get("/dashboard", headers=_hdr(branch_user_token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["scope"] == "Branch"
        for summary in data["branch_summaries"]:
            assert summary["branch_id"] == hq_branch_id, (
                f"Got branch_id={summary['branch_id']} but expected HQ ({hq_branch_id})"
            )

    async def test_review_user_branch_scoped_review_counts(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        review.decide user on HQ branch: review_pending is an int (branch filter works).
        We can't assert an exact count, but verify the field is present and non-negative.
        """
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["review.decide"], "SpecificBranch", hq_branch_id,
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data["review_pending"], int)
        assert data["review_pending"] >= 0
        assert isinstance(data["review_edit_requested"], int)


# ---------------------------------------------------------------------------
# D. Regression / field presence
# ---------------------------------------------------------------------------

class TestDashboardRegression:

    async def test_admin_dashboard_has_all_required_fields(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Regression: all DashboardResponse fields present for all-perms admin."""
        r = await session_client.get("/dashboard", headers=_hdr(auth_token))
        assert r.status_code == 200, r.text
        data = r.json()

        required_fields = [
            "generated_at", "scope", "sections_available",
            "periods_draft", "periods_open", "periods_in_review",
            "periods_approved", "periods_locked",
            "approved_periods",
            "review_pending", "review_edit_requested",
            "active_drivers", "pending_transfers", "pending_rates",
            "last_finalized_period", "setup_warnings", "branch_summaries",
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field!r}"

    async def test_payroll_entry_user_period_counts_are_non_negative_ints(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Period counts are non-negative integers for admin."""
        r = await session_client.get("/dashboard", headers=_hdr(auth_token))
        assert r.status_code == 200, r.text
        data = r.json()
        for key in (
            "periods_draft", "periods_open", "periods_in_review",
            "periods_approved", "periods_locked",
        ):
            assert isinstance(data[key], int), f"{key} is not int"
            assert data[key] >= 0, f"{key} is negative"

    async def test_admin_gets_transfers_section_and_int_pending_transfers(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Admin (all perms incl. drivers.manage) gets transfers section and int count."""
        r = await session_client.get("/dashboard", headers=_hdr(auth_token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert "transfers" in data["sections_available"]
        assert isinstance(data["pending_transfers"], int)
        assert data["pending_transfers"] >= 0

    async def test_payroll_view_only_user_null_transfers_and_rates(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        payroll.view only user (no drivers.*, no payrates.*) gets:
          - pending_transfers = None (section not available)
          - pending_rates = None (section not available)
        """
        _, token = await _make_user_with_perms(
            session_client, auth_token,
            ["payroll.view"], "SpecificBranch", hq_branch_id,
        )
        r = await session_client.get("/dashboard", headers=_hdr(token))
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["pending_transfers"] is None, (
            f"expected pending_transfers=None, got {data['pending_transfers']}"
        )
        assert data["pending_rates"] is None, (
            f"expected pending_rates=None, got {data['pending_rates']}"
        )
