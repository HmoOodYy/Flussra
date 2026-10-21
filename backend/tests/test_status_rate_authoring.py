"""Status rates are Driver-owned schedules of the Company Status rate columns.

A Status rate column owns one scalar RateDefinition. A permanent Driver's rate for it is an
ordinary rate schedule (Pending, then Approved, then Superseded or Voided) authored with the same
compensation routes, the same authority and the same dated lifecycle as an ordinary PayDefinition
rate. Nothing is copied at a transfer and nothing is keyed by an assignment.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    approve_schedule,
    create_pending,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    set_scalar_value,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)
from tests.payroll_runtime_fixtures import (
    assign_weekly_setup,
    build_catalog_tenant,
    create_period,
    query,
    seed_status_column,
    seed_status_key,
    status_column_of,
)

pytestmark = pytest.mark.asyncio
D = Decimal
BASE = "/compensation/driver-rate-schedules"


@pytest.fixture(name="tenant")
def catalog_tenant(p3b_dsn):
    return build_catalog_tenant(p3b_dsn)


def _definition(tenant, column_id: int | None = None) -> dict:
    """The Status rate column's RateDefinition, shaped like a PayDefinition summary."""
    _, rate_definition_id, component_id = status_column_of(tenant, column_id)
    return {"rate_definition_id": rate_definition_id,
            "components": [{"rate_component_definition_id": component_id}]}


def _code(response) -> str:
    return response.json()["detail"]["code"]


async def _author(client, tenant, definition, amount="25", **kwargs) -> dict:
    pending = await create_pending(client, tenant, definition, **kwargs)
    filled = await set_scalar_value(client, tenant, pending, definition, amount)
    assert filled.status_code == 200, filled.text
    approved = await approve_schedule(client, tenant, pending)
    assert approved.status_code == 200, approved.text
    return approved.json()


# ---------------------------------------------------------------------------
# The lifecycle is the ordinary schedule lifecycle
# ---------------------------------------------------------------------------

async def test_a_status_rate_is_authored_and_approved_like_any_rate_and_pays_the_status(
    p3c_client, p3c_engine, tenant,
):
    definition = _definition(tenant)
    column_id, _, _ = status_column_of(tenant)
    approved = await _author(p3c_client, tenant, definition, "25", effective_from="2020-01-01")
    assert approved["status"] == "Approved"
    assert approved["pay_definition_id"] is None
    assert approved["status_rate_column_id"] == column_id

    seed_status_key(tenant, "Vacation", off=False, hoursvalue=D("8"), statusratecolumnid=column_id)
    await assign_weekly_setup(p3c_engine, tenant, tenant.branch_a, mask=0)
    period = await create_period(p3c_client, tenant, tenant.branch_a)
    saved = await p3c_client.post(
        f"/payroll/periods/{period['payroll_period_id']}/day-grid",
        json={"work_date": period["start_date"], "rows": [
            {"driver_assignment_id": tenant.driver_a, "values": {}, "status_key": "vacation"}]},
        headers=tenant.admin)
    assert saved.status_code == 200, saved.text
    preview = await p3c_client.get(
        f"/payroll/periods/{period['payroll_period_id']}/calculation-preview", headers=tenant.admin)
    (driver,) = [d for d in preview.json()["drivers"] if d["driver_assignment_id"] == tenant.driver_a]
    assert D(str(driver["status_pay"])) == D("200.00")
    (line,) = [row for row in driver["lines"] if row["source_type"] == "StatusEntryState"]
    assert D(str(line["resolved_rate"])) == D("25") and D(str(line["quantity"])) == D("8")


async def test_a_status_schedule_is_edited_replaced_approved_voided_and_discarded_like_any_other(
    p3c_client, tenant,
):
    definition = _definition(tenant)
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2030-01-01")
    schedule_id = pending["driver_rate_schedule_id"]
    # Incomplete: a schedule without its value cannot be approved.
    refused = await approve_schedule(p3c_client, tenant, pending)
    assert refused.status_code == 422

    assert (await set_scalar_value(p3c_client, tenant, pending, definition, "10")).status_code == 200
    replaced = await set_scalar_value(p3c_client, tenant, pending, definition, "12.5")
    assert replaced.status_code == 200
    assert [D(str(v["amount"])) for v in replaced.json()["values"]] == [D("12.5")]
    assert (await p3c_client.patch(
        f"{BASE}/{schedule_id}", json={"notes": "agreed"}, headers=tenant.admin)).status_code == 200
    assert (await approve_schedule(p3c_client, tenant, pending)).status_code == 200
    # An Approved schedule is never edited: it is voided with a reason.
    assert (await set_scalar_value(p3c_client, tenant, pending, definition, "99")).status_code == 409
    voided = await p3c_client.post(
        f"{BASE}/{schedule_id}/void", json={"reason": "wrong"}, headers=tenant.admin)
    assert voided.status_code == 200 and voided.json()["status"] == "Voided"

    spare = await create_pending(p3c_client, tenant, definition, effective_from="2031-01-01")
    assert (await p3c_client.delete(
        f"{BASE}/{spare['driver_rate_schedule_id']}", headers=tenant.admin)).status_code == 204


async def test_approving_a_later_schedule_supersedes_the_earlier_one(p3c_client, tenant):
    definition = _definition(tenant)
    first = await _author(p3c_client, tenant, definition, "20", effective_from="2020-01-01")
    second = await _author(p3c_client, tenant, definition, "30", effective_from="2030-01-01")
    reloaded = await p3c_client.get(
        f"{BASE}/{first['driver_rate_schedule_id']}", headers=tenant.admin)
    assert reloaded.json()["status"] == "Superseded"
    assert reloaded.json()["effective_to"] == "2029-12-31"
    assert second["status"] == "Approved"


async def test_status_schedules_of_different_columns_do_not_interfere(p3c_client, tenant):
    other_column = seed_status_column(tenant, "Second column")
    first = await _author(p3c_client, tenant, _definition(tenant), "20", effective_from="2020-01-01")
    second = await _author(p3c_client, tenant, _definition(tenant, other_column), "35",
                           effective_from="2020-01-01")
    for schedule in (first, second):
        reloaded = await p3c_client.get(
            f"{BASE}/{schedule['driver_rate_schedule_id']}", headers=tenant.admin)
        assert reloaded.json()["status"] == "Approved"


# ---------------------------------------------------------------------------
# The Driver's rate listing carries the Status rate columns beside the PayDefinitions
# ---------------------------------------------------------------------------

async def test_the_driver_pay_rate_listing_includes_every_active_status_rate_column(
    p3c_client, tenant,
):
    default_column, _, _ = status_column_of(tenant)
    extra_column = seed_status_column(tenant, "Night premium")
    inactive = seed_status_column(tenant, "Retired column")
    query(tenant, "UPDATE payroll.statusratecolumns SET isactive = FALSE "
                  "WHERE statusratecolumnid = %s", (inactive,))
    await _author(p3c_client, tenant, _definition(tenant), "25", effective_from="2020-01-01")

    listing = await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a_id}/pay-rates", headers=tenant.admin)
    assert listing.status_code == 200, listing.text
    columns = {row["status_rate_column_id"]: row
               for row in listing.json() if row["kind"] == "StatusRateColumn"}
    assert set(columns) == {default_column, extra_column}
    row = columns[default_column]
    assert row["pay_definition_id"] is None and row["definition_code"] is None
    assert row["unit"] == "hour" and row["applicable_in_context_branch"] is None
    assert row["status_rate_column_name"] == row["definition_name"]
    assert D(str(row["current"]["amount"])) == D("25")
    assert columns[extra_column]["current"] is None and columns[extra_column]["pending"] is None

    summary = await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a_id}/rate-schedules/summary", headers=tenant.admin)
    assert summary.status_code == 200, summary.text
    (item,) = [row for row in summary.json() if row["kind"] == "StatusRateColumn"]
    assert item["status_rate_column_id"] == default_column and item["pay_definition_id"] is None
    assert item["definition_name"] == row["definition_name"]


async def test_a_status_rate_does_not_depend_on_any_branch_configuration(p3c_client, tenant):
    # A Status rate column has no Branch applicability: a Driver of either Branch gets the same
    # authoring surface.
    definition = _definition(tenant)
    for driver_id in (tenant.driver_a_id, tenant.driver_b_id):
        approved = await _author(p3c_client, tenant, definition, "18", driver_id=driver_id,
                                 effective_from="2020-01-01")
        assert approved["status"] == "Approved"


async def test_the_resolution_endpoint_resolves_a_status_rate_by_date(p3c_client, tenant):
    definition = _definition(tenant)
    await _author(p3c_client, tenant, definition, "22", effective_from="2020-01-01")
    response = await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a_id}/rate-definitions/"
        f"{definition['rate_definition_id']}/resolution",
        params={"work_date": "2025-03-04"}, headers=tenant.admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rate_definition_id"] == definition["rate_definition_id"]
    assert body["pay_definition_id"] is None and body["schedule_status"] == "Approved"
    assert D(str(body["components"][0]["amount"])) == D("22")


# ---------------------------------------------------------------------------
# The owner must be an active Status rate column
# ---------------------------------------------------------------------------

async def test_an_inactive_status_rate_column_cannot_be_authored_against(p3c_client, tenant):
    column = seed_status_column(tenant, "Closing column")
    definition = _definition(tenant, column)
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2030-02-01")
    await set_scalar_value(p3c_client, tenant, pending, definition, "10")
    query(tenant, "UPDATE payroll.statusratecolumns SET isactive = FALSE "
                  "WHERE statusratecolumnid = %s", (column,))

    created = await p3c_client.post(BASE, json={
        "driver_id": tenant.driver_a_id, "rate_definition_id": definition["rate_definition_id"],
        "effective_from": "2031-02-01"}, headers=tenant.admin)
    assert created.status_code == 422 and _code(created) == "STATUS_RATE_COLUMN_NOT_ACTIVE"
    approved = await approve_schedule(p3c_client, tenant, pending)
    assert approved.status_code == 422 and _code(approved) == "STATUS_RATE_COLUMN_NOT_ACTIVE"
    # A Pending schedule has no payroll effect: it can still be discarded.
    assert (await p3c_client.delete(
        f"{BASE}/{pending['driver_rate_schedule_id']}", headers=tenant.admin)).status_code == 204


async def test_a_rate_definition_of_another_company_is_not_found(p3c_client, tenant, p3b_dsn):
    from tests.payroll_runtime_fixtures import build_payroll_tenant

    stranger = build_payroll_tenant(p3b_dsn)
    foreign = _definition(stranger)
    response = await p3c_client.post(BASE, json={
        "driver_id": tenant.driver_a_id, "rate_definition_id": foreign["rate_definition_id"],
        "effective_from": "2030-01-01"}, headers=tenant.admin)
    assert response.status_code == 404
    assert _code(response) == "RATE_DEFINITION_NOT_FOUND"


# ---------------------------------------------------------------------------
# Authority is the ordinary Driver compensation authority
# ---------------------------------------------------------------------------

async def test_status_rate_authoring_needs_the_compensation_edit_permission(p3c_client, tenant):
    definition = _definition(tenant)
    body = {"driver_id": tenant.driver_a_id, "rate_definition_id": definition["rate_definition_id"],
            "effective_from": "2030-01-01"}
    for user in (tenant.viewer, tenant.no_permissions):
        response = await p3c_client.post(BASE, json=body, headers=tenant.headers(user))
        assert response.status_code == 403, (user, response.text)
    assert (await p3c_client.post(BASE, json=body, headers=tenant.admin)).status_code == 201


async def test_an_unauthorized_caller_cannot_probe_the_company_currency_through_a_status_rate(
    p3c_client, p3b_dsn,
):
    from tests.payroll_runtime_fixtures import build_payroll_tenant

    unconfigured = build_payroll_tenant(p3b_dsn, currency=None)
    definition = _definition(unconfigured)
    body = {"driver_id": unconfigured.driver_a_id,
            "rate_definition_id": definition["rate_definition_id"],
            "effective_from": "2030-01-01"}
    denied = await p3c_client.post(
        BASE, json=body, headers=unconfigured.headers(unconfigured.no_permissions))
    assert denied.status_code == 403
    assert "COMPANY_CURRENCY_REQUIRED" not in denied.text


async def test_a_branch_user_authors_only_for_drivers_of_their_own_branch(p3c_client, tenant):
    definition = _definition(tenant)
    branch_user = tenant.headers(tenant.branch_user)         # SpecificBranch on branch A
    own = await p3c_client.post(BASE, json={
        "driver_id": tenant.driver_a_id, "rate_definition_id": definition["rate_definition_id"],
        "effective_from": "2030-01-01"}, headers=branch_user)
    assert own.status_code == 201
    other = await p3c_client.post(BASE, json={
        "driver_id": tenant.driver_b_id, "rate_definition_id": definition["rate_definition_id"],
        "effective_from": "2030-01-01"}, headers=branch_user)
    assert other.status_code == 403


async def test_a_viewer_reads_status_schedules_but_cannot_change_them(p3c_client, tenant):
    definition = _definition(tenant)
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2030-03-01")
    viewer = tenant.headers(tenant.viewer)
    read = await p3c_client.get(f"{BASE}/{pending['driver_rate_schedule_id']}", headers=viewer)
    assert read.status_code == 200
    changed = await p3c_client.patch(
        f"{BASE}/{pending['driver_rate_schedule_id']}", json={"notes": "x"}, headers=viewer)
    assert changed.status_code == 403
    listing = await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a_id}/pay-rates", headers=viewer)
    assert listing.status_code == 200
    assert any(row["kind"] == "StatusRateColumn" for row in listing.json())


# ---------------------------------------------------------------------------
# The default column heals itself
# ---------------------------------------------------------------------------

def _remove_every_column(tenant) -> None:
    """Delete the Company's Status rate columns with their definitions, as a Company that never
    had any (nothing references them). The structure lock is not what these tests probe."""
    import psycopg2

    from tests.db_state import replica_replication_role_sync

    conn = psycopg2.connect(client_encoding="utf-8", **tenant.dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur, replica_replication_role_sync(cur):
            cur.execute("""
                DELETE FROM payroll.ratecomponentdefinitions WHERE ratedefinitionid IN (
                    SELECT ratedefinitionid FROM payroll.ratedefinitions
                    WHERE companyid = %s AND statusratecolumnid IS NOT NULL)
            """, (tenant.company_id,))
            cur.execute("DELETE FROM payroll.ratedefinitions "
                        "WHERE companyid = %s AND statusratecolumnid IS NOT NULL",
                        (tenant.company_id,))
            cur.execute("DELETE FROM payroll.statusratecolumns WHERE companyid = %s",
                        (tenant.company_id,))
    finally:
        conn.close()
    assert query(tenant, "SELECT count(*) FROM payroll.statusratecolumns "
                         "WHERE companyid = %s", (tenant.company_id,)) == [(0,)]


def _default_columns(tenant) -> list[tuple]:
    return query(tenant, """
        SELECT c.statusratecolumnid, c.columnname, rd.ratedefinitionid
        FROM payroll.statusratecolumns c
        JOIN payroll.ratedefinitions rd ON rd.statusratecolumnid = c.statusratecolumnid
        WHERE c.companyid = %s AND c.isdefault AND c.isactive
    """, (tenant.company_id,))


async def test_listing_the_status_rate_columns_creates_the_missing_default(p3c_client, tenant):
    _remove_every_column(tenant)
    listing = await p3c_client.get("/settings/status-rate-columns", headers=tenant.admin)
    assert listing.status_code == 200, listing.text
    columns = listing.json()
    assert [c["is_default"] for c in columns] == [True]
    assert columns[0]["rate_definition_id"] and columns[0]["rate_component_definition_id"]
    assert len(_default_columns(tenant)) == 1
    # Idempotent: listing again creates nothing new.
    again = await p3c_client.get("/settings/status-rate-columns", headers=tenant.admin)
    assert [c["status_rate_column_id"] for c in again.json()] \
        == [c["status_rate_column_id"] for c in columns]


async def test_creating_a_column_in_a_company_without_one_keeps_exactly_one_default(
    p3c_client, tenant,
):
    _remove_every_column(tenant)
    created = await p3c_client.post(
        "/settings/status-rate-columns", json={"column_name": "Premium"}, headers=tenant.admin)
    assert created.status_code == 201, created.text
    assert created.json()["is_default"] is False
    assert len(_default_columns(tenant)) == 1
    listing = (await p3c_client.get("/settings/status-rate-columns", headers=tenant.admin)).json()
    assert sorted(c["column_name"] for c in listing) == sorted(
        [created.json()["column_name"], _default_columns(tenant)[0][1]])


async def test_creating_a_status_in_a_company_without_a_column_creates_the_default(
    p3c_client, tenant,
):
    _remove_every_column(tenant)
    created = await p3c_client.post(
        "/settings/status-keys", json={"key_name": "First status", "category": "Vacation"},
        headers=tenant.admin)
    assert created.status_code == 201, created.text
    assert len(_default_columns(tenant)) == 1
    assert created.json()["status_rate_column_id"] is None      # healing does not bind the Status
