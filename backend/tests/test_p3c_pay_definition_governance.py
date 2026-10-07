"""Generic PayDefinition governance and provenance for the dormant target model."""
from __future__ import annotations

from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import errors

from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    build_tenant,
    create_definition,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
    p3c_unconfigured_tenant,
)

pytestmark = pytest.mark.asyncio

LEGACY_TABLES = (
    "ratetypes", "payitemratetypemap", "payitemrateslots", "payitems",
    "branchpayitemconfig", "driverrates", "cdpidefinitions", "cdpirequests",
)


def _legacy_counts(cur) -> dict[str, int]:
    counts = {}
    for table in LEGACY_TABLES:
        cur.execute(f"SELECT count(*) FROM payroll.{table}")
        counts[table] = cur.fetchone()[0]
    return counts


async def _draft(client, tenant, branch_id=None, headers=None, **fields):
    body = {
        "requesting_branch_id": branch_id or tenant.branch_a,
        "definition_name": "Arbitrary item",
        "input_type": "Decimal",
        "unit": "unit",
        "calculation_method": "PerUnit",
    } | fields
    return await client.post(
        "/compensation/pay-definition-requests", json=body,
        headers=headers or tenant.headers(tenant.branch_user),
    )


async def _submit(client, tenant, request, headers=None):
    return await client.post(
        f"/compensation/pay-definition-requests/{request['request_id']}/submit",
        json={"expected_revision": request["revision"]},
        headers=headers or tenant.headers(tenant.branch_user),
    )


async def _decide(client, tenant, request, action, headers=None, reason="because"):
    return await client.post(
        f"/compensation/pay-definition-requests/{request['request_id']}/decide",
        json={"action": action, "expected_revision": request["revision"], "reason": reason},
        headers=headers or tenant.admin,
    )


async def _submitted(client, tenant, **fields):
    draft = (await _draft(client, tenant, **fields)).json()
    return (await _submit(client, tenant, draft)).json()


# ---------------------------------------------------------------------------
# Direct creation
# ---------------------------------------------------------------------------

async def test_direct_create_builds_one_scalar_structure_and_provenance(p3c_client, tenant):
    created = await create_definition(
        p3c_client, tenant, definition_code="ITEM_ALPHA", definition_name="Item alpha")

    assert created["company_id"] == tenant.company_id
    assert created["calculation_method"] == "PerUnit"
    assert created["rate_shape"] == "Scalar"
    assert created["rate_definition_id"] is not None
    assert len(created["components"]) == 1
    assert created["components"][0]["sequence_no"] == 1
    assert created["components"][0]["ordinal_from"] is None
    assert created["structure_locked_at_utc"] is not None
    provenance = created["provenance"]
    assert provenance["creation_mode"] == "DirectCreate"
    assert provenance["created_by_user_id"] == tenant.owner
    assert provenance["source_request_id"] is None
    assert provenance["requesting_branch_id"] is None
    assert provenance["governance_schema_version"] == 1
    assert provenance["calculation_method_version"] == 1


async def test_creation_adds_no_legacy_routing_identities(p3c_client, tenant, cur):
    before = _legacy_counts(cur)
    await create_definition(p3c_client, tenant)
    submitted = await _submitted(p3c_client, tenant)
    approved = await _decide(p3c_client, tenant, submitted, "Approve")
    assert approved.status_code == 200, approved.text
    assert _legacy_counts(cur) == before


async def test_company_is_valid_with_zero_pay_definitions(p3c_client, tenant):
    response = await p3c_client.get("/compensation/pay-definitions", headers=tenant.admin)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("code", ["HOURS", "MILES", "LOADS", "STOPS", "CUSTOM_UNITS", "ITEM_BETA"])
async def test_arbitrary_business_codes_have_no_privileged_behavior(p3c_client, tenant, code):
    created = await create_definition(
        p3c_client, tenant, definition_code=code, definition_name=code.lower())
    assert created["definition_code"] == code
    assert created["rate_shape"] == "Scalar"
    assert len(created["components"]) == 1


async def test_omitted_code_is_generated_without_business_meaning(p3c_client, tenant):
    response = await p3c_client.post(
        "/compensation/pay-definitions",
        json={"definition_name": "No code", "input_type": "WholeNumber",
              "calculation_method": "PerUnit"},
        headers=tenant.admin)
    assert response.status_code == 201, response.text
    assert response.json()["definition_code"].startswith("PD")
    assert response.json()["input_type"] == "WholeNumber"


async def test_same_code_in_two_companies_but_not_twice_in_one(p3c_client, tenant, p3b_dsn):
    other = build_tenant(p3b_dsn)
    await create_definition(p3c_client, tenant, definition_code="SHARED")
    await create_definition(p3c_client, other, definition_code="SHARED")
    duplicate = await p3c_client.post(
        "/compensation/pay-definitions",
        json={"definition_code": "SHARED", "definition_name": "x", "input_type": "Decimal",
              "calculation_method": "PerUnit"},
        headers=tenant.admin)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "DEFINITION_CODE_CONFLICT"


async def test_ordinal_tier_is_not_authorable_yet(p3c_client, tenant):
    response = await p3c_client.post(
        "/compensation/pay-definitions",
        json={"definition_name": "Tiered", "input_type": "WholeNumber",
              "calculation_method": "OrdinalTier"},
        headers=tenant.admin)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "CALCULATION_METHOD_NOT_AVAILABLE"

    draft = (await _draft(p3c_client, tenant, calculation_method="OrdinalTier",
                          input_type="WholeNumber")).json()
    submit = await _submit(p3c_client, tenant, draft)
    assert submit.status_code == 422
    assert submit.json()["detail"]["code"] == "CALCULATION_METHOD_NOT_AVAILABLE"


@pytest.mark.parametrize("bad", [
    {"input_type": "Time"}, {"calculation_method": "Fixed"}, {"definition_name": "  "},
    {"calculation_method": "RangeBracket"},
])
async def test_invalid_definition_fields_are_rejected(p3c_client, tenant, bad):
    body = {"definition_name": "Item", "input_type": "Decimal",
            "calculation_method": "PerUnit"} | bad
    response = await p3c_client.post(
        "/compensation/pay-definitions", json=body, headers=tenant.admin)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Request lifecycle
# ---------------------------------------------------------------------------

async def test_request_approval_creates_the_definition_and_full_provenance(
    p3c_client, tenant,
):
    draft = (await _draft(
        p3c_client, tenant, definition_code="ITEM_ALPHA",
        definition_name="Item alpha", notes="why")).json()
    assert draft["status"] == "Draft" and draft["revision"] == 1
    submitted = (await _submit(p3c_client, tenant, draft)).json()
    assert submitted["status"] == "PendingCompanyApproval"
    assert submitted["submitted_by_user_id"] == tenant.branch_user

    approved = await _decide(p3c_client, tenant, submitted, "Approve")
    assert approved.status_code == 200, approved.text
    request = approved.json()
    assert request["status"] == "Approved"
    definition_id = request["approved_pay_definition_id"]
    assert definition_id is not None

    definition = (await p3c_client.get(
        f"/compensation/pay-definitions/{definition_id}", headers=tenant.admin)).json()
    assert definition["definition_code"] == "ITEM_ALPHA"
    assert definition["status"] == "Active"
    assert definition["structure_locked_at_utc"] is not None
    provenance = definition["provenance"]
    assert provenance["creation_mode"] == "Request"
    assert provenance["source_request_id"] == request["request_id"]
    assert provenance["requesting_branch_id"] == tenant.branch_a
    assert provenance["created_by_user_id"] == tenant.branch_user
    assert provenance["submitted_by_user_id"] == tenant.branch_user
    assert provenance["submitted_at_utc"] is not None
    assert provenance["approved_by_user_id"] == tenant.owner
    assert provenance["approved_at_utc"] is not None

    events = (await p3c_client.get(
        f"/compensation/pay-definition-requests/{request['request_id']}/events",
        headers=tenant.admin)).json()
    assert [e["event_type"] for e in events] == ["DraftCreated", "Submitted", "Approved"]
    assert [e["actor_user_id"] for e in events] == [
        tenant.branch_user, tenant.branch_user, tenant.owner]


async def test_return_resubmit_reject_and_copy_lineage(p3c_client, tenant):
    submitted = await _submitted(p3c_client, tenant)
    returned = (await _decide(p3c_client, tenant, submitted, "ReturnToDraft")).json()
    assert returned["status"] == "Draft"
    resubmitted = (await _submit(p3c_client, tenant, returned)).json()
    rejected = await _decide(p3c_client, tenant, resubmitted, "Reject", reason="no")
    assert rejected.status_code == 200
    rejected = rejected.json()
    assert rejected["status"] == "Rejected"

    events = (await p3c_client.get(
        f"/compensation/pay-definition-requests/{rejected['request_id']}/events",
        headers=tenant.admin)).json()
    assert [e["event_type"] for e in events] == [
        "DraftCreated", "Submitted", "ReturnedToDraft", "Resubmitted", "Rejected"]

    copy = await p3c_client.post(
        f"/compensation/pay-definition-requests/{rejected['request_id']}/copy",
        headers=tenant.headers(tenant.branch_user))
    assert copy.status_code == 201, copy.text
    copied = copy.json()
    assert copied["status"] == "Draft" and copied["revision"] == 1
    assert copied["copied_from_request_id"] == rejected["request_id"]
    assert copied["request_id"] != rejected["request_id"]
    copied_events = (await p3c_client.get(
        f"/compensation/pay-definition-requests/{copied['request_id']}/events",
        headers=tenant.admin)).json()
    assert [e["event_type"] for e in copied_events] == ["CopiedFromRejected"]


async def test_only_rejected_requests_can_be_copied(p3c_client, tenant):
    draft = (await _draft(p3c_client, tenant)).json()
    response = await p3c_client.post(
        f"/compensation/pay-definition-requests/{draft['request_id']}/copy",
        headers=tenant.headers(tenant.branch_user))
    assert response.status_code == 422


async def test_draft_update_uses_optimistic_revisions(p3c_client, tenant):
    draft = (await _draft(p3c_client, tenant)).json()
    headers = tenant.headers(tenant.branch_user)
    url = f"/compensation/pay-definition-requests/{draft['request_id']}"
    first = await p3c_client.patch(
        url, json={"expected_revision": 1, "definition_name": "Renamed", "unit": None},
        headers=headers)
    assert first.status_code == 200
    assert first.json()["revision"] == 2
    assert first.json()["definition_name"] == "Renamed"
    assert first.json()["unit"] is None

    stale = await p3c_client.patch(
        url, json={"expected_revision": 1, "definition_name": "Late"}, headers=headers)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "REQUEST_REVISION_CONFLICT"

    submitted = await _submit(p3c_client, tenant, first.json())
    assert submitted.status_code == 200
    locked = await p3c_client.patch(
        url, json={"expected_revision": 3, "definition_name": "No"}, headers=headers)
    assert locked.status_code == 422


async def test_incomplete_draft_cannot_be_submitted(p3c_client, tenant):
    created = await p3c_client.post(
        "/compensation/pay-definition-requests",
        json={"requesting_branch_id": tenant.branch_a},
        headers=tenant.headers(tenant.branch_user))
    assert created.status_code == 201
    submit = await _submit(p3c_client, tenant, created.json())
    assert submit.status_code == 422
    assert submit.json()["detail"]["code"] == "REQUEST_INCOMPLETE"


async def test_decision_requires_pending_status_and_current_revision(p3c_client, tenant):
    submitted = await _submitted(p3c_client, tenant)
    stale = dict(submitted, revision=submitted["revision"] - 1)
    conflict = await _decide(p3c_client, tenant, stale, "Approve")
    assert conflict.status_code == 409
    approved = await _decide(p3c_client, tenant, submitted, "Approve")
    assert approved.status_code == 200
    again = await _decide(p3c_client, tenant, approved.json(), "Approve")
    assert again.status_code == 422


async def test_a_request_approves_into_exactly_one_definition(p3c_client, tenant, cur):
    submitted = await _submitted(p3c_client, tenant)
    await _decide(p3c_client, tenant, submitted, "Approve")
    await _decide(p3c_client, tenant, submitted, "Approve")
    cur.execute(
        "SELECT count(*) FROM payroll.paydefinitionprovenance WHERE sourcerequestid = %s",
        (submitted["request_id"],))
    assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

async def test_requests_and_definitions_are_isolated_between_companies(
    p3c_client, tenant, p3b_dsn,
):
    other = build_tenant(p3b_dsn)
    request = (await _draft(p3c_client, tenant)).json()
    definition = await create_definition(p3c_client, tenant)

    for method, url in (
        ("get", f"/compensation/pay-definition-requests/{request['request_id']}"),
        ("get", f"/compensation/pay-definition-requests/{request['request_id']}/events"),
        ("get", f"/compensation/pay-definitions/{definition['pay_definition_id']}"),
    ):
        response = await getattr(p3c_client, method)(url, headers=other.admin)
        assert response.status_code == 404, (url, response.text)
    assert (await p3c_client.get(
        "/compensation/pay-definitions", headers=other.admin)).json() == []
    assert (await p3c_client.get(
        "/compensation/pay-definition-requests", headers=other.admin)).json() == []
    foreign_decide = await _decide(p3c_client, tenant, request, "Reject", headers=other.admin)
    assert foreign_decide.status_code == 404


async def test_branch_scoped_users_act_only_on_their_branch(p3c_client, tenant):
    branch_user = tenant.headers(tenant.branch_user)
    own = await _draft(p3c_client, tenant, headers=branch_user)
    assert own.status_code == 201
    foreign = await _draft(p3c_client, tenant, branch_id=tenant.branch_b, headers=branch_user)
    assert foreign.status_code == 403

    other_branch_request = (await _draft(
        p3c_client, tenant, branch_id=tenant.branch_b, headers=tenant.admin)).json()
    listed = (await p3c_client.get(
        "/compensation/pay-definition-requests", headers=branch_user)).json()
    assert {r["requesting_branch_id"] for r in listed} == {tenant.branch_a}
    read = await p3c_client.get(
        f"/compensation/pay-definition-requests/{other_branch_request['request_id']}",
        headers=branch_user)
    assert read.status_code == 403
    assert (await p3c_client.patch(
        f"/compensation/pay-definition-requests/{other_branch_request['request_id']}",
        json={"expected_revision": 1, "definition_name": "x"}, headers=branch_user,
    )).status_code == 403


async def test_company_actions_require_company_wide_authority(p3c_client, tenant):
    branch_user = tenant.headers(tenant.branch_user)
    submitted = await _submitted(p3c_client, tenant)
    assert (await _decide(
        p3c_client, tenant, submitted, "Approve", headers=branch_user)).status_code == 403
    direct = await p3c_client.post(
        "/compensation/pay-definitions",
        json={"definition_name": "x", "input_type": "Decimal", "calculation_method": "PerUnit"},
        headers=branch_user)
    assert direct.status_code == 403


async def test_read_only_and_unpermissioned_users_cannot_author(p3c_client, tenant):
    for user in (tenant.viewer, tenant.no_permissions):
        headers = tenant.headers(user)
        assert (await _draft(p3c_client, tenant, headers=headers)).status_code == 403
        assert (await p3c_client.post(
            "/compensation/pay-definitions",
            json={"definition_name": "x", "input_type": "Decimal",
                  "calculation_method": "PerUnit"}, headers=headers)).status_code == 403
    assert (await p3c_client.get(
        "/compensation/pay-definitions", headers=tenant.headers(tenant.viewer))).status_code == 200
    assert (await p3c_client.get(
        "/compensation/pay-definitions",
        headers=tenant.headers(tenant.no_permissions))).status_code == 403


async def test_unauthenticated_requests_are_rejected(p3c_client):
    assert (await p3c_client.get("/compensation/pay-definitions")).status_code in (401, 403)


# ---------------------------------------------------------------------------
# Database-level provenance and history integrity
# ---------------------------------------------------------------------------

async def test_request_events_are_append_only(p3c_client, tenant, cur):
    draft = (await _draft(p3c_client, tenant)).json()
    with pytest.raises(errors.CheckViolation, match="PAY_DEFINITION_HISTORY_APPEND_ONLY"):
        cur.execute(
            "UPDATE payroll.paydefinitionrequestevents SET reason = 'x' "
            "WHERE paydefinitionrequestid = %s", (draft["request_id"],))
    with pytest.raises(errors.CheckViolation, match="PAY_DEFINITION_HISTORY_APPEND_ONLY"):
        cur.execute(
            "DELETE FROM payroll.paydefinitionrequestevents "
            "WHERE paydefinitionrequestid = %s", (draft["request_id"],))
    # The driver reports an ON DELETE RESTRICT failure as either a restrict or a
    # foreign-key violation depending on version; the constraint name is the invariant.
    with pytest.raises(psycopg2.IntegrityError) as blocked:
        cur.execute(
            "DELETE FROM payroll.paydefinitionrequests WHERE paydefinitionrequestid = %s",
            (draft["request_id"],))
    assert blocked.value.diag.constraint_name == "fk_paydefinitionrequestevents_request"


async def test_provenance_is_immutable(p3c_client, tenant, cur):
    definition = await create_definition(p3c_client, tenant)
    with pytest.raises(errors.CheckViolation, match="PAY_DEFINITION_HISTORY_APPEND_ONLY"):
        cur.execute(
            "UPDATE payroll.paydefinitionprovenance SET governanceschemaversion = 2 "
            "WHERE paydefinitionid = %s", (definition["pay_definition_id"],))
    with pytest.raises(errors.CheckViolation, match="PAY_DEFINITION_HISTORY_APPEND_ONLY"):
        cur.execute(
            "DELETE FROM payroll.paydefinitionprovenance WHERE paydefinitionid = %s",
            (definition["pay_definition_id"],))


async def test_terminal_requests_and_request_identity_are_immutable(
    p3c_client, tenant, cur,
):
    submitted = await _submitted(p3c_client, tenant)
    rejected = (await _decide(p3c_client, tenant, submitted, "Reject")).json()
    with pytest.raises(errors.CheckViolation, match="PAY_DEFINITION_REQUEST_TERMINAL"):
        cur.execute(
            "UPDATE payroll.paydefinitionrequests SET notes = 'x' "
            "WHERE paydefinitionrequestid = %s", (rejected["request_id"],))
    draft = (await _draft(p3c_client, tenant)).json()
    with pytest.raises(errors.CheckViolation, match="PAY_DEFINITION_REQUEST_IDENTITY_IMMUTABLE"):
        cur.execute(
            "UPDATE payroll.paydefinitionrequests SET requestingbranchid = %s "
            "WHERE paydefinitionrequestid = %s", (tenant.branch_b, draft["request_id"]))


async def test_request_branch_and_company_ownership_is_enforced(
    p3c_client, tenant, p3b_dsn, cur,
):
    other = build_tenant(p3b_dsn)
    with pytest.raises(errors.ForeignKeyViolation):
        cur.execute("""
            INSERT INTO payroll.paydefinitionrequests
                (companyid, requestingbranchid, createdbyuserid)
            VALUES (%s, %s, %s)
        """, (tenant.company_id, other.branch_a, tenant.owner))


def _orphan_definition(cur, tenant) -> int:
    cur.execute("""
        INSERT INTO payroll.paydefinitions
            (companyid, definitioncode, definitionname, inputtype, calculationmethod)
        VALUES (%s, %s, 'unrecorded', 'Decimal', 'PerUnit') RETURNING paydefinitionid
    """, (tenant.company_id, "O" + uuid4().hex[:10]))
    return cur.fetchone()[0]


async def test_provenance_requires_a_matching_approved_request(p3c_client, tenant, p3b_dsn):
    draft = (await _draft(p3c_client, tenant)).json()
    conn = psycopg2.connect(client_encoding="utf-8", **p3b_dsn)
    try:
        with conn.cursor() as cursor:
            orphan = _orphan_definition(cursor, tenant)
            cursor.execute("""
                INSERT INTO payroll.paydefinitionprovenance
                    (paydefinitionid, companyid, creationmode, sourcerequestid,
                     requestingbranchid, createdbyuserid, submittedbyuserid, submittedatutc,
                     approvedbyuserid, approvedatutc)
                VALUES (%s, %s, 'Request', %s, %s, %s, %s, now(), %s, now())
            """, (orphan, tenant.company_id, draft["request_id"], tenant.branch_a,
                  tenant.owner, tenant.owner, tenant.owner))
            with pytest.raises(errors.CheckViolation, match="PROVENANCE_REQUEST_MISMATCH"):
                conn.commit()
    finally:
        conn.rollback()
        conn.close()


async def test_provenance_mode_fields_are_consistent(tenant, cur):
    orphan = _orphan_definition(cur, tenant)
    with pytest.raises(errors.CheckViolation):
        cur.execute("""
            INSERT INTO payroll.paydefinitionprovenance
                (paydefinitionid, companyid, creationmode, createdbyuserid, approvedbyuserid)
            VALUES (%s, %s, 'DirectCreate', %s, %s)
        """, (orphan, tenant.company_id, tenant.owner, tenant.owner))
    with pytest.raises(errors.CheckViolation):
        cur.execute("""
            INSERT INTO payroll.paydefinitionprovenance
                (paydefinitionid, companyid, creationmode, createdbyuserid)
            VALUES (%s, %s, 'Request', %s)
        """, (orphan, tenant.company_id, tenant.owner))
