"""Context authority and PayDefinition status govern Driver rate schedule authoring.

Branch applicability is not an authoring rule (D5): it is decided at calculation.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.compensation import definitions, schedules
from app.compensation.schemas import ScheduleCreate
from tests.compensation_api_fixtures import (  # noqa: F401 - register fixtures
    approve_schedule,
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
from tests.test_p3c_compensation_concurrency import _blocked_with_pid, _status

pytestmark = pytest.mark.asyncio

BASE = "/compensation/driver-rate-schedules"


def _body(tenant, definition, effective_from="2026-01-01", driver_id=None):
    return {"driver_id": driver_id or tenant.driver_a_id,
            "rate_definition_id": definition["rate_definition_id"],
            "effective_from": effective_from}


def _id(schedule) -> int:
    return schedule["driver_rate_schedule_id"]


async def _post(client, tenant, definition, **kwargs):
    return await client.post(BASE, json=_body(tenant, definition, **kwargs), headers=tenant.admin)


async def _filled(client, tenant, definition, **kwargs):
    pending = await create_pending(client, tenant, definition, **kwargs)
    assert (await set_scalar_value(client, tenant, pending, definition, "10")).status_code == 200
    return pending


def _code(response) -> str:
    return response.json()["detail"]["code"]


# ---------------------------------------------------------------------------
# Creation: any company-Active scalar definition is authorable (D5)
# ---------------------------------------------------------------------------

async def test_branch_applicability_is_not_checked_at_authoring(p3c_client, tenant):
    """Missing, inactive and other-Branch configuration all permit creation: whether a
    definition applies is decided at calculation, where a missing value still blocks."""
    missing = await create_definition(p3c_client, tenant)
    assert (await _post(p3c_client, tenant, missing)).status_code == 201

    inactive = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, inactive, tenant.branch_a, active=False)
    assert (await _post(p3c_client, tenant, inactive)).status_code == 201

    elsewhere = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, elsewhere, tenant.branch_a)
    assert (await _post(p3c_client, tenant, elsewhere, driver_id=tenant.driver_b_id)).status_code == 201


async def test_future_activation_does_not_restrict_earlier_dates(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a, effective_from="2030-01-01")
    assert (await _post(
        p3c_client, tenant, definition, effective_from="2029-12-31")).status_code == 201


# ---------------------------------------------------------------------------
# Authority: the context tenure's Branch, or company-wide
# ---------------------------------------------------------------------------

async def test_branch_scoped_authority_follows_the_drivers_context_branch(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    branch_user = tenant.headers(tenant.branch_user)  # SpecificBranch on branch_a
    own = await p3c_client.post(
        BASE, json=_body(tenant, definition), headers=branch_user)
    assert own.status_code == 201
    other = await p3c_client.post(
        BASE, json=_body(tenant, definition, driver_id=tenant.driver_b_id), headers=branch_user)
    assert other.status_code == 403


async def test_a_start_date_without_any_tenure_is_refused_for_everyone(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    before_tenure = "1999-12-31"  # the fixture assignments start 2000-01-01
    admin = await _post(p3c_client, tenant, definition, effective_from=before_tenure)
    assert admin.status_code == 409
    assert _code(admin) == "DRIVER_COMPENSATION_HISTORICAL"
    branch_user = await p3c_client.post(
        BASE, json=_body(tenant, definition, effective_from=before_tenure),
        headers=tenant.headers(tenant.branch_user))
    assert branch_user.status_code == 403


# ---------------------------------------------------------------------------
# Pending edit
# ---------------------------------------------------------------------------

async def test_editing_effective_from_does_not_revalidate_applicability(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a, effective_from="2025-01-01")
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2026-01-01")
    moved = await p3c_client.patch(
        f"{BASE}/{_id(pending)}", json={"effective_from": "2024-12-31"}, headers=tenant.admin)
    assert moved.status_code == 200
    assert moved.json()["effective_from"] == "2024-12-31"


async def test_editing_effective_from_into_no_tenure_is_refused(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition, effective_from="2026-01-01")
    refused = await p3c_client.patch(
        f"{BASE}/{_id(pending)}", json={"effective_from": "1999-12-31"}, headers=tenant.admin)
    assert refused.status_code == 409
    assert _code(refused) == "DRIVER_COMPENSATION_HISTORICAL"


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

async def test_approval_after_the_branch_deactivates_the_definition_still_succeeds(
    p3c_client, tenant, p3c_engine,
):
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled(p3c_client, tenant, definition)
    grant_applicability(tenant, definition, tenant.branch_a, active=False)

    response = await approve_schedule(p3c_client, tenant, pending)
    assert response.status_code == 200, response.text
    assert await _status(p3c_engine, pending) == "Approved"


async def test_approval_after_retirement_is_blocked_but_discard_is_not(
    p3c_client, tenant, p3c_engine,
):
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled(p3c_client, tenant, definition)
    retired = await p3c_client.post(
        f"/compensation/pay-definitions/{definition['pay_definition_id']}/retire",
        headers=tenant.admin)
    assert retired.status_code == 200

    response = await approve_schedule(p3c_client, tenant, pending)
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


async def _schedule_count(engine, definition) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(
            text("SELECT count(*) FROM payroll.driverrateschedules WHERE ratedefinitionid = :r"),
            {"r": definition["rate_definition_id"]})).scalar_one()


async def test_creation_waits_for_an_in_flight_retirement_and_then_fails(
    p3c_client, p3c_engine, tenant,
):
    """Retirement holds the RateDefinition lock: authoring queues behind it, and once
    Retired commits no schedule is created."""
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)

    async def create(conn):
        return await schedules.create_pending(
            tenant.company_id, tenant.owner,
            ScheduleCreate(driver_id=tenant.driver_a_id,
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
    assert await _schedule_count(p3c_engine, definition) == 0


async def test_retirement_waits_for_in_flight_authoring_and_takes_the_definition_second(
    p3c_client, p3c_engine, tenant,
):
    """Authoring holds the RateDefinition lock: retirement queues behind it without
    having locked the PayDefinition, then retires after the schedule committed."""
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)

    async def retire(conn):
        return await definitions.retire_definition(
            tenant.company_id, tenant.owner, definition["pay_definition_id"], conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            created = await schedules.create_pending(
                tenant.company_id, tenant.owner,
                ScheduleCreate(driver_id=tenant.driver_a_id,
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
        return await schedules.approve(tenant.company_id, tenant.owner, _id(pending), conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await definitions.retire_definition(
                tenant.company_id, tenant.owner, definition["pay_definition_id"], holder)
            task, _ = await _blocked_with_pid(p3c_engine, approve)
    result = await task
    assert getattr(result, "detail", {}).get("code") == "PAY_DEFINITION_NOT_ACTIVE", result
    assert await _status(p3c_engine, pending) == "Pending"
