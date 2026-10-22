"""OrdinalTier through the generic PayDefinition governance and DriverRateSchedule lifecycle.

An OrdinalTier definition is requested, approved or created directly exactly like a PerUnit one;
its topology is definition-owned and a Driver's schedule supplies one complete value set for it.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from psycopg2 import errors

from app.compensation import resolver
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    ORDINAL_TIERS,
    approve_schedule,
    authored_tier_schedule,
    build_tenant,
    create_definition,
    create_ordinal_definition,
    create_pending,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
    p3c_unconfigured_tenant,
    set_tier_values,
)
from tests.compensation_database_fixtures import (  # noqa: F401 - register fixtures
    p3b_cursor,
    p3b_database,
)

pytestmark = pytest.mark.asyncio

DEFINITIONS = "/compensation/pay-definitions"
REQUESTS = "/compensation/pay-definition-requests"
SCHEDULES = "/compensation/driver-rate-schedules"


def _bounds(definition: dict) -> list[tuple[int, int | None]]:
    return [(c["ordinal_from"], c["ordinal_to"]) for c in definition["components"]]


def _definition_count(cur, company_id: int) -> int:
    cur.execute("SELECT count(*) FROM payroll.paydefinitions WHERE companyid = %s", (company_id,))
    return cur.fetchone()[0]


async def _draft(client, tenant, **fields):
    body = {
        "requesting_branch_id": tenant.branch_a,
        "definition_name": "Tiered item",
        "input_type": "WholeNumber",
        "unit": "stop",
        "calculation_method": "OrdinalTier",
        "ordinal_tiers": ORDINAL_TIERS,
    } | fields
    return await client.post(REQUESTS, json=body, headers=tenant.headers(tenant.branch_user))


async def _submit(client, tenant, request):
    return await client.post(
        f"{REQUESTS}/{request['request_id']}/submit",
        json={"expected_revision": request["revision"]},
        headers=tenant.headers(tenant.branch_user))


async def _decide(client, tenant, request, action="Approve"):
    return await client.post(
        f"{REQUESTS}/{request['request_id']}/decide",
        json={"action": action, "expected_revision": request["revision"], "reason": "because"},
        headers=tenant.admin)


# ---------------------------------------------------------------------------
# Definition governance: direct creation
# ---------------------------------------------------------------------------

async def test_direct_creation_builds_the_definition_owned_topology(p3c_client, tenant, cur):
    created = await create_ordinal_definition(
        p3c_client, tenant, definition_code="ITEM_BETA", definition_name="Beta route")
    assert created["calculation_method"] == "OrdinalTier"
    assert created["input_type"] == "WholeNumber"
    assert created["rate_shape"] == "OrdinalTierSchedule"
    assert _bounds(created) == [(1, 1), (2, 2), (3, None)]
    assert [c["sequence_no"] for c in created["components"]] == [1, 2, 3]
    # The governed event is the first authoritative use of the structure, as for PerUnit.
    assert created["structure_locked_at_utc"] is not None
    assert created["provenance"]["creation_mode"] == "DirectCreate"
    assert created["provenance"]["calculation_method_version"] == 1
    cur.execute("""
        SELECT c.sequenceno, c.shape, c.ordinalfrom, c.ordinalto
        FROM payroll.ratecomponentdefinitions c WHERE c.ratedefinitionid = %s ORDER BY 1
    """, (created["rate_definition_id"],))
    assert cur.fetchall() == [
        (1, "OrdinalTierSchedule", 1, 1), (2, "OrdinalTierSchedule", 2, 2),
        (3, "OrdinalTierSchedule", 3, None)]


async def test_a_single_open_ended_tier_is_a_valid_topology(p3c_client, tenant):
    created = await create_ordinal_definition(
        p3c_client, tenant, ordinal_tiers=[{"ordinal_from": 1, "ordinal_to": None}])
    assert _bounds(created) == [(1, None)]


async def test_perunit_and_ordinal_tier_definitions_coexist_with_arbitrary_names(
    p3c_client, tenant,
):
    per_unit = await create_definition(p3c_client, tenant, definition_code="ITEM_ALPHA")
    tiered = await create_ordinal_definition(p3c_client, tenant, definition_code="HOURS")
    assert (per_unit["rate_shape"], tiered["rate_shape"]) == ("Scalar", "OrdinalTierSchedule")
    assert [c["ordinal_from"] for c in per_unit["components"]] == [None]
    listing = (await p3c_client.get(DEFINITIONS, headers=tenant.admin)).json()
    assert {d["definition_code"]: d["calculation_method"] for d in listing} == {
        "ITEM_ALPHA": "PerUnit", "HOURS": "OrdinalTier"}


@pytest.mark.parametrize(("name", "tiers"), [
    ("a gap", [(1, 1), (3, None)]),
    ("an overlap", [(1, 3), (3, None)]),
    ("a first tier that does not start at 1", [(2, None)]),
    ("a zero lower bound", [(0, None)]),
    ("a negative bound", [(1, -2), (-1, None)]),
    ("an open-ended tier that is not final", [(1, None), (2, None)]),
    ("a bounded final tier", [(1, 1), (2, 2)]),
    ("a tier ending before it starts", [(1, 2), (3, 2), (3, None)]),
    ("no tiers", []),
])
async def test_invalid_topologies_are_rejected_without_creating_anything(
    p3c_client, tenant, cur, name, tiers,
):
    before = _definition_count(cur, tenant.company_id)
    response = await p3c_client.post(DEFINITIONS, headers=tenant.admin, json={
        "definition_name": "Tiered", "input_type": "WholeNumber",
        "calculation_method": "OrdinalTier",
        "ordinal_tiers": [{"ordinal_from": low, "ordinal_to": high} for low, high in tiers]})
    assert response.status_code == 422, name
    assert response.json()["detail"]["code"] == "INVALID_TIER_TOPOLOGY"
    assert _definition_count(cur, tenant.company_id) == before


async def test_a_definition_without_a_topology_or_with_fractional_bounds_is_rejected(
    p3c_client, tenant, cur,
):
    before = _definition_count(cur, tenant.company_id)
    missing = await p3c_client.post(DEFINITIONS, headers=tenant.admin, json={
        "definition_name": "Tiered", "input_type": "WholeNumber",
        "calculation_method": "OrdinalTier"})
    assert (missing.status_code, missing.json()["detail"]["code"]) == (422, "INVALID_TIER_TOPOLOGY")
    fractional = await p3c_client.post(DEFINITIONS, headers=tenant.admin, json={
        "definition_name": "Tiered", "input_type": "WholeNumber",
        "calculation_method": "OrdinalTier",
        "ordinal_tiers": [{"ordinal_from": 1, "ordinal_to": 1.5}, {"ordinal_from": 2.5}]})
    assert fractional.status_code == 422
    assert _definition_count(cur, tenant.company_id) == before


async def test_an_ordinal_tier_definition_requires_whole_number_input(p3c_client, tenant, cur):
    before = _definition_count(cur, tenant.company_id)
    response = await p3c_client.post(DEFINITIONS, headers=tenant.admin, json={
        "definition_name": "Tiered", "input_type": "Decimal",
        "calculation_method": "OrdinalTier", "ordinal_tiers": ORDINAL_TIERS})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ORDINAL_TIER_REQUIRES_WHOLE_NUMBER"
    assert _definition_count(cur, tenant.company_id) == before


async def test_a_per_unit_definition_takes_no_tiers(p3c_client, tenant, cur):
    before = _definition_count(cur, tenant.company_id)
    response = await p3c_client.post(DEFINITIONS, headers=tenant.admin, json={
        "definition_name": "Plain", "input_type": "Decimal", "calculation_method": "PerUnit",
        "ordinal_tiers": ORDINAL_TIERS})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "TIERS_NOT_APPLICABLE"
    assert _definition_count(cur, tenant.company_id) == before


@pytest.mark.parametrize("method", ["RangeBracket", "RangeProgressive", "Block", "Fixed",
                                    "Calculated", "EnteredAmount"])
async def test_unsupported_methods_are_not_selectable(p3c_client, tenant, method):
    direct = await p3c_client.post(DEFINITIONS, headers=tenant.admin, json={
        "definition_name": "Other", "input_type": "WholeNumber", "calculation_method": method})
    assert direct.status_code == 422
    draft = await _draft(p3c_client, tenant, calculation_method=method)
    assert draft.status_code == 422


async def test_only_company_wide_editors_create_definitions_directly(p3c_client, tenant):
    body = {"definition_name": "Tiered", "input_type": "WholeNumber",
            "calculation_method": "OrdinalTier", "ordinal_tiers": ORDINAL_TIERS}
    for user in (tenant.branch_user, tenant.viewer, tenant.no_permissions):
        response = await p3c_client.post(DEFINITIONS, json=body, headers=tenant.headers(user))
        assert response.status_code == 403


async def test_structure_of_a_governed_definition_is_locked_from_the_start(
    p3c_client, tenant, cur,
):
    created = await create_ordinal_definition(p3c_client, tenant)
    rate_definition_id = created["rate_definition_id"]
    for statement in (
        "UPDATE payroll.ratecomponentdefinitions SET ordinalto = 9 WHERE ratedefinitionid = %s",
        "DELETE FROM payroll.ratecomponentdefinitions WHERE ratedefinitionid = %s",
    ):
        with pytest.raises(errors.CheckViolation, match="RATE_STRUCTURE_LOCKED"):
            cur.execute(statement, (rate_definition_id,))
    with pytest.raises(errors.CheckViolation, match="RATE_STRUCTURE_LOCKED"):
        cur.execute(
            "UPDATE payroll.paydefinitions SET calculationmethod = 'PerUnit', "
            "inputtype = 'Decimal' WHERE paydefinitionid = %s", (created["pay_definition_id"],))


# ---------------------------------------------------------------------------
# Definition governance: request, approval and provenance
# ---------------------------------------------------------------------------

async def test_a_request_carries_its_topology_through_approval(p3c_client, tenant):
    draft = (await _draft(p3c_client, tenant, definition_code="ITEM_BETA")).json()
    assert draft["calculation_method"] == "OrdinalTier"
    assert draft["ordinal_tiers"] == ORDINAL_TIERS
    submitted = (await _submit(p3c_client, tenant, draft)).json()
    approved = await _decide(p3c_client, tenant, submitted)
    assert approved.status_code == 200, approved.text
    request = approved.json()
    assert request["status"] == "Approved" and request["ordinal_tiers"] == ORDINAL_TIERS

    definition = (await p3c_client.get(
        f"{DEFINITIONS}/{request['approved_pay_definition_id']}", headers=tenant.admin)).json()
    assert definition["calculation_method"] == "OrdinalTier"
    assert _bounds(definition) == [(1, 1), (2, 2), (3, None)]
    assert definition["structure_locked_at_utc"] is not None
    provenance = definition["provenance"]
    assert provenance["creation_mode"] == "Request"
    assert provenance["source_request_id"] == request["request_id"]
    assert provenance["requesting_branch_id"] == tenant.branch_a
    assert provenance["calculation_method_version"] == 1


async def test_a_draft_topology_is_editable_while_a_draft_and_cleared_with_null(
    p3c_client, tenant,
):
    draft = (await _draft(p3c_client, tenant, ordinal_tiers=None)).json()
    assert draft["ordinal_tiers"] is None
    updated = await p3c_client.patch(
        f"{REQUESTS}/{draft['request_id']}", headers=tenant.headers(tenant.branch_user),
        json={"expected_revision": draft["revision"], "ordinal_tiers": [
            {"ordinal_from": 1, "ordinal_to": 4}, {"ordinal_from": 5, "ordinal_to": None}]})
    assert updated.status_code == 200, updated.text
    assert updated.json()["ordinal_tiers"] == [
        {"ordinal_from": 1, "ordinal_to": 4}, {"ordinal_from": 5, "ordinal_to": None}]
    stale = await p3c_client.patch(
        f"{REQUESTS}/{draft['request_id']}", headers=tenant.headers(tenant.branch_user),
        json={"expected_revision": draft["revision"], "ordinal_tiers": []})
    assert stale.status_code == 409


async def test_a_request_without_a_valid_topology_cannot_be_submitted(p3c_client, tenant):
    absent = (await _draft(p3c_client, tenant, ordinal_tiers=None)).json()
    response = await _submit(p3c_client, tenant, absent)
    assert (response.status_code, response.json()["detail"]["code"]) == (
        422, "REQUEST_INCOMPLETE")
    for tiers in ([(1, 1), (3, None)], [(1, 1), (2, 2)], [(2, None)]):
        draft = (await _draft(p3c_client, tenant, ordinal_tiers=[
            {"ordinal_from": low, "ordinal_to": high} for low, high in tiers])).json()
        response = await _submit(p3c_client, tenant, draft)
        assert (response.status_code, response.json()["detail"]["code"]) == (
            422, "INVALID_TIER_TOPOLOGY")
    decimal = (await _draft(p3c_client, tenant, input_type="Decimal")).json()
    response = await _submit(p3c_client, tenant, decimal)
    assert response.json()["detail"]["code"] == "ORDINAL_TIER_REQUIRES_WHOLE_NUMBER"


async def test_approval_validates_the_topology_again_and_creates_nothing_when_invalid(
    p3c_client, tenant, cur,
):
    submitted = (await _submit(
        p3c_client, tenant, (await _draft(p3c_client, tenant)).json())).json()
    cur.execute(
        "UPDATE payroll.paydefinitionrequests SET ordinaltiers = %s::jsonb "
        "WHERE paydefinitionrequestid = %s",
        (json.dumps([{"ordinal_from": 1, "ordinal_to": 1}, {"ordinal_from": 3}]),
         submitted["request_id"]))
    before = _definition_count(cur, tenant.company_id)
    response = await _decide(p3c_client, tenant, submitted)
    assert (response.status_code, response.json()["detail"]["code"]) == (
        422, "INVALID_TIER_TOPOLOGY")
    assert _definition_count(cur, tenant.company_id) == before
    cur.execute("SELECT status FROM payroll.paydefinitionrequests WHERE paydefinitionrequestid = %s",
                (submitted["request_id"],))
    assert cur.fetchone() == ("PendingCompanyApproval",)


async def test_a_rejected_request_copies_its_topology(p3c_client, tenant):
    submitted = (await _submit(
        p3c_client, tenant, (await _draft(
            p3c_client, tenant, ordinal_tiers=[
                {"ordinal_from": 1, "ordinal_to": 2}, {"ordinal_from": 3}])).json())).json()
    rejected = (await _decide(p3c_client, tenant, submitted, "Reject")).json()
    copy = await p3c_client.post(
        f"{REQUESTS}/{rejected['request_id']}/copy", headers=tenant.headers(tenant.branch_user))
    assert copy.status_code in (200, 201), copy.text
    assert copy.json()["ordinal_tiers"] == [
        {"ordinal_from": 1, "ordinal_to": 2}, {"ordinal_from": 3, "ordinal_to": None}]


# ---------------------------------------------------------------------------
# DriverRateSchedule lifecycle: one atomic complete value set for all tiers
# ---------------------------------------------------------------------------

async def test_a_pending_tier_schedule_starts_with_every_tier_value_unset(p3c_client, tenant):
    definition = await create_ordinal_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)
    assert pending["status"] == "Pending"
    assert [(v["sequence_no"], v["ordinal_from"], v["ordinal_to"], v["amount"])
            for v in pending["values"]] == [(1, 1, 1, None), (2, 2, 2, None), (3, 3, None, None)]


async def test_a_complete_set_approves_as_one_authoritative_schedule(p3c_client, tenant):
    definition = await create_ordinal_definition(p3c_client, tenant)
    approved = await authored_tier_schedule(p3c_client, tenant, definition, ("3", "4", "5"))
    assert approved["status"] == "Approved"
    assert approved["pay_definition_id"] == definition["pay_definition_id"]
    assert [Decimal(v["amount"]) for v in approved["values"]] == [3, 4, 5]
    # Authoritative values are immutable, like any schedule's.
    response = await set_tier_values(
        p3c_client, tenant, approved, definition, ("9", "9", "9"))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SCHEDULE_NOT_PENDING"


@pytest.mark.parametrize("amounts", [
    ("3", "4", None), (None, None, None), ("3", None, "5"),
])
async def test_a_partial_value_set_can_never_become_authoritative(p3c_client, tenant, amounts):
    definition = await create_ordinal_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)
    assert (await set_tier_values(p3c_client, tenant, pending, definition, amounts)).status_code == 200
    response = await approve_schedule(p3c_client, tenant, pending)
    assert (response.status_code, response.json()["detail"]["code"]) == (
        422, "SCHEDULE_INCOMPLETE")
    stored = (await p3c_client.get(
        f"{SCHEDULES}/{pending['driver_rate_schedule_id']}", headers=tenant.admin)).json()
    assert stored["status"] == "Pending"


async def test_a_submitted_set_that_omits_a_tier_is_incomplete_not_zero(p3c_client, tenant):
    definition = await create_ordinal_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)
    first_two = definition["components"][:2]
    response = await p3c_client.put(
        f"{SCHEDULES}/{pending['driver_rate_schedule_id']}/values", headers=tenant.admin,
        json={"values": [{"rate_component_definition_id": c["rate_component_definition_id"],
                          "amount": "1"} for c in first_two]})
    assert response.status_code == 200
    assert [v["amount"] for v in response.json()["values"]] == ["1.0000", "1.0000", None]
    approval = await approve_schedule(p3c_client, tenant, pending)
    assert approval.json()["detail"]["code"] == "SCHEDULE_INCOMPLETE"


async def test_explicit_zero_tier_values_are_valid_and_distinct_from_missing(p3c_client, tenant):
    definition = await create_ordinal_definition(p3c_client, tenant)
    approved = await authored_tier_schedule(p3c_client, tenant, definition, ("0", "0", "0"))
    assert [v["amount"] for v in approved["values"]] == ["0.0000", "0.0000", "0.0000"]


@pytest.mark.parametrize("amount", ["-1", "1.00005", "abc"])
async def test_invalid_tier_amounts_are_rejected(p3c_client, tenant, amount):
    definition = await create_ordinal_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)
    response = await set_tier_values(
        p3c_client, tenant, pending, definition, ("1", amount, "1"))
    assert response.status_code == 422


async def test_values_must_reference_this_definitions_tiers_once(p3c_client, tenant):
    definition = await create_ordinal_definition(p3c_client, tenant)
    other = await create_ordinal_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)
    foreign = other["components"][0]["rate_component_definition_id"]
    own = definition["components"][0]["rate_component_definition_id"]
    unknown = await p3c_client.put(
        f"{SCHEDULES}/{pending['driver_rate_schedule_id']}/values", headers=tenant.admin,
        json={"values": [{"rate_component_definition_id": foreign, "amount": "1"}]})
    assert (unknown.status_code, unknown.json()["detail"]["code"]) == (422, "UNKNOWN_COMPONENT")
    duplicate = await p3c_client.put(
        f"{SCHEDULES}/{pending['driver_rate_schedule_id']}/values", headers=tenant.admin,
        json={"values": [{"rate_component_definition_id": own, "amount": "1"},
                         {"rate_component_definition_id": own, "amount": "2"}]})
    assert duplicate.json()["detail"]["code"] == "DUPLICATE_COMPONENT_VALUE"


async def test_a_successor_supersedes_the_whole_schedule_and_history_keeps_every_value(
    p3c_client, tenant,
):
    definition = await create_ordinal_definition(p3c_client, tenant)
    first = await authored_tier_schedule(
        p3c_client, tenant, definition, ("3", "4", "5"), effective_from="2026-01-01")
    second = await authored_tier_schedule(
        p3c_client, tenant, definition, ("6", "7", "8"), effective_from="2026-06-01")
    assert second["status"] == "Approved"
    history = (await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a_id}/rate-definitions/"
        f"{definition['rate_definition_id']}/rate-schedules", headers=tenant.admin)).json()
    assert [(h["status"], h["effective_from"], h["effective_to"],
             [Decimal(v["amount"]) for v in h["values"]]) for h in history] == [
        ("Approved", "2026-06-01", None, [6, 7, 8]),
        ("Superseded", "2026-01-01", "2026-05-31", [3, 4, 5])]
    assert history[1]["driver_rate_schedule_id"] == first["driver_rate_schedule_id"]


async def test_one_pending_schedule_per_driver_and_tier_definition_and_discard_clears_it(
    p3c_client, tenant, cur,
):
    definition = await create_ordinal_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)
    await set_tier_values(p3c_client, tenant, pending, definition, ("1", "2", "3"))
    again = await p3c_client.post(SCHEDULES, headers=tenant.admin, json={
        "driver_id": tenant.driver_a_id, "rate_definition_id": definition["rate_definition_id"],
        "effective_from": "2026-02-01"})
    assert (again.status_code, again.json()["detail"]["code"]) == (409, "PENDING_SCHEDULE_EXISTS")
    discarded = await p3c_client.delete(
        f"{SCHEDULES}/{pending['driver_rate_schedule_id']}", headers=tenant.admin)
    assert discarded.status_code == 204
    cur.execute("""
        SELECT count(*) FROM payroll.driverrateschedulevalues WHERE driverratescheduleid = %s
    """, (pending["driver_rate_schedule_id"],))
    assert cur.fetchone() == (0,)


async def test_an_authoritative_tier_schedule_can_be_voided_and_its_window_reused(
    p3c_client, tenant,
):
    definition = await create_ordinal_definition(p3c_client, tenant)
    approved = await authored_tier_schedule(p3c_client, tenant, definition, effective_from="2026-01-01")
    voided = await p3c_client.post(
        f"{SCHEDULES}/{approved['driver_rate_schedule_id']}/void", headers=tenant.admin,
        json={"reason": "wrong values"})
    assert voided.status_code == 200 and voided.json()["status"] == "Voided"
    again = await authored_tier_schedule(
        p3c_client, tenant, definition, ("1", "1", "1"), effective_from="2026-01-01")
    assert again["status"] == "Approved"


async def test_schedules_of_two_drivers_and_two_definitions_are_independent(p3c_client, tenant):
    first = await create_ordinal_definition(p3c_client, tenant)
    second = await create_ordinal_definition(p3c_client, tenant)
    a = await authored_tier_schedule(p3c_client, tenant, first, ("1", "2", "3"))
    b = await authored_tier_schedule(
        p3c_client, tenant, first, ("4", "5", "6"), driver_id=tenant.driver_a2_id)
    c = await authored_tier_schedule(p3c_client, tenant, second, ("7", "8", "9"))
    assert len({a["driver_rate_schedule_id"], b["driver_rate_schedule_id"],
                c["driver_rate_schedule_id"]}) == 3


async def test_an_unconfigured_company_cannot_write_or_approve_tier_money(
    p3c_client, unconfigured_tenant, cur,
):
    tenant = unconfigured_tenant
    definition = await create_ordinal_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)
    write = await set_tier_values(p3c_client, tenant, pending, definition, ("1", "2", "3"))
    assert (write.status_code, write.json()["detail"]["code"]) == (422, "COMPANY_CURRENCY_REQUIRED")
    approval = await approve_schedule(p3c_client, tenant, pending)
    assert approval.json()["detail"]["code"] == "COMPANY_CURRENCY_REQUIRED"
    cur.execute("UPDATE core.companies SET currencycode = 'EUR' WHERE companyid = %s",
                (tenant.company_id,))
    assert (await set_tier_values(
        p3c_client, tenant, pending, definition, ("1", "2", "3"))).status_code == 200
    assert (await approve_schedule(p3c_client, tenant, pending)).status_code == 200


async def test_tier_schedules_are_isolated_between_companies(p3c_client, tenant, p3b_dsn):
    other = build_tenant(p3b_dsn)
    definition = await create_ordinal_definition(p3c_client, tenant)
    approved = await authored_tier_schedule(p3c_client, tenant, definition)
    read = await p3c_client.get(
        f"{SCHEDULES}/{approved['driver_rate_schedule_id']}", headers=other.admin)
    assert read.status_code == 404
    cross = await p3c_client.post(SCHEDULES, headers=other.admin, json={
        "driver_id": other.driver_a_id, "rate_definition_id": definition["rate_definition_id"],
        "effective_from": "2026-01-01"})
    assert cross.status_code == 404


async def test_a_branch_scoped_author_acts_only_on_drivers_of_their_branch(p3c_client, tenant):
    definition = await create_ordinal_definition(p3c_client, tenant)
    mine = await p3c_client.post(SCHEDULES, headers=tenant.headers(tenant.branch_user), json={
        "driver_id": tenant.driver_a_id, "rate_definition_id": definition["rate_definition_id"],
        "effective_from": "2026-01-01"})
    assert mine.status_code == 201, mine.text
    other_branch = await p3c_client.post(
        SCHEDULES, headers=tenant.headers(tenant.branch_user), json={
            "driver_id": tenant.driver_b_id, "rate_definition_id": definition["rate_definition_id"],
            "effective_from": "2026-01-01"})
    assert other_branch.status_code in (403, 404)


# ---------------------------------------------------------------------------
# The resolver returns the whole ordered schedule
# ---------------------------------------------------------------------------

async def test_the_resolver_returns_every_tier_of_one_schedule_never_independent_latest_values(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_ordinal_definition(p3c_client, tenant)
    first = await authored_tier_schedule(
        p3c_client, tenant, definition, ("3", "4", "5"), effective_from="2026-01-01")
    second = await authored_tier_schedule(
        p3c_client, tenant, definition, ("6", "7", "8"), effective_from="2026-06-01")

    async def resolved(day: date):
        async with p3c_engine.connect() as db:
            return await resolver.resolve(
                tenant.company_id, tenant.driver_a_id, definition["rate_definition_id"], day, db)

    early = await resolved(date(2026, 3, 1))
    assert early.driver_rate_schedule_id == first["driver_rate_schedule_id"]
    assert early.rate_shape == "OrdinalTierSchedule" and early.calculation_method == "OrdinalTier"
    assert [(c.sequence_no, c.ordinal_from, c.ordinal_to, c.amount) for c in early.components] == [
        (1, 1, 1, Decimal("3")), (2, 2, 2, Decimal("4")), (3, 3, None, Decimal("5"))]
    late = await resolved(date(2026, 6, 1))
    assert late.driver_rate_schedule_id == second["driver_rate_schedule_id"]
    assert [c.amount for c in late.components] == [Decimal("6"), Decimal("7"), Decimal("8")]
    assert await resolved(date(2025, 12, 31)) is None


async def test_a_pending_or_voided_tier_schedule_is_never_resolved(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_ordinal_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2026-01-01")
    await set_tier_values(p3c_client, tenant, pending, definition, ("3", "4", "5"))
    async with p3c_engine.connect() as db:
        assert await resolver.resolve(
            tenant.company_id, tenant.driver_a_id, definition["rate_definition_id"],
            date(2026, 2, 1), db) is None


# ---------------------------------------------------------------------------
# The Driver rate-authoring read model
# ---------------------------------------------------------------------------

def _rows(response) -> dict[int, dict]:
    assert response.status_code == 200, response.text
    return {row["rate_definition_id"]: row for row in response.json()}


async def test_the_authoring_matrix_lists_tiered_and_scalar_definitions_in_one_contract(
    p3c_client, tenant,
):
    scalar = await create_definition(p3c_client, tenant, definition_name="Alpha")
    tiered = await create_ordinal_definition(p3c_client, tenant, definition_name="Beta")
    rows = _rows(await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a_id}/pay-rates?as_of=2026-02-01",
        headers=tenant.admin))
    plain, tier_row = rows[scalar["rate_definition_id"]], rows[tiered["rate_definition_id"]]

    assert (plain["rate_shape"], plain["calculation_method"]) == ("Scalar", "PerUnit")
    assert plain["rate_component_definition_id"] == scalar["components"][0][
        "rate_component_definition_id"]
    assert (tier_row["rate_shape"], tier_row["calculation_method"]) == (
        "OrdinalTierSchedule", "OrdinalTier")
    assert tier_row["rate_component_definition_id"] is None
    assert [(c["sequence_no"], c["ordinal_from"], c["ordinal_to"])
            for c in tier_row["components"]] == [(1, 1, 1), (2, 2, 2), (3, 3, None)]
    assert tier_row["current"] is None and tier_row["pending"] is None


async def test_matrix_schedule_briefs_carry_the_whole_value_set_with_missing_not_zero(
    p3c_client, tenant,
):
    tiered = await create_ordinal_definition(p3c_client, tenant)
    await authored_tier_schedule(
        p3c_client, tenant, tiered, ("3", "0", "5"), effective_from="2026-01-01")
    pending = await create_pending(p3c_client, tenant, tiered, effective_from="2026-09-01")
    await set_tier_values(p3c_client, tenant, pending, tiered, ("1", "2", "3"))
    # Clear the middle value: it is missing, never zero.
    await p3c_client.put(
        f"{SCHEDULES}/{pending['driver_rate_schedule_id']}/values", headers=tenant.admin,
        json={"values": [
            {"rate_component_definition_id": tiered["components"][0]["rate_component_definition_id"],
             "amount": "1"},
            {"rate_component_definition_id": tiered["components"][2]["rate_component_definition_id"],
             "amount": "3"}]})
    row = _rows(await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a_id}/pay-rates?as_of=2026-02-01",
        headers=tenant.admin))[tiered["rate_definition_id"]]
    assert row["current"]["amount"] is None
    assert [v["amount"] for v in row["current"]["values"]] == ["3.0000", "0.0000", "5.0000"]
    assert [v["amount"] for v in row["pending"]["values"]] == ["1.0000", None, "3.0000"]


async def test_a_scalar_schedule_brief_keeps_its_amount_and_gains_its_one_value(
    p3c_client, tenant,
):
    from tests.compensation_api_fixtures import authored_schedule

    scalar = await create_definition(p3c_client, tenant)
    await authored_schedule(p3c_client, tenant, scalar, "25", effective_from="2026-01-01")
    row = _rows(await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a_id}/pay-rates?as_of=2026-02-01",
        headers=tenant.admin))[scalar["rate_definition_id"]]
    assert row["current"]["amount"] == "25.0000"
    assert [v["amount"] for v in row["current"]["values"]] == ["25.0000"]


async def test_a_retired_tier_definition_leaves_the_matrix_but_keeps_its_history(
    p3c_client, tenant,
):
    tiered = await create_ordinal_definition(p3c_client, tenant)
    await authored_tier_schedule(p3c_client, tenant, tiered, effective_from="2026-01-01")
    retired = await p3c_client.post(
        f"{DEFINITIONS}/{tiered['pay_definition_id']}/retire", headers=tenant.admin)
    assert retired.status_code == 200, retired.text
    rows = _rows(await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a_id}/pay-rates?as_of=2026-02-01",
        headers=tenant.admin))
    assert tiered["rate_definition_id"] not in rows
    summary = (await p3c_client.get(
        f"/compensation/drivers/{tenant.driver_a_id}/rate-schedules/summary",
        headers=tenant.admin)).json()
    assert [i["pay_definition_status"] for i in summary
            if i["rate_definition_id"] == tiered["rate_definition_id"]] == ["Retired"]
    blocked = await p3c_client.post(SCHEDULES, headers=tenant.admin, json={
        "driver_id": tenant.driver_a_id, "rate_definition_id": tiered["rate_definition_id"],
        "effective_from": "2027-01-01"})
    assert blocked.status_code == 422
