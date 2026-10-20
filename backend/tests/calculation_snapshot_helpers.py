"""Shared scaffolding for the immutable calculation snapshot tests.

Real tenants, real periods created by the canonical creator, real Day Grid entries and the real
pay-rule and Bonus routes: the snapshot is always captured from a genuine live calculation
packet. The writer is called directly inside a caller-owned transaction, exactly as the
lifecycle calls it.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.company_currency import get_company_currency
from app.payroll.calculation_snapshot import _capture_calculation_snapshot
from app.payroll.period_calculation import _build_live_calculation_packet
from app.payroll.period_read import get_period_by_id
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    create_period,
    get_grid,
    query,
    rated_definition,
    seed_status_key,
    set_employment,
    setup_anchor,
)

D = Decimal
WEEKEND_OFF = 65          # Sunday (bit 0) and Saturday (bit 6)

_NO_DEFINITION = "no definition"


def day(period: dict, offset: int = 0) -> str:
    return (date.fromisoformat(period["start_date"]) + timedelta(days=offset)).isoformat()


async def open_period(client, engine, tenant, *, mask: int = 0, hire_offset: int | None = None,
                      rate: str | None = _NO_DEFINITION, **definition) -> tuple[dict, int | None, dict | None]:
    """An Open Branch A period. With ``rate`` it also has one rated PayDefinition: returns the
    period, its frozen definition id and the PayDefinition (``rate=None``: no Driver rate)."""
    if hire_offset is not None:
        set_employment(tenant, tenant.driver_a,
                       hire_date=setup_anchor() + timedelta(days=hire_offset))
    pay_definition = None
    if rate != _NO_DEFINITION:
        pay_definition = await rated_definition(client, tenant, rate=rate, **definition)
    await assign_weekly_setup(engine, tenant, tenant.branch_a, mask=mask)
    period = await create_period(client, tenant, tenant.branch_a)
    if pay_definition is None:
        return period, None, None
    grid = await get_grid(client, tenant, period)
    return period, grid["columns"][0]["payroll_period_definition_id"], pay_definition


async def enter(client, tenant, period, offset, *, ppd=None, quantity=None, driver=None,
                status_key=None) -> None:
    row: dict[str, Any] = {"driver_assignment_id": driver or tenant.driver_a,
                           "values": {} if ppd is None else {str(ppd): quantity}}
    if status_key is not None:
        row["status_key"] = status_key
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": day(period, offset), "rows": [row]}, headers=tenant.admin)
    assert response.status_code == 200, response.text


async def add_rule(client, tenant, period, rule_type, amount) -> dict:
    response = await client.post(
        "/payroll/driver-pay-rules",
        json={"driver_id": tenant.driver_a_id, "rule_type": rule_type, "amount": amount,
              "effective_from": period["start_date"]},
        headers=tenant.admin)
    assert response.status_code == 201, response.text
    return response.json()


async def add_bonus(client, tenant, period, amount="2.00") -> dict:
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/bonuses",
        json={"driver_assignment_id": tenant.driver_a, "amount": amount}, headers=tenant.admin)
    assert response.status_code == 201, response.text
    return response.json()


def status_pay_setup(tenant, rate: str) -> tuple[int, int]:
    """A paid (non-off) Status key worth one hour, its Branch rate column and the Driver's
    assignment-owned Status rate. Returns (column id, driver rate id)."""
    (rate_type_id,) = query(tenant, "SELECT ratetypeid FROM payroll.ratetypes "
                                    "WHERE ratecode = 'STATUS_PAY'")[0]
    column_id = query(tenant, """
        INSERT INTO payroll.statusratecolumns
            (companyid, branchid, ratetypeid, columnname, normalizedcolumnname, isdefault, isactive)
        VALUES (%s, %s, %s, %s, %s, TRUE, TRUE) RETURNING statusratecolumnid
    """, (tenant.company_id, tenant.branch_a, rate_type_id, "Status pay", "STATUS PAY"))[0][0]
    seed_status_key(tenant, "PAID", off=False, hoursvalue=D("1"), statusratecolumnid=column_id)
    driver_rate_id = query(tenant, """
        INSERT INTO payroll.driverrates
            (companyid, branchid, driverassignmentid, ratetypeid, amount, effectivefrom, status,
             createdbyuserid, approvedbyuserid, approvedatutc)
        VALUES (%s, %s, %s, %s, %s, DATE '2000-01-01', 'Approved', %s, %s, NOW())
        RETURNING driverrateid
    """, (tenant.company_id, tenant.branch_a, tenant.driver_a, rate_type_id, D(rate),
          tenant.owner, tenant.owner))[0][0]
    return column_id, driver_rate_id


async def build_packet(engine, tenant, period):
    async with engine.begin() as conn:
        summary = await get_period_by_id(
            tenant.company_id, tenant.owner, period["payroll_period_id"], conn)
        return await _build_live_calculation_packet(summary, tenant.company_id, conn)


async def capture(engine, tenant, period) -> SimpleNamespace:
    """Build the live packet and freeze it, in one transaction, as submission does."""
    async with engine.begin() as conn:
        summary = await get_period_by_id(
            tenant.company_id, tenant.owner, period["payroll_period_id"], conn)
        currency = await get_company_currency(tenant.company_id, conn)
        packet = await _build_live_calculation_packet(summary, tenant.company_id, conn)
        snapshot_id = await _capture_calculation_snapshot(
            period=summary, company_id=tenant.company_id, user_id=tenant.owner,
            packet=packet, db=conn, context="Submit", currency=currency)
    return SimpleNamespace(snapshot_id=snapshot_id, packet=packet)


def rows(tenant, statement: str, *params) -> list[tuple]:
    return query(tenant, statement, params) or []


def snapshot_table_counts(tenant, snapshot_id: int) -> dict[str, int]:
    """Row counts of every table holding evidence of one snapshot."""
    queries = {
        "snapshots": "SELECT count(*) FROM payroll.payrollcalculationsnapshots "
                     "WHERE payrollcalculationsnapshotid = %s",
        "driver_totals": "SELECT count(*) FROM payroll.payrollcalculationdrivertotals "
                         "WHERE payrollcalculationsnapshotid = %s",
        "components": "SELECT count(*) FROM payroll.payrollcalculationsnapshotcomponents "
                      "WHERE payrollcalculationsnapshotid = %s",
        "evaluations": "SELECT count(*) FROM payroll.payrollcalculationsnapshotminmaxevaluations "
                       "WHERE payrollcalculationsnapshotid = %s",
        "rules": "SELECT count(*) FROM payroll.payrollcalculationsnapshotminmaxrules "
                 "WHERE payrollcalculationsnapshotid = %s",
        "lines": "SELECT count(*) FROM payroll.payrollcalculationsnapshotlines sl "
                 "JOIN payroll.payrollcalculationdrivertotals dt "
                 "ON dt.payrollcalculationdrivertotalid = sl.payrollcalculationdrivertotalid "
                 "WHERE dt.payrollcalculationsnapshotid = %s",
        "used_rates": "SELECT count(*) FROM payroll.payrollcalculationsnapshotusedratedefinitions "
                      "WHERE payrollcalculationsnapshotid = %s",
        "status_entries": "SELECT count(*) FROM payroll.payrollcalculationsnapshotstatusentries "
                          "WHERE payrollcalculationsnapshotid = %s",
        "bonus_events": "SELECT count(*) FROM payroll.payrollcalculationsnapshotbonusevents "
                        "WHERE payrollcalculationsnapshotid = %s",
    }
    return {name: query(tenant, sql, (snapshot_id,))[0][0] for name, sql in queries.items()}
