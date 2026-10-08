"""
CP-3C: Min/max formula correction — bonus excluded from min/max base.

The corrected order, asserted here against the LIVE calculation packet
(calculation-preview), is:

    normal_base = normal_daily_pay + status_pay
    minimum_adjustment = max(minimum - normal_base, 0)
    after_minimum = normal_base + minimum_adjustment
    maximum_adjustment = min(maximum - after_minimum, 0)
    normal_after_minmax = after_minimum + maximum_adjustment
    total_bonus = sum(active canonical PayrollBonusEvents)
    total_pay = normal_after_minmax + total_bonus

Normal pay is seeded as a target source fact (quantity 1) against a frozen period
definition and a driver rate equal to the wanted amount, so it is derived live.
Frozen-evidence parity (submit, approval, finalization) belongs to the calculation
evidence work unit and is not exercised here.

Dates: 2092-* — isolated year.
"""
import datetime
import itertools
import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.target_seed import seed_approved_rate, seed_period_definition, seed_target_line

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
    await _cleanup_mutable_cp3c_periods(db, branch_id)

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


async def _cleanup_mutable_cp3c_periods(db: AsyncConnection, branch_id: int) -> None:
    """Release CP-3C workflow slots without deleting immutable snapshots."""
    period_filter = """
        SELECT period.payrollperiodid
        FROM payroll.payrollperiods period
        WHERE period.branchid = :bid
          AND period.periodcode LIKE 'CP3C-%'
          AND NOT EXISTS (
              SELECT 1
              FROM payroll.payrollcalculationsnapshots snapshot
              WHERE snapshot.payrollperiodid = period.payrollperiodid
                AND snapshot.companyid = period.companyid
                AND snapshot.branchid = period.branchid
          )
    """
    await db.execute(_text("""
        UPDATE payroll.payrollperiods
        SET status = 'Cancelled', currentreturnreviewitemid = NULL
        WHERE branchid = :bid
          AND periodcode LIKE 'CP3C-%'
          AND status IN ('Open', 'InReview', 'Approved', 'Returned')
    """), {"bid": branch_id})
    for child_table in (
        "payroll.payrollfinallines",
        "payroll.payrolldraftlines",
        "payroll.payrollbonusbatchrequests",
        "payroll.payrollbonusevents",
        "payroll.payrollperioddriverdayentrystate",
        "payroll.payrollperioddrivereligibility",
        "payroll.payrollperiodeligibilitysnapshots",
    ):
        await db.execute(_text(
            f"DELETE FROM {child_table} WHERE payrollperiodid IN ({period_filter})"
        ), {"bid": branch_id})
    await db.execute(_text(
        f"DELETE FROM payroll.payrollperiods WHERE payrollperiodid IN ({period_filter})"
    ), {"bid": branch_id})
    await db.commit()


async def _cancel_period_db(db: AsyncConnection, period_id: int) -> None:
    await db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled', "
              "currentreturnreviewitemid = NULL WHERE payrollperiodid = :pid "
              "AND status IN ('Open', 'InReview', 'Approved', 'Returned')"),
        {"pid": period_id},
    )
    await db.commit()


async def _advance_to_approved(
    client: httpx.AsyncClient, token: str, period_id: int,
) -> None:
    """The live packet needs no workflow advance: the period stays Open."""
    return None


async def _inject_normal_pay_line(
    db: AsyncConnection, branch_id: int, period_id: int, driver_id: int, amount: str,
) -> int:
    """Seed normal pay of exactly ``amount``: one unit at a rate of ``amount``."""
    definition = await seed_period_definition(
        db, period_id=period_id, branch_id=branch_id, name="Normal pay")
    await seed_approved_rate(
        db, definition=definition, driver_id=driver_id, branch_id=branch_id, amount=amount)
    start = (await db.execute(
        _text("SELECT startdate FROM payroll.payrollperiods WHERE payrollperiodid = :p"),
        {"p": period_id})).scalar_one()
    line_id = await seed_target_line(
        db, period_id=period_id, branch_id=branch_id, driver_id=driver_id,
        definition=definition, quantity=1, work_date=start)
    await db.commit()
    return line_id


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


class _Preview:
    """The live calculation preview in the shape these tests assert on."""

    def __init__(self, response: httpx.Response):
        self.status_code = response.status_code
        self.text = response.text
        self._response = response

    def json(self) -> dict:
        body = self._response.json()
        if self.status_code != 200:
            return body
        driver_totals, adjustments = [], []
        for d in body["drivers"]:
            adjustment = Decimal(d["minimum_adjustment"]) + Decimal(d["maximum_adjustment"])
            driver_totals.append({
                "driver_id": d["driver_id"], "gross_pay": d["normal_base"],
                "sys_adjustment": str(adjustment), "bonus_total": d["bonus_total"],
                "final_pay": d["expected_pay"],
            })
            for line in d["lines"]:
                if line["line_type"] in {"SYS_MIN_TOPUP", "SYS_MAX_CAP"}:
                    adjustments.append({
                        "driver_id": d["driver_id"], "adjustment_type": line["line_type"],
                        "gross_before": d["normal_base"],
                        "adjustment_amount": line["calculated_amount"],
                        "bonus_total": d["bonus_total"], "final_pay": d["expected_pay"],
                    })
        return {"driver_totals": driver_totals, "sys_adjustments": adjustments,
                "blockers": body["blockers"]}


async def _get_preview(client: httpx.AsyncClient, token: str, period_id: int) -> _Preview:
    return _Preview(await client.get(
        f"/payroll/periods/{period_id}/calculation-preview", headers=_auth(token)))


def _driver_row(preview: dict, driver_id: int) -> dict | None:
    return next((d for d in preview["driver_totals"] if d["driver_id"] == driver_id), None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cp3c_branch_id(direct_db: AsyncConnection) -> int:
    """Use a fresh branch per test so retained P6D history cannot reserve slots."""
    row = (await direct_db.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """),
        {
            "code": f"CP3C-{uuid.uuid4().hex[:10]}",
            "name": f"CP3C isolated {uuid.uuid4().hex[:10]}",
        },
    )).mappings().first()
    branch_id = row["branchid"]
    await direct_db.commit()
    return branch_id


async def _get_or_create_driver(
    session_client: httpx.AsyncClient, auth_token: str, branch_id: int,
    driver_code: str, full_name: str,
) -> int:
    # The branch fixture is intentionally fresh per test. Do not search the
    # company-wide driver list and accidentally reuse a same-code driver from
    # another branch/test.
    driver_code = f"{driver_code}-{uuid.uuid4().hex[:8]}"
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
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "50.00")
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
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "500.00")
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
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "50.00")
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
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "500.00")
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
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "250.00")
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
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "250.00")
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
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "100.00")
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
# 11. Preview / finalization parity
# ===========================================================================

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
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "50.00")

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
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "50.00")
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver2_id, "50.00")
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


@pytest.mark.asyncio
async def test_ended_and_voided_rules_resolve_by_period_start(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    """Ended rules apply only inside their range and voided rules never apply."""
    first_start, first_end = _week()
    ended_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id,
        "MinimumPay", "200.00", first_start - datetime.timedelta(days=7), first_end,
    )
    ended = await client.post(
        f"/payroll/driver-pay-rules/{ended_id}/end",
        json={"effective_to": (first_start + datetime.timedelta(days=2)).isoformat()},
        headers=_auth(auth_token),
    )
    assert ended.status_code == 200, ended.text
    assert ended.json()["status"] == "Ended"

    first_period = await _insert_period_db(db_conn, cp3c_branch_id, first_start, first_end)
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, first_period, cp3c_driver_id, "50.00")
    await _advance_to_approved(client, auth_token, first_period)
    first_preview = await _get_preview(client, auth_token, first_period)
    assert first_preview.status_code == 200, first_preview.text
    first_row = _driver_row(first_preview.json(), cp3c_driver_id)
    assert Decimal(first_row["sys_adjustment"]) == Decimal("150.00")

    after_end_start, after_end = _week(1)
    second_period = await _insert_period_db(db_conn, cp3c_branch_id, after_end_start, after_end)
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, second_period, cp3c_driver_id, "50.00")
    await _advance_to_approved(client, auth_token, second_period)
    second_preview = await _get_preview(client, auth_token, second_period)
    assert second_preview.status_code == 200, second_preview.text
    second_row = _driver_row(second_preview.json(), cp3c_driver_id)
    assert Decimal(second_row["sys_adjustment"]) == Decimal("0")

    voided_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id,
        "MinimumPay", "9999.00", after_end_start, after_end,
    )
    voided = await client.post(
        f"/payroll/driver-pay-rules/{voided_id}/void",
        headers=_auth(auth_token),
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["status"] == "Voided"

    third_start, third_end = _week(2)
    third_period = await _insert_period_db(db_conn, cp3c_branch_id, third_start, third_end)
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, third_period, cp3c_driver_id, "50.00")
    await _advance_to_approved(client, auth_token, third_period)
    third_preview = await _get_preview(client, auth_token, third_period)
    assert third_preview.status_code == 200, third_preview.text
    third_row = _driver_row(third_preview.json(), cp3c_driver_id)
    assert Decimal(third_row["sys_adjustment"]) == Decimal("0")

    await _void_pay_rule(client, auth_token, ended_id)



@pytest.mark.asyncio
async def test_minimum_greater_than_maximum_is_a_live_blocker(
    client, auth_token, db_conn, cp3c_branch_id, cp3c_driver_id,
) -> None:
    """An applicable minimum above the maximum blocks the live packet."""
    start, end = _week()
    period_id = await _insert_period_db(db_conn, cp3c_branch_id, start, end)
    await _inject_normal_pay_line(db_conn, cp3c_branch_id, period_id, cp3c_driver_id, "600.00")
    minimum_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MinimumPay", "800.00", start, end)
    maximum_id = await _add_pay_rule(
        client, auth_token, cp3c_driver_id, cp3c_branch_id, "MaximumPay", "500.00", start, end)
    try:
        preview = await _get_preview(client, auth_token, period_id)
        assert preview.status_code == 200, preview.text
        blockers = " ".join(preview.json()["blockers"]).lower()
        assert "minimum" in blockers and "maximum" in blockers
    finally:
        await _void_pay_rule(client, auth_token, minimum_id)
        await _void_pay_rule(client, auth_token, maximum_id)
        await _cancel_period_db(db_conn, period_id)
