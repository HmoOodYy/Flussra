"""GET /core/drivers/{driver_id}/assignments: a Driver's tenures, never widened beyond scope."""

from datetime import timedelta

import pytest

from tests.builders.access import create_user_with_role_token
from tests.builders.transfer_scenarios import (
    auth,
    branch_today,
    local_boundary,
    make_branch,
    make_driver,
    scheduled_transfer,
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


async def test_an_admin_sees_every_assignment_with_branch_window_state_and_reason(
    client, auth_token, direct_db,
):
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    effective_at = await local_boundary(direct_db, source_branch, today + timedelta(days=4))
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch, effective_at)

    response = await client.get(
        f"/core/drivers/{driver['driver_id']}/assignments", headers=auth(auth_token))
    assert response.status_code == 200, response.text
    rows = {row["driver_assignment_id"]: row for row in response.json()}
    assert set(rows) == {transfer["source_id"], transfer["destination_id"]}
    assert rows[transfer["source_id"]]["state"] == "current"
    assert rows[transfer["source_id"]]["branch_id"] == source_branch
    assert rows[transfer["source_id"]]["end_reason"] == "Transferred"
    assert rows[transfer["destination_id"]]["state"] == "pending"
    assert rows[transfer["destination_id"]]["branch_id"] == target_branch
    assert rows[transfer["destination_id"]]["effective_to_at"] is None
    assert all(row["driver_id"] == driver["driver_id"] and row["branch_name"] for row in rows.values())
    # Newest first.
    assert [row["driver_assignment_id"] for row in response.json()][0] == transfer["destination_id"]


async def test_a_branch_scoped_user_sees_only_assignments_in_branches_it_may_read(
    client, auth_token, direct_db,
):
    first, other_branch = await make_branch(direct_db), await make_branch(direct_db)
    token = await _drivers_view_token(client, auth_token, first)
    driver = await make_driver(client, auth_token, first)
    today = await branch_today(direct_db, first)
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], other_branch,
        await local_boundary(direct_db, first, today + timedelta(days=4)))

    seen = await client.get(
        f"/core/drivers/{driver['driver_id']}/assignments", headers=auth(token))
    assert seen.status_code == 200, seen.text
    assert [row["driver_assignment_id"] for row in seen.json()] == [transfer["source_id"]]


async def test_a_user_with_no_readable_branch_is_denied_and_unknown_drivers_are_404(
    client, auth_token, branch_user_token, direct_db,
):
    first, second = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, first)
    today = await branch_today(direct_db, first)
    await scheduled_transfer(
        client, auth_token, driver["driver_id"], second,
        await local_boundary(direct_db, first, today + timedelta(days=4)))

    denied = await client.get(
        f"/core/drivers/{driver['driver_id']}/assignments", headers=auth(branch_user_token))
    assert denied.status_code == 403, denied.text
    missing = await client.get("/core/drivers/2000000000/assignments", headers=auth(auth_token))
    assert missing.status_code == 404
    unauthenticated = await client.get(f"/core/drivers/{driver['driver_id']}/assignments")
    assert unauthenticated.status_code in (401, 403)
