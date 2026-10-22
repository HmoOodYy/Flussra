"""Driver Pay Rates read model: every authorable target PayDefinition, flagged by applicability.

A Driver's rate schedules are Driver-owned, so the read model lists every company-Active,
PayDefinition-owned scalar definition (D5) and says, per definition, whether it is
applicable in the Branch of the Driver's context tenure on the reference date. The Company's
active Status rate columns are listed beside them (kind StatusRateColumn), without Branch
applicability.
"""
from __future__ import annotations

import pytest

from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    approve_schedule,
    authored_schedule,
    build_tenant,
    create_definition,
    create_pending,
    grant_applicability,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
    set_scalar_value,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.payroll_runtime_fixtures import query, seed_status_column, status_column_of

pytestmark = pytest.mark.asyncio


async def _activate(client, tenant, branch_id, definition, *, active=True, headers=None):
    grant_applicability(tenant, definition, branch_id, active=active)


async def _listing(client, tenant, driver_id, headers=None, **params) -> list[dict]:
    response = await client.get(
        f"/compensation/drivers/{driver_id}/pay-rates", params=params,
        headers=headers or tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()


async def _rows(client, tenant, driver_id, headers=None, **params):
    """The PayDefinition rows, by PayDefinition."""
    return {row["pay_definition_id"]: row
            for row in await _listing(client, tenant, driver_id, headers, **params)
            if row["kind"] == "PayDefinition"}


async def _status_rows(client, tenant, driver_id, headers=None, **params):
    """The Status rate column rows, by Status rate column."""
    return {row["status_rate_column_id"]: row
            for row in await _listing(client, tenant, driver_id, headers, **params)
            if row["kind"] == "StatusRateColumn"}


async def test_every_authorable_definition_is_listed_and_flagged_by_the_context_branch(
    p3c_client, tenant,
):
    stops = await create_definition(p3c_client, tenant, definition_code="STOPS")
    miles = await create_definition(p3c_client, tenant, definition_code="MILES")
    unconfigured = await create_definition(p3c_client, tenant, definition_code="CUSTOM_UNITS")
    inactive = await create_definition(p3c_client, tenant, definition_code="ITEM_ALPHA")
    elsewhere = await create_definition(p3c_client, tenant, definition_code="ELSEWHERE")
    await _activate(p3c_client, tenant, tenant.branch_a, stops)
    await _activate(p3c_client, tenant, tenant.branch_a, miles)
    await _activate(p3c_client, tenant, tenant.branch_a, inactive, active=False)
    await _activate(p3c_client, tenant, tenant.branch_b, elsewhere)

    rows = await _rows(p3c_client, tenant, tenant.driver_a_id)
    flags = {row["definition_code"]: row["applicable_in_context_branch"] for row in rows.values()}
    assert flags == {"STOPS": True, "MILES": True, "CUSTOM_UNITS": False,
                     "ITEM_ALPHA": False, "ELSEWHERE": False}
    assert unconfigured["pay_definition_id"] in rows  # authorable although not applicable

    in_b = await _rows(p3c_client, tenant, tenant.driver_b_id)
    assert {row["definition_code"]: row["applicable_in_context_branch"]
            for row in in_b.values()} == {"STOPS": False, "MILES": False, "CUSTOM_UNITS": False,
                                          "ITEM_ALPHA": False, "ELSEWHERE": True}


async def test_status_rate_columns_are_listed_beside_the_definitions_without_applicability(
    p3c_client, tenant,
):
    default_column, _, _ = status_column_of(tenant)
    extra = seed_status_column(tenant, "Night premium")
    closed = seed_status_column(tenant, "Closed column")
    query(tenant, "UPDATE payroll.statusratecolumns SET isactive = FALSE "
                  "WHERE statusratecolumnid = %s", (closed,))
    definition = await create_definition(p3c_client, tenant, definition_code="STOPS")
    await _activate(p3c_client, tenant, tenant.branch_a, definition)

    listing = await _listing(p3c_client, tenant, tenant.driver_a_id)
    assert {row["kind"] for row in listing} == {"PayDefinition", "StatusRateColumn"}
    statuses = await _status_rows(p3c_client, tenant, tenant.driver_a_id)
    assert set(statuses) == {default_column, extra}          # the inactive column is absent
    default = statuses[default_column]
    assert default["definition_name"] == "Status Pay"
    assert default["status_rate_column_name"] == "Status Pay"
    assert default["unit"] == "hour" and default["input_type"] == "Decimal"
    assert default["applicable_in_context_branch"] is None
    assert default["pay_definition_id"] is None and default["definition_code"] is None
    assert default["current"] is None and default["future"] is None and default["pending"] is None
    # The Status rows never appear among the definitions, and vice versa.
    assert set(await _rows(p3c_client, tenant, tenant.driver_a_id)) == {
        definition["pay_definition_id"]}


async def test_a_date_with_no_tenure_has_no_context_branch_to_flag(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    await _activate(p3c_client, tenant, tenant.branch_a, definition)
    before_any_tenure = await _rows(p3c_client, tenant, tenant.driver_a_id, as_of="1999-01-01")
    assert before_any_tenure[definition["pay_definition_id"]]["applicable_in_context_branch"] is None


async def test_zero_definitions_yield_no_rows_and_retired_ones_leave(p3c_client, tenant):
    assert await _rows(p3c_client, tenant, tenant.driver_a_id) == {}
    assert len(await _status_rows(p3c_client, tenant, tenant.driver_a_id)) == 1
    retired = await create_definition(p3c_client, tenant)
    kept = await create_definition(p3c_client, tenant)
    await p3c_client.post(
        f"/compensation/pay-definitions/{retired['pay_definition_id']}/retire", headers=tenant.admin)
    rows = await _rows(p3c_client, tenant, tenant.driver_a_id)
    assert set(rows) == {kept["pay_definition_id"]}


async def test_missing_zero_and_positive_rates_are_distinct(p3c_client, tenant):
    missing = await create_definition(p3c_client, tenant, definition_code="A_MISSING")
    zero = await create_definition(p3c_client, tenant, definition_code="B_ZERO")
    positive = await create_definition(p3c_client, tenant, definition_code="C_POSITIVE")
    await authored_schedule(p3c_client, tenant, zero, "0")
    await authored_schedule(p3c_client, tenant, positive, "7.5")

    rows = await _rows(p3c_client, tenant, tenant.driver_a_id)
    assert rows[missing["pay_definition_id"]]["current"] is None
    assert rows[zero["pay_definition_id"]]["current"]["amount"] == "0.0000"
    assert rows[positive["pay_definition_id"]]["current"]["amount"] == "7.5000"
    assert list(rows) == sorted(
        rows, key=lambda i: (rows[i]["definition_name"].lower(), rows[i]["definition_code"], i))


async def test_pending_future_and_current_schedules_are_reported_separately(
    p3c_client, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    await authored_schedule(p3c_client, tenant, definition, "10", effective_from="2020-01-01")
    await authored_schedule(p3c_client, tenant, definition, "12", effective_from="2099-01-01")
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2099-06-01")
    await set_scalar_value(p3c_client, tenant, pending, definition, "15")

    row = (await _rows(p3c_client, tenant, tenant.driver_a_id))[definition["pay_definition_id"]]
    assert row["current"]["amount"] == "10.0000" and row["current"]["status"] == "Superseded"
    assert row["future"]["amount"] == "12.0000" and row["future"]["effective_from"] == "2099-01-01"
    assert row["pending"]["amount"] == "15.0000"
    assert row["rate_component_definition_id"] == definition["components"][0][
        "rate_component_definition_id"]

    later = (await _rows(p3c_client, tenant, tenant.driver_a_id, as_of="2099-02-01"))[
        definition["pay_definition_id"]]
    assert later["current"]["amount"] == "12.0000"
    assert later["future"] is None


async def test_unset_pending_value_is_missing_not_zero(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    await create_pending(p3c_client, tenant, definition)
    row = (await _rows(p3c_client, tenant, tenant.driver_a_id))[definition["pay_definition_id"]]
    assert row["pending"]["amount"] is None
    approval = await approve_schedule(
        p3c_client, tenant, {"driver_rate_schedule_id": row["pending"]["driver_rate_schedule_id"]})
    assert approval.status_code == 422


async def test_read_model_security(p3c_client, tenant, p3b_dsn):
    await create_definition(p3c_client, tenant)
    url = "/compensation/drivers/{}/pay-rates"
    assert (await p3c_client.get(
        url.format(tenant.driver_a_id), headers=tenant.headers(tenant.viewer))).status_code == 200
    assert (await p3c_client.get(
        url.format(tenant.driver_a_id),
        headers=tenant.headers(tenant.no_permissions))).status_code == 403
    branch_user = tenant.headers(tenant.branch_user)
    assert (await p3c_client.get(url.format(tenant.driver_a_id), headers=branch_user)).status_code == 200
    assert (await p3c_client.get(url.format(tenant.driver_b_id), headers=branch_user)).status_code == 403
    assert (await p3c_client.get(
        url.format(tenant.driver_a_id), headers=build_tenant(p3b_dsn).admin)).status_code == 404
