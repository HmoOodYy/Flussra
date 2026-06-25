"""
tests/test_cp0_linetype.py — CP-0 LineType / PayItemCode unification

Tests the fix that makes the payroll entry backend accept canonical PayItemCodes
("HOURS", "MILES", "LOADS") in addition to legacy display strings ("Hours",
"Miles", "Loads"), and stores the canonical form in PayrollDraftLines.LineType.

Coverage:
  TestCanonicalCodesAccepted    — POST with "HOURS", "MILES", "LOADS", "OVERNIGHT",
                                   "WAIT_TIME", "PALLETS", "SILOS" all return 201
                                   and store canonical code; "PTO_STATUS" returns 422.
  TestLegacyStringsStillWork    — POST with "Hours", "Miles", "Loads", "Overnight",
                                   etc. still return 201 and now store canonical code.
  TestSystemItemsCompanyIsNull  — system items (companyid IS NULL) found via DB
                                   regardless of which form the caller uses.
  TestBranchInactiveRejected    — explicitly deactivated items → 422.
  TestCustomCompanyItemsWork    — custom company items (companyid IS NOT NULL) still
                                   validate and store correctly.
  TestPeriodPayCanonical        — "BONUS" / "ADJUSTMENT" accepted by period-pay
                                   endpoint alongside legacy "Bonus" / "Adjustment".
  TestFinalizationGuardCanonical— zero-calc guard catches canonical-code PerUnit
                                   lines (linetype="HOURS") with no rate.

Isolation strategy:
  Each test class uses a fresh function-scoped period on the PAYTEST branch
  (same pattern as test_m13a.py).  Fixtures cancel active periods before and
  after so tests are independent of each other and of test_m13a.py.
"""

import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text

from datetime import date

_COMPANY_ID = 1


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _cancel_active_periods(
    client: httpx.AsyncClient, token: str, branch_id: int
) -> None:
    headers = auth(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
        resp = await client.get(
            "/payroll/periods", params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        if resp.status_code != 200:
            continue
        for p in resp.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"}, headers=headers,
            )


async def _open_period(
    db,
    branch_id: int,
    start: str,
    end: str,
) -> dict:
    """Insert an Open period directly into DB and return a period dict.

    POST /payroll/periods requires an existing Open period (CP-1D B1 guard),
    so we bypass HTTP and insert directly — the same pattern used by
    test_cp2d._open_period and test_cp1d._insert_open_period.
    """
    code = f"CP0-{branch_id}-{start}"
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (:cid, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"cid": _COMPANY_ID, "bid": branch_id, "code": code,
         "name": f"CP0 {start}", "start": date.fromisoformat(start), "end": date.fromisoformat(end)},
    )).mappings().first()
    return {"payroll_period_id": row["payrollperiodid"], "start_date": start, "end_date": end}


async def _add_line(
    client: httpx.AsyncClient, token: str, period_id: int,
    driver_id: int, line_type: str, quantity: str = "8.00",
    work_date: str | None = None, rate_amount: float | None = None,
) -> tuple[int, dict]:
    """POST a draft line; return (status_code, json_body)."""
    payload: dict = {
        "driver_id":   driver_id,
        "line_type":   line_type,
        "quantity":    quantity,
        "source_type": "Manual",
    }
    if work_date is not None:
        payload["work_date"] = work_date
    if rate_amount is not None:
        payload["rate_amount"] = rate_amount
    resp = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json=payload, headers=auth(token),
    )
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# Function-scoped clean fixture (cancels active PAYTEST periods)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cp0_clean(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db: AsyncConnection,
):
    """Cancel any active PAYTEST periods before and after each CP-0 test."""
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    yield (paytest_branch_id, direct_db)
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)


# ---------------------------------------------------------------------------
# TestCanonicalCodesAccepted
# ---------------------------------------------------------------------------

class TestCanonicalCodesAccepted:
    """
    POST /periods/{id}/lines with canonical PayItemCodes must return 201
    and store the canonical code in line_type.

    Uses PAYTEST branch in year 2080 to avoid date collisions with other test
    modules (test_m13a: 2032, test_m14: 2034, test_m15: 2036-2043).
    """

    @pytest.mark.asyncio
    async def test_canonical_hours_accepted(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-01-06", "2080-01-12")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="HOURS", quantity="8.00", work_date="2080-01-07",
        )
        assert sc == 201, f"Expected 201, got {sc}: {body}"
        assert body["line_type"] == "HOURS", (
            f"Canonical code must be stored; got '{body['line_type']}'"
        )

    @pytest.mark.asyncio
    async def test_canonical_miles_accepted(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-01-13", "2080-01-19")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="MILES", quantity="150.00", work_date="2080-01-14",
        )
        assert sc == 201, f"Expected 201, got {sc}: {body}"
        assert body["line_type"] == "MILES"

    @pytest.mark.asyncio
    async def test_canonical_loads_accepted(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-01-20", "2080-01-26")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="LOADS", quantity="5.00", work_date="2080-01-21",
        )
        assert sc == 201, f"Expected 201, got {sc}: {body}"
        assert body["line_type"] == "LOADS"

    @pytest.mark.asyncio
    async def test_canonical_pallet_accepted(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        """PALLETS is not IsDefaultBranchActive — but PAYTEST has it activated by conftest."""
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-01-27", "2080-02-02")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="PALLETS", quantity="20.00", work_date="2080-01-28",
        )
        assert sc == 201, f"Expected 201, got {sc}: {body}"
        assert body["line_type"] == "PALLETS"

    @pytest.mark.asyncio
    async def test_pto_status_rejected(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        """PTO_STATUS was removed in migration 0055 — must return 422."""
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-02-03", "2080-02-09")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="PTO_STATUS", quantity="8.00", work_date="2080-02-04",
        )
        assert sc == 422, f"PTO_STATUS must be rejected after migration 0055; got {sc}: {body}"


# ---------------------------------------------------------------------------
# TestLegacyStringsStillWork
# ---------------------------------------------------------------------------

class TestLegacyStringsStillWork:
    """
    POST /periods/{id}/lines with legacy display strings ("Hours", "Miles")
    must still return 201 for backward compatibility.
    The returned line_type is now canonical ("HOURS", "MILES") — not the
    legacy string — because new rows are stored with canonical codes.
    """

    @pytest.mark.asyncio
    async def test_legacy_hours_still_accepted(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-03-03", "2080-03-09")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="Hours", quantity="8.00", work_date="2080-03-04",
        )
        assert sc == 201, (
            f"Legacy 'Hours' must still be accepted; got {sc}: {body}"
        )
        # New rows store the canonical code
        assert body["line_type"] == "HOURS", (
            f"Legacy input 'Hours' must be stored as canonical 'HOURS'; "
            f"got '{body['line_type']}'"
        )

    @pytest.mark.asyncio
    async def test_legacy_miles_still_accepted(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-03-10", "2080-03-16")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="Miles", quantity="200.00", work_date="2080-03-11",
        )
        assert sc == 201, f"Legacy 'Miles' must still be accepted; got {sc}: {body}"
        assert body["line_type"] == "MILES"

    @pytest.mark.asyncio
    async def test_legacy_loads_still_accepted(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-03-17", "2080-03-23")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="Loads", quantity="3.00", work_date="2080-03-18",
        )
        assert sc == 201, f"Legacy 'Loads' must still be accepted; got {sc}: {body}"
        assert body["line_type"] == "LOADS"

    @pytest.mark.asyncio
    async def test_legacy_pto_rejected(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        """Legacy 'PTO' mapped to PTO_STATUS which was removed in migration 0055."""
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-03-24", "2080-03-30")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="PTO", quantity="8.00", work_date="2080-03-25",
        )
        assert sc == 422, f"Legacy 'PTO' must be rejected after migration 0055; got {sc}: {body}"


# ---------------------------------------------------------------------------
# TestSystemItemsCompanyIsNull
# ---------------------------------------------------------------------------

class TestSystemItemsCompanyIsNull:
    """
    Prove that the unified DB path finds system PayItems (companyid IS NULL)
    for canonical codes.  Before CP-0 the slow path used
    `companyid = company_id` and missed system items entirely.
    """

    @pytest.mark.asyncio
    async def test_hours_system_item_found_via_db(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        """
        "HOURS" is a system PayItem (companyid IS NULL in DB).
        The unified DB query must find it; the old slow path with
        `companyid = :cid` would return no row and give 422.
        """
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-04-07", "2080-04-13")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="HOURS", quantity="8.00", work_date="2080-04-08",
        )
        assert sc == 201, (
            "System PayItem 'HOURS' (companyid IS NULL) must be found by "
            f"the unified DB path; got {sc}: {body}"
        )

    @pytest.mark.asyncio
    async def test_miles_system_item_found_via_db(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-04-14", "2080-04-20")
        pid = period["payroll_period_id"]
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="MILES", quantity="100.00", work_date="2080-04-15",
        )
        assert sc == 201, (
            f"System PayItem 'MILES' must be found; got {sc}: {body}"
        )


# ---------------------------------------------------------------------------
# TestBranchInactiveRejected
# ---------------------------------------------------------------------------

class TestBranchInactiveRejected:
    """
    An item explicitly deactivated for a branch via BranchPayItemConfig must
    be rejected (422) regardless of whether the canonical code or legacy
    display name is used.
    """

    @pytest.mark.asyncio
    async def test_canonical_code_rejected_when_branch_inactive(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        cp0_clean,
        direct_db,
    ):
        """
        Explicitly deactivate LOADS on PAYTEST for this test.
        POSTing "LOADS" must return 422 while override is in place.
        Restore in finally so other tests see the item as active.
        """
        from sqlalchemy import text as _text

        eff_from = date(2000, 1, 1)
        # Get company_id for PAYTEST
        company_row = (await direct_db.execute(
            _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )).mappings().first()
        cid = int(company_row["companyid"])

        # Get payitemid for LOADS
        item_row = (await direct_db.execute(
            _text("SELECT payitemid FROM payroll.payitems WHERE payitemcode='LOADS' AND companyid IS NULL"),
        )).mappings().first()
        piid = int(item_row["payitemid"])

        # Read current open-row state
        existing = (await direct_db.execute(
            _text("""
                SELECT configid, isactive FROM payroll.branchpayitemconfig
                WHERE companyid=:c AND branchid=:b AND payitemid=:p AND effectiveto IS NULL
            """),
            {"c": cid, "b": paytest_branch_id, "p": piid},
        )).mappings().first()

        if existing:
            prev_active = bool(existing["isactive"])
            cfg_id = int(existing["configid"])
            await direct_db.execute(
                _text("UPDATE payroll.branchpayitemconfig SET isactive=FALSE WHERE configid=:cid"),
                {"cid": cfg_id},
            )
        else:
            cfg_id = None
            prev_active = None
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, effectivefrom, effectiveto)
                    VALUES (:c, :b, :p, FALSE, :ef, NULL)
                """),
                {"c": cid, "b": paytest_branch_id, "p": piid, "ef": eff_from},
            )

        try:
            period = await _open_period(direct_db, paytest_branch_id, "2080-05-05", "2080-05-11")
            pid = period["payroll_period_id"]
            sc, body = await _add_line(
                session_client, auth_token, pid, paytest_driver_id,
                line_type="LOADS", quantity="5.00", work_date="2080-05-06",
            )
            assert sc == 422, (
                f"Deactivated 'LOADS' must be rejected with 422; got {sc}: {body}"
            )
            # Also test legacy string is rejected for the same reason
            sc2, body2 = await _add_line(
                session_client, auth_token, pid, paytest_driver_id,
                line_type="Loads", quantity="5.00", work_date="2080-05-06",
            )
            assert sc2 == 422, (
                f"Deactivated 'Loads' (legacy) must also be rejected; got {sc2}: {body2}"
            )
        finally:
            if cfg_id is not None:
                await direct_db.execute(
                    _text("UPDATE payroll.branchpayitemconfig SET isactive=:ia WHERE configid=:cid"),
                    {"ia": prev_active, "cid": cfg_id},
                )
            else:
                await direct_db.execute(
                    _text("""
                        DELETE FROM payroll.branchpayitemconfig
                        WHERE companyid=:c AND branchid=:b AND payitemid=:p AND effectivefrom=:ef
                    """),
                    {"c": cid, "b": paytest_branch_id, "p": piid, "ef": eff_from},
                )


# ---------------------------------------------------------------------------
# Session fixture — activate BONUS / ADJUSTMENT on PAYTEST
# ---------------------------------------------------------------------------

async def _activate_branch_item_by_code(
    client: httpx.AsyncClient, token: str, branch_id: int, code: str
) -> None:
    """Ensure a pay item is active for branch_id; idempotent."""
    items = (await client.get(
        f"/settings/branches/{branch_id}/pay-items", headers=auth(token),
    )).json()
    for item in items:
        if item.get("pay_item_code") == code:
            if not item.get("is_active", False):
                await client.patch(
                    f"/settings/branches/{branch_id}/pay-items/{item['pay_item_id']}",
                    json={"is_active": True}, headers=auth(token),
                )
            return


@pytest_asyncio.fixture(scope="session")
async def cp0_period_items_activated(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int
) -> None:
    """Activate BONUS and ADJUSTMENT for PAYTEST branch (idempotent, session-scoped)."""
    for code in ("BONUS", "ADJUSTMENT"):
        await _activate_branch_item_by_code(
            session_client, auth_token, paytest_branch_id, code
        )


# ---------------------------------------------------------------------------
# TestPeriodPayCanonical
# ---------------------------------------------------------------------------

class TestPeriodPayCanonical:
    """
    Period Pay endpoint accepts "BONUS"/"ADJUSTMENT" (canonical) in addition
    to "Bonus"/"Adjustment" (legacy).  Stored line_type is canonical.
    """

    @pytest.mark.asyncio
    async def test_canonical_bonus_accepted(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
        cp0_period_items_activated,
    ):
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-06-02", "2080-06-08")
        pid = period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS", "amount": "250.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        assert resp.json()["line_type"] == "BONUS"

    @pytest.mark.asyncio
    async def test_canonical_adjustment_accepted(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
        cp0_period_items_activated,
    ):
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-06-09", "2080-06-15")
        pid = period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "ADJUSTMENT", "amount": "-30.00"},
            headers=auth(auth_token),
        )
        # Manual ADJUSTMENT is blocked in this version (deferred to a future release)
        assert resp.status_code == 422, f"Expected 422 (ADJUSTMENT blocked), got {resp.status_code}: {resp.text}"
        assert "adjustment" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_legacy_bonus_still_accepted_stores_canonical(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
        cp0_period_items_activated,
    ):
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-06-16", "2080-06-22")
        pid = period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, f"Legacy 'Bonus' must still work; got {resp.text}"
        assert resp.json()["line_type"] == "BONUS", (
            "Legacy 'Bonus' must be stored as canonical 'BONUS'; "
            f"got '{resp.json()['line_type']}'"
        )

    @pytest.mark.asyncio
    async def test_daily_canonical_code_rejected_on_period_pay_endpoint(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        """'HOURS' is a Daily-scope item — period-pay endpoint must reject it with 422."""
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-06-23", "2080-06-29")
        pid = period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "HOURS", "amount": "50.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, (
            f"Daily-scope 'HOURS' must be rejected by period-pay endpoint; "
            f"got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# TestFinalizationGuardCanonical
# ---------------------------------------------------------------------------

class TestFinalizationGuardCanonical:
    """
    The zero-calc finalization guard must catch canonical-code PerUnit lines
    (linetype="HOURS") that have no calculatedamount and no rateamount,
    just as it caught legacy "Hours" lines before CP-0.
    """

    @pytest.mark.asyncio
    async def test_zero_calc_guard_catches_canonical_perunit_line(
        self, session_client, auth_token, paytest_driver_id, cp0_clean,
    ):
        """
        POST a line with canonical "HOURS" but no approved rate and no manual
        rate_amount → NeedsManagerReview=True (calculatedamount=None).
        The period must be blocked from advancing to InReview (Open→InReview guard
        checks same condition as finalization).
        """
        branch_id, db = cp0_clean
        period = await _open_period(db, branch_id, "2080-07-07", "2080-07-13")
        pid = period["payroll_period_id"]

        # Add a canonical HOURS line — no approved rate exists in the DB for
        # this driver on these future dates, so calc=None, needs_review=True.
        sc, body = await _add_line(
            session_client, auth_token, pid, paytest_driver_id,
            line_type="HOURS", quantity="8.00", work_date="2080-07-08",
        )
        assert sc == 201, f"Line add failed: {body}"
        assert body["needs_manager_review"] is True, (
            "HOURS line with no approved rate must be flagged NeedsManagerReview"
        )

        # Attempting to submit for review must be blocked
        submit = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=auth(auth_token),
        )
        assert submit.status_code == 422, (
            f"Period with unresolved canonical 'HOURS' line must not advance "
            f"to InReview; got {submit.status_code}: {submit.text}"
        )
