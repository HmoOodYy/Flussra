"""The Access-scoped Employee link-candidate read and the linked-account fields it supports.

An Access administrator can pick an Employee to link without any Workforce permission, sees only
the minimum needed to identify it, never sees an Employee that is already linked, and never sees
another Company's Employees.
"""
from __future__ import annotations

import pytest

from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    Tenant,
    build_tenant,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    connect,
    p3b_cursor,
    p3b_database,
)

pytestmark = pytest.mark.asyncio

CANDIDATES = "/admin/employee-link-candidates"
MINIMUM_FIELDS = {
    "employee_id", "employee_key", "full_name", "branch_id", "branch_name",
    "employment_status", "driver_state",
}


def _sql(tenant: Tenant, statement: str, params=()):
    conn = connect(tenant.dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def _employee(tenant: Tenant, name: str, *, key: str | None = None) -> int:
    """A non-Driver Employee with no User."""
    return _sql(tenant, """
        INSERT INTO core.employees (companyid, branchid, fullname, employeekey, employmentstatus)
        VALUES (%s, %s, %s, %s, 'Active') RETURNING employeeid
    """, (tenant.company_id, tenant.branch_a, name, key))[0][0]


def _driver_employee(tenant: Tenant, driver_assignment_id: int) -> int:
    return _sql(tenant, "SELECT employeeid FROM core.driverassignments WHERE driverassignmentid = %s",
                (driver_assignment_id,))[0][0]


def _link(tenant: Tenant, user_id: int, employee_id: int) -> None:
    _sql(tenant, "UPDATE sec.users SET employeeid = %s WHERE userid = %s", (employee_id, user_id))


@pytest.fixture(name="access_only")
def access_only_tenant(p3b_dsn) -> Tenant:
    """The owner holds only users.edit and users.view: Access authority, no Workforce authority."""
    return build_tenant(p3b_dsn, permissions=["users.view", "users.edit"])


@pytest.fixture(name="workforce_only")
def workforce_only_tenant(p3b_dsn) -> Tenant:
    return build_tenant(p3b_dsn, permissions=["employees.view", "employees.manage"])


@pytest.fixture(name="workforce_and_access")
def workforce_and_access_tenant(p3b_dsn) -> Tenant:
    return build_tenant(p3b_dsn, permissions=["employees.view", "employees.manage", "users.view"])


async def _ids(client, tenant, headers=None, **params):
    response = await client.get(CANDIDATES, params=params, headers=headers or tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()


async def test_an_access_administrator_sees_unlinked_employees_with_only_the_minimum_fields(
    p3c_client, access_only,
):
    tenant = access_only
    plain = _employee(tenant, "Plain Employee", key="EMP-1")
    driver = _driver_employee(tenant, tenant.driver_a)
    rows = {row["employee_id"]: row for row in await _ids(p3c_client, tenant)}

    assert {plain, driver} <= set(rows)
    for row in rows.values():
        assert set(row) == MINIMUM_FIELDS
    assert rows[plain]["driver_state"] == "none" and rows[plain]["employee_key"] == "EMP-1"
    assert rows[driver]["driver_state"] == "current"
    assert rows[plain]["branch_id"] == tenant.branch_a
    assert rows[plain]["employment_status"] == "Active"


async def test_an_employee_with_a_linked_user_is_not_a_candidate(p3c_client, access_only):
    tenant = access_only
    linked = _employee(tenant, "Already Linked")
    free = _employee(tenant, "Still Free")
    _link(tenant, tenant.viewer, linked)
    ids = {row["employee_id"] for row in await _ids(p3c_client, tenant)}
    assert free in ids and linked not in ids


async def test_search_and_driver_state_narrow_the_candidates(p3c_client, access_only):
    tenant = access_only
    alpha = _employee(tenant, "Alpha Searchable", key="KEY-ALPHA")
    _employee(tenant, "Beta Other")
    driver = _driver_employee(tenant, tenant.driver_a)
    assert [r["employee_id"] for r in await _ids(p3c_client, tenant, q="searchable")] == [alpha]
    assert [r["employee_id"] for r in await _ids(p3c_client, tenant, q="key-alpha")] == [alpha]
    drivers = {r["employee_id"] for r in await _ids(p3c_client, tenant, driver_state="current")}
    assert driver in drivers and alpha not in drivers
    none = {r["employee_id"] for r in await _ids(p3c_client, tenant, driver_state="none")}
    assert alpha in none and driver not in none
    bad = await p3c_client.get(CANDIDATES, params={"driver_state": "bogus"}, headers=tenant.admin)
    assert bad.status_code == 422


async def test_a_pending_driver_is_reported_pending(p3c_client, access_only):
    tenant = access_only
    employee = _employee(tenant, "Starts Next Month")
    driver = _sql(tenant, """
        INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
        VALUES (%s, %s, 'PEND-1', 'Active') RETURNING driverid
    """, (tenant.company_id, employee))[0][0]
    _sql(tenant, """
        INSERT INTO core.driverassignments (companyid, branchid, employeeid, driverid, effectivefromat)
        VALUES (%s, %s, %s, %s, NOW() + INTERVAL '30 days')
    """, (tenant.company_id, tenant.branch_a, employee, driver))
    rows = {r["employee_id"]: r for r in await _ids(p3c_client, tenant)}
    assert rows[employee]["driver_state"] == "pending"


async def test_candidates_never_cross_companies(p3c_client, access_only, p3b_dsn):
    tenant = access_only
    other = build_tenant(p3b_dsn, permissions=["users.view", "users.edit"])
    mine = _employee(tenant, "Mine Only")
    theirs = _employee(other, "Theirs Only")
    mine_ids = {r["employee_id"] for r in await _ids(p3c_client, tenant)}
    their_ids = {r["employee_id"] for r in await _ids(p3c_client, other)}
    assert mine in mine_ids and theirs not in mine_ids
    assert theirs in their_ids and mine not in their_ids


async def test_the_read_needs_access_authority_not_workforce_authority(
    p3c_client, access_only, workforce_only,
):
    # users.edit alone suffices ...
    assert (await p3c_client.get(CANDIDATES, headers=access_only.admin)).status_code == 200
    # ... while full Workforce authority does not.
    denied = await p3c_client.get(CANDIDATES, headers=workforce_only.admin)
    assert denied.status_code == 403
    # No permission, or a Branch-scoped grant, is not company-level Access authority.
    assert (await p3c_client.get(
        CANDIDATES, headers=access_only.headers(access_only.no_permissions))).status_code == 403
    assert (await p3c_client.get(
        CANDIDATES, headers=access_only.headers(access_only.branch_user))).status_code == 403
    assert (await p3c_client.get(CANDIDATES)).status_code in (401, 403)


async def test_account_create_permission_alone_also_reads_candidates(p3c_client, p3b_dsn):
    creator = build_tenant(p3b_dsn, permissions=["users.create"])
    assert (await p3c_client.get(CANDIDATES, headers=creator.admin)).status_code == 200


async def test_an_access_administrator_links_and_unlinks_without_workforce_authority(
    p3c_client, access_only,
):
    tenant = access_only
    employee = _employee(tenant, "Link Me")
    # Without employees.view the Workforce roster stays closed to this operator.
    assert (await p3c_client.get("/workforce/employees", headers=tenant.admin)).status_code == 403

    linked = await p3c_client.put(
        f"/admin/users/{tenant.viewer}/employee-link", json={"employee_id": employee},
        headers=tenant.admin)
    assert linked.status_code == 200, linked.text
    assert linked.json()["employee_id"] == employee
    assert linked.json()["employee_name"] == "Link Me"
    assert employee not in {r["employee_id"] for r in await _ids(p3c_client, tenant)}

    listed = (await p3c_client.get("/admin/users", headers=tenant.admin)).json()
    assert {u["user_id"]: u["employee_name"] for u in listed}[tenant.viewer] == "Link Me"
    unlinked = await p3c_client.delete(
        f"/admin/users/{tenant.viewer}/employee-link", headers=tenant.admin)
    assert unlinked.status_code == 200 and unlinked.json()["employee_name"] is None
    assert employee in {r["employee_id"] for r in await _ids(p3c_client, tenant)}


async def test_a_user_with_no_employee_has_no_employee_name(p3c_client, access_only):
    tenant = access_only
    user = (await p3c_client.get(f"/admin/users/{tenant.owner}", headers=tenant.admin)).json()
    assert user["employee_id"] is None and user["employee_name"] is None


async def test_the_linking_is_refused_for_an_employee_of_another_company(
    p3c_client, access_only, p3b_dsn,
):
    tenant = access_only
    other = build_tenant(p3b_dsn, permissions=["users.view", "users.edit"])
    foreign = _employee(other, "Foreign Employee")
    refused = await p3c_client.put(
        f"/admin/users/{tenant.viewer}/employee-link", json={"employee_id": foreign},
        headers=tenant.admin)
    assert refused.status_code == 422


async def test_the_employee_detail_reports_whether_its_account_is_staged(
    p3c_client, workforce_and_access,
):
    tenant = workforce_and_access
    employee = _employee(tenant, "Staged Account Holder")
    staged_user = _sql(tenant, """
        INSERT INTO sec.users (companyid, employeeid, username, displayname, passwordhash,
                               isactive, canlogin, isstaged)
        VALUES (%s, %s, %s, 'Staged Person', 'x', TRUE, FALSE, TRUE) RETURNING userid
    """, (tenant.company_id, employee, "staged_" + str(employee)))[0][0]
    detail = (await p3c_client.get(f"/workforce/employees/{employee}", headers=tenant.admin)).json()
    assert detail["linked_user"]["user_id"] == staged_user
    assert detail["linked_user"]["is_staged"] is True
    assert detail["linked_user"]["can_login"] is False
    _sql(tenant, "UPDATE sec.users SET isstaged = FALSE, canlogin = TRUE WHERE userid = %s",
         (staged_user,))
    detail = (await p3c_client.get(f"/workforce/employees/{employee}", headers=tenant.admin)).json()
    assert (detail["linked_user"]["is_staged"], detail["linked_user"]["can_login"]) == (False, True)


def _linked_account(tenant: Tenant, employee: int) -> int:
    return _sql(tenant, """
        INSERT INTO sec.users (companyid, employeeid, username, displayname, passwordhash,
                               isactive, canlogin, isstaged)
        VALUES (%s, %s, %s, 'Linked Person', 'x', TRUE, TRUE, FALSE) RETURNING userid
    """, (tenant.company_id, employee, "linked_" + str(employee)))[0][0]


async def test_a_workforce_only_reader_receives_no_account_detail(p3c_client, workforce_only):
    tenant = workforce_only
    employee = _employee(tenant, "Has An Account")
    _linked_account(tenant, employee)

    detail = await p3c_client.get(f"/workforce/employees/{employee}", headers=tenant.admin)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["employee_id"] == employee and body["full_name"] == "Has An Account"
    assert body["linked_user"] is None
    assert f"linked_{employee}" not in detail.text
    admin_read = await p3c_client.get("/admin/users", headers=tenant.admin)
    assert admin_read.status_code == 403


async def test_a_reader_with_access_authority_receives_the_contextual_account_summary(
    p3c_client, workforce_and_access,
):
    tenant = workforce_and_access
    employee = _employee(tenant, "Has An Account")
    user_id = _linked_account(tenant, employee)

    body = (await p3c_client.get(f"/workforce/employees/{employee}", headers=tenant.admin)).json()
    assert body["linked_user"]["user_id"] == user_id
    assert body["linked_user"]["username"] == f"linked_{employee}"
    assert (body["linked_user"]["is_active"], body["linked_user"]["can_login"],
            body["linked_user"]["is_staged"]) == (True, True, False)


async def test_workforce_writes_do_not_disclose_the_account_to_a_workforce_only_caller(
    p3c_client, workforce_only,
):
    tenant = workforce_only
    employee = _employee(tenant, "Edited Person")
    _linked_account(tenant, employee)
    edited = await p3c_client.patch(
        f"/workforce/employees/{employee}", json={"preferred_name": "Eddie"}, headers=tenant.admin)
    assert edited.status_code == 200, edited.text
    assert edited.json()["linked_user"] is None


async def test_candidates_can_be_paged_and_searched_past_the_first_page(p3c_client, access_only):
    tenant = access_only
    ids = [_employee(tenant, f"Paged {index:03d}", key=f"PG-{index:03d}") for index in range(7)]

    first = await _ids(p3c_client, tenant, q="paged", limit=3)
    second = await _ids(p3c_client, tenant, q="paged", limit=3, offset=3)
    third = await _ids(p3c_client, tenant, q="paged", limit=3, offset=6)
    paged = [row["employee_id"] for row in first + second + third]
    assert paged == ids

    far = await _ids(p3c_client, tenant, q="paged 006", limit=3)
    assert [row["employee_id"] for row in far] == [ids[6]]
    by_key = await _ids(p3c_client, tenant, q="pg-005")
    assert [row["employee_id"] for row in by_key] == [ids[5]]


async def test_one_employee_can_be_resolved_by_id_wherever_it_sits(p3c_client, access_only):
    tenant = access_only
    ids = [_employee(tenant, f"Zed {index:03d}") for index in range(5)]
    last = ids[-1]
    window = await _ids(p3c_client, tenant, limit=2)
    assert last not in [row["employee_id"] for row in window]

    resolved = await _ids(p3c_client, tenant, employee_id=last)
    assert [row["employee_id"] for row in resolved] == [last]
    assert set(resolved[0]) == MINIMUM_FIELDS


async def test_an_employee_id_that_is_linked_or_foreign_does_not_resolve(
    p3c_client, access_only, p3b_dsn,
):
    tenant = access_only
    linked = _employee(tenant, "Already Linked")
    _linked_account(tenant, linked)
    other = build_tenant(p3b_dsn, permissions=["users.view", "users.edit"])
    foreign = _employee(other, "Foreign")
    assert await _ids(p3c_client, tenant, employee_id=linked) == []
    assert await _ids(p3c_client, tenant, employee_id=foreign) == []
