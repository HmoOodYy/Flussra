"""
Payroll Trust Phase 9 — Final Source Snapshot + Used Source Row Immutability.

Verifies that:
  T1  Finalization writes a non-null SourceSnapshot on regular final lines
  T2  Ledger API exposes source_snapshot (non-null, contains expected fields)
  T3  Direct SQL UPDATE of a used DriverRate's Amount is blocked
  T4  Direct SQL DELETE of a used DriverRate is blocked
  T5  Unused DriverRate can still be updated (Amount) and voided
  T6  Future rate approval still works after historical finalized payroll
  T7  SYS_MIN_TOPUP line has a system_generated SourceSnapshot

Year slots: 2091-2110 (distinct from prior phases).
"""
import json as _json
import pytest
import pytest_asyncio
import httpx
from datetime import date as _date
from decimal import Decimal
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import create_async_engine
import asyncpg
from uuid import uuid4
from app.payroll_setup.policy import assign_setup, create_draft, create_setup, publish_version


@pytest_asyncio.fixture
async def trust_branch_id(db_conn, session_client, auth_token) -> int:
    """Give each test a branch whose finalized evidence cannot affect another test."""
    row = (await db_conn.execute(_text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {"code": (code := f"P9_{uuid4().hex}"), "name": code})).scalar_one()
    branch_id = int(row)
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

# ---------------------------------------------------------------------------
# Year slots
# ---------------------------------------------------------------------------
T1_START, T1_END, T1_WORK = "2091-01-06", "2091-01-12", "2091-01-07"
T5_START, T5_END, T5_WORK = "2092-02-03", "2092-02-09", "2092-02-04"
T6A_START, T6A_END = "2093-03-03", "2093-03-09"   # historical period (finalized)
T6B_START, T6B_END = "2094-04-07", "2094-04-13"   # future period using new rate
T7_START, T7_END, T7_WORK = "2095-05-05", "2095-05-11", "2095-05-06"

PREVIEW_URL  = "/payroll/periods/{pid}/finalization-preview"
FINALIZE_URL = "/payroll/periods/{pid}/finalize"
LEDGER_URL   = "/payroll/periods/{pid}/final-lines"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tok(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_driver(client, token, branch_id, suffix, hire_date="2091-01-01"):
    r = await client.post(
        "/core/drivers",
        json={"branch_id": branch_id, "full_name": f"P9 {suffix}",
              "driver_code": f"P9-{suffix[:8]}",
              "cdl_number": f"CDL-P9-{suffix[:6]}",
              "email": f"p9{suffix[:6].lower()}@example.com",
              "hire_date": hire_date},
        headers=_tok(token),
    )
    assert r.status_code == 201, f"create_driver: {r.text}"
    return r.json()["driver_id"]


async def _delete_driver(client, token, driver_id):
    await client.delete(f"/core/drivers/{driver_id}", headers=_tok(token))


async def _get_hourly_rate_type_id(client, token):
    r = await client.get("/payroll/rate-types", headers=_tok(token))
    assert r.status_code == 200
    for rt in r.json():
        if rt["rate_code"] == "HOURLY":
            return rt["rate_type_id"]
    raise AssertionError("HOURLY RateType not found")


async def _create_and_approve_rate(client, token, driver_id, rate_type_id,
                                   effective_from, amount="18.00"):
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


async def _open_period(client, token, branch_id, test_database_url, start, end):
    headers = _tok(token)
    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.begin() as db:
            user_id = (await db.execute(_text("""
                SELECT UserID FROM sec.Users WHERE CompanyID = 1 AND Username = 'admin'
            """))).scalar_one()
            setup_id = await create_setup(
                1, user_id, f"P9_{uuid4().hex[:12]}", "P9 trust setup", db,
            )
            anchor = _date.fromisoformat(start)
            draft_id = await create_draft(
                1, user_id, setup_id, db,
                payroll_frequency="Week", anchor_start_date=anchor,
                normal_days_off_mask=0,
            )
            await publish_version(1, user_id, setup_id, draft_id, anchor, db)
            await assign_setup(1, user_id, branch_id, setup_id, anchor, db)
    finally:
        await engine.dispose()
    preview = await client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": "OPEN_CREATION"}, headers=headers,
    )
    assert preview.status_code == 200, f"candidate preview: {preview.text}"
    selected = preview.json()["selected"]
    assert selected["start_date"] == start and selected["end_date"] == end
    assert selected["creatable"], selected
    created = await client.post(
        f"/payroll/branches/{branch_id}/period-creations",
        json={"candidate_key": selected["candidate_key"]}, headers=headers,
    )
    assert created.status_code == 201, f"create period: {created.text}"
    return created.json()["payroll_period_id"]


async def _advance_to_approved(client, token, pid, driver_id, work_date):
    """Open → InReview (HOURS line) → Approved via review flow."""
    headers = _tok(token)
    r = await client.post(f"/payroll/periods/{pid}/lines",
                          json={"driver_id": driver_id, "work_date": work_date,
                                "line_type": "HOURS", "quantity": "8"},
                          headers=headers)
    assert r.status_code == 201, f"add line: {r.text}"
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


async def _finalize(client, token, pid):
    r = await client.post(FINALIZE_URL.format(pid=pid), headers=_tok(token))
    assert r.status_code == 200, f"finalize: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# T1 — SourceSnapshot is populated on regular final lines
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p9_t1_sourcesnapshot_written_on_finalization(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
):
    """
    T1: After finalization, every non-SYS final line derived from a rate-driven
    draft line must retain scalar source IDs and immutable snapshot provenance.
    """
    headers = _tok(auth_token)
    driver_id = None
    pid = None

    try:
        rate_type_id = await _get_hourly_rate_type_id(session_client, auth_token)

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T1SNAP", hire_date="2091-01-01")
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id,
            effective_from=T1_START, amount="20.00",
        )

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T1_START, T1_END)
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T1_WORK)
        await _finalize(session_client, auth_token, pid)

        # Query the final lines directly to check SourceSnapshot
        rows = await direct_db.execute(
            _text("""
                SELECT fl.sourcesnapshot, fl.driverrateid, fl.ratetypeid, fl.payitemid,
                       fl.linetype
                FROM   payroll.payrollfinallines fl
                WHERE  fl.payrollperiodid = :pid
                  AND  fl.linetype NOT IN ('SYS_MIN_TOPUP', 'SYS_MAX_CAP')
                  AND  fl.driverrateid IS NOT NULL
            """),
            {"pid": pid},
        )
        rate_lines = rows.mappings().all()

        assert len(rate_lines) > 0, "Expected at least one rate-driven final line"

        for row in rate_lines:
            snap = row["sourcesnapshot"]
            assert snap is not None, (
                f"SourceSnapshot is NULL on linetype={row['linetype']}, "
                f"driverrateid={row['driverrateid']}"
            )
            # asyncpg returns JSONB as dict already; handle both str and dict
            if isinstance(snap, str):
                snap = _json.loads(snap)
            assert isinstance(snap, dict), f"SourceSnapshot is not a dict: {snap!r}"

            assert {"payroll_calculation_snapshot_id", "snapshot_line_id", "source_evidence"} <= snap.keys()
            assert snap["source_type"] == "DraftLine"
            assert snap["source_evidence"]["RateBehavior"] == "PerUnit"
            assert row["driverrateid"] is not None
            assert row["ratetypeid"] is not None
            assert row["payitemid"] is not None

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T2 — Ledger API exposes source_snapshot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p9_t2_ledger_api_exposes_sourcesnapshot(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
):
    """
    T2: GET /payroll/periods/{id}/final-lines returns the canonical immutable
    provenance wrapper alongside scalar source columns.
    """
    headers = _tok(auth_token)
    driver_id = None
    pid = None

    # Re-use T1_WORK dates with a slight offset so the period is fresh
    T2_START, T2_END, T2_WORK = "2091-02-02", "2091-02-08", "2091-02-03"

    try:
        rate_type_id = await _get_hourly_rate_type_id(session_client, auth_token)

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T2LEDG", hire_date="2091-01-01")
        await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id,
            effective_from=T2_START, amount="22.50",
        )

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T2_START, T2_END)
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T2_WORK)
        await _finalize(session_client, auth_token, pid)

        # Call the ledger API
        r = await session_client.get(LEDGER_URL.format(pid=pid), headers=headers)
        assert r.status_code == 200, f"ledger: {r.text}"
        lines = r.json()
        assert len(lines) > 0

        rate_lines = [ln for ln in lines if ln.get("driver_rate_id") is not None]
        assert len(rate_lines) > 0, "No rate-driven lines returned from ledger"

        for ln in rate_lines:
            snap = ln.get("source_snapshot")
            assert snap is not None, (
                f"source_snapshot is null in API response for linetype={ln['line_type']}"
            )
            assert isinstance(snap, dict)
            assert {"payroll_calculation_snapshot_id", "snapshot_line_id", "source_evidence"} <= snap.keys()
            assert snap["source_type"] == "DraftLine"
            assert ln["driver_rate_id"] is not None
            assert ln["rate_type_id"] is not None
            assert ln["pay_item_id"] is not None

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T3 — Direct SQL UPDATE of a used DriverRate's Amount is blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p9_t3_used_driverrate_amount_update_blocked(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
):
    """
    T3: After finalization references a DriverRate, a direct SQL UPDATE of that
    rate's Amount must be rejected with restrict_violation / mutation guard error.
    """
    driver_id = None
    pid = None
    rate_id = None

    T3_START2, T3_END2, T3_WORK2 = "2091-03-02", "2091-03-08", "2091-03-03"

    try:
        rate_type_id = await _get_hourly_rate_type_id(session_client, auth_token)

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T3UPDT", hire_date="2091-01-01")
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id,
            effective_from=T3_START2, amount="15.00",
        )

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T3_START2, T3_END2)
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T3_WORK2)
        await _finalize(session_client, auth_token, pid)

        # Verify the rate IS referenced in final lines
        ref = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        assert ref.scalar() > 0, "Rate not referenced in final lines — test precondition failed"

        # Attempt direct UPDATE of Amount — must be blocked
        blocked = False
        try:
            await direct_db.execute(
                _text("UPDATE payroll.driverrates SET amount = 99.99 WHERE driverrateid = :rid"),
                {"rid": rate_id},
            )
        except Exception as exc:
            err_str = str(exc).lower()
            assert "driverrate_mutation_guard" in err_str or "restrict_violation" in err_str, (
                f"Expected mutation guard error, got: {exc}"
            )
            blocked = True

        assert blocked, (
            "Expected UPDATE of used DriverRate Amount to be blocked, but it succeeded"
        )

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T4 — Direct SQL DELETE of a used DriverRate is blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p9_t4_used_driverrate_delete_blocked(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
):
    """
    T4: After finalization references a DriverRate, a direct SQL DELETE of that
    rate must be rejected with restrict_violation / mutation guard error.
    """
    driver_id = None
    pid = None
    rate_id = None

    T4_START2, T4_END2, T4_WORK2 = "2091-04-06", "2091-04-12", "2091-04-07"

    try:
        rate_type_id = await _get_hourly_rate_type_id(session_client, auth_token)

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T4DELT", hire_date="2091-01-01")
        rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id,
            effective_from=T4_START2, amount="17.00",
        )

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T4_START2, T4_END2)
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T4_WORK2)
        await _finalize(session_client, auth_token, pid)

        # Verify referenced
        ref = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        assert ref.scalar() > 0, "Rate not referenced — precondition failed"

        # Attempt direct DELETE — must be blocked
        blocked = False
        try:
            await direct_db.execute(
                _text("DELETE FROM payroll.driverrates WHERE driverrateid = :rid"),
                {"rid": rate_id},
            )
        except Exception as exc:
            err_str = str(exc).lower()
            assert "driverrate_mutation_guard" in err_str or "restrict_violation" in err_str, (
                f"Expected mutation guard error, got: {exc}"
            )
            blocked = True

        assert blocked, (
            "Expected DELETE of used DriverRate to be blocked, but it succeeded"
        )

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T5 — Unused DriverRate can still be updated and voided
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p9_t5_unused_driverrate_still_mutable(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
):
    """
    T5: A DriverRate that has NOT been referenced in any finalized PayrollFinalLines
    can still be updated (Amount) and voided via direct SQL — the mutation guard
    must not block unused rates.
    """
    driver_id = None

    try:
        rate_type_id = await _get_hourly_rate_type_id(session_client, auth_token)

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T5UNSD", hire_date="2092-01-01")

        # Create a rate — leave it as PendingApproval (never finalized)
        r = await session_client.post("/payroll/rates", json={
            "driver_id": driver_id, "rate_type_id": rate_type_id,
            "amount": "25.00", "effective_from": T5_START,
        }, headers=_tok(auth_token))
        assert r.status_code == 201, f"create rate: {r.text}"
        rate_id = r.json()["driver_rate_id"]

        # Verify NOT referenced in any final lines
        ref = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        assert ref.scalar() == 0, "Rate should not be referenced yet"

        # Direct UPDATE of Amount should succeed (unused rate)
        await direct_db.execute(
            _text("UPDATE payroll.driverrates SET amount = 30.00 WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        updated = await direct_db.execute(
            _text("SELECT amount FROM payroll.driverrates WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        assert Decimal(str(updated.scalar())) == Decimal("30.00"), "Amount update failed"

        # Direct DELETE should also succeed
        await direct_db.execute(
            _text("DELETE FROM payroll.driverrates WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        gone = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.driverrates WHERE driverrateid = :rid"),
            {"rid": rate_id},
        )
        assert gone.scalar() == 0, "Rate was not deleted"
        rate_id = None  # already deleted

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T6 — Future rate approval still works after historical finalized payroll
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p9_t6_future_rate_approval_after_historical_finalization(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
):
    """
    T6: Approving a new DriverRate for the same driver after an older rate has
    been referenced in finalized payroll must succeed.
    The supersession path (Status='Superseded' + EffectiveTo update on old rate)
    must NOT be blocked by the mutation guard.
    """
    headers = _tok(auth_token)
    driver_id = None
    pid_a = None

    try:
        rate_type_id = await _get_hourly_rate_type_id(session_client, auth_token)

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T6SUPR", hire_date="2093-01-01")

        # Create and approve historical rate
        old_rate_id = await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id,
            effective_from=T6A_START, amount="18.00",
        )

        # Finalize a period that uses the old rate
        pid_a = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                    T6A_START, T6A_END)
        await _advance_to_approved(session_client, auth_token, pid_a, driver_id,
                                    "2093-03-04")
        await _finalize(session_client, auth_token, pid_a)

        # Verify old rate is now referenced in final lines
        ref = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE driverrateid = :rid"),
            {"rid": old_rate_id},
        )
        assert ref.scalar() > 0, "Old rate not referenced — precondition failed"

        # Now create a NEW rate for the same driver+type (should supersede the old one)
        r = await session_client.post("/payroll/rates", json={
            "driver_id": driver_id, "rate_type_id": rate_type_id,
            "amount": "21.00", "effective_from": T6B_START,
        }, headers=headers)
        assert r.status_code == 201, f"new rate create: {r.text}"
        new_rate_id = r.json()["driver_rate_id"]

        # Approve the new rate — triggers supersession of the old rate
        r = await session_client.post(f"/payroll/rates/{new_rate_id}/approve",
                                       headers=headers)
        assert r.status_code == 200, (
            f"New rate approval failed (mutation guard may have blocked supersession): {r.text}"
        )

        # Verify old rate is now Superseded (Status change allowed by guard)
        old_status = await direct_db.execute(
            _text("SELECT status FROM payroll.driverrates WHERE driverrateid = :rid"),
            {"rid": old_rate_id},
        )
        assert old_status.scalar() == "Superseded", (
            "Old rate should be Superseded after new rate approval"
        )

        # Verify EffectiveTo was set on the old rate (supersession boundary)
        old_eff_to = await direct_db.execute(
            _text("SELECT effectiveto FROM payroll.driverrates WHERE driverrateid = :rid"),
            {"rid": old_rate_id},
        )
        assert old_eff_to.scalar() is not None, (
            "Old rate EffectiveTo should be set after supersession"
        )

    finally:
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)


# ---------------------------------------------------------------------------
# T7 — SYS_MIN_TOPUP line has a system_generated SourceSnapshot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p9_t7_sys_min_topup_has_system_snapshot(
    session_client: httpx.AsyncClient,
    auth_token: str,
    trust_branch_id: int,
    test_database_url: str,
    direct_db,
):
    """
    T7: When a SYS_MIN_TOPUP line is inserted during finalization, it must
    carry a SourceSnapshot with system_generated=True, reason='minimum_pay_topup',
    and the numeric amounts used to compute the top-up.
    """
    driver_id = None
    pid = None
    rule_id = None

    try:
        rate_type_id = await _get_hourly_rate_type_id(session_client, auth_token)

        # Look up companyid from the branch
        br_row = await direct_db.execute(
            _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": trust_branch_id},
        )
        company_id = br_row.scalar()
        assert company_id is not None, "Could not resolve company_id from branch"

        driver_id = await _create_driver(session_client, auth_token, trust_branch_id,
                                         "T7MNTOP", hire_date="2095-01-01")

        # Give driver a very small HOURLY rate so earned pay will be below minimum
        await _create_and_approve_rate(
            session_client, auth_token, driver_id, rate_type_id,
            effective_from=T7_START, amount="1.00",
        )

        # Insert a minimum pay rule that will exceed the driver's earned pay
        rule_result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, branchid, driverid, ruletype, amount,
                     effectivefrom, status)
                VALUES
                    (:cid, :bid, :did, 'MinimumPay', 500.00,
                     :edate, 'Active')
                RETURNING driverpayruleid
            """),
            {
                "cid": company_id,
                "bid": trust_branch_id,
                "did": driver_id,
                "edate": _date.fromisoformat(T7_START),
            },
        )
        rule_id = rule_result.scalar()

        pid = await _open_period(session_client, auth_token, trust_branch_id, test_database_url,
                                  T7_START, T7_END)
        await _advance_to_approved(session_client, auth_token, pid, driver_id, T7_WORK)
        await _finalize(session_client, auth_token, pid)

        # Find the SYS_MIN_TOPUP line
        rows = await direct_db.execute(
            _text("""
                SELECT sourcesnapshot, finalamount
                FROM   payroll.payrollfinallines
                WHERE  payrollperiodid = :pid AND linetype = 'SYS_MIN_TOPUP'
            """),
            {"pid": pid},
        )
        topup_rows = rows.mappings().all()
        assert len(topup_rows) == 1, (
            f"Expected 1 SYS_MIN_TOPUP line, found {len(topup_rows)}"
        )

        snap = topup_rows[0]["sourcesnapshot"]
        assert snap is not None, "SYS_MIN_TOPUP SourceSnapshot is NULL"
        if isinstance(snap, str):
            snap = _json.loads(snap)

        assert snap["source_type"] == "System", f"Unexpected source type: {snap}"
        evidence = snap["source_evidence"]
        assert evidence["RuleType"] == "MinimumPay"
        assert evidence["DriverPayRuleID"] == rule_id
        assert Decimal(str(evidence["RuleAmount"])) == Decimal("500")

        # The finalized system line amount should be positive.
        final_amount = Decimal(str(topup_rows[0]["finalamount"]))
        assert final_amount > 0, f"top-up final amount should be positive: {final_amount}"

    finally:
        # Used rules are immutable provenance and cannot be deleted.
        pass
        if driver_id:
            await _delete_driver(session_client, auth_token, driver_id)
