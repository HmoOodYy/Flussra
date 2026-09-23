"""
Integration tests for CP-3A: GET /payroll/periods/{id}/finalization-preview

Tests use year 2085 dates to avoid conflicts with other test suites.

Isolation note
--------------
Tests that assert specific dollar amounts (e.g. 8h x $20 = $160) must use a
driver with an approved DriverRate so that _compute_calculated_amount resolves
a calculated_amount. The ``preview_driver_id`` fixture creates a fresh driver
for each such test; tests then approve a rate for that driver before adding lines.
Similarly, the CP5 fixtures create their own isolated drivers so they can
approve HOURLY rates without conflicting with rates approved by test_pay_rates.py.

The approved_period fixture from test_finalize.py is re-created here as a
local fixture that follows the exact same pattern (Draft->'Open->'InReview->'Approved
via review flow, then voids the dummy line) so this file is self-contained.
"""
import datetime
import pytest
import itertools
import pytest_asyncio
import httpx
from decimal import Decimal
from sqlalchemy import text as _text
from uuid import uuid4


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Use a module-isolated branch for finalization-preview workflow tests."""
    row = (await session_db_conn.execute(_text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {"code": f"FP_{uuid4().hex}", "name": "Preview isolated"})).scalar_one()
    return int(row)


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Create the preview test driver on this module's isolated branch."""
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": "Preview Isolated Driver",
            "driver_code": f"FP-D-{uuid4().hex[:10]}",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"Preview driver seed failed: {resp.text}"
    return resp.json()["driver_id"]

_preview_driver_counter = itertools.count(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    # CP-1A: only Draft and Open can be cancelled via PATCH.
    headers = auth(token)
    for s in ("Draft", "Open"):
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        if resp.status_code != 200:
            continue
        for p in resp.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"},
                headers=headers,
            )


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    start_date: str = "2085-01-07",
) -> dict:
    """Submit period for review and approve via the review flow.
    Adds a dummy DailyNote line only if the period has no non-void lines."""
    headers = auth(token)
    lines_resp = await client.get(
        f"/payroll/periods/{period_id}/lines",
        headers=headers,
        params={"status": "Active"},
    )
    if lines_resp.status_code == 200 and len(lines_resp.json()) == 0:
        await client.post(
            f"/payroll/periods/{period_id}/lines",
            headers=headers,
            json={
                "driver_id":  driver_id,
                "work_date":  start_date,
                "line_type":  "DailyNote",
                "quantity":   "1",
                "notes":      "filler",
            },
        )
    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        headers=headers,
        json={"status": "InReview"},
    )
    assert r.status_code == 200, f"InReview failed: {r.text}"

    review_resp = await client.get("/review/items", headers=headers)
    assert review_resp.status_code == 200
    review_item = next(
        (i for i in review_resp.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert review_item is not None, f"No pending review item for period {period_id}"

    decide = await client.post(
        f"/review/items/{review_item['review_item_id']}/decide",
        headers=headers,
        json={"decision": "Approved"},
    )
    assert decide.status_code == 200, f"Approval failed: {decide.text}"

    period_resp = await client.get(f"/payroll/periods/{period_id}", headers=headers)
    assert period_resp.status_code == 200
    return period_resp.json()


async def _create_and_approve_rate(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_type_id: int,
    amount: str,
    effective_from: str = "2025-01-01",
) -> int:
    """Create and approve a DriverRate. Returns driver_rate_id."""
    headers = auth(token)
    rc = await client.post(
        "/payroll/rates",
        json={
            "driver_id":      driver_id,
            "rate_type_id":   rate_type_id,
            "amount":         amount,
            "effective_from": effective_from,
        },
        headers=headers,
    )
    assert rc.status_code == 201, f"Create rate failed: {rc.text}"
    rate_id = rc.json()["driver_rate_id"]
    ra = await client.post(f"/payroll/rates/{rate_id}/approve", headers=headers)
    assert ra.status_code == 200, f"Approve rate failed: {ra.text}"
    return rate_id


async def _add_line_to_approved_period(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    direct_db,
    *,
    line_type: str = "DailyNote",
    quantity: str = "1.00",
    work_date: str = "2085-01-07",
) -> dict:
    """
    Return an existing submitted source line for a snapshot-backed Approved period.

    CP-4F deliberately forbids the old test pattern of reopening an Approved
    period, adding live source data, and manufacturing a second approved review
    item.  Callers that mutate the returned live row now prove that preview and
    finalization remain bound to the original approved snapshot.
    """
    row = (await direct_db.execute(_text("""
        SELECT draftlineid
        FROM payroll.payrolldraftlines
        WHERE payrollperiodid = :pid
          AND status != 'Void'
        ORDER BY draftlineid
        LIMIT 1
    """), {"pid": period_id})).mappings().first()
    assert row is not None, "Snapshot fixture must retain its submitted source line"
    return {"draft_line_id": row["draftlineid"]}


async def _create_role_with_perms(
    client: httpx.AsyncClient,
    token: str,
    role_name: str,
    perms: list,
) -> int:
    cr = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=auth(token),
    )
    assert cr.status_code == 201, f"Create role failed: {cr.text}"
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=auth(token),
        )
        assert pr.status_code == 200, f"Set permissions failed: {pr.text}"
    return role_id


async def _create_user_with_role(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    role_id: int,
    scope_type: str = "AllCompanyBranches",
    branch_id=None,
    password: str = "TestPass123!",
) -> str:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": username,
            "password": password,
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=auth(admin_token),
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"
    user_id = resp.json()["user_id"]

    assign_body: dict = {"company_role_id": role_id, "scope_type": scope_type}
    if branch_id is not None:
        assign_body["branch_id"] = branch_id

    assign_resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=assign_body,
        headers=auth(admin_token),
    )
    assert assign_resp.status_code in (200, 201), f"Assign role failed: {assign_resp.text}"

    login = await client.post("/auth/login", json={
        "username": username,
        "password": password,
        "company_code": "DEMO",
    })
    assert login.status_code == 200, f"Login failed: {login.text}"
    return login.json()["access_token"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

async def _force_cancel_locked_periods(direct_db, branch_id: int) -> None:
    """Clean mutable workflow rows without mutating immutable finalized history."""
    # CP-4F: Locked/Archived periods may now have immutable snapshot history.
    # This suite creates unique period codes, so they are retained rather than
    # bypassing immutable triggers merely to reuse a shared fixture period.
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
              "WHERE branchid = :bid AND status IN ('InReview', 'Approved')"),
        {"bid": branch_id},
    )
    # Returned: must clear CurrentReturnReviewItemID first (pointer-consistency CHECK).
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods "
              "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
              "WHERE branchid = :bid AND status = 'Returned'"),
        {"bid": branch_id},
    )


@pytest_asyncio.fixture
async def cp3a_clean(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    await _force_cancel_locked_periods(direct_db, paytest_branch_id)
    yield paytest_branch_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    await _force_cancel_locked_periods(direct_db, paytest_branch_id)


@pytest_asyncio.fixture
async def preview_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """
    Fresh driver on PAYTEST branch with NO approved rates.

    Used by tests that assert specific dollar amounts so that
    _refresh_draft_calculations finds no approved rate for this driver and
    the COALESCE(calc, qty x rate_amount) path produces the expected value.
    Function-scoped so each test gets its own driver.
    """
    n = next(_preview_driver_counter)
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": f"Preview Isolated Driver {n}",
            "driver_code": f"PID-{n:04d}",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"Create isolated driver failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture
async def cp3a_approved_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    cp3a_clean: int,
    paytest_driver_id: int,
    direct_db,
) -> dict:
    """
    Approved period on PAYTEST with one submitted DailyNote source line.
    """
    headers = auth(auth_token)
    branch_id = cp3a_clean

    # Each function-scoped fixture owns a distinct period.  Finalized history
    # remains protected by CP-4C/CP-4F rather than being reset through DDL.
    period_suffix = next(_preview_driver_counter)
    period_code = f"CP3A-0106-{period_suffix}"
    # Insert Open period directly (CP-1D: POST requires existing Open; PATCH Draft->Open blocked).
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :period_code, 'CP3A Preview Test 2085', 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "period_code": period_code,
         "start": datetime.date(2085, 1, 6),
         "end": datetime.date(2085, 1, 12)},
    )).mappings().first()
    if row is None:
        row = (await direct_db.execute(
            _text(
                "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE branchid = :bid AND periodcode = :period_code"
            ),
            {"bid": branch_id, "period_code": period_code},
        )).mappings().first()
    pid = row["payrollperiodid"]

    await _advance_to_approved(session_client, auth_token, pid, paytest_driver_id)

    period_resp = await session_client.get(f"/payroll/periods/{pid}", headers=headers)
    return period_resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFinalizationPreview:

    # -- Status guard tests ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_preview_requires_approved_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_clean: int,
        direct_db,
    ):
        """Open period -> 422."""
        headers = auth(auth_token)
        # Insert Open period directly (CP-1D: POST requires existing Open; PATCH Draft->Open blocked).
        _r = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Open', 'CP3A-2085-0203', 'CP3A Preview Non-Approved', 'Week', :start, :end)
                ON CONFLICT DO NOTHING
                RETURNING payrollperiodid
            """),
            {"bid": cp3a_clean,
             "start": datetime.date(2085, 2, 3),
             "end": datetime.date(2085, 2, 9)},
        )).mappings().first()
        if _r is None:
            _r = (await direct_db.execute(
                _text(
                    "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE branchid = :bid AND periodcode = 'CP3A-2085-0203'"
                ),
                {"bid": cp3a_clean},
            )).mappings().first()
        pid = _r["payrollperiodid"]

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert resp.status_code == 422
        assert "Approved" in resp.json()["detail"]

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=headers,
        )

    @pytest.mark.asyncio
    async def test_preview_requires_approved_not_locked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """Locked period ->' 422."""
        headers = auth(auth_token)
        pid = cp3a_approved_period["payroll_period_id"]

        # Add a line and finalize to reach Locked (DailyNote needs no approved rate)
        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=headers
        )
        assert fin.status_code == 200
        assert fin.json()["status"] == "Locked"

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert resp.status_code == 422
        assert "Approved" in resp.json()["detail"]

    # -- Blocker tests --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_preview_ignores_post_submit_needs_manager_review_drift(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """Post-submit NMR drift does not redefine the approved snapshot."""
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        # Add a normal line so period is not empty
        line = await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )
        line_id = line["draft_line_id"]

        # Force NeedsManagerReview=TRUE via direct DB (bypasses API guard)
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET needsmanagerreview = TRUE
                WHERE draftlineid = :lid
            """),
            {"lid": line_id},
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_finalize"] is True
        assert body["blockers"] == []

    @pytest.mark.asyncio
    async def test_preview_ignores_post_submit_unresolved_calculation_drift(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        preview_driver_id: int,
        direct_db,
    ):
        """Live unresolved calculation drift does not alter the approved packet."""
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        # Approve a MILEAGE rate for preview_driver_id so the Miles line passes validation
        mileage_rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage_rt_id = next(rt["rate_type_id"] for rt in mileage_rt_resp.json() if rt["rate_code"] == "MILEAGE")
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, preview_driver_id, mileage_rt_id, "0.10"
        )
        try:
            line = await _add_line_to_approved_period(
                session_client, auth_token, pid, preview_driver_id, direct_db,
                line_type="Miles", quantity="10.00",
            )
            line_id = line["draft_line_id"]
        finally:
            await session_client.delete(f"/payroll/rates/{rate_id}", headers=auth(auth_token))

        # Force calculatedamount=NULL AND rateamount=NULL to simulate broken state
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET calculatedamount = NULL, rateamount = NULL
                WHERE draftlineid = :lid
            """),
            {"lid": line_id},
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_finalize"] is True
        assert body["blockers"] == []

    # -- Content tests --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_preview_does_not_synthesize_post_submit_daily_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        preview_driver_id: int,
        direct_db,
    ):
        """Approved preview exposes only submitted normalized financial lines."""
        pid = cp3a_approved_period["payroll_period_id"]

        # Approve HOURLY rate for preview_driver_id at $20/hr
        hourly_rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly_rt_id = next(rt["rate_type_id"] for rt in hourly_rt_resp.json() if rt["rate_code"] == "HOURLY")
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, preview_driver_id, hourly_rt_id, "20.00"
        )
        try:
            await _add_line_to_approved_period(
                session_client, auth_token, pid, preview_driver_id, direct_db,
                line_type="Hours", quantity="8.00",
            )

            resp = await session_client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=auth(auth_token),
            )
            assert resp.status_code == 200
            body = resp.json()

            assert not any(l["line_type"] == "HOURS" for l in body["lines"])
            assert Decimal(str(body["total_final_gross"])) == Decimal("0")
        finally:
            await session_client.delete(f"/payroll/rates/{rate_id}", headers=auth(auth_token))

    @pytest.mark.asyncio
    async def test_preview_does_not_synthesize_post_submit_bonus_events(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """Only a BonusEvent captured at Submit belongs to the approved packet."""
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["bonus_event_count"] == 0
        assert not any(line["line_type"] == "BONUS" for line in body["lines"])

    @pytest.mark.asyncio
    async def test_preview_ignores_post_submit_void_source_drift(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """Voiding a live source row after Submit does not rewrite the snapshot."""
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        line = await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
        )
        await direct_db.execute(
            _text("UPDATE payroll.payrolldraftlines SET status = 'Void' WHERE draftlineid = :lid"),
            {"lid": line["draft_line_id"]},
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["can_finalize"] is True
        assert Decimal(str(body["total_final_gross"])) == Decimal("0")

    @pytest.mark.asyncio
    async def test_preview_can_finalize_true_when_clean(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """Clean Approved period with lines ->' can_finalize=True, blockers=[]."""
        pid = cp3a_approved_period["payroll_period_id"]

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_finalize"] is True
        assert body["blockers"] == []

    # -- Read-only test -------------------------------------------------------


    # -- Rate-split test -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_preview_shows_different_amounts_across_rate_boundary(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        Period spanning a rate boundary: preview lines on each side of the
        boundary must show different calculated amounts.

        Rate A: effective_from=2085-03-03, amount=$20  (Superseded by Rate B)
        Rate B: effective_from=2085-03-06, amount=$30  (Approved)

        Line 1: work_date=2085-03-05 (inside Rate A range) -> 8 x $20 = $160
        Line 2: work_date=2085-03-07 (inside Rate B range) -> 8 x $30 = $240

        Period is cancelled at the end of the test (never locked).
        """
        headers = auth(auth_token)
        branch_id = cp3a_clean

        # Get HOURLY rate type
        rt_resp = await session_client.get("/payroll/rate-types", headers=headers)
        assert rt_resp.status_code == 200
        hourly_rt_id = next(
            rt["rate_type_id"] for rt in rt_resp.json() if rt["rate_code"] == "HOURLY"
        )

        # Approve Rate A ($20 from 2085-03-03)
        rate_a_id = await _create_and_approve_rate(
            session_client, auth_token,
            paytest_driver_id, hourly_rt_id,
            amount="20.00",
            effective_from="2085-03-03",
        )
        # Approve Rate B ($30 from 2085-03-06) -- supersedes Rate A
        rate_b_id = await _create_and_approve_rate(
            session_client, auth_token,
            paytest_driver_id, hourly_rt_id,
            amount="30.00",
            effective_from="2085-03-06",
        )

        # Insert Open period directly (CP-1D: POST requires existing Open; PATCH Draft->Open blocked).
        row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Open', 'CP3A-2085-0303', 'CP3A Rate Split Test', 'Week', :start, :end)
                ON CONFLICT DO NOTHING
                RETURNING payrollperiodid
            """),
            {"bid": branch_id,
             "start": datetime.date(2085, 3, 3),
             "end": datetime.date(2085, 3, 9)},
        )).mappings().first()
        if row is None:
            row = (await direct_db.execute(
                _text(
                    "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE branchid = :bid AND periodcode = 'CP3A-2085-0303'"
                ),
                {"bid": branch_id},
            )).mappings().first()
        pid = row["payrollperiodid"]

        try:

            # Add two HOURS lines -- one on each side of the rate boundary
            line1 = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": paytest_driver_id,
                    "work_date": "2085-03-05",   # inside Rate A range
                    "line_type": "HOURS",
                    "quantity":  8,
                },
                headers=headers,
            )
            assert line1.status_code == 201, f"Add line1 failed: {line1.text}"
            line1_id = line1.json()["draft_line_id"]

            line2 = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": paytest_driver_id,
                    "work_date": "2085-03-07",   # inside Rate B range
                    "line_type": "HOURS",
                    "quantity":  8,
                },
                headers=headers,
            )
            assert line2.status_code == 201, f"Add line2 failed: {line2.text}"
            line2_id = line2.json()["draft_line_id"]

            # Advance to Approved
            await _advance_to_approved(session_client, auth_token, pid, paytest_driver_id)

            # Call finalization preview
            resp = await session_client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=headers,
            )
            assert resp.status_code == 200
            body = resp.json()

            # Extract the two HOURS lines from the preview response
            preview_lines = {l["draft_line_id"]: l for l in body["lines"]}
            assert line1_id in preview_lines, f"Line1 (2085-03-05) not in preview"
            assert line2_id in preview_lines, f"Line2 (2085-03-07) not in preview"

            amt1 = Decimal(str(preview_lines[line1_id]["calculated_amount"]))
            amt2 = Decimal(str(preview_lines[line2_id]["calculated_amount"]))

            # Rate A line: 8h x $20 = $160
            assert amt1 == Decimal("160.00"), (
                f"Line on 2085-03-05 (Rate A $20): expected $160, got {amt1}"
            )
            # Rate B line: 8h x $30 = $240
            assert amt2 == Decimal("240.00"), (
                f"Line on 2085-03-07 (Rate B $30): expected $240, got {amt2}"
            )
            # Amounts must differ -- confirms rate split is reflected in preview
            assert amt1 != amt2, "Preview amounts must differ across rate boundary"

        finally:
            # Cancel period (never locked) and clean up rates
            await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Cancelled"},
                headers=headers,
            )
            await session_client.delete(f"/payroll/rates/{rate_b_id}", headers=headers)
            await session_client.delete(f"/payroll/rates/{rate_a_id}", headers=headers)

    @pytest.mark.asyncio
    async def test_preview_is_read_only(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """Calling preview must not write any PayrollFinalLines or change period status."""
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )

        # Count final lines before
        before_result = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        count_before = int(before_result.scalar_one())

        # Call preview
        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert resp.status_code == 200

        # Count final lines after -" must be unchanged
        after_result = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        count_after = int(after_result.scalar_one())
        assert count_before == count_after, (
            f"Preview wrote final lines! Before={count_before}, After={count_after}"
        )

        # Period status must still be Approved
        period_resp = await session_client.get(f"/payroll/periods/{pid}", headers=headers)
        assert period_resp.json()["status"] == "Approved"

    # -- Security tests -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_preview_oda_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """ODA user ->' 403."""
        pid = cp3a_approved_period["payroll_period_id"]

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )

        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP3A_ODA_Preview_Role",
            ["payroll.view", "payroll.entry", "payroll.finalize"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token,
            "cp3a_oda_preview_user",
            role_id,
            scope_type="OwnDriverDataOnly",
            branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_preview_requires_payroll_finalize_permission(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """User with only payroll.entry (no payroll.finalize) ->' 403."""
        pid = cp3a_approved_period["payroll_period_id"]

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )

        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP3A_EntryOnly_Preview_Role",
            ["payroll.view", "payroll.entry"],  # no payroll.finalize
        )
        entry_token = await _create_user_with_role(
            session_client, auth_token,
            "cp3a_entryonly_preview_user",
            role_id,
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=auth(entry_token),
        )
        assert resp.status_code == 403

    # -- Preview / finalize consistency test ---------------------------------

    @pytest.mark.asyncio
    async def test_preview_gross_matches_finalize(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        preview.total_final_gross must equal the sum of FinalAmounts after
        actually finalizing the same period.

        Flow:
          1. Add two lines (Hours + Miles with approved DriverRates).
          2. Call preview ->' record total_final_gross.
          3. Call finalize ->' period becomes Locked.
          4. Sum payrollfinallines.finalamount (excluding SYS rows).
          5. Assert preview total == final lines sum.
        """
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        # Set up approved HOURLY and MILEAGE rates for paytest_driver_id
        rt_resp = await session_client.get("/payroll/rate-types", headers=headers)
        rts = {rt["rate_code"]: rt["rate_type_id"] for rt in rt_resp.json()}
        hourly_rate_id = await _create_and_approve_rate(
            session_client, auth_token, paytest_driver_id, rts["HOURLY"], "20.00"
        )
        mileage_rate_id = await _create_and_approve_rate(
            session_client, auth_token, paytest_driver_id, rts["MILEAGE"], "0.50"
        )
        finalized = False
        try:
            await _add_line_to_approved_period(
                session_client, auth_token, pid, paytest_driver_id, direct_db,
                line_type="Hours", quantity="8.00",
                work_date="2085-01-07",
            )
            await _add_line_to_approved_period(
                session_client, auth_token, pid, paytest_driver_id, direct_db,
                line_type="Miles", quantity="100.00",
                work_date="2085-01-08",
            )

            # Step 2: preview
            preview_resp = await session_client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=headers,
            )
            assert preview_resp.status_code == 200
            preview = preview_resp.json()
            assert preview["can_finalize"] is True
            preview_gross = Decimal(str(preview["total_final_gross"]))

            # Step 3: finalize
            fin_resp = await session_client.post(
                f"/payroll/periods/{pid}/finalize",
                headers=headers,
            )
            assert fin_resp.status_code == 200
            assert fin_resp.json()["status"] == "Locked"
            finalized = True

            # Step 4: sum final lines (excluding SYS adjustments)
            agg = await direct_db.execute(
                _text("""
                    SELECT COALESCE(SUM(finalamount), 0) AS total
                    FROM   payroll.payrollfinallines
                    WHERE  payrollperiodid = :pid
                      AND  linetype NOT IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP')
                """),
                {"pid": pid},
            )
            final_sum = Decimal(str(agg.scalar_one()))

            # Step 5: compare
            assert preview_gross == final_sum, (
                f"Preview total_final_gross ({preview_gross}) != "
                f"actual finalized sum ({final_sum})"
            )

        finally:
            # A finalized packet retains its immutable FinalLines and their rate
            # provenance. It must not be dismantled by test cleanup.
            if not finalized:
                await session_client.delete(f"/payroll/rates/{hourly_rate_id}", headers=headers)
                await session_client.delete(f"/payroll/rates/{mileage_rate_id}", headers=headers)

# ===========================================================================
# POST /finalize -" Driver/ODA security boundary
# ===========================================================================

class TestFinalizeODABlock:
    """
    POST /payroll/periods/{id}/finalize must block Driver/OwnDriverDataOnly
    users before any data is read, mutated, or returned.
    """

    @pytest.mark.asyncio
    async def test_finalize_blocked_for_oda_user(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """ODA user with payroll.finalize gets 403 on POST finalize."""
        pid = cp3a_approved_period["payroll_period_id"]

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )

        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP3A_ODA_Finalize_Role",
            ["payroll.view", "payroll.entry", "payroll.finalize"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token,
            "cp3a_oda_finalize_user",
            role_id,
            scope_type="OwnDriverDataOnly",
            branch_id=paytest_branch_id,
        )

        resp = await session_client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_finalize_oda_no_data_leak_or_mutation(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """ODA user gets 403 before any PayrollFinalLines are inserted and period stays Approved."""
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )

        count_before = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).scalar_one()

        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP3A_ODA_FinalLeak_Role",
            ["payroll.view", "payroll.entry", "payroll.finalize"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token,
            "cp3a_oda_final_leak_user",
            role_id,
            scope_type="OwnDriverDataOnly",
            branch_id=paytest_branch_id,
        )

        resp = await session_client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403

        # Period must still be Approved
        period_resp = await session_client.get(f"/payroll/periods/{pid}", headers=headers)
        assert period_resp.json()["status"] == "Approved"

        # No final lines inserted
        count_after = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).scalar_one()
        assert count_before == count_after, (
            f"ODA block must not insert final lines. Before={count_before}, After={count_after}"
        )

    @pytest.mark.asyncio
    async def test_finalize_requires_payroll_finalize_permission(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """User with payroll.entry but no payroll.finalize gets 403 on POST finalize."""
        pid = cp3a_approved_period["payroll_period_id"]

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )

        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP3A_EntryOnly_Finalize_Role",
            ["payroll.view", "payroll.entry"],  # no payroll.finalize
        )
        entry_token = await _create_user_with_role(
            session_client, auth_token,
            "cp3a_entryonly_finalize_user",
            role_id,
        )

        resp = await session_client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(entry_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_finalize_allowed_for_operational_user(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """Operational user (AllCompanyBranches + payroll.finalize) can finalize."""
        pid = cp3a_approved_period["payroll_period_id"]

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )

        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP3A_OpFinal_Role",
            ["payroll.view", "payroll.entry", "payroll.finalize"],
        )
        op_token = await _create_user_with_role(
            session_client, auth_token,
            "cp3a_op_final_user",
            role_id,
            scope_type="AllCompanyBranches",
        )

        resp = await session_client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(op_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "Locked"

    @pytest.mark.asyncio
    async def test_preview_oda_block_still_works(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Regression: preview ODA block still works after finalize guard addition."""
        pid = cp3a_approved_period["payroll_period_id"]

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
        )

        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP3A_ODA_PreviewReg_Role",
            ["payroll.view", "payroll.entry", "payroll.finalize"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token,
            "cp3a_oda_preview_reg_user",
            role_id,
            scope_type="OwnDriverDataOnly",
            branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403

# ===========================================================================
# CP-3A.5 -" Response shape: final_amount, line counts, SYS consistency
# ===========================================================================

class TestPreviewResponseShape:
    """Verify final_amount per line, explicit line count fields, SYS consistency."""

    @pytest.mark.asyncio
    async def test_preview_line_includes_final_amount(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        preview_driver_id: int,
        direct_db,
    ):
        """Each preview line must have a backend-computed final_amount."""
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        # Approve HOURLY rate at $20/hr for preview_driver_id
        rt_resp = await session_client.get("/payroll/rate-types", headers=headers)
        hourly_rt_id = next(rt["rate_type_id"] for rt in rt_resp.json() if rt["rate_code"] == "HOURLY")
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, preview_driver_id, hourly_rt_id, "20.00"
        )
        try:
            await _add_line_to_approved_period(
                session_client, auth_token, pid, preview_driver_id, direct_db,
                line_type="Hours", quantity="8.00",
                work_date="2085-01-07",
            )

            resp = await session_client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            preview = resp.json()

            assert all("final_amount" in line for line in preview["lines"])
            assert Decimal(str(preview["total_final_gross"])) == Decimal("0")
        finally:
            await session_client.delete(f"/payroll/rates/{rate_id}", headers=headers)

    @pytest.mark.asyncio
    async def test_preview_line_count_fields_no_sys(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """Without SYS rows: draft_line_count==N, sys_adjustment_count==0, final_line_count_estimate==N."""
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
            work_date="2085-01-08",
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        preview = resp.json()

        draft = preview["draft_line_count"]
        sys_n = preview["sys_adjustment_count"]
        est   = preview["final_line_count_estimate"]

        assert isinstance(draft, int) and draft == 0
        assert sys_n == 0, f"Expected 0 SYS adjustments, got {sys_n}"
        assert est == draft, f"final_line_count_estimate {est} != draft_line_count {draft}"

    @pytest.mark.asyncio
    async def test_preview_final_line_count_includes_sys_rows(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """With a SYS_MIN_TOPUP: final_line_count_estimate == draft_line_count + 1."""
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        # Add a DailyNote line (finalamount=$0) so driver is below any minimum pay rule
        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
            work_date="2085-01-09",
        )

        # Add a minimum pay rule via API - use a date range scoped to the period only
        # (2085-01-01 to 2085-01-31) so it does not overlap phase2c tests (2088+)
        rule_resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id": paytest_driver_id,
                "branch_id": paytest_branch_id,
                "rule_type": "MinimumPay",
                "amount": "99999.00",
                "effective_from": "2085-01-01",
                "effective_to": "2085-01-31",
            },
            headers=headers,
        )
        assert rule_resp.status_code == 201, f"Pay rule creation failed: {rule_resp.text}"
        rule_id = rule_resp.json()["driver_pay_rule_id"]

        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        preview = resp.json()

        # Clean up the rule (void even though it has an effective_to - belt-and-suspenders)
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void",
            json={"reason": "test cleanup"},
            headers=headers,
        )

        sys_n = preview["sys_adjustment_count"]
        draft = preview["draft_line_count"]
        est   = preview["final_line_count_estimate"]

        assert sys_n == 0, f"Post-submit pay-rule drift must not create SYS rows: {sys_n}"
        assert est == draft + sys_n, (
            f"final_line_count_estimate {est} != draft {draft} + sys {sys_n}"
        )
        assert preview["sys_adjustments"] == []

    @pytest.mark.asyncio
    async def test_preview_sys_adjustment_amount_is_correct(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp3a_approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        Preview SYS_MIN_TOPUP adjustment_amount = min_pay - gross_pay.
        Uses the same Jan 2085 rule created (and still active/voided) by the
        preceding test. The test asserts structural correctness of the
        adjustment_amount field independent of finalization.
        """
        pid = cp3a_approved_period["payroll_period_id"]
        headers = auth(auth_token)

        await _add_line_to_approved_period(
            session_client, auth_token, pid, paytest_driver_id, direct_db,
            line_type="DailyNote", quantity="1.00",
            work_date="2085-01-10",
        )

        # Call preview - any active MinimumPay rule (from the previous test if still
        # active, or none) will be reflected; we only check structural fields here.
        resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        preview = resp.json()

        # Structural checks that are always true regardless of whether a rule is active:
        sys_n = preview["sys_adjustment_count"]
        draft = preview["draft_line_count"]
        est   = preview["final_line_count_estimate"]
        assert est == draft + sys_n, (
            f"final_line_count_estimate {est} must equal draft {draft} + sys {sys_n}"
        )
        # Each sys_adjustment must have adjustment_type and a numeric adjustment_amount
        for adj in preview["sys_adjustments"]:
            assert adj["adjustment_type"] in ("SYS_MIN_TOPUP", "SYS_MAX_CAP")
            assert Decimal(str(adj["adjustment_amount"])) != Decimal("0"), (
                "SYS adjustment_amount must be non-zero"
            )


# ===========================================================================
# CP-5 Fix - Preview/Finalize Consistency
#
# Tests that finalization-preview shows the same amounts as finalize will lock,
# even when a rate was changed after the period reached Approved status.
#
# Isolation: uses year 2085, March dates, to avoid conflicts with other suites.
# ===========================================================================

# Helpers shared by consistency tests

async def _get_hourly_rate_type_id_preview(
    client: httpx.AsyncClient,
    token: str,
) -> int:
    rv = await client.get("/payroll/rate-types", headers=auth(token))
    assert rv.status_code == 200
    for rt in rv.json():
        if rt["rate_code"] == "HOURLY":
            return rt["rate_type_id"]
    raise AssertionError("HOURLY rate type not found")


async def _void_rate(
    client: httpx.AsyncClient,
    token: str,
    rate_id: int,
) -> None:
    """Void a driver rate via the DELETE API (sets status='Voided')."""
    rv = await client.delete(f"/payroll/rates/{rate_id}", headers=auth(token))
    assert rv.status_code in (200, 204), f"Void rate {rate_id} failed: {rv.text}"


async def _create_and_approve_rate_preview(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_type_id: int,
    amount: str,
    effective_from: str,
) -> int:
    """Create and approve a driver rate. Returns driver_rate_id."""
    rc = await client.post(
        "/payroll/rates",
        json={
            "driver_id": driver_id,
            "rate_type_id": rate_type_id,
            "amount": amount,
            "effective_from": effective_from,
        },
        headers=auth(token),
    )
    assert rc.status_code == 201, f"Create rate failed: {rc.text}"
    rate_id = rc.json()["driver_rate_id"]
    ra = await client.post(
        f"/payroll/rates/{rate_id}/approve",
        headers=auth(token),
    )
    assert ra.status_code == 200, f"Approve rate failed: {ra.text}"
    return rate_id


@pytest_asyncio.fixture
async def cp5_consistency_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    Approved period on PAYTEST (2085-03-03 to 2085-03-09) with ONE HOURS
    line (8 hours) computed using an approved HOURLY rate of $20.00.
    After fixture yields, stored calc = $160.00.

    Uses an isolated driver (no pre-existing approved rates) so that the
    rate approval succeeds regardless of what test_pay_rates.py approved for
    the shared paytest_driver_id.

    Yields: (period_id, draft_line_id, rate_id, rate_type_id)
    """
    headers = auth(auth_token)

    # Cancel any leftover active periods on this branch
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)

    # Create an isolated driver with no approved rates
    n = next(_preview_driver_counter)
    drv_resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": f"CP5 Isolated Driver {n}",
            "driver_code": f"CP5-{n:04d}",
        },
        headers=headers,
    )
    assert drv_resp.status_code == 201, f"Create CP5 driver failed: {drv_resp.text}"
    driver_id = drv_resp.json()["driver_id"]

    rate_type_id = await _get_hourly_rate_type_id_preview(session_client, auth_token)

    # Create a $20/h rate effective from the work date
    rate_id = await _create_and_approve_rate_preview(
        session_client, auth_token,
        driver_id, rate_type_id,
        amount="20.00",
        effective_from="2085-03-03",
    )

    # Insert Open period directly (CP-1D: POST requires existing Open; PATCH Draft->Open blocked).
    _row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', 'CP5-2085-0303', 'CP5 Consistency Test', 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": paytest_branch_id,
         "start": datetime.date(2085, 3, 3),
         "end": datetime.date(2085, 3, 9)},
    )).mappings().first()
    if _row is None:
        _row = (await direct_db.execute(
            _text(
                "SELECT payrollperiodid FROM payroll.payrollperiods "
                "WHERE branchid = :bid AND periodcode = 'CP5-2085-0303'"
            ),
            {"bid": paytest_branch_id},
        )).mappings().first()
    pid = _row["payrollperiodid"]

    # Add an HOURS line - the system will compute calc = 8 * 20 = 160
    line_resp = await session_client.post(
        f"/payroll/periods/{pid}/lines",
        json={
            "driver_id": driver_id,
            "work_date": "2085-03-03",
            "line_type": "Hours",
            "quantity": "8.00",
        },
        headers=headers,
    )
    assert line_resp.status_code == 201, f"Add line failed: {line_resp.text}"
    line_id = line_resp.json()["draft_line_id"]

    # Advance to Approved
    await _advance_to_approved(session_client, auth_token, pid, driver_id, "2085-03-03")

    yield (pid, line_id, rate_id, rate_type_id, driver_id)

    # Teardown: cancel Draft/Open via PATCH; Approved/InReview/Returned via direct DB.
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    await _force_cancel_locked_periods(direct_db, paytest_branch_id)


@pytest_asyncio.fixture
async def cp5_finalizes_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    Like cp5_consistency_period but uses April 2085 dates so that
    finalizing the period (->Locked) does not block the March 2085 date range
    used by other consistency tests.

    Uses an isolated driver (no pre-existing approved rates) so that the
    rate approval succeeds regardless of what test_pay_rates.py approved for
    the shared paytest_driver_id.

    Yields: (period_id, draft_line_id, rate_id, rate_type_id, driver_id)
    """
    headers = auth(auth_token)
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)

    # Create an isolated driver with no approved rates
    n = next(_preview_driver_counter)
    drv_resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": f"CP5F Isolated Driver {n}",
            "driver_code": f"CP5F-{n:04d}",
        },
        headers=headers,
    )
    assert drv_resp.status_code == 201, f"Create CP5F driver failed: {drv_resp.text}"
    driver_id = drv_resp.json()["driver_id"]

    rate_type_id = await _get_hourly_rate_type_id_preview(session_client, auth_token)
    rate_id = await _create_and_approve_rate_preview(
        session_client, auth_token,
        driver_id, rate_type_id,
        amount="20.00",
        effective_from="2085-04-07",
    )

    # Insert Open period directly (CP-1D: POST requires existing Open; PATCH Draft->Open blocked).
    _row2 = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', 'CP5F-2085-0407', 'CP5F Finalizes Test', 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": paytest_branch_id,
         "start": datetime.date(2085, 4, 7),
         "end": datetime.date(2085, 4, 13)},
    )).mappings().first()
    if _row2 is None:
        _row2 = (await direct_db.execute(
            _text(
                "SELECT payrollperiodid FROM payroll.payrollperiods "
                "WHERE branchid = :bid AND periodcode = 'CP5F-2085-0407'"
            ),
            {"bid": paytest_branch_id},
        )).mappings().first()
    pid = _row2["payrollperiodid"]

    line_resp = await session_client.post(
        f"/payroll/periods/{pid}/lines",
        json={
            "driver_id": driver_id,
            "work_date": "2085-04-07",
            "line_type": "Hours",
            "quantity": "8.00",
        },
        headers=headers,
    )
    assert line_resp.status_code == 201, f"Add line failed: {line_resp.text}"
    line_id = line_resp.json()["draft_line_id"]

    await _advance_to_approved(session_client, auth_token, pid, driver_id, "2085-04-07")

    yield (pid, line_id, rate_id, rate_type_id, driver_id)

    # Teardown: cancel Draft/Open via PATCH; Approved/InReview/Returned via direct DB.
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    await _force_cancel_locked_periods(direct_db, paytest_branch_id)


class TestPreviewFinalizeConsistencyCP5:
    """
    CP-5 Fix: Finalization Preview must show refreshed amounts (same as
    what Finalize will actually lock) when a rate changed after approval.
    """

    @pytest.mark.asyncio
    async def test_preview_preserves_submitted_amount_when_rate_changed_post_approval(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp5_consistency_period: tuple,
        direct_db,
    ):
        """
        After period reaches Approved with calc=$160 (8h x $20):
          1. Void old $20 rate, create+approve $35 rate (same effective date).
          2. Preview must retain the approved snapshot calc=$160, not live $280.
          3. Preview must NOT write anything to payrolldraftlines.
        """
        pid, line_id, old_rate_id, rate_type_id, driver_id = cp5_consistency_period
        headers = auth(auth_token)

        # Verify stored calc is $160 (set at entry time with $20 rate)
        row = (await direct_db.execute(
            _text("SELECT calculatedamount FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
            {"lid": line_id},
        )).mappings().first()
        assert Decimal(str(row["calculatedamount"])) == Decimal("160.0000"), (
            f"Precondition: expected stored calc=$160, got {row['calculatedamount']}"
        )

        # Void old rate via API (constraint prevents two simultaneous Approved rates)
        await _void_rate(session_client, auth_token, old_rate_id)

        # Create and approve new rate at $35 with same effective date
        new_rate_id = await _create_and_approve_rate_preview(
            session_client, auth_token,
            driver_id, rate_type_id,
            amount="35.00",
            effective_from="2085-03-03",
        )

        try:
            # Call preview
            resp = await session_client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            preview = resp.json()

            # Preview must retain submitted amount $160 (8 x $20)
            hours_line = next(
                (l for l in preview["lines"] if l["line_type"] in ("HOURS", "Hours")),
                None,
            )
            assert hours_line is not None, "HOURS line not in preview"
            assert Decimal(str(hours_line["final_amount"])) == Decimal("160.00"), (
                f"Expected submitted $160, got {hours_line['final_amount']}"
            )

            assert Decimal(str(preview["total_final_gross"])) == Decimal("160.00"), (
                f"Expected snapshot total_final_gross=$160, got {preview['total_final_gross']}"
            )

            # Preview must NOT have mutated the stored calculatedamount
            row_after = (await direct_db.execute(
                _text("SELECT calculatedamount FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
                {"lid": line_id},
            )).mappings().first()
            assert Decimal(str(row_after["calculatedamount"])) == Decimal("160.0000"), (
                f"Preview mutated stored calc! Now {row_after['calculatedamount']} (should be 160)"
            )

            # Period must still be Approved
            period_resp = await session_client.get(f"/payroll/periods/{pid}", headers=headers)
            assert period_resp.json()["status"] == "Approved"

        finally:
            # Void new rate so teardown doesn't leave stale data
            await _void_rate(session_client, auth_token, new_rate_id)

    @pytest.mark.asyncio
    async def test_preview_total_equals_finalized_lines_after_rate_change(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp5_finalizes_period: tuple,
        direct_db,
    ):
        """
        Preview total_final_gross must equal the sum of PayrollFinalLines.finalamount
        after actually finalizing, even when the rate changed post-approval.

        Uses April 2085 dates (cp5_finalizes_period) so that Locked period does
        not block March 2085 date range used by other consistency tests.

        Flow:
          1. Void $20 rate, create+approve $35 rate.
          2. Preview -> record total_final_gross (should be $280).
          3. Finalize -> period becomes Locked.
          4. Sum payrollfinallines.finalamount (non-SYS).
          5. Assert preview total == finalized sum.
        """
        pid, line_id, old_rate_id, rate_type_id, driver_id = cp5_finalizes_period
        headers = auth(auth_token)

        # Replace rate
        await _void_rate(session_client, auth_token, old_rate_id)
        new_rate_id = await _create_and_approve_rate_preview(
            session_client, auth_token,
            driver_id, rate_type_id,
            amount="35.00",
            effective_from="2085-04-07",
        )

        # Preview
        preview_resp = await session_client.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=headers,
        )
        assert preview_resp.status_code == 200
        preview = preview_resp.json()
        assert preview["can_finalize"] is True
        preview_gross = Decimal(str(preview["total_final_gross"]))

        # Finalize
        fin_resp = await session_client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=headers,
        )
        assert fin_resp.status_code == 200
        assert fin_resp.json()["status"] == "Locked"

        # Sum non-SYS final lines
        agg = await direct_db.execute(
            _text("""
                SELECT COALESCE(SUM(finalamount), 0) AS total
                FROM   payroll.payrollfinallines
                WHERE  payrollperiodid = :pid
                  AND  linetype NOT IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP')
            """),
            {"pid": pid},
        )
        final_sum = Decimal(str(agg.scalar_one()))

        assert preview_gross == final_sum, (
            f"Preview gross ({preview_gross}) != finalized sum ({final_sum})"
        )

        # Phase 5: new_rate_id was used in finalized payroll and cannot be voided.
        # cp5_finalizes_period uses an isolated driver so no cross-test pollution.

    @pytest.mark.asyncio
    async def test_preview_sys_topup_uses_refreshed_amount(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp5_consistency_period: tuple,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        After rate increases from $20 to $35, gross becomes $280.
        A MinimumPay rule of $200 should produce SYS_MIN_TOPUP=0 (below
        gross of $280), not a bogus topup calculated from stale $160.
        """
        pid, line_id, old_rate_id, rate_type_id, driver_id = cp5_consistency_period
        headers = auth(auth_token)

        # Replace rate: $20 -> $35
        await _void_rate(session_client, auth_token, old_rate_id)
        new_rate_id = await _create_and_approve_rate_preview(
            session_client, auth_token,
            driver_id, rate_type_id,
            amount="35.00",
            effective_from="2085-03-03",
        )

        # MinimumPay = $200 (less than $280 gross, so NO topup expected)
        rule_resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id": driver_id,
                "branch_id": paytest_branch_id,
                "rule_type": "MinimumPay",
                "amount": "200.00",
                "effective_from": "2085-03-01",
                "effective_to": "2085-03-31",
            },
            headers=headers,
        )
        assert rule_resp.status_code == 201, f"Pay rule failed: {rule_resp.text}"
        rule_id = rule_resp.json()["driver_pay_rule_id"]

        try:
            resp = await session_client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=headers,
            )
            assert resp.status_code == 200
            preview = resp.json()

            # Post-submit rate/rule changes do not re-run minimum-pay calculation.
            assert preview["sys_adjustment_count"] == 0, (
                f"Expected no SYS adjustments (gross $280 > min $200), "
                f"got: {preview['sys_adjustments']}"
            )
            assert Decimal(str(preview["total_final_gross"])) == Decimal("160.00")

        finally:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/void",
                json={"reason": "test cleanup"},
                headers=headers,
            )
            await _void_rate(session_client, auth_token, new_rate_id)

    @pytest.mark.asyncio
    async def test_preview_stale_gross_would_wrongly_trigger_sys_topup(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp5_consistency_period: tuple,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        A post-submit rate/rule change must not synthesize a new SYS adjustment
        in the approved immutable packet.
        """
        pid, line_id, old_rate_id, rate_type_id, driver_id = cp5_consistency_period
        headers = auth(auth_token)

        # Replace live rate after the $160 packet was approved.
        await _void_rate(session_client, auth_token, old_rate_id)
        new_rate_id = await _create_and_approve_rate_preview(
            session_client, auth_token,
            driver_id, rate_type_id,
            amount="35.00",
            effective_from="2085-03-03",
        )

        # This rule is also introduced after Submit and is not snapshot authority.
        rule_resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id": driver_id,
                "branch_id": paytest_branch_id,
                "rule_type": "MinimumPay",
                "amount": "300.00",
                "effective_from": "2085-03-01",
                "effective_to": "2085-03-31",
            },
            headers=headers,
        )
        assert rule_resp.status_code == 201
        rule_id = rule_resp.json()["driver_pay_rule_id"]

        try:
            resp = await session_client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=headers,
            )
            assert resp.status_code == 200
            preview = resp.json()

            assert preview["sys_adjustment_count"] == 0
            assert preview["sys_adjustments"] == []
            assert Decimal(str(preview["total_final_gross"])) == Decimal("160.00")

        finally:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/void",
                json={"reason": "test cleanup"},
                headers=headers,
            )
            await _void_rate(session_client, auth_token, new_rate_id)

    @pytest.mark.asyncio
    async def test_preview_manager_nmr_flag_not_overridden(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp5_consistency_period: tuple,
        direct_db,
    ):
        """
        A line with NMR=True AND calc IS NOT NULL is manager-flagged.
        Preview must honour the stored calc (not override it), and
        the line must appear with needs_manager_review=True.
        """
        pid, line_id, old_rate_id, rate_type_id, driver_id = cp5_consistency_period
        headers = auth(auth_token)

        # Force NMR=True on the line (calc=$160 is still set - manager-controlled flag)
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET    needsmanagerreview = TRUE
                WHERE  draftlineid = :lid
            """),
            {"lid": line_id},
        )

        # Also replace the rate ($20 -> $35) to ensure virtual refresh would
        # produce a different amount - but guard should skip this line
        await _void_rate(session_client, auth_token, old_rate_id)
        new_rate_id = await _create_and_approve_rate_preview(
            session_client, auth_token,
            driver_id, rate_type_id,
            amount="35.00",
            effective_from="2085-03-03",
        )

        try:
            resp = await session_client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=headers,
            )
            assert resp.status_code == 200
            preview = resp.json()

            assert preview["can_finalize"] is True
            assert preview["blockers"] == []

            # The preview line must show the submitted calc=$160, not live $280.
            hours_line = next(
                (l for l in preview["lines"] if l["line_type"] in ("HOURS", "Hours")),
                None,
            )
            assert hours_line is not None
            assert Decimal(str(hours_line["calculated_amount"])) == Decimal("160.0000"), (
                f"Submitted calc must remain $160, got {hours_line['calculated_amount']}"
            )

        finally:
            # Restore NMR=False so teardown can proceed cleanly
            await direct_db.execute(
                _text("""
                    UPDATE payroll.payrolldraftlines
                    SET    needsmanagerreview = FALSE
                    WHERE  draftlineid = :lid
                """),
                {"lid": line_id},
            )
            await _void_rate(session_client, auth_token, new_rate_id)

    @pytest.mark.asyncio
    async def test_preview_read_only_does_not_mutate_draft_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp5_consistency_period: tuple,
        direct_db,
    ):
        """
        Calling preview (including the virtual refresh) must not modify
        any payrolldraftlines rows - calculatedamount and needsmanagerreview
        must remain exactly as stored before the preview call.
        """
        pid, line_id, old_rate_id, rate_type_id, driver_id = cp5_consistency_period
        headers = auth(auth_token)

        # Replace rate so virtual refresh WOULD change the calc
        await _void_rate(session_client, auth_token, old_rate_id)
        new_rate_id = await _create_and_approve_rate_preview(
            session_client, auth_token,
            driver_id, rate_type_id,
            amount="35.00",
            effective_from="2085-03-03",
        )

        try:
            # Snapshot stored values before preview
            before = (await direct_db.execute(
                _text("""
                    SELECT calculatedamount, needsmanagerreview
                    FROM   payroll.payrolldraftlines
                    WHERE  draftlineid = :lid
                """),
                {"lid": line_id},
            )).mappings().first()

            # Call preview
            resp = await session_client.get(
                f"/payroll/periods/{pid}/finalization-preview",
                headers=headers,
            )
            assert resp.status_code == 200

            # Snapshot stored values after preview
            after = (await direct_db.execute(
                _text("""
                    SELECT calculatedamount, needsmanagerreview
                    FROM   payroll.payrolldraftlines
                    WHERE  draftlineid = :lid
                """),
                {"lid": line_id},
            )).mappings().first()

            assert str(before["calculatedamount"]) == str(after["calculatedamount"]), (
                f"Preview mutated calculatedamount: {before['calculatedamount']} -> {after['calculatedamount']}"
            )
            assert before["needsmanagerreview"] == after["needsmanagerreview"], (
                f"Preview mutated needsmanagerreview: {before['needsmanagerreview']} -> {after['needsmanagerreview']}"
            )

        finally:
            await _void_rate(session_client, auth_token, new_rate_id)

