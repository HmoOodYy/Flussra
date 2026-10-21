"""Final-ledger integrity and read boundary that survive the P4b cutover.

Finalization itself is unavailable until the evidence work unit, but the database
guarantees around PayrollFinalLines and Locked periods, and the ledger read boundary,
are independent of how a ledger row is produced. Rows here are seeded the way the
controlled finalization path writes them (through the insert-guard setting).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.builders.final_evidence import insert_final_line, seed_snapshot_component
from tests.calculation_snapshot_helpers import finalize, standard_scenario, submit_and_approve
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
    build_payroll_tenant,
    force_status,
    make_driver_self_user,
    make_user,
    open_period_with_grid,
    query,
)

pytestmark = pytest.mark.asyncio

@pytest.fixture(name="tenant")
def payroll_tenant(p3b_dsn):
    return build_payroll_tenant(p3b_dsn)


async def _insert_final_line(engine, tenant, period_id: int, **chain_options) -> int:
    """The controlled write path: a snapshot component and the FinalLine projecting it."""
    async with engine.begin() as conn:
        chain = await seed_snapshot_component(
            conn, period_id=period_id, driver_assignment_id=tenant.driver_a, **chain_options)
        return await insert_final_line(conn, chain)


async def _finalized_period(client, engine, tenant) -> dict:
    """A period finalized through the real lifecycle: verifiable snapshot and FinalLines."""
    scenario = await standard_scenario(client, engine, tenant)
    await submit_and_approve(client, tenant, scenario.period)
    await finalize(client, tenant, scenario.period)
    return scenario.period


async def _locked_period(client, engine, tenant) -> tuple[dict, int]:
    period, _ = await open_period_with_grid(client, engine, tenant)
    period_id = period["payroll_period_id"]
    force_status(tenant, period_id, "Approved")
    line_id = await _insert_final_line(engine, tenant, period_id)
    force_status(tenant, period_id, "Locked")
    return period, line_id


# ---------------------------------------------------------------------------
# Database guarantees
# ---------------------------------------------------------------------------

async def test_final_lines_can_only_be_inserted_through_the_controlled_path(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    async with p3c_engine.begin() as conn:
        chain = await seed_snapshot_component(
            conn, period_id=period_id, driver_assignment_id=tenant.driver_a)
    for status in ("Open", "Locked"):
        if status == "Locked":
            force_status(tenant, period_id, "Locked")
        async with p3c_engine.connect() as conn:
            with pytest.raises(DBAPIError) as refused:
                await conn.execute(text(
                    "INSERT INTO payroll.payrollfinallines "
                    "(companyid, branchid, payrollperiodid, driverassignmentid, driverid, employeeid, "
                    "linetype, linescope, finalamount, sourcetype, sourceid, sourcesnapshot, "
                    "currencycode, currencyminorunitdigits, payrollcalculationsnapshotid, "
                    "payrollcalculationsnapshotcomponentid, componentkind) "
                    "VALUES (:company, :branch, :period, :assignment, :driver, :employee, 'BONUS', "
                    "'Period', 100, 'SnapshotComponent', 'x', '{}'::jsonb, 'USD', 2, :snapshot, "
                    ":component, 'BONUS')"), {**chain.params, "snapshot": chain.snapshot_id,
                                              "component": chain.component_id})
            assert "payroll_insert_guard" in str(refused.value)
    assert query(tenant, "SELECT count(*) FROM payroll.payrollfinallines "
                         "WHERE payrollperiodid = %s", (period_id,)) == [(0,)]


async def test_locked_period_final_lines_cannot_be_updated_or_deleted(
    p3c_client, p3c_engine, tenant,
):
    period, line_id = await _locked_period(p3c_client, p3c_engine, tenant)
    with pytest.raises(psycopg2.errors.RestrictViolation):
        query(tenant, "UPDATE payroll.payrollfinallines SET finalamount = 1 "
                      "WHERE finallineid = %s", (line_id,))
    with pytest.raises(psycopg2.errors.RestrictViolation):
        query(tenant, "DELETE FROM payroll.payrollfinallines WHERE finallineid = %s", (line_id,))
    assert query(tenant, "SELECT finalamount FROM payroll.payrollfinallines "
                         "WHERE finallineid = %s", (line_id,)) == [(Decimal("100"),)]


async def test_a_locked_period_only_moves_forward_to_archived_and_archived_is_terminal(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _locked_period(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    for status in ("Open", "Draft", "Approved", "Cancelled"):
        with pytest.raises(psycopg2.errors.RestrictViolation):
            query(tenant, "UPDATE payroll.payrollperiods SET status = %s "
                          "WHERE payrollperiodid = %s", (status, period_id))
    query(tenant, "UPDATE payroll.payrollperiods SET status = 'Archived' "
                  "WHERE payrollperiodid = %s", (period_id,))
    with pytest.raises(psycopg2.errors.RestrictViolation):
        query(tenant, "UPDATE payroll.payrollperiods SET status = 'Locked' "
                      "WHERE payrollperiodid = %s", (period_id,))


# ---------------------------------------------------------------------------
# Ledger read boundary
# ---------------------------------------------------------------------------

async def test_final_lines_are_served_for_a_finalized_period(
    p3c_client, p3c_engine, tenant,
):
    period = await _finalized_period(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    served = await p3c_client.get(f"/payroll/periods/{period_id}/final-lines",
                                  headers=tenant.admin)
    assert served.status_code == 200, served.text
    lines = served.json()
    assert [Decimal(line["final_amount"]) for line in lines] == [
        Decimal("0.67"), Decimal("2.00"), Decimal("39.33")]
    assert all(line["currency_code"] == "USD" and line["currency_minor_unit_digits"] == 2
               for line in lines)
    filtered = await p3c_client.get(
        f"/payroll/periods/{period_id}/final-lines",
        params={"driver_assignment_id": tenant.driver_a2}, headers=tenant.admin)
    assert filtered.json() == []
    listed = await p3c_client.get("/payroll/periods", params={"status": "Locked"},
                                  headers=tenant.admin)
    summary = next(p for p in listed.json() if p["payroll_period_id"] == period_id)
    assert Decimal(summary["final_gross"]) == Decimal("42.00")
    assert summary["final_lines"] == 3 and summary["final_driver_count"] == 1
    assert summary["financial_authority"] == "FROZEN"
    assert (await p3c_client.get("/payroll/periods/987654/final-lines",
                                 headers=tenant.admin)).status_code == 404


async def test_final_lines_are_not_served_for_a_period_that_is_not_finalized(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    for status in ("Open", "Approved"):
        if status != "Open":
            force_status(tenant, period_id, status)
        response = await p3c_client.get(f"/payroll/periods/{period_id}/final-lines",
                                        headers=tenant.admin)
        assert response.status_code == 422, (status, response.text)


async def test_final_lines_and_finalized_routes_enforce_the_access_boundary(
    p3c_client, p3c_engine, tenant,
):
    period = await _finalized_period(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    final_lines = f"/payroll/periods/{period_id}/final-lines"
    finalized = [f"/payroll/finalized/{period_id}/overview",
                 f"/payroll/finalized/{period_id}/reports/period-pay",
                 f"/payroll/finalized/{period_id}/off-drivers",
                 f"/payroll/finalized/{period_id}/rates-used",
                 f"/payroll/finalized/{period_id}/audit"]

    viewer = make_user(tenant, ["payroll.view"])
    assert (await p3c_client.get(final_lines, headers=viewer)).status_code == 200
    denied = {
        "no permission": make_user(tenant, []),
        "driver self": make_driver_self_user(tenant, ["payroll.view", "ledger.view"]),
        "other branch": make_user(tenant, ["payroll.view"], scope="SpecificBranch",
                                  branch_id=tenant.branch_b),
        "other company": build_payroll_tenant(tenant.dsn).admin,
    }
    for label, headers in denied.items():
        for url in [final_lines, *finalized]:
            response = await p3c_client.get(url, headers=headers)
            assert response.status_code in (403, 404), (label, url, response.status_code)
            assert period["period_code"] not in response.text, (label, url)

    # The finalized library needs the ledger permission, not the operational one.
    no_ledger = make_user(tenant, ["payroll.view", "payroll.entry", "reports.view"])
    for url in finalized:
        assert (await p3c_client.get(url, headers=no_ledger)).status_code == 403, url
    ledger = make_user(tenant, ["ledger.view", "ledger.audit.view"])
    for url in finalized:
        response = await p3c_client.get(url, headers=ledger)
        assert response.status_code in (200, 409, 422), (url, response.status_code)
        assert "payitem" not in response.text.lower() or response.status_code != 200


async def test_finalized_audit_route_uses_the_audit_owner_and_its_own_permission(
    p3c_client, p3c_engine, tenant, monkeypatch,
):
    scenario = await standard_scenario(p3c_client, p3c_engine, tenant)
    period = scenario.period
    await submit_and_approve(p3c_client, tenant, period)
    await finalize(p3c_client, tenant, period)
    from app.payroll import finalized_audit

    owner_calls = []
    original = finalized_audit.build_finalized_audit

    async def recording_owner(**kwargs):
        owner_calls.append(kwargs["period_id"])
        return await original(**kwargs)

    monkeypatch.setattr(finalized_audit, "build_finalized_audit", recording_owner)
    url = f"/payroll/finalized/{period['payroll_period_id']}/audit"

    ledger_viewer = make_user(tenant, ["ledger.view"])
    assert (await p3c_client.get(url, headers=ledger_viewer)).status_code == 403
    auditor = make_user(tenant, ["ledger.view", "ledger.audit.view"])
    response = await p3c_client.get(url, headers=auditor)
    assert response.status_code == 200, response.text
    assert response.json()["metadata"]["authority_kind"] == "IMMUTABLE_PERIOD_AUDIT_EVIDENCE"
    assert owner_calls == [period["payroll_period_id"]] * 2


async def test_finalized_period_discovery_is_minimal_and_scoped(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await _locked_period(p3c_client, p3c_engine, tenant)
    ledger = make_user(tenant, ["ledger.view"])
    discovered = await p3c_client.get("/payroll/finalized", headers=ledger)
    assert discovered.status_code == 200
    items = discovered.json()
    assert period["payroll_period_id"] in [
        item.get("payroll_period_id", item.get("period_id")) for item in items]
    # Only minimal identification: no money, no per-driver detail.
    for item in items:
        assert "final_gross" not in item and "drivers" not in item
    elsewhere = make_user(tenant, ["ledger.view"], scope="SpecificBranch",
                          branch_id=tenant.branch_b)
    other = await p3c_client.get("/payroll/finalized", headers=elsewhere)
    assert other.status_code == 200
    assert period["payroll_period_id"] not in [
        item.get("payroll_period_id", item.get("period_id")) for item in other.json()]
    assert (await p3c_client.get("/payroll/finalized", headers=make_user(tenant, ["payroll.view"])
                                 )).status_code == 403
    assert (await p3c_client.get(
        "/payroll/finalized", headers=make_driver_self_user(tenant, ["ledger.view"])
    )).status_code == 403


# ---------------------------------------------------------------------------
# A DriverRate referenced by the final ledger is protected (Status rates still use them)
# ---------------------------------------------------------------------------

def _driver_rate(tenant, driver_assignment_id: int) -> int:
    rate_type_id = query(tenant, "SELECT ratetypeid FROM payroll.ratetypes "
                                 "WHERE companyid IS NULL ORDER BY ratetypeid LIMIT 1")[0][0]
    return query(tenant, """
        INSERT INTO payroll.driverrates
            (companyid, branchid, driverassignmentid, ratetypeid, amount, effectivefrom)
        VALUES (%s, %s, %s, %s, 15, DATE '2000-01-01') RETURNING driverrateid
    """, (tenant.company_id, tenant.branch_a, driver_assignment_id, rate_type_id))[0][0]


async def test_a_driver_rate_used_by_a_final_line_cannot_be_voided_changed_or_deleted(
    p3c_client, p3c_engine, tenant,
):
    period, _ = await open_period_with_grid(p3c_client, p3c_engine, tenant)
    period_id = period["payroll_period_id"]
    used, unused = _driver_rate(tenant, tenant.driver_a), _driver_rate(tenant, tenant.driver_a2)
    force_status(tenant, period_id, "Approved")
    # The finalized snapshot records the Status rate its line used (the used-rate evidence).
    await _insert_final_line(
        p3c_engine, tenant, period_id, amount=Decimal("15"), work_date=date(2000, 1, 5),
        driver_rate_id=used)

    with pytest.raises(psycopg2.errors.RestrictViolation, match="driverrate_void_guard"):
        query(tenant, "UPDATE payroll.driverrates SET status = 'Voided' WHERE driverrateid = %s",
              (used,))
    with pytest.raises(psycopg2.errors.RestrictViolation):
        query(tenant, "UPDATE payroll.driverrates SET amount = 99 WHERE driverrateid = %s",
              (used,))
    with pytest.raises(psycopg2.errors.RestrictViolation):
        query(tenant, "DELETE FROM payroll.driverrates WHERE driverrateid = %s", (used,))
    assert query(tenant, "SELECT amount FROM payroll.driverrates WHERE driverrateid = %s",
                 (used,)) == [(Decimal("15"),)]

    # An unreferenced rate stays fully mutable.
    query(tenant, "UPDATE payroll.driverrates SET amount = 20 WHERE driverrateid = %s", (unused,))
    query(tenant, "UPDATE payroll.driverrates SET status = 'Voided' WHERE driverrateid = %s",
          (unused,))
    assert query(tenant, "SELECT status FROM payroll.driverrates WHERE driverrateid = %s",
                 (unused,)) == [("Voided",)]
