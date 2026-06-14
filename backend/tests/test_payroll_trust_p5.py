"""
Payroll Trust Phase 5 -- Used DriverRate Void Guard.

Verifies that a DriverRate referenced by PayrollFinalLines cannot be voided:

  T1  void_rate (API) rejects an Approved rate used in finalized payroll -- 422
  T2  void_rate (API) rejects a Superseded rate used in finalized payroll -- 422
  T3  void_rate (API) allows an unused Approved rate -- 204
  T4  DB trigger blocks direct SQL void of a finalized-used rate -- restrict_violation
  T5  DB trigger allows direct SQL void of an unused rate
  T6  finalization writes DriverRateID into PayrollFinalLines for rate-driven lines

All tests use years 2052-2058 on the PAYTEST branch.
Per-test ephemeral drivers are created and cleaned up in fixture teardown.
"""
import pytest
import pytest_asyncio
import httpx
import sqlalchemy.exc
from sqlalchemy import text as _text

# ---------------------------------------------------------------------------
# Year slots -- distinct from Phase 3C (2043-2051) and each other
# ---------------------------------------------------------------------------
T1_START, T1_END, T1_WORK = "2052-05-05", "2052-05-18", "2052-05-08"
T2_START, T2_END, T2_WORK = "2053-05-05", "2053-05-18", "2053-05-08"
T3_START, T3_END, T3_WORK = "2054-05-05", "2054-05-18", "2054-05-08"
T4_START, T4_END, T4_WORK = "2055-05-05", "2055-05-18", "2055-05-08"
T5_START, T5_END, T5_WORK = "2056-05-05", "2056-05-18", "2056-05-08"
T6_START, T6_END, T6_WORK = "2057-05-05", "2057-05-18", "2057-05-08"


# ---------------------------------------------------------------------------
# Helpers (adapted from test_payroll_trust_p3c)
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
    hire_date: str = "2052-01-01",
) -> int:
    r = await client.post(
        "/core/drivers",
        json={
            "branch_id":      branch_id,
            "full_name":      f"P5 Driver {suffix}",
            "preferred_name": f"P5-{suffix}",
            "driver_code":    f"P5DRV-{suffix}",
            "cdl_number":     f"CDL-P5-{suffix}",
            "email":          f"p5drv{suffix}@example.com",
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
    effective_from: str = "2052-01-01",
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
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str,
    end: str,
) -> int:
    headers = _auth(token)
    r = await client.post(
        "/payroll/periods",
        json={"branch_id": branch_id, "period_type": "Week",
              "start_date": start, "end_date": end},
        headers=headers,
    )
    assert r.status_code == 201, f"period create: {r.text}"
    pid = r.json()["payroll_period_id"]
    r = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "Open"},
        headers=headers,
    )
    assert r.status_code == 200, f"Open: {r.text}"
    return pid


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str,
) -> None:
    """Open -> InReview (add PTO line) -> Approved via review flow."""
    headers = _auth(token)

    dummy = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json={"driver_id": driver_id, "work_date": work_date,
              "line_type": "PTO_STATUS", "quantity": "1"},
        headers=headers,
    )
    assert dummy.status_code == 201, f"PTO line: {dummy.text}"

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
    Full flow: create driver + HOURS rate -> create period -> add HOURS line ->
    advance to Approved -> finalize (Locked).
    Returns (period_id, driver_id, rate_id).
    """
    drv = await _create_driver(client, token, branch_id, suffix,
                               hire_date=start[:4] + "-01-01")
    rate_id = await _create_and_approve_rate(
        client, token, drv, rate_type_id, "15.00",
        effective_from=start[:4] + "-01-01",
    )
    pid = await _make_period(client, token, branch_id, start, end)
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
async def p5_env(
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
# T1 -- void_rate (API) rejects Approved rate used in finalized payroll
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p5_t1_void_rejects_finalized_used_approved_rate(p5_env):
    """
    T1: After a payroll period is finalized with an Approved DriverRate,
    attempting to void that rate via DELETE /payroll/rates/{id} must return 422.
    The rate status must remain unchanged.
    """
    c, tok = p5_env["client"], p5_env["token"]
    bid, rtid = p5_env["branch_id"], p5_env["hourly_rtid"]
    db = p5_env["db"]

    pid, drv, rate_id = await _lock_period_with_rate(
        c, tok, bid, rtid, T1_START, T1_END, T1_WORK, "T1", db,
    )
    p5_env["created_drivers"].append(drv)

    # Confirm the final line references the DriverRate
    row = (await db.execute(_text("""
        SELECT driverrateid FROM payroll.payrollfinallines
        WHERE payrollperiodid = :pid AND driverrateid = :rid LIMIT 1
    """), {"pid": pid, "rid": rate_id})).mappings().first()
    assert row is not None, "PayrollFinalLines must reference the DriverRateID after finalization"

    # Attempt void
    resp = await c.delete(
        f"/payroll/rates/{rate_id}",
        headers=_auth(tok),
    )
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    detail = resp.json().get("detail", "")
    assert "finalized payroll" in detail.lower() or "cannot be voided" in detail.lower(), (
        f"Response detail should mention finalized payroll: {detail!r}"
    )

    # Rate status must be unchanged (Superseded or Approved -- not Voided)
    status_row = (await db.execute(_text(
        "SELECT status FROM payroll.driverrates WHERE driverrateid = :rid"
    ), {"rid": rate_id})).mappings().first()
    assert status_row is not None
    assert status_row["status"] != "Voided", (
        f"Rate must not be voided; status = {status_row['status']!r}"
    )

    # PayrollFinalLines still reference the DriverRateID
    ref_row = (await db.execute(_text("""
        SELECT 1 FROM payroll.payrollfinallines
        WHERE payrollperiodid = :pid AND driverrateid = :rid LIMIT 1
    """), {"pid": pid, "rid": rate_id})).mappings().first()
    assert ref_row is not None, "PayrollFinalLines must still reference the DriverRateID"


# ---------------------------------------------------------------------------
# T2 -- void_rate (API) rejects Superseded rate used in finalized payroll
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p5_t2_void_rejects_finalized_used_superseded_rate(p5_env):
    """
    T2: If a Superseded rate was referenced in finalized payroll, it cannot
    be voided. Supersession alone does not remove the audit-trail obligation.
    """
    c, tok = p5_env["client"], p5_env["token"]
    bid, rtid = p5_env["branch_id"], p5_env["hourly_rtid"]
    db = p5_env["db"]

    # Finalize payroll with rate A
    _pid, drv, rate_a_id = await _lock_period_with_rate(
        c, tok, bid, rtid, T2_START, T2_END, T2_WORK, "T2", db,
    )
    p5_env["created_drivers"].append(drv)

    # Approve rate B for the same driver/rate type -> rate A becomes Superseded
    rate_b_id = await _create_and_approve_rate(
        c, tok, drv, rtid, amount="20.00",
        effective_from=T2_START[:4] + "-06-01",
    )

    # Confirm rate A is now Superseded
    status_a = (await db.execute(_text(
        "SELECT status FROM payroll.driverrates WHERE driverrateid = :rid"
    ), {"rid": rate_a_id})).mappings().first()
    assert status_a is not None
    assert status_a["status"] == "Superseded", (
        f"Rate A should be Superseded after approving rate B, got {status_a['status']!r}"
    )

    # Attempt to void rate A (Superseded but used in final payroll)
    resp = await c.delete(f"/payroll/rates/{rate_a_id}", headers=_auth(tok))
    assert resp.status_code == 422, (
        f"Expected 422 for Superseded+finalized-used rate, got {resp.status_code}: {resp.text}"
    )
    detail = resp.json().get("detail", "")
    assert "finalized payroll" in detail.lower() or "cannot be voided" in detail.lower(), (
        f"Detail should mention finalized payroll: {detail!r}"
    )

    # Rate A must remain Superseded, not Voided
    status_a2 = (await db.execute(_text(
        "SELECT status FROM payroll.driverrates WHERE driverrateid = :rid"
    ), {"rid": rate_a_id})).mappings().first()
    assert status_a2["status"] == "Superseded", (
        f"Rate A must remain Superseded; got {status_a2['status']!r}"
    )

    # Clean up rate B
    await c.delete(f"/payroll/rates/{rate_b_id}", headers=_auth(tok))


# ---------------------------------------------------------------------------
# T3 -- void_rate (API) allows an unused Approved rate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p5_t3_void_allows_unused_rate(p5_env):
    """
    T3: A rate that has never been referenced in any PayrollFinalLines row
    can still be voided. Existing behavior must be preserved.
    """
    c, tok = p5_env["client"], p5_env["token"]
    bid, rtid = p5_env["branch_id"], p5_env["hourly_rtid"]

    drv = await _create_driver(c, tok, bid, "T3", hire_date="2054-01-01")
    p5_env["created_drivers"].append(drv)

    # Create + approve a rate, but do NOT finalize any payroll
    rate_id = await _create_and_approve_rate(
        c, tok, drv, rtid, amount="12.00", effective_from="2054-01-01",
    )

    # Void it -- must succeed
    resp = await c.delete(f"/payroll/rates/{rate_id}", headers=_auth(tok))
    assert resp.status_code == 204, f"Unused rate void must return 204: {resp.text}"


# ---------------------------------------------------------------------------
# T4 -- DB trigger blocks direct SQL void of finalized-used rate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p5_t4_db_trigger_blocks_direct_void_of_finalized_rate(p5_env):
    """
    T4: Migration 0037 adds trg_guard_driverrate_void.
    Direct DB UPDATE SET status='Voided' on a finalized-used rate must raise
    restrict_violation (sqlalchemy.exc.IntegrityError).
    """
    c, tok = p5_env["client"], p5_env["token"]
    bid, rtid = p5_env["branch_id"], p5_env["hourly_rtid"]
    db = p5_env["db"]

    pid, drv, rate_id = await _lock_period_with_rate(
        c, tok, bid, rtid, T4_START, T4_END, T4_WORK, "T4", db,
    )
    p5_env["created_drivers"].append(drv)

    # Confirm final line exists with this rate
    row = (await db.execute(_text("""
        SELECT driverrateid FROM payroll.payrollfinallines
        WHERE payrollperiodid = :pid AND driverrateid = :rid LIMIT 1
    """), {"pid": pid, "rid": rate_id})).mappings().first()
    assert row is not None, "Final line must reference the DriverRateID"

    # Direct DB void must be rejected by trigger
    with pytest.raises(sqlalchemy.exc.IntegrityError) as exc_info:
        await db.execute(_text(
            "UPDATE payroll.driverrates SET status = 'Voided' "
            "WHERE driverrateid = :rid"
        ), {"rid": rate_id})

    assert "driverrate_void_guard" in str(exc_info.value).lower(), (
        f"Expected driverrate_void_guard in error: {exc_info.value}"
    )

    # Rate must still not be Voided
    status_row = (await db.execute(_text(
        "SELECT status FROM payroll.driverrates WHERE driverrateid = :rid"
    ), {"rid": rate_id})).mappings().first()
    assert status_row["status"] != "Voided", "Rate must not be voided after trigger rejection"


# ---------------------------------------------------------------------------
# T5 -- DB trigger allows direct SQL void of unused rate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p5_t5_db_trigger_allows_direct_void_of_unused_rate(p5_env):
    """
    T5: The trg_guard_driverrate_void trigger must NOT block direct SQL void
    of a rate that has no PayrollFinalLines reference.
    """
    c, tok = p5_env["client"], p5_env["token"]
    bid, rtid = p5_env["branch_id"], p5_env["hourly_rtid"]
    db = p5_env["db"]

    drv = await _create_driver(c, tok, bid, "T5", hire_date="2056-01-01")
    p5_env["created_drivers"].append(drv)

    # Create + approve rate (no finalization)
    rate_id = await _create_and_approve_rate(
        c, tok, drv, rtid, amount="11.00", effective_from="2056-01-01",
    )

    # Direct DB void must succeed (no final line references this rate)
    await db.execute(_text(
        "UPDATE payroll.driverrates SET status = 'Voided' WHERE driverrateid = :rid"
    ), {"rid": rate_id})

    status_row = (await db.execute(_text(
        "SELECT status FROM payroll.driverrates WHERE driverrateid = :rid"
    ), {"rid": rate_id})).mappings().first()
    assert status_row["status"] == "Voided", (
        f"Unused rate should be Voided via direct SQL, got {status_row['status']!r}"
    )


# ---------------------------------------------------------------------------
# T6 -- finalization writes DriverRateID into PayrollFinalLines
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_p5_t6_finalization_writes_driver_rate_id(p5_env):
    """
    T6: PayrollFinalLines.DriverRateID must be non-NULL for HOURS lines after
    finalization when the driver has an active Approved/Superseded DriverRate.
    The source snapshot (DriverRateID, RateTypeID, ResolvedRateAmount, RateBehavior)
    must all be present.
    """
    c, tok = p5_env["client"], p5_env["token"]
    bid, rtid = p5_env["branch_id"], p5_env["hourly_rtid"]
    db = p5_env["db"]

    pid, drv, rate_id = await _lock_period_with_rate(
        c, tok, bid, rtid, T6_START, T6_END, T6_WORK, "T6", db,
    )
    p5_env["created_drivers"].append(drv)

    # Check the HOURS final line for the full source snapshot
    row = (await db.execute(_text("""
        SELECT driverrateid, ratetypeid, resolvedrateamount, ratebehavior
        FROM   payroll.payrollfinallines
        WHERE  payrollperiodid = :pid
          AND  driverid        = :did
          AND  linetype        = 'HOURS'
        LIMIT 1
    """), {"pid": pid, "did": drv})).mappings().first()

    assert row is not None, "No HOURS final line found after finalization"
    assert row["driverrateid"] is not None, (
        "DriverRateID must be non-NULL in final line for rate-driven HOURS line"
    )
    assert row["driverrateid"] == rate_id, (
        f"DriverRateID in final line ({row['driverrateid']}) must match "
        f"the approved rate ({rate_id})"
    )
    assert row["ratetypeid"] is not None, "RateTypeID must be populated in final line"
    assert row["resolvedrateamount"] is not None, (
        "ResolvedRateAmount must be populated in final line for PerUnit rate"
    )
    assert row["ratebehavior"] is not None, "RateBehavior must be populated in final line"
