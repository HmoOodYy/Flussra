"""Direct Status compensation: hours x the permanent Driver's Status rate, from the selection.

A Status selection is paid HoursValue x the rate of the Driver's schedule of the Status rate
column's RateDefinition, resolved by the WorkDate. Nothing is projected into draft lines.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    query,
    seed_status_key,
    seed_status_rate,
    status_column_of,
)

pytestmark = pytest.mark.asyncio
D = Decimal


@pytest.fixture(name="tenant")
def usd_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _day(period: dict, offset: int = 0) -> str:
    return (date.fromisoformat(period["start_date"]) + timedelta(days=offset)).isoformat()


async def _period(client, engine, tenant) -> dict:
    await assign_weekly_setup(engine, tenant, tenant.branch_a, mask=0)
    return await create_period(client, tenant, tenant.branch_a)


async def _select(client, tenant, period, offset, status, *, driver=None) -> None:
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": _day(period, offset), "rows": [
            {"driver_assignment_id": driver or tenant.driver_a, "values": {}, "status_key": status}]},
        headers=tenant.admin)
    assert response.status_code == 200, response.text


async def _preview(client, tenant, period) -> dict:
    response = await client.get(
        f"/payroll/periods/{period['payroll_period_id']}/calculation-preview",
        headers=tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()


def _driver(preview: dict, assignment_id: int) -> dict:
    return next(d for d in preview["drivers"] if d["driver_assignment_id"] == assignment_id)


def _paid_status(tenant, name="Vacation", hours="8") -> int:
    column, _, _ = status_column_of(tenant)
    return seed_status_key(tenant, name, off=False, hoursvalue=D(hours), statusratecolumnid=column)


async def test_a_vacation_day_of_eight_hours_at_25_pays_200(p3c_client, p3c_engine, tenant):
    _paid_status(tenant)
    seed_status_rate(tenant, "25")
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Vacation")

    preview = await _preview(p3c_client, tenant, period)
    driver = _driver(preview, tenant.driver_a)
    (line,) = [row for row in driver["lines"] if row["source_type"] == "StatusEntryState"]
    assert (D(str(line["quantity"])), D(str(line["resolved_rate"])), D(str(line["calculated_amount"]))) \
        == (D("8"), D("25"), D("200"))
    assert line["calculation_status"] == "Calculated" and line["needs_manager_review"] is False
    (component,) = [c for c in driver["components"] if c["kind"] == "STATUS"]
    assert D(str(component["official_amount"])) == D("200.00")
    assert D(str(driver["status_pay"])) == D("200.00") and D(str(driver["expected_pay"])) == D("200.00")
    assert preview["has_blockers"] is False


async def test_status_pay_is_never_written_to_draft_lines(p3c_client, p3c_engine, tenant):
    _paid_status(tenant)
    seed_status_rate(tenant, "25")
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Vacation")
    await _preview(p3c_client, tenant, period)

    assert query(tenant, """
        SELECT count(*) FROM payroll.payrolldraftlines
        WHERE payrollperiodid = %s AND payrollperioddefinitionid IS NOT NULL
    """, (period["payroll_period_id"],)) == [(0,)]


async def test_a_status_without_a_rate_column_is_non_monetary_and_never_a_blocker(
    p3c_client, p3c_engine, tenant,
):
    seed_status_key(tenant, "Unpaid leave", off=True, hoursvalue=D("8"))
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Unpaid leave")

    preview = await _preview(p3c_client, tenant, period)
    assert preview["has_blockers"] is False
    assert preview["drivers"] == []


async def test_a_monetary_status_without_a_rate_blocks_until_the_driver_has_one(
    p3c_client, p3c_engine, tenant,
):
    _paid_status(tenant)
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Vacation")

    preview = await _preview(p3c_client, tenant, period)
    driver = _driver(preview, tenant.driver_a)
    (line,) = [row for row in driver["lines"] if row["source_type"] == "StatusEntryState"]
    assert line["calculation_status"] == "MissingRate" and line["calculated_amount"] is None
    assert line["needs_manager_review"] is True and "Status rate" in line["blocker_reason"]
    assert preview["has_blockers"] is True

    seed_status_rate(tenant, "25")
    fixed = await _preview(p3c_client, tenant, period)
    assert fixed["has_blockers"] is False
    assert D(str(_driver(fixed, tenant.driver_a)["status_pay"])) == D("200.00")


async def test_an_explicit_zero_rate_is_valid_and_pays_nothing(p3c_client, p3c_engine, tenant):
    _paid_status(tenant)
    seed_status_rate(tenant, "0")
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Vacation")

    preview = await _preview(p3c_client, tenant, period)
    driver = _driver(preview, tenant.driver_a)
    (line,) = [row for row in driver["lines"] if row["source_type"] == "StatusEntryState"]
    assert line["calculation_status"] == "Calculated" and D(str(line["calculated_amount"])) == D("0")
    assert preview["has_blockers"] is False
    assert D(str(driver["status_pay"])) == D("0.00")


async def test_each_day_uses_the_schedule_effective_on_its_own_work_date(
    p3c_client, p3c_engine, tenant,
):
    _paid_status(tenant, hours="8")
    period = await _period(p3c_client, p3c_engine, tenant)
    change = date.fromisoformat(period["start_date"]) + timedelta(days=2)
    seed_status_rate(tenant, "20", effective_to=(change - timedelta(days=1)).isoformat(),
                     superseded=True)
    seed_status_rate(tenant, "30", effective_from=change.isoformat())
    for offset in range(4):
        await _select(p3c_client, tenant, period, offset, "Vacation")

    driver = _driver(await _preview(p3c_client, tenant, period), tenant.driver_a)
    by_day = {row["work_date"]: D(str(row["resolved_rate"]))
              for row in driver["lines"] if row["source_type"] == "StatusEntryState"}
    assert [by_day[_day(period, offset)] for offset in range(4)] == [D("20"), D("20"), D("30"), D("30")]
    assert D(str(driver["status_pay"])) == D("8") * (D("20") * 2 + D("30") * 2)


async def test_a_pending_schedule_is_not_a_rate(p3c_client, p3c_engine, tenant):
    _paid_status(tenant)
    seed_status_rate(tenant, "25", approve=False)
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Vacation")

    driver = _driver(await _preview(p3c_client, tenant, period), tenant.driver_a)
    (line,) = [row for row in driver["lines"] if row["source_type"] == "StatusEntryState"]
    assert line["calculation_status"] == "MissingRate"


async def test_two_drivers_are_paid_from_their_own_schedules(p3c_client, p3c_engine, tenant):
    _paid_status(tenant)
    seed_status_rate(tenant, "25", driver_id=tenant.driver_a_id)
    seed_status_rate(tenant, "40", driver_id=tenant.driver_a2_id)
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Vacation", driver=tenant.driver_a)
    await _select(p3c_client, tenant, period, 0, "Vacation", driver=tenant.driver_a2)

    preview = await _preview(p3c_client, tenant, period)
    assert D(str(_driver(preview, tenant.driver_a)["status_pay"])) == D("200.00")
    assert D(str(_driver(preview, tenant.driver_a2)["status_pay"])) == D("320.00")


# ---------------------------------------------------------------------------
# Rounding, Min/Max and what the amount does not depend on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hours, rate, line_amount, official", [
    # The product is quantized once to four decimals, ties to the even digit.
    ("3.25", "2.0002", "6.5006", "6.50"),     # 6.500650: the retained digit is even, it stays
    ("2.75", "2.0002", "5.5006", "5.50"),     # 5.500550: the retained digit is odd, it rounds up
])
async def test_the_line_is_rounded_once_to_four_decimals_half_even_and_paid_to_the_cent(
    p3c_client, p3c_engine, tenant, hours, rate, line_amount, official,
):
    _paid_status(tenant, hours=hours)
    seed_status_rate(tenant, rate)
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Vacation")

    driver = _driver(await _preview(p3c_client, tenant, period), tenant.driver_a)
    (line,) = [row for row in driver["lines"] if row["source_type"] == "StatusEntryState"]
    assert D(str(line["calculated_amount"])) == D(line_amount)
    assert D(str(driver["status_pay"])) == D(official)


async def _rule(client, tenant, period, rule_type, amount) -> None:
    response = await client.post(
        "/payroll/driver-pay-rules",
        json={"driver_id": tenant.driver_a_id, "rule_type": rule_type, "amount": amount,
              "effective_from": period["start_date"]},
        headers=tenant.admin)
    assert response.status_code == 201, response.text


async def test_status_pay_is_part_of_the_min_max_base(p3c_client, p3c_engine, tenant):
    _paid_status(tenant)
    seed_status_rate(tenant, "25")
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Vacation")           # 8 h x 25 = 200
    await _rule(p3c_client, tenant, period, "MinimumPay", "500")

    driver = _driver(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert D(str(driver["minimum_adjustment"])) == D("300.00")
    assert D(str(driver["expected_pay"])) == D("500.00")


async def test_a_maximum_caps_the_status_pay_too(p3c_client, p3c_engine, tenant):
    _paid_status(tenant)
    seed_status_rate(tenant, "25")
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Vacation")
    await _rule(p3c_client, tenant, period, "MaximumPay", "150")

    driver = _driver(await _preview(p3c_client, tenant, period), tenant.driver_a)
    assert D(str(driver["maximum_adjustment"])) == D("-50.00")
    assert D(str(driver["expected_pay"])) == D("150.00")


async def test_the_allowance_category_is_a_label_and_never_changes_the_amount(
    p3c_client, p3c_engine, tenant,
):
    column, _, _ = status_column_of(tenant)
    seed_status_key(tenant, "Holiday pay", off=False, hoursvalue=D("8"), category="Holiday",
                    statusratecolumnid=column)
    seed_status_key(tenant, "Sick pay", off=False, hoursvalue=D("8"), category="Sick",
                    statusratecolumnid=column)
    seed_status_rate(tenant, "25")
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Holiday pay")
    await _select(p3c_client, tenant, period, 1, "Sick pay")

    driver = _driver(await _preview(p3c_client, tenant, period), tenant.driver_a)
    amounts = [D(str(row["calculated_amount"]))
               for row in driver["lines"] if row["source_type"] == "StatusEntryState"]
    assert amounts == [D("200"), D("200")]
    assert D(str(driver["status_pay"])) == D("400.00")


async def test_a_status_with_a_rate_column_pays_from_canonical_entry_state(
    p3c_client, p3c_engine, tenant,
):
    """Status pay, blockers and frozen evidence use canonical EntryState only."""
    from tests.calculation_snapshot_helpers import capture

    _paid_status(tenant)
    seed_status_key(tenant, "Other leave", off=True)
    seed_status_rate(tenant, "25")
    period = await _period(p3c_client, p3c_engine, tenant)
    await _select(p3c_client, tenant, period, 0, "Vacation")
    period_id = period["payroll_period_id"]
    assert query(tenant, """
        SELECT count(*) FROM payroll.payrolldraftlines WHERE payrollperiodid = %s
    """, (period_id,)) == [(0,)]

    before = await _preview(p3c_client, tenant, period)
    assert [line["source_type"] for driver in before["drivers"]
            for line in driver["lines"]] == ["StatusEntryState"]
    frozen_before = await capture(p3c_engine, tenant, period)

    rewritten = await _preview(p3c_client, tenant, period)
    voided = await _preview(p3c_client, tenant, period)
    frozen_after = await capture(p3c_engine, tenant, period)

    for other in (rewritten, voided):
        assert D(str(_driver(other, tenant.driver_a)["status_pay"])) == D("200.00")
        assert other["has_blockers"] is False
        assert other["drivers"] == before["drivers"]
    evidence = [query(tenant, """
        SELECT statuskeyid, statushoursvaluesnapshot, statuscategorysnapshot, workdate
        FROM payroll.payrollcalculationsnapshotstatusentries
        WHERE payrollcalculationsnapshotid = %s
    """, (frozen.snapshot_id,)) for frozen in (frozen_before, frozen_after)]
    assert evidence[0] == evidence[1] and len(evidence[0]) == 1


async def test_pto_status_cannot_be_entered_it_is_not_a_status(p3c_client, p3c_engine, tenant):
    _paid_status(tenant)
    period = await _period(p3c_client, p3c_engine, tenant)
    response = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": _day(period), "rows": [
            {"driver_assignment_id": tenant.driver_a, "values": {}, "status_key": "PTO_STATUS"}]},
        headers=tenant.admin)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "STATUS_NOT_FOUND"


async def test_status_pay_is_not_a_payitem_and_cannot_be_written_as_an_ordinary_line(
    p3c_client, p3c_engine, tenant,
):
    assert query(tenant, """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'payroll' AND table_name = 'payrollstatuskeys'
          AND column_name ILIKE '%%payitem%%'
    """) == []
    assert query(tenant, "SELECT to_regclass('payroll.payitems')") == [(None,)]
    period = await _period(p3c_client, p3c_engine, tenant)
    # An ordinary source line is a quantity against a period definition: a line type, a rate
    # or an amount is not an input at all.
    for extra in ({"line_type": "STATUS_PAY"}, {"line_type": "STATUS_PAYMENT"},
                  {"calculated_amount": "200"}):
        response = await p3c_client.post(
            f"/payroll/periods/{period['payroll_period_id']}/lines",
            json={"driver_assignment_id": tenant.driver_a, "work_date": _day(period),
                  "payroll_period_definition_id": 1, "quantity": "8", **extra},
            headers=tenant.admin)
        assert response.status_code == 422, (extra, response.text)
        assert {error["type"] for error in response.json()["detail"]} == {"extra_forbidden"}
