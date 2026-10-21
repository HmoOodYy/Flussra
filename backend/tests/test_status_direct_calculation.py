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
        WHERE payrollperiodid = %s AND (sourcetype = 'System' OR sourceid LIKE 'STATUS_PAYMENT%%'
                                        OR linetype IN ('STATUS_PAY', 'STATUS_PAYMENT'))
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
