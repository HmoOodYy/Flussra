"""A Pending Driver rate schedule is approved by someone else when the Company disallows
self-approval; with the policy on (the default) nothing changes."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.builders.access import create_company_role_with_permissions
from tests.builders.transfer_scenarios import sfx
from tests.ownership import preserved_company_profile
from tests.test_driver_owned_rate_schedules import (
    LONG_AGO,
    _approve,
    _code,
    _create,
    _world,
)
from tests.test_ended_assignment_write_guard import _definition, _expect, _manager, _values

pytestmark = pytest.mark.asyncio

SCHEDULES = "/compensation/driver-rate-schedules"


async def _set_policy(db, allow: bool) -> None:
    await db.execute(
        text("UPDATE core.companies SET allowselfapproval = :a WHERE companycode = 'DEMO'"),
        {"a": allow})


async def _pending(client, token, w, definition) -> int:
    created = await _expect(await _create(client, token, w, definition, LONG_AGO), 201)
    schedule_id = created.json()["driver_rate_schedule_id"]
    await _expect(await _values(client, token, schedule_id, definition), 200)
    return schedule_id


async def _second_manager(client, admin, db, branch_id) -> str:
    role_id = await create_company_role_with_permissions(
        client, admin, f"SA manager {sfx()}",
        ["drivers.view", "payrates.view", "payrates.edit", "payitems.view"])
    return await _manager(client, admin, role_id, branch_id, db=db)


async def _status(db, schedule_id: int) -> str:
    return (await db.execute(text(
        "SELECT status FROM payroll.driverrateschedules WHERE driverratescheduleid = :s"),
        {"s": schedule_id})).scalar_one()


async def test_creator_cannot_approve_when_self_approval_is_disallowed(
    client, auth_token, direct_db,
):
    async with preserved_company_profile(direct_db):
        await _set_policy(direct_db, False)
        w = await _world(client, auth_token, direct_db)
        other = await _second_manager(client, auth_token, direct_db, w.old_branch)
        definition = await _definition(client, auth_token)
        schedule_id = await _pending(client, w.old_manager, w, definition)

        refused = await _approve(client, w.old_manager, schedule_id)

        assert refused.status_code == 403, refused.text
        assert _code(refused) == "RATE_SCHEDULE_SELF_APPROVAL_DISALLOWED"
        assert "another user" in refused.json()["detail"]["message"].lower()
        assert await _status(direct_db, schedule_id) == "Pending"

        approved = await _approve(client, other, schedule_id)
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "Approved"


async def test_last_editor_and_creator_are_both_refused_but_a_third_user_can_approve(
    client, auth_token, direct_db,
):
    async with preserved_company_profile(direct_db):
        await _set_policy(direct_db, False)
        w = await _world(client, auth_token, direct_db)
        other = await _second_manager(client, auth_token, direct_db, w.old_branch)
        third = await _second_manager(client, auth_token, direct_db, w.old_branch)
        definition = await _definition(client, auth_token)
        schedule_id = await _pending(client, w.old_manager, w, definition)
        await _expect(await _values(client, other, schedule_id, definition, "40"), 200)

        # The creator and the person who last changed the values are both refused.
        for token in (w.old_manager, other):
            refused = await _approve(client, token, schedule_id)
            assert refused.status_code == 403, refused.text
            assert _code(refused) == "RATE_SCHEDULE_SELF_APPROVAL_DISALLOWED"
        assert (await _approve(client, third, schedule_id)).status_code == 200


async def test_creator_can_approve_when_self_approval_is_allowed(client, auth_token, direct_db):
    async with preserved_company_profile(direct_db):
        await _set_policy(direct_db, True)
        w = await _world(client, auth_token, direct_db)
        definition = await _definition(client, auth_token)
        schedule_id = await _pending(client, w.old_manager, w, definition)

        approved = await _approve(client, w.old_manager, schedule_id)

        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "Approved"


async def test_self_approval_refusal_leaves_other_schedule_checks_unchanged(
    client, auth_token, direct_db,
):
    """The policy gate does not mask an incomplete schedule for a different approver."""
    async with preserved_company_profile(direct_db):
        await _set_policy(direct_db, False)
        w = await _world(client, auth_token, direct_db)
        other = await _second_manager(client, auth_token, direct_db, w.old_branch)
        definition = await _definition(client, auth_token)
        created = await _expect(await _create(client, w.old_manager, w, definition, LONG_AGO), 201)
        schedule_id = created.json()["driver_rate_schedule_id"]

        incomplete = await _approve(client, other, schedule_id)

        assert incomplete.status_code == 422, incomplete.text
        assert _code(incomplete) == "SCHEDULE_INCOMPLETE"
