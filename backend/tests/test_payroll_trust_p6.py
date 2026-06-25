"""
Payroll Trust Phase 6 -- PayrollFinalLines INSERT guard.

Verifies that direct INSERT into payroll.payrollfinallines is blocked by the
DB trigger added in migration 0038 (trg_guard_final_line_insert), and that
real finalization via finalize_period still works.

Tests:
  A  Direct SQL INSERT is blocked (restrict_violation) without the GUC.
  B  Real finalization via the API inserts final lines successfully.
  C  Direct INSERT into a Locked/Archived period is also blocked.
  D  Phase 3C UPDATE/DELETE immutability is unaffected.
  E  Phase 5 DriverRate void guard is unaffected.

All tests use year slots 2058-2064 (distinct from P3C 2043-2051, P5 2052-2057).
Isolated drivers are created per test to avoid cross-test contamination.
"""
import pytest
import pytest_asyncio
import httpx
import sqlalchemy.exc
from datetime import date as _date
from sqlalchemy import text as _text


# ---------------------------------------------------------------------------
# Year slots
# ---------------------------------------------------------------------------
A_START, A_END, A_WORK = "2058-03-04", "2058-03-10", "2058-03-05"
B_START, B_END, B_WORK = "2059-04-07", "2059-04-13", "2059-04-08"
C_START, C_END, C_WORK = "2060-05-05", "2060-05-11", "2060-05-06"
D_START, D_END, D_WORK = "2061-06-02", "2061-06-08", "2061-06-03"
E_START, E_END, E_WORK = "2062-07-07", "2062-07-13", "2062-07-08"


# ---------------------------------------------------------------------------
# Helpers (adapted from test_payroll_trust_p5)
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    headers = _auth(token)
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


async def _create_driver(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    suffix: str,
    hire_date: str = "2058-01-01",
) -> int:
    r = await client.post(
        "/core/drivers",
        json={
            "branch_id":      branch_id,
            "full_name":      f"P6 Driver {suffix}",
            "preferred_name": f"P6-{suffix}",
            "driver_code":    f"P6DRV-{suffix}",
            "cdl_number":     f"CDL-P6-{suffix}",
            "email":          f"p6drv{suffix}@example.com",
            "hire_date":      hire_date,
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"driver create: {r.text}"
    return r.json()["driver_id"]


async def _delete_driver(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
) -> None:
    await client.delete(f"/core/drivers/{driver_id}", headers=_auth(token))


async def _create_and_approve_rate(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_type_id: int,
    amount: str = "15.00",
    effective_from: str = "2058-01-01",
) -> int:
    headers = _auth(token)
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
    assert rc.status_code == 201, f"create rate: {rc.text}"
    rate_id = rc.json()["driver_rate_id"]
    ra = await client.post(f"/payroll/rates/{rate_id}/approve", headers=headers)
    assert ra.status_code == 200, f"approve rate: {ra.text}"
    return rate_id


async def _make_period(
    db,
    branch_id: int,
    start: str,
    end: str,
) -> int:
    """Insert an Open period directly into DB.  Returns period_id."""
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": f"P6-{branch_id}-{start}",
         "name": f"P6 {start}", "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str,
) -> None:
    """Open -> InReview (add DailyNote line) -> Approved via review flow."""
    headers = _auth(token)
    dummy = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json={"driver_id": driver_id, "work_date": work_date,
              "line_type": "DailyNote", "notes": "filler"},
        headers=headers,
    )
    assert dummy.status_code == 201, f"DailyNote line: {dummy.text}"

    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=headers,
    )
    assert r.status_code == 200, f"InReview: {r.text}"

    review_resp = await client.get("/review/items", headers=headers)
    assert review_resp.status_code == 200
    item = next(
        (i for i in review_resp.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, "No pending review item found"

    dec = await client.post(
        f"/review/items/{item['review_item_id']}/decide",
        json={"decision": "Approved"},
        headers=headers,
    )
    assert dec.status_code == 200, f"review approve: {dec.text}"


async def _finalize(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
) -> dict:
    r = await client.post(
        f"/payroll/periods/{period_id}/finalize",
        headers=_auth(token),
    )
    assert r.status_code == 200, f"finalize: {r.text}"
    return r.json()


async def _lock_period_with_rate(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    rate_type_id: int,
    start: str,
    end: str,
    work_date: str,
    suffix: str,
    db,
) -> tuple[int, int, int]:
    """
    Full flow: create driver + HOURS rate -> create period -> add HOURS line
    -> advance to Approved -> finalize (Locked).
    Returns (period_id, driver_id, rate_id).
    """
    drv = await _create_driver(client, token, branch_id, suffix,
                               hire_date=start[:4] + "-01-01")
    rate_id = await _create_and_approve_rate(
        client, token, drv, rate_type_id, "15.00",
        effective_from=start[:4] + "-01-01",
    )
    pid = await _make_period(db, branch_id, start, end)
    r = await client.post(
        f"/payroll/periods/{pid}/lines",
        json={"driver_id": drv, "work_date": work_date,
              "line_type": "HOURS", "quantity": "8.0000"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"add HOURS line: {r.text}"
    await _advance_to_approved(client, token, pid, drv, work_date=work_date)
    await _finalize(client, token, pid)
    return pid, drv, rate_id


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def p6_env(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    await _cancel_periods(session_client, auth_token, paytest_branch_id)

    env = {
        "client":          session_client,
        "token":           auth_token,
        "branch_id":       paytest_branch_id,
        "hourly_rtid":     paytest_rate_type_id,
        "db":              direct_db,
        "created_drivers": [],
    }
    yield env

    await _cancel_periods(session_client, auth_token, paytest_branch_id)
    for did in env["created_drivers"]:
        await _delete_driver(session_client, auth_token, did)


# ---------------------------------------------------------------------------
# Test A — Direct SQL INSERT is blocked (restrict_violation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p6_a_direct_insert_blocked_without_guc(p6_env):
    """
    A: A direct INSERT into payroll.payrollfinallines without the
    app.allow_payroll_final_line_insert GUC must raise restrict_violation.

    This verifies migration 0038 (trg_guard_final_line_insert) is active.
    The GUC is NOT set on this direct_db connection, so the trigger fires.
    """
    db = p6_env["db"]
    bid = p6_env["branch_id"]

    # Fetch company_id for a valid branch
    cid_row = await db.execute(
        _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
        {"bid": bid},
    )
    cid = cid_row.scalar_one()

    # Use a non-existent period_id (-1) — the trigger fires before FK checks
    with pytest.raises(sqlalchemy.exc.IntegrityError) as exc_info:
        await db.execute(
            _text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid,
                     linetype, quantity, finalamount, sourcetype)
                VALUES
                    (:cid, :bid, -1, -1, 'HOURS', 1, 100, 'DirectTest')
            """),
            {"cid": cid, "bid": bid},
        )

    err_str = str(exc_info.value).lower()
    assert "payroll_insert_guard" in err_str, (
        f"Expected payroll_insert_guard in error; got: {exc_info.value}"
    )
    # asyncpg surfaces the ERRCODE as class name "RestrictViolationError"
    assert "restrictviolation" in err_str or "restrict_violation" in err_str, (
        f"Expected RestrictViolationError/restrict_violation; got: {exc_info.value}"
    )

    # Confirm no row was inserted
    row = await db.execute(
        _text("SELECT COUNT(*) FROM payroll.payrollfinallines "
              "WHERE payrollperiodid = -1"),
    )
    assert row.scalar_one() == 0, "No final line should have been inserted"


# ---------------------------------------------------------------------------
# Test B — Real finalization still inserts final lines successfully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p6_b_real_finalization_inserts_final_lines(p6_env):
    """
    B: The controlled finalization path (POST /payroll/periods/{id}/finalize)
    must still insert PayrollFinalLines successfully even with the Phase 6
    INSERT guard active.

    finalize_period sets the transaction-local GUC before Step 3, so the
    trigger allows the insert.
    """
    c, tok = p6_env["client"], p6_env["token"]
    bid, rtid = p6_env["branch_id"], p6_env["hourly_rtid"]
    db = p6_env["db"]

    pid, drv, _rate_id = await _lock_period_with_rate(
        c, tok, bid, rtid, B_START, B_END, B_WORK, "B1", db,
    )
    p6_env["created_drivers"].append(drv)

    # Confirm final lines were written
    rows = await db.execute(
        _text("""
            SELECT COUNT(*) FROM payroll.payrollfinallines
            WHERE payrollperiodid = :pid AND companyid = (
                SELECT companyid FROM core.branches WHERE branchid = :bid
            )
        """),
        {"pid": pid, "bid": bid},
    )
    cnt = rows.scalar_one()
    assert cnt >= 1, (
        f"Expected at least 1 PayrollFinalLine after finalization; got {cnt}"
    )

    # Period must be Locked
    status_row = await db.execute(
        _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
        {"pid": pid},
    )
    assert status_row.scalar_one() == "Locked", "Period must be Locked after finalization"


# ---------------------------------------------------------------------------
# Test C — Direct INSERT into Locked/Archived period is blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p6_c_direct_insert_into_locked_period_blocked(p6_env):
    """
    C: Even for a legitimately Locked period, a direct INSERT (without the
    GUC) is blocked by the Phase 6 trigger.

    After finalization the period is Locked and has real final lines.
    A direct INSERT attempt must raise restrict_violation; the period_status
    in the error message must read 'Locked'.
    """
    c, tok = p6_env["client"], p6_env["token"]
    bid, rtid = p6_env["branch_id"], p6_env["hourly_rtid"]
    db = p6_env["db"]

    pid, drv, _rate_id = await _lock_period_with_rate(
        c, tok, bid, rtid, C_START, C_END, C_WORK, "C1", db,
    )
    p6_env["created_drivers"].append(drv)

    # Confirm the period is Locked
    status_row = await db.execute(
        _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
        {"pid": pid},
    )
    assert status_row.scalar_one() == "Locked"

    cid_row = await db.execute(
        _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
        {"bid": bid},
    )
    cid = cid_row.scalar_one()

    # Record existing final-line count
    count_before = (await db.execute(
        _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
        {"pid": pid},
    )).scalar_one()

    # Direct INSERT into the Locked period — must be blocked
    with pytest.raises(sqlalchemy.exc.IntegrityError) as exc_info:
        await db.execute(
            _text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid,
                     linetype, quantity, finalamount, sourcetype)
                VALUES
                    (:cid, :bid, :pid, :did, 'HOURS', 1, 999.99, 'FakeInsert')
            """),
            {"cid": cid, "bid": bid, "pid": pid, "did": drv},
        )

    err_str = str(exc_info.value).lower()
    assert "payroll_insert_guard" in err_str, (
        f"Expected payroll_insert_guard in error; got: {exc_info.value}"
    )
    assert "restrictviolation" in err_str or "restrict_violation" in err_str, (
        f"Expected RestrictViolationError; got: {exc_info.value}"
    )
    # The error message should mention the Locked status
    assert "locked" in err_str, (
        f"Expected 'Locked' in error message for a Locked period; got: {exc_info.value}"
    )

    # No extra line must have been inserted
    count_after = (await db.execute(
        _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
        {"pid": pid},
    )).scalar_one()
    assert count_after == count_before, (
        f"No new lines should have been inserted; before={count_before}, after={count_after}"
    )


# ---------------------------------------------------------------------------
# Test D — Phase 3C UPDATE/DELETE immutability is unaffected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p6_d_phase3c_update_delete_still_blocked(p6_env):
    """
    D: Phase 3C's trg_final_line_immutable (BEFORE UPDATE OR DELETE) must
    still block modification/deletion of final lines for Locked periods.
    Phase 6 INSERT guard must not interfere with the Phase 3C guard.
    """
    c, tok = p6_env["client"], p6_env["token"]
    bid, rtid = p6_env["branch_id"], p6_env["hourly_rtid"]
    db = p6_env["db"]

    pid, drv, _rate_id = await _lock_period_with_rate(
        c, tok, bid, rtid, D_START, D_END, D_WORK, "D1", db,
    )
    p6_env["created_drivers"].append(drv)

    # Confirm at least one final line exists
    fl_row = (await db.execute(
        _text("""
            SELECT finallineid FROM payroll.payrollfinallines
            WHERE payrollperiodid = :pid LIMIT 1
        """),
        {"pid": pid},
    )).mappings().first()
    assert fl_row is not None, "No final lines found after finalization"
    fl_id = fl_row["finallineid"]

    # Attempt UPDATE — must be blocked by Phase 3C trigger
    with pytest.raises(sqlalchemy.exc.IntegrityError) as upd_exc:
        await db.execute(
            _text("""
                UPDATE payroll.payrollfinallines
                SET    finalamount = 0
                WHERE  finallineid = :fid
            """),
            {"fid": fl_id},
        )
    assert "payroll_ledger_immutable" in str(upd_exc.value).lower(), (
        f"Expected payroll_ledger_immutable in UPDATE error; got: {upd_exc.value}"
    )

    # Attempt DELETE — must be blocked by Phase 3C trigger
    with pytest.raises(sqlalchemy.exc.IntegrityError) as del_exc:
        await db.execute(
            _text("DELETE FROM payroll.payrollfinallines WHERE finallineid = :fid"),
            {"fid": fl_id},
        )
    assert "payroll_ledger_immutable" in str(del_exc.value).lower(), (
        f"Expected payroll_ledger_immutable in DELETE error; got: {del_exc.value}"
    )


# ---------------------------------------------------------------------------
# Test E — Phase 5 DriverRate void guard is unaffected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p6_e_phase5_driverrate_void_guard_still_active(p6_env):
    """
    E: Phase 5's trg_guard_driverrate_void (BEFORE UPDATE OF status) must
    still block voiding a DriverRate referenced in PayrollFinalLines.
    Phase 6 INSERT guard must not interfere with the Phase 5 guard.
    """
    c, tok = p6_env["client"], p6_env["token"]
    bid, rtid = p6_env["branch_id"], p6_env["hourly_rtid"]
    db = p6_env["db"]

    pid, drv, rate_id = await _lock_period_with_rate(
        c, tok, bid, rtid, E_START, E_END, E_WORK, "E1", db,
    )
    p6_env["created_drivers"].append(drv)

    # Confirm final line references the DriverRate
    ref_row = (await db.execute(_text("""
        SELECT driverrateid FROM payroll.payrollfinallines
        WHERE payrollperiodid = :pid AND driverrateid = :rid LIMIT 1
    """), {"pid": pid, "rid": rate_id})).mappings().first()
    assert ref_row is not None, "PayrollFinalLines must reference the DriverRateID"

    # API void must be rejected (Phase 5 service guard)
    resp = await c.delete(f"/payroll/rates/{rate_id}", headers=_auth(tok))
    assert resp.status_code == 422, (
        f"Expected 422 from Phase 5 guard; got {resp.status_code}: {resp.text}"
    )

    # Direct DB void must be rejected by Phase 5 trigger
    with pytest.raises(sqlalchemy.exc.IntegrityError) as exc_info:
        await db.execute(
            _text("UPDATE payroll.driverrates SET status = 'Voided' "
                  "WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
    assert "driverrate_void_guard" in str(exc_info.value).lower(), (
        f"Expected driverrate_void_guard; got: {exc_info.value}"
    )
