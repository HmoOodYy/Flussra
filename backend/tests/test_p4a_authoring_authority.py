"""Branch applicability and PayDefinition status govern target rate authoring."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.compensation import assignments, definitions
from app.compensation.schemas import AssignmentCreate
from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    approve_assignment,
    create_definition,
    create_pending,
    grant_applicability,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
    set_scalar_value,
)
from tests.test_p3c_compensation_concurrency import _blocked_with_pid, _status

pytestmark = pytest.mark.asyncio

BASE = "/compensation/driver-rate-assignments"


def _body(tenant, definition, effective_from="2026-01-01", driver_id=None):
    return {"driver_id": driver_id or tenant.driver_a,
            "rate_definition_id": definition["rate_definition_id"],
            "effective_from": effective_from}


def _id(assignment) -> int:
    return assignment["driver_rate_assignment_id"]


async def _post(client, tenant, definition, **kwargs):
    return await client.post(BASE, json=_body(tenant, definition, **kwargs), headers=tenant.admin)


async def _filled(client, tenant, definition, **kwargs):
    pending = await create_pending(client, tenant, definition, **kwargs)
    assert (await set_scalar_value(client, tenant, pending, definition, "10")).status_code == 200
    return pending


def _code(response) -> str:
    return response.json()["detail"]["code"]


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

async def test_missing_branch_configuration_blocks_creation(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    response = await _post(p3c_client, tenant, definition)
    assert response.status_code == 422
    assert _code(response) == "PAY_DEFINITION_NOT_APPLICABLE"


async def test_inactive_branch_configuration_blocks_creation(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a, active=False)
    response = await _post(p3c_client, tenant, definition)
    assert response.status_code == 422
    assert _code(response) == "PAY_DEFINITION_NOT_APPLICABLE"


async def test_active_branch_configuration_permits_creation(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)
    assert (await _post(p3c_client, tenant, definition)).status_code == 201


async def test_applicability_is_per_driver_branch(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)
    response = await _post(p3c_client, tenant, definition, driver_id=tenant.driver_b)
    assert response.status_code == 422
    assert _code(response) == "PAY_DEFINITION_NOT_APPLICABLE"


async def test_future_activation_blocks_earlier_dates_but_not_its_own_start(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a, effective_from="2030-01-01")
    before = await _post(p3c_client, tenant, definition, effective_from="2029-12-31")
    assert before.status_code == 422
    assert _code(before) == "PAY_DEFINITION_NOT_APPLICABLE"
    assert (await _post(
        p3c_client, tenant, definition, effective_from="2030-01-01")).status_code == 201


async def test_applicability_ending_later_does_not_block_a_window_that_starts_inside_it(
    p3c_client, tenant, p3b_dsn,
):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a, effective_from="2000-01-01")
    import psycopg2
    conn = psycopg2.connect(client_encoding="utf-8", **p3b_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE payroll.branchpayitemconfig SET effectiveto = '2026-06-30'
            WHERE companyid = %s AND paydefinitionid = %s
        """, (tenant.company_id, definition["pay_definition_id"]))
    conn.close()
    body = _body(tenant, definition, "2026-01-01") | {"effective_to": "2027-12-31"}
    assert (await p3c_client.post(BASE, json=body, headers=tenant.admin)).status_code == 201


# ---------------------------------------------------------------------------
# Pending edit
# ---------------------------------------------------------------------------

async def test_editing_effective_from_into_a_non_applicable_date_is_blocked(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a, effective_from="2025-01-01")
    pending = await create_pending(
        p3c_client, tenant, definition, effective_from="2026-01-01", applicable=False)

    blocked = await p3c_client.patch(
        f"{BASE}/{_id(pending)}", json={"effective_from": "2024-12-31"}, headers=tenant.admin)
    assert blocked.status_code == 422
    assert _code(blocked) == "PAY_DEFINITION_NOT_APPLICABLE"

    moved = await p3c_client.patch(
        f"{BASE}/{_id(pending)}", json={"effective_from": "2025-06-01"}, headers=tenant.admin)
    assert moved.status_code == 200
    assert moved.json()["effective_from"] == "2025-06-01"


async def test_editing_notes_does_not_revalidate_applicability(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)
    grant_applicability(tenant, definition, tenant.branch_a, active=False)
    noted = await p3c_client.patch(
        f"{BASE}/{_id(pending)}", json={"notes": "still editable"}, headers=tenant.admin)
    assert noted.status_code == 200


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

async def test_approval_after_the_branch_deactivates_the_definition_is_blocked(
    p3c_client, tenant, p3c_engine,
):
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled(p3c_client, tenant, definition)
    grant_applicability(tenant, definition, tenant.branch_a, active=False)

    response = await approve_assignment(p3c_client, tenant, pending)
    assert response.status_code == 422
    assert _code(response) == "PAY_DEFINITION_NOT_APPLICABLE"
    assert await _status(p3c_engine, pending) == "Pending"

    discarded = await p3c_client.delete(f"{BASE}/{_id(pending)}", headers=tenant.admin)
    assert discarded.status_code in (200, 204)


async def test_approval_after_retirement_is_blocked_but_discard_is_not(
    p3c_client, tenant, p3c_engine,
):
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled(p3c_client, tenant, definition)
    retired = await p3c_client.post(
        f"/compensation/pay-definitions/{definition['pay_definition_id']}/retire",
        headers=tenant.admin)
    assert retired.status_code == 200

    response = await approve_assignment(p3c_client, tenant, pending)
    assert response.status_code == 422
    assert _code(response) == "PAY_DEFINITION_NOT_ACTIVE"
    assert await _status(p3c_engine, pending) == "Pending"

    assert (await p3c_client.delete(
        f"{BASE}/{_id(pending)}", headers=tenant.admin)).status_code in (200, 204)


async def test_creation_after_retirement_is_blocked(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)
    await p3c_client.post(
        f"/compensation/pay-definitions/{definition['pay_definition_id']}/retire",
        headers=tenant.admin)
    response = await _post(p3c_client, tenant, definition)
    assert response.status_code == 422
    assert _code(response) == "PAY_DEFINITION_NOT_ACTIVE"


# ---------------------------------------------------------------------------
# Retirement versus authoring: real PostgreSQL synchronization
# ---------------------------------------------------------------------------

async def _definition_status(engine, definition) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(
            text("SELECT status FROM payroll.paydefinitions WHERE paydefinitionid = :pid"),
            {"pid": definition["pay_definition_id"]})).scalar_one()


async def _assignment_count(engine, definition) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(
            text("SELECT count(*) FROM payroll.driverrateassignments WHERE ratedefinitionid = :r"),
            {"r": definition["rate_definition_id"]})).scalar_one()


async def test_creation_waits_for_an_in_flight_retirement_and_then_fails(
    p3c_client, p3c_engine, tenant,
):
    """Retirement holds the RateDefinition lock: authoring queues behind it, and once
    Retired commits no assignment is created."""
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)

    async def create(conn):
        return await assignments.create_pending(
            tenant.company_id, tenant.owner,
            AssignmentCreate(driver_id=tenant.driver_a,
                             rate_definition_id=definition["rate_definition_id"],
                             effective_from="2026-01-01"), conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            await definitions.retire_definition(
                tenant.company_id, tenant.owner, definition["pay_definition_id"], holder)
            task, waiter_pid = await _blocked_with_pid(p3c_engine, create)
            async with p3c_engine.connect() as observer:
                blockers = (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})).scalar_one()
            assert blockers == [holder_pid]
    result = await task
    assert getattr(result, "detail", {}).get("code") == "PAY_DEFINITION_NOT_ACTIVE", result
    assert await _definition_status(p3c_engine, definition) == "Retired"
    assert await _assignment_count(p3c_engine, definition) == 0


async def test_retirement_waits_for_in_flight_authoring_and_takes_the_definition_second(
    p3c_client, p3c_engine, tenant,
):
    """Authoring holds the RateDefinition lock: retirement queues behind it without
    having locked the PayDefinition, then retires after the assignment committed."""
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)

    async def retire(conn):
        return await definitions.retire_definition(
            tenant.company_id, tenant.owner, definition["pay_definition_id"], conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            created = await assignments.create_pending(
                tenant.company_id, tenant.owner,
                AssignmentCreate(driver_id=tenant.driver_a,
                                 rate_definition_id=definition["rate_definition_id"],
                                 effective_from="2026-01-01"), holder)
            task, waiter_pid = await _blocked_with_pid(p3c_engine, retire)
            async with p3c_engine.connect() as observer:
                blockers = (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})).scalar_one()
            assert blockers == [holder_pid]
            # The waiter holds no PayDefinition lock yet: RateDefinition is taken first.
            async with p3c_engine.begin() as probe:
                await probe.execute(
                    text("SELECT 1 FROM payroll.paydefinitions "
                         "WHERE paydefinitionid = :pid FOR UPDATE NOWAIT"),
                    {"pid": definition["pay_definition_id"]})
    result = await task
    assert not isinstance(result, Exception), result
    assert await _definition_status(p3c_engine, definition) == "Retired"
    assert await _status(p3c_engine, created.model_dump()) == "Pending"


async def test_approval_waits_for_an_in_flight_retirement_and_then_fails(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled(p3c_client, tenant, definition)

    async def approve(conn):
        return await assignments.approve(tenant.company_id, tenant.owner, _id(pending), conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await definitions.retire_definition(
                tenant.company_id, tenant.owner, definition["pay_definition_id"], holder)
            task, _ = await _blocked_with_pid(p3c_engine, approve)
    result = await task
    assert getattr(result, "detail", {}).get("code") == "PAY_DEFINITION_NOT_ACTIVE", result
    assert await _status(p3c_engine, pending) == "Pending"
