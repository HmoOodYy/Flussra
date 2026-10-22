"""Transfer-read scope: drivers.view OR drivers.edit, additive across Company and Branch scope.

Company-wide transfer authority reads every request; otherwise the caller reads only requests
whose source or target Branch it holds transfer authority for. A Company-wide assignment in an
unrelated permission family neither grants nor suppresses that Branch authority.
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


def _sql(tenant: Tenant, statement: str, params=()):
    conn = connect(tenant.dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def _operator(tenant: Tenant, *assignments: tuple[list[str], str, int | None]) -> int:
    """A User holding one role per (permissions, scope, branch) assignment."""
    user_id = _sql(tenant, """
        INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
        VALUES (%s, %s, 'scope operator', 'x', TRUE, TRUE) RETURNING userid
    """, (tenant.company_id, unique("op")))[0][0]
    for permissions, scope, branch_id in assignments:
        role_id = _sql(tenant, """
            INSERT INTO sec.companyroles
                (companyid, rolecode, rolename, rolelevel, isdefault, isprotected, iscustom, isactive)
            VALUES (%s, %s, 'scope role', 50, FALSE, FALSE, TRUE, TRUE) RETURNING companyroleid
        """, (tenant.company_id, unique("R")))[0][0]
        for permission in permissions:
            _sql(tenant, "INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode) VALUES (%s, %s)",
                 (role_id, permission))
        _sql(tenant, """
            INSERT INTO sec.userbranchroles
                (userid, companyid, branchid, companyroleid, scopetype, isactive)
            VALUES (%s, %s, %s, %s, %s, TRUE)
        """, (user_id, tenant.company_id, branch_id, role_id, scope))
    return user_id


@pytest.fixture(name="scoped")
def scoped_tenant(p3b_dsn) -> Tenant:
    return build_tenant(p3b_dsn, permissions=["drivers.edit"])


async def _two_transfers(client, tenant: Tenant) -> tuple[int, int]:
    """One transfer touching Branch A (A→B) and one that does not (B→C)."""
    conn = connect(tenant.dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault, timezoneid)
                VALUES (%s, %s, 'Branch C', 'Active', FALSE, 'UTC') RETURNING branchid
            """, (tenant.company_id, unique("B")))
            branch_c = cur.fetchone()[0]
    finally:
        conn.close()
    touching_a = await client.post("/driver-transfers", headers=tenant.admin, json={
        "driver_id": tenant.driver_a_id, "target_branch_id": tenant.branch_b,
        "initiated_by": "SourceBranch",
    })
    away_from_a = await client.post("/driver-transfers", headers=tenant.admin, json={
        "driver_id": tenant.driver_b_id, "target_branch_id": branch_c,
        "initiated_by": "SourceBranch",
    })
    assert touching_a.status_code == 201, touching_a.text
    assert away_from_a.status_code == 201, away_from_a.text
    return touching_a.json()["transfer_request_id"], away_from_a.json()["transfer_request_id"]


async def _listed(client, tenant: Tenant, user_id: int) -> set[int]:
    response = await client.get("/driver-transfers", headers=tenant.headers(user_id))
    assert response.status_code == 200, response.text
    return {item["transfer_request_id"] for item in response.json()["items"]}


async def test_unrelated_company_scope_does_not_suppress_branch_transfer_authority(
    p3c_client, scoped,
):
    tenant = scoped
    near, far = await _two_transfers(p3c_client, tenant)
    mixed = _operator(
        tenant,
        (["users.view"], "AllCompanyBranches", None),
        (["drivers.edit"], "SpecificBranch", tenant.branch_a),
    )

    assert await _listed(p3c_client, tenant, mixed) == {near}
    headers = tenant.headers(mixed)
    assert (await p3c_client.get(f"/driver-transfers/{near}", headers=headers)).status_code == 200
    assert (await p3c_client.get(f"/driver-transfers/{far}", headers=headers)).status_code == 404

    candidates = await p3c_client.get("/driver-transfers/driver-candidates", headers=headers)
    assert candidates.status_code == 200
    assert {row["source_branch_id"] for row in candidates.json()} <= {tenant.branch_a}


async def test_company_wide_drivers_view_or_edit_reads_every_transfer(p3c_client, scoped):
    tenant = scoped
    near, far = await _two_transfers(p3c_client, tenant)
    for permission in ("drivers.view", "drivers.edit"):
        user = _operator(tenant, ([permission], "AllCompanyBranches", None))
        assert await _listed(p3c_client, tenant, user) == {near, far}, permission


async def test_branch_only_drivers_view_or_edit_stays_branch_filtered(p3c_client, scoped):
    tenant = scoped
    near, far = await _two_transfers(p3c_client, tenant)
    for permission in ("drivers.view", "drivers.edit"):
        user = _operator(tenant, ([permission], "SpecificBranch", tenant.branch_a))
        assert await _listed(p3c_client, tenant, user) == {near}, permission
        headers = tenant.headers(user)
        assert (await p3c_client.get(f"/driver-transfers/{far}", headers=headers)).status_code == 404


async def test_a_branch_grant_never_widens_to_other_branches_through_company_scope(
    p3c_client, scoped,
):
    tenant = scoped
    near, far = await _two_transfers(p3c_client, tenant)
    # Branch authority on the Branch the far transfer starts from, with company scope for users only.
    user = _operator(
        tenant,
        (["users.view"], "AllCompanyBranches", None),
        (["drivers.view"], "SpecificBranch", tenant.branch_b),
    )
    # Branch B is the source of the far transfer and the target of the near one: both touch B.
    assert await _listed(p3c_client, tenant, user) == {near, far}
    only_a = _operator(
        tenant,
        (["users.view"], "AllCompanyBranches", None),
        (["drivers.view"], "SpecificBranch", tenant.branch_a),
    )
    assert far not in await _listed(p3c_client, tenant, only_a)


async def test_no_transfer_permission_anywhere_is_denied(p3c_client, scoped):
    tenant = scoped
    await _two_transfers(p3c_client, tenant)
    for assignments in (
        [(["users.view"], "AllCompanyBranches", None)],
        [(["users.view"], "AllCompanyBranches", None), (["payrates.view"], "SpecificBranch", tenant.branch_a)],
        [(["payrates.view"], "SpecificBranch", tenant.branch_a)],
    ):
        user = _operator(tenant, *assignments)
        response = await p3c_client.get("/driver-transfers", headers=tenant.headers(user))
        assert response.status_code == 403, assignments


async def test_company_scope_transfer_authority_is_not_leaked_across_companies(
    p3c_client, scoped, p3b_dsn,
):
    tenant = scoped
    await _two_transfers(p3c_client, tenant)
    other = build_tenant(p3b_dsn, permissions=["drivers.edit"])
    other_ids = await _listed(p3c_client, other, other.owner)
    mine = await _listed(p3c_client, tenant, tenant.owner)
    assert other_ids.isdisjoint(mine)
