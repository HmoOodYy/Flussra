"""Fixtures for the target period runtime (P4b) tests.

Built on the disposable database and tenant helpers of the target Compensation tests:
each test owns a Company with Branches, Drivers, a Payroll Setup assignment and the
payroll permissions it needs, so nothing touches the shared application test data.
Periods and approved assignments are intentionally undeletable, so nothing is cleaned.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import httpx
import psycopg2
from sqlalchemy import text

from tests.p3b_fixtures import make_driver
from tests.p3c_fixtures import (
    PERMISSIONS,
    Tenant,
    approve_assignment,
    build_tenant,
    create_definition,
    create_pending,
    grant_applicability,
    set_scalar_value,
)

PAYROLL_PERMISSIONS = PERMISSIONS + [
    "payroll.view", "payroll.entry", "payroll.period.create", "payroll.finalize",
    "reports.view", "payroll_setup.manage", "payroll_setup.publish", "payroll_setup.assign",
    "payroll_setup.view",
]

def setup_anchor() -> date:
    """The Monday of last week: onboarding allows a start within two periods of today."""
    today = date.today()
    return today - timedelta(days=today.weekday()) - timedelta(days=7)


def build_payroll_tenant(dsn: dict, *, currency: str | None = "USD") -> Tenant:
    return build_tenant(dsn, currency=currency, permissions=PAYROLL_PERMISSIONS)


async def assign_weekly_setup(engine, tenant: Tenant, branch_id: int, *, mask: int = 0) -> None:
    """Give the Branch a published weekly Payroll Setup (the period creation authority)."""
    from app.payroll_setup.payroll_policy import (
        assign_setup,
        create_draft,
        create_setup,
        publish_version,
    )

    anchor = setup_anchor()
    async with engine.begin() as conn:
        setup_id = await create_setup(
            tenant.company_id, tenant.owner, None, f"P4B {branch_id}", conn)
        draft_id = await create_draft(
            tenant.company_id, tenant.owner, setup_id, conn,
            payroll_frequency="Week", anchor_start_date=anchor,
            normal_days_off_mask=mask)
        await publish_version(
            tenant.company_id, tenant.owner, setup_id, draft_id, anchor, conn)
        await assign_setup(
            tenant.company_id, tenant.owner, branch_id, setup_id, anchor, conn)


async def create_period(
    client: httpx.AsyncClient, tenant: Tenant, branch_id: int, *, mode: str = "OPEN_CREATION",
) -> dict:
    """Create the next period of the Branch through the canonical candidate flow."""
    headers = tenant.admin
    candidates = await client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": mode}, headers=headers)
    assert candidates.status_code == 200, candidates.text
    key = candidates.json()["selected"]["candidate_key"]
    created = await client.post(
        f"/payroll/branches/{branch_id}/period-creations",
        json={"candidate_key": key}, headers=headers)
    assert created.status_code in (200, 201), created.text
    return created.json()


def add_driver(tenant: Tenant) -> int:
    """A further Active Driver in the Tenant's first Branch."""
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            return make_driver(cur, tenant.company_id, tenant.branch_a)
    finally:
        conn.close()


async def rated_definition(
    client: httpx.AsyncClient, tenant: Tenant, *, rate: str | None = "25",
    effective_from: str = "2020-01-01", activate: bool = True, branch_id: int | None = None,
    driver_id: int | None = None, **overrides,
) -> dict:
    """An arbitrary PayDefinition, applicable to the Branch, with an approved scalar rate.

    ``rate=None`` leaves the Driver without any assignment (a missing rate).
    """
    definition = await create_definition(client, tenant, **overrides)
    if activate:
        grant_applicability(tenant, definition, branch_id or tenant.branch_a)
    if rate is not None:
        await approve_rate(
            client, tenant, definition, rate, effective_from=effective_from,
            driver_id=driver_id)
    return definition


async def approve_rate(
    client: httpx.AsyncClient, tenant: Tenant, definition: dict, amount: str, *,
    effective_from: str, effective_to: str | None = None, driver_id: int | None = None,
) -> dict:
    pending = await create_pending(
        client, tenant, definition, driver_id=driver_id,
        effective_from=effective_from, effective_to=effective_to)
    filled = await set_scalar_value(client, tenant, pending, definition, amount)
    assert filled.status_code == 200, filled.text
    approved = await approve_assignment(client, tenant, pending)
    assert approved.status_code == 200, approved.text
    return approved.json()


def period_definitions(period_id: int, tenant: Tenant) -> list[dict]:
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT payrollperioddefinitionid, paydefinitionid, definitioncodesnapshot,
                       isactiveinperiod
                FROM payroll.payrollperioddefinitions
                WHERE payrollperiodid = %s ORDER BY sortorder, payrollperioddefinitionid
            """, (period_id,))
            return [
                {"payroll_period_definition_id": r[0], "pay_definition_id": r[1],
                 "code": r[2], "is_active": r[3]}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def day_after(start: date, days: int) -> date:
    return start + timedelta(days=days)


async def sql(engine, statement: str, **params):
    async with engine.begin() as conn:
        return (await conn.execute(text(statement), params)).all()


def money(value) -> Decimal:
    return Decimal(str(value))

