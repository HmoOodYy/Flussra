"""Payroll Trust Phase 7 — source integrity and snapshot finalization trust."""
import pytest
import pytest_asyncio
from uuid import uuid4
import httpx
from decimal import Decimal
from datetime import date as _date
from sqlalchemy import text as _text, text as _sqla_text
from sqlalchemy.exc import IntegrityError


# ---------------------------------------------------------------------------
# Year slots
# ---------------------------------------------------------------------------
T1_START, T1_END, T1_WORK = "2066-03-04", "2066-03-10", "2066-03-05"
T2_START, T2_END, T2_WORK = "2067-04-07", "2067-04-13", "2067-04-08"
T4_START, T4_END, T4_WORK = "2068-05-05", "2068-05-11", "2068-05-06"
T5_START, T5_END, T5_WORK = "2069-06-02", "2069-06-08", "2069-06-03"

PREVIEW_URL = "/payroll/periods/{pid}/finalization-preview"
FINALIZE_URL = "/payroll/periods/{pid}/finalize"


# ---------------------------------------------------------------------------
# Helpers (adapted from test_payroll_trust_p5/p6)
# ---------------------------------------------------------------------------

def _tok(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def trust_branch_id(direct_db, session_client, auth_token):
    """Keep each trust flow and its retained history on a fresh branch."""
    code = f"P7_{uuid4().hex[:12]}"
    branch_id = (await direct_db.execute(_text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {"code": code, "name": code})).scalar_one()
    items = await session_client.get(
        f"/settings/branches/{branch_id}/pay-items", headers=_tok(auth_token),
    )
    assert items.status_code == 200, items.text
    hours_id = next(item["pay_item_id"] for item in items.json()
                    if item["pay_item_code"] == "HOURS")
    active = await session_client.patch(
        f"/settings/branches/{branch_id}/pay-items/{hours_id}",
        json={"is_active": True}, headers=_tok(auth_token),
    )
    assert active.status_code == 200, active.text
    return branch_id


async def _create_driver(client, token, branch_id, suffix, hire_date="2066-01-01"):
    r = await client.post(
        "/core/drivers",
        json={"branch_id": branch_id, "full_name": f"P7 {suffix}",
              "driver_code": f"P7-{suffix[:8]}",
              "cdl_number": f"CDL-P7-{suffix[:6]}",
              "email": f"p7{suffix[:6]}@example.com",
              "hire_date": hire_date},
        headers=_tok(token),
    )
    assert r.status_code == 201, f"create_driver: {r.text}"
    return r.json()["driver_id"]


async def _delete_driver(client, token, driver_id):
    await client.delete(f"/core/drivers/{driver_id}", headers=_tok(token))


async def _create_and_approve_rate(client, token, driver_id, rate_type_id,
                                   amount="15.00", effective_from="2066-01-01"):
    headers = _tok(token)
    rc = await client.post("/payroll/rates",
                           json={"driver_id": driver_id, "rate_type_id": rate_type_id,
                                 "amount": amount, "effective_from": effective_from},
                           headers=headers)
    assert rc.status_code == 201, f"create rate: {rc.text}"
    rid = rc.json()["driver_rate_id"]
    ra = await client.post(f"/payroll/rates/{rid}/approve", headers=headers)
    assert ra.status_code == 200, f"approve rate: {ra.text}"
    return rid


async def _open_period(db, branch_id, start, end):
    """Insert an Open period directly into DB.  Returns period_id."""
    row = (await db.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": f"P7-{branch_id}-{start}",
         "name": f"P7 {start}", "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _advance_to_approved(client, token, pid, driver_id, work_date):
    """Open -> InReview (dummy DailyNote) -> Approved via review flow."""
    headers = _tok(token)
    await client.post(f"/payroll/periods/{pid}/lines",
                      json={"driver_id": driver_id, "work_date": work_date,
                            "line_type": "DailyNote", "notes": "filler"},
                      headers=headers)
    r = await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "InReview"}, headers=headers)
    assert r.status_code == 200, f"InReview: {r.text}"
    items = await client.get("/review/items", headers=headers)
    item = next((i for i in items.json()
                 if i.get("entity_name") == "PayrollPeriods"
                 and i.get("entity_id") == str(pid)
                 and i.get("status") == "Pending"), None)
    assert item is not None, "review item not found"
    dec = await client.post(f"/review/items/{item['review_item_id']}/decide",
                            json={"decision": "Approved"}, headers=headers)
    assert dec.status_code == 200, f"approve review: {dec.text}"


# ---------------------------------------------------------------------------
# T1 — Source uniqueness is enforced; approved snapshot remains authoritative
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p7_t1_duplicate_daily_source_is_rejected_and_snapshot_finalizes(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    """
    T1: The active-Daily uniqueness invariant rejects duplicate source data.
    Once the valid packet is submitted and approved, its snapshot remains the
    finalization authority without bypassing that database invariant.
    """
    headers = _tok(auth_token)
    drv = await _create_driver(session_client, auth_token, trust_branch_id,
                               "T1Dup", hire_date="2066-01-01")
    try:
        await _create_and_approve_rate(session_client, auth_token, drv,
                                       paytest_rate_type_id,
                                       effective_from="2066-01-01")
        pid = await _open_period(direct_db, trust_branch_id,
                                 T1_START, T1_END)
        await _advance_to_approved(session_client, auth_token, pid, drv, T1_WORK)

        async with direct_db.engine.connect() as transactional_db:
            async with transactional_db.begin():
                with pytest.raises(IntegrityError):
                    await transactional_db.execute(
                    _text("""
                        INSERT INTO payroll.payrolldraftlines
                            (companyid, branchid, payrollperiodid, driverid,
                             workdate, linetype, linescope, quantity,
                             sourcetype, status, needsmanagerreview, addedbyuserid)
                        SELECT companyid, :bid, :pid, :did,
                               :wdate, 'DailyNote', 'Daily', 1,
                               'Manual', 'Active', FALSE, 1
                        FROM payroll.payrollperiods
                        WHERE payrollperiodid = :pid
                    """),
                    {"bid": trust_branch_id, "pid": pid, "did": drv,
                     "wdate": _date.fromisoformat(T1_WORK)},
                )

        prev = await session_client.get(PREVIEW_URL.format(pid=pid), headers=headers)
        assert prev.status_code == 200, f"preview: {prev.text}"
        assert prev.json()["can_finalize"] is True

        fin = await session_client.post(FINALIZE_URL.format(pid=pid), headers=headers)
        assert fin.status_code == 200, f"finalize: {fin.text}"
        assert fin.json()["status"] == "Locked"

    finally:
        await _delete_driver(session_client, auth_token, drv)


# ---------------------------------------------------------------------------
# T2 — Live eligibility drift does not redefine an approved snapshot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p7_t2_live_eligibility_drift_does_not_block_snapshot_finalization(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    """
    T2: Eligibility is validated before Submit.  A termination recorded after
    approval does not alter the immutable packet already approved for finalization.
    """
    headers = _tok(auth_token)
    drv = await _create_driver(session_client, auth_token, trust_branch_id,
                               "T2Inelig", hire_date="2067-01-01")
    emp_id_row = await direct_db.execute(
        _text("SELECT employeeid FROM core.drivers WHERE driverid = :did"),
        {"did": drv},
    )
    emp_id = emp_id_row.scalar_one()

    try:
        await _create_and_approve_rate(session_client, auth_token, drv,
                                       paytest_rate_type_id,
                                       effective_from="2067-01-01")
        pid = await _open_period(direct_db, trust_branch_id,
                                 T2_START, T2_END)

        # Add a HOURS line on T2_WORK (will become ineligible after we terminate)
        r = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": drv, "work_date": T2_WORK,
                  "line_type": "HOURS", "quantity": "8"},
            headers=headers,
        )
        assert r.status_code == 201, f"add HOURS line: {r.text}"

        await _advance_to_approved(session_client, auth_token, pid, drv, T2_WORK)

        # Terminate the driver BEFORE T2_WORK so the line is ineligible
        term_date = str(_date.fromisoformat(T2_WORK) - __import__("datetime").timedelta(days=1))
        await direct_db.execute(
            _text("""
                UPDATE core.employees
                SET    terminationdate = :tdate
                WHERE  employeeid = :eid
            """),
            {"tdate": _date.fromisoformat(term_date), "eid": emp_id},
        )

        prev = await session_client.get(
            PREVIEW_URL.format(pid=pid), headers=headers,
        )
        assert prev.status_code == 200, f"preview: {prev.text}"
        data = prev.json()
        assert data["can_finalize"] is True, f"snapshot preview blockers={data['blockers']}"

        fin = await session_client.post(FINALIZE_URL.format(pid=pid), headers=headers)
        assert fin.status_code == 200, f"finalize: {fin.text}"
        assert fin.json()["status"] == "Locked"

    finally:
        # Restore terminationdate so driver cleanup works
        await direct_db.execute(
            _text("UPDATE core.employees SET terminationdate = NULL WHERE employeeid = :eid"),
            {"eid": emp_id},
        )
        await _delete_driver(session_client, auth_token, drv)


# ---------------------------------------------------------------------------
# T3 — Active tenant mappings remain structurally safe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p7_t3_active_company_mappings_do_not_cross_tenants(
    direct_db,
):
    """Company-owned PayItems are never mapped to another company's RateType."""
    rows = (await direct_db.execute(_text("""
        SELECT m.payitemratetypemapid
        FROM payroll.payitemratetypemap AS m
        JOIN payroll.payitems AS p ON p.payitemid = m.payitemid
        JOIN payroll.ratetypes AS r ON r.ratetypeid = m.ratetypeid
        WHERE p.companyid IS NOT NULL
          AND r.companyid IS NOT NULL
          AND p.companyid <> r.companyid
    """))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# T4 — Unresolved financial input blocks Submit before snapshot capture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p7_t4_unresolved_nmr_line_blocks_submit_before_snapshot_capture(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    """
    T4: A driver with no approved HOURLY rate produces an unresolved line and
    therefore cannot Submit a financial packet for review or finalization.
    """
    headers = _tok(auth_token)
    # Create driver WITHOUT any approved rate so the HOURS line is unresolved.
    drv = await _create_driver(session_client, auth_token, trust_branch_id,
                               "T4NMR", hire_date="2068-01-01")
    try:
        pid = await _open_period(direct_db, trust_branch_id,
                                 T4_START, T4_END)

        # Add HOURS line — NMR=True because no rate exists for this driver
        r = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": drv, "work_date": T4_WORK,
                  "line_type": "HOURS", "quantity": "8"},
            headers=headers,
        )
        assert r.status_code == 201, f"add HOURS: {r.text}"

        submit = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=headers,
        )
        assert submit.status_code == 422, f"submit: {submit.text}"
        detail = submit.json().get("detail", "").lower()
        assert any(keyword in detail for keyword in ("manager review", "unresolved", "rate"))
        snapshot_count = (await direct_db.execute(_text("""
            SELECT COUNT(*)
            FROM payroll.payrollcalculationsnapshots
            WHERE payrollperiodid = :pid
        """), {"pid": pid})).scalar_one()
        assert snapshot_count == 0

    finally:
        await _delete_driver(session_client, auth_token, drv)


# ---------------------------------------------------------------------------
# T5 — Valid period: preview can_finalize=True, finalize succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p7_t5_valid_period_preview_and_finalize(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    """
    T5: A clean period with an approved rate, no blockers, and eligible driver
    must have preview can_finalize=True and finalize must succeed (200 Locked).
    """
    headers = _tok(auth_token)
    drv = await _create_driver(session_client, auth_token, trust_branch_id,
                               "T5Valid", hire_date="2069-01-01")
    try:
        await _create_and_approve_rate(session_client, auth_token, drv,
                                       paytest_rate_type_id,
                                       effective_from="2069-01-01")
        pid = await _open_period(direct_db, trust_branch_id,
                                 T5_START, T5_END)

        r = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": drv, "work_date": T5_WORK,
                  "line_type": "HOURS", "quantity": "8"},
            headers=headers,
        )
        assert r.status_code == 201, f"add HOURS line: {r.text}"

        await _advance_to_approved(session_client, auth_token, pid, drv, T5_WORK)

        # ── Preview must report can_finalize=True, no blockers ──
        prev = await session_client.get(
            PREVIEW_URL.format(pid=pid), headers=headers,
        )
        assert prev.status_code == 200, f"preview: {prev.text}"
        data = prev.json()
        assert data["can_finalize"] is True, (
            f"Preview must report can_finalize=True; blockers={data['blockers']}"
        )
        assert data["blockers"] == [], (
            f"No blockers expected; got: {data['blockers']}"
        )

        # ── Finalize must succeed ──
        fin = await session_client.post(FINALIZE_URL.format(pid=pid), headers=headers)
        assert fin.status_code == 200, f"finalize must succeed; got {fin.status_code}: {fin.text}"
        assert fin.json()["status"] == "Locked", "Period must be Locked after finalization"

        # ── Final lines must exist ──
        cid_row = await direct_db.execute(
            _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": trust_branch_id},
        )
        cid = cid_row.scalar_one()
        cnt = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines "
                  "WHERE payrollperiodid = :pid AND companyid = :cid"),
            {"pid": pid, "cid": cid},
        )).scalar_one()
        assert cnt >= 1, f"Expected ≥1 final lines after finalization; got {cnt}"

    finally:
        await _delete_driver(session_client, auth_token, drv)
