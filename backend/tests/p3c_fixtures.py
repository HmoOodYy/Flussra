"""Shared fixtures for the target Compensation authoring tests.

The tests run against the disposable database built by p3b_fixtures, with their
own Company tenants, roles, users and Drivers, so they never touch the shared
application test data. Approved assignments and request history are
intentionally undeletable, so nothing is cleaned up.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import uuid4

import httpx
import psycopg2
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests.p3b_fixtures import make_branch, make_company, make_driver

PERMISSIONS = ["payitems.view", "payitems.edit", "payrates.view", "payrates.edit"]


@pytest_asyncio.fixture(scope="session", name="p3c_engine")
async def p3c_database_engine(p3b_dsn):
    url = (
        f"postgresql+asyncpg://{p3b_dsn['user']}@{p3b_dsn['host']}:{p3b_dsn['port']}"
        f"/{p3b_dsn['database']}"
    )
    engine = create_async_engine(url, echo=False)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session", name="p3c_app")
async def p3c_application(p3c_engine):
    from app.main import create_app

    app = create_app()
    app.state.engine = p3c_engine

    async def _get_db() -> AsyncGenerator[AsyncConnection, None]:
        async with p3c_engine.begin() as conn:
            yield conn

    from app.dependencies import get_db

    app.dependency_overrides[get_db] = _get_db
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(name="p3c_client")
async def p3c_http_client(p3c_app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=p3c_app), base_url="http://p3c.test"
    ) as client:
        yield client


@dataclass
class Tenant:
    company_id: int
    branch_a: int
    branch_b: int
    driver_a: int
    driver_a2: int
    driver_b: int
    owner: int
    branch_user: int
    viewer: int
    no_permissions: int
    dsn: dict | None = None

    def headers(self, user_id: int) -> dict[str, str]:
        from app.auth.security import create_access_token

        return {"Authorization": f"Bearer {create_access_token(user_id, self.company_id)}"}

    @property
    def admin(self) -> dict[str, str]:
        return self.headers(self.owner)


def _role(cur, company_id: int, code: str, permissions: list[str]) -> int:
    cur.execute("""
        INSERT INTO sec.companyroles
            (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
        VALUES (%s, %s, %s, 50, FALSE, FALSE, TRUE, TRUE) RETURNING companyroleid
    """, (company_id, code, code))
    role_id = cur.fetchone()[0]
    for permission in permissions:
        cur.execute("""
            INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode)
            VALUES (%s, %s)
        """, (role_id, permission))
    return role_id


def _user(cur, company_id: int, role_id: int, scope: str, branch_id: int | None) -> int:
    name = "u" + uuid4().hex[:12]
    cur.execute("""
        INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
        VALUES (%s, %s, %s, 'x', TRUE, TRUE) RETURNING userid
    """, (company_id, name, name))
    user_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO sec.userbranchroles
            (userid, companyid, branchid, companyroleid, scopetype, isactive)
        VALUES (%s, %s, %s, %s, %s, TRUE)
    """, (user_id, company_id, branch_id, role_id, scope))
    return user_id


def build_tenant(
    dsn: dict, *, currency: str | None = "USD", permissions: list[str] | None = None,
) -> Tenant:
    conn = psycopg2.connect(client_encoding="utf-8", **dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            company_id = make_company(cur, currency)
            branch_a = make_branch(cur, company_id, is_default=True)
            branch_b = make_branch(cur, company_id)
            full = _role(cur, company_id, "P3C_FULL", permissions or PERMISSIONS)
            read = _role(cur, company_id, "P3C_READ", ["payitems.view", "payrates.view"])
            none = _role(cur, company_id, "P3C_NONE", [])
            return Tenant(
                company_id=company_id,
                branch_a=branch_a,
                branch_b=branch_b,
                driver_a=make_driver(cur, company_id, branch_a),
                driver_a2=make_driver(cur, company_id, branch_a),
                driver_b=make_driver(cur, company_id, branch_b),
                owner=_user(cur, company_id, full, "AllCompanyBranches", None),
                branch_user=_user(cur, company_id, full, "SpecificBranch", branch_a),
                viewer=_user(cur, company_id, read, "AllCompanyBranches", None),
                no_permissions=_user(cur, company_id, none, "AllCompanyBranches", None),
                dsn=dsn,
            )
    finally:
        conn.close()


@pytest.fixture(name="tenant")
def p3c_tenant(p3b_dsn) -> Tenant:
    return build_tenant(p3b_dsn)


@pytest.fixture(name="unconfigured_tenant")
def p3c_unconfigured_tenant(p3b_dsn) -> Tenant:
    return build_tenant(p3b_dsn, currency=None)


async def create_definition(client: httpx.AsyncClient, tenant: Tenant, **overrides) -> dict:
    body = {
        "definition_code": "D" + uuid4().hex[:10],
        "definition_name": "Arbitrary definition",
        "input_type": "Decimal",
        "unit": "unit",
        "calculation_method": "PerUnit",
    } | overrides
    response = await client.post("/compensation/pay-definitions", json=body, headers=tenant.admin)
    assert response.status_code == 201, response.text
    return response.json()


def grant_applicability(
    tenant: Tenant, definition: dict, branch_id: int, *,
    effective_from: str = "2000-01-01", active: bool = True,
) -> None:
    """Test setup: set a PayDefinition's single BranchPayItemConfig version directly."""
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM payroll.branchpayitemconfig
                WHERE companyid = %s AND branchid = %s AND paydefinitionid = %s
            """, (tenant.company_id, branch_id, definition["pay_definition_id"]))
            cur.execute("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, paydefinitionid, isactive, effectivefrom)
                VALUES (%s, %s, %s, %s, %s)
            """, (tenant.company_id, branch_id, definition["pay_definition_id"], active,
                  effective_from))
    finally:
        conn.close()


def _ensure_applicable(tenant: Tenant, definition: dict, driver_id: int) -> None:
    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT branchid FROM core.drivers WHERE driverid = %s", (driver_id,))
            branch_id = cur.fetchone()[0]
            cur.execute("""
                SELECT 1 FROM payroll.branchpayitemconfig
                WHERE companyid = %s AND branchid = %s AND paydefinitionid = %s
            """, (tenant.company_id, branch_id, definition["pay_definition_id"]))
            exists = cur.fetchone() is not None
    finally:
        conn.close()
    if not exists:
        grant_applicability(tenant, definition, branch_id)


async def create_pending(
    client: httpx.AsyncClient, tenant: Tenant, definition: dict, *,
    driver_id: int | None = None, effective_from: str = "2026-01-01",
    effective_to: str | None = None, headers: dict | None = None,
    applicable: bool = True,
) -> dict:
    if applicable:
        _ensure_applicable(tenant, definition, driver_id or tenant.driver_a)
    response = await client.post(
        "/compensation/driver-rate-assignments",
        json={"driver_id": driver_id or tenant.driver_a,
              "rate_definition_id": definition["rate_definition_id"],
              "effective_from": effective_from, "effective_to": effective_to},
        headers=headers or tenant.admin,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def set_scalar_value(
    client: httpx.AsyncClient, tenant: Tenant, assignment: dict, definition: dict, amount,
    headers: dict | None = None,
) -> httpx.Response:
    return await client.put(
        f"/compensation/driver-rate-assignments/{assignment['driver_rate_assignment_id']}/values",
        json={"values": [{
            "rate_component_definition_id":
                definition["components"][0]["rate_component_definition_id"],
            "amount": amount,
        }]},
        headers=headers or tenant.admin,
    )


async def approve_assignment(
    client: httpx.AsyncClient, tenant: Tenant, assignment: dict, headers: dict | None = None,
) -> httpx.Response:
    return await client.post(
        f"/compensation/driver-rate-assignments/{assignment['driver_rate_assignment_id']}/approve",
        headers=headers or tenant.admin,
    )


async def authored_assignment(
    client: httpx.AsyncClient, tenant: Tenant, definition: dict, amount="25", **kwargs,
) -> dict:
    """Create, fill and approve one scalar assignment through the API."""
    pending = await create_pending(client, tenant, definition, **kwargs)
    filled = await set_scalar_value(client, tenant, pending, definition, amount)
    assert filled.status_code == 200, filled.text
    approved = await approve_assignment(client, tenant, pending)
    assert approved.status_code == 200, approved.text
    return approved.json()
