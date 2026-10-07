"""Fixtures for the target period runtime (P4b) tests.

Built on the disposable database and tenant helpers of the target Compensation tests:
each test owns a Company with Branches, Drivers, a Payroll Setup assignment and the
payroll permissions it needs, so nothing touches the shared application test data.
Periods and approved assignments are intentionally undeletable, so nothing is cleaned.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

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
    "payroll_setup.view", "review.decide", "ledger.view", "ledger.audit.view",
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



# ---------------------------------------------------------------------------
# Workflow helpers shared by the P4b contract tests
# ---------------------------------------------------------------------------

def _sql(tenant: Tenant, statement: str, params=()):
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def query(tenant: Tenant, statement: str, params=()):
    """Run one statement against the Tenant's database and return its rows (or None)."""
    return _sql(tenant, statement, params)


def make_user(
    tenant: Tenant, permissions: list[str], *, scope: str = "AllCompanyBranches",
    branch_id: int | None = None, role_code: str | None = None,
) -> dict[str, str]:
    """A further user with exactly the given permissions; returns its request headers."""
    from tests.p3c_fixtures import _role, _user

    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            role = _role(
                cur, tenant.company_id, role_code or "R" + uuid4().hex[:10], permissions)
            user = _user(cur, tenant.company_id, role, scope, branch_id)
    finally:
        conn.close()
    return tenant.headers(user)


def force_status(tenant: Tenant, period_id: int, status: str) -> None:
    """Put a period in a workflow state without running its path (test setup only).

    A Returned period gets the review item its pointer requires. The row guards are
    disabled for the statement only.
    """
    _sql(tenant, "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER USER")
    try:
        pointer = None
        if status == "Returned":
            pointer = _sql(tenant, """
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requesttype, entityschema, entityname, entityid,
                     title, status)
                SELECT companyid, branchid, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                       payrollperiodid::text, 'forced', 'Rejected'
                FROM payroll.payrollperiods WHERE payrollperiodid = %s
                RETURNING reviewitemid""", (period_id,))[0][0]
        _sql(tenant, "UPDATE payroll.payrollperiods SET status = %s, "
                     "currentreturnreviewitemid = %s WHERE payrollperiodid = %s",
             (status, pointer, period_id))
    finally:
        _sql(tenant, "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER USER")


def add_inreview_item(tenant: Tenant, period_id: int, *, requested_by: int | None = None) -> int:
    """A Pending PeriodApproval review item for a period already forced to InReview."""
    return _sql(tenant, """
        INSERT INTO review.managerreviewitems
            (companyid, branchid, requestedbyuserid, requesttype, entityschema, entityname,
             entityid, title, status)
        SELECT companyid, branchid, %s, 'PeriodApproval', 'payroll', 'PayrollPeriods',
               payrollperiodid::text, 'Period approval', 'Pending'
        FROM payroll.payrollperiods WHERE payrollperiodid = %s
        RETURNING reviewitemid""", (requested_by, period_id))[0][0]


async def open_period_with_grid(
    client: httpx.AsyncClient, engine, tenant: Tenant, *, rate: str | None = "25",
    **definition_overrides,
) -> tuple[dict, dict]:
    """An Open period of Branch A with one rated definition; returns (period, grid)."""
    await rated_definition(client, tenant, rate=rate, **definition_overrides)
    await assign_weekly_setup(engine, tenant, tenant.branch_a)
    period = await create_period(client, tenant, tenant.branch_a)
    grid = await get_grid(client, tenant, period)
    return period, grid


async def get_grid(
    client: httpx.AsyncClient, tenant: Tenant, period: dict, *, work_date: str | None = None,
    headers: dict | None = None,
) -> dict:
    params = {"work_date": work_date} if work_date else {}
    response = await client.get(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid", params=params,
        headers=headers or tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()


def seed_status_key(
    tenant: Tenant, code: str, *, branch_id: int | None = None, off: bool = True,
    active: bool = True, **limits,
) -> int:
    """A Branch PayrollStatusKey (``limits`` are the LimitUses* column values)."""
    columns = ["companyid", "branchid", "statuscode", "normalizedstatuscode", "keyname",
               "isoffreason", "isactive"]
    values = [tenant.company_id, branch_id or tenant.branch_a, code, code.upper(), code,
              off, active]
    for column, value in limits.items():
        columns.append(column)
        values.append(value)
    return _sql(tenant, f"""
        INSERT INTO payroll.payrollstatuskeys ({", ".join(columns)})
        VALUES ({", ".join(["%s"] * len(values))}) RETURNING statuskeyid
    """, values)[0][0]


def make_driver_self_user(tenant: Tenant, permissions: list[str]) -> dict[str, str]:
    """A DRIVER role user with Self scope: denied from operational surfaces whatever the
    permissions it holds."""
    return make_user(
        tenant, permissions, scope="Self", branch_id=None, role_code="DRIVER")


def set_employment(
    tenant: Tenant, driver_id: int, *, hire_date: date | None = None,
    termination_date: date | None = None, employment_status: str = "Active",
    driver_status: str = "Active",
) -> None:
    query(tenant, """
        UPDATE core.employees SET hiredate = %s, terminationdate = %s, employmentstatus = %s
        WHERE employeeid = (SELECT employeeid FROM core.drivers WHERE driverid = %s)
    """, (hire_date, termination_date, employment_status, driver_id))
    query(tenant, "UPDATE core.drivers SET driverstatus = %s WHERE driverid = %s",
          (driver_status, driver_id))
