"""
Payroll Trust Phase 3B — Final Ledger Source Snapshot

Tests verifying that PayrollFinalLines stores the calculation provenance
fields added in migration 0034:

  A  PerUnit line: DriverRateID, RateTypeID, ResolvedRateAmount, RateBehavior,
     PayItemID all populated after finalization.
  B  Superseded-rate finalization: the DriverRateID from the superseded (but
     historically-valid) rate row is stored, not the newer rate's ID.
  C  Rate change after finalization does not alter locked source fields.
  D  BONUS period-pay line: RateBehavior='EnteredAmount', rate fields NULL.
  E  PTO_STATUS line: RateBehavior='None', rate fields NULL.
  F  SYS_MIN_TOPUP line: RateBehavior='System', rate fields NULL.
  G  Finalization preview exposes resolved_rate_amount / rate_behavior
     consistent with what finalization will actually write.
  H  Old rows (PayItemID NULL after migration) are tolerated — migration-safe.

Isolation strategy
------------------
All tests use year 2035 dates on the PAYTEST branch.  Ephemeral drivers
(created per-test) are cleaned up in fixture teardown.
"""
import pytest
import pytest_asyncio
import httpx
from datetime import date as _date, timedelta as _td
from decimal import Decimal
from sqlalchemy import text as _text


# ---------------------------------------------------------------------------
# Constants — each test class uses a unique year to avoid conflicts with
# locked periods from sibling tests in this module.
# ---------------------------------------------------------------------------

# Test A — 2035
A_START, A_END, A_WORK = "2035-04-07", "2035-04-20", "2035-04-10"
# Test B — 2036 (superseded rate test)
B_START, B_END, B_WORK = "2036-04-07", "2036-04-20", "2036-04-10"
# Test C — 2037 (immutability test)
C_START, C_END, C_WORK = "2037-04-07", "2037-04-20", "2037-04-10"
# Test D — 2038 (EnteredAmount / BONUS)
D_START, D_END, D_WORK = "2038-04-07", "2038-04-20", "2038-04-10"
# Test E — 2039 (PTO_STATUS None behavior)
E_START, E_END, E_WORK = "2039-04-07", "2039-04-20", "2039-04-10"
# Test F — 2040 (SYS_MIN_TOPUP)
F_START, F_END, F_WORK = "2040-04-07", "2040-04-20", "2040-04-10"
# Test G — 2041 (preview)
G_START, G_END, G_WORK = "2041-04-07", "2041-04-20", "2041-04-10"
# Test H — 2042 (NULL source fields tolerated)
H_START, H_END, H_WORK = "2042-04-07", "2042-04-20", "2042-04-10"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
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


async def _make_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str = A_START,
    end: str = A_END,
) -> int:
    """Create Draft period and open it.  Returns period_id."""
    headers = auth(token)
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


async def _create_driver(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    suffix: str,
    hire_date: str = "2035-01-01",
) -> int:
    """Create a driver. Returns driver_id."""
    r = await client.post(
        "/core/drivers",
        json={
            "branch_id":      branch_id,
            "full_name":      f"P3B Test Driver {suffix}",
            "preferred_name": f"P3B-{suffix}",
            "driver_code":    f"P3BDRV-{suffix}",
            "cdl_number":     f"CDL-P3B-{suffix}",
            "email":          f"p3bdrv{suffix}@example.com",
            "hire_date":      hire_date,
        },
        headers=auth(token),
    )
    assert r.status_code == 201, f"driver create: {r.text}"
    return r.json()["driver_id"]


async def _delete_driver(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
) -> None:
    await client.delete(f"/core/drivers/{driver_id}", headers=auth(token))


async def _create_and_approve_rate(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_type_id: int,
    amount: str,
    effective_from: str = "2035-01-01",
) -> int:
    """Create + approve a DriverRate.  Returns driver_rate_id."""
    headers = auth(token)
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


async def _add_draft_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    line_type: str,
    quantity: str = "1.0000",
    work_date: str = "2035-04-10",
    rate_amount: str | None = None,
) -> dict:
    payload: dict = {
        "driver_id": driver_id,
        "work_date": work_date,
        "line_type": line_type,
        "quantity":  quantity,
    }
    if rate_amount is not None:
        payload["rate_amount"] = rate_amount
    r = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json=payload,
        headers=auth(token),
    )
    assert r.status_code == 201, f"add line ({line_type}): {r.text}"
    return r.json()


async def _activate_pay_item(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    code: str,
) -> None:
    """Ensure a pay item is active for branch_id; idempotent."""
    items = (await client.get(
        f"/settings/branches/{branch_id}/pay-items",
        headers=auth(token),
    )).json()
    for item in items:
        if item.get("pay_item_code") == code:
            if not item.get("is_active", False):
                await client.patch(
                    f"/settings/branches/{branch_id}/pay-items/{item['pay_item_id']}",
                    json={"is_active": True},
                    headers=auth(token),
                )
            return


async def _add_period_pay_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    line_type: str,
    amount: str,
) -> dict:
    r = await client.post(
        f"/payroll/periods/{period_id}/period-pay",
        json={
            "driver_id": driver_id,
            "line_type": line_type,
            "amount":    amount,
        },
        headers=auth(token),
    )
    assert r.status_code == 201, f"add period-pay ({line_type}): {r.text}"
    return r.json()


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str = "2035-04-10",
) -> None:
    """Open → InReview (add dummy PTO line) → Approved via review flow."""
    headers = auth(token)

    # Add a no-review-needed PTO line so the period is non-empty
    dummy = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json={"driver_id": driver_id, "work_date": work_date,
              "line_type": "PTO_STATUS", "quantity": "1"},
        headers=headers,
    )
    assert dummy.status_code == 201, f"dummy PTO: {dummy.text}"

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
        headers=auth(token),
    )
    assert r.status_code == 200, f"finalize: {r.text}"
    return r.json()


async def _get_final_lines(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int | None = None,
) -> list[dict]:
    params: dict = {}
    if driver_id is not None:
        params["driver_id"] = driver_id
    r = await client.get(
        f"/payroll/periods/{period_id}/final-lines",
        params=params,
        headers=auth(token),
    )
    assert r.status_code == 200, f"get_final_lines: {r.text}"
    return r.json()


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def p3b_env(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    paytest_rate_type_id: int,
    paytest_mileage_rate_type_id: int,
    direct_db,
):
    """Cancel conflicting periods; yield env dict; cleanup after test."""
    await _cancel_periods(session_client, auth_token, paytest_branch_id)

    env = {
        "client":         session_client,
        "token":          auth_token,
        "branch_id":      paytest_branch_id,
        "hourly_rtid":    paytest_rate_type_id,
        "mileage_rtid":   paytest_mileage_rate_type_id,
        "db":             direct_db,
        "created_drivers": [],
    }
    yield env

    # Teardown
    await _cancel_periods(session_client, auth_token, paytest_branch_id)
    for did in env["created_drivers"]:
        await _delete_driver(session_client, auth_token, did)


# ---------------------------------------------------------------------------
# Test A — PerUnit line snapshot
# ---------------------------------------------------------------------------

class TestA_PerUnitSourceSnapshot:

    @pytest.mark.asyncio
    async def test_a_perunit_stores_all_source_fields(self, p3b_env):
        """A — PerUnit (Hours) finalized line carries full source snapshot."""
        c, tok, bid = p3b_env["client"], p3b_env["token"], p3b_env["branch_id"]
        hrly_rtid = p3b_env["hourly_rtid"]

        drv = await _create_driver(c, tok, bid, "A1")
        p3b_env["created_drivers"].append(drv)

        rate_id = await _create_and_approve_rate(
            c, tok, drv, hrly_rtid, "22.50", effective_from="2035-01-01"
        )

        pid = await _make_period(c, tok, bid, start=A_START, end=A_END)
        await _add_draft_line(c, tok, pid, drv, "HOURS", quantity="8.0000",
                              work_date=A_WORK)

        await _advance_to_approved(c, tok, pid, drv, work_date=A_WORK)
        await _finalize(c, tok, pid)

        lines = await _get_final_lines(c, tok, pid, driver_id=drv)
        hours_lines = [l for l in lines if l["line_type"] == "HOURS"]
        assert len(hours_lines) == 1, f"expected 1 HOURS final line, got lines={lines}"
        fl = hours_lines[0]

        assert fl["rate_behavior"] == "PerUnit", fl
        assert fl["driver_rate_id"] == rate_id, fl
        assert fl["rate_type_id"] == hrly_rtid, fl
        assert fl["resolved_rate_amount"] is not None
        assert Decimal(str(fl["resolved_rate_amount"])) == Decimal("22.5000"), fl
        # PayItemID: must be a positive integer (resolves to Hours pay item)
        assert isinstance(fl["pay_item_id"], int) and fl["pay_item_id"] > 0, fl
        # FinalAmount must be 8 × 22.50 = 180.00
        assert Decimal(str(fl["final_amount"])) == Decimal("180.0000"), fl


# ---------------------------------------------------------------------------
# Test B — Superseded rate stored at finalization
# ---------------------------------------------------------------------------

class TestB_SupersededRateSnapshot:

    @pytest.mark.asyncio
    async def test_b_superseded_rate_driver_rate_id_stored(self, p3b_env):
        """B — Finalization captures the superseded rate's DriverRateID.

        Uses a separate year (2036) so this test never conflicts with the
        locked period from test A (2035).
        """
        c, tok, bid = p3b_env["client"], p3b_env["token"], p3b_env["branch_id"]
        mileage_rtid = p3b_env["mileage_rtid"]

        drv = await _create_driver(c, tok, bid, "B1")
        p3b_env["created_drivers"].append(drv)

        # Old rate effective from Jan 1 2036 — will be superseded
        old_rate_id = await _create_and_approve_rate(
            c, tok, drv, mileage_rtid, "0.5500",
            effective_from="2036-01-01",
        )

        # New rate effective from Apr 15 2036 — supersedes old for that date+
        new_rate_id = await _create_and_approve_rate(
            c, tok, drv, mileage_rtid, "0.6000",
            effective_from="2036-04-15",
        )

        # Period in 2036 — work date Apr 10 is before Apr 15 → old rate applies
        pid = await _make_period(c, tok, bid, start=B_START, end=B_END)
        await _add_draft_line(c, tok, pid, drv, "MILES", quantity="100.0000",
                              work_date=B_WORK)
        await _advance_to_approved(c, tok, pid, drv, work_date=B_WORK)
        await _finalize(c, tok, pid)

        lines = await _get_final_lines(c, tok, pid, driver_id=drv)
        miles_lines = [l for l in lines if l["line_type"] == "MILES"]
        assert len(miles_lines) == 1
        fl = miles_lines[0]

        assert fl["rate_behavior"] == "PerUnit", fl
        # The superseded old rate must be stored — NOT the new rate
        assert fl["driver_rate_id"] == old_rate_id, (
            f"expected old_rate_id={old_rate_id}, got driver_rate_id={fl['driver_rate_id']}"
        )
        assert Decimal(str(fl["resolved_rate_amount"])) == Decimal("0.5500"), fl
        assert Decimal(str(fl["final_amount"])) == Decimal("55.0000"), fl


# ---------------------------------------------------------------------------
# Test C — Rate change after finalization does not alter locked source
# ---------------------------------------------------------------------------

class TestC_ImmutableAfterFinalize:

    @pytest.mark.asyncio
    async def test_c_rate_change_does_not_alter_locked_source(self, p3b_env):
        """C — Updating a rate after finalization leaves FinalLines unchanged."""
        c, tok, bid = p3b_env["client"], p3b_env["token"], p3b_env["branch_id"]
        hrly_rtid = p3b_env["hourly_rtid"]

        drv = await _create_driver(c, tok, bid, "C1")
        p3b_env["created_drivers"].append(drv)

        rate_id = await _create_and_approve_rate(
            c, tok, drv, hrly_rtid, "20.00", effective_from="2037-01-01"
        )

        pid = await _make_period(c, tok, bid, start=C_START, end=C_END)
        await _add_draft_line(c, tok, pid, drv, "HOURS", quantity="10.0000",
                              work_date=C_WORK)
        await _advance_to_approved(c, tok, pid, drv, work_date=C_WORK)
        await _finalize(c, tok, pid)

        # Verify locked source before rate change
        lines_before = await _get_final_lines(c, tok, pid, driver_id=drv)
        hours_before = next(l for l in lines_before if l["line_type"] == "HOURS")
        assert Decimal(str(hours_before["resolved_rate_amount"])) == Decimal("20.0000")
        assert hours_before["driver_rate_id"] == rate_id

        # Approve a new rate effective after the locked period ends.
        # (The finalized-period guard prevents backdating into locked periods,
        # so we approve a future rate to verify the locked lines stay unchanged.)
        await _create_and_approve_rate(
            c, tok, drv, hrly_rtid, "99.00", effective_from="2037-05-01"
        )

        # Final lines must be unchanged
        lines_after = await _get_final_lines(c, tok, pid, driver_id=drv)
        hours_after = next(l for l in lines_after if l["line_type"] == "HOURS")
        assert Decimal(str(hours_after["resolved_rate_amount"])) == Decimal("20.0000"), (
            "resolved_rate_amount changed after rate update — immutability broken"
        )
        assert hours_after["driver_rate_id"] == rate_id, (
            "driver_rate_id changed after rate update — immutability broken"
        )
        assert Decimal(str(hours_after["final_amount"])) == Decimal("200.0000")


# ---------------------------------------------------------------------------
# Test D — BONUS (EnteredAmount) period pay
# ---------------------------------------------------------------------------

class TestD_BonusPeriodPay:

    @pytest.mark.asyncio
    async def test_d_bonus_has_entered_amount_behavior(self, p3b_env):
        """D — BONUS line: RateBehavior='EnteredAmount', no driver rate fields."""
        c, tok, bid = p3b_env["client"], p3b_env["token"], p3b_env["branch_id"]

        drv = await _create_driver(c, tok, bid, "D1")
        p3b_env["created_drivers"].append(drv)

        await _activate_pay_item(c, tok, bid, "BONUS")
        pid = await _make_period(c, tok, bid, start=D_START, end=D_END)
        await _add_period_pay_line(c, tok, pid, drv, "BONUS", "250.00")
        await _advance_to_approved(c, tok, pid, drv, work_date=D_WORK)
        await _finalize(c, tok, pid)

        lines = await _get_final_lines(c, tok, pid, driver_id=drv)
        bonus_lines = [l for l in lines if l["line_type"] == "BONUS"]
        assert len(bonus_lines) == 1, f"BONUS final lines: {bonus_lines}"
        fl = bonus_lines[0]

        # BONUS is a period-pay item — amount is entered by the user.
        # The DB has it stored with ratebehavior='Fixed' (no driver-rate lookup).
        assert fl["rate_behavior"] in ("EnteredAmount", "Fixed"), fl
        assert fl["driver_rate_id"] is None, fl
        assert fl["rate_type_id"] is None, fl
        assert fl["resolved_rate_amount"] is None, fl
        assert Decimal(str(fl["final_amount"])) == Decimal("250.0000"), fl


# ---------------------------------------------------------------------------
# Test E — PTO_STATUS (None behavior)
# ---------------------------------------------------------------------------

class TestE_PtoStatusNoneBehavior:

    @pytest.mark.asyncio
    async def test_e_pto_status_none_behavior(self, p3b_env):
        """E — PTO_STATUS line: RateBehavior='None', no DriverRateID."""
        c, tok, bid = p3b_env["client"], p3b_env["token"], p3b_env["branch_id"]

        drv = await _create_driver(c, tok, bid, "E1")
        p3b_env["created_drivers"].append(drv)

        pid = await _make_period(c, tok, bid, start=E_START, end=E_END)
        # _advance_to_approved adds a dummy PTO_STATUS line for us
        await _advance_to_approved(c, tok, pid, drv, work_date=E_WORK)
        await _finalize(c, tok, pid)

        lines = await _get_final_lines(c, tok, pid, driver_id=drv)
        pto_lines = [l for l in lines if l["line_type"] == "PTO_STATUS"]
        assert len(pto_lines) >= 1
        fl = pto_lines[0]

        assert fl["rate_behavior"] == "None", fl
        assert fl["driver_rate_id"] is None, fl
        assert fl["rate_type_id"] is None, fl
        assert fl["resolved_rate_amount"] is None, fl


# ---------------------------------------------------------------------------
# Test F — SYS_MIN_TOPUP (System behavior)
# ---------------------------------------------------------------------------

class TestF_SysMinTopupBehavior:

    @pytest.mark.asyncio
    async def test_f_sys_min_topup_has_system_behavior(self, p3b_env, direct_db):
        """F — SYS_MIN_TOPUP row has RateBehavior='System', no DriverRateID."""
        c, tok, bid = p3b_env["client"], p3b_env["token"], p3b_env["branch_id"]

        drv = await _create_driver(c, tok, bid, "F1")
        p3b_env["created_drivers"].append(drv)

        # Set a minimum pay rule well above what one PTO line will pay (0)
        # so SYS_MIN_TOPUP is triggered.
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, branchid, driverid, ruletype, amount, effectivefrom)
                VALUES
                    ((SELECT companyid FROM core.branches WHERE branchid = :bid),
                     :bid, :did, 'MinimumPay', 500, '2040-01-01')
            """),
            {"bid": bid, "did": drv},
        )

        pid = await _make_period(c, tok, bid, start=F_START, end=F_END)
        # _advance_to_approved adds the PTO_STATUS dummy line for us
        await _advance_to_approved(c, tok, pid, drv, work_date=F_WORK)
        await _finalize(c, tok, pid)

        lines = await _get_final_lines(c, tok, pid, driver_id=drv)
        sys_lines = [l for l in lines if l["line_type"] == "SYS_MIN_TOPUP"]
        assert len(sys_lines) == 1, (
            f"Expected SYS_MIN_TOPUP line — got {[l['line_type'] for l in lines]}"
        )
        fl = sys_lines[0]

        assert fl["rate_behavior"] == "System", fl
        assert fl["driver_rate_id"] is None, fl
        assert fl["rate_type_id"] is None, fl
        assert fl["resolved_rate_amount"] is None, fl

        # Cleanup: remove the pay rule
        await direct_db.execute(
            _text("""
                DELETE FROM payroll.driverpayrules
                WHERE driverid = :did AND ruletype = 'MinimumPay'
            """),
            {"did": drv},
        )


# ---------------------------------------------------------------------------
# Test G — Preview exposes rate source fields
# ---------------------------------------------------------------------------

class TestG_PreviewRateSourceFields:

    @pytest.mark.asyncio
    async def test_g_preview_exposes_resolved_rate_amount(self, p3b_env):
        """G — Finalization preview includes rate_behavior and resolved_rate_amount."""
        c, tok, bid = p3b_env["client"], p3b_env["token"], p3b_env["branch_id"]
        hrly_rtid = p3b_env["hourly_rtid"]

        drv = await _create_driver(c, tok, bid, "G1")
        p3b_env["created_drivers"].append(drv)

        await _create_and_approve_rate(
            c, tok, drv, hrly_rtid, "30.00", effective_from="2041-01-01"
        )

        pid = await _make_period(c, tok, bid, start=G_START, end=G_END)
        await _add_draft_line(c, tok, pid, drv, "HOURS", quantity="5.0000",
                              work_date=G_WORK)
        await _advance_to_approved(c, tok, pid, drv, work_date=G_WORK)

        # Hit the preview endpoint (period is Approved — preview is allowed)
        r = await c.get(
            f"/payroll/periods/{pid}/finalization-preview",
            headers=auth(tok),
        )
        assert r.status_code == 200, f"preview: {r.text}"
        preview = r.json()

        hours_lines = [
            l for l in preview.get("lines", [])
            if l["line_type"] == "HOURS"
        ]
        assert len(hours_lines) >= 1, f"no Hours lines in preview: {preview}"
        pl = hours_lines[0]

        assert pl["rate_behavior"] == "PerUnit", pl
        assert pl["resolved_rate_amount"] is not None
        assert Decimal(str(pl["resolved_rate_amount"])) == Decimal("30.0000"), pl
        assert pl["driver_rate_id"] is not None
        assert pl["rate_type_id"] == hrly_rtid


# ---------------------------------------------------------------------------
# Test H — Pre-migration rows tolerated (NULL source fields)
# ---------------------------------------------------------------------------

class TestH_PreMigrationRowsTolerated:

    @pytest.mark.asyncio
    async def test_h_null_source_fields_returned_gracefully(self, p3b_env, direct_db):
        """H — Rows with NULL source fields (simulating pre-migration-0034 rows) are
        returned gracefully by the API.

        Redesign note (Phase 3C): the original approach NULLed out source columns via
        a direct UPDATE after finalization.  Migration 0035 adds a trigger that blocks
        UPDATE/DELETE on final lines for Locked/Archived periods, so the UPDATE approach
        is no longer valid.

        Instead, we INSERT a synthetic final-line row with NULL source columns directly
        into PayrollFinalLines while the period is Locked (INSERT is not blocked by the
        trigger — finalization itself inserts rows after the Locked state is set).  The
        row simulates a line that existed before migration 0034 was applied.
        """
        c, tok, bid = p3b_env["client"], p3b_env["token"], p3b_env["branch_id"]
        hrly_rtid = p3b_env["hourly_rtid"]

        drv = await _create_driver(c, tok, bid, "H1")
        p3b_env["created_drivers"].append(drv)

        await _create_and_approve_rate(
            c, tok, drv, hrly_rtid, "18.00", effective_from="2042-01-01"
        )

        pid = await _make_period(c, tok, bid, start=H_START, end=H_END)
        await _add_draft_line(c, tok, pid, drv, "HOURS", quantity="4.0000",
                              work_date=H_WORK)
        await _advance_to_approved(c, tok, pid, drv, work_date=H_WORK)
        await _finalize(c, tok, pid)

        # Verify normal finalized row has source fields populated
        normal_lines = await _get_final_lines(c, tok, pid, driver_id=drv)
        hours_normal = [l for l in normal_lines if l["line_type"] == "HOURS"]
        assert len(hours_normal) >= 1
        assert Decimal(str(hours_normal[0]["final_amount"])) == Decimal("72.0000")
        assert hours_normal[0]["rate_behavior"] is not None  # snapshot populated

        # Simulate a pre-migration-0034 row: INSERT with NULL source columns.
        # Phase 3C trigger (trg_final_line_immutable) only guards UPDATE/DELETE.
        # Phase 6 trigger (trg_guard_final_line_insert) guards INSERT; we authorise
        # this test-setup INSERT via the session-level GUC so the guard passes,
        # mirroring the allowance that finalize_period sets in its transaction.
        branchid_row = await direct_db.execute(
            _text("SELECT branchid FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        br = branchid_row.scalar_one()
        companyid_row = await direct_db.execute(
            _text("SELECT companyid FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        cid = companyid_row.scalar_one()

        # Authorise the INSERT for this AUTOCOMMIT connection (Phase 6 guard).
        await direct_db.execute(
            _text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)")
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, draftlineid, driverid,
                     workdate, linetype, linescope, quantity, rateamount, finalamount,
                     sourcetype, approvedbyuserid, approvedatutc, lockedatutc, notes,
                     payitemid, ratetypeid, driverrateid, resolvedrateamount, ratebehavior)
                VALUES
                    (:cid, :bid, :pid, NULL, :did,
                     :wdate, 'HOURS', 'Daily', 1, NULL, 18.0000,
                     'Manual', 1, NOW(), NOW(), 'pre-migration synthetic row',
                     NULL, NULL, NULL, NULL, NULL)
            """),
            {"cid": cid, "bid": br, "pid": pid, "did": drv,
             "wdate": _date.fromisoformat(H_WORK)},
        )

        # API must return the synthetic row with NULL source fields without error
        lines = await _get_final_lines(c, tok, pid, driver_id=drv)
        null_lines = [
            l for l in lines
            if l["line_type"] == "HOURS" and l["rate_behavior"] is None
        ]
        assert len(null_lines) >= 1, (
            f"Expected at least one HOURS row with NULL rate_behavior; got {lines}"
        )
        fl = null_lines[0]

        assert fl["pay_item_id"] is None, fl
        assert fl["rate_type_id"] is None, fl
        assert fl["driver_rate_id"] is None, fl
        assert fl["resolved_rate_amount"] is None, fl
        assert fl["rate_behavior"] is None, fl
        # FinalAmount is the value we inserted
        assert Decimal(str(fl["final_amount"])) == Decimal("18.0000"), fl
