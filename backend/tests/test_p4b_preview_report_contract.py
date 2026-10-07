"""Calculation preview and live report contract that survives the P4b target cutover.

Lifecycle availability, access boundary, driver union with Bonus, read-only behavior and
the preview/report reconciliation. Per-definition money is covered in
test_p4b_live_calculation; frozen-evidence authority is deferred to the evidence work unit
and fails closed meanwhile.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
)
from tests.p4b_fixtures import (
    assign_weekly_setup,
    build_payroll_tenant,
    create_period,
    force_status,
    get_grid,
    make_driver_self_user,
    make_user,
    open_period_with_grid,
    query,
    rated_definition,
)

pytestmark = pytest.mark.asyncio

REPORTS = ["drivers", "period-work", "period-pay", "mixed"]


@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


def _preview(period) -> str:
    return f"/payroll/periods/{period['payroll_period_id']}/calculation-preview"


def _report(period, name) -> str:
    return f"/payroll/periods/{period['payroll_period_id']}/reports/{name}"


async def _entered(client, engine, tenant, quantity="8"):
    period, grid = await open_period_with_grid(client, engine, tenant)
    ppd = grid["columns"][0]["payroll_period_definition_id"]
    saved = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": period["start_date"], "rows": [
            {"driver_id": tenant.driver_a, "values": {str(ppd): quantity}}]},
        headers=tenant.admin)
    assert saved.status_code == 200, saved.text
    return period, ppd


# ---------------------------------------------------------------------------
# Calculation preview
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    ("Open", 200), ("Returned", 200), ("Draft", 422), ("InReview", 422),
    ("Approved", 422), ("Locked", 422), ("Cancelled", 422)])
async def test_the_preview_is_available_only_for_open_and_returned_periods(
    p3c_client, p3c_engine, tenant, status, expected,
):
    period, _ = await _entered(p3c_client, p3c_engine, tenant)
    if status != "Open":
        force_status(tenant, period["payroll_period_id"], status)
    response = await p3c_client.get(_preview(period), headers=tenant.admin)
    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json()["provisional"] is True


async def test_the_preview_requires_view_or_entry_and_denies_everyone_else(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _entered(p3c_client, p3c_engine, tenant)
    allowed = [make_user(tenant, ["payroll.view"]), make_user(tenant, ["payroll.entry"])]
    denied = {
        "finalize only": make_user(tenant, ["payroll.finalize"]),
        "no permission": make_user(tenant, []),
        "other branch": make_user(tenant, ["payroll.view", "payroll.entry"],
                                  scope="SpecificBranch", branch_id=tenant.branch_b),
        "driver self": make_driver_self_user(tenant, ["payroll.view", "payroll.entry"]),
        "other company": build_payroll_tenant(tenant.dsn).admin,
    }
    for headers in allowed:
        assert (await p3c_client.get(_preview(period), headers=headers)).status_code == 200
    for label, headers in denied.items():
        response = await p3c_client.get(_preview(period), headers=headers)
        assert response.status_code in (403, 404), (label, response.status_code)


async def test_the_preview_never_writes(p3c_client, p3c_engine, tenant):
    period, _ = await _entered(p3c_client, p3c_engine, tenant)
    tables = ["payroll.payrolldraftlines", "payroll.payrollperiods",
              "payroll.payrollperioddefinitions", "payroll.payrollcalculationsnapshots",
              "payroll.payrollfinallines", "review.managerreviewitems", "audit.auditlog"]

    def counts():
        return [query(tenant, f"SELECT count(*) FROM {table}")[0][0] for table in tables]

    before = counts()
    assert (await p3c_client.get(_preview(period), headers=tenant.admin)).status_code == 200
    assert counts() == before


async def test_bonus_events_join_the_preview_once_and_voided_ones_drop_out(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _entered(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]

    async def drivers():
        preview = (await p3c_client.get(_preview(period), headers=tenant.admin)).json()
        return {d["driver_id"]: d for d in preview["drivers"]}, preview

    first = await p3c_client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": tenant.driver_a, "amount": "15.5"}, headers=tenant.admin)
    second = await p3c_client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": tenant.driver_a2, "amount": "7"}, headers=tenant.admin)
    assert first.status_code == second.status_code == 201
    by_driver, preview = await drivers()
    # A bonus-only driver is included once; a multi-source driver is still a single row.
    assert sorted(by_driver) == sorted([tenant.driver_a, tenant.driver_a2])
    assert len(preview["drivers"]) == 2
    assert Decimal(by_driver[tenant.driver_a]["expected_pay"]) == Decimal("215.5")
    assert Decimal(by_driver[tenant.driver_a2]["expected_pay"]) == Decimal("7")
    assert Decimal(preview["total_expected_pay"]) == Decimal("222.5")

    voided = await p3c_client.delete(
        f"/payroll/periods/{period_id}/bonuses/{second.json()['bonus_event_id']}",
        headers=tenant.admin)
    assert voided.status_code == 200
    by_driver, preview = await drivers()
    assert sorted(by_driver) == [tenant.driver_a]
    assert Decimal(preview["total_expected_pay"]) == Decimal("215.5")


async def test_an_eligible_driver_without_financial_source_is_not_in_the_preview(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _entered(p3c_client, p3c_engine, tenant)
    preview = (await p3c_client.get(_preview(period), headers=tenant.admin)).json()
    assert [d["driver_id"] for d in preview["drivers"]] == [tenant.driver_a]


# ---------------------------------------------------------------------------
# Live reports
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", REPORTS)
async def test_live_reports_reconcile_with_the_preview(p3c_client, p3c_engine, tenant, name):
    period, ppd = await _entered(p3c_client, p3c_engine, tenant, "3")
    await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/bonuses",
        json={"driver_id": tenant.driver_a, "amount": "1.2345"}, headers=tenant.admin)
    preview = (await p3c_client.get(_preview(period), headers=tenant.admin)).json()
    report = (await p3c_client.get(_report(period, name), headers=tenant.admin)).json()
    assert report["metadata"]["authority_kind"] == "LIVE"
    assert report["metadata"]["financials_available"] is True
    assert [c["payroll_period_definition_id"] for c in report["columns"]] == [ppd]
    driver = report["drivers"][0]
    assert Decimal(driver["pay"]["total_pay"]) == Decimal(preview["total_expected_pay"])
    assert Decimal(driver["pay"]["bonus_total"]) == Decimal("1.2345")
    if name != "period-work":      # the work report carries quantities only
        assert [Decimal(a["amount"]) for a in driver["pay"]["definition_amounts"]]             == [Decimal("75")]
    assert Decimal(driver["work"]["daily_rows"][0]["quantity"]) == 3


@pytest.mark.parametrize("name", REPORTS)
async def test_report_routes_enforce_the_access_boundary(
    p3c_client, p3c_engine, tenant, name,
):
    period, _ = await _entered(p3c_client, p3c_engine, tenant)
    reports_only = make_user(tenant, ["reports.view"])
    assert (await p3c_client.get(_report(period, name), headers=reports_only)).status_code == 200
    denied = {
        "no reports.view": make_user(tenant, ["payroll.view", "payroll.entry"]),
        "no permission": make_user(tenant, []),
        "driver self": make_driver_self_user(tenant, ["reports.view", "payroll.view"]),
        "other branch": make_user(tenant, ["reports.view"], scope="SpecificBranch",
                                  branch_id=tenant.branch_b),
        "other company": build_payroll_tenant(tenant.dsn).admin,
    }
    for label, headers in denied.items():
        response = await p3c_client.get(_report(period, name), headers=headers)
        assert response.status_code in (403, 404), (label, response.status_code)
        # A denial never echoes the identity of the period.
        assert period["period_code"] not in response.text, label


@pytest.mark.parametrize("status,code", [
    ("Draft", "REPORT_FINANCIALS_UNAVAILABLE"), ("Cancelled", "REPORT_UNAVAILABLE")])
async def test_reports_of_prepared_and_cancelled_periods_stay_closed(
    p3c_client, p3c_engine, tenant, status, code,
):
    period, _ = await _entered(p3c_client, p3c_engine, tenant)
    force_status(tenant, period["payroll_period_id"], status)
    for name in REPORTS:
        response = await p3c_client.get(_report(period, name), headers=tenant.admin)
        if response.status_code == 200:
            # Prepared periods may serve the operational (work-only) view, never money.
            metadata = response.json()["metadata"]
            assert status == "Draft" and metadata["financials_available"] is False, name
            assert response.json()["pay_totals"] is None, name
        else:
            assert response.status_code == 422 and code in response.text, (name, response.text)
    pay = await p3c_client.get(_report(period, "period-pay"), headers=tenant.admin)
    assert pay.status_code == 422 and code in pay.text


@pytest.mark.parametrize("status", ["InReview", "Approved", "Locked"])
async def test_frozen_report_authorities_fail_closed_until_evidence_exists(
    p3c_client, p3c_engine, tenant, status,
):
    """No submitted/approved/final evidence exists for target periods: never rebuilt live."""
    period, _ = await _entered(p3c_client, p3c_engine, tenant)
    force_status(tenant, period["payroll_period_id"], status)
    for name in REPORTS:
        response = await p3c_client.get(_report(period, name), headers=tenant.admin)
        assert response.status_code in (409, 422), (status, name, response.status_code)


async def test_the_work_report_columns_follow_the_frozen_period_definitions(
    p3c_client, p3c_engine, tenant,
):
    await rated_definition(p3c_client, tenant, definition_code="ZED", definition_name="Zed")
    await rated_definition(p3c_client, tenant, definition_code="ABE", definition_name="Abe")
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    grid = await get_grid(p3c_client, tenant, period)
    report = (await p3c_client.get(_report(period, "period-work"), headers=tenant.admin)).json()
    assert [c["payroll_period_definition_id"] for c in report["columns"]] \
        == [c["payroll_period_definition_id"] for c in grid["columns"]]
    assert [c["label"] for c in report["columns"]] == ["Abe", "Zed"]
