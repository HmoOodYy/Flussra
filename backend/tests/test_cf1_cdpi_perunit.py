"""
CF-1: CDPI PerUnit Calculation / Finalization Backend Integration Tests.

Verifies that the existing generic calculation and finalization paths correctly
handle company-owned Daily PerUnit pay items (CDPI) created via PR-1B.

Tests
-----
CF1  CDPI Number PerUnit: draft line calculatedamount = qty * driver_rate
CF2  CDPI Time PerUnit: draft line calculatedamount = hours * driver_rate
CF3  Finalization includes CDPI PerUnit final line with correct amounts and SourceSnapshot
CF4  Missing driver rate produces needsmanagerreview=True (no exception)
CF5  Branch-inactive CDPI item is rejected when adding a draft line (422)
CF6  Standard HOURS calculation is unaffected (regression)

Year slots: all periods use 2090 dates (unused by other test files).
All CDPI items are seeded directly via SQL and cleaned up in finally blocks.
"""
import pytest
import pytest_asyncio
import httpx
from decimal import Decimal
import json
from datetime import date as _date
from sqlalchemy import text as _text

# ---------------------------------------------------------------------------
# Year slot constants
# ---------------------------------------------------------------------------
CF1_START, CF1_END, CF1_WORK = "2090-03-03", "2090-03-16", "2090-03-05"
CF2_START, CF2_END, CF2_WORK = "2090-04-07", "2090-04-20", "2090-04-09"
CF3_START, CF3_END, CF3_WORK = "2090-05-05", "2090-05-18", "2090-05-07"
CF4_START, CF4_END, CF4_WORK = "2090-06-02", "2090-06-15", "2090-06-04"
CF5_START, CF5_END, CF5_WORK = "2090-07-07", "2090-07-20", "2090-07-09"
CF6_START, CF6_END, CF6_WORK = "2090-08-04", "2090-08-17", "2090-08-06"

FINALIZE_URL = "/payroll/periods/{pid}/finalize"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _tok(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_periods(client, token, branch_id):
    headers = _tok(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
        r = await client.get("/payroll/periods",
                             params={"branch_id": branch_id, "status": s},
                             headers=headers)
        if r.status_code != 200:
            continue
        for p in r.json():
            await client.patch(f"/payroll/periods/{p['payroll_period_id']}/status",
                               json={"status": "Cancelled"}, headers=headers)


async def _create_driver(client, token, branch_id, suffix, hire_date="2090-01-01"):
    r = await client.post("/core/drivers", json={
        "branch_id": branch_id,
        "full_name": f"CF1 {suffix}",
        "driver_code": f"CF1-{suffix[:8]}",
        "cdl_number": f"CDL-CF1-{suffix[:6]}",
        "email": f"cf1{suffix[:6].lower().replace('-', '')}@example.com",
        "hire_date": hire_date,
    }, headers=_tok(token))
    assert r.status_code == 201, f"create_driver: {r.text}"
    return r.json()["driver_id"]


async def _delete_driver(client, token, driver_id):
    await client.delete(f"/core/drivers/{driver_id}", headers=_tok(token))


async def _open_period(client, token, branch_id, start, end):
    headers = _tok(token)
    r = await client.post("/payroll/periods", json={
        "branch_id": branch_id, "period_type": "Week",
        "start_date": start, "end_date": end,
    }, headers=headers)
    assert r.status_code == 201, f"create_period: {r.text}"
    pid = r.json()["payroll_period_id"]
    r = await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=headers)
    assert r.status_code == 200
    return pid


async def _advance_to_approved(client, token, pid, driver_id, work_date,
                                line_type, quantity):
    headers = _tok(token)
    r = await client.post(f"/payroll/periods/{pid}/lines", json={
        "driver_id": driver_id, "work_date": work_date,
        "line_type": line_type, "quantity": quantity,
    }, headers=headers)
    assert r.status_code == 201, f"add_line: {r.text}"
    line_id = r.json()["draft_line_id"]

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
    return line_id


async def _finalize(client, token, pid):
    r = await client.post(FINALIZE_URL.format(pid=pid), headers=_tok(token))
    assert r.status_code == 200, f"finalize: {r.text}"
    return r.json()


async def _force_cleanup_period(direct_db, pid):
    """Disable immutability triggers, delete all period rows, re-enable triggers."""
    triggers = [
        "ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable",
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert",
        "ALTER TABLE payroll.driverrates DISABLE TRIGGER trg_guard_driverrate_used_mutation",
        "ALTER TABLE payroll.driverratetiers DISABLE TRIGGER trg_guard_driverratetier_used_mutation",
    ]
    for sql in triggers:
        await direct_db.execute(_text(sql))
    try:
        await direct_db.execute(
            _text("DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
    finally:
        enables = [t.replace("DISABLE", "ENABLE") for t in triggers]
        for sql in enables:
            await direct_db.execute(_text(sql))


# ---------------------------------------------------------------------------
# CDPI seed helpers (inline — no shared test helpers)
# ---------------------------------------------------------------------------

async def _seed_cdpi_perunit(
    db,
    *,
    company_id: int,
    code: str,
    name: str,
    datatype: str = "Decimal",
) -> tuple[int, int]:
    """
    Insert a minimal CDPI PerUnit pay item + its company-scoped RateType +
    PayItemRateTypeMap row (mirrors what PR-1B approval creates).

    Returns (pay_item_id, rate_type_id).
    """
    # 1. PayItem
    pi_result = await db.execute(
        _text("""
            INSERT INTO payroll.payitems
                (companyid, payitemcode, payitemname, datatype, ratebehavior,
                 unit, category, itemscope, issystemstandard, requiresrate,
                 isdefaultbranchactive, status)
            VALUES (:cid, :code, :name, :dtype, 'PerUnit',
                    'Unit', 'Custom', 'Daily', FALSE, FALSE,
                    FALSE, 'Active')
            ON CONFLICT (payitemcode, companyid) WHERE companyid IS NOT NULL
                DO UPDATE SET status = 'Active', datatype = EXCLUDED.datatype
            RETURNING payitemid
        """),
        {"cid": company_id, "code": code, "name": name, "dtype": datatype},
    )
    pay_item_id: int = pi_result.scalar_one()

    rate_code = f"CDPI_{pay_item_id}_PER_UNIT"

    # 2. Company-scoped RateType (mirrors PR-1B)
    rt_result = await db.execute(
        _text("""
            INSERT INTO payroll.ratetypes
                (ratecode, ratename, unitname, isactive, companyid)
            VALUES (:code, :name, 'unit', TRUE, :cid)
            ON CONFLICT DO NOTHING
            RETURNING ratetypeid
        """),
        {"code": rate_code, "name": f"CDPI {name} Per Unit", "cid": company_id},
    )
    rate_type_id = rt_result.scalar_one_or_none()
    if rate_type_id is None:
        rate_type_id = (await db.execute(
            _text("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = :code"),
            {"code": rate_code},
        )).scalar_one()

    # 3. PayItemRateTypeMap
    await db.execute(
        _text("""
            INSERT INTO payroll.payitemratetypemap
                (payitemid, ratetypeid, isprimary, status)
            VALUES (:pid, :rtid, TRUE, 'Active')
            ON CONFLICT (payitemid, ratetypeid) DO NOTHING
        """),
        {"pid": pay_item_id, "rtid": rate_type_id},
    )

    return pay_item_id, rate_type_id


async def _activate_branch(db, *, pay_item_id: int, company_id: int, branch_id: int):
    await db.execute(
        _text("""
            DELETE FROM payroll.branchpayitemconfig
            WHERE payitemid = :pid AND companyid = :cid AND branchid = :bid
        """),
        {"pid": pay_item_id, "cid": company_id, "bid": branch_id},
    )
    await db.execute(
        _text("""
            INSERT INTO payroll.branchpayitemconfig
                (companyid, branchid, payitemid, isactive, effectivefrom)
            VALUES (:cid, :bid, :pid, TRUE, '2000-01-01')
        """),
        {"cid": company_id, "bid": branch_id, "pid": pay_item_id},
    )


async def _cleanup_cdpi(db, *, pay_item_id: int):
    """Remove all CDPI-related rows in FK-safe order."""
    await db.execute(
        _text("DELETE FROM payroll.payitemrateslots WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    rt_id = (await db.execute(
        _text("SELECT ratetypeid FROM payroll.payitemratetypemap WHERE payitemid = :pid LIMIT 1"),
        {"pid": pay_item_id},
    )).scalar_one_or_none()
    await db.execute(
        _text("DELETE FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    if rt_id is not None:
        # Delete DriverRates before deleting the RateType (FK constraint)
        await db.execute(
            _text("DELETE FROM payroll.driverrates WHERE ratetypeid = :rtid"),
            {"rtid": rt_id},
        )
        await db.execute(
            _text("""
                DELETE FROM payroll.ratetypes
                WHERE ratetypeid = :rtid
                  AND ratecode = :code
            """),
            {"rtid": rt_id, "code": f"CDPI_{pay_item_id}_PER_UNIT"},
        )
    await db.execute(
        _text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payitems WHERE payitemid = :pid"),
        {"pid": pay_item_id},
    )


async def _create_and_approve_rate(client, token, driver_id, rate_type_id,
                                    effective_from, amount):
    headers = _tok(token)
    r = await client.post("/payroll/rates", json={
        "driver_id": driver_id, "rate_type_id": rate_type_id,
        "amount": amount, "effective_from": effective_from,
    }, headers=headers)
    assert r.status_code == 201, f"create rate: {r.text}"
    rid = r.json()["driver_rate_id"]
    r = await client.post(f"/payroll/rates/{rid}/approve", headers=headers)
    assert r.status_code == 200, f"approve rate: {r.text}"
    return rid


async def _get_company_id(db) -> int:
    return (await db.execute(
        _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO'")
    )).scalar_one()


# ---------------------------------------------------------------------------
# CF1: CDPI Number PerUnit — draft calculatedamount = qty * driver_rate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cf1_cdpi_number_perunit_draft_calculation(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    CF1: Adding a draft line for a CDPI Number PerUnit item computes
    calculatedamount = quantity * driver_rate_amount.
    """
    cid = await _get_company_id(direct_db)
    pay_item_id = None
    driver_id = None
    pid = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        pay_item_id, rate_type_id = await _seed_cdpi_perunit(
            direct_db, company_id=cid,
            code="CF1_NUM_PERUNIT", name="CF1 Number PerUnit", datatype="Decimal",
        )
        await _activate_branch(
            direct_db, pay_item_id=pay_item_id,
            company_id=cid, branch_id=paytest_branch_id,
        )

        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id, "CF1NUM",
        )
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id,
            effective_from=CF1_START, amount="7.50",
        )

        pid = await _open_period(
            session_client, auth_token, paytest_branch_id, CF1_START, CF1_END,
        )

        item_code = f"CF1_NUM_PERUNIT"
        r = await session_client.post(f"/payroll/periods/{pid}/lines", json={
            "driver_id": driver_id,
            "work_date": CF1_WORK,
            "line_type": item_code,
            "quantity": "4",
        }, headers=_tok(auth_token))
        assert r.status_code == 201, f"add_line: {r.text}"
        line = r.json()

        # calculatedamount = 4 * 7.50 = 30.00
        assert line["calculated_amount"] is not None, "calculated_amount should not be None"
        assert Decimal(str(line["calculated_amount"])) == Decimal("30.00"), (
            f"Expected 30.00, got {line['calculated_amount']}"
        )
        assert line.get("needs_manager_review") is False

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if pay_item_id:
            await _cleanup_cdpi(direct_db, pay_item_id=pay_item_id)


# ---------------------------------------------------------------------------
# CF2: CDPI Time PerUnit — draft calculatedamount = hours * driver_rate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cf2_cdpi_time_perunit_draft_calculation(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    CF2: A CDPI Time-datatype PerUnit item computes calculatedamount correctly.
    The quantity is a decimal hour value; calculatedamount = hours * driver_rate.
    """
    cid = await _get_company_id(direct_db)
    pay_item_id = None
    driver_id = None
    pid = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        pay_item_id, rate_type_id = await _seed_cdpi_perunit(
            direct_db, company_id=cid,
            code="CF1_TIME_PERUNIT", name="CF1 Time PerUnit", datatype="Time",
        )
        await _activate_branch(
            direct_db, pay_item_id=pay_item_id,
            company_id=cid, branch_id=paytest_branch_id,
        )

        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id, "CF1TME",
        )
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id,
            effective_from=CF2_START, amount="12.00",
        )

        pid = await _open_period(
            session_client, auth_token, paytest_branch_id, CF2_START, CF2_END,
        )

        r = await session_client.post(f"/payroll/periods/{pid}/lines", json={
            "driver_id": driver_id,
            "work_date": CF2_WORK,
            "line_type": "CF1_TIME_PERUNIT",
            "quantity": "2.5",
        }, headers=_tok(auth_token))
        assert r.status_code == 201, f"add_line: {r.text}"
        line = r.json()

        # calculatedamount = 2.5 * 12.00 = 30.00
        assert line["calculated_amount"] is not None
        assert Decimal(str(line["calculated_amount"])) == Decimal("30.00"), (
            f"Expected 30.00, got {line['calculated_amount']}"
        )
        assert line.get("needs_manager_review") is False

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if pay_item_id:
            await _cleanup_cdpi(direct_db, pay_item_id=pay_item_id)


# ---------------------------------------------------------------------------
# CF3: Finalization includes CDPI PerUnit final line with correct amounts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cf3_finalization_includes_cdpi_perunit_line(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    CF3: After finalization, PayrollFinalLines contains a row for the CDPI
    PerUnit item with the correct finalamount, driverrateid, ratetypeid,
    resolvedrateamount, and SourceSnapshot fields.
    """
    cid = await _get_company_id(direct_db)
    pay_item_id = None
    driver_id = None
    pid = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        pay_item_id, rate_type_id = await _seed_cdpi_perunit(
            direct_db, company_id=cid,
            code="CF1_FIN_PERUNIT", name="CF1 Fin PerUnit", datatype="Decimal",
        )
        await _activate_branch(
            direct_db, pay_item_id=pay_item_id,
            company_id=cid, branch_id=paytest_branch_id,
        )

        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id, "CF1FIN",
        )
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id,
            effective_from=CF3_START, amount="5.00",
        )

        pid = await _open_period(
            session_client, auth_token, paytest_branch_id, CF3_START, CF3_END,
        )
        await _advance_to_approved(
            session_client, auth_token, pid, driver_id, CF3_WORK,
            line_type="CF1_FIN_PERUNIT", quantity="3",
        )
        await _finalize(session_client, auth_token, pid)

        rows = (await direct_db.execute(
            _text("""
                SELECT fl.finalamount, fl.driverrateid, fl.ratetypeid,
                       fl.resolvedrateamount, fl.quantity, fl.sourcesnapshot
                FROM   payroll.payrollfinallines fl
                WHERE  fl.payrollperiodid = :pid
                  AND  fl.driverrateid    = :rid
            """),
            {"pid": pid, "rid": rate_id},
        )).mappings().all()

        assert len(rows) == 1, f"Expected 1 final line, got {len(rows)}"
        row = rows[0]

        # Scalar columns
        assert row["driverrateid"] == rate_id
        assert row["ratetypeid"] == rate_type_id
        assert Decimal(str(row["resolvedrateamount"])) == Decimal("5.00")
        assert Decimal(str(row["finalamount"])) == Decimal("15.00")  # 3 * 5.00

        # SourceSnapshot
        snap = row["sourcesnapshot"]
        if isinstance(snap, str):
            snap = json.loads(snap)
        assert snap is not None, "SourceSnapshot is NULL"
        assert snap.get("driver_rate_id") == rate_id
        assert snap.get("rate_type_id") == rate_type_id
        assert Decimal(str(snap.get("driver_rate_amount"))) == Decimal("5.00")
        assert snap.get("pay_item_id") == pay_item_id

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if pay_item_id:
            await _cleanup_cdpi(direct_db, pay_item_id=pay_item_id)


# ---------------------------------------------------------------------------
# CF4: Missing driver rate → needsmanagerreview=True (no exception)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cf4_missing_driver_rate_sets_needs_manager_review(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    CF4: When a driver has no approved DriverRate for a CDPI PerUnit item's
    RateType, add_draft_line succeeds but needs_manager_review=True and
    calculated_amount is None.
    """
    cid = await _get_company_id(direct_db)
    pay_item_id = None
    driver_id = None
    pid = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        pay_item_id, rate_type_id = await _seed_cdpi_perunit(
            direct_db, company_id=cid,
            code="CF1_NMR_PERUNIT", name="CF1 NMR PerUnit", datatype="Decimal",
        )
        await _activate_branch(
            direct_db, pay_item_id=pay_item_id,
            company_id=cid, branch_id=paytest_branch_id,
        )

        # No driver rate created for this item
        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id, "CF1NMR",
        )

        pid = await _open_period(
            session_client, auth_token, paytest_branch_id, CF4_START, CF4_END,
        )

        r = await session_client.post(f"/payroll/periods/{pid}/lines", json={
            "driver_id": driver_id,
            "work_date": CF4_WORK,
            "line_type": "CF1_NMR_PERUNIT",
            "quantity": "2",
        }, headers=_tok(auth_token))
        assert r.status_code == 201, f"add_line: {r.text}"
        line = r.json()

        assert line.get("needs_manager_review") is True, (
            f"Expected needs_manager_review=True, got {line.get('needs_manager_review')}"
        )
        assert line.get("calculated_amount") is None, (
            f"Expected calculated_amount=None, got {line.get('calculated_amount')}"
        )

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if pay_item_id:
            await _cleanup_cdpi(direct_db, pay_item_id=pay_item_id)


# ---------------------------------------------------------------------------
# CF5: Branch-inactive CDPI item rejected at draft line add (422)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cf5_branch_inactive_cdpi_rejected(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    CF5: Attempting to add a draft line for a CDPI item that has no active
    BranchPayItemConfig for the period's branch returns 422.
    """
    cid = await _get_company_id(direct_db)
    pay_item_id = None
    driver_id = None
    pid = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        pay_item_id, _ = await _seed_cdpi_perunit(
            direct_db, company_id=cid,
            code="CF1_INACTIVE_PU", name="CF1 Inactive PerUnit", datatype="Decimal",
        )
        # Deliberately NOT activating for the branch (isdefaultbranchactive=FALSE)

        driver_id = await _create_driver(
            session_client, auth_token, paytest_branch_id, "CF1INA",
        )

        pid = await _open_period(
            session_client, auth_token, paytest_branch_id, CF5_START, CF5_END,
        )

        r = await session_client.post(f"/payroll/periods/{pid}/lines", json={
            "driver_id": driver_id,
            "work_date": CF5_WORK,
            "line_type": "CF1_INACTIVE_PU",
            "quantity": "1",
        }, headers=_tok(auth_token))
        assert r.status_code == 422, (
            f"Expected 422 for inactive CDPI item, got {r.status_code}: {r.text}"
        )

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
        if pay_item_id:
            await _cleanup_cdpi(direct_db, pay_item_id=pay_item_id)


# ---------------------------------------------------------------------------
# CF6: Standard HOURS calculation is unaffected (regression)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cf6_standard_hours_calculation_unaffected(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_driver_id: int,
    direct_db,
):
    """
    CF6: CDPI changes do not affect the standard HOURS/HOURLY calculation path.
    A draft line for HOURS with an approved HOURLY rate produces the expected
    calculatedamount and finalizes correctly.
    """
    pid = None
    rate_id = None

    try:
        await _cancel_periods(session_client, auth_token, paytest_branch_id)

        # Get HOURLY rate type and create a rate for the standard test driver
        r = await session_client.get("/payroll/rate-types", headers=_tok(auth_token))
        assert r.status_code == 200
        hourly_rt_id = next(
            rt["rate_type_id"] for rt in r.json() if rt["rate_code"] == "HOURLY"
        )
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, paytest_driver_id, hourly_rt_id,
            effective_from=CF6_START, amount="20.00",
        )

        pid = await _open_period(
            session_client, auth_token, paytest_branch_id, CF6_START, CF6_END,
        )

        # Add draft line, verify calculation, then advance and finalize
        line_id = await _advance_to_approved(
            session_client, auth_token, pid, paytest_driver_id, CF6_WORK,
            line_type="HOURS", quantity="8",
        )

        # Verify draft calculation via direct DB
        wdate = _date(2090, 8, 6)
        draft_row = (await direct_db.execute(
            _text("""
                SELECT calculatedamount, needsmanagerreview
                FROM   payroll.payrolldraftlines
                WHERE  payrollperiodid = :pid
                  AND  driverid = :did
                  AND  workdate = :wdate
                  AND  linetype = 'HOURS'
            """),
            {"pid": pid, "did": paytest_driver_id, "wdate": wdate},
        )).mappings().first()
        assert draft_row is not None, "Draft line not found in DB"
        assert Decimal(str(draft_row["calculatedamount"])) == Decimal("160.00"), (
            f"Expected 160.00, got {draft_row['calculatedamount']}"
        )
        assert draft_row["needsmanagerreview"] is False

        await _finalize(session_client, auth_token, pid)

        rows = (await direct_db.execute(
            _text("""
                SELECT fl.finalamount, fl.driverrateid, fl.ratetypeid
                FROM   payroll.payrollfinallines fl
                WHERE  fl.payrollperiodid = :pid
                  AND  fl.driverrateid    = :rid
            """),
            {"pid": pid, "rid": rate_id},
        )).mappings().all()
        assert len(rows) >= 1, "No final lines for HOURLY rate"
        assert Decimal(str(rows[0]["finalamount"])) == Decimal("160.00")

    finally:
        if pid:
            await _force_cleanup_period(direct_db, pid)
        if rate_id:
            # Delete the HOURLY rate created for the shared paytest_driver_id so
            # subsequent tests that create an HOURLY rate for this driver are not
            # blocked by the "approved rate already exists" guard.
            await direct_db.execute(
                _text("DELETE FROM payroll.driverrates WHERE driverrateid = :rid"),
                {"rid": rate_id},
            )
