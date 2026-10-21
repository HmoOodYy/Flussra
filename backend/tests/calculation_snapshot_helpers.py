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
    seed_status_rate,
    set_employment,
    setup_anchor,
    status_column_of,
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
    """A paid (non-off) Status key worth one hour bound to the Company default Status rate
    column, and the Driver's Status rate schedule. Returns (column id, schedule id)."""
    column_id, _, _ = status_column_of(tenant)
    seed_status_key(tenant, "PAID", off=False, hoursvalue=D("1"), statusratecolumnid=column_id)
    return column_id, seed_status_rate(tenant, D(rate))


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


# ---------------------------------------------------------------------------
# Lifecycle: submit -> review decision -> finalize, through the real routes
# ---------------------------------------------------------------------------

def allow_self_approval(tenant) -> None:
    """Let the submitting user decide, so one tenant user drives the whole lifecycle."""
    query(tenant, "UPDATE core.companies SET allowselfapproval = TRUE WHERE companyid = %s",
          (tenant.company_id,))


async def submit(client, tenant, period, *, expect: int = 200):
    response = await client.patch(
        f"/payroll/periods/{period['payroll_period_id']}/status",
        json={"status": "InReview"}, headers=tenant.admin)
    assert response.status_code == expect, response.text
    return response


async def pending_review_item(tenant, period) -> int:
    ((item_id,),) = rows(tenant, """
        SELECT reviewitemid FROM review.managerreviewitems
        WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods' AND entityid = %s
          AND requesttype = 'PeriodApproval' AND status = 'Pending'
    """, str(period["payroll_period_id"]))
    return item_id


async def decide(client, tenant, item_id: int, decision: str = "Approved", *, expect: int = 200):
    response = await client.post(
        f"/review/items/{item_id}/decide",
        json={"decision": decision, "decision_reason": "reviewed"}, headers=tenant.admin)
    assert response.status_code == expect, response.text
    return response


async def finalize(client, tenant, period, *, expect: int = 200):
    response = await client.post(
        f"/payroll/periods/{period['payroll_period_id']}/finalize", headers=tenant.admin)
    assert response.status_code == expect, response.text
    return response


async def finalization_preview(client, tenant, period, *, expect: int = 200):
    response = await client.get(
        f"/payroll/periods/{period['payroll_period_id']}/finalization-preview",
        headers=tenant.admin)
    assert response.status_code == expect, response.text
    return response


async def submit_and_approve(client, tenant, period) -> int:
    """Submit, approve and return the review item id (the period is then Approved)."""
    allow_self_approval(tenant)
    await submit(client, tenant, period)
    item_id = await pending_review_item(tenant, period)
    await decide(client, tenant, item_id)
    return item_id


# ---------------------------------------------------------------------------
# A standard evidenced period and small lifecycle assertions
# ---------------------------------------------------------------------------

async def standard_scenario(client, engine, tenant) -> SimpleNamespace:
    """An Open period of a Driver hired on day 3 with two ordinary days, a Minimum rule that
    applies for 4/7 of the period and a Bonus: ordinary 0.67, top-up 39.33, Bonus 2.00."""
    period, ppd, definition = await open_period(
        client, engine, tenant, hire_offset=3, rate="0.3333",
        definition_code="HAUL", definition_name="Haul")
    for offset in (3, 4):
        await enter(client, tenant, period, offset, ppd=ppd, quantity="1")
    rule = await add_rule(client, tenant, period, "MinimumPay", "70")
    await add_bonus(client, tenant, period, "2.00")
    return SimpleNamespace(period=period, ppd=ppd, definition=definition, rule=rule,
                           period_id=period["payroll_period_id"])


def period_status(tenant, period) -> str:
    return rows(tenant, "SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = %s",
                period["payroll_period_id"])[0][0]


def period_evidence_counts(tenant, period) -> dict[str, int]:
    """Everything a refused or rolled-back lifecycle action must leave untouched."""
    pid = period["payroll_period_id"]
    scalar = lambda sql, *p: rows(tenant, sql, *p)[0][0]  # noqa: E731
    return {
        "snapshots": scalar("SELECT count(*) FROM payroll.payrollcalculationsnapshots "
                            "WHERE payrollperiodid = %s", pid),
        "final_lines": scalar("SELECT count(*) FROM payroll.payrollfinallines "
                              "WHERE payrollperiodid = %s", pid),
        "review_items": scalar("SELECT count(*) FROM review.managerreviewitems "
                               "WHERE entityname = 'PayrollPeriods' AND entityid = %s", str(pid)),
        "workflow_evidence": scalar("SELECT count(*) FROM payroll.payrollperiodworkflowactionevidence "
                                    "WHERE payrollperiodid = %s", pid),
    }


def snapshot_ids(tenant, period) -> list[int]:
    return [r[0] for r in rows(tenant, """
        SELECT payrollcalculationsnapshotid FROM payroll.payrollcalculationsnapshots
        WHERE payrollperiodid = %s ORDER BY revisionnumber
    """, period["payroll_period_id"])]


def bound_snapshot_id(tenant, item_id: int) -> int:
    return rows(tenant, "SELECT payrollcalculationsnapshotid FROM review.managerreviewitems "
                        "WHERE reviewitemid = %s", item_id)[0][0]


# ---------------------------------------------------------------------------
# Live-source mutation: nothing a snapshot froze may follow it
# ---------------------------------------------------------------------------

async def mutate_live_sources(engine, tenant, period) -> None:
    """Change every live source the frozen snapshot was built from: the ordinary rate and
    schedule values, the definition label, the Min/Max rule, the Bonus, the Driver's name and
    the day-grid quantities. Triggers are suppressed because the point is that frozen money
    does not read any of it."""
    from sqlalchemy import text

    from tests.db_state import replica_replication_role

    statements = (
        "UPDATE payroll.driverrateschedulevalues SET amount = amount * 7 WHERE amount IS NOT NULL",
        "UPDATE payroll.paydefinitions SET definitionname = 'Renamed after submission' "
        "WHERE companyid = :company",
        "UPDATE payroll.driverpayrules SET amount = 1, status = 'Voided' WHERE companyid = :company",
        "UPDATE payroll.payrollbonusevents SET amount = 999 WHERE companyid = :company",
        "UPDATE core.employees SET fullname = 'Renamed Person' WHERE companyid = :company",
        "UPDATE payroll.payrolldraftlines SET quantity = quantity * 10 "
        "WHERE payrollperiodid = :period",
    )
    async with engine.begin() as conn:
        async with replica_replication_role(conn):
            for statement in statements:
                await conn.execute(text(statement), {
                    "company": tenant.company_id, "period": period["payroll_period_id"]})
