"""DD3: payable rounding and Min/Max proration in the live calculation packet.

Real application, real periods created by the canonical creator (frozen PayrollPeriodDays,
frozen eligibility), real Day Grid entries and the real pay-rule, Bonus and preview routes.
The pure rounding and proration cores are covered in test_dd3_payable_rounding; transfers
across two Branches are in test_dd3_transfer_proration.

The weekly period is Monday .. Sunday: offsets 0..6. A mask of 65 makes Sunday and Saturday
normal days off, so the scheduled days are Monday .. Friday.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest

from app.payroll.period_calculation import _build_live_calculation_packet
from tests.builders.pay_rules import DEFAULT_ANCHOR, insert_pay_rule
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
    unique,
)
from tests.db_state import TriggerIdentity, suspended_test_triggers
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    get_grid,
    query,
    rated_definition,
    seed_status_key,
    seed_status_rate,
    set_employment,
    setup_anchor,
    status_column_of,
)

pytestmark = pytest.mark.asyncio

PAY_RULE_CONTEXT_GUARD = TriggerIdentity(
    "payroll", "driverpayrules", "trg_drivercompensationcontextwritable")
WEEKEND_OFF = 65          # Sunday (bit 0) and Saturday (bit 6)
D = Decimal


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


@pytest.fixture(name="jpy_tenant")
def jpy_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn, currency="JPY")


@pytest.fixture(name="kwd_tenant")
def kwd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn, currency="KWD")


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def _day(period: dict, offset: int = 0) -> str:
    return (date.fromisoformat(period["start_date"]) + timedelta(days=offset)).isoformat()


_NO_DEFINITION = "no definition"


async def _period(client, engine, tenant, *, mask: int = 0, hire_offset: int | None = None,
                  rate: str | None = _NO_DEFINITION) -> tuple[dict, int | None]:
    """An Open Branch A period. With ``rate`` it also has one rated definition and its Day Grid
    column is returned (``rate=None`` is a definition whose Driver has no rate)."""
    if hire_offset is not None:
        set_employment(tenant, tenant.driver_a,
                       hire_date=setup_anchor() + timedelta(days=hire_offset))
    with_definition = rate != _NO_DEFINITION
    if with_definition:
        await rated_definition(client, tenant, rate=rate)
    await assign_weekly_setup(engine, tenant, tenant.branch_a, mask=mask)
    period = await create_period(client, tenant, tenant.branch_a)
    if not with_definition:
        return period, None
    grid = await get_grid(client, tenant, period)
    return period, grid["columns"][0]["payroll_period_definition_id"]


async def _enter(client, tenant, period, offset, *, ppd=None, quantity=None,
                 driver=None, status_key=None) -> None:
    row = {"driver_assignment_id": driver or tenant.driver_a,
           "values": {} if ppd is None else {str(ppd): quantity}}
    if status_key is not None:
        row["status_key"] = status_key
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": _day(period, offset), "rows": [row]}, headers=tenant.admin)
    assert response.status_code == 200, response.text


async def _rule(client, tenant, period, rule_type, amount, *, driver_id=None):
    response = await client.post(
        "/payroll/driver-pay-rules",
        json={"driver_id": driver_id or tenant.driver_a_id, "rule_type": rule_type,
              "amount": amount, "effective_from": period["start_date"]},
        headers=tenant.admin)
    assert response.status_code == 201, response.text
    return response.json()


async def _preview(client, tenant, period) -> dict:
    response = await client.get(
        f"/payroll/periods/{period['payroll_period_id']}/calculation-preview",
        headers=tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()


def _row(preview: dict, assignment_id: int) -> dict:
    return next(d for d in preview["drivers"] if d["driver_assignment_id"] == assignment_id)


def _components(row: dict, kind: str) -> list[dict]:
    return [c for c in row["components"] if c["kind"] == kind]


def _money(value) -> Decimal:
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Ordinary earnings: summed first, rounded once per DriverAssignment x PayDefinition
# ---------------------------------------------------------------------------

async def test_ordinary_lines_are_summed_across_days_and_rounded_once(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, rate="0.3333")
    for offset in range(3):
        await _enter(p3c_client, tenant, period, offset, ppd=ppd, quantity="1")

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    (component,) = _components(row, "ORDINARY")
    # Three raw lines of 0.3333: rounding each day would pay 0.99, rounding the sum pays 1.00.
    assert [_money(line["calculated_amount"]) for line in row["lines"]] == [D("0.3333")] * 3
    assert _money(component["pre_rounding_amount"]) == D("0.9999")
    assert _money(component["official_amount"]) == D("1.00")
    assert component["line_count"] == 3
    assert _money(row["daily_pay"]) == D("1.00") != D("0.33") * 3
    assert _money(row["expected_pay"]) == D("1.00")
    assert _money(preview["total_expected_pay"]) == D("1.00")
    assert preview["has_blockers"] is False


async def test_each_definition_is_its_own_component_and_the_total_is_never_re_rounded(
    p3c_client, p3c_engine, tenant,
):
    await rated_definition(p3c_client, tenant, rate="0.4949", definition_code="ITEM_A",
                           definition_name="Alpha")
    await rated_definition(p3c_client, tenant, rate="0.4949", definition_code="ITEM_B",
                           definition_name="Beta")
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    grid = await get_grid(p3c_client, tenant, period)
    columns = {c["definition_code"]: c["payroll_period_definition_id"] for c in grid["columns"]}
    await _enter(p3c_client, tenant, period, 0, ppd=columns["ITEM_A"], quantity="1")
    await _enter(p3c_client, tenant, period, 0, ppd=columns["ITEM_B"], quantity="1")

    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert sorted(_money(c["official_amount"]) for c in _components(row, "ORDINARY")) == [
        D("0.49"), D("0.49")]
    # The driver's total is the exact sum of the official components (0.98), not the rounded
    # sum of the raw amounts (0.9898 -> 0.99).
    assert _money(row["daily_pay"]) == D("0.98")
    assert _money(row["expected_pay"]) == D("0.98")
    assert _money(row["expected_pay"]) == sum(_money(c["official_amount"]) for c in row["components"])


@pytest.mark.parametrize("rate, expected", [("0.125", "0.12"), ("0.135", "0.14")])
async def test_usd_ties_round_half_even(p3c_client, p3c_engine, tenant, rate, expected):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, rate=rate)
    await _enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="1")
    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert _money(row["daily_pay"]) == D(expected)


async def test_jpy_has_no_decimals_and_ties_go_to_the_even_neighbour(p3c_client, p3c_engine, jpy_tenant):
    tenant = jpy_tenant
    for code, rate in (("HALF", "0.5"), ("ONE_HALF", "1.5"), ("TWO_HALF", "2.5")):
        await rated_definition(p3c_client, tenant, rate=rate, definition_code=code,
                               definition_name=code)
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    grid = await get_grid(p3c_client, tenant, period)
    for column in grid["columns"]:
        await _enter(p3c_client, tenant, period, 0,
                     ppd=column["payroll_period_definition_id"], quantity="1")

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert (preview["currency_code"], preview["currency_minor_unit_digits"]) == ("JPY", 0)
    official = {_money(c["pre_rounding_amount"]): _money(c["official_amount"])
                for c in _components(row, "ORDINARY")}
    assert official == {D("0.5"): D("0"), D("1.5"): D("2"), D("2.5"): D("2")}
    assert _money(row["daily_pay"]) == D("4")
    assert _money(row["expected_pay"]) == D("4")
    # Raw line amounts are untouched by the currency.
    assert sorted(_money(line["calculated_amount"]) for line in row["lines"]) == [
        D("0.5"), D("1.5"), D("2.5")]


async def test_kwd_has_three_decimals_and_rates_keep_four(p3c_client, p3c_engine, kwd_tenant):
    tenant = kwd_tenant
    for code, rate in (("EVEN_TIE", "1.2345"), ("ODD_TIE", "1.2355")):
        await rated_definition(p3c_client, tenant, rate=rate, definition_code=code,
                               definition_name=code)
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    for column in (await get_grid(p3c_client, tenant, period))["columns"]:
        await _enter(p3c_client, tenant, period, 0,
                     ppd=column["payroll_period_definition_id"], quantity="1")

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert preview["currency_minor_unit_digits"] == 3
    # The four-decimal rates are applied as stored; only the official amounts are rounded.
    assert sorted(_money(line["calculated_amount"]) for line in row["lines"]) == [
        D("1.2345"), D("1.2355")]
    assert sorted(_money(c["official_amount"]) for c in _components(row, "ORDINARY")) == [
        D("1.234"), D("1.236")]
    assert _money(row["daily_pay"]) == D("2.470")


async def test_a_zero_rate_is_a_valid_zero_amount_and_not_a_blocker(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, rate="0")
    await _enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="7")
    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    (component,) = _components(row, "ORDINARY")
    assert _money(component["official_amount"]) == 0 and component["unresolved_line_count"] == 0
    assert _money(row["expected_pay"]) == 0 and preview["has_blockers"] is False


async def test_a_missing_rate_still_blocks_and_is_never_a_silent_zero(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, rate=None)
    await _enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="7")

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    (component,) = _components(row, "ORDINARY")
    assert preview["has_blockers"] is True and row["needs_manager_review"] is True
    assert component["unresolved_line_count"] == 1
    assert row["lines"][0]["calculation_status"] == "MissingRate"
    assert row["lines"][0]["calculated_amount"] is None
    assert any("require manager review" in blocker for blocker in preview["blockers"])


# ---------------------------------------------------------------------------
# Status pay: one official amount per DriverAssignment x status rate column
# ---------------------------------------------------------------------------

def _status_pay_setup(tenant, rate: str) -> tuple[int, int]:
    """A paid (non-off) Status key worth one hour, its rate column, and the Driver's rate."""
    column_id, _, _ = status_column_of(tenant)
    key_id = seed_status_key(tenant, "PAID", off=False, hoursvalue=D("1"),
                             statusratecolumnid=column_id)
    seed_status_rate(tenant, D(rate))
    return column_id, key_id


async def test_status_pay_is_summed_per_rate_column_and_rounded_once(p3c_client, p3c_engine, tenant):
    column_id, _ = _status_pay_setup(tenant, "0.3333")
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    for offset in range(3):
        await _enter(p3c_client, tenant, period, offset, status_key="PAID")

    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    (component,) = _components(row, "STATUS")
    assert component["status_rate_column_id"] == column_id and component["line_count"] == 3
    assert _money(component["pre_rounding_amount"]) == D("0.9999")
    assert _money(component["official_amount"]) == D("1.00")        # not 3 x 0.33
    assert _money(row["status_pay"]) == D("1.00")
    assert _money(row["expected_pay"]) == D("1.00")


# ---------------------------------------------------------------------------
# Bonus: one official amount per DriverAssignment x period
# ---------------------------------------------------------------------------

async def test_bonus_events_are_normalized_on_entry_and_aggregated_into_one_component(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    created = []
    for typed, saved in (("1.005", "1.00"), ("2.015", "2.02")):
        response = await p3c_client.post(
            f"/payroll/periods/{period['payroll_period_id']}/bonuses",
            json={"driver_assignment_id": tenant.driver_a, "amount": typed}, headers=tenant.admin)
        assert response.status_code == 201, response.text
        assert _money(response.json()["amount"]) == D(saved)       # the normalized value is returned
        created.append(response.json()["bonus_event_id"])

    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    (component,) = _components(row, "BONUS")
    assert component["line_count"] == 2
    assert _money(component["pre_rounding_amount"]) == _money(component["official_amount"]) == D("3.02")
    assert _money(row["bonus_total"]) == D("3.02") and _money(row["expected_pay"]) == D("3.02")
    assert row["minmax"]["status"] == "NO_RULE"
    stored = query(tenant, "SELECT amount FROM payroll.payrollbonusevents "
                           "WHERE payrollperiodid = %s ORDER BY payrollbonuseventid",
                   (period["payroll_period_id"],))
    assert [_money(a) for (a,) in stored] == [D("1.00"), D("2.02")]
    assert len(created) == 2


# ---------------------------------------------------------------------------
# Min/Max proration
# ---------------------------------------------------------------------------

async def test_a_minimum_is_prorated_by_the_eligible_scheduled_days(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, hire_offset=3, rate="10")
    await _enter(p3c_client, tenant, period, 3, ppd=ppd, quantity="10")        # earned 100
    rule = await _rule(p3c_client, tenant, period, "MinimumPay", "700")

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    minmax = row["minmax"]
    assert (minmax["status"], minmax["numerator"], minmax["denominator"]) == ("APPLIED", 4, 7)
    assert _money(minmax["minimum_rule_amount"]) == 700
    assert _money(minmax["prorated_minimum"]) == D("400.0000")              # 700 x 4/7
    assert minmax["minimum_rule_id"] == rule["driver_pay_rule_id"]
    assert _money(row["minimum_adjustment"]) == D("300.00")                   # 400 - 100
    assert _money(row["expected_pay"]) == D("400.00")
    (line,) = [ln for ln in row["lines"] if ln["line_type"] == "SYS_MIN_TOPUP"]
    assert _money(line["calculated_amount"]) == D("300.00")
    (adjustment,) = _components(row, "MINIMUM_ADJUSTMENT")
    assert adjustment["driver_pay_rule_id"] == rule["driver_pay_rule_id"]
    assert _money(adjustment["pre_rounding_amount"]) == D("300.0000")


async def test_a_maximum_uses_the_same_fraction_and_the_cap_is_negative(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, hire_offset=3, rate="10")
    await _enter(p3c_client, tenant, period, 3, ppd=ppd, quantity="20")        # earned 200
    await _rule(p3c_client, tenant, period, "MaximumPay", "210")

    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert (row["minmax"]["numerator"], row["minmax"]["denominator"]) == (4, 7)
    assert _money(row["minmax"]["prorated_maximum"]) == D("120.0000")         # 210 x 4/7
    assert _money(row["maximum_adjustment"]) == D("-80.00")
    assert _money(row["expected_pay"]) == D("120.00")
    (line,) = [ln for ln in row["lines"] if ln["line_type"] == "SYS_MAX_CAP"]
    assert _money(line["calculated_amount"]) == D("-80.00")


async def test_a_full_period_applies_the_rule_amounts_unchanged(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, rate="10")
    await _enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="10")
    await _rule(p3c_client, tenant, period, "MinimumPay", "250.5")
    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert (row["minmax"]["numerator"], row["minmax"]["denominator"]) == (7, 7)
    assert _money(row["minmax"]["prorated_minimum"]) == _money(row["minmax"]["minimum_rule_amount"]) == D("250.5")
    assert _money(row["minimum_adjustment"]) == D("150.50")


async def test_a_prorated_minimum_is_rounded_once_as_an_adjustment(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, hire_offset=4, rate="0")
    await _enter(p3c_client, tenant, period, 4, ppd=ppd, quantity="1")         # earned 0
    await _rule(p3c_client, tenant, period, "MinimumPay", "100")
    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert (row["minmax"]["numerator"], row["minmax"]["denominator"]) == (3, 7)
    (adjustment,) = _components(row, "MINIMUM_ADJUSTMENT")
    assert _money(adjustment["pre_rounding_amount"]) == D("42.8571")           # internal precision
    assert _money(adjustment["official_amount"]) == D("42.86")                  # the one rounding
    assert _money(row["minimum_adjustment"]) == D("42.86")
    (line,) = [ln for ln in row["lines"] if ln["line_type"] == "SYS_MIN_TOPUP"]
    assert _money(line["calculated_amount"]) == D("42.86")


@pytest.mark.parametrize("rule_type, amount", [("MinimumPay", "700"), ("MaximumPay", "20")])
async def test_a_driver_starting_on_the_weekend_gets_neither_top_up_nor_cap(
    p3c_client, p3c_engine, tenant, rule_type, amount,
):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, mask=WEEKEND_OFF, hire_offset=5,
                                rate="10")
    await _enter(p3c_client, tenant, period, 5, ppd=ppd, quantity="5")         # Saturday: earned 50
    await _rule(p3c_client, tenant, period, rule_type, amount)

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert (row["minmax"]["status"], row["minmax"]["numerator"], row["minmax"]["denominator"]) == (
        "NO_ELIGIBLE_SCHEDULED_DAYS", 0, 5)
    assert _money(row["minimum_adjustment"]) == 0 and _money(row["maximum_adjustment"]) == 0
    assert _money(row["expected_pay"]) == D("50.00")                           # paid exactly as earned
    assert preview["blockers"] == [] and preview["warnings"] == []


async def test_a_period_without_scheduled_days_applies_nothing_and_warns(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, rate="10")
    await _enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="5")
    minimum = await _rule(p3c_client, tenant, period, "MinimumPay", "10")
    maximum = await _rule(p3c_client, tenant, period, "MaximumPay", "20")
    query(tenant, """
        UPDATE payroll.payrollperioddays SET isdefaultworkday = FALSE, isconfiguredoffday = TRUE
        WHERE payrollperiodid = %s
    """, (period["payroll_period_id"],))

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert (row["minmax"]["status"], row["minmax"]["numerator"], row["minmax"]["denominator"]) == (
        "NO_SCHEDULED_DAYS", 0, 0)
    assert _money(row["expected_pay"]) == D("50.00")
    assert preview["blockers"] == []                                          # a warning, not a blocker
    (warning,) = preview["warnings"]
    assert str(tenant.driver_a) in warning and str(tenant.driver_a_id) in warning
    assert str(minimum["driver_pay_rule_id"]) in warning
    assert str(maximum["driver_pay_rule_id"]) in warning


async def _bonus_source(client, tenant, period) -> None:
    """A financial source that references no day row (a Day Grid entry would)."""
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/bonuses",
        json={"driver_assignment_id": tenant.driver_a, "amount": "1.00"}, headers=tenant.admin)
    assert response.status_code == 201, response.text


async def test_a_period_without_a_frozen_day_calendar_blocks_instead_of_inferring_days(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    await _bonus_source(p3c_client, tenant, period)
    rule = await _rule(p3c_client, tenant, period, "MinimumPay", "700")
    query(tenant, "DELETE FROM payroll.payrollperioddays WHERE payrollperiodid = %s",
          (period["payroll_period_id"],))

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert row["minmax"]["status"] == "MINMAX_PERIOD_DAYS_MISSING"
    assert (row["minmax"]["numerator"], row["minmax"]["denominator"]) == (None, None)
    assert any(b.startswith("MINMAX_PERIOD_DAYS_MISSING") and str(rule["driver_pay_rule_id"]) in b
               for b in row["blockers"])
    assert preview["has_blockers"] is True and preview["warnings"] == []
    assert _money(row["minimum_adjustment"]) == 0 and _money(row["expected_pay"]) == D("1.00")


async def test_a_period_without_day_rows_and_without_a_rule_is_not_blocked(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    await _bonus_source(p3c_client, tenant, period)
    query(tenant, "DELETE FROM payroll.payrollperioddays WHERE payrollperiodid = %s",
          (period["payroll_period_id"],))
    preview = await _preview(p3c_client, tenant, period)
    assert preview["blockers"] == []
    assert _row(preview, tenant.driver_a)["minmax"]["status"] == "NO_RULE"


async def test_an_off_reason_day_does_not_count_and_a_non_off_status_does(p3c_client, p3c_engine, tenant):
    seed_status_key(tenant, "VAC", off=True)
    seed_status_key(tenant, "LATE", off=False)
    period, ppd = await _period(p3c_client, p3c_engine, tenant, rate="10")
    await _enter(p3c_client, tenant, period, 0, status_key="VAC")
    await _enter(p3c_client, tenant, period, 1, status_key="VAC")
    await _enter(p3c_client, tenant, period, 2, status_key="LATE")
    await _enter(p3c_client, tenant, period, 3, ppd=ppd, quantity="10")        # earned 100
    await _rule(p3c_client, tenant, period, "MinimumPay", "700")

    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert (row["minmax"]["numerator"], row["minmax"]["denominator"]) == (5, 7)
    assert _money(row["minmax"]["prorated_minimum"]) == D("500.0000")         # 700 x 5/7
    assert _money(row["minimum_adjustment"]) == D("400.00")


async def test_an_unscheduled_worked_day_never_increases_the_fraction(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, mask=WEEKEND_OFF, hire_offset=3,
                                rate="10")
    await _enter(p3c_client, tenant, period, 4, ppd=ppd, quantity="1")         # Friday: earned 10
    await _enter(p3c_client, tenant, period, 5, ppd=ppd, quantity="9")         # Saturday: unscheduled
    await _rule(p3c_client, tenant, period, "MinimumPay", "500")

    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    # Eligible Thursday .. Sunday; scheduled Monday .. Friday: Thursday and Friday count.
    assert (row["minmax"]["numerator"], row["minmax"]["denominator"]) == (2, 5)
    assert _money(row["minmax"]["prorated_minimum"]) == D("200.0000")
    assert _money(row["minimum_adjustment"]) == D("100.00")                   # 200 - (10 + 90)


async def test_the_fraction_comes_from_the_frozen_period_days_not_live_payroll_setup(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, mask=WEEKEND_OFF, rate="10")
    await _enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="10")
    await _rule(p3c_client, tenant, period, "MinimumPay", "500")
    # Payroll Setup moves on to a calendar with no days off; the created period is unaffected.
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_b, mask=0)
    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert (row["minmax"]["numerator"], row["minmax"]["denominator"]) == (5, 5)


def _split_driver(tenant, cut: datetime) -> tuple[int, int, int]:
    """A Driver whose Branch A tenure is two consecutive DriverAssignments cut at ``cut``.

    Returns (DriverID, first assignment, second assignment).
    """
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO core.employees (companyid, branchid, fullname, employmentstatus)
                VALUES (%s, %s, 'Split driver', 'Active') RETURNING employeeid
            """, (tenant.company_id, tenant.branch_a))
            employee_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
                VALUES (%s, %s, %s, 'Active') RETURNING driverid
            """, (tenant.company_id, employee_id, unique("D")))
            driver_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO core.driverassignments
                    (companyid, branchid, employeeid, driverid, effectivefromat, effectivetoat,
                     endreason)
                VALUES (%s, %s, %s, %s, TIMESTAMPTZ '2000-01-01 00:00:00+00', %s, 'Transferred')
                RETURNING driverassignmentid
            """, (tenant.company_id, tenant.branch_a, employee_id, driver_id, cut))
            first = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO core.driverassignments
                    (companyid, branchid, employeeid, driverid, effectivefromat,
                     transferredfromassignmentid)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING driverassignmentid
            """, (tenant.company_id, tenant.branch_a, employee_id, driver_id, cut, first))
            return driver_id, first, cur.fetchone()[0]
    finally:
        conn.close()


async def test_two_assignments_of_one_driver_in_a_period_are_evaluated_on_their_own_days(
    p3c_client, p3c_engine, tenant,
):
    cut = datetime.combine(setup_anchor() + timedelta(days=3), datetime.min.time(), tzinfo=UTC)
    driver_id, first, second = _split_driver(tenant, cut)
    # The rate belongs to the Driver and can only start inside a current tenure.
    await rated_definition(p3c_client, tenant, rate="0", driver_assignment_id=second,
                           effective_from=cut.date().isoformat())
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    ppd = (await get_grid(p3c_client, tenant, period))["columns"][0]["payroll_period_definition_id"]
    # The first assignment has a financial source of its own (a Bonus); the second works.
    bonus = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/bonuses",
        json={"driver_assignment_id": first, "amount": "1.00"}, headers=tenant.admin)
    assert bonus.status_code == 201, bonus.text
    await _enter(p3c_client, tenant, period, 3, ppd=ppd, quantity="1", driver=second)
    # The rule predates the split (a transfer happened inside the period); the writer guard
    # for ended tenures is suspended only to build that history directly.
    async with p3c_engine.begin() as conn:
        async with suspended_test_triggers(conn, (PAY_RULE_CONTEXT_GUARD,)):
            await insert_pay_rule(
                conn, company_id=tenant.company_id, driver_id=driver_id,
                branch_id=tenant.branch_a, rule_type="MinimumPay", amount="700",
                start=setup_anchor())

    preview = await _preview(p3c_client, tenant, period)
    first_row, second_row = _row(preview, first), _row(preview, second)
    assert (first_row["minmax"]["numerator"], first_row["minmax"]["denominator"]) == (3, 7)
    assert (second_row["minmax"]["numerator"], second_row["minmax"]["denominator"]) == (4, 7)
    # Each assignment is topped up to its own share (300 and 400): one guarantee, not two.
    assert _money(first_row["minimum_adjustment"]) == D("300.00")
    assert _money(second_row["minimum_adjustment"]) == D("400.00")
    assert _money(first_row["expected_pay"]) == D("301.00")                   # the Bonus is added after
    assert _money(preview["total_expected_pay"]) == D("701.00")


# ---------------------------------------------------------------------------
# Pay-calendar compatibility and the hard blockers, with proration in play
# ---------------------------------------------------------------------------

async def _direct_rule(engine, tenant, **kwargs) -> int:
    async with engine.begin() as conn:
        return await insert_pay_rule(
            conn, company_id=tenant.company_id, driver_id=tenant.driver_a_id,
            branch_id=tenant.branch_a, start=setup_anchor(), **kwargs)


@pytest.mark.parametrize("calendar", [
    {"frequency": "Biweek"},
    {"frequency": "Week", "anchor": DEFAULT_ANCHOR + timedelta(days=1)},
    {"frequency": "Month", "anchor": date(2000, 1, 1)},
], ids=["different-frequency", "misaligned-anchor", "month-vs-week"])
async def test_an_incompatible_calendar_blocks_even_for_a_partial_period(
    p3c_client, p3c_engine, tenant, calendar,
):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, hire_offset=3, rate="10")
    await _enter(p3c_client, tenant, period, 3, ppd=ppd, quantity="10")
    rule_id = await _direct_rule(p3c_engine, tenant, rule_type="MinimumPay", amount="700", **calendar)

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert row["minmax"]["status"] == "CALENDAR_MISMATCH"
    assert any(b.startswith("MINMAX_CALENDAR_MISMATCH") and str(rule_id) in b for b in row["blockers"])
    assert preview["has_blockers"] is True
    # Never rescaled, never silently dropped, never duplicated.
    assert _money(row["minimum_adjustment"]) == 0
    assert [ln for ln in row["lines"] if ln["line_type"] in ("SYS_MIN_TOPUP", "SYS_MAX_CAP")] == []
    assert _money(row["expected_pay"]) == D("100.00")


async def test_another_anchor_on_the_same_grid_is_compatible_and_prorates(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, hire_offset=3, rate="10")
    await _enter(p3c_client, tenant, period, 3, ppd=ppd, quantity="10")
    await _direct_rule(p3c_engine, tenant, rule_type="MinimumPay", amount="700",
                       anchor=DEFAULT_ANCHOR - timedelta(days=14))
    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert preview["blockers"] == [] and row["minmax"]["status"] == "APPLIED"
    assert _money(row["minimum_adjustment"]) == D("300.00")


async def test_a_days_off_mask_is_not_part_of_the_calendar_identity(p3c_client, p3c_engine, tenant):
    # The rule was captured under a calendar with no days off; the period has weekends off.
    period, ppd = await _period(p3c_client, p3c_engine, tenant, mask=WEEKEND_OFF, rate="10")
    await _enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="10")
    await _direct_rule(p3c_engine, tenant, rule_type="MinimumPay", amount="500")
    preview = await _preview(p3c_client, tenant, period)
    assert preview["blockers"] == []
    assert _money(_row(preview, tenant.driver_a)["minimum_adjustment"]) == D("400.00")


async def test_a_minimum_above_the_maximum_stays_a_hard_blocker_when_prorated(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, hire_offset=3, rate="10")
    await _enter(p3c_client, tenant, period, 3, ppd=ppd, quantity="10")
    await _direct_rule(p3c_engine, tenant, rule_type="MinimumPay", amount="800")
    await _direct_rule(p3c_engine, tenant, rule_type="MaximumPay", amount="700")

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert row["minmax"]["status"] == "MIN_EXCEEDS_MAX"
    assert any("exceeds maximum pay" in b for b in row["blockers"]) and preview["has_blockers"]
    assert _money(row["minimum_adjustment"]) == 0 and _money(row["maximum_adjustment"]) == 0


async def test_a_negative_maximum_adjustment_for_a_full_period_and_with_a_minimum_present(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, rate="10")
    await _enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="50")        # earned 500
    await _rule(p3c_client, tenant, period, "MinimumPay", "100")
    await _rule(p3c_client, tenant, period, "MaximumPay", "300")
    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert _money(row["minimum_adjustment"]) == 0
    assert _money(row["maximum_adjustment"]) == D("-200.00")
    assert _money(row["expected_pay"]) == D("300.00")


# ---------------------------------------------------------------------------
# Typed Bonus / Minimum / Maximum amounts: normalized on entry in the Company currency
# ---------------------------------------------------------------------------

async def test_jpy_bonus_create_update_and_batch_are_normalized_to_whole_units(
    p3c_client, p3c_engine, jpy_tenant,
):
    tenant = jpy_tenant
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    url = f"/payroll/periods/{period['payroll_period_id']}/bonuses"
    created = await p3c_client.post(
        url, json={"driver_assignment_id": tenant.driver_a, "amount": "1500.5"}, headers=tenant.admin)
    assert created.status_code == 201, created.text
    assert _money(created.json()["amount"]) == D("1500")                     # tie to the even neighbour
    event_id = created.json()["bonus_event_id"]
    updated = await p3c_client.patch(
        f"{url}/{event_id}", json={"amount": "1501.5"}, headers=tenant.admin)
    assert updated.status_code == 200, updated.text
    assert _money(updated.json()["amount"]) == D("1502")

    zero = await p3c_client.post(
        url, json={"driver_assignment_id": tenant.driver_a, "amount": "0.4"}, headers=tenant.admin)
    assert zero.status_code == 422 and zero.json()["detail"]["code"] == "AMOUNT_ROUNDS_TO_ZERO"

    summary = (await p3c_client.get(f"{url}/summary", headers=tenant.admin)).json()
    batch = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/bonuses/batch",
        json={"idempotency_key": uuid4().hex,
              "expected_bonus_data_revision": summary["bonus_data_revision"],
              "items": [{"driver_assignment_id": tenant.driver_a, "amount": "2.5"},
                        {"driver_assignment_id": tenant.driver_a, "amount": "3.5"}]},
        headers=tenant.admin)
    assert batch.status_code == 201, batch.text
    assert [_money(e["amount"]) for e in batch.json()["events"]] == [D("2"), D("4")]

    row = _row(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert _money(row["bonus_total"]) == D("1508")                           # 1502 + 2 + 4


async def test_kwd_bonus_and_pay_rule_amounts_are_normalized_to_three_decimals(
    p3c_client, p3c_engine, kwd_tenant,
):
    tenant = kwd_tenant
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    bonus = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/bonuses",
        json={"driver_assignment_id": tenant.driver_a, "amount": "1.2345"}, headers=tenant.admin)
    assert bonus.status_code == 201 and _money(bonus.json()["amount"]) == D("1.234")

    rule = await _rule(p3c_client, tenant, period, "MinimumPay", "100.1235")
    assert _money(rule["amount"]) == D("100.124")
    stored = query(tenant, "SELECT amount FROM payroll.driverpayrules WHERE driverpayruleid = %s",
                   (rule["driver_pay_rule_id"],))
    assert _money(stored[0][0]) == D("100.124")
    audit = query(tenant, """
        SELECT newvaluejson::jsonb->>'amount' FROM audit.auditlog
        WHERE entityname = 'DriverPayRules' AND entityid = %s AND actioncode = 'DRIVER_PAY_RULE_CREATED'
    """, (str(rule["driver_pay_rule_id"]),))
    assert _money(audit[0][0]) == D("100.124")


async def test_a_pay_rule_amount_that_rounds_to_zero_is_refused_in_the_currency(
    p3c_client, p3c_engine, jpy_tenant,
):
    tenant = jpy_tenant
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    refused = await p3c_client.post(
        "/payroll/driver-pay-rules",
        json={"driver_id": tenant.driver_a_id, "rule_type": "MinimumPay", "amount": "0.4",
              "effective_from": period["start_date"]}, headers=tenant.admin)
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "AMOUNT_ROUNDS_TO_ZERO"
    assert query(tenant, "SELECT count(*) FROM payroll.driverpayrules WHERE driverid = %s",
                 (tenant.driver_a_id,)) == [(0,)]
    ok = await p3c_client.post(
        "/payroll/driver-pay-rules",
        json={"driver_id": tenant.driver_a_id, "rule_type": "MinimumPay", "amount": "1000.5",
              "effective_from": period["start_date"]}, headers=tenant.admin)
    assert ok.status_code == 201 and _money(ok.json()["amount"]) == D("1000")


# ---------------------------------------------------------------------------
# Dev data that was never normalized is refused, not rewritten
# ---------------------------------------------------------------------------

async def test_a_stored_bonus_that_is_not_at_currency_precision_blocks_the_packet(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _period(p3c_client, p3c_engine, tenant)
    event_id = query(tenant, """
        INSERT INTO payroll.payrollbonusevents
            (companyid, branchid, payrollperiodid, driverassignmentid, amount, status,
             createdbyuserid, createdatutc, datarevision)
        VALUES (%s, %s, %s, %s, %s, 'Active', %s, NOW(), 1) RETURNING payrollbonuseventid
    """, (tenant.company_id, tenant.branch_a, period["payroll_period_id"], tenant.driver_a,
          D("10.3333"), tenant.owner))[0][0]

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert preview["has_blockers"] is True
    assert any("SOURCE_AMOUNT_PRECISION" in b and f"bonus event {event_id}" in b for b in row["blockers"])
    # The stored value is never rewritten.
    assert _money(query(tenant, "SELECT amount FROM payroll.payrollbonusevents "
                                "WHERE payrollbonuseventid = %s", (event_id,))[0][0]) == D("10.3333")


async def test_a_stored_pay_rule_that_is_not_at_currency_precision_blocks_the_packet(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, rate="10")
    await _enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="1")
    rule_id = await _direct_rule(p3c_engine, tenant, rule_type="MinimumPay", amount="100.0050")
    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert row["minmax"]["status"] == "SOURCE_AMOUNT_PRECISION"
    assert any("SOURCE_AMOUNT_PRECISION" in b and f"pay rule {rule_id}" in b for b in row["blockers"])
    assert _money(row["minimum_adjustment"]) == 0


# ---------------------------------------------------------------------------
# One packet for every reader
# ---------------------------------------------------------------------------

async def test_preview_hub_and_report_agree_on_the_official_totals(p3c_client, p3c_engine, tenant):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, hire_offset=3, rate="0.3333")
    for offset in (3, 4, 5):
        await _enter(p3c_client, tenant, period, offset, ppd=ppd, quantity="1")
    await _rule(p3c_client, tenant, period, "MinimumPay", "70")
    bonus = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/bonuses",
        json={"driver_assignment_id": tenant.driver_a, "amount": "5.005"}, headers=tenant.admin)
    assert bonus.status_code == 201

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    # Earned 0.9999 -> 1.00; Minimum 70 x 4/7 = 40; top-up 39; Bonus 5.005 -> 5.00 (tie to even).
    assert _money(row["daily_pay"]) == D("1.00") and _money(row["minimum_adjustment"]) == D("39.00")
    assert _money(row["bonus_total"]) == D("5.00") and _money(row["expected_pay"]) == D("45.00")

    hub = (await p3c_client.get("/payroll/current", headers=tenant.admin)).json()
    slot = next(b for b in hub["branches"] if b["branch_id"] == tenant.branch_a)["slots"]["open"]
    summary = slot["financial_summary"]
    assert _money(summary["total_expected_pay"]) == _money(preview["total_expected_pay"]) == D("45.00")
    assert _money(summary["normal_pay"]) == D("1.00") and _money(summary["bonus_total"]) == D("5.00")
    assert _money(summary["system_adjustments"]) == D("39.00")

    report = (await p3c_client.get(
        f"/payroll/periods/{period['payroll_period_id']}/reports/period-pay",
        headers=tenant.admin)).json()
    pay = next(d for d in report["drivers"] if d["driver_assignment_id"] == tenant.driver_a)["pay"]
    assert _money(pay["total_pay"]) == D("45.00") and _money(pay["gross_pay"]) == D("1.00")
    assert [_money(a["amount"]) for a in pay["definition_amounts"]] == [D("1.00")]
    assert _money(report["pay_totals"]["total_pay"]) == D("45.00")
    assert [_money(a["amount"]) for a in report["definition_totals"]] == [D("1.00")]


async def test_a_company_without_currency_blocks_instead_of_guessing_digits(
    p3c_client, p3c_engine, p3b_dsn,
):
    tenant = build_payroll_tenant(p3b_dsn, currency=None)
    period, ppd = await _period(p3c_client, p3c_engine, tenant, rate=None)
    await _enter(p3c_client, tenant, period, 0, ppd=ppd, quantity="3")

    preview = await _preview(p3c_client, tenant, period)
    row = _row(preview, tenant.driver_a)
    assert preview["currency_code"] is None and preview["currency_minor_unit_digits"] is None
    assert preview["has_blockers"] is True
    assert any(b.startswith("COMPANY_CURRENCY_REQUIRED") for b in preview["blockers"])
    assert _money(row["expected_pay"]) == 0


# ---------------------------------------------------------------------------
# The packet carries everything P4c freezes
# ---------------------------------------------------------------------------

async def test_the_packet_carries_currency_versions_components_and_the_proration_record(
    p3c_client, p3c_engine, tenant,
):
    period, ppd = await _period(p3c_client, p3c_engine, tenant, hire_offset=3, rate="0.3333")
    for offset in (3, 4):
        await _enter(p3c_client, tenant, period, offset, ppd=ppd, quantity="1")
    rule = await _rule(p3c_client, tenant, period, "MinimumPay", "70")
    await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/bonuses",
        json={"driver_assignment_id": tenant.driver_a, "amount": "2.00"}, headers=tenant.admin)

    from app.payroll.period_read import get_period_by_id

    async with p3c_engine.begin() as conn:
        summary = await get_period_by_id(
            tenant.company_id, tenant.owner, period["payroll_period_id"], conn)
        packet = await _build_live_calculation_packet(summary, tenant.company_id, conn)

    assert (packet.currency_code, packet.currency_minor_unit_digits) == ("USD", 2)
    assert packet.payable_rounding_version == "payable-rounding-v1"
    assert packet.minmax_proration_version == "minmax-proration-v1"
    driver = next(d for d in packet.drivers if d.driver_assignment_id == tenant.driver_a)

    kinds = [c.kind for c in driver.components]
    assert kinds == ["ORDINARY", "BONUS", "MINIMUM_ADJUSTMENT"]
    ordinary, bonus, minimum = driver.components
    assert (ordinary.payroll_period_definition_id, ordinary.calculation_method,
            ordinary.calculation_method_version) == (ppd, "PerUnit", 1)
    assert ordinary.pay_definition_id is not None and len(ordinary.source_ids) == 2
    assert (ordinary.pre_rounding_amount, ordinary.official_amount) == (D("0.6666"), D("0.67"))
    assert (bonus.pre_rounding_amount, bonus.official_amount, len(bonus.source_ids)) == (
        D("2.00"), D("2.00"), 1)
    assert minimum.driver_pay_rule_id == rule["driver_pay_rule_id"]
    # 70 x 4/7 = 40.0000, less the official ordinary base of 0.67.
    assert (minimum.pre_rounding_amount, minimum.official_amount) == (D("39.3300"), D("39.33"))
    assert driver.expected_pay == sum((c.official_amount for c in driver.components), D("0"))
    assert driver.expected_pay == D("42.00")

    proration = driver.minmax
    assert (proration.status, proration.numerator, proration.denominator) == ("APPLIED", 4, 7)
    assert proration.version == "minmax-proration-v1"
    assert (proration.payroll_period_id, proration.branch_id, proration.driver_id) == (
        period["payroll_period_id"], tenant.branch_a, tenant.driver_a_id)
    assert proration.base_amount == D("0.67") and proration.maximum_rule is None
    recorded = proration.minimum_rule
    assert (recorded.driver_pay_rule_id, recorded.amount, recorded.prorated_amount) == (
        rule["driver_pay_rule_id"], D("70"), D("40.0000"))
    assert (recorded.calendar_frequency, recorded.calendar_anchor_start_date,
            recorded.calendar_custom_interval_days) == ("Week", setup_anchor(), None)
    assert recorded.calendar_source_branch_id == tenant.branch_a
    assert recorded.calendar_source_payroll_setup_version_id > 0

    (line,) = [ln for ln in driver.lines if ln.line_type == "SYS_MIN_TOPUP"]
    evidence = line.source_evidence
    assert line.calculated_amount == D("39.33")
    assert evidence["DriverPayRuleID"] == rule["driver_pay_rule_id"]
    assert evidence["RuleAmount"] == D("70") and evidence["NormalBase"] == D("0.67")
    assert (evidence["ProrationNumerator"], evidence["ProrationDenominator"]) == (4, 7)
    assert evidence["ProratedAmount"] == D("40.0000")
    assert (evidence["PreRoundingAdjustment"], evidence["OfficialAdjustment"]) == (
        D("39.3300"), D("39.33"))
