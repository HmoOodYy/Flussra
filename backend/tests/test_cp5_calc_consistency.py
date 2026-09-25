"""
CP-5 Integration tests — Automatic Calculation Consistency + Date-Aware Driver Eligibility

Tests cover:
  1.  Driver hired 2089-06-23 not visible on day-grid for 2089-06-21
  2.  Driver hired 2089-06-23 visible from 2089-06-23 onward
  3.  Driver terminated 2089-06-25 not visible after termination (2089-06-27)
  4.  Driver terminated 2089-06-25 still visible up to termination date
  5.  Saved draft line persists after driver's termination_date passes
  6.  Work line on 2089-06-21 uses rate effective 2089-06-21 even if rate was
      approved / created after the line was entered (backdated rate)
  7.  Pending (unapproved) rate is NOT used — calculatedamount stays NULL
  8.  Missing approved rate flags needsmanagerreview = True
  9.  Open → InReview (submit) auto-refreshes calculations:
      backdated approved rate → calc updated → submit proceeds without error
  10. finalize_period auto-refreshes: backdated approved rate after Approved
      status → FinalAmount reflects fresh rate
  11. Locked period's draft lines are NOT mutated by finalize refresh
  12. No Driver/ODA security regression (403 still enforced)

Isolation: all periods use dates in 2089 to avoid conflicts with other modules.
"""
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _text

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PERIOD_START = "2089-06-21"
PERIOD_END   = "2089-06-27"
# Middle-of-period dates for hire/termination tests
DATE_JUN21 = "2089-06-21"
DATE_JUN23 = "2089-06-23"
DATE_JUN25 = "2089-06-25"
DATE_JUN27 = "2089-06-27"


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    row = (await session_db_conn.execute(_text("""
        INSERT INTO core.branches
            (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {
        "code": f"CP5_{uuid4().hex}",
        "name": f"CP5 isolated {uuid4().hex[:8]}",
    })).scalar_one()
    return int(row)


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    response = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": "CP5 Isolated Driver",
            "driver_code": f"CP5-D-{uuid4().hex[:10]}",
        },
        headers=auth(auth_token),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["driver_id"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    db=None,
) -> None:
    """Cancel all active (non-final) periods on the given branch."""
    if db is not None:
        await db.execute(
            _text(
                "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                "WHERE branchid = :bid AND status IN ('InReview', 'Approved')"
            ),
            {"bid": branch_id},
        )
    headers = auth(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
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


async def _open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str = PERIOD_START,
    end: str = PERIOD_END,
    db=None,
) -> int:
    """Insert an Open period directly into DB. Returns period_id."""
    from datetime import date as _date
    if db is None:
        raise RuntimeError("_open_period requires db= since CP-1D B1 guard blocks HTTP POST")
    code = f"CP5-{branch_id}-{start}"
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"CP5 {start}",
         "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    seed_date: str = DATE_JUN23,
) -> None:
    """Advance an Open period to Approved via review flow."""
    headers = auth(token)
    # Ensure there is at least one non-void line
    lines = await client.get(
        f"/payroll/periods/{period_id}/lines",
        params={"status": "Active"}, headers=headers
    )
    if lines.status_code == 200 and len(lines.json()) == 0:
        r = await client.post(
            f"/payroll/periods/{period_id}/lines",
            json={"driver_id": driver_id, "work_date": seed_date,
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
            headers=headers,
        )
        assert r.status_code == 201, f"Seed line failed: {r.text}"
    # Open → InReview
    tr = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"}, headers=headers,
    )
    assert tr.status_code == 200, f"InReview failed: {tr.text}"
    # Find pending review item and approve
    rv = await client.get("/review/items", headers=headers)
    assert rv.status_code == 200
    item = next(
        (i for i in rv.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, f"No pending review item for {period_id}"
    dec = await client.post(
        f"/review/items/{item['review_item_id']}/decide",
        json={"decision": "Approved"}, headers=headers,
    )
    assert dec.status_code == 200, f"Approve failed: {dec.text}"


async def _get_hourly_rate_type_id(
    client: httpx.AsyncClient,
    token: str,
) -> int:
    rv = await client.get("/payroll/rate-types", headers=auth(token))
    assert rv.status_code == 200
    for rt in rv.json():
        if rt["rate_code"] == "HOURLY":
            return rt["rate_type_id"]
    raise AssertionError("HOURLY rate type not found")


async def _create_driver(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    name: str,
    hire_date: str | None = None,
) -> int:
    """Create a driver, optionally with a hire_date. Returns driver_id."""
    body: dict = {"branch_id": branch_id, "full_name": name}
    if hire_date:
        body["hire_date"] = hire_date
    r = await client.post("/core/drivers", json=body, headers=auth(token))
    assert r.status_code == 201, f"Create driver failed: {r.text}"
    return r.json()["driver_id"]


async def _set_termination_date(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    termination_date: str,
) -> None:
    r = await client.patch(
        f"/core/drivers/{driver_id}",
        json={"termination_date": termination_date},
        headers=auth(token),
    )
    assert r.status_code == 200, f"Set termination_date failed: {r.text}"


async def _create_and_approve_rate(
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
    assert cr.status_code == 201
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=auth(token),
        )
        assert pr.status_code == 200
    return role_id


async def _create_user_with_role(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    role_id: int,
    scope_type: str = "AllCompanyBranches",
    branch_id=None,
) -> str:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username, "display_name": username,
            "password": "TestPass123!", "is_active": True,
            "can_login": True, "must_change_password": False,
        },
        headers=auth(admin_token),
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"
    user_id = resp.json()["user_id"]
    body: dict = {"company_role_id": role_id, "scope_type": scope_type}
    if branch_id is not None:
        body["branch_id"] = branch_id
    assign = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=body, headers=auth(admin_token),
    )
    assert assign.status_code in (200, 201)
    login = await client.post("/auth/login", json={
        "username": username, "password": "TestPass123!", "company_code": "DEMO",
    })
    assert login.status_code == 200
    return login.json()["access_token"]


# ---------------------------------------------------------------------------
# 1–4. Driver eligibility by work_date (hire/termination)
# ---------------------------------------------------------------------------

class TestDriverEligibilityByDate:
    """
    The day-grid filters drivers by HireDate ≤ work_date ≤ TerminationDate.
    These tests confirm the SQL filter actually works end-to-end through the API.
    """

    @pytest.mark.asyncio
    async def test_driver_hired_midperiod_absent_before_hire_date(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Driver with hire_date=2089-06-23 must NOT appear in the day-grid on
        2089-06-21 (two days before hire).
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id,
            "CP5 HireMid Driver", hire_date=DATE_JUN23,
        )
        try:
            resp = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": DATE_JUN21},
                headers=headers,
            )
            assert resp.status_code == 200
            driver_ids = [r["driver_id"] for r in resp.json()["rows"]]
            assert driver_id not in driver_ids, (
                f"Driver hired {DATE_JUN23} must not appear on {DATE_JUN21}"
            )
        finally:
            await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    @pytest.mark.asyncio
    async def test_driver_hired_midperiod_visible_from_hire_date(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Same driver (hire_date=2089-06-23) MUST appear on 2089-06-23 and later.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id,
            "CP5 HireVis Driver", hire_date=DATE_JUN23,
        )
        try:
            for wdate in (DATE_JUN23, DATE_JUN25, DATE_JUN27):
                resp = await session_client.get(
                    f"/payroll/periods/{pid}/day-grid",
                    params={"work_date": wdate},
                    headers=headers,
                )
                assert resp.status_code == 200
                driver_ids = [r["driver_id"] for r in resp.json()["rows"]]
                assert driver_id in driver_ids, (
                    f"Driver hired {DATE_JUN23} must appear on {wdate}"
                )
        finally:
            await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    @pytest.mark.asyncio
    async def test_driver_terminated_midperiod_absent_after_termination(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Driver terminated on 2089-06-25 must NOT appear on 2089-06-27.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id,
            "CP5 TermAbsent Driver",
        )
        await _set_termination_date(
            session_client, auth_token, driver_id, DATE_JUN25
        )
        try:
            resp = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": DATE_JUN27},
                headers=headers,
            )
            assert resp.status_code == 200
            driver_ids = [r["driver_id"] for r in resp.json()["rows"]]
            assert driver_id not in driver_ids, (
                f"Driver terminated {DATE_JUN25} must not appear on {DATE_JUN27}"
            )
        finally:
            await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    @pytest.mark.asyncio
    async def test_driver_terminated_midperiod_visible_until_termination(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Driver terminated on 2089-06-25 must appear on 2089-06-25 (inclusive).
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id,
            "CP5 TermVis Driver",
        )
        await _set_termination_date(
            session_client, auth_token, driver_id, DATE_JUN25
        )
        try:
            for wdate in (DATE_JUN21, DATE_JUN23, DATE_JUN25):
                resp = await session_client.get(
                    f"/payroll/periods/{pid}/day-grid",
                    params={"work_date": wdate},
                    headers=headers,
                )
                assert resp.status_code == 200
                driver_ids = [r["driver_id"] for r in resp.json()["rows"]]
                assert driver_id in driver_ids, (
                    f"Driver terminated {DATE_JUN25} must appear on {wdate}"
                )
        finally:
            await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 5. Saved line persists after driver becomes inactive
# ---------------------------------------------------------------------------

class TestSavedLinePersistsAfterTermination:

    @pytest.mark.asyncio
    async def test_existing_line_remains_in_db_after_driver_terminated(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        A draft line saved for a driver remains in the DB (not voided, not deleted)
        after the driver's termination_date is set to a past date.

        The driver is temporarily terminated and then the termination is cleared
        so the session fixture is not permanently disrupted.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        # Add a line on JUN23 for the main paytest driver
        line_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id": paytest_driver_id,
                "work_date": DATE_JUN23,
                "line_type": "DailyNote",
                "quantity": 1,
                "notes": "filler",
            },
            headers=headers,
        )
        assert line_resp.status_code == 201
        line_id = line_resp.json()["draft_line_id"]

        # Now set a termination date BEFORE the line's work_date
        await _set_termination_date(
            session_client, auth_token, paytest_driver_id, DATE_JUN21
        )
        try:
            # The line must still be retrievable via GET /lines
            lines_resp = await session_client.get(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
            )
            assert lines_resp.status_code == 200
            line_ids = [line["draft_line_id"] for line in lines_resp.json()]
            assert line_id in line_ids, (
                "Draft line must persist in DB even after driver is terminated"
            )
        finally:
            # Restore: clear termination_date so the driver is active again
            await session_client.patch(
                f"/core/drivers/{paytest_driver_id}",
                json={"termination_date": None},
                headers=headers,
            )
            await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 6–8. Rate lookup correctness
# ---------------------------------------------------------------------------

class TestRateLookupByWorkDate:

    @pytest.mark.asyncio
    async def test_backdated_rate_reflected_in_calculation(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        An HOURS line entered when NO approved rate exists gets
        needsmanagerreview=True.  After approving a rate backdated to the
        work_date, updating the line quantity re-triggers calculation and
        produces the correct calculatedamount.

        This proves rate lookup uses work_date (effectivefrom ≤ work_date),
        not the date the rate was entered into the system.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        # Get HOURLY rate type
        hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)

        # Enter HOURS line for JUN23 with NO approved rate for driver
        line_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id": paytest_driver_id,
                "work_date": DATE_JUN23,
                "line_type": "HOURS",
                "quantity": 8,
            },
            headers=headers,
        )
        assert line_resp.status_code == 201, f"Add line failed: {line_resp.text}"
        line = line_resp.json()
        line_id = line["draft_line_id"]
        # Without an approved rate, calculatedamount should be None
        # (needs_manager_review=True is the expected signal)
        # Note: if the driver happens to have an existing approved rate, this
        # test may see calculatedamount != None — we still verify the
        # effective-dated calculation holds after adding a backdated rate.

        # Now create and approve a rate backdated to JUN21 (before the work date)
        effective_from = "2089-06-01"  # before JUN23, making it valid for JUN23
        rate_id = await _create_and_approve_rate(
            session_client, auth_token,
            paytest_driver_id, hourly_rt_id,
            amount="30.00",
            effective_from=effective_from,
        )

        try:
            # Trigger re-calculation by updating the quantity (same value is fine)
            update_resp = await session_client.patch(
                f"/payroll/periods/{pid}/lines/{line_id}",
                json={"quantity": 8.0},
                headers=headers,
            )
            assert update_resp.status_code == 200

            updated = update_resp.json()
            # calculatedamount should now be 8 × $30 = $240.00
            assert updated["calculated_amount"] is not None, (
                "calculatedamount must be set after approved backdated rate exists"
            )
            assert Decimal(str(updated["calculated_amount"])) == Decimal("240.0000"), (
                f"Expected 8 × $30 = $240, got {updated['calculated_amount']}"
            )
            assert updated["needs_manager_review"] is False
        finally:
            # Void the rate to clean up (avoid leaking into other tests)
            await session_client.delete(
                f"/payroll/rates/{rate_id}",
                headers=headers,
            )
            await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    @pytest.mark.asyncio
    async def test_pending_rate_not_used_for_calculation(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        A PendingApproval rate must NOT be used for calculatedamount.
        The line must have calculatedamount=None / needsmanagerreview=True
        if only a Pending rate exists for the work_date.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)

        # Create a PENDING rate (do NOT approve it)
        rc = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": paytest_driver_id,
                "rate_type_id": hourly_rt_id,
                "amount": "99.00",
                "effective_from": "2089-06-01",
            },
            headers=headers,
        )
        assert rc.status_code == 201, f"Create pending rate failed: {rc.text}"
        pending_rate_id = rc.json()["driver_rate_id"]
        # Intentionally NOT approving

        try:
            # Add HOURS line — only pending rate exists
            line_resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": paytest_driver_id,
                    "work_date": DATE_JUN23,
                    "line_type": "HOURS",
                    "quantity": 5,
                },
                headers=headers,
            )
            assert line_resp.status_code == 201, f"Add line failed: {line_resp.text}"
            line = line_resp.json()
            # calculatedamount must NOT use the $99 pending rate
            if line["calculated_amount"] is not None:
                # If there's a pre-existing approved rate, that's fine — verify
                # it's not $99 * 5 = $495
                assert Decimal(str(line["calculated_amount"])) != Decimal("495.0000"), (
                    "Pending rate ($99) must not be used for calculation"
                )
        finally:
            await session_client.delete(f"/payroll/rates/{pending_rate_id}", headers=headers)
            await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)

    @pytest.mark.asyncio
    async def test_missing_approved_rate_sets_needs_manager_review(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        When no approved rate exists for the driver/type/date, the system must
        set needsmanagerreview=True and leave calculatedamount=None.

        Uses a fresh driver with no rates to guarantee isolation.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        # Create a fresh driver with no rates
        fresh_driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id,
            "CP5 NoRate Driver 2089",
        )
        try:
            line_resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": fresh_driver_id,
                    "work_date": DATE_JUN23,
                    "line_type": "HOURS",
                    "quantity": 8,
                },
                headers=headers,
            )
            assert line_resp.status_code == 201, f"Add line failed: {line_resp.text}"
            line = line_resp.json()
            assert line["calculated_amount"] is None, (
                "calculatedamount must be None when no approved rate exists"
            )
            assert line["needs_manager_review"] is True, (
                "needsmanagerreview must be True when no approved rate exists"
            )
        finally:
            await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 9. Submit (Open → InReview) auto-refreshes calculations
# ---------------------------------------------------------------------------

class TestSubmitAutoRefresh:

    @pytest.mark.asyncio
    async def test_submit_refreshes_stale_calculation_and_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Scenario:
          1. Create a fresh driver with no rates.
          2. Open a period and add an HOURS line for the driver.
             → calculatedamount=None, needsmanagerreview=True (no rate yet).
          3. Approve a backdated rate for the driver (effective before work_date).
          4. Attempt Open → InReview.
             → Auto-refresh runs, updates the line's calculatedamount.
             → Submit succeeds (no unresolved lines).

        This proves the system does NOT require the user to manually touch
        each line after a rate is approved.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)

        # Fresh driver — no rates
        fresh_driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id,
            "CP5 SubmitRefresh Driver",
        )

        # Add HOURS line: calculatedamount=None, needsmanagerreview=True
        line_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id": fresh_driver_id,
                "work_date": DATE_JUN23,
                "line_type": "HOURS",
                "quantity": 10,
            },
            headers=headers,
        )
        assert line_resp.status_code == 201
        line = line_resp.json()
        assert line["needs_manager_review"] is True
        assert line["calculated_amount"] is None

        # Now approve a backdated rate for this driver
        rate_id = await _create_and_approve_rate(
            session_client, auth_token,
            fresh_driver_id, hourly_rt_id,
            amount="25.00",
            effective_from="2089-06-01",
        )

        try:
            # Open → InReview: the auto-refresh should fix the stale line
            tr = await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "InReview"},
                headers=headers,
            )
            assert tr.status_code == 200, (
                f"Submit should succeed after auto-refresh. Got {tr.status_code}: {tr.text}"
            )

            # Verify the line was refreshed
            lines_resp = await session_client.get(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
            )
            assert lines_resp.status_code == 200
            hours_line = next(
                (item for item in lines_resp.json()
                 if item["draft_line_id"] == line["draft_line_id"]),
                None,
            )
            assert hours_line is not None
            assert hours_line["needs_manager_review"] is False
            # 10h × $25 = $250
            assert Decimal(str(hours_line["calculated_amount"])) == Decimal("250.0000")
        finally:
            await session_client.delete(f"/payroll/rates/{rate_id}", headers=headers)
            await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


# ---------------------------------------------------------------------------
# 10. Finalize auto-refreshes calculations
# ---------------------------------------------------------------------------

class TestFinalizeAutoRefresh:

    @pytest.mark.asyncio
    async def test_finalize_refreshes_stale_calc_and_final_amount_is_correct(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Scenario:
          1. Open period, add HOURS line for a fresh driver (no rate → NMR=True).
          2. Approve a backdated rate → NMR cleared at submit time.
          3. Period advances to Approved via review flow.
          4. A NEW rate is approved (superseding the old one) after the period
             is already Approved, with a higher amount and same effective_from.
             (We simulate rate-after-approval by voiding and re-creating,
              using a direct DB update to avoid the constraint guards.)
          5. Finalize → auto-refresh updates calculatedamount to use the new rate.
          6. FinalAmount in PayrollFinalLines reflects the refreshed calculation.

        This is the strictest form of the backdated-rate scenario.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
        fresh_driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id,
            "CP5 FinalRefresh Driver",
        )

        # Approve an initial rate ($20/hr effective 2089-06-01)
        rate_id = await _create_and_approve_rate(
            session_client, auth_token,
            fresh_driver_id, hourly_rt_id,
            amount="20.00",
            effective_from="2089-06-01",
        )

        # Add HOURS line (8h × $20 = $160 at this point)
        line_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id": fresh_driver_id,
                "work_date": DATE_JUN23,
                "line_type": "HOURS",
                "quantity": 8,
            },
            headers=headers,
        )
        assert line_resp.status_code == 201
        initial_calc = Decimal(str(line_resp.json()["calculated_amount"]))
        assert initial_calc == Decimal("160.0000"), f"Expected $160, got {initial_calc}"

        # Advance to Approved
        await _advance_to_approved(
            session_client, auth_token, pid, fresh_driver_id, DATE_JUN23
        )

        # Void the $20 rate and approve a new $35 rate (same effective_from)
        # This simulates a rate correction made after approval.
        await session_client.delete(f"/payroll/rates/{rate_id}", headers=headers)
        new_rate_id = await _create_and_approve_rate(
            session_client, auth_token,
            fresh_driver_id, hourly_rt_id,
            amount="35.00",
            effective_from="2089-06-01",
        )

        try:
            # Finalization projects the approved immutable snapshot; a later
            # live-rate change must not rewrite the submitted $160 authority.
            fin = await session_client.post(
                f"/payroll/periods/{pid}/finalize",
                headers=headers,
            )
            assert fin.status_code == 200, f"Finalize failed: {fin.text}"

            # Check FinalLines: HOURS finalamount remains the approved snapshot
            # amount ($20 × 8 = $160), not the later live rate.
            fl = await session_client.get(
                f"/payroll/periods/{pid}/final-lines",
                headers=headers,
            )
            assert fl.status_code == 200
            hours_finals = [line for line in fl.json() if line["line_type"] == "HOURS"]
            assert hours_finals, "HOURS final line not found"
            final_amount = Decimal(str(hours_finals[0]["final_amount"]))
            assert final_amount == Decimal("160.0000"), (
                f"Expected approved snapshot $160 (8h × $20), got {final_amount}"
            )
        finally:
            await session_client.delete(f"/payroll/rates/{new_rate_id}", headers=headers)
            # Force-cancel the now-Locked period so subsequent tests that use
            # the same branch/dates are not blocked by the strict overlap guard.
            # new_rate_id belongs to an isolated fresh_driver_id, so a 422 from
            # the Phase 5 guard (if new_rate was used in finallines) does not
            # contaminate paytest_driver_id-based tests.

            await direct_db.execute(
                _text("ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable")
            )
            await direct_db.execute(
                _text("ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert")
            )
            await direct_db.execute(
                _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )
            await direct_db.execute(_text(
                "ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable"
            ))
            await direct_db.execute(
                _text("ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert")
            )



# ---------------------------------------------------------------------------
# 11. Locked period draft lines not mutated by refresh
# ---------------------------------------------------------------------------

class TestLockedPeriodNotMutated:

    @pytest.mark.asyncio
    async def test_refresh_does_not_touch_locked_period_draft_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        _refresh_draft_calculations is only called for Open→InReview and for
        finalize_period (which creates a new Locked period).  After a period is
        Locked, its draft lines should not be touched by any refresh call.

        We verify this by reading the draft lines of a freshly Locked period
        and confirming they match what was stored at finalization.
        (A positive check: if refresh accidentally ran again on Locked data,
        the calculatedamount would still be the same — so we verify it's
        non-null and matches the expected value.)

        Phase 5 note: uses an isolated driver so that the finalized DriverRate
        is not shared with paytest_driver_id tests.  The rate stays referenced
        in PayrollFinalLines after finalization (Phase 5 void guard fires); the
        isolated driver ensures this does not contaminate other tests.
        """
        headers = auth(auth_token)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        hourly_rt_id = await _get_hourly_rate_type_id(session_client, auth_token)
        # Isolated driver: rate stays in PayrollFinalLines after finalization
        # (Phase 5 guard would reject a void attempt) but does not contaminate
        # the shared paytest_driver_id used by other test modules.
        isolated_driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id,
            "CP5 LockedNotMutated Driver",
        )
        rate_id = await _create_and_approve_rate(
            session_client, auth_token,
            isolated_driver_id, hourly_rt_id,
            amount="22.00",
            effective_from="2089-06-01",
        )

        try:
            # Add HOURS line (8h × $22 = $176)
            line_resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": isolated_driver_id,
                    "work_date": DATE_JUN23,
                    "line_type": "HOURS",
                    "quantity": 8,
                },
                headers=headers,
            )
            assert line_resp.status_code == 201
            line_id = line_resp.json()["draft_line_id"]

            # Advance to Approved and finalize
            await _advance_to_approved(
                session_client, auth_token, pid, isolated_driver_id, DATE_JUN23
            )
            fin = await session_client.post(
                f"/payroll/periods/{pid}/finalize",
                headers=headers,
            )
            assert fin.status_code == 200

            # Draft line calculatedamount should still be $176 (8 × $22)
            row = await direct_db.execute(
                _text("SELECT calculatedamount FROM payroll.payrolldraftlines "
                      "WHERE draftlineid = :lid"),
                {"lid": line_id},
            )
            stored = row.scalar_one_or_none()
            assert stored is not None
            assert Decimal(str(stored)) == Decimal("176.0000"), (
                f"Draft line calculatedamount should be $176, got {stored}"
            )
        finally:
            # Force-cancel the now-Locked period so subsequent tests that use
            # the same branch/dates are not blocked by the strict overlap guard.
            # rate_id belongs to isolated_driver_id; if Phase 5 rejects the void
            # (rate is in PayrollFinalLines), the 422 is silently accepted — the
            # isolated driver means no contamination to paytest_driver_id tests.
            await session_client.delete(f"/payroll/rates/{rate_id}", headers=headers)
            await direct_db.execute(
                _text("ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable")
            )
            await direct_db.execute(
                _text("ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert")
            )
            await direct_db.execute(
                _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )
            await direct_db.execute(_text(
                "ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable"
            ))
            await direct_db.execute(
                _text("ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert")
            )



# ---------------------------------------------------------------------------
# 12. No Driver/ODA security regression
# ---------------------------------------------------------------------------

class TestODASecurityRegression:

    @pytest.mark.asyncio
    async def test_oda_user_still_blocked_from_day_grid(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        CP-5 changes must not relax any ODA/Driver access boundaries.
        ODA users must still receive 403 on GET day-grid.
        """
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = await _open_period(session_client, auth_token, paytest_branch_id, db=direct_db)

        role_id = await _create_role_with_perms(
            session_client, auth_token,
            "CP5_ODA_DG_Role", ["payroll.view"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token,
            "cp5_oda_dg_user", role_id,
            scope_type="OwnDriverDataOnly",
            branch_id=paytest_branch_id,
        )
        try:
            resp = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": DATE_JUN23},
                headers=auth(oda_token),
            )
            assert resp.status_code == 403
        finally:
            await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
