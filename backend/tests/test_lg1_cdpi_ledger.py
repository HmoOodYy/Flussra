"""
LG-1: CDPI Finalized Lines / Ledger Backend Verification.

Verifies that CDPI PerUnit finalized payroll lines are visible through the
existing finalized/ledger backend read path without any CDPI-specific code.

Ledger endpoint: GET /payroll/periods/{id}/final-lines
Period summary:  GET /payroll/periods (status=Locked)

Audit finding: the existing generic path already works — _FINAL_SELECT fetches
all PayrollFinalLines rows without PayItem-type filtering, and FinalLineSummary
already exposes pay_item_id, rate_type_id, driver_rate_id, resolved_rate_amount,
rate_behavior, and source_snapshot.  LG-1 is a tests-only phase.

Tests
-----
LG1  CDPI finalized line appears in GET /final-lines with correct identity/amount fields
LG2  FinalLine typed fields and SourceSnapshot prove exact immutable CDPI SnapshotLine provenance
LG3  GET /final-lines includes CDPI amount alongside a standard line; both correct
LG4  Finalized period blocks CDPI day-grid edits (403 from POST /day-grid)
LG5  Standard finalized HOURS line still appears correctly (regression)
LG6  PeriodSummary.final_gross includes CDPI final amount

Year slots: all use 2093 dates (unused by other test files).
CDPI items created via cdpi_service.create_direct_company_item (real PR-1B path).
"""
import datetime
import itertools
import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text as _text

from app.cdpi import service as cdpi_service
from app.cdpi.schemas import CdpiDirectCreateRequest
from app.payroll.service import _create_period_pay_item_rows

# ---------------------------------------------------------------------------
# Year slot constants
# ---------------------------------------------------------------------------
LG_START, LG_END, LG_WORK = "2093-03-03", "2093-03-16", "2093-03-05"
_RATE_EFFECTIVE_FROM = "2090-01-01"
_PERIOD_SLOT = itertools.count()
_PERIOD_WORK_DATES: dict[int, str] = {}

FINALIZE_URL  = "/payroll/periods/{pid}/finalize"
FINAL_LINES   = "/payroll/periods/{pid}/final-lines"
DAY_GRID_POST = "/payroll/periods/{pid}/day-grid"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tok(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_periods(client, token, branch_id):
    """Retain prior-period history; each test now receives a unique date slot."""
    del client, token, branch_id


async def _get_ids(db):
    company_id = (await db.execute(
        _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO'")
    )).scalar_one()
    admin_id = (await db.execute(
        _text("SELECT userid FROM sec.users WHERE username = 'admin'")
    )).scalar_one()
    return company_id, admin_id


async def _create_driver(client, token, branch_id, suffix):
    r = await client.post("/core/drivers", json={
        "branch_id": branch_id,
        "full_name": f"LG1 {suffix}",
        "driver_code": f"LG1-{suffix[:8]}",
        "cdl_number": f"CDL-LG1-{suffix[:6]}",
        "email": f"lg1{suffix[:6].lower().replace('-','')}@example.com",
        "hire_date": "2093-01-01",
    }, headers=_tok(token))
    assert r.status_code == 201, f"create_driver: {r.text}"
    return r.json()["driver_id"]


async def _delete_driver(client, token, driver_id):
    """Retain drivers referenced by finalized fixtures for the test database lifetime."""
    del client, token, driver_id


async def _open_period(db, branch_id):
    start = datetime.date.fromisoformat(LG_START) + datetime.timedelta(
        days=21 * next(_PERIOD_SLOT)
    )
    end = start + datetime.timedelta(days=13)
    pid = (await db.execute(_text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
        VALUES (1, :branch_id, 'Open', :period_code, :period_name, 'Week', :start_date, :end_date)
        RETURNING payrollperiodid
    """), {
        "branch_id": branch_id,
        "period_code": f"LG1-{start.isoformat()}",
        "period_name": f"LG1 {start.isoformat()}",
        "start_date": start,
        "end_date": end,
    })).scalar_one()
    await _create_period_pay_item_rows(pid, 1, branch_id, start, db)
    await db.commit()
    _PERIOD_WORK_DATES[pid] = (start + datetime.timedelta(days=2)).isoformat()
    return pid


async def _advance_to_approved(client, token, pid, driver_id, line_type, quantity):
    headers = _tok(token)
    r = await client.post(f"/payroll/periods/{pid}/lines", json={
        "driver_id": driver_id, "work_date": _PERIOD_WORK_DATES[pid],
        "line_type": line_type, "quantity": quantity,
    }, headers=headers)
    assert r.status_code == 201, f"add_line: {r.text}"

    r = await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "InReview"}, headers=headers)
    assert r.status_code == 200

    items = await client.get("/review/items", headers=headers)
    item = next(
        (i for i in items.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(pid)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, "review item not found"
    dec = await client.post(f"/review/items/{item['review_item_id']}/decide",
                            json={"decision": "Approved"}, headers=headers)
    assert dec.status_code == 200


async def _finalize(client, token, pid):
    r = await client.post(FINALIZE_URL.format(pid=pid), headers=_tok(token))
    assert r.status_code == 200, f"finalize: {r.text}"
    return r.json()


async def _force_cleanup_period(db, pid):
    """Retain finalized periods and their immutable evidence until DB teardown."""
    del db, pid


async def _cleanup_cdpi_item(db, *, pay_item_id: int):
    """Retain catalog rows referenced by finalized fixtures until DB teardown."""
    del db, pay_item_id


async def _activate_branch(db, *, pay_item_id, company_id, branch_id):
    await db.execute(_text("""
        DELETE FROM payroll.branchpayitemconfig
        WHERE payitemid = :pid AND companyid = :cid AND branchid = :bid
    """), {"pid": pay_item_id, "cid": company_id, "bid": branch_id})
    await db.execute(_text("""
        INSERT INTO payroll.branchpayitemconfig
            (companyid, branchid, payitemid, isactive, effectivefrom)
        VALUES (:cid, :bid, :pid, TRUE, '2000-01-01')
    """), {"cid": company_id, "bid": branch_id, "pid": pay_item_id})


async def _create_and_approve_rate(client, token, driver_id, rate_type_id, amount):
    headers = _tok(token)
    r = await client.post("/payroll/rates", json={
        "driver_id": driver_id, "rate_type_id": rate_type_id,
        "amount": amount, "effective_from": _RATE_EFFECTIVE_FROM,
    }, headers=headers)
    assert r.status_code == 201, f"create rate: {r.text}"
    rid = r.json()["driver_rate_id"]
    r = await client.post(f"/payroll/rates/{rid}/approve", headers=headers)
    assert r.status_code == 200, f"approve rate: {r.text}"
    return rid


# ---------------------------------------------------------------------------
# LG1: CDPI finalized line appears in GET /final-lines with correct fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lg1_cdpi_final_line_in_ledger(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    LG1: After finalizing a period with a CDPI PerUnit line, GET /final-lines
    returns a row that matches the CDPI PayItem and has correct financial fields.
    """
    cid, admin_id = await _get_ids(direct_db)
    pay_item_id = None
    driver_id   = None
    pid         = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="LG1 Wait Hours",
                                    input_type="Number", calc_method_key="PerUnit"),
            direct_db,
        )
        pay_item_id = result.pay_item_id

        rate_type_id = (await direct_db.execute(_text(
            "SELECT ratetypeid FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
            {"pid": pay_item_id})).scalar_one()

        await _activate_branch(
            direct_db, pay_item_id=pay_item_id,
            company_id=cid, branch_id=paytest_branch_id,
        )

        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "LG1A")
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id, amount="20.00")

        pid = await _open_period(direct_db, paytest_branch_id)
        await _advance_to_approved(session_client, auth_token, pid, driver_id,
                                   line_type=result.pay_item_code, quantity="3")
        await _finalize(session_client, auth_token, pid)

        r = await session_client.get(
            FINAL_LINES.format(pid=pid),
            headers=_tok(auth_token),
        )
        assert r.status_code == 200, f"GET /final-lines: {r.text}"
        lines = r.json()

        cdpi_lines = [line for line in lines if line.get("pay_item_id") == pay_item_id]
        assert len(cdpi_lines) == 1, (
            f"Expected 1 final line for CDPI pay_item_id={pay_item_id}, "
            f"got {len(cdpi_lines)}"
        )
        line = cdpi_lines[0]

        # Identity
        assert line["line_type"] == result.pay_item_code
        assert line["pay_item_id"] == pay_item_id
        assert line["rate_type_id"] == rate_type_id
        assert line["driver_rate_id"] == rate_id

        # Financial values
        assert Decimal(str(line["quantity"])) == Decimal("3")
        assert Decimal(str(line["resolved_rate_amount"])) == Decimal("20.00")
        assert Decimal(str(line["final_amount"])) == Decimal("60.00")  # 3 * 20

        # Rate behavior
        assert line.get("rate_behavior") == "PerUnit"

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if pay_item_id:
            await _cleanup_cdpi_item(direct_db, pay_item_id=pay_item_id)


# ---------------------------------------------------------------------------
# LG2: FinalLine typed fields and exact immutable SnapshotLine provenance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lg2_cdpi_final_line_source_snapshot(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """LG2: FinalLine fields and SourceSnapshot resolve one exact CDPI snapshot line."""
    cid, admin_id = await _get_ids(direct_db)
    pay_item_id = None
    driver_id   = None
    pid         = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="LG2 Snapshot Item",
                                    input_type="Number", calc_method_key="PerUnit"),
            direct_db,
        )
        pay_item_id = result.pay_item_id

        rate_type_id = (await direct_db.execute(_text(
            "SELECT ratetypeid FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
            {"pid": pay_item_id})).scalar_one()

        await _activate_branch(
            direct_db, pay_item_id=pay_item_id,
            company_id=cid, branch_id=paytest_branch_id,
        )

        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "LG2A")
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id, amount="15.00")

        pid = await _open_period(direct_db, paytest_branch_id)
        await _advance_to_approved(session_client, auth_token, pid, driver_id,
                                   line_type=result.pay_item_code, quantity="4")
        await _finalize(session_client, auth_token, pid)

        r = await session_client.get(
            FINAL_LINES.format(pid=pid),
            headers=_tok(auth_token),
        )
        assert r.status_code == 200
        lines = r.json()
        cdpi_lines = [line for line in lines if line.get("pay_item_id") == pay_item_id]
        assert len(cdpi_lines) == 1
        line = cdpi_lines[0]

        # FinalLines retain the typed financial identity used by the final projection.
        assert line["pay_item_id"] == pay_item_id
        assert line["rate_type_id"] == rate_type_id
        assert line["driver_rate_id"] == rate_id
        assert Decimal(str(line["resolved_rate_amount"])) == Decimal("15.00")
        assert line["rate_behavior"] == "PerUnit"

        snap = line.get("source_snapshot")
        assert snap is not None, "source_snapshot must not be None for a CDPI line"
        if isinstance(snap, str):
            snap = json.loads(snap)

        required_provenance = {
            "payroll_calculation_snapshot_id",
            "revision_number",
            "snapshot_hash",
            "snapshot_line_id",
            "source_type",
            "source_id",
            "source_evidence",
        }
        assert required_provenance <= snap.keys()
        assert snap["source_type"] == "DraftLine"
        assert str(snap["source_id"]) == str(line["draft_line_id"])
        assert isinstance(snap["source_evidence"], dict)

        snapshot_line = (await direct_db.execute(_text("""
            SELECT sl.payrollcalculationsnapshotlineid,
                   sl.payitemid,
                   sl.ratetypeid,
                   sl.driverrateid,
                   sl.resolvedrateamount,
                   sl.quantity,
                   sl.calculatedamount,
                   sl.linetype,
                   sl.linescope,
                   sl.sourcetype,
                   sl.sourceid,
                   sl.sourceevidencejsonb,
                   totals.payrollcalculationsnapshotid
            FROM payroll.payrollcalculationsnapshotlines sl
            JOIN payroll.payrollcalculationdrivertotals totals
              ON totals.payrollcalculationdrivertotalid = sl.payrollcalculationdrivertotalid
            WHERE sl.payrollcalculationsnapshotlineid = :snapshot_line_id
        """), {"snapshot_line_id": snap["snapshot_line_id"]})).mappings().one()

        assert snapshot_line["payrollcalculationsnapshotid"] == snap[
            "payroll_calculation_snapshot_id"
        ]
        assert snapshot_line["payitemid"] == pay_item_id
        assert snapshot_line["ratetypeid"] == rate_type_id
        assert snapshot_line["driverrateid"] == rate_id
        assert Decimal(str(snapshot_line["resolvedrateamount"])) == Decimal("15.00")
        assert Decimal(str(snapshot_line["quantity"])) == Decimal("4")
        assert Decimal(str(snapshot_line["calculatedamount"])) == Decimal("60.00")
        assert snapshot_line["linetype"] == result.pay_item_code
        assert snapshot_line["linescope"] == line["line_scope"]
        assert snapshot_line["sourcetype"] == "DraftLine"
        assert str(snapshot_line["sourceid"]) == str(snap["source_id"])
        assert snapshot_line["sourceevidencejsonb"] == snap["source_evidence"]

        source_draft_line = (await direct_db.execute(_text("""
            SELECT draftlineid
            FROM payroll.payrolldraftlines
            WHERE draftlineid = :draft_line_id
              AND payrollperiodid = :period_id
              AND driverid = :driver_id
              AND linetype = :line_type
        """), {
            "draft_line_id": int(str(snap["source_id"])),
            "period_id": pid,
            "driver_id": driver_id,
            "line_type": result.pay_item_code,
        })).scalar_one()
        assert source_draft_line == line["draft_line_id"]

        snapshot_header = (await direct_db.execute(_text("""
            SELECT payrollcalculationsnapshotid,
                   revisionnumber,
                   snapshothash,
                   payrollperiodid,
                   companyid,
                   branchid
            FROM payroll.payrollcalculationsnapshots
            WHERE payrollcalculationsnapshotid = :snapshot_id
        """), {"snapshot_id": snap["payroll_calculation_snapshot_id"]})).mappings().one()

        assert snapshot_header["payrollcalculationsnapshotid"] == snap[
            "payroll_calculation_snapshot_id"
        ]
        assert snapshot_header["revisionnumber"] == snap["revision_number"]
        assert snapshot_header["snapshothash"] == snap["snapshot_hash"]
        assert snapshot_header["payrollperiodid"] == pid
        assert snapshot_header["companyid"] == cid
        assert snapshot_header["branchid"] == paytest_branch_id

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if pay_item_id:
            await _cleanup_cdpi_item(direct_db, pay_item_id=pay_item_id)


# ---------------------------------------------------------------------------
# LG3: GET /final-lines includes CDPI amount alongside a standard line
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lg3_ledger_includes_cdpi_amount_with_standard_line(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    LG3: When a period contains both a standard HOURS line and a CDPI PerUnit
    line, GET /final-lines returns both.  Amounts are independently correct.
    """
    cid, admin_id = await _get_ids(direct_db)
    pay_item_id  = None
    driver_id    = None
    pid          = None
    hourly_rate_id = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        # --- CDPI item ---
        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="LG3 Extra Pay",
                                    input_type="Number", calc_method_key="PerUnit"),
            direct_db,
        )
        pay_item_id = result.pay_item_id

        cdpi_rt_id = (await direct_db.execute(_text(
            "SELECT ratetypeid FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
            {"pid": pay_item_id})).scalar_one()

        await _activate_branch(
            direct_db, pay_item_id=pay_item_id,
            company_id=cid, branch_id=paytest_branch_id,
        )

        # --- HOURLY rate type ---
        r = await session_client.get("/payroll/rate-types", headers=_tok(auth_token))
        assert r.status_code == 200
        hourly_rt_id = next(
            rt["rate_type_id"] for rt in r.json() if rt["rate_code"] == "HOURLY"
        )

        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "LG3A")
        hourly_rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, hourly_rt_id, amount="18.00")
        cdpi_rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, cdpi_rt_id, amount="10.00")

        pid = await _open_period(direct_db, paytest_branch_id)

        # Add HOURS line
        headers = _tok(auth_token)
        rh = await session_client.post(f"/payroll/periods/{pid}/lines", json={
            "driver_id": driver_id, "work_date": _PERIOD_WORK_DATES[pid],
            "line_type": "HOURS", "quantity": "8",
        }, headers=headers)
        assert rh.status_code == 201, f"add hours: {rh.text}"

        # Add CDPI line
        rc = await session_client.post(f"/payroll/periods/{pid}/lines", json={
            "driver_id": driver_id, "work_date": _PERIOD_WORK_DATES[pid],
            "line_type": result.pay_item_code, "quantity": "5",
        }, headers=headers)
        assert rc.status_code == 201, f"add cdpi: {rc.text}"

        # Advance to Approved
        r = await session_client.patch(f"/payroll/periods/{pid}/status",
                                       json={"status": "InReview"}, headers=headers)
        assert r.status_code == 200
        items = await session_client.get("/review/items", headers=headers)
        item = next(
            (i for i in items.json()
             if i.get("entity_name") == "PayrollPeriods"
             and i.get("entity_id") == str(pid)
             and i.get("status") == "Pending"),
            None,
        )
        assert item is not None
        dec = await session_client.post(
            f"/review/items/{item['review_item_id']}/decide",
            json={"decision": "Approved"}, headers=headers)
        assert dec.status_code == 200

        await _finalize(session_client, auth_token, pid)

        r = await session_client.get(FINAL_LINES.format(pid=pid), headers=headers)
        assert r.status_code == 200
        lines = r.json()

        # HOURS line
        hours_lines = [line for line in lines if line["line_type"] == "HOURS"]
        assert len(hours_lines) >= 1, "HOURS final line missing"
        assert Decimal(str(hours_lines[0]["final_amount"])) == Decimal("144.00")  # 8 * 18

        # CDPI line
        cdpi_lines = [line for line in lines if line.get("pay_item_id") == pay_item_id]
        assert len(cdpi_lines) == 1, "CDPI final line missing"
        assert Decimal(str(cdpi_lines[0]["final_amount"])) == Decimal("50.00")  # 5 * 10

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if hourly_rate_id:
            # Delete HOURLY rate created for shared-adjacent driver to avoid guard conflicts
            pass
        if pay_item_id:
            await _cleanup_cdpi_item(direct_db, pay_item_id=pay_item_id)


# ---------------------------------------------------------------------------
# LG4: Finalized period blocks CDPI day-grid edits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lg4_finalized_period_blocks_cdpi_day_grid_edits(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    LG4: After finalization, attempting to save the day grid for a CDPI column
    is rejected by the existing period-status guard (403, not Open/InReview).
    The final line is unchanged.
    """
    cid, admin_id = await _get_ids(direct_db)
    pay_item_id = None
    driver_id   = None
    pid         = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="LG4 Block Test",
                                    input_type="Number", calc_method_key="PerUnit"),
            direct_db,
        )
        pay_item_id = result.pay_item_id

        rate_type_id = (await direct_db.execute(_text(
            "SELECT ratetypeid FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
            {"pid": pay_item_id})).scalar_one()

        await _activate_branch(
            direct_db, pay_item_id=pay_item_id,
            company_id=cid, branch_id=paytest_branch_id,
        )

        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "LG4A")
        await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id, amount="10.00")

        pid = await _open_period(direct_db, paytest_branch_id)
        await _advance_to_approved(session_client, auth_token, pid, driver_id,
                                   line_type=result.pay_item_code, quantity="2")
        await _finalize(session_client, auth_token, pid)

        # Attempt day-grid save on the now-Locked period
        r = await session_client.post(
            DAY_GRID_POST.format(pid=pid),
            json={
                "work_date": _PERIOD_WORK_DATES[pid],
                "rows": [{
                    "driver_id": driver_id,
                    "values": {result.pay_item_code: "99"},
                }],
            },
            headers=_tok(auth_token),
        )
        assert r.status_code == 403, (
            f"Expected 403 for Locked period day-grid save, got {r.status_code}: {r.text}"
        )

        # Final line is unchanged (still 2 * 10 = 20)
        fl = await session_client.get(FINAL_LINES.format(pid=pid), headers=_tok(auth_token))
        assert fl.status_code == 200
        cdpi_lines = [line for line in fl.json() if line.get("pay_item_id") == pay_item_id]
        assert len(cdpi_lines) == 1
        assert Decimal(str(cdpi_lines[0]["final_amount"])) == Decimal("20.00")

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if pay_item_id:
            await _cleanup_cdpi_item(direct_db, pay_item_id=pay_item_id)


# ---------------------------------------------------------------------------
# LG5: Standard finalized HOURS line regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lg5_standard_hours_final_line_regression(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    LG5: A standard HOURS/HOURLY final line still appears correctly in
    GET /final-lines after LG-1 changes (regression guard).
    """
    pid        = None
    rate_id    = None
    driver_id  = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        r = await session_client.get("/payroll/rate-types", headers=_tok(auth_token))
        assert r.status_code == 200
        hourly_rt_id = next(
            rt["rate_type_id"] for rt in r.json() if rt["rate_code"] == "HOURLY"
        )

        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "LG5A")
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, hourly_rt_id, amount="22.00")

        pid = await _open_period(direct_db, paytest_branch_id)
        await _advance_to_approved(session_client, auth_token, pid, driver_id,
                                   line_type="HOURS", quantity="10")
        await _finalize(session_client, auth_token, pid)

        r = await session_client.get(FINAL_LINES.format(pid=pid), headers=_tok(auth_token))
        assert r.status_code == 200
        lines = r.json()

        hours_lines = [line for line in lines if line["line_type"] == "HOURS"]
        assert len(hours_lines) >= 1, "HOURS final line missing"
        assert Decimal(str(hours_lines[0]["final_amount"])) == Decimal("220.00")  # 10 * 22
        assert hours_lines[0]["driver_rate_id"] == rate_id
        assert hours_lines[0]["rate_type_id"] == hourly_rt_id

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if rate_id:
            pass


# ---------------------------------------------------------------------------
# LG6: PeriodSummary.final_gross includes CDPI final amount
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lg6_period_summary_final_gross_includes_cdpi(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    LG6: After finalization, the PeriodSummary returned for a Locked period
    has final_gross that includes the CDPI PerUnit finalized amount.
    """
    cid, admin_id = await _get_ids(direct_db)
    pay_item_id = None
    driver_id   = None
    pid         = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        result = await cdpi_service.create_direct_company_item(
            cid, admin_id,
            CdpiDirectCreateRequest(item_name="LG6 Gross Check",
                                    input_type="Number", calc_method_key="PerUnit"),
            direct_db,
        )
        pay_item_id = result.pay_item_id

        rate_type_id = (await direct_db.execute(_text(
            "SELECT ratetypeid FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
            {"pid": pay_item_id})).scalar_one()

        await _activate_branch(
            direct_db, pay_item_id=pay_item_id,
            company_id=cid, branch_id=paytest_branch_id,
        )

        driver_id = await _create_driver(session_client, auth_token, paytest_branch_id, "LG6A")
        await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id, amount="25.00")

        pid = await _open_period(direct_db, paytest_branch_id)
        await _advance_to_approved(session_client, auth_token, pid, driver_id,
                                   line_type=result.pay_item_code, quantity="6")
        period_summary = await _finalize(session_client, auth_token, pid)

        # finalize returns a PeriodSummary with final_gross already populated
        assert Decimal(str(period_summary["final_gross"])) == Decimal("150.00"), (
            f"Expected final_gross=150.00 (6 * 25), got {period_summary['final_gross']}"
        )
        assert period_summary["final_driver_count"] >= 1

        # Also verify via GET /payroll/periods (Locked filter)
        r = await session_client.get(
            "/payroll/periods",
            params={"branch_id": paytest_branch_id, "status": "Locked"},
            headers=_tok(auth_token),
        )
        assert r.status_code == 200
        locked = [p for p in r.json() if p["payroll_period_id"] == pid]
        assert len(locked) == 1, "Period not in Locked list"
        assert Decimal(str(locked[0]["final_gross"])) == Decimal("150.00"), (
            f"GET /periods Locked final_gross mismatch: {locked[0]['final_gross']}"
        )

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if pay_item_id:
            await _cleanup_cdpi_item(direct_db, pay_item_id=pay_item_id)
