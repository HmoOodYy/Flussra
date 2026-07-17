"""
CP-3C: Min/max formula correction — bonus excluded from min/max base.

Corrects the financial bug where finalize_period and get_finalization_preview
both summed bonus into the earned/gross figure BEFORE comparing against
Minimum/Maximum pay rules. The corrected order is:

    normal_base = normal_daily_pay + non_bonus_normal_period_pay
    minimum_adjustment = max(minimum - normal_base, 0)
    after_minimum = normal_base + minimum_adjustment
    maximum_adjustment = min(maximum - after_minimum, 0)
    normal_after_minmax = after_minimum + maximum_adjustment
    total_bonus = sum(active canonical PayrollBonusEvents)
    total_pay = normal_after_minmax + total_bonus

Covers both the persisted finalization path (finalize_period ->
PayrollFinalLines -> GET .../final-lines) and the read-only preview path
(get_finalization_preview), and asserts they agree under the corrected order.

Dates: 2092-* — isolated year (CP-3B2b uses 2093, CP-3B2a uses 2094,
CP-3B1 uses 2095, CP-3A uses 2096).

Run from backend/:
    python -B -m pytest tests/test_cp3c_minmax_bonus.py -v -p no:cacheprovider
"""
import datetime
import itertools
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_d = datetime.date(2092, 6, 1)
_BASE_MONDAY = _d + datetime.timedelta(days=(7 - _d.weekday()) % 7)
_CTR = itertools.count(0)
_RUN_ID = uuid.uuid4().hex[:8]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _week(offset: int = 0) -> tuple[datetime.date, datetime.date]:
    n = next(_CTR) + offset
    start = _BASE_MONDAY + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


async def _insert_period_db(
    db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    status: str = "Open",
) -> int:
    await db.execute(_text(
        "ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable"
    ))
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
    ))
    for child_table in (
        "payroll.payrollfinallines",
        "payroll.payrolldraftlines",
        "payroll.payrollbonusbatchrequests",
        "payroll.payrollbonusevents",
        "payroll.payrollperioddriverdayentrystate",
        "payroll.payrollperioddrivereligibility",
        "payroll.payrollperiodeligibilitysnapshots",
    ):
        await db.execute(
            _text(f"""
                DELETE FROM {child_table}
                WHERE payrollperiodid IN (
                    SELECT payrollperiodid FROM payroll.payrollperiods
                    WHERE branchid = :bid AND periodcode LIKE 'CP3C-%'
                )
            """),
            {"bid": branch_id},
        )
    await db.execute(
        _text("DELETE FROM payroll.payrollperiods WHERE branchid = :bid AND periodcode LIKE 'CP3C-%'"),
        {"bid": branch_id},
    )
    await db.execute(_text(
        "ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable"
    ))
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert"
    ))

    code = f"CP3C-{_RUN_ID}-{branch_id}-{start.isoformat()}"
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, :status, :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "status": status, "code": code,
         "name": f"CP3C {start}", "start": start, "end": end},
    )).mappings().first()
    await db.commit()
    assert r is not None
    return r["payrollperiodid"]


async def _cancel_period_db(db: AsyncConnection, period_id: int) -> None:
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
    ))
    await db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled', "
              "currentreturnreviewitemid = NULL WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert"
    ))
    await db.commit()


async def _advance_to_approved(
    client: httpx.AsyncClient, token: str, period_id: int,
) -> None:
    """Push period through Open -> InReview -> Approved via the review system."""
    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=_auth(token),
    )
    assert r.status_code == 200, f"Submit (InReview) failed: {r.text}"

    rv = await client.get("/review/items", headers=_auth(token))
    assert rv.status_code == 200, rv.text
    review_item = next(
        (
            it for it in rv.json()
            if it.get("entity_name") == "PayrollPeriods"
            and str(it.get("entity_id")) == str(period_id)
            and it.get("status") == "Pending"
        ),
        None,
    )
    assert review_item is not None, f"No review item found for period {period_id}"

    decide = await client.post(
        f"/review/items/{review_item['review_item_id']}/decide",
        headers=_auth(token),
        json={"decision": "Approved"},
    )
    assert decide.status_code == 200, f"Approval failed: {decide.text}"


async def _inject_adjustment_line(
    db: AsyncConnection, branch_id: int, period_id: int, driver_id: int, amount: str,
    linetype: str = "Adjustment",
) -> int:
    """Directly insert a non-BONUS Period DraftLine with a fixed calculated
    amount (bypasses branch pay-item activation) — the same technique CP-3A's
    own test_non_bonus_period_pay_unaffected uses to seed a specific normal-pay
    dollar figure without depending on rate resolution."""
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
    ))
    row = (await db.execute(
        _text("""
            INSERT INTO payroll.payrolldraftlines
                (companyid, branchid, payrollperiodid, driverid,
                 workdate, linetype, linescope, calculatedamount,
                 sourcetype, status, needsmanagerreview, addedbyuserid)
            VALUES
                (1, :bid, :pid, :did,
                 NULL, :linetype, 'Period', :amount,
                 'Manual', 'Active', FALSE, 1)
            RETURNING draftlineid
        """),
        {"bid": branch_id, "pid": period_id, "did": driver_id, "amount": amount, "linetype": linetype},
    )).mappings().first()
    await db.execute(_text(
        "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert"
    ))
    await db.commit()
    assert row is not None
    return row["draftlineid"]


async def _inject_status_payment_line(
    db: AsyncConnection, branch_id: int, period_id: int, driver_id: int, amount: str,
) -> int:
    return await _inject_adjustment_line(
        db, branch_id, period_id, driver_id, amount, linetype="STATUS_PAYMENT"
    )


async def _inject_legacy_bonus_draftline(
    db: AsyncConnection, branch_id: int, period_id: int, driver_id: int, amount: str,
) -> int:
    """Simulate a pre-CP-3A legacy BONUS DraftLine trace row — must never be
    counted as normal pay, bonus, or anything else in CP-3C's formula."""
    return await _inject_adjustment_line(
        db, branch_id, period_id, driver_id, amount, linetype="BONUS"
    )


async def _post_bonus(
    client: httpx.AsyncClient, token: str, period_id: int, driver_id: int, amount: str,
) -> int:
    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": driver_id, "amount": amount},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"Bonus create failed: {r.text}"
    return r.json()["bonus_event_id"]


async def _add_pay_rule(
    client: httpx.AsyncClient, token: str, driver_id: int, branch_id: int,
    rule_type: str, amount: str, start: datetime.date, end: datetime.date,
) -> int:
    r = await client.post(
        "/payroll/driver-pay-rules",
        json={
            "driver_id": driver_id,
            "branch_id": branch_id,
            "rule_type": rule_type,
            "amount": amount,
            "effective_from": start.isoformat(),
            "effective_to": end.isoformat(),
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"Pay rule creation failed: {r.text}"
    return r.json()["driver_pay_rule_id"]


async def _void_pay_rule(client: httpx.AsyncClient, token: str, rule_id: int) -> None:
    await client.post(
        f"/payroll/driver-pay-rules/{rule_id}/void",
        json={"reason": "CP-3C test cleanup"},
        headers=_auth(token),
    )


async def _get_preview(client: httpx.AsyncClient, token: str, period_id: int) -> httpx.Response:
    return await client.get(
        f"/payroll/periods/{period_id}/finalization-preview",
        headers=_auth(token),
    )


def _driver_row(preview: dict, driver_id: int) -> dict | None:
    return next((d for d in preview["driver_totals"] if d["driver_id"] == driver_id), None)


async def _finalize(client: httpx.AsyncClient, token: str, period_id: int) -> httpx.Response:
    return await client.post(
        f"/payroll/periods/{period_id}/finalize",
        headers=_auth(token),
    )


async def _get_final_lines(client: httpx.AsyncClient, token: str, period_id: int) -> list[dict]:
    r = await client.get(f"/payroll/periods/{period_id}/final-lines", headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()


async def _finalized_driver_total(final_lines: list[dict], driver_id: int) -> Decimal:
    return sum(
        (Decimal(str(fl["final_amount"])) for fl in final_lines if fl["driver_id"] == driver_id),
        Decimal("0"),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cp3c_branch_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    resp = await session_client.get("/core/branches", headers=_auth(auth_token))
    assert resp.status_code == 200, resp.text
    for branch in resp.json():
        if branch["branch_code"] == "PAYTEST":
            return branch["branch_id"]
    raise AssertionError("PAYTEST branch not found")


async def _get_or_create_driver(
    session_client: httpx.AsyncClient, auth_token: str, branch_id: int,
    driver_code: str, full_name: str,
) -> int:
    r_list = await session_client.get("/core/drivers", headers=_auth(auth_token))
    if r_list.status_code == 200:
        for d in r_list.json():
            if d.get("driver_code") == driver_code:
                return d["driver_id"]
    resp = await session_client.post(
        "/core/drivers",
        json={"branch_id": branch_id, "full_name": full_name, "driver_code": driver_code},
        headers=_auth(auth_token),
    )
    assert resp.status_code == 201, f"Driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture
async def cp3c_driver_id(
    session_client: httpx.AsyncClient, auth_token: str, cp3c_branch_id: int,
) -> int:
    return await _get_or_create_driver(
        session_client, auth_token, cp3c_branch_id,
        driver_code=f"CP3C-DRV-{_BASE_MONDAY.isoformat()}", full_name="CP3C MinMax Driver",
    )


@pytest_asyncio.fixture
async def cp3c_driver2_id(
    session_client: httpx.AsyncClient, auth_token: str, cp3c_branch_id: int,
) -> int:
    return await _get_or_create_driver(
        session_client, auth_token, cp3c_branch_id,
        driver_code=f"CP3C-DRV2-{_BASE_MONDAY.isoformat()}", full_name="CP3C MinMax Driver Two",
    )


# ===========================================================================
# 1/3. Minimum / Maximum without bonus — unchanged regression
# ===========================================================================

@pytest.mark.asyncio
async def test_minimum_without_bonus_unchanged(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "50.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3c_driver_id)
    assert Decimal(row["gross_pay"]) == Decimal("50.00")
    assert Decimal(row["sys_adjustment"]) == Decimal("150.00")
    assert Decimal(row["bonus_total"]) == Decimal("0")
    assert Decimal(row["final_pay"]) == Decimal("200.00")

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_maximum_without_bonus_unchanged(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "500.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MaximumPay", "300.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3c_driver_id)
    assert Decimal(row["gross_pay"]) == Decimal("500.00")
    assert Decimal(row["sys_adjustment"]) == Decimal("-200.00")
    assert Decimal(row["bonus_total"]) == Decimal("0")
    assert Decimal(row["final_pay"]) == Decimal("300.00")

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 2. Minimum with bonus — bonus does not reduce the top-up
# ===========================================================================

@pytest.mark.asyncio
async def test_minimum_with_bonus_does_not_reduce_topup(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "50.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "40.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    preview = r.json()
    row = _driver_row(preview, cp3c_driver_id)
    assert Decimal(row["gross_pay"]) == Decimal("50.00"), "normal_base must exclude bonus"
    assert Decimal(row["sys_adjustment"]) == Decimal("150.00"), (
        "minimum top-up must be computed from normal pay only (200 - 50), "
        "not reduced by the 40.00 bonus"
    )
    assert Decimal(row["bonus_total"]) == Decimal("40.00")
    assert Decimal(row["final_pay"]) == Decimal("240.00"), "total = minimum (200) + bonus (40)"

    # P1 fix: FinalizationPreviewSysAdjustment must report the driver's true
    # total, not a bonus-free intermediate — direct assertions on the
    # sys_adjustments[] entry itself, not just driver_totals[].
    sys_adj = [a for a in preview["sys_adjustments"] if a["driver_id"] == cp3c_driver_id]
    assert len(sys_adj) == 1
    adj = sys_adj[0]
    assert adj["adjustment_type"] == "SYS_MIN_TOPUP"
    assert Decimal(adj["gross_before"]) == Decimal("50.00"), "gross_before must exclude bonus"
    assert Decimal(adj["adjustment_amount"]) == Decimal("150.00"), (
        "adjustment_amount must be based on normal pay only (200 - 50)"
    )
    assert Decimal(adj["bonus_total"]) == Decimal("40.00")
    assert Decimal(adj["final_pay"]) == Decimal("240.00"), "sys_adjustment final_pay must be the true total"
    assert Decimal(adj["final_pay"]) == Decimal(row["final_pay"]), (
        "sys_adjustments[].final_pay must agree with driver_totals[].final_pay"
    )

    fin = await _finalize(client, auth_token, period_id)
    assert fin.status_code == 200, fin.text
    final_lines = await _get_final_lines(client, auth_token, period_id)
    finalized_total = await _finalized_driver_total(final_lines, cp3c_driver_id)
    assert finalized_total == Decimal(adj["final_pay"]), (
        "sys_adjustments[].final_pay must agree with the finalized ledger sum"
    )
    assert finalized_total == Decimal("240.00")

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 4. Maximum with bonus — bonus does not increase/trigger the cap
# ===========================================================================

@pytest.mark.asyncio
async def test_maximum_with_bonus_added_after_cap(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "500.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "100.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MaximumPay", "300.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    preview = r.json()
    row = _driver_row(preview, cp3c_driver_id)
    assert Decimal(row["gross_pay"]) == Decimal("500.00"), "normal_base must exclude bonus"
    assert Decimal(row["sys_adjustment"]) == Decimal("-200.00"), (
        "cap must be computed from normal pay only (300 - 500), "
        "not enlarged by the 100.00 bonus"
    )
    assert Decimal(row["bonus_total"]) == Decimal("100.00")
    assert Decimal(row["final_pay"]) == Decimal("400.00"), "total = maximum (300) + bonus (100)"

    # P1 fix: direct assertions on the sys_adjustments[] entry itself.
    sys_adj = [a for a in preview["sys_adjustments"] if a["driver_id"] == cp3c_driver_id]
    assert len(sys_adj) == 1
    adj = sys_adj[0]
    assert adj["adjustment_type"] == "SYS_MAX_CAP"
    assert Decimal(adj["gross_before"]) == Decimal("500.00"), "gross_before must exclude bonus"
    assert Decimal(adj["adjustment_amount"]) == Decimal("-200.00"), (
        "cap adjustment_amount must be based on normal pay only (300 - 500)"
    )
    assert Decimal(adj["bonus_total"]) == Decimal("100.00")
    assert Decimal(adj["final_pay"]) == Decimal("400.00"), "sys_adjustment final_pay must be the true total"
    assert Decimal(adj["final_pay"]) == Decimal(row["final_pay"]), (
        "sys_adjustments[].final_pay must agree with driver_totals[].final_pay"
    )

    fin = await _finalize(client, auth_token, period_id)
    assert fin.status_code == 200, fin.text
    final_lines = await _get_final_lines(client, auth_token, period_id)
    finalized_total = await _finalized_driver_total(final_lines, cp3c_driver_id)
    assert finalized_total == Decimal(adj["final_pay"]), (
        "sys_adjustments[].final_pay must agree with the finalized ledger sum"
    )
    assert finalized_total == Decimal("400.00")

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_bonus_alone_does_not_trigger_maximum_cap(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    """Normal pay alone is below the maximum; bonus alone would push the OLD
    (buggy) combined total over it. The cap must NOT trigger under CP-3C."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "250.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "100.00")  # 250+100=350 > 300 (old bug)
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MaximumPay", "300.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3c_driver_id)
    assert Decimal(row["sys_adjustment"]) == Decimal("0"), (
        "normal pay (250) is below max (300); bonus must not trigger a cap "
        "that normal pay alone would not"
    )
    assert Decimal(row["final_pay"]) == Decimal("350.00"), "total = normal (250) + bonus (100), uncapped"

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 5. Normal between min/max with bonus — no adjustment
# ===========================================================================

@pytest.mark.asyncio
async def test_normal_between_min_max_with_bonus_no_adjustment(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "250.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "30.00")
    min_rule = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )
    max_rule = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MaximumPay", "400.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3c_driver_id)
    assert Decimal(row["sys_adjustment"]) == Decimal("0")
    assert Decimal(row["final_pay"]) == Decimal("280.00"), "total = normal (250) + bonus (30)"

    await _void_pay_rule(client, auth_token, min_rule)
    await _void_pay_rule(client, auth_token, max_rule)
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 6. Bonus-only driver — not dropped from min/max processing
# ===========================================================================

@pytest.mark.asyncio
async def test_bonus_only_driver_gets_minimum_from_zero_base(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    """A driver with ONLY a bonus event (no normal pay lines at all) must
    still be processed for min/max, with normal_base = 0 — not silently
    dropped from the aggregation (the WHERE-filter bug this fix avoids)."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "75.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3c_driver_id)
    assert row is not None, "bonus-only driver must still appear in driver_totals"
    assert Decimal(row["gross_pay"]) == Decimal("0"), "normal_base must be 0, not missing"
    assert Decimal(row["sys_adjustment"]) == Decimal("200.00"), "minimum computed from 0 base"
    assert Decimal(row["bonus_total"]) == Decimal("75.00")
    assert Decimal(row["final_pay"]) == Decimal("275.00"), "total = minimum (200) + bonus (75)"

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 7. Multiple bonus events — active summed, voided excluded
# ===========================================================================

@pytest.mark.asyncio
async def test_multiple_bonus_events_active_summed_voided_excluded(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "100.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "20.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "30.00")
    voided_id = await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "999.00")
    void_resp = await client.delete(
        f"/payroll/periods/{period_id}/bonuses/{voided_id}", headers=_auth(auth_token)
    )
    assert void_resp.status_code == 200, void_resp.text

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3c_driver_id)
    assert Decimal(row["bonus_total"]) == Decimal("50.00"), "only Active events (20+30) summed"
    assert Decimal(row["final_pay"]) == Decimal("150.00")

    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 8. STATUS_PAYMENT — remains in normal/minmax base
# ===========================================================================

@pytest.mark.asyncio
async def test_status_payment_remains_in_minmax_base(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_status_payment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "180.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "25.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3c_driver_id)
    assert Decimal(row["gross_pay"]) == Decimal("180.00"), "STATUS_PAYMENT counts as normal pay"
    assert Decimal(row["sys_adjustment"]) == Decimal("20.00"), "min top-up = 200 - 180 (STATUS_PAYMENT counted)"
    assert Decimal(row["final_pay"]) == Decimal("225.00"), "total = minimum (200) + bonus (25)"

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 9. Non-BONUS period pay (ADJUSTMENT) — remains in normal/minmax base
# ===========================================================================

@pytest.mark.asyncio
async def test_adjustment_remains_in_minmax_base(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "150.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3c_driver_id)
    assert Decimal(row["gross_pay"]) == Decimal("150.00")
    assert Decimal(row["sys_adjustment"]) == Decimal("50.00")

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 10. Legacy BONUS DraftLines — ignored; canonical PayrollBonusEvents only
# ===========================================================================

@pytest.mark.asyncio
async def test_legacy_bonus_draftline_ignored_in_minmax(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "100.00")
    # Legacy BONUS DraftLine trace row — must not count as normal pay OR bonus.
    await _inject_legacy_bonus_draftline(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "500.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "20.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3c_driver_id)
    assert Decimal(row["gross_pay"]) == Decimal("100.00"), "legacy BONUS DraftLine must not count as normal pay"
    assert Decimal(row["bonus_total"]) == Decimal("20.00"), "legacy BONUS DraftLine must not count as bonus either"
    assert Decimal(row["sys_adjustment"]) == Decimal("100.00")
    assert Decimal(row["final_pay"]) == Decimal("220.00")

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 11. Preview / finalization parity
# ===========================================================================

@pytest.mark.asyncio
async def test_preview_finalization_parity_minimum(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "50.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "40.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)

    preview = await _get_preview(client, auth_token, period_id)
    assert preview.status_code == 200, preview.text
    preview_row = _driver_row(preview.json(), cp3c_driver_id)
    preview_final = Decimal(preview_row["final_pay"])
    assert preview_final == Decimal("240.00")

    fin = await _finalize(client, auth_token, period_id)
    assert fin.status_code == 200, fin.text

    final_lines = await _get_final_lines(client, auth_token, period_id)
    finalized_total = await _finalized_driver_total(final_lines, cp3c_driver_id)
    assert finalized_total == preview_final, (
        f"Preview final_pay ({preview_final}) must match the finalized ledger's "
        f"per-driver sum ({finalized_total}) under the corrected order"
    )
    assert finalized_total == Decimal("240.00")

    # The BONUS final line itself must still be present, unadjusted.
    bonus_lines = [fl for fl in final_lines if fl["driver_id"] == cp3c_driver_id and fl["line_type"] == "BONUS"]
    assert len(bonus_lines) == 1
    assert Decimal(str(bonus_lines[0]["final_amount"])) == Decimal("40.00")

    # And the SYS_MIN_TOPUP final line must reflect normal pay only (200 - 50 = 150).
    topup_lines = [
        fl for fl in final_lines
        if fl["driver_id"] == cp3c_driver_id and fl["line_type"] == "SYS_MIN_TOPUP"
    ]
    assert len(topup_lines) == 1
    assert Decimal(str(topup_lines[0]["final_amount"])) == Decimal("150.00")

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_preview_finalization_parity_maximum(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "500.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "100.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MaximumPay", "300.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)

    preview = await _get_preview(client, auth_token, period_id)
    assert preview.status_code == 200, preview.text
    preview_row = _driver_row(preview.json(), cp3c_driver_id)
    preview_final = Decimal(preview_row["final_pay"])
    assert preview_final == Decimal("400.00")

    fin = await _finalize(client, auth_token, period_id)
    assert fin.status_code == 200, fin.text

    final_lines = await _get_final_lines(client, auth_token, period_id)
    finalized_total = await _finalized_driver_total(final_lines, cp3c_driver_id)
    assert finalized_total == preview_final == Decimal("400.00")

    cap_lines = [
        fl for fl in final_lines
        if fl["driver_id"] == cp3c_driver_id and fl["line_type"] == "SYS_MAX_CAP"
    ]
    assert len(cap_lines) == 1
    assert Decimal(str(cap_lines[0]["final_amount"])) == Decimal("-200.00"), "cap based on normal pay only"

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


@pytest.mark.asyncio
async def test_finalization_bonus_only_driver_gets_minimum(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    """Finalization-side confirmation of test 6: a bonus-only driver still
    receives a SYS_MIN_TOPUP based on normal_base=0, not omitted."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "75.00")
    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    fin = await _finalize(client, auth_token, period_id)
    assert fin.status_code == 200, fin.text

    final_lines = await _get_final_lines(client, auth_token, period_id)
    topup_lines = [
        fl for fl in final_lines
        if fl["driver_id"] == cp3c_driver_id and fl["line_type"] == "SYS_MIN_TOPUP"
    ]
    assert len(topup_lines) == 1, "bonus-only driver must still receive a min top-up final line"
    assert Decimal(str(topup_lines[0]["final_amount"])) == Decimal("200.00"), "topup computed from 0 base"
    total = await _finalized_driver_total(final_lines, cp3c_driver_id)
    assert total == Decimal("275.00"), "total = minimum (200) + bonus (75)"

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 12. CP-3B2b regression — batch-created bonus follows the same formula
# ===========================================================================

@pytest.mark.asyncio
async def test_batch_created_bonus_follows_corrected_formula(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "50.00")

    batch_resp = await client.post(
        f"/payroll/periods/{period_id}/bonuses/batch",
        json={
            "idempotency_key": f"cp3c-{uuid.uuid4().hex}",
            "expected_bonus_data_revision": 0,
            "items": [{"driver_id": cp3c_driver_id, "amount": "40.00"}],
        },
        headers=_auth(auth_token),
    )
    assert batch_resp.status_code == 201, batch_resp.text

    rule_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row = _driver_row(r.json(), cp3c_driver_id)
    assert Decimal(row["gross_pay"]) == Decimal("50.00")
    assert Decimal(row["sys_adjustment"]) == Decimal("150.00"), (
        "batch-created bonus must not reduce the minimum top-up, "
        "identical to a single-event bonus"
    )
    assert Decimal(row["bonus_total"]) == Decimal("40.00")
    assert Decimal(row["final_pay"]) == Decimal("240.00")

    await _void_pay_rule(client, auth_token, rule_id)
    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 13. Draft/Prepared — no financial exposure change
# ===========================================================================

@pytest.mark.asyncio
async def test_draft_period_still_has_no_finalization_preview(
    client, auth_token, db_conn, cp3c_branch_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end, status="Draft")

    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 422, r.text

    await _cancel_period_db(db_conn, period_id)


# ===========================================================================
# 15 (partial, targeted). Two-driver isolation — one driver's bonus does not
# affect another driver's min/max in the same period.
# ===========================================================================

@pytest.mark.asyncio
async def test_two_drivers_bonus_isolated_per_driver(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id, cp3c_driver2_id,
) -> None:
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "50.00")
    await _inject_adjustment_line(db_conn, cp3c_branch_id, period_id, cp3c_driver2_id, "50.00")
    await _post_bonus(client, auth_token, period_id, cp3c_driver_id, "500.00")
    rule1 = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )
    rule2 = await _add_pay_rule(
        client, auth_token, cp3c_driver2_id, cp3c_branch_id, "MinimumPay", "200.00", start, end
    )

    await _advance_to_approved(client, auth_token, period_id)
    r = await _get_preview(client, auth_token, period_id)
    assert r.status_code == 200, r.text
    row1 = _driver_row(r.json(), cp3c_driver_id)
    row2 = _driver_row(r.json(), cp3c_driver2_id)
    assert Decimal(row1["sys_adjustment"]) == Decimal("150.00")
    assert Decimal(row2["sys_adjustment"]) == Decimal("150.00"), (
        "driver2's minimum top-up must be unaffected by driver1's large bonus"
    )

    await _void_pay_rule(client, auth_token, rule1)
    await _void_pay_rule(client, auth_token, rule2)
    await _cancel_period_db(db_conn, period_id)
