"""Branch applicability of Company PayDefinitions (operational configuration authority)."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from psycopg2 import errors

from app.compensation.branch_config import resolve_effective_from
from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    build_tenant,
    create_definition,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
)

pytestmark = pytest.mark.asyncio


def _today() -> str:
    return date.today().isoformat()


def _later(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _url(tenant, branch_id: int, definition: dict) -> str:
    return f"/compensation/branches/{branch_id}/pay-definitions/{definition['pay_definition_id']}"


async def _configure(client, tenant, branch_id, definition, *, active=True, effective_from=None,
                     notes=None, headers=None):
    return await client.patch(
        _url(tenant, branch_id, definition),
        json={"is_active": active, "effective_from": effective_from, "notes": notes},
        headers=headers or tenant.admin)


async def _branch_states(client, tenant, branch_id, headers=None):
    response = await client.get(
        f"/compensation/branches/{branch_id}/pay-definitions", headers=headers or tenant.admin)
    assert response.status_code == 200, response.text
    return {item["pay_definition_id"]: item for item in response.json()}


# ---------------------------------------------------------------------------
# Semantics: Company-owned, Branch-applicable, no implicit activation
# ---------------------------------------------------------------------------

async def test_company_without_pay_definitions_is_valid_and_lists_nothing(p3c_client, tenant):
    for branch_id in (tenant.branch_a, tenant.branch_b):
        response = await p3c_client.get(
            f"/compensation/branches/{branch_id}/pay-definitions", headers=tenant.admin)
        assert response.status_code == 200
        assert response.json() == []


async def test_direct_creation_does_not_activate_any_branch(p3c_client, tenant, cur):
    definition = await create_definition(p3c_client, tenant, definition_code="CUSTOM_UNITS")
    for branch_id in (tenant.branch_a, tenant.branch_b):
        state = (await _branch_states(p3c_client, tenant, branch_id))[definition["pay_definition_id"]]
        assert state["is_configured"] is False
        assert state["is_active"] is False
        assert state["current_config"] is None
    cur.execute("SELECT count(*) FROM payroll.branchpayitemconfig WHERE paydefinitionid = %s",
                (definition["pay_definition_id"],))
    assert cur.fetchone()[0] == 0


async def test_one_definition_can_be_active_in_one_branch_and_not_another(p3c_client, tenant):
    stops = await create_definition(p3c_client, tenant, definition_code="STOPS")
    miles = await create_definition(p3c_client, tenant, definition_code="MILES")
    pallets = await create_definition(p3c_client, tenant, definition_code="PALLETS")
    assert (await _configure(p3c_client, tenant, tenant.branch_a, stops)).status_code == 200
    assert (await _configure(p3c_client, tenant, tenant.branch_a, miles)).status_code == 200
    assert (await _configure(p3c_client, tenant, tenant.branch_a, pallets, active=False)).status_code == 200
    assert (await _configure(p3c_client, tenant, tenant.branch_b, miles)).status_code == 200
    assert (await _configure(p3c_client, tenant, tenant.branch_b, pallets)).status_code == 200

    a = await _branch_states(p3c_client, tenant, tenant.branch_a)
    b = await _branch_states(p3c_client, tenant, tenant.branch_b)
    active_a = {i["definition_code"] for i in a.values() if i["is_active"]}
    active_b = {i["definition_code"] for i in b.values() if i["is_active"]}
    assert active_a == {"STOPS", "MILES"}
    assert active_b == {"MILES", "PALLETS"}
    assert a[pallets["pay_definition_id"]]["is_configured"] is True  # explicitly inactive
    assert b[stops["pay_definition_id"]]["is_configured"] is False   # unconfigured


async def test_request_approval_activates_only_the_requesting_branch(p3c_client, tenant, cur):
    headers = tenant.headers(tenant.branch_user)
    draft = (await p3c_client.post(
        "/compensation/pay-definition-requests", headers=headers,
        json={"requesting_branch_id": tenant.branch_a, "definition_name": "Requested",
              "input_type": "Decimal", "calculation_method": "PerUnit"})).json()
    submitted = (await p3c_client.post(
        f"/compensation/pay-definition-requests/{draft['request_id']}/submit", headers=headers,
        json={"expected_revision": draft["revision"]})).json()
    approved = await p3c_client.post(
        f"/compensation/pay-definition-requests/{submitted['request_id']}/decide",
        headers=tenant.admin,
        json={"action": "Approve", "expected_revision": submitted["revision"], "reason": "ok"})
    assert approved.status_code == 200, approved.text
    definition_id = approved.json()["approved_pay_definition_id"]

    a = await _branch_states(p3c_client, tenant, tenant.branch_a)
    b = await _branch_states(p3c_client, tenant, tenant.branch_b)
    assert a[definition_id]["is_active"] is True
    assert b[definition_id]["is_configured"] is False
    cur.execute("""
        SELECT paydefinitionid, payitemid FROM payroll.branchpayitemconfig
        WHERE paydefinitionid = %s
    """, (definition_id,))
    assert cur.fetchall() == [(definition_id, None)]


# ---------------------------------------------------------------------------
# Effective-dated versioning
# ---------------------------------------------------------------------------

async def test_same_day_change_amends_the_open_version_in_place(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    first = (await _configure(p3c_client, tenant, tenant.branch_a, definition, notes="one")).json()
    second = (await _configure(
        p3c_client, tenant, tenant.branch_a, definition, active=False, notes="two")).json()
    assert first["current_config"]["config_id"] == second["current_config"]["config_id"]
    assert second["is_active"] is False and second["notes"] == "two"
    history = (await p3c_client.get(f"{_url(tenant, tenant.branch_a, definition)}/history",
                                    headers=tenant.admin)).json()
    assert len(history) == 1


async def test_future_change_versions_and_closes_the_previous_version(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    await _configure(p3c_client, tenant, tenant.branch_a, definition)
    change_on = _later(30)
    state = (await _configure(
        p3c_client, tenant, tenant.branch_a, definition, active=False,
        effective_from=change_on)).json()
    assert state["is_active"] is True  # still current today
    assert state["pending_config"]["effective_from"] == change_on
    assert state["pending_config"]["is_active"] is False

    history = (await p3c_client.get(f"{_url(tenant, tenant.branch_a, definition)}/history",
                                    headers=tenant.admin)).json()
    assert [(h["is_active"], h["effective_to"]) for h in history] == [
        (False, None), (True, (date.fromisoformat(change_on) - timedelta(days=1)).isoformat())]


async def test_pending_version_is_replaced_in_place(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    await _configure(p3c_client, tenant, tenant.branch_a, definition)
    await _configure(p3c_client, tenant, tenant.branch_a, definition, active=False,
                     effective_from=_later(40))
    state = (await _configure(p3c_client, tenant, tenant.branch_a, definition, active=False,
                              effective_from=_later(20))).json()
    assert state["pending_config"]["effective_from"] == _later(20)
    history = (await p3c_client.get(f"{_url(tenant, tenant.branch_a, definition)}/history",
                                    headers=tenant.admin)).json()
    assert len(history) == 2


def test_open_period_boundary_moves_or_rejects_the_effective_date():
    today = date(2026, 3, 10)
    assert resolve_effective_from(None, None, today) == today
    assert resolve_effective_from(date(2026, 4, 1), None, today) == date(2026, 4, 1)
    boundary = date(2026, 3, 15)
    assert resolve_effective_from(None, boundary, today) == date(2026, 3, 16)
    assert resolve_effective_from(date(2026, 3, 20), boundary, today) == date(2026, 3, 20)
    with pytest.raises(HTTPException) as rejected:
        resolve_effective_from(date(2026, 3, 15), boundary, today)
    assert rejected.value.status_code == 422
    assert rejected.value.detail["code"] == "OPEN_PERIOD_BOUNDARY"


# ---------------------------------------------------------------------------
# Bulk configuration
# ---------------------------------------------------------------------------

async def test_bulk_configuration_applies_to_all_active_branches_atomically(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    response = await p3c_client.patch(
        f"/compensation/pay-definitions/{definition['pay_definition_id']}/branch-config",
        headers=tenant.admin, json={"target": "AllBranches", "is_active": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requested_branch_count"] == body["updated_branch_count"] == 2
    assert {r["status"] for r in body["results"]} == {"Created"}
    for branch_id in (tenant.branch_a, tenant.branch_b):
        state = (await _branch_states(p3c_client, tenant, branch_id))[definition["pay_definition_id"]]
        assert state["is_active"] is True


async def test_bulk_selected_branches_rejects_unknown_branches_without_writing(
    p3c_client, tenant, p3b_dsn,
):
    other = build_tenant(p3b_dsn)
    definition = await create_definition(p3c_client, tenant)
    response = await p3c_client.patch(
        f"/compensation/pay-definitions/{definition['pay_definition_id']}/branch-config",
        headers=tenant.admin,
        json={"target": "SelectedBranches", "branch_ids": [tenant.branch_a, other.branch_a],
              "is_active": True})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "BRANCH_NOT_FOUND"
    state = (await _branch_states(p3c_client, tenant, tenant.branch_a))[definition["pay_definition_id"]]
    assert state["is_configured"] is False


async def test_bulk_target_shape_is_validated(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    url = f"/compensation/pay-definitions/{definition['pay_definition_id']}/branch-config"
    for body in (
        {"target": "SelectedBranches", "is_active": True},
        {"target": "AllBranches", "branch_ids": [tenant.branch_a], "is_active": True},
        {"target": "SelectedBranches", "branch_ids": [tenant.branch_a, tenant.branch_a],
         "is_active": True},
    ):
        assert (await p3c_client.patch(url, headers=tenant.admin, json=body)).status_code == 422


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------

async def test_retirement_preserves_history_and_blocks_new_activation(p3c_client, tenant, cur):
    definition = await create_definition(p3c_client, tenant)
    await _configure(p3c_client, tenant, tenant.branch_a, definition)
    retired = await p3c_client.post(
        f"/compensation/pay-definitions/{definition['pay_definition_id']}/retire",
        headers=tenant.admin)
    assert retired.status_code == 200
    body = retired.json()
    assert body["status"] == "Retired"
    assert body["provenance"] is not None
    assert body["rate_definition_id"] == definition["rate_definition_id"]
    assert len(body["components"]) == 1

    # Hidden from branch lists, activation refused, deactivation still allowed.
    assert definition["pay_definition_id"] not in await _branch_states(
        p3c_client, tenant, tenant.branch_a)
    refused = await _configure(p3c_client, tenant, tenant.branch_b, definition)
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "PAY_DEFINITION_NOT_ACTIVE"
    assert (await _configure(
        p3c_client, tenant, tenant.branch_a, definition, active=False)).status_code == 200

    # No new rate authoring, history intact, nothing deleted.
    blocked = await p3c_client.post(
        "/compensation/driver-rate-assignments", headers=tenant.admin,
        json={"driver_id": tenant.driver_a, "rate_definition_id": definition["rate_definition_id"],
              "effective_from": "2026-01-01"})
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "PAY_DEFINITION_NOT_ACTIVE"
    cur.execute("SELECT count(*) FROM payroll.branchpayitemconfig WHERE paydefinitionid = %s",
                (definition["pay_definition_id"],))
    assert cur.fetchone()[0] >= 1
    listed = (await p3c_client.get("/compensation/pay-definitions", headers=tenant.admin)).json()
    assert {d["pay_definition_id"]: d["status"] for d in listed}[
        definition["pay_definition_id"]] == "Retired"
    assert definition["pay_definition_id"] not in {
        d["pay_definition_id"] for d in (await p3c_client.get(
            "/compensation/pay-definitions?include_retired=false",
            headers=tenant.admin)).json()}


async def test_retirement_is_idempotent_and_requires_company_authority(p3c_client, tenant, p3b_dsn):
    definition = await create_definition(p3c_client, tenant)
    url = f"/compensation/pay-definitions/{definition['pay_definition_id']}/retire"
    assert (await p3c_client.post(url, headers=tenant.headers(tenant.branch_user))).status_code == 403
    assert (await p3c_client.post(url, headers=tenant.headers(tenant.viewer))).status_code == 403
    assert (await p3c_client.post(url, headers=build_tenant(p3b_dsn).admin)).status_code == 404
    assert (await p3c_client.post(url, headers=tenant.admin)).status_code == 200
    assert (await p3c_client.post(url, headers=tenant.admin)).json()["status"] == "Retired"


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

async def test_branch_scope_company_isolation_and_permissions(p3c_client, tenant, p3b_dsn):
    definition = await create_definition(p3c_client, tenant)
    branch_user = tenant.headers(tenant.branch_user)
    assert (await _configure(
        p3c_client, tenant, tenant.branch_a, definition, headers=branch_user)).status_code == 200
    assert (await _configure(
        p3c_client, tenant, tenant.branch_b, definition, headers=branch_user)).status_code == 403
    bulk = await p3c_client.patch(
        f"/compensation/pay-definitions/{definition['pay_definition_id']}/branch-config",
        headers=branch_user, json={"target": "AllBranches", "is_active": True})
    assert bulk.status_code == 403

    # Reads: a branch user sees only its own branch; a viewer cannot write.
    assert (await p3c_client.get(
        f"/compensation/branches/{tenant.branch_b}/pay-definitions",
        headers=branch_user)).status_code == 403
    viewer = tenant.headers(tenant.viewer)
    assert (await _configure(
        p3c_client, tenant, tenant.branch_a, definition, headers=viewer)).status_code == 403

    # Another Company's identifiers never resolve.
    other = build_tenant(p3b_dsn)
    assert (await _configure(
        p3c_client, tenant, other.branch_a, definition)).status_code == 404
    assert (await _configure(
        p3c_client, other, other.branch_a, definition, headers=other.admin)).status_code == 404
    assert (await p3c_client.get(
        f"/compensation/branches/{tenant.branch_a}/pay-definitions", headers=other.admin)
    ).status_code == 404
    assert (await p3c_client.get(
        f"{_url(tenant, tenant.branch_a, definition)}/history", headers=other.admin)
    ).status_code == 404


async def test_driver_self_accounts_are_denied_branch_configuration(
    session_client, auth_token, hq_branch_id,
):
    from uuid import uuid4

    from tests.builders.access import create_user_with_role_token, get_company_role_id

    role_id = await get_company_role_id(session_client, auth_token, "DRIVER")
    token = await create_user_with_role_token(
        session_client, auth_token, f"p4a_{uuid4().hex[:10]}", role_id,
        scope_type="Self", driver_branch_id=hq_branch_id)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await session_client.get(
        f"/compensation/branches/{hq_branch_id}/pay-definitions", headers=headers)).status_code == 403
    assert (await session_client.patch(
        f"/compensation/branches/{hq_branch_id}/pay-definitions/1",
        json={"is_active": True}, headers=headers)).status_code == 403
    assert (await session_client.post(
        "/compensation/pay-definitions/1/retire", headers=headers)).status_code == 403
    assert (await session_client.get(
        "/compensation/drivers/1/pay-rates", headers=headers)).status_code == 403


# ---------------------------------------------------------------------------
# Database invariants
# ---------------------------------------------------------------------------

async def test_config_rows_require_a_definition_and_cannot_carry_a_legacy_identity(
    p3c_client, tenant, cur,
):
    definition = await create_definition(p3c_client, tenant)
    insert = """
        INSERT INTO payroll.branchpayitemconfig
            (companyid, branchid, {extra}isactive, effectivefrom)
        VALUES (%s, %s, {values}TRUE, '2026-01-01')
    """
    with pytest.raises(errors.NotNullViolation):  # PayDefinitionID is required
        cur.execute(insert.format(extra="", values=""), (tenant.company_id, tenant.branch_a))
    cur.execute("SELECT payitemid FROM payroll.payitems ORDER BY payitemid LIMIT 1")
    legacy_item = cur.fetchone()[0]
    with pytest.raises(errors.CheckViolation):  # PayItemID can never be populated
        cur.execute(
            insert.format(extra="paydefinitionid, payitemid, ", values="%s, %s, "),
            (tenant.company_id, tenant.branch_a, definition["pay_definition_id"], legacy_item))


async def test_config_ownership_and_version_integrity(p3c_client, tenant, p3b_dsn, cur):
    definition = await create_definition(p3c_client, tenant)
    other = build_tenant(p3b_dsn)
    insert = """
        INSERT INTO payroll.branchpayitemconfig
            (companyid, branchid, paydefinitionid, isactive, effectivefrom, effectiveto)
        VALUES (%s, %s, %s, TRUE, %s, %s)
    """
    pd = definition["pay_definition_id"]
    with pytest.raises(errors.ForeignKeyViolation):  # branch of another Company
        cur.execute(insert, (tenant.company_id, other.branch_a, pd, "2026-01-01", None))
    with pytest.raises(errors.ForeignKeyViolation):  # definition of another Company
        cur.execute(insert, (other.company_id, other.branch_a, pd, "2026-01-01", None))
    cur.execute(insert, (tenant.company_id, tenant.branch_a, pd, "2026-01-01", "2026-01-31"))
    with pytest.raises(errors.ExclusionViolation):  # overlapping versions
        cur.execute(insert, (tenant.company_id, tenant.branch_a, pd, "2026-01-15", "2026-02-15"))
    cur.execute(insert, (tenant.company_id, tenant.branch_a, pd, "2026-02-01", None))
    with pytest.raises(errors.UniqueViolation):  # a second open version
        cur.execute(insert, (tenant.company_id, tenant.branch_a, pd, "2027-01-01", None))
    with pytest.raises(errors.CheckViolation):  # window must be ordered
        cur.execute(insert, (tenant.company_id, tenant.branch_b, pd, "2026-05-01", "2026-04-01"))


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

async def test_concurrent_first_configuration_leaves_one_open_version(p3c_client, tenant, cur):
    definition = await create_definition(p3c_client, tenant)
    results = await asyncio.gather(
        _configure(p3c_client, tenant, tenant.branch_a, definition, notes="x"),
        _configure(p3c_client, tenant, tenant.branch_a, definition, notes="y"),
        _configure(p3c_client, tenant, tenant.branch_a, definition, notes="z"))
    assert [r.status_code for r in results] == [200, 200, 200]
    cur.execute("""
        SELECT count(*), count(*) FILTER (WHERE effectiveto IS NULL)
        FROM payroll.branchpayitemconfig WHERE paydefinitionid = %s AND branchid = %s
    """, (definition["pay_definition_id"], tenant.branch_a))
    assert cur.fetchone() == (1, 1)


async def test_concurrent_versioning_is_deterministic_and_never_overlaps(p3c_client, tenant, cur):
    definition = await create_definition(p3c_client, tenant)
    await _configure(p3c_client, tenant, tenant.branch_a, definition)
    results = await asyncio.gather(
        _configure(p3c_client, tenant, tenant.branch_a, definition, active=False,
                   effective_from=_later(10)),
        _configure(p3c_client, tenant, tenant.branch_a, definition, active=False,
                   effective_from=_later(20)))
    assert [r.status_code for r in results] == [200, 200]
    cur.execute("""
        SELECT effectivefrom, effectiveto FROM payroll.branchpayitemconfig
        WHERE paydefinitionid = %s AND branchid = %s ORDER BY effectivefrom
    """, (definition["pay_definition_id"], tenant.branch_a))
    versions = cur.fetchall()
    assert sum(1 for _, end in versions if end is None) == 1
    for (_, end), (start, _) in zip(versions, versions[1:], strict=False):
        assert end is not None and end < start


async def test_retirement_racing_activation_never_activates_a_retired_definition(
    p3c_client, tenant, cur,
):
    definition = await create_definition(p3c_client, tenant)
    activate, retire = await asyncio.gather(
        _configure(p3c_client, tenant, tenant.branch_a, definition),
        p3c_client.post(
            f"/compensation/pay-definitions/{definition['pay_definition_id']}/retire",
            headers=tenant.admin))
    assert retire.status_code == 200
    assert activate.status_code in (200, 422)
    cur.execute("""
        SELECT pd.status, bool_or(c.isactive)
        FROM payroll.paydefinitions pd
        LEFT JOIN payroll.branchpayitemconfig c ON c.paydefinitionid = pd.paydefinitionid
        WHERE pd.paydefinitionid = %s GROUP BY pd.status
    """, (definition["pay_definition_id"],))
    status, active = cur.fetchone()
    assert status == "Retired"
    # Activation either lost the race (422, no row) or committed before retirement.
    assert (activate.status_code == 422) == (active is None)
