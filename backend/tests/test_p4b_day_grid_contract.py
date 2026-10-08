"""Day Grid contract that survives the P4b target cutover.

Roster eligibility, date handling, quantity and Status validation, usage limits, audit
and the operational access boundary. Column identity and live money are covered in
test_p4b_source_lines and test_p4b_live_calculation.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    create_definition,
    grant_applicability,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.p4b_fixtures import (
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
    set_employment,
    setup_anchor,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _url(period) -> str:
    return f"/payroll/periods/{period['payroll_period_id']}/day-grid"


def _day(period, offset=0) -> str:
    return (date.fromisoformat(period["start_date"]) + timedelta(days=offset)).isoformat()


async def _save(client, tenant, period, rows, *, offset=0, headers=None):
    return await client.post(
        _url(period), json={"work_date": _day(period, offset), "rows": rows},
        headers=headers or tenant.admin)


def _row(driver_id, ppd=None, quantity=None, **extra) -> dict:
    values = {} if ppd is None else {str(ppd): quantity}
    return {"driver_id": driver_id, "values": values, **extra}


def _ppd(grid) -> int:
    return grid["columns"][0]["payroll_period_definition_id"]


def _row_of(grid, driver_id) -> dict:
    return next(r for r in grid["rows"] if r["driver_id"] == driver_id)


# ---------------------------------------------------------------------------
# Columns and roster
# ---------------------------------------------------------------------------

async def test_columns_are_exactly_the_definitions_active_for_the_branch(
    p3c_client, p3c_engine, tenant,
):
    await rated_definition(p3c_client, tenant, definition_code="ACTIVE_ONE",
                           definition_name="Active one")
    off = await create_definition(p3c_client, tenant, definition_code="OFF_HERE")
    grant_applicability(tenant, off, tenant.branch_a, active=False)
    await create_definition(p3c_client, tenant, definition_code="NOT_CONFIGURED")
    elsewhere = await create_definition(p3c_client, tenant, definition_code="OTHER_BRANCH")
    grant_applicability(tenant, elsewhere, tenant.branch_b)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    grid = await get_grid(p3c_client, tenant, period)
    assert [c["definition_code"] for c in grid["columns"]] == ["ACTIVE_ONE"]
    # Period-level scope has no meaning for the target model: nothing else is a column.
    assert all(c["calculation_method"] == "PerUnit" for c in grid["columns"])


async def test_the_roster_is_the_branch_drivers_only_and_excludes_inactive_ones(
    p3c_client, p3c_engine, tenant,
):
    set_employment(tenant, tenant.driver_a2, driver_status="Inactive")
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    ids = [r["driver_id"] for r in grid["rows"]]
    assert tenant.driver_a in ids
    assert tenant.driver_a2 not in ids          # driver profile not Active
    assert tenant.driver_b not in ids           # belongs to the other Branch
    assert grid["summary"]["total_drivers"] == len(ids)


async def test_hire_and_termination_dates_bound_the_roster_by_work_date(
    p3c_client, p3c_engine, tenant,
):
    start = setup_anchor()
    set_employment(tenant, tenant.driver_a, hire_date=start + timedelta(days=2),
                   termination_date=start + timedelta(days=4))
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    expected = {0: False, 1: False, 2: True, 3: True, 4: True, 5: False}
    for offset, present in expected.items():
        grid = await get_grid(p3c_client, tenant, period, work_date=_day(period, offset))
        assert (tenant.driver_a in [r["driver_id"] for r in grid["rows"]]) is present, offset


async def test_an_ineligible_driver_cannot_be_saved(p3c_client, p3c_engine, tenant):
    start = setup_anchor()
    set_employment(tenant, tenant.driver_a, hire_date=start + timedelta(days=3))
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    rejected = await _save(p3c_client, tenant, period,
                           [_row(tenant.driver_a, _ppd(grid), "4")])
    assert rejected.status_code == 422
    assert query(tenant, "SELECT count(*) FROM payroll.payrolldraftlines "
                         "WHERE payrollperiodid = %s", (period["payroll_period_id"],)) == [(0,)]


async def test_existing_source_survives_a_retroactive_termination_and_stays_visible(
    p3c_client, p3c_engine, tenant,
):
    start = setup_anchor()
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    ppd = _ppd(grid)
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "4")]
                        )).status_code == 200
    set_employment(tenant, tenant.driver_a, hire_date=start,
                   termination_date=start - timedelta(days=1), employment_status="Terminated")
    seen = await get_grid(p3c_client, tenant, period, work_date=_day(period))
    assert Decimal(_row_of(seen, tenant.driver_a)["values"][str(ppd)]["quantity"]) == 4
    assert query(tenant, "SELECT status FROM payroll.payrolldraftlines "
                         "WHERE payrollperiodid = %s", (period["payroll_period_id"],)
                 ) == [("Active",)]


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

async def test_work_dates_outside_the_period_are_rejected_and_the_default_date_is_in_range(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    for work_date in (_day(period, -1), _day(period, 7)):
        response = await p3c_client.get(_url(period), params={"work_date": work_date},
                                        headers=tenant.admin)
        assert response.status_code == 400
        assert (await _save(p3c_client, tenant, period, [], offset=-1)).status_code == 400
    default = await get_grid(p3c_client, tenant, period)
    assert period["start_date"] <= default["work_date"] <= period["end_date"]
    assert (await get_grid(p3c_client, tenant, period, work_date=_day(period, 3))
            )["work_date"] == _day(period, 3)


# ---------------------------------------------------------------------------
# Quantity validation
# ---------------------------------------------------------------------------

async def test_invalid_quantities_are_rejected_before_any_write(p3c_client, p3c_engine, tenant):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    ppd = _ppd(grid)
    period_id = period["payroll_period_id"]
    for bad in ("abc", "-1", "NaN", "1e400"):
        response = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, bad)])
        assert response.status_code == 422, (bad, response.text)
    # All-or-nothing: one invalid row rejects the whole request.
    mixed = await _save(p3c_client, tenant, period, [
        _row(tenant.driver_a, ppd, "3"), _row(tenant.driver_a2, ppd, "abc")])
    assert mixed.status_code == 422
    assert query(tenant, "SELECT count(*) FROM payroll.payrolldraftlines "
                         "WHERE payrollperiodid = %s", (period_id,)) == [(0,)]


async def test_decimal_quantities_are_stored_exactly_and_empty_or_zero_clears_the_line(
    p3c_client, p3c_engine, tenant,
):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    ppd = _ppd(grid)
    period_id = period["payroll_period_id"]
    saved = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "8.50")])
    assert saved.status_code == 200
    assert query(tenant, "SELECT quantity FROM payroll.payrolldraftlines "
                         "WHERE payrollperiodid = %s", (period_id,)) == [(Decimal("8.5"),)]
    for clearing in ("", "0"):
        await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "5")])
        cleared = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, clearing)])
        assert cleared.status_code == 200
        assert query(tenant, "SELECT count(*) FROM payroll.payrolldraftlines "
                             "WHERE payrollperiodid = %s AND status <> 'Void'",
                     (period_id,)) == [(0,)]


# ---------------------------------------------------------------------------
# Status keys
# ---------------------------------------------------------------------------

async def test_status_keys_are_validated_saved_and_cleared(p3c_client, p3c_engine, tenant):
    seed_status_key(tenant, "VAC", off=True)
    seed_status_key(tenant, "LATE", off=False)
    seed_status_key(tenant, "DEAD", off=True, active=False)
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    assert sorted(k["key_code"] for k in grid["status_keys"]) == ["LATE", "VAC"]

    for bad in ("MADE_UP", "DEAD"):
        assert (await _save(p3c_client, tenant, period,
                            [_row(tenant.driver_a, status_key=bad)])).status_code == 422
    saved = await _save(p3c_client, tenant, period, [
        _row(tenant.driver_a, status_key="VAC", notes="family event")])
    assert saved.status_code == 200, saved.text
    row = _row_of(saved.json(), tenant.driver_a)
    assert row["status_key"] == "VAC" and row["is_off"] is True and row["notes"] == "family event"
    assert saved.json()["summary"]["off"] == 1

    # A non-off key does not count as off; clearing removes the status.
    await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status_key="LATE")])
    after = await get_grid(p3c_client, tenant, period, work_date=_day(period))
    assert _row_of(after, tenant.driver_a)["status_key"] == "LATE"
    assert after["summary"]["off"] == 0
    cleared = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status_key=None)])
    assert _row_of(cleared.json(), tenant.driver_a)["status_key"] is None


@pytest.mark.parametrize("limit", [
    "limitusesperperiod", "limitusesperdriver", "limitusesacrossdrivers", "limitusesperday"])
async def test_status_key_usage_limits_are_enforced(p3c_client, p3c_engine, tenant, limit):
    seed_status_key(tenant, "LIM", **{limit + "enabled": True, limit: 1})
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    first = await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status_key="LIM")])
    assert first.status_code == 200, first.text
    if limit == "limitusesperday" or limit == "limitusesacrossdrivers":
        second = await _save(p3c_client, tenant, period,
                             [_row(tenant.driver_a2, status_key="LIM")])
    else:
        second = await _save(p3c_client, tenant, period,
                             [_row(tenant.driver_a, status_key="LIM")], offset=1)
    assert second.status_code == 422, (limit, second.text)
    # Re-saving the same driver and day is an update, not a second use.
    assert (await _save(p3c_client, tenant, period,
                        [_row(tenant.driver_a, status_key="LIM", notes="again")]
                        )).status_code == 200


async def test_a_batch_cannot_exceed_a_status_limit_and_clearing_frees_the_usage(
    p3c_client, p3c_engine, tenant,
):
    seed_status_key(tenant, "LIM", limitusesperperiodenabled=True, limitusesperperiod=1)
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    batch = await _save(p3c_client, tenant, period, [
        _row(tenant.driver_a, status_key="LIM"), _row(tenant.driver_a2, status_key="LIM")])
    assert batch.status_code == 422
    assert query(tenant, "SELECT count(*) FROM payroll.payrollperioddriverdayentrystate "
                         "WHERE payrollperiodid = %s", (period["payroll_period_id"],)) == [(0,)]
    assert (await _save(p3c_client, tenant, period,
                        [_row(tenant.driver_a, status_key="LIM")])).status_code == 200
    await _save(p3c_client, tenant, period, [_row(tenant.driver_a, status_key=None)])
    assert (await _save(p3c_client, tenant, period,
                        [_row(tenant.driver_a2, status_key="LIM")], offset=1)).status_code == 200


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _grid_audit(tenant, period_id) -> list[str]:
    return [r[0] for r in query(tenant, """
        SELECT actioncode FROM audit.auditlog
        WHERE entityschema = 'payroll' AND branchid IS NOT NULL
          AND (entityid IN (SELECT draftlineid::text FROM payroll.payrolldraftlines
                            WHERE payrollperiodid = %s)
               OR (entityname LIKE 'PayrollPeriodDriverDayEntryState%%'
                   AND newvaluejson::text LIKE %s))
        ORDER BY auditid""", (period_id, f'%"payroll_period_id": {period_id}%'))]


async def test_grid_mutations_write_audit_and_a_failed_save_writes_none(
    p3c_client, p3c_engine, tenant,
):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    ppd, period_id = _ppd(grid), period["payroll_period_id"]
    before = query(tenant, "SELECT count(*) FROM audit.auditlog")[0][0]
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "abc")]
                        )).status_code == 422
    assert query(tenant, "SELECT count(*) FROM audit.auditlog")[0][0] == before

    await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "4")])
    added = query(tenant, "SELECT count(*) FROM audit.auditlog")[0][0]
    assert added > before
    await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "0")])
    assert query(tenant, "SELECT count(*) FROM audit.auditlog")[0][0] > added
    actions = {r[0] for r in query(
        tenant, "SELECT DISTINCT actioncode FROM audit.auditlog WHERE companyid = %s",
        (tenant.company_id,))}
    assert {"DRAFT_LINE_ADDED", "DRAFT_LINE_VOIDED"} <= actions
    assert period_id


# ---------------------------------------------------------------------------
# Operational access boundary
# ---------------------------------------------------------------------------

async def test_driver_self_accounts_never_reach_the_day_grid(p3c_client, p3c_engine, tenant):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    ppd = _ppd(grid)
    driver = make_driver_self_user(tenant, ["payroll.view", "payroll.entry"])
    assert (await p3c_client.get(_url(period), headers=driver)).status_code == 403
    assert (await _save(p3c_client, tenant, period, [_row(tenant.driver_a, ppd, "4")],
                        headers=driver)).status_code == 403
    assert query(tenant, "SELECT count(*) FROM payroll.payrolldraftlines "
                         "WHERE payrollperiodid = %s", (period["payroll_period_id"],)) == [(0,)]


async def test_day_grid_reads_and_writes_need_the_matching_permission_and_branch_scope(
    p3c_client, p3c_engine, tenant,
):
    period, grid = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    ppd = _ppd(grid)
    viewer = make_user(tenant, ["payroll.view"])
    entry = make_user(tenant, ["payroll.entry"], scope="SpecificBranch", branch_id=tenant.branch_a)
    elsewhere = make_user(tenant, ["payroll.view", "payroll.entry"],
                          scope="SpecificBranch", branch_id=tenant.branch_b)
    nobody = make_user(tenant, [])
    row = [_row(tenant.driver_a, ppd, "2")]

    assert (await p3c_client.get(_url(period), headers=viewer)).status_code == 200
    assert (await _save(p3c_client, tenant, period, row, headers=viewer)).status_code == 403
    assert (await p3c_client.get(_url(period), headers=entry)).status_code == 200
    assert (await _save(p3c_client, tenant, period, row, headers=entry)).status_code == 200
    assert (await p3c_client.get(_url(period), headers=nobody)).status_code == 403
    assert (await p3c_client.get(_url(period), headers=elsewhere)).status_code in (403, 404)
    assert (await _save(p3c_client, tenant, period, row, headers=elsewhere)
            ).status_code in (403, 404)
