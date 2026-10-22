"""The transfer-authorized Driver candidate read.

It rests on the transfer-create authority (`drivers.edit` on the source Branch), so a transfer
editor needs no `drivers.view`, and it offers exactly the Drivers transfer creation accepts: a
current, not-ending assignment with no transfer in flight and no pending assignment. The permanent
Driver work status is not part of the rule.
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
    unique,
)

pytestmark = pytest.mark.asyncio

CANDIDATES = "/driver-transfers/driver-candidates"


def _sql(tenant: Tenant, statement: str, params=()):
    conn = connect(tenant.dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def _future_only_driver(tenant: Tenant, branch_id: int) -> int:
    """An Active Driver whose only assignment starts in the future: no current assignment."""
    employee = _sql(tenant, """
        INSERT INTO core.employees (companyid, branchid, fullname, employeetype, employmentstatus)
        VALUES (%s, %s, 'Future Only Driver', 'Driver', 'Active') RETURNING employeeid
    """, (tenant.company_id, branch_id))[0][0]
    driver = _sql(tenant, """
        INSERT INTO core.drivers (companyid, employeeid, drivernumber, driverstatus)
        VALUES (%s, %s, %s, 'Active') RETURNING driverid
    """, (tenant.company_id, employee, unique("D")))[0][0]
    _sql(tenant, """
        INSERT INTO core.driverassignments (companyid, driverid, employeeid, branchid, effectivefromat)
        VALUES (%s, %s, %s, %s, NOW() + INTERVAL '30 days')
    """, (tenant.company_id, driver, employee, branch_id))
    return driver


def _driver_ids(rows) -> set[int]:
    return {row["driver_id"] for row in rows}


@pytest.fixture(name="editor")
def transfer_editor_tenant(p3b_dsn) -> Tenant:
    """drivers.edit only: transfer authority without drivers.view, employees.* or users.*."""
    return build_tenant(p3b_dsn, permissions=["drivers.edit"])


async def test_a_transfer_editor_without_drivers_view_gets_current_drivers(p3c_client, editor):
    tenant = editor
    denied = await p3c_client.get("/core/drivers", headers=tenant.admin)
    assert denied.status_code == 403

    response = await p3c_client.get(CANDIDATES, headers=tenant.admin)
    assert response.status_code == 200, response.text
    rows = response.json()
    assert {tenant.driver_a_id, tenant.driver_a2_id, tenant.driver_b_id} <= _driver_ids(rows)
    for row in rows:
        assert set(row) == {
            "driver_id", "driver_number", "full_name", "driver_assignment_id",
            "source_branch_id", "source_branch_name",
        }


async def test_a_driver_on_leave_with_a_current_assignment_is_still_offered(p3c_client, editor):
    tenant = editor
    _sql(tenant, "UPDATE core.drivers SET driverstatus = 'OnLeave' WHERE driverid = %s",
         (tenant.driver_a_id,))
    rows = (await p3c_client.get(CANDIDATES, headers=tenant.admin)).json()
    assert tenant.driver_a_id in _driver_ids(rows)
    created = await p3c_client.post("/driver-transfers", headers=tenant.admin, json={
        "driver_id": tenant.driver_a_id, "target_branch_id": tenant.branch_b,
        "initiated_by": "SourceBranch",
    })
    assert created.status_code == 201, created.text


async def test_a_driver_with_only_a_future_assignment_is_not_offered(p3c_client, editor):
    tenant = editor
    future = _future_only_driver(tenant, tenant.branch_a)
    rows = (await p3c_client.get(CANDIDATES, headers=tenant.admin)).json()
    assert future not in _driver_ids(rows)
    refused = await p3c_client.post("/driver-transfers", headers=tenant.admin, json={
        "driver_id": future, "target_branch_id": tenant.branch_b, "initiated_by": "SourceBranch",
    })
    assert refused.status_code == 422


async def test_a_driver_with_a_transfer_in_flight_is_not_offered(p3c_client, editor):
    tenant = editor
    created = await p3c_client.post("/driver-transfers", headers=tenant.admin, json={
        "driver_id": tenant.driver_a2_id, "target_branch_id": tenant.branch_b,
        "initiated_by": "SourceBranch",
    })
    assert created.status_code == 201, created.text
    rows = (await p3c_client.get(CANDIDATES, headers=tenant.admin)).json()
    assert tenant.driver_a2_id not in _driver_ids(rows)


async def test_only_drivers_of_branches_the_caller_may_edit_are_offered(p3c_client, editor):
    tenant = editor
    rows = (await p3c_client.get(CANDIDATES, headers=tenant.headers(tenant.branch_user))).json()
    assert tenant.driver_a_id in _driver_ids(rows)
    assert tenant.driver_b_id not in _driver_ids(rows)
    assert {row["source_branch_id"] for row in rows} == {tenant.branch_a}


async def test_the_candidate_read_requires_transfer_edit_authority(p3c_client, p3b_dsn):
    reader = build_tenant(p3b_dsn, permissions=["drivers.view"])
    assert (await p3c_client.get(CANDIDATES, headers=reader.admin)).status_code == 403
    nobody = build_tenant(p3b_dsn, permissions=["payrates.view"])
    assert (await p3c_client.get(CANDIDATES, headers=nobody.admin)).status_code == 403
    assert (await p3c_client.get(CANDIDATES)).status_code in (401, 403)


async def test_candidates_never_cross_companies(p3c_client, editor, p3b_dsn):
    tenant = editor
    other = build_tenant(p3b_dsn, permissions=["drivers.edit"])
    rows = (await p3c_client.get(CANDIDATES, headers=tenant.admin)).json()
    assert _driver_ids(rows).isdisjoint({other.driver_a_id, other.driver_a2_id, other.driver_b_id})


async def test_a_transfer_editor_can_open_the_transfer_list_without_drivers_view(
    p3c_client, editor, p3b_dsn,
):
    tenant = editor
    listed = await p3c_client.get("/driver-transfers", headers=tenant.admin)
    assert listed.status_code == 200, listed.text
    unrelated = build_tenant(p3b_dsn, permissions=["payrates.view"])
    assert (await p3c_client.get("/driver-transfers", headers=unrelated.admin)).status_code == 403
