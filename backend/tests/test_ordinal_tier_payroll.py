"""Live target payroll with an OrdinalTier definition.

OrdinalTier plugs into the existing generic path: period definitions freeze its method, the
WorkDate selects one whole Driver schedule through the one resolver, the method-owned boundary
calculates the line, and aggregation and payable rounding stay the packet's job.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.calculation_snapshot_helpers import (
    add_bonus,
    add_rule,
    build_packet,
    day,
    enter,
    open_tier_period,
    submit,
)
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.db_state import TriggerIdentity, suspended_test_triggers
from tests.payroll_runtime_fixtures import (
    approve_tier_rate,
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    get_grid,
    period_definitions,
    rated_definition,
    rated_ordinal_definition,
    setup_anchor,
)

pytestmark = pytest.mark.asyncio
D = Decimal

PERIOD_DEFINITION_IMMUTABLE = TriggerIdentity(
    "payroll", "payrollperioddefinitions", "trg_payrollperioddefinitions_immutable"
)


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _cell(grid: dict, ppd: int, assignment_id: int) -> dict:
    row = next(r for r in grid["rows"] if r["driver_assignment_id"] == assignment_id)
    return row["values"][str(ppd)]


async def _save(client, tenant, period, values: dict, *, offset=0):
    return await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": day(period, offset),
              "rows": [{"driver_assignment_id": tenant.driver_a, "values": values}]},
        headers=tenant.admin)


def _driver(packet, assignment_id: int):
    return next(d for d in packet.drivers if d.driver_assignment_id == assignment_id)


# ---------------------------------------------------------------------------
# The canonical example
# ---------------------------------------------------------------------------

async def test_four_ordinal_units_pay_three_plus_four_plus_five_plus_five(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    saved = await _save(p3c_client, tenant, period, {str(ppd): "4"})
    assert saved.status_code == 200, saved.text
    grid = saved.json()
    cell = _cell(grid, ppd, tenant.driver_a)
    assert D(cell["calculated_amount"]) == D("17")
    assert cell["calculation_status"] == "Calculated" and cell["needs_manager_review"] is False
    assert D(grid["summary"]["gross_total"]) == D("17")

    packet = await build_packet(p3c_engine, tenant, period)
    assert packet.blockers == []
    driver = _driver(packet, tenant.driver_a)
    assert (driver.daily_pay, driver.expected_pay) == (D("17.00"), D("17.00"))
    (component,) = [c for c in driver.components if c.kind == "ORDINARY"]
    assert (component.calculation_method, component.calculation_method_version) == ("OrdinalTier", 1)
    assert (component.pre_rounding_amount, component.official_amount) == (D("17"), D("17.00"))
    (line,) = [ln for ln in driver.lines if ln.source_type == "DraftLine"]
    assert (line.quantity, line.calculated_amount) == (D("4"), D("17"))
    assert line.calculation_status == "Calculated" and line.calculation_version == "ordinal-tier-v1"
    # A tiered line names no single rate: it carries every tier value and its expansion.
    assert line.resolved_rate_amount is None and line.rate_component_definition_id is None
    assert [(v.ordinal_from, v.ordinal_to, v.amount) for v in line.rate_values] == [
        (1, 1, D("3")), (2, 2, D("4")), (3, None, D("5"))]
    assert [(c.units, c.amount) for c in line.tier_contributions] == [
        (1, D("3")), (1, D("4")), (2, D("10"))]


@pytest.mark.parametrize(("quantity", "expected"), [("1", "3"), ("2", "7"), ("3", "12"), ("9", "42")])
async def test_each_entered_quantity_pays_the_tiers_it_reaches(
    p3c_client, p3c_engine, tenant, quantity, expected,
):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    saved = await _save(p3c_client, tenant, period, {str(ppd): quantity})
    assert D(_cell(saved.json(), ppd, tenant.driver_a)["calculated_amount"]) == D(expected)


async def test_the_ordinals_restart_for_every_source_line(p3c_client, p3c_engine, tenant):
    """The calculation boundary receives one line at a time: four stops on one day and two on
    another earn 17 + 7, not the continuation of one running count."""
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="4")
    await enter(p3c_client, tenant, period, 2, ppd=ppd, quantity="2")
    packet = await build_packet(p3c_engine, tenant, period)
    assert _driver(packet, tenant.driver_a).daily_pay == D("24.00")


async def test_the_calculation_preview_and_line_list_report_the_same_derived_money(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="4")
    pid = period["payroll_period_id"]
    preview = (await p3c_client.get(
        f"/payroll/periods/{pid}/calculation-preview", headers=tenant.admin)).json()
    assert preview["has_blockers"] is False
    (preview_line,) = preview["drivers"][0]["lines"]
    assert D(preview_line["calculated_amount"]) == D("17") and preview_line["resolved_rate"] is None
    lines = (await p3c_client.get(f"/payroll/periods/{pid}/lines", headers=tenant.admin)).json()
    assert [(D(ln["calculated_amount"]), ln["calculation_status"]) for ln in lines] == [
        (D("17"), "Calculated")]


# ---------------------------------------------------------------------------
# WorkDate and whole schedules
# ---------------------------------------------------------------------------

async def test_the_work_date_selects_one_whole_tier_schedule(p3c_client, p3c_engine, tenant):
    period, ppd, definition = await open_tier_period(
        p3c_client, p3c_engine, tenant, amounts=("3", "4", "5"))
    await approve_tier_rate(
        p3c_client, tenant, definition, ("6", "7", "8"), effective_from=day(period, 3))
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="4")
    await enter(p3c_client, tenant, period, 4, ppd=ppd, quantity="4")
    packet = await build_packet(p3c_engine, tenant, period)
    amounts = {ln.work_date: ln.calculated_amount
               for ln in _driver(packet, tenant.driver_a).lines if ln.source_type == "DraftLine"}
    # Old date, old schedule (3+4+5+5); new date, new schedule (6+7+8+8): never a mix.
    assert amounts == {
        date.fromisoformat(day(period, 1)): D("17"), date.fromisoformat(day(period, 4)): D("29")}
    schedule_ids = {ln.driver_rate_schedule_id
                    for ln in _driver(packet, tenant.driver_a).lines if ln.source_type == "DraftLine"}
    assert len(schedule_ids) == 2


# ---------------------------------------------------------------------------
# Zero, whole numbers, missing
# ---------------------------------------------------------------------------

async def test_explicit_zero_tier_values_pay_zero_and_are_not_a_blocker(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_tier_period(
        p3c_client, p3c_engine, tenant, amounts=("0", "0", "0"))
    saved = await _save(p3c_client, tenant, period, {str(ppd): "6"})
    cell = _cell(saved.json(), ppd, tenant.driver_a)
    assert (D(cell["calculated_amount"]), cell["calculation_status"]) == (D("0"), "Calculated")
    packet = await build_packet(p3c_engine, tenant, period)
    assert packet.blockers == [] and _driver(packet, tenant.driver_a).daily_pay == D("0")


async def test_a_zero_quantity_is_valid_and_earns_nothing(p3c_client, p3c_engine, tenant):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    saved = await _save(p3c_client, tenant, period, {str(ppd): "0"})
    assert saved.status_code == 200, saved.text
    packet = await build_packet(p3c_engine, tenant, period)
    assert packet.blockers == []
    assert _driver(packet, tenant.driver_a).daily_pay == D("0") if packet.drivers else True


@pytest.mark.parametrize("quantity", ["2.5", "0.1", "-1"])
async def test_a_quantity_that_is_not_a_whole_number_is_refused_at_entry(
    p3c_client, p3c_engine, tenant, quantity,
):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    refused = await _save(p3c_client, tenant, period, {str(ppd): quantity})
    assert refused.status_code == 422
    created = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/lines",
        json={"driver_assignment_id": tenant.driver_a, "work_date": day(period, 1),
              "payroll_period_definition_id": ppd, "quantity": quantity}, headers=tenant.admin)
    assert created.status_code == 422
    assert (await build_packet(p3c_engine, tenant, period)).drivers == []


async def test_a_missing_schedule_is_a_blocker_never_zero(p3c_client, p3c_engine, tenant):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant, amounts=None)
    saved = await _save(p3c_client, tenant, period, {str(ppd): "4"})
    assert saved.status_code == 200
    cell = _cell(saved.json(), ppd, tenant.driver_a)
    assert cell["calculated_amount"] is None
    assert (cell["calculation_status"], cell["needs_manager_review"]) == ("MissingRate", True)
    packet = await build_packet(p3c_engine, tenant, period)
    assert packet.blockers or _driver(packet, tenant.driver_a).needs_manager_review
    # Submission is refused and nothing is frozen.
    from tests.calculation_snapshot_helpers import allow_self_approval
    allow_self_approval(tenant)
    refused = await submit(p3c_client, tenant, period, expect=422)
    assert refused.status_code == 422


async def test_a_date_before_the_first_schedule_is_missing(p3c_client, p3c_engine, tenant):
    period, ppd, _ = await open_tier_period(
        p3c_client, p3c_engine, tenant, effective_from=(setup_anchor() + timedelta(days=5)).isoformat())
    saved = await _save(p3c_client, tenant, period, {str(ppd): "4"}, offset=1)
    assert _cell(saved.json(), ppd, tenant.driver_a)["calculation_status"] == "MissingRate"


# ---------------------------------------------------------------------------
# Aggregation, payable rounding and the other domains
# ---------------------------------------------------------------------------

async def test_line_money_stays_exact_and_is_rounded_once_per_component(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_tier_period(
        p3c_client, p3c_engine, tenant, amounts=("0.3333", "0.3333", "0.3333"))
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="1")
    await enter(p3c_client, tenant, period, 2, ppd=ppd, quantity="1")
    driver = _driver(await build_packet(p3c_engine, tenant, period), tenant.driver_a)
    assert [ln.calculated_amount for ln in driver.lines if ln.source_type == "DraftLine"] == [
        D("0.3333"), D("0.3333")]
    (component,) = [c for c in driver.components if c.kind == "ORDINARY"]
    # Two lines of 0.3333 sum to 0.6666 and round once, to 0.67, under payable-rounding-v1.
    assert (component.pre_rounding_amount, component.official_amount) == (D("0.6666"), D("0.67"))
    assert driver.daily_pay == D("0.67")


async def test_ordinal_tier_and_per_unit_aggregate_in_their_own_components(
    p3c_client, p3c_engine, tenant,
):
    tiered = await rated_ordinal_definition(p3c_client, tenant, definition_name="Beta route")
    plain = await rated_definition(p3c_client, tenant, rate="2", definition_name="Alpha item")
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    ppd = {d["pay_definition_id"]: d["payroll_period_definition_id"]
           for d in period_definitions(period["payroll_period_id"], tenant)}
    saved = await _save(p3c_client, tenant, period, {
        str(ppd[tiered["pay_definition_id"]]): "4", str(ppd[plain["pay_definition_id"]]): "10"})
    assert saved.status_code == 200, saved.text
    await add_bonus(p3c_client, tenant, period, "2.00")

    driver = _driver(await build_packet(p3c_engine, tenant, period), tenant.driver_a)
    by_method = {c.calculation_method: c for c in driver.components if c.kind == "ORDINARY"}
    assert set(by_method) == {"OrdinalTier", "PerUnit"}
    assert by_method["OrdinalTier"].official_amount == D("17.00")
    assert by_method["PerUnit"].official_amount == D("20.00")
    (bonus,) = [c for c in driver.components if c.kind == "BONUS"]
    assert bonus.official_amount == D("2.00")
    assert (driver.daily_pay, driver.bonus_total, driver.expected_pay) == (
        D("37.00"), D("2.00"), D("39.00"))


async def test_a_minimum_rule_still_tops_up_the_ordinary_base_that_includes_tier_pay(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="4")
    await add_rule(p3c_client, tenant, period, "MinimumPay", "70")
    await add_bonus(p3c_client, tenant, period, "1.00")
    driver = _driver(await build_packet(p3c_engine, tenant, period), tenant.driver_a)
    assert driver.daily_pay == D("17.00")
    assert driver.minmax.base_amount == D("17.00")           # Bonus is outside the base
    assert driver.minimum_adjustment == D("53.00")           # 70 - 17
    assert driver.expected_pay == D("71.00")                 # 17 + 53 + 1


# ---------------------------------------------------------------------------
# Frozen method and version dispatch
# ---------------------------------------------------------------------------

async def test_an_unsupported_frozen_version_fails_closed_and_never_runs_the_first_algorithm(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    saved = await _save(p3c_client, tenant, period, {str(ppd): "4"})
    assert D(_cell(saved.json(), ppd, tenant.driver_a)["calculated_amount"]) == D("17")

    async with p3c_engine.begin() as conn:
        async with suspended_test_triggers(conn, (PERIOD_DEFINITION_IMMUTABLE,)):
            await conn.execute(
                text("UPDATE payroll.payrollperioddefinitions "
                     "SET calculationmethodversionsnapshot = 2 WHERE payrollperiodid = :period"),
                {"period": period["payroll_period_id"]})

    grid = await get_grid(p3c_client, tenant, period)
    cell = _cell(grid, ppd, tenant.driver_a)
    assert cell["calculated_amount"] is None
    assert (cell["calculation_status"], cell["needs_manager_review"]) == ("MethodNotReady", True)
    blocked = await _save(p3c_client, tenant, period, {str(ppd): "1"}, offset=1)
    assert blocked.status_code == 409
    preview = (await p3c_client.get(
        f"/payroll/periods/{period['payroll_period_id']}/calculation-preview",
        headers=tenant.admin)).json()
    assert preview["has_blockers"] is True


async def test_no_money_is_stored_on_the_source_line_so_none_can_become_authority(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="4")
    async with p3c_engine.connect() as conn:
        stored = (await conn.execute(
            text("SELECT payrollperioddefinitionid, quantity, needsmanagerreview "
                 "FROM payroll.payrolldraftlines WHERE payrollperiodid = :period"),
            {"period": period["payroll_period_id"]})).all()
        retired = (await conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'payroll' AND table_name = 'payrolldraftlines'
              AND column_name IN ('rateamount', 'calculatedamount', 'linetype', 'sourcesnapshot')
        """))).scalars().all()
    assert [tuple(row) for row in stored] == [(ppd, D("4"), False)]
    assert retired == []
    driver = _driver(await build_packet(p3c_engine, tenant, period), tenant.driver_a)
    assert driver.daily_pay == D("17.00")


async def test_the_live_reports_show_the_derived_tier_money_per_definition(
    p3c_client, p3c_engine, tenant,
):
    period, ppd, _ = await open_tier_period(p3c_client, p3c_engine, tenant)
    await enter(p3c_client, tenant, period, 1, ppd=ppd, quantity="4")
    pid = period["payroll_period_id"]
    for view in ("drivers", "period-work", "period-pay", "mixed"):
        response = await p3c_client.get(f"/payroll/periods/{pid}/reports/{view}", headers=tenant.admin)
        assert response.status_code == 200, (view, response.text[:200])
        (driver,) = response.json()["drivers"]
        assert driver["work"]["daily_rows"][0]["quantity"] == "4.0000"
        assert D(driver["pay"]["daily_pay"]) == D("17.00")
        assert D(driver["pay"]["total_pay"]) == D("17.00")
    pay = (await p3c_client.get(
        f"/payroll/periods/{pid}/reports/period-pay", headers=tenant.admin)).json()
    (amount,) = pay["drivers"][0]["pay"]["definition_amounts"]
    assert (amount["payroll_period_definition_id"], D(amount["amount"])) == (ppd, D("17.00"))
