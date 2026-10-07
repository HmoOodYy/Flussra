"""Dormant target PerUnit DriverRateAssignment authoring."""
from __future__ import annotations

import json

import pytest

from app.compensation import assignments as assignment_service
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
    p3c_unconfigured_tenant,
    set_scalar_value,
)

pytestmark = pytest.mark.asyncio

BASE = "/compensation/driver-rate-assignments"


def _id(assignment: dict) -> int:
    return assignment["driver_rate_assignment_id"]


async def _get(client, tenant, assignment, headers=None):
    response = await client.get(f"{BASE}/{_id(assignment)}", headers=headers or tenant.admin)
    assert response.status_code == 200, response.text
    return response.json()


async def _definition_and_pending(client, tenant, **kwargs):
    definition = await create_definition(client, tenant)
    return definition, await create_pending(client, tenant, definition, **kwargs)


# ---------------------------------------------------------------------------
# Pending authoring
# ---------------------------------------------------------------------------

async def test_pending_assignment_starts_non_authoritative_with_unset_values(
    p3c_client, tenant,
):
    definition, pending = await _definition_and_pending(p3c_client, tenant)
    assert pending["status"] == "Pending"
    assert pending["branch_id"] == tenant.branch_a
    assert pending["approved_at_utc"] is None
    assert len(pending["values"]) == 1
    assert pending["values"][0]["amount"] is None
    assert pending["values"][0]["rate_component_definition_id"] == \
        definition["components"][0]["rate_component_definition_id"]


async def test_one_pending_assignment_per_driver_and_rate_definition(p3c_client, tenant):
    definition, _ = await _definition_and_pending(p3c_client, tenant)
    duplicate = await p3c_client.post(
        BASE, headers=tenant.admin,
        json={"driver_id": tenant.driver_a, "rate_definition_id": definition["rate_definition_id"],
              "effective_from": "2027-01-01"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "PENDING_ASSIGNMENT_EXISTS"
    await create_pending(p3c_client, tenant, definition, driver_id=tenant.driver_a2)


async def test_pending_dates_and_notes_are_editable_in_place(p3c_client, tenant):
    _, pending = await _definition_and_pending(p3c_client, tenant)
    response = await p3c_client.patch(
        f"{BASE}/{_id(pending)}", headers=tenant.admin,
        json={"effective_from": "2026-02-01", "effective_to": "2026-12-31", "notes": "n"})
    assert response.status_code == 200, response.text
    assert response.json()["effective_from"] == "2026-02-01"
    assert response.json()["effective_to"] == "2026-12-31"
    cleared = await p3c_client.patch(
        f"{BASE}/{_id(pending)}", headers=tenant.admin, json={"effective_to": None})
    assert cleared.json()["effective_to"] is None
    assert cleared.json()["notes"] == "n"
    invalid = await p3c_client.patch(
        f"{BASE}/{_id(pending)}", headers=tenant.admin,
        json={"effective_to": "2025-01-01"})
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "INVALID_EFFECTIVE_WINDOW"


async def test_zero_is_a_configured_value_and_missing_is_not_zero(p3c_client, tenant):
    definition, pending = await _definition_and_pending(p3c_client, tenant)
    incomplete = await approve_assignment(p3c_client, tenant, pending)
    assert incomplete.status_code == 422
    assert incomplete.json()["detail"]["code"] == "ASSIGNMENT_INCOMPLETE"

    nulled = await set_scalar_value(p3c_client, tenant, pending, definition, None)
    assert nulled.status_code == 200
    assert nulled.json()["values"][0]["amount"] is None
    assert (await approve_assignment(p3c_client, tenant, pending)).status_code == 422

    zero = await set_scalar_value(p3c_client, tenant, pending, definition, "0")
    assert zero.status_code == 200
    assert zero.json()["values"][0]["amount"] == "0.0000"
    approved = await approve_assignment(p3c_client, tenant, pending)
    assert approved.status_code == 200, approved.text
    assert approved.json()["values"][0]["amount"] == "0.0000"


@pytest.mark.parametrize("amount", ["-1", "-0.0001", "1.23456", "abc"])
async def test_invalid_amounts_are_rejected_without_rounding(p3c_client, tenant, amount):
    definition, pending = await _definition_and_pending(p3c_client, tenant)
    response = await set_scalar_value(p3c_client, tenant, pending, definition, amount)
    assert response.status_code == 422
    assert (await _get(p3c_client, tenant, pending))["values"][0]["amount"] is None


async def test_value_set_must_reference_this_definitions_components_once(p3c_client, tenant):
    definition, pending = await _definition_and_pending(p3c_client, tenant)
    other = await create_definition(p3c_client, tenant)
    foreign = other["components"][0]["rate_component_definition_id"]
    own = definition["components"][0]["rate_component_definition_id"]
    unknown = await p3c_client.put(
        f"{BASE}/{_id(pending)}/values", headers=tenant.admin,
        json={"values": [{"rate_component_definition_id": foreign, "amount": "1"}]})
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "UNKNOWN_COMPONENT"
    duplicate = await p3c_client.put(
        f"{BASE}/{_id(pending)}/values", headers=tenant.admin,
        json={"values": [{"rate_component_definition_id": own, "amount": "1"},
                         {"rate_component_definition_id": own, "amount": "2"}]})
    assert duplicate.status_code == 422
    assert duplicate.json()["detail"]["code"] == "DUPLICATE_COMPONENT_VALUE"


async def test_replacing_the_value_set_overwrites_and_clears(p3c_client, tenant):
    definition, pending = await _definition_and_pending(p3c_client, tenant)
    await set_scalar_value(p3c_client, tenant, pending, definition, "10")
    updated = await set_scalar_value(p3c_client, tenant, pending, definition, "12.5")
    assert updated.json()["values"][0]["amount"] == "12.5000"
    cleared = await p3c_client.put(
        f"{BASE}/{_id(pending)}/values", headers=tenant.admin, json={"values": []})
    assert cleared.status_code == 200
    assert cleared.json()["values"][0]["amount"] is None


async def test_only_scalar_pay_definition_owned_definitions_are_authorable(
    p3c_client, tenant, cur,
):
    cur.execute("SELECT ratetypeid FROM payroll.ratetypes WHERE ratecode = 'STATUS_PAY'")
    rate_type_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO payroll.statusratecolumns (companyid, branchid, ratetypeid, columnname)
        VALUES (%s, %s, %s, 'Dormant') RETURNING statusratecolumnid
    """, (tenant.company_id, tenant.branch_a, rate_type_id))
    column_id = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO payroll.ratedefinitions (companyid, statusratecolumnid, ownerbranchid, shape)
        VALUES (%s, %s, %s, 'Scalar') RETURNING ratedefinitionid
    """, (tenant.company_id, column_id, tenant.branch_a))
    status_definition = cur.fetchone()[0]
    response = await p3c_client.post(
        BASE, headers=tenant.admin,
        json={"driver_id": tenant.driver_a, "rate_definition_id": status_definition,
              "effective_from": "2026-01-01"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "RATE_DEFINITION_NOT_AUTHORABLE"


async def test_inactive_definitions_are_not_authorable(p3c_client, tenant, cur):
    definition = await create_definition(p3c_client, tenant)
    cur.execute("UPDATE payroll.paydefinitions SET status = 'Inactive' WHERE paydefinitionid = %s",
                (definition["pay_definition_id"],))
    response = await p3c_client.post(
        BASE, headers=tenant.admin,
        json={"driver_id": tenant.driver_a, "rate_definition_id": definition["rate_definition_id"],
              "effective_from": "2026-01-01"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PAY_DEFINITION_NOT_ACTIVE"


async def test_governed_definition_structure_is_locked_from_the_start(p3c_client, tenant, cur):
    from psycopg2 import errors

    definition = await create_definition(p3c_client, tenant)
    with pytest.raises(errors.CheckViolation, match="RATE_STRUCTURE_LOCKED"):
        cur.execute("DELETE FROM payroll.ratecomponentdefinitions WHERE ratedefinitionid = %s",
                    (definition["rate_definition_id"],))
    with pytest.raises(errors.CheckViolation, match="RATE_STRUCTURE_LOCKED"):
        cur.execute(
            "UPDATE payroll.paydefinitions SET calculationmethod = 'OrdinalTier', "
            "inputtype = 'WholeNumber' WHERE paydefinitionid = %s",
            (definition["pay_definition_id"],))


# ---------------------------------------------------------------------------
# Approval, supersession and immutability
# ---------------------------------------------------------------------------

async def test_first_approval_makes_the_assignment_authoritative_and_immutable(
    p3c_client, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    approved = await authored_assignment(p3c_client, tenant, definition, "25")
    assert approved["status"] == "Approved"
    assert approved["approved_by_user_id"] == tenant.owner
    assert approved["values"][0]["amount"] == "25.0000"

    for call in (
        p3c_client.put(f"{BASE}/{_id(approved)}/values", headers=tenant.admin,
                       json={"values": []}),
        p3c_client.patch(f"{BASE}/{_id(approved)}", headers=tenant.admin,
                         json={"notes": "x"}),
        p3c_client.delete(f"{BASE}/{_id(approved)}", headers=tenant.admin),
        approve_assignment(p3c_client, tenant, approved),
    ):
        response = await call
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "ASSIGNMENT_NOT_PENDING"
    assert (await _get(p3c_client, tenant, approved))["values"][0]["amount"] == "25.0000"


async def test_successor_approval_supersedes_the_current_schedule_atomically(
    p3c_client, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    first = await authored_assignment(p3c_client, tenant, definition, "25",
                                      effective_from="2026-01-01")
    second = await authored_assignment(p3c_client, tenant, definition, "30",
                                       effective_from="2026-06-01")
    assert second["status"] == "Approved"
    superseded = await _get(p3c_client, tenant, first)
    assert superseded["status"] == "Superseded"
    assert superseded["effective_to"] == "2026-05-31"
    assert superseded["values"][0]["amount"] == "25.0000"
    assert superseded["approved_by_user_id"] == first["approved_by_user_id"]

    history = (await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a}/rate-definitions/"
        f"{definition['rate_definition_id']}/assignments", headers=tenant.admin)).json()
    assert [(a["status"], a["effective_from"]) for a in history] == [
        ("Approved", "2026-06-01"), ("Superseded", "2026-01-01")]


async def test_successor_keeps_an_earlier_bounded_end(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    first = await authored_assignment(p3c_client, tenant, definition, "25",
                                      effective_from="2026-01-01", effective_to="2026-03-31")
    await authored_assignment(p3c_client, tenant, definition, "30", effective_from="2026-06-01")
    assert (await _get(p3c_client, tenant, first))["effective_to"] == "2026-03-31"


async def test_successor_must_take_effect_after_the_current_schedule(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    first = await authored_assignment(p3c_client, tenant, definition, "25",
                                      effective_from="2026-03-01")
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2026-03-01")
    await set_scalar_value(p3c_client, tenant, pending, definition, "9")
    response = await approve_assignment(p3c_client, tenant, pending)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SUCCESSOR_NOT_AFTER_CURRENT"
    assert (await _get(p3c_client, tenant, first))["status"] == "Approved"
    assert (await _get(p3c_client, tenant, pending))["status"] == "Pending"


async def test_incomplete_successor_leaves_the_current_schedule_untouched(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    first = await authored_assignment(p3c_client, tenant, definition, "25")
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2026-09-01")
    response = await approve_assignment(p3c_client, tenant, pending)
    assert response.status_code == 422
    current = await _get(p3c_client, tenant, first)
    assert (current["status"], current["effective_to"]) == ("Approved", None)


async def test_a_failed_successor_approval_rolls_back_the_supersession(
    p3c_client, tenant, cur,
):
    definition = await create_definition(p3c_client, tenant)
    later = await authored_assignment(p3c_client, tenant, definition, "5",
                                      effective_from="2026-06-01", effective_to="2026-06-30")
    cur.execute("""
        UPDATE payroll.driverrateassignments SET status = 'Superseded'
        WHERE driverrateassignmentid = %s
    """, (_id(later),))
    current = await authored_assignment(p3c_client, tenant, definition, "25",
                                        effective_from="2026-01-01", effective_to="2026-03-31")
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2026-05-01")
    await set_scalar_value(p3c_client, tenant, pending, definition, "30")

    response = await approve_assignment(p3c_client, tenant, pending)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ASSIGNMENT_WINDOW_OVERLAP"
    assert (await _get(p3c_client, tenant, current))["status"] == "Approved"
    assert (await _get(p3c_client, tenant, pending))["status"] == "Pending"


async def test_assignments_are_independent_per_driver_and_definition(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    other_definition = await create_definition(p3c_client, tenant)
    await authored_assignment(p3c_client, tenant, definition, "1")
    await authored_assignment(p3c_client, tenant, definition, "2", driver_id=tenant.driver_a2)
    await authored_assignment(p3c_client, tenant, other_definition, "3")
    summary = (await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a}/rate-assignments/summary",
        headers=tenant.admin)).json()
    assert {item["rate_definition_id"] for item in summary} == {
        definition["rate_definition_id"], other_definition["rate_definition_id"]}
    assert all(item["approved_assignment_id"] is not None for item in summary)
    assert all(item["pending_assignment_id"] is None for item in summary)


# ---------------------------------------------------------------------------
# Discard and void
# ---------------------------------------------------------------------------

async def test_discard_deletes_pending_with_values_and_audits_the_authenticated_actor(
    p3c_client, tenant, cur,
):
    definition, pending = await _definition_and_pending(p3c_client, tenant)
    await set_scalar_value(p3c_client, tenant, pending, definition, "7.25")

    response = await p3c_client.delete(
        f"{BASE}/{_id(pending)}", headers=tenant.headers(tenant.branch_user))
    assert response.status_code == 204
    assert (await p3c_client.get(f"{BASE}/{_id(pending)}", headers=tenant.admin)).status_code == 404
    cur.execute("SELECT count(*) FROM payroll.driverratevalues WHERE driverrateassignmentid = %s",
                (_id(pending),))
    assert cur.fetchone()[0] == 0

    cur.execute("""
        SELECT actoruserid, oldvaluejson, companyid, branchid FROM audit.auditlog
        WHERE actioncode = 'DRIVER_RATE_ASSIGNMENT_DISCARDED' AND entityid = %s
    """, (str(_id(pending)),))
    actor, payload, company_id, branch_id = cur.fetchone()
    assert (actor, company_id, branch_id) == (tenant.branch_user, tenant.company_id,
                                              tenant.branch_a)
    values = json.loads(payload)["Values"]
    assert [(v["RateComponentDefinitionID"], float(v["Amount"])) for v in values] == [
        (definition["components"][0]["rate_component_definition_id"], 7.25)]

    replacement = await create_pending(p3c_client, tenant, definition)
    assert replacement["status"] == "Pending"


async def test_service_discard_sets_the_actor_only_for_its_transaction(
    p3c_client, p3c_engine, tenant, cur,
):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    definition = await create_definition(p3c_client, tenant)
    first = await create_pending(p3c_client, tenant, definition, driver_id=tenant.driver_a)
    second = await create_pending(p3c_client, tenant, definition, driver_id=tenant.driver_a2)
    single = create_async_engine(str(p3c_engine.url), pool_size=1, max_overflow=0)
    try:
        async with single.begin() as conn:
            await assignment_service.discard(tenant.company_id, tenant.owner, _id(first), conn)
            assert (await conn.execute(
                text("SELECT current_setting('flussra.actor_user_id', true)"))).scalar_one() \
                == str(tenant.owner)
        async with single.begin() as conn:
            assert (await conn.execute(
                text("SELECT current_setting('flussra.actor_user_id', true)"))).scalar_one() in (None, "")

        with pytest.raises(RuntimeError):
            async with single.begin() as conn:
                await assignment_service.discard(
                    tenant.company_id, tenant.owner, _id(second), conn)
                raise RuntimeError("roll back")
        async with single.begin() as conn:
            assert (await conn.execute(
                text("SELECT current_setting('flussra.actor_user_id', true)"))).scalar_one() in (None, "")
        assert (await p3c_client.get(f"{BASE}/{_id(second)}", headers=tenant.admin)).status_code == 200

        async with single.begin() as conn:
            await assignment_service.discard(
                tenant.company_id, tenant.branch_user, _id(second), conn)
    finally:
        await single.dispose()
    cur.execute("""
        SELECT entityid, actoruserid FROM audit.auditlog
        WHERE actioncode = 'DRIVER_RATE_ASSIGNMENT_DISCARDED' AND entityid = ANY(%s)
        ORDER BY entityid
    """, ([str(_id(first)), str(_id(second))],))
    assert dict(cur.fetchall()) == {str(_id(first)): tenant.owner,
                                    str(_id(second)): tenant.branch_user}


async def test_pending_cannot_be_voided_and_authoritative_assignments_can(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    first = await authored_assignment(p3c_client, tenant, definition, "25",
                                      effective_from="2026-01-01")
    second = await authored_assignment(p3c_client, tenant, definition, "30",
                                       effective_from="2026-06-01")
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2027-01-01")

    refused = await p3c_client.post(
        f"{BASE}/{_id(pending)}/void", headers=tenant.admin, json={"reason": "no"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "PENDING_CANNOT_BE_VOIDED"
    assert (await _get(p3c_client, tenant, pending))["status"] == "Pending"

    for assignment in (first, second):
        voided = await p3c_client.post(
            f"{BASE}/{_id(assignment)}/void", headers=tenant.admin, json={"reason": "wrong"})
        assert voided.status_code == 200, voided.text
        assert voided.json()["status"] == "Voided"
        assert voided.json()["voided_by_user_id"] == tenant.owner
        assert voided.json()["void_reason"] == "wrong"
        assert voided.json()["values"][0]["amount"] is not None
    again = await p3c_client.post(
        f"{BASE}/{_id(first)}/void", headers=tenant.admin, json={"reason": "again"})
    assert again.status_code == 409
    empty_reason = await p3c_client.post(
        f"{BASE}/{_id(second)}/void", headers=tenant.admin, json={"reason": " "})
    assert empty_reason.status_code == 422


async def test_voided_window_is_reusable_by_a_new_schedule(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    first = await authored_assignment(p3c_client, tenant, definition, "25",
                                      effective_from="2026-01-01")
    await p3c_client.post(f"{BASE}/{_id(first)}/void", headers=tenant.admin,
                          json={"reason": "wrong"})
    replacement = await authored_assignment(p3c_client, tenant, definition, "26",
                                            effective_from="2026-01-01")
    assert replacement["status"] == "Approved"


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

async def test_unconfigured_company_can_read_but_not_write_or_approve_money(
    p3c_client, unconfigured_tenant, cur,
):
    tenant = unconfigured_tenant
    definition = await create_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)

    write = await set_scalar_value(p3c_client, tenant, pending, definition, "5")
    assert write.status_code == 422
    assert write.json()["detail"]["code"] == "COMPANY_CURRENCY_REQUIRED"
    approval = await approve_assignment(p3c_client, tenant, pending)
    assert approval.status_code == 422
    assert approval.json()["detail"]["code"] == "COMPANY_CURRENCY_REQUIRED"
    assert (await _get(p3c_client, tenant, pending))["values"][0]["amount"] is None
    assert (await p3c_client.get(
        f"/compensation/pay-definitions/{definition['pay_definition_id']}",
        headers=tenant.admin)).status_code == 200

    cur.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s",
                (tenant.company_id,))
    assert (await set_scalar_value(p3c_client, tenant, pending, definition, "5")).status_code == 200
    assert (await approve_assignment(p3c_client, tenant, pending)).status_code == 200


async def test_authoritative_and_voided_target_state_keeps_the_currency_immutable(
    p3c_client, tenant, cur,
):
    from psycopg2 import errors

    definition = await create_definition(p3c_client, tenant)
    cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (tenant.company_id,))
    assert cur.fetchone()[0] is False

    pending = await create_pending(p3c_client, tenant, definition)
    await set_scalar_value(p3c_client, tenant, pending, definition, "5")
    cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (tenant.company_id,))
    assert cur.fetchone()[0] is False

    await approve_assignment(p3c_client, tenant, pending)
    for state in ("Approved", "Voided"):
        if state == "Voided":
            await p3c_client.post(f"{BASE}/{_id(pending)}/void", headers=tenant.admin,
                                  json={"reason": "wrong"})
        cur.execute("SELECT core.fn_company_has_durable_monetary_state(%s)", (tenant.company_id,))
        assert cur.fetchone()[0] is True
        with pytest.raises(errors.CheckViolation, match="COMPANY_CURRENCY_CHANGE_BLOCKED"):
            cur.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s",
                        (tenant.company_id,))


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

async def test_assignments_are_isolated_between_companies(p3c_client, tenant, p3b_dsn):
    other = build_tenant(p3b_dsn)
    definition, pending = await _definition_and_pending(p3c_client, tenant)

    assert (await p3c_client.get(
        f"{BASE}/{_id(pending)}", headers=other.admin)).status_code == 404
    for call in (
        p3c_client.patch(f"{BASE}/{_id(pending)}", headers=other.admin, json={"notes": "x"}),
        p3c_client.put(f"{BASE}/{_id(pending)}/values", headers=other.admin,
                       json={"values": []}),
        p3c_client.post(f"{BASE}/{_id(pending)}/approve", headers=other.admin),
        p3c_client.delete(f"{BASE}/{_id(pending)}", headers=other.admin),
        p3c_client.post(f"{BASE}/{_id(pending)}/void", headers=other.admin,
                        json={"reason": "x"}),
    ):
        assert (await call).status_code == 404

    foreign_driver = await p3c_client.post(
        BASE, headers=other.admin,
        json={"driver_id": tenant.driver_a, "rate_definition_id": definition["rate_definition_id"],
              "effective_from": "2026-01-01"})
    assert foreign_driver.status_code == 404
    foreign_definition = await p3c_client.post(
        BASE, headers=other.admin,
        json={"driver_id": other.driver_a, "rate_definition_id": definition["rate_definition_id"],
              "effective_from": "2026-01-01"})
    assert foreign_definition.status_code == 404
    assert (await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a}/rate-definitions/"
        f"{definition['rate_definition_id']}/assignments", headers=other.admin)).status_code == 404


async def test_branch_scoped_authors_act_only_on_their_branch_drivers(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    branch_user = tenant.headers(tenant.branch_user)
    own = await create_pending(p3c_client, tenant, definition, headers=branch_user)
    foreign = await p3c_client.post(
        BASE, headers=branch_user,
        json={"driver_id": tenant.driver_b, "rate_definition_id": definition["rate_definition_id"],
              "effective_from": "2026-01-01"})
    assert foreign.status_code == 403
    admin_created = await create_pending(p3c_client, tenant, definition, driver_id=tenant.driver_b)
    assert (await p3c_client.get(
        f"{BASE}/{_id(admin_created)}", headers=branch_user)).status_code == 403
    assert (await p3c_client.delete(
        f"{BASE}/{_id(admin_created)}", headers=branch_user)).status_code == 403
    assert (await p3c_client.get(f"{BASE}/{_id(own)}", headers=branch_user)).status_code == 200


async def test_read_only_and_unpermissioned_users_cannot_mutate_or_read(p3c_client, tenant):
    definition, pending = await _definition_and_pending(p3c_client, tenant)
    viewer = tenant.headers(tenant.viewer)
    assert (await p3c_client.get(f"{BASE}/{_id(pending)}", headers=viewer)).status_code == 200
    assert (await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a}/rate-definitions/"
        f"{definition['rate_definition_id']}/assignments", headers=viewer)).status_code == 200
    for call in (
        p3c_client.post(BASE, headers=viewer,
                        json={"driver_id": tenant.driver_a2,
                              "rate_definition_id": definition["rate_definition_id"],
                              "effective_from": "2026-01-01"}),
        p3c_client.patch(f"{BASE}/{_id(pending)}", headers=viewer, json={"notes": "x"}),
        p3c_client.put(f"{BASE}/{_id(pending)}/values", headers=viewer, json={"values": []}),
        p3c_client.post(f"{BASE}/{_id(pending)}/approve", headers=viewer),
        p3c_client.delete(f"{BASE}/{_id(pending)}", headers=viewer),
    ):
        assert (await call).status_code == 403
    nobody = tenant.headers(tenant.no_permissions)
    assert (await p3c_client.get(f"{BASE}/{_id(pending)}", headers=nobody)).status_code == 403
    assert (await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a}/rate-assignments/summary",
        headers=nobody)).status_code == 403


async def test_driver_self_accounts_are_denied_generic_compensation_administration(
    session_client, auth_token, hq_branch_id,
):
    from uuid import uuid4

    from tests.builders.access import create_user_with_role_token, get_company_role_id

    role_id = await get_company_role_id(session_client, auth_token, "DRIVER")
    token = await create_user_with_role_token(
        session_client, auth_token, f"p3c_{uuid4().hex[:10]}", role_id,
        scope_type="Self", driver_branch_id=hq_branch_id)
    headers = {"Authorization": f"Bearer {token}"}
    requests = [
        ("get", "/compensation/pay-definitions", None),
        ("post", "/compensation/pay-definitions",
         {"definition_name": "x", "input_type": "Decimal", "calculation_method": "PerUnit"}),
        ("get", "/compensation/pay-definition-requests", None),
        ("post", "/compensation/pay-definition-requests", {"requesting_branch_id": hq_branch_id}),
        ("post", BASE, {"driver_id": 1, "rate_definition_id": 1, "effective_from": "2026-01-01"}),
        ("get", "/compensation/drivers/1/rate-assignments/summary", None),
        ("get", "/compensation/drivers/1/rate-definitions/1/resolution?work_date=2026-01-01", None),
    ]
    for method, url, body in requests:
        response = await getattr(session_client, method)(url, headers=headers,
                                                          **({"json": body} if body else {}))
        assert response.status_code == 403, (method, url, response.status_code, response.text)
