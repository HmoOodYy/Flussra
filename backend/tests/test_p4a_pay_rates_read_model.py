"""Driver Pay Rates read model over branch-applicable target PayDefinitions."""
from __future__ import annotations

import pytest

from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    approve_assignment,
    authored_assignment,
    build_tenant,
    create_definition,
    create_pending,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
    set_scalar_value,
)

pytestmark = pytest.mark.asyncio


async def _activate(client, tenant, branch_id, definition, *, active=True, headers=None):
    response = await client.patch(
        f"/compensation/branches/{branch_id}/pay-definitions/{definition['pay_definition_id']}",
        json={"is_active": active}, headers=headers or tenant.admin)
    assert response.status_code == 200, response.text


async def _rows(client, tenant, driver_id, headers=None, **params):
    response = await client.get(
        f"/compensation/drivers/{driver_id}/pay-rates", params=params,
        headers=headers or tenant.admin)
    assert response.status_code == 200, response.text
    return {row["pay_definition_id"]: row for row in response.json()}


async def test_only_definitions_active_in_the_drivers_branch_are_listed(p3c_client, tenant):
    stops = await create_definition(p3c_client, tenant, definition_code="STOPS")
    miles = await create_definition(p3c_client, tenant, definition_code="MILES")
    unconfigured = await create_definition(p3c_client, tenant, definition_code="CUSTOM_UNITS")
    inactive = await create_definition(p3c_client, tenant, definition_code="ITEM_ALPHA")
    await _activate(p3c_client, tenant, tenant.branch_a, stops)
    await _activate(p3c_client, tenant, tenant.branch_a, miles)
    await _activate(p3c_client, tenant, tenant.branch_a, inactive, active=False)
    await _activate(p3c_client, tenant, tenant.branch_b, unconfigured)

    in_a = await _rows(p3c_client, tenant, tenant.driver_a)
    in_b = await _rows(p3c_client, tenant, tenant.driver_b)
    assert {r["definition_code"] for r in in_a.values()} == {"STOPS", "MILES"}
    assert {r["definition_code"] for r in in_b.values()} == {"CUSTOM_UNITS"}
    assert unconfigured["pay_definition_id"] not in in_a
    assert inactive["pay_definition_id"] not in in_a


async def test_zero_definitions_and_unconfigured_branches_yield_no_rows(p3c_client, tenant):
    assert await _rows(p3c_client, tenant, tenant.driver_a) == {}
    await create_definition(p3c_client, tenant)
    assert await _rows(p3c_client, tenant, tenant.driver_a) == {}


async def test_missing_zero_and_positive_rates_are_distinct(p3c_client, tenant):
    missing = await create_definition(p3c_client, tenant, definition_code="A_MISSING")
    zero = await create_definition(p3c_client, tenant, definition_code="B_ZERO")
    positive = await create_definition(p3c_client, tenant, definition_code="C_POSITIVE")
    for definition in (missing, zero, positive):
        await _activate(p3c_client, tenant, tenant.branch_a, definition)
    await authored_assignment(p3c_client, tenant, zero, "0")
    await authored_assignment(p3c_client, tenant, positive, "7.5")

    rows = await _rows(p3c_client, tenant, tenant.driver_a)
    assert rows[missing["pay_definition_id"]]["current"] is None
    assert rows[zero["pay_definition_id"]]["current"]["amount"] == "0.0000"
    assert rows[positive["pay_definition_id"]]["current"]["amount"] == "7.5000"
    assert list(rows) == sorted(
        rows, key=lambda i: (rows[i]["definition_name"].lower(), rows[i]["definition_code"], i))


async def test_pending_future_and_current_assignments_are_reported_separately(
    p3c_client, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    await _activate(p3c_client, tenant, tenant.branch_a, definition)
    await authored_assignment(p3c_client, tenant, definition, "10", effective_from="2020-01-01")
    await authored_assignment(p3c_client, tenant, definition, "12", effective_from="2099-01-01")
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2099-06-01")
    await set_scalar_value(p3c_client, tenant, pending, definition, "15")

    row = (await _rows(p3c_client, tenant, tenant.driver_a))[definition["pay_definition_id"]]
    assert row["current"]["amount"] == "10.0000" and row["current"]["status"] == "Superseded"
    assert row["future"]["amount"] == "12.0000" and row["future"]["effective_from"] == "2099-01-01"
    assert row["pending"]["amount"] == "15.0000"
    assert row["rate_component_definition_id"] == definition["components"][0][
        "rate_component_definition_id"]

    later = (await _rows(p3c_client, tenant, tenant.driver_a, as_of="2099-02-01"))[
        definition["pay_definition_id"]]
    assert later["current"]["amount"] == "12.0000"
    assert later["future"] is None


async def test_unset_pending_value_is_missing_not_zero(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    await _activate(p3c_client, tenant, tenant.branch_a, definition)
    await create_pending(p3c_client, tenant, definition)
    row = (await _rows(p3c_client, tenant, tenant.driver_a))[definition["pay_definition_id"]]
    assert row["pending"]["amount"] is None
    approval = await approve_assignment(
        p3c_client, tenant, {"driver_rate_assignment_id": row["pending"]["driver_rate_assignment_id"]})
    assert approval.status_code == 422


async def test_retired_and_deactivated_definitions_leave_the_read_model(p3c_client, tenant):
    retired = await create_definition(p3c_client, tenant)
    deactivated = await create_definition(p3c_client, tenant)
    for definition in (retired, deactivated):
        await _activate(p3c_client, tenant, tenant.branch_a, definition)
    await p3c_client.post(
        f"/compensation/pay-definitions/{retired['pay_definition_id']}/retire", headers=tenant.admin)
    await _activate(p3c_client, tenant, tenant.branch_a, deactivated, active=False)
    assert await _rows(p3c_client, tenant, tenant.driver_a) == {}


async def test_read_model_security(p3c_client, tenant, p3b_dsn):
    definition = await create_definition(p3c_client, tenant)
    await _activate(p3c_client, tenant, tenant.branch_a, definition)
    await _activate(p3c_client, tenant, tenant.branch_b, definition)
    url = "/compensation/drivers/{}/pay-rates"
    assert (await p3c_client.get(
        url.format(tenant.driver_a), headers=tenant.headers(tenant.viewer))).status_code == 200
    assert (await p3c_client.get(
        url.format(tenant.driver_a), headers=tenant.headers(tenant.no_permissions))).status_code == 403
    branch_user = tenant.headers(tenant.branch_user)
    assert (await p3c_client.get(url.format(tenant.driver_a), headers=branch_user)).status_code == 200
    assert (await p3c_client.get(url.format(tenant.driver_b), headers=branch_user)).status_code == 403
    assert (await p3c_client.get(
        url.format(tenant.driver_a), headers=build_tenant(p3b_dsn).admin)).status_code == 404
