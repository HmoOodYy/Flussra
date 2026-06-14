"""
Payroll Trust Phase 7 — Shared Preview / Finalization Validator.

Verifies that get_finalization_preview reports exactly the same blockers
that finalize_period would enforce.  Four structural checks are now shared:

  T1  Preview blocks duplicate Daily draft lines  (+ finalize also rejects)
  T2  Preview blocks ineligible driver line       (+ finalize also rejects)
  T3  Preview blocks contaminated/foreign RateType(+ finalize also rejects)
  T4  Preview blocks unresolved NMR/missing-rate  (+ finalize also rejects)
  T5  Valid period: preview can_finalize=True, finalize succeeds
  T6  Preview/finalize parity: same keyword in both responses for T1-T4

Year slots: 2066-2075 (distinct from P3C 2043-2051, P5 2052-2057,
P6 2058-2064, and T15/p4b_env which uses 2065).
"""
import pytest
import pytest_asyncio
import httpx
from decimal import Decimal
from datetime import date as _date
from sqlalchemy import text as _text

# Re-use the p4b_env fixture + helper for the contamination test (T3)
from tests.test_payroll_trust_p4b import (
    p4b_env,           # noqa: F401 – imported so pytest sees it as a fixture
    _bypass_trigger_insert_map,
    _get_token_b,
    _auth,
)


# ---------------------------------------------------------------------------
# Year slots
# ---------------------------------------------------------------------------
T1_START, T1_END, T1_WORK = "2066-03-04", "2066-03-10", "2066-03-05"
T2_START, T2_END, T2_WORK = "2067-04-07", "2067-04-13", "2067-04-08"
# T3 uses dates injected directly into Company B (managed inline)
T4_START, T4_END, T4_WORK = "2068-05-05", "2068-05-11", "2068-05-06"
T5_START, T5_END, T5_WORK = "2069-06-02", "2069-06-08", "2069-06-03"

PREVIEW_URL = "/payroll/periods/{pid}/finalization-preview"
FINALIZE_URL = "/payroll/periods/{pid}/finalize"


# ---------------------------------------------------------------------------
# Helpers (adapted from test_payroll_trust_p5/p6)
# ---------------------------------------------------------------------------

def _tok(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_periods(client, token, branch_id):
    headers = _tok(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
        resp = await client.get("/payroll/periods",
                                params={"branch_id": branch_id, "status": s},
                                headers=headers)
        if resp.status_code != 200:
            continue
        for p in resp.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"}, headers=headers,
            )


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


async def _open_period(client, token, branch_id, start, end):
    headers = _tok(token)
    r = await client.post("/payroll/periods",
                          json={"branch_id": branch_id, "period_type": "Week",
                                "start_date": start, "end_date": end},
                          headers=headers)
    assert r.status_code == 201, f"create period: {r.text}"
    pid = r.json()["payroll_period_id"]
    r = await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=headers)
    assert r.status_code == 200, f"Open: {r.text}"
    return pid


async def _advance_to_approved(client, token, pid, driver_id, work_date):
    """Open -> InReview (dummy PTO) -> Approved via review flow."""
    headers = _tok(token)
    await client.post(f"/payroll/periods/{pid}/lines",
                      json={"driver_id": driver_id, "work_date": work_date,
                            "line_type": "PTO_STATUS", "quantity": "1"},
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
# T1 — Preview blocks duplicate Daily draft lines
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p7_t1_preview_blocks_duplicate_daily_lines(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    """
    T1: After injecting duplicate Daily draft lines (bypassing the unique
    index), both preview and finalize must report a blocker containing
    'duplicate'.
    """
    headers = _tok(auth_token)
    await _cancel_periods(session_client, auth_token, paytest_branch_id)

    drv = await _create_driver(session_client, auth_token, paytest_branch_id,
                               "T1Dup", hire_date="2066-01-01")
    try:
        await _create_and_approve_rate(session_client, auth_token, drv,
                                       paytest_rate_type_id,
                                       effective_from="2066-01-01")
        pid = await _open_period(session_client, auth_token, paytest_branch_id,
                                 T1_START, T1_END)
        await _advance_to_approved(session_client, auth_token, pid, drv, T1_WORK)

        # Inject duplicate Daily lines by temporarily dropping the unique index.
        # Assertions run INSIDE the inner try so the index is only rebuilt after
        # the duplicates are voided (avoids UniqueViolation on CREATE INDEX).
        await direct_db.execute(_text(
            "DROP INDEX IF EXISTS "
            "payroll.uix_payrolldraftlines_daily_active_business_key"
        ))
        try:
            for _ in range(2):
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrolldraftlines
                            (companyid, branchid, payrollperiodid, driverid,
                             workdate, linetype, linescope, quantity,
                             sourcetype, status, needsmanagerreview, addedbyuserid)
                        SELECT companyid, :bid, :pid, :did,
                               :wdate, 'PTO_STATUS', 'Daily', 1,
                               'Manual', 'Active', FALSE, 1
                        FROM   payroll.payrollperiods
                        WHERE  payrollperiodid = :pid
                    """),
                    {"bid": paytest_branch_id, "pid": pid, "did": drv,
                     "wdate": _date.fromisoformat(T1_WORK)},
                )

            # ── Preview must report can_finalize=False with duplicate blocker ──
            prev = await session_client.get(
                PREVIEW_URL.format(pid=pid), headers=headers,
            )
            assert prev.status_code == 200, f"preview: {prev.text}"
            data = prev.json()
            assert data["can_finalize"] is False, "Preview must report can_finalize=False"
            assert any("duplicate" in b.lower() for b in data["blockers"]), (
                f"Preview must report duplicate blocker; got: {data['blockers']}"
            )

            # ── Finalize must also reject with same keyword ──
            fin = await session_client.post(FINALIZE_URL.format(pid=pid), headers=headers)
            assert fin.status_code == 422, f"finalize must be blocked; got {fin.status_code}"
            assert "duplicate" in fin.json().get("detail", "").lower(), (
                f"finalize detail must mention 'duplicate': {fin.json()}"
            )

            # T6 parity: same keyword in preview blocker and finalize detail
            preview_text = " ".join(data["blockers"]).lower()
            finalize_text = fin.json().get("detail", "").lower()
            assert "duplicate" in preview_text and "duplicate" in finalize_text, (
                "T6 parity: 'duplicate' must appear in both preview blockers and finalize detail"
            )

        finally:
            # Void ALL PTO_STATUS lines for this period so the unique index can
            # be rebuilt cleanly (original + 2 injected → no active duplicates).
            await direct_db.execute(
                _text("""
                    UPDATE payroll.payrolldraftlines
                    SET    status = 'Void'
                    WHERE  payrollperiodid = :pid AND linetype = 'PTO_STATUS'
                """),
                {"pid": pid},
            )
            await direct_db.execute(_text("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    uix_payrolldraftlines_daily_active_business_key
                ON payroll.payrolldraftlines
                    (companyid, payrollperiodid, driverid, workdate, linetype)
                WHERE linescope = 'Daily'
                  AND status   != 'Void'
            """))

    finally:
        await _delete_driver(session_client, auth_token, drv)


# ---------------------------------------------------------------------------
# T2 — Preview blocks ineligible driver line
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p7_t2_preview_blocks_ineligible_driver_line(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    """
    T2: After terminating the driver so the work_date falls after termination,
    both preview and finalize must report a blocker containing 'eligible' or
    'ineligible'.
    """
    headers = _tok(auth_token)
    await _cancel_periods(session_client, auth_token, paytest_branch_id)

    drv = await _create_driver(session_client, auth_token, paytest_branch_id,
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
        pid = await _open_period(session_client, auth_token, paytest_branch_id,
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

        # ── Preview must report can_finalize=False with eligibility blocker ──
        prev = await session_client.get(
            PREVIEW_URL.format(pid=pid), headers=headers,
        )
        assert prev.status_code == 200, f"preview: {prev.text}"
        data = prev.json()
        assert data["can_finalize"] is False, "Preview must report can_finalize=False"
        blockers_text = " ".join(data["blockers"]).lower()
        assert "eligible" in blockers_text or "ineligible" in blockers_text, (
            f"Preview must report eligibility blocker; got: {data['blockers']}"
        )

        # ── Finalize must also reject ──
        fin = await session_client.post(FINALIZE_URL.format(pid=pid), headers=headers)
        assert fin.status_code == 422, f"finalize must be blocked; got {fin.status_code}"
        detail = fin.json().get("detail", "").lower()
        assert "eligible" in detail or "ineligible" in detail, (
            f"finalize detail must mention eligibility: {fin.json()}"
        )

        # T6 parity
        assert ("eligible" in blockers_text or "ineligible" in blockers_text) and \
               ("eligible" in detail or "ineligible" in detail), (
            "T6 parity: eligibility keyword must appear in both preview and finalize"
        )

    finally:
        # Restore terminationdate so driver cleanup works
        await direct_db.execute(
            _text("UPDATE core.employees SET terminationdate = NULL WHERE employeeid = :eid"),
            {"eid": emp_id},
        )
        await _delete_driver(session_client, auth_token, drv)


# ---------------------------------------------------------------------------
# T3 — Preview blocks contaminated / foreign RateType
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p7_t3_preview_blocks_contaminated_rate_type(
    p4b_env,
    client: httpx.AsyncClient,
    direct_db,
):
    """
    T3: When a Company B period has a draft line referencing a Company A
    (foreign) CPI_ RateType via a contaminated PayItemRateTypeMap, both
    preview and finalize must report a blocker containing 'rate type' /
    'contaminated' / 'not valid'.

    Setup mirrors T15 in test_payroll_trust_p4b.py (direct DB injection
    bypassing Phase 4C trigger) but now also checks the preview endpoint.
    """
    cid_b   = p4b_env["cid_b"]
    bid_b   = p4b_env["bid_b"]
    pi_b_id = p4b_env["pi_b_id"]
    rt_a_id = p4b_env["rt_a_custom_id"]
    drv_b   = p4b_env["driver_b_id"]
    uid_b   = p4b_env["uid_b"]
    token_b = await _get_token_b(client)

    pi_b_code = (await direct_db.execute(
        _text("SELECT payitemcode FROM payroll.payitems WHERE payitemid = :id"),
        {"id": pi_b_id},
    )).scalar_one()

    # Contaminate: map Company B's PayItem to Company A's CPI_ RateType
    await _bypass_trigger_insert_map(direct_db, pi_b_id, rt_a_id)

    # Directly insert an Approved payroll period for Company B
    period_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.payrollperiods
            (companyid, branchid, periodcode, periodname, periodtype,
             startdate, enddate, status, createdbyuserid)
        VALUES (:cid, :bid, 'P7T3-2067-01', 'P7 T3 Contamination', 'Week',
                '2067-08-04', '2067-08-10', 'Approved', :uid)
        RETURNING payrollperiodid
    """), {"cid": cid_b, "bid": bid_b, "uid": uid_b})).mappings().first()
    pid = period_row["payrollperiodid"]

    # Directly insert a non-void draft line for Company B driver
    draft_row = (await direct_db.execute(_text("""
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverid,
             workdate, linetype, linescope, quantity, rateamount,
             status, sourcetype, needsmanagerreview)
        VALUES (:cid, :bid, :pid, :did,
                '2067-08-05', :lt, 'Daily', 1, '10.00',
                'Approved', 'Manual', FALSE)
        RETURNING draftlineid
    """), {"cid": cid_b, "bid": bid_b, "pid": pid,
           "did": drv_b, "lt": pi_b_code})).mappings().first()
    draft_id = draft_row["draftlineid"]

    try:
        # ── Preview must report can_finalize=False with contamination blocker ──
        prev = await client.get(
            PREVIEW_URL.format(pid=pid), headers=_auth(token_b),
        )
        assert prev.status_code == 200, f"preview: {prev.text}"
        data = prev.json()
        assert data["can_finalize"] is False, (
            f"Preview must report can_finalize=False for contaminated RateType; "
            f"blockers={data['blockers']}"
        )
        blockers_text = " ".join(data["blockers"]).lower()
        assert ("rate type" in blockers_text or "contaminated" in blockers_text
                or "not valid" in blockers_text), (
            f"Preview must mention contaminated/rate type; got: {data['blockers']}"
        )

        # ── Finalize must also reject ──
        fin = await client.post(FINALIZE_URL.format(pid=pid), headers=_auth(token_b))
        assert fin.status_code == 422, f"finalize must be blocked; got {fin.status_code}"
        detail = fin.json().get("detail", "").lower()
        assert ("rate type" in detail or "contaminated" in detail
                or "not valid" in detail), (
            f"finalize detail must mention contamination: {fin.json()}"
        )

        # T6 parity
        assert any(kw in blockers_text for kw in ("rate type", "contaminated", "not valid")) \
            and any(kw in detail for kw in ("rate type", "contaminated", "not valid")), (
            "T6 parity: contamination keyword must appear in both preview and finalize"
        )

    finally:
        await direct_db.execute(
            _text("DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.payrolldraftlines WHERE draftlineid = :id"),
            {"id": draft_id},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        await direct_db.execute(_text("""
            DELETE FROM payroll.payitemratetypemap
            WHERE payitemid = :piid AND ratetypeid = :rtid
        """), {"piid": pi_b_id, "rtid": rt_a_id})


# ---------------------------------------------------------------------------
# T4 — Preview blocks unresolved NMR / missing-rate line
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p7_t4_preview_blocks_nmr_unresolved_line(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    """
    T4: A driver with no approved HOURLY rate will produce an HOURS draft line
    with needsmanagerreview=True (calculatedamount cannot be resolved).  Both
    preview and finalize must report a blocker mentioning 'manager review' or
    'nmr' / 'unresolved'.
    """
    headers = _tok(auth_token)
    await _cancel_periods(session_client, auth_token, paytest_branch_id)

    # Create driver WITHOUT any approved rate so the HOURS line will have
    # needsmanagerreview=True (no rate → cannot resolve calculatedamount).
    # We force the period to Approved status directly in the DB to bypass the
    # InReview NMR guard (which would block us from submitting for review with
    # unresolved lines).  The goal is to test that BOTH preview and finalize
    # detect the unresolved-NMR state — not to test the review workflow itself.
    drv = await _create_driver(session_client, auth_token, paytest_branch_id,
                               "T4NMR", hire_date="2068-01-01")
    try:
        pid = await _open_period(session_client, auth_token, paytest_branch_id,
                                 T4_START, T4_END)

        # Add HOURS line — NMR=True because no rate exists for this driver
        r = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": drv, "work_date": T4_WORK,
                  "line_type": "HOURS", "quantity": "8"},
            headers=headers,
        )
        assert r.status_code == 201, f"add HOURS: {r.text}"

        # Force period to Approved bypassing the review workflow so we can
        # call preview/finalize without clearing the NMR line.
        await direct_db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Approved' WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )

        # ── Preview must report can_finalize=False ──
        prev = await session_client.get(
            PREVIEW_URL.format(pid=pid), headers=headers,
        )
        assert prev.status_code == 200, f"preview: {prev.text}"
        data = prev.json()
        assert data["can_finalize"] is False, (
            f"Preview must be blocked; blockers={data['blockers']}"
        )
        blockers_text = " ".join(data["blockers"]).lower()
        assert ("manager review" in blockers_text
                or "unresolved" in blockers_text
                or "nmr" in blockers_text), (
            f"Preview must mention NMR/unresolved; got: {data['blockers']}"
        )

        # ── Finalize must also reject ──
        fin = await session_client.post(FINALIZE_URL.format(pid=pid), headers=headers)
        assert fin.status_code == 422, f"finalize must be blocked; got {fin.status_code}"
        detail = fin.json().get("detail", "").lower()
        assert ("manager review" in detail or "unresolved" in detail
                or "nmr" in detail), (
            f"finalize detail must mention NMR/unresolved: {fin.json()}"
        )

        # T6 parity
        assert (any(kw in blockers_text for kw in ("manager review", "unresolved", "nmr"))
                and any(kw in detail for kw in ("manager review", "unresolved", "nmr"))), (
            "T6 parity: NMR keyword must appear in both preview and finalize"
        )

    finally:
        await _delete_driver(session_client, auth_token, drv)


# ---------------------------------------------------------------------------
# T5 — Valid period: preview can_finalize=True, finalize succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p7_t5_valid_period_preview_and_finalize(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    """
    T5: A clean period with an approved rate, no blockers, and eligible driver
    must have preview can_finalize=True and finalize must succeed (200 Locked).
    """
    headers = _tok(auth_token)
    await _cancel_periods(session_client, auth_token, paytest_branch_id)

    drv = await _create_driver(session_client, auth_token, paytest_branch_id,
                               "T5Valid", hire_date="2069-01-01")
    try:
        await _create_and_approve_rate(session_client, auth_token, drv,
                                       paytest_rate_type_id,
                                       effective_from="2069-01-01")
        pid = await _open_period(session_client, auth_token, paytest_branch_id,
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
            {"bid": paytest_branch_id},
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
