"""Period entry overview: the day grid of every work date of a period in one read.

Each day must be exactly what the single-date Day Grid read returns for that date, the read
is authorized like it, follows the frozen period days for configured days off, and writes
nothing.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.calculation_snapshot_helpers import (
    finalize,
    standard_scenario,
    submit_and_approve,
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
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    get_grid,
    make_driver_self_user,
    make_user,
    open_period_with_grid,
    query,
    rated_definition,
    seed_status_key,
)

pytestmark = pytest.mark.asyncio

# Normal days off as a Sunday-first weekday mask: Sunday (bit 0) and Saturday (bit 6).
WEEKEND_OFF = 65


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _overview_url(period) -> str:
    return f"/payroll/periods/{period['payroll_period_id']}/entry-overview"


def _day(period, offset=0) -> str:
    return (date.fromisoformat(period["start_date"]) + timedelta(days=offset)).isoformat()


async def _overview(client, tenant, period, *, headers=None) -> dict:
    response = await client.get(_overview_url(period), headers=headers or tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()


async def _save(client, tenant, period, rows, *, offset=0):
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": _day(period, offset), "rows": rows}, headers=tenant.admin)
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Equality with the single-date read
# ---------------------------------------------------------------------------

async def test_every_overview_day_equals_the_single_date_day_grid(
    p3c_client, p3c_engine, tenant,
):
    seed_status_key(tenant, "VAC", off=True)
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    ppd = str(grid["columns"][0]["payroll_period_definition_id"])
    await _save(p3c_client, tenant, period, [
        {"driver_assignment_id": tenant.driver_a, "values": {ppd: "8"}},
        {"driver_assignment_id": tenant.driver_a2, "values": {ppd: "3.5"}, "notes": "late start"},
    ], offset=0)
    await _save(p3c_client, tenant, period, [
        {"driver_assignment_id": tenant.driver_a2, "values": {}, "status_key": "VAC",
         "notes": "family event"},
    ], offset=2)
    await _save(p3c_client, tenant, period, [
        {"driver_assignment_id": tenant.driver_a, "values": {ppd: "5"}, "notes": "extra route"},
    ], offset=4)

    overview = await _overview(p3c_client, tenant, period)

    assert [d["work_date"] for d in overview["days"]] == [_day(period, n) for n in range(7)]
    populated = 0
    for offset, day in enumerate(overview["days"]):
        single = await get_grid(p3c_client, tenant, period, work_date=_day(period, offset))
        assert day["rows"] == single["rows"], offset
        assert day["summary"] == single["summary"], offset
        assert overview["period"] == single["period"]
        assert overview["columns"] == single["columns"]
        assert overview["status_keys"] == single["status_keys"]
        assert overview["status_evidence"] == single["status_evidence"]
        populated += sum(1 for row in day["rows"] if row["values"] or row["status_key"] or row["notes"])
    assert populated == 4
    assert overview["days"][2]["summary"]["off"] == 1


async def test_a_finalized_period_overview_carries_the_status_evidence_of_the_day_grid(
    p3c_client, p3c_engine, tenant,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    await submit_and_approve(p3c_client, tenant, scenario.period)
    await finalize(p3c_client, tenant, scenario.period)
    period = scenario.period

    overview = await _overview(p3c_client, tenant, period)

    assert overview["status_evidence"] == {"state": "EMPTY", "reason_code": None}
    for offset, day in enumerate(overview["days"]):
        single = await get_grid(p3c_client, tenant, period, work_date=_day(period, offset))
        assert day["rows"] == single["rows"], offset
        assert day["summary"] == single["summary"], offset
        assert overview["status_evidence"] == single["status_evidence"]


# ---------------------------------------------------------------------------
# Configured days off
# ---------------------------------------------------------------------------

async def test_configured_day_off_flags_follow_the_frozen_period_days(
    p3c_client, p3c_engine, tenant,
):
    await rated_definition(p3c_client, tenant)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a, mask=WEEKEND_OFF)
    period = await create_period(p3c_client, tenant, tenant.branch_a)

    overview = await _overview(p3c_client, tenant, period)

    flags = {d["work_date"]: d["is_configured_off_day"] for d in overview["days"]}
    assert len(flags) == 7
    frozen = dict(query(
        tenant, "SELECT workdate::text, isconfiguredoffday FROM payroll.payrollperioddays "
                "WHERE payrollperiodid = %s", (period["payroll_period_id"],)))
    assert flags == frozen
    for work_date, is_off in flags.items():
        assert is_off is (date.fromisoformat(work_date).weekday() in (5, 6)), work_date
    # A configured day off is still a valid work date of the grid.
    saturday = _day(period, 5)
    assert (await get_grid(p3c_client, tenant, period, work_date=saturday))["work_date"] == saturday


async def test_no_day_is_a_configured_day_off_without_a_weekly_mask(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    overview = await _overview(p3c_client, tenant, period)
    assert [d["is_configured_off_day"] for d in overview["days"]] == [False] * 7


# ---------------------------------------------------------------------------
# Authorization parity with the single-date read
# ---------------------------------------------------------------------------

async def test_the_overview_is_authorized_like_the_single_date_day_grid(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    day_grid_url = f"/payroll/periods/{period['payroll_period_id']}/day-grid"
    viewer = make_user(tenant, ["payroll.view"])
    entry = make_user(tenant, ["payroll.entry"], scope="SpecificBranch", branch_id=tenant.branch_a)
    elsewhere = make_user(tenant, ["payroll.view", "payroll.entry"],
                          scope="SpecificBranch", branch_id=tenant.branch_b)
    nobody = make_user(tenant, [])
    driver = make_driver_self_user(tenant, ["payroll.view", "payroll.entry"])

    for headers in (tenant.admin, viewer, entry, elsewhere, nobody, driver):
        single = await p3c_client.get(day_grid_url, headers=headers)
        overview = await p3c_client.get(_overview_url(period), headers=headers)
        assert overview.status_code == single.status_code, (single.text, overview.text)

    assert (await p3c_client.get(_overview_url(period), headers=viewer)).status_code == 200
    assert (await p3c_client.get(_overview_url(period), headers=entry)).status_code == 200
    assert (await p3c_client.get(_overview_url(period), headers=nobody)).status_code == 403
    assert (await p3c_client.get(_overview_url(period), headers=driver)).status_code == 403
    assert (await p3c_client.get(_overview_url(period), headers=elsewhere)
            ).status_code in (403, 404)


async def test_an_unknown_period_has_no_overview(p3c_client, p3c_engine, tenant):
    response = await p3c_client.get(
        "/payroll/periods/2147483000/entry-overview", headers=tenant.admin)
    assert response.status_code == 404
    assert (await p3c_client.get(
        "/payroll/periods/2147483000/day-grid", headers=tenant.admin)).status_code == 404


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------

_FINGERPRINTS = {
    "draft lines": "SELECT count(*), md5(COALESCE(string_agg(t::text, '|' ORDER BY t::text), '')) "
                   "FROM payroll.payrolldraftlines t WHERE payrollperiodid = %s",
    "entry state": "SELECT count(*), md5(COALESCE(string_agg(t::text, '|' ORDER BY t::text), '')) "
                   "FROM payroll.payrollperioddriverdayentrystate t WHERE payrollperiodid = %s",
    "period": "SELECT count(*), md5(COALESCE(string_agg(t::text, '|' ORDER BY t::text), '')) "
              "FROM payroll.payrollperiods t WHERE payrollperiodid = %s",
    "period days": "SELECT count(*), md5(COALESCE(string_agg(t::text, '|' ORDER BY t::text), '')) "
                   "FROM payroll.payrollperioddays t WHERE payrollperiodid = %s",
}


def _fingerprint(tenant, period_id) -> dict:
    state = {name: query(tenant, sql, (period_id,)) for name, sql in _FINGERPRINTS.items()}
    state["audit"] = query(
        tenant, "SELECT count(*) FROM audit.auditlog WHERE companyid = %s", (tenant.company_id,))
    return state


async def test_reading_the_overview_writes_nothing(p3c_client, p3c_engine, tenant):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    ppd = str(grid["columns"][0]["payroll_period_definition_id"])
    await _save(p3c_client, tenant, period, [
        {"driver_assignment_id": tenant.driver_a, "values": {ppd: "4"}, "notes": "kept"}])
    period_id = period["payroll_period_id"]
    before = _fingerprint(tenant, period_id)

    first = await _overview(p3c_client, tenant, period)
    second = await _overview(p3c_client, tenant, period)

    assert _fingerprint(tenant, period_id) == before
    assert first == second
