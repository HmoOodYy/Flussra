"""GET /core/driver-assignments: branch-scoped DriverAssignment roster."""

import pytest

from tests.builders.access import create_user_with_role_token, get_company_role_id
from tests.builders.transfer_scenarios import (
    approved_transfer,
    auth,
    make_branch,
    make_driver,
    sfx,
)

pytestmark = pytest.mark.asyncio


async def _drivers_view_token(client, admin_token, branch_id) -> str:
    """A user with drivers.view on one Branch only (SpecificBranch scope)."""
    role = await client.post("/admin/company-roles", json={
        "role_code": f"DV_{sfx()}", "role_name": "Drivers view only", "role_level": 20,
        "is_default": False, "is_protected": False,
    }, headers=auth(admin_token))
    assert role.status_code in (200, 201), role.text
    role_id = role.json()["company_role_id"]
    granted = await client.put(
        f"/admin/company-roles/{role_id}/permissions",
        json={"permission_codes": ["drivers.view"]}, headers=auth(admin_token))
    assert granted.status_code in (200, 201, 204), granted.text
    return await create_user_with_role_token(
        client, admin_token, f"dv_{sfx()}", role_id,
        scope_type="SpecificBranch", branch_id=branch_id,
    )


async def _immediate_transfer(client, token, driver_id, target_branch_id) -> dict:
    request_id = await approved_transfer(client, token, driver_id, target_branch_id, None)
    completed = await client.post(
        f"/driver-transfers/{request_id}/complete", json={}, headers=auth(token))
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["status"] == "Completed", body
    return {"source_id": body["source_driver_assignment_id"],
            "destination_id": body["destination_driver_assignment_id"]}


async def test_the_assignment_roster_follows_branch_scope_after_a_permanent_transfer(
    client, auth_token, direct_db,
):
    """HEAD had no such read: an old-Branch manager could never reach the ended assignment."""
    old_branch, new_branch = await make_branch(direct_db), await make_branch(direct_db)
    old_manager = await _drivers_view_token(client, auth_token, old_branch)
    new_manager = await _drivers_view_token(client, auth_token, new_branch)
    driver = await make_driver(client, auth_token, old_branch)
    moved = await _immediate_transfer(client, auth_token, driver["driver_id"], new_branch)

    async def roster(token, **params):
        response = await client.get("/core/driver-assignments", params=params, headers=auth(token))
        assert response.status_code == 200, response.text
        return {row["driver_assignment_id"]: row for row in response.json()
                if row["driver_id"] == driver["driver_id"]}

    old_view = await roster(old_manager)
    assert set(old_view) == {moved["source_id"]}
    ended = old_view[moved["source_id"]]
    assert ended["state"] == "ended" and ended["end_reason"] == "Transferred"
    assert ended["branch_id"] == old_branch and ended["branch_name"]
    assert ended["driver_number"].startswith("SC-") and ended["full_name"]
    assert ended["driver_status"] == "Active"

    new_view = await roster(new_manager)
    assert set(new_view) == {moved["destination_id"]}
    assert new_view[moved["destination_id"]]["state"] == "current"

    admin_view = await roster(auth_token)
    assert set(admin_view) == {moved["source_id"], moved["destination_id"]}

    # Filters: by Branch and by state; an unknown state is a validation error.
    assert set(await roster(auth_token, branch_id=old_branch)) == {moved["source_id"]}
    assert set(await roster(auth_token, state="current")) == {moved["destination_id"]}
    assert set(await roster(auth_token, state="ended")) == {moved["source_id"]}
    assert set(await roster(old_manager, state="current")) == set()
    invalid = await client.get("/core/driver-assignments", params={"state": "bogus"},
                               headers=auth(auth_token))
    assert invalid.status_code == 422


async def test_the_assignment_roster_hides_branches_without_drivers_view_and_follows_self_reads(
    client, auth_token, branch_user_token, direct_db,
):
    branch = await make_branch(direct_db)
    await make_driver(client, auth_token, branch)

    # PAYROLL_VIEWER scoped to HQ has no drivers.view anywhere: the target roster denies it.
    roster = await client.get("/core/driver-assignments", headers=auth(branch_user_token))
    assert roster.status_code == 403, roster.text

    # A Self (DRIVER) account cannot use the general Driver roster.
    role_id = await get_company_role_id(client, auth_token, "DRIVER")
    self_token = await create_user_with_role_token(
        client, auth_token, f"self_{sfx()}", role_id, scope_type="Self", driver_branch_id=branch)
    self_roster = await client.get("/core/driver-assignments", headers=auth(self_token))
    assert self_roster.status_code == 403, self_roster.text

    unauthenticated = await client.get("/core/driver-assignments")
    assert unauthenticated.status_code in (401, 403)
