"""
Payroll Trust Phase 8 — Fail-Closed Rate Behavior.

Verifies that _resolve_rate_behavior no longer falls back to PerUnit for
unmapped/orphaned RateTypes, and that preview/finalization block periods that
contain lines with unresolvable rate type mappings.

  T1  Unmapped system-like RateType (companyid=NULL) → DriverRate create rejected
  T2  Unmapped company-owned RateType (companyid=cid) → DriverRate create rejected
  T3  Valid system HOURLY RateType still works (create, approve, finalize)
  T4  Valid custom PerUnit RateType with mapping still works (create DriverRate)
  T5  Preview/finalize parity for draft line with unresolvable rate mapping

Year slots: 2076-2090 (distinct from P6 2058-2064, P7 2066-2075).
"""
import pytest
import pytest_asyncio
import httpx
from datetime import date as _date
from sqlalchemy import text as _text, text as _sqla_text

# ---------------------------------------------------------------------------
# Year slots / URL templates
# ---------------------------------------------------------------------------
T3_START, T3_END, T3_WORK = "2078-03-03", "2078-03-09", "2078-03-04"
T5_START, T5_END, T5_WORK = "2080-05-05", "2080-05-11", "2080-05-06"

PREVIEW_URL  = "/payroll/periods/{pid}/finalization-preview"
FINALIZE_URL = "/payroll/periods/{pid}/finalize"

# Unique identifiers for test-only DB rows (cleaned up in finally blocks)
T1_RATE_CODE  = "TST_P8_T1_SYS"   # companyid=NULL, no PayItemRateTypeMap
T2_RATE_CODE  = "TST_P8_T2_CPI"   # companyid=cid,  no PayItemRateTypeMap
T4_RATE_CODE  = "TST_P8_T4_RT"    # companyid=cid,  WITH PayItemRateTypeMap
T4_ITEM_CODE  = "TST_P8_T4_ITEM"  # custom PayItem for T4
T5_ITEM_CODE  = "TST_P8_T5_ITEM"  # custom PayItem for T5 (no mapping)


# ---------------------------------------------------------------------------
# Helpers (shared with P6/P7 pattern)
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
            await client.patch(f"/payroll/periods/{p['payroll_period_id']}/status",
                               json={"status": "Cancelled"},
                               headers=headers)


async def _create_driver(client, token, branch_id, suffix, hire_date="2076-01-01"):
    r = await client.post(
        "/core/drivers",
        json={"branch_id": branch_id, "full_name": f"P8 {suffix}",
              "driver_code": f"P8-{suffix[:8]}",
              "cdl_number": f"CDL-P8-{suffix[:6]}",
              "email": f"p8{suffix[:6].lower()}@example.com",
              "hire_date": hire_date},
        headers=_tok(token),
    )
    assert r.status_code == 201, f"create_driver: {r.text}"
    return r.json()["driver_id"]


async def _delete_driver(client, token, driver_id):
    await client.delete(f"/core/drivers/{driver_id}", headers=_tok(token))


async def _create_and_approve_rate(client, token, driver_id, rate_type_id,
                                   effective_from="2076-01-01", amount="18.00"):
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
        {"bid": branch_id, "code": f"P8-{branch_id}-{start}",
         "name": f"P8 {start}", "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _advance_to_approved(client, token, pid, driver_id, work_date):
    """Open → InReview (dummy DailyNote) → Approved via review flow."""
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
# T1 — Unmapped system-like RateType (companyid=NULL) → DriverRate rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p8_t1_unmapped_system_ratetype_fails_closed(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_driver_id: int,
    direct_db,
):
    """
    T1: A RateType with companyid=NULL and no PayItemRateTypeMap entry is an
    orphaned system-like type whose rate behavior is unresolvable.
    Creating a DriverRate for it must be rejected with 422 mentioning
    'rate behavior' or 'mapping' — NOT silently treated as PerUnit.
    """
    headers = _tok(auth_token)

    # Insert the orphaned system-like RateType directly (no mapping row)
    rt_id_row = await direct_db.execute(
        _text("""
            INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
            VALUES (:code, 'P8 T1 Test Type', 'Unit', TRUE, NULL)
            RETURNING ratetypeid
        """),
        {"code": T1_RATE_CODE},
    )
    rt_id = rt_id_row.scalar_one()

    try:
        # Attempt to create a DriverRate for the unmapped RateType via API
        r = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":     paytest_driver_id,
                "rate_type_id":  rt_id,
                "amount":        "10.00",
                "effective_from": "2076-01-01",
            },
            headers=headers,
        )
        assert r.status_code == 422, (
            f"Expected 422 for unmapped RateType; got {r.status_code}: {r.text}"
        )
        detail = r.json().get("detail", "").lower()
        assert "rate behavior" in detail or "mapping" in detail, (
            f"Error must mention 'rate behavior' or 'mapping'; got: {detail!r}"
        )
        # Explicit negative: must NOT silently claim PerUnit
        assert "perunit" not in detail.replace(" ", ""), (
            f"Must not fall back to PerUnit; got: {detail!r}"
        )

    finally:
        # Delete the test RateType (no DriverRates were created)
        await direct_db.execute(
            _text("DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"),
            {"rtid": rt_id},
        )


# ---------------------------------------------------------------------------
# T2 — Unmapped company-owned (CPI-style) RateType → DriverRate rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p8_t2_unmapped_company_ratetype_fails_closed(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_driver_id: int,
    direct_db,
):
    """
    T2: A RateType owned by the company (companyid=cid) with no PayItemRateTypeMap
    is an orphaned/CPI-style type whose behavior is unresolvable.
    Creating a DriverRate for it must be rejected with 422 — not PerUnit fallback.
    """
    headers = _tok(auth_token)

    # Resolve company_id for paytest branch
    cid_row = await direct_db.execute(
        _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
        {"bid": paytest_branch_id},
    )
    company_id = cid_row.scalar_one()

    # Insert an orphaned company-owned RateType (no mapping row)
    rt_id_row = await direct_db.execute(
        _text("""
            INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
            VALUES (:code, 'P8 T2 CPI Test', 'Unit', TRUE, :cid)
            RETURNING ratetypeid
        """),
        {"code": T2_RATE_CODE, "cid": company_id},
    )
    rt_id = rt_id_row.scalar_one()

    try:
        r = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":     paytest_driver_id,
                "rate_type_id":  rt_id,
                "amount":        "10.00",
                "effective_from": "2077-01-01",
            },
            headers=headers,
        )
        assert r.status_code == 422, (
            f"Expected 422 for orphaned CPI RateType; got {r.status_code}: {r.text}"
        )
        detail = r.json().get("detail", "").lower()
        assert "rate behavior" in detail or "mapping" in detail, (
            f"Error must mention 'rate behavior' or 'mapping'; got: {detail!r}"
        )

    finally:
        await direct_db.execute(
            _text("DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"),
            {"rtid": rt_id},
        )


# ---------------------------------------------------------------------------
# T3 — Valid system HOURLY RateType still works end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p8_t3_valid_system_ratetype_still_works(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    """
    T3: The fail-closed change must not break standard system rate types.
    HOURLY has a PayItemRateTypeMap entry → create + approve DriverRate succeeds,
    period with HOURS lines can be finalized (preview can_finalize=True, status=Locked).
    """
    headers = _tok(auth_token)
    await _cancel_periods(session_client, auth_token, paytest_branch_id)

    drv = await _create_driver(session_client, auth_token, paytest_branch_id,
                               "T3Sys", hire_date="2078-01-01")
    try:
        # Create and approve HOURLY rate — must NOT raise 422
        await _create_and_approve_rate(session_client, auth_token, drv,
                                       paytest_rate_type_id,
                                       effective_from="2078-01-01")

        pid = await _open_period(direct_db, paytest_branch_id,
                                 T3_START, T3_END)

        # Add HOURS line (rate-dependent)
        r = await session_client.post(f"/payroll/periods/{pid}/lines",
                                      json={"driver_id": drv, "work_date": T3_WORK,
                                            "line_type": "HOURS", "quantity": "8"},
                                      headers=headers)
        assert r.status_code == 201, f"add HOURS: {r.text}"

        await _advance_to_approved(session_client, auth_token, pid, drv, T3_WORK)

        # Preview must report can_finalize=True with empty blockers
        prev = await session_client.get(PREVIEW_URL.format(pid=pid), headers=headers)
        assert prev.status_code == 200, f"preview: {prev.text}"
        data = prev.json()
        assert data["can_finalize"] is True, (
            f"System HOURLY should be finalizable; blockers={data.get('blockers')}"
        )

        # Finalize must succeed
        fin = await session_client.post(FINALIZE_URL.format(pid=pid), headers=headers)
        assert fin.status_code == 200, f"finalize: {fin.text}"
        assert fin.json()["status"] == "Locked"

    finally:
        await _delete_driver(session_client, auth_token, drv)


# ---------------------------------------------------------------------------
# T4 — Valid custom PerUnit RateType with mapping still works
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p8_t4_valid_custom_ratetype_with_mapping_still_works(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_driver_id: int,
    direct_db,
):
    """
    T4: A custom (CPI-style) RateType that HAS an active PayItemRateTypeMap entry
    must continue to work — _resolve_rate_behavior returns the correct behavior
    (PerUnit) and DriverRate creation succeeds.
    """
    headers = _tok(auth_token)

    # Resolve company_id
    cid_row = await direct_db.execute(
        _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
        {"bid": paytest_branch_id},
    )
    company_id = cid_row.scalar_one()

    # Insert a custom RateType
    rt_id_row = await direct_db.execute(
        _text("""
            INSERT INTO payroll.ratetypes (ratecode, ratename, unitname, isactive, companyid)
            VALUES (:code, 'P8 T4 Custom RT', 'Unit', TRUE, :cid)
            RETURNING ratetypeid
        """),
        {"code": T4_RATE_CODE, "cid": company_id},
    )
    rt_id = rt_id_row.scalar_one()

    # Insert a matching custom PayItem
    pi_id_row = await direct_db.execute(
        _text("""
            INSERT INTO payroll.payitems
                (companyid, payitemcode, payitemname, ratebehavior,
                 requiresrate, isdefaultbranchactive, status,
                 category, datatype, itemscope)
            VALUES
                (:cid, :code, 'P8 T4 Custom Item', 'PerUnit',
                 TRUE, TRUE, 'Active',
                 'Custom', 'Decimal', 'Daily')
            RETURNING payitemid
        """),
        {"cid": company_id, "code": T4_ITEM_CODE},
    )
    pi_id = pi_id_row.scalar_one()

    # Link PayItem → RateType via PayItemRateTypeMap
    await direct_db.execute(
        _text("""
            INSERT INTO payroll.payitemratetypemap
                (payitemid, ratetypeid, isprimary, status)
            VALUES (:piid, :rtid, TRUE, 'Active')
        """),
        {"piid": pi_id, "rtid": rt_id},
    )

    try:
        # DriverRate create for the mapped custom RateType must SUCCEED (no 422)
        r = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":     paytest_driver_id,
                "rate_type_id":  rt_id,
                "amount":        "15.00",
                "effective_from": "2079-01-01",
            },
            headers=headers,
        )
        assert r.status_code == 201, (
            f"Custom RateType with mapping should succeed; got {r.status_code}: {r.text}"
        )
        dr_id = r.json()["driver_rate_id"]

        # Approve must also succeed
        ar = await session_client.post(f"/payroll/rates/{dr_id}/approve", headers=headers)
        assert ar.status_code == 200, f"approve custom rate: {ar.text}"

    finally:
        # Clean up: delete DriverRates → PayItemRateTypeMap → PayItem → RateType
        await direct_db.execute(
            _text("""
                DELETE FROM payroll.driverrates
                WHERE ratetypeid = :rtid
            """),
            {"rtid": rt_id},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.payitemratetypemap WHERE payitemid = :piid"),
            {"piid": pi_id},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.payitems WHERE payitemid = :piid"),
            {"piid": pi_id},
        )
        await direct_db.execute(
            _text("DELETE FROM payroll.ratetypes WHERE ratetypeid = :rtid"),
            {"rtid": rt_id},
        )


# ---------------------------------------------------------------------------
# T5 — Unresolvable rate mapping blocks Submit before snapshot capture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p8_t5_unresolvable_mapping_blocks_submit_before_snapshot_capture(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """
    T5: A draft line whose PayItem has ratebehavior='PerUnit' but NO
    PayItemRateTypeMap entry has an unresolvable rate behavior.
    Submit must fail before it can capture an immutable snapshot.
    """
    headers = _tok(auth_token)
    await _cancel_periods(session_client, auth_token, paytest_branch_id)

    # Resolve company_id
    cid_row = await direct_db.execute(
        _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
        {"bid": paytest_branch_id},
    )
    company_id = cid_row.scalar_one()

    # Insert a custom PayItem with PerUnit behavior but NO PayItemRateTypeMap.
    # isdefaultbranchactive=TRUE so it is immediately usable in the branch.
    pi_id_row = await direct_db.execute(
        _text("""
            INSERT INTO payroll.payitems
                (companyid, payitemcode, payitemname, ratebehavior,
                 requiresrate, isdefaultbranchactive, status,
                 category, datatype, itemscope)
            VALUES
                (:cid, :code, 'P8 T5 Unmapped Item', 'PerUnit',
                 TRUE, TRUE, 'Active',
                 'Custom', 'Decimal', 'Daily')
            RETURNING payitemid
        """),
        {"cid": company_id, "code": T5_ITEM_CODE},
    )
    pi_id = pi_id_row.scalar_one()

    drv = await _create_driver(session_client, auth_token, paytest_branch_id,
                               "T5Map", hire_date="2080-01-01")
    pid = None
    try:
        pid = await _open_period(direct_db, paytest_branch_id,
                                 T5_START, T5_END)

        # Add a draft line using the unmapped custom pay item
        r = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": drv, "work_date": T5_WORK,
                  "line_type": T5_ITEM_CODE, "quantity": "5"},
            headers=headers,
        )
        assert r.status_code == 201, f"add unmapped line: {r.text}"

        BLOCKER_KEYWORDS = (
            "manager review", "unresolved", "rate behavior", "mapping", "unresolvable",
        )
        submit = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=headers,
        )
        assert submit.status_code == 422, f"submit: {submit.text}"
        detail = submit.json().get("detail", "").lower()
        assert any(kw in detail for kw in BLOCKER_KEYWORDS), detail
        snapshot_count = (await direct_db.execute(_text("""
            SELECT COUNT(*)
            FROM payroll.payrollcalculationsnapshots
            WHERE payrollperiodid = :pid
        """), {"pid": pid})).scalar_one()
        assert snapshot_count == 0

    finally:
        if pid is not None:
            # Release the workflow slot while retaining any immutable evidence.
            await direct_db.execute(
                _text("ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert")
            )
            await direct_db.execute(
                _text("UPDATE payroll.payrollperiods SET status = 'Cancelled', currentreturnreviewitemid = NULL WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )
            await direct_db.execute(
                _text("ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert")
            )
        await _delete_driver(session_client, auth_token, drv)
        await direct_db.execute(
            _text("DELETE FROM payroll.payitems WHERE payitemid = :piid"),
            {"piid": pi_id},
        )
