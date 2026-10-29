"""Who may change whose access: not your own, not more than you hold, not a role you hold.

The Company Owner holds every permission, so the ceilings never bind them, but the Owner
still cannot change their own role or extra permissions.
"""

from uuid import uuid4

import httpx
import pytest

from tests.builders.access import (
    create_actor_with_permissions,
    create_company_role_with_permissions,
    create_neutral_test_user,
)

pytestmark = pytest.mark.asyncio

# Enough to call the assignment, extra-permission and role-permission endpoints.
MANAGER_CODES = [
    "users.view", "users.edit", "roles.view", "roles.edit",
    "payroll.view", "reports.view",
]


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def tag() -> str:
    return uuid4().hex[:8]


async def manager(client: httpx.AsyncClient, owner_token: str, codes=None) -> dict:
    return await create_actor_with_permissions(
        client, owner_token, f"mgr_{tag()}", MANAGER_CODES if codes is None else codes,
    )


async def target_user(client: httpx.AsyncClient, owner_token: str) -> dict:
    return await create_neutral_test_user(client, owner_token, f"tgt_{tag()}")


async def role_with(client: httpx.AsyncClient, owner_token: str, codes: list[str]) -> int:
    return await create_company_role_with_permissions(
        client, owner_token, f"Safety role {tag()}", codes,
    )


async def owner_user_id(client: httpx.AsyncClient, owner_token: str) -> int:
    response = await client.get("/auth/me", headers=hdr(owner_token))
    assert response.status_code == 200, response.text
    return response.json()["user_id"]


async def active_assignment_id(client: httpx.AsyncClient, token: str, user_id: int) -> int:
    response = await client.get(f"/admin/users/{user_id}", headers=hdr(token))
    assert response.status_code == 200, response.text
    return response.json()["company_role_assignment_id"]


def assign_body(role_id: int) -> dict:
    return {"company_role_id": role_id, "scope_type": "AllCompanyBranches"}


def refusal_code(response: httpx.Response) -> str:
    detail = response.json()["detail"]
    assert isinstance(detail, dict), detail
    return detail["code"]


async def test_nobody_can_assign_themselves_a_company_role(client, auth_token):
    actor = await manager(client, auth_token)
    other_role = await role_with(client, auth_token, ["payroll.view"])

    response = await client.post(
        f"/admin/users/{actor['user_id']}/company-role-assignments",
        json=assign_body(other_role), headers=hdr(actor["token"]),
    )

    assert response.status_code == 403
    assert refusal_code(response) == "SELF_ACCESS_CHANGE_FORBIDDEN"
    assert await active_assignment_id(client, auth_token, actor["user_id"]) is not None


async def test_nobody_can_revoke_their_own_company_role_assignment(client, auth_token):
    actor = await manager(client, auth_token)
    assignment_id = await active_assignment_id(client, auth_token, actor["user_id"])

    response = await client.delete(
        f"/admin/users/{actor['user_id']}/company-role-assignments/{assignment_id}",
        headers=hdr(actor["token"]),
    )

    assert response.status_code == 403
    assert refusal_code(response) == "SELF_ACCESS_CHANGE_FORBIDDEN"


async def test_nobody_can_change_their_own_extra_permissions(client, auth_token):
    actor = await manager(client, auth_token)

    response = await client.put(
        f"/admin/users/{actor['user_id']}/permission-overrides",
        json={"permission_codes": ["payroll.view"]}, headers=hdr(actor["token"]),
    )

    assert response.status_code == 403
    assert refusal_code(response) == "SELF_ACCESS_CHANGE_FORBIDDEN"


async def test_the_company_owner_cannot_change_their_own_access_either(client, auth_token):
    owner_id = await owner_user_id(client, auth_token)
    assignment_id = await active_assignment_id(client, auth_token, owner_id)
    other_role = await role_with(client, auth_token, ["payroll.view"])

    assigned = await client.post(
        f"/admin/users/{owner_id}/company-role-assignments",
        json=assign_body(other_role), headers=hdr(auth_token),
    )
    revoked = await client.delete(
        f"/admin/users/{owner_id}/company-role-assignments/{assignment_id}",
        headers=hdr(auth_token),
    )
    overrides = await client.put(
        f"/admin/users/{owner_id}/permission-overrides",
        json={"permission_codes": ["payroll.view"]}, headers=hdr(auth_token),
    )

    for response in (assigned, revoked, overrides):
        assert response.status_code == 403, response.text
        assert refusal_code(response) == "SELF_ACCESS_CHANGE_FORBIDDEN"


async def test_someone_else_can_still_change_a_persons_access(client, auth_token):
    actor = await manager(client, auth_token)
    target = await target_user(client, auth_token)
    role = await role_with(client, auth_token, ["payroll.view"])

    assigned = await client.post(
        f"/admin/users/{target['user_id']}/company-role-assignments",
        json=assign_body(role), headers=hdr(actor["token"]),
    )
    overrides = await client.put(
        f"/admin/users/{target['user_id']}/permission-overrides",
        json={"permission_codes": ["reports.view"]}, headers=hdr(actor["token"]),
    )

    assert assigned.status_code in (200, 201), assigned.text
    assert overrides.status_code == 200, overrides.text


async def test_assigning_a_role_needs_every_permission_in_it(client, auth_token):
    actor = await manager(client, auth_token)
    target = await target_user(client, auth_token)
    beyond = await role_with(client, auth_token, ["payroll.view", "payroll.approve", "payroll.finalize"])
    within = await role_with(client, auth_token, ["payroll.view", "reports.view"])

    refused = await client.post(
        f"/admin/users/{target['user_id']}/company-role-assignments",
        json=assign_body(beyond), headers=hdr(actor["token"]),
    )
    allowed = await client.post(
        f"/admin/users/{target['user_id']}/company-role-assignments",
        json=assign_body(within), headers=hdr(actor["token"]),
    )

    assert refused.status_code == 403
    assert refusal_code(refused) == "GRANT_CEILING_EXCEEDED"
    detail = refused.json()["detail"]
    assert detail["permission_codes"] == ["payroll.approve", "payroll.finalize"]
    assert "payroll.approve" in detail["message"] and "payroll.finalize" in detail["message"]
    assert allowed.status_code in (200, 201), allowed.text


async def test_refused_assignment_leaves_the_existing_role_in_place(client, auth_token):
    actor = await manager(client, auth_token)
    target = await target_user(client, auth_token)
    before = await active_assignment_id(client, auth_token, target["user_id"])
    beyond = await role_with(client, auth_token, ["payroll.view", "payroll.approve"])

    refused = await client.post(
        f"/admin/users/{target['user_id']}/company-role-assignments",
        json=assign_body(beyond), headers=hdr(actor["token"]),
    )

    assert refused.status_code == 403
    assert await active_assignment_id(client, auth_token, target["user_id"]) == before


async def test_the_company_owner_can_assign_any_role(client, auth_token):
    target = await target_user(client, auth_token)
    broad = await role_with(client, auth_token, ["payroll.view", "payroll.approve", "payroll.finalize"])

    response = await client.post(
        f"/admin/users/{target['user_id']}/company-role-assignments",
        json=assign_body(broad), headers=hdr(auth_token),
    )

    assert response.status_code in (200, 201), response.text


async def test_the_settings_manage_fallback_does_not_lift_the_role_ceiling(client, auth_token):
    actor = await manager(client, auth_token, ["settings.manage"])
    target = await target_user(client, auth_token)
    beyond = await role_with(client, auth_token, ["payroll.view", "payroll.approve"])

    response = await client.post(
        f"/admin/users/{target['user_id']}/company-role-assignments",
        json=assign_body(beyond), headers=hdr(actor["token"]),
    )

    assert response.status_code == 403
    assert refusal_code(response) == "GRANT_CEILING_EXCEEDED"


async def test_creating_or_provisioning_a_user_with_a_role_needs_every_permission_in_it(client, auth_token):
    actor = await manager(client, auth_token, [*MANAGER_CODES, "users.create"])
    beyond = await role_with(client, auth_token, ["payroll.view", "payroll.approve"])
    within = await role_with(client, auth_token, ["payroll.view"])

    def payload(role_id: int) -> dict:
        return {
            "username": f"new_{tag()}", "display_name": "New Person", "password": "TestPass123!",
            "is_active": True, "can_login": True, "must_change_password": False,
            "role_assignment": assign_body(role_id),
        }

    refused = await client.post("/admin/users", json=payload(beyond), headers=hdr(actor["token"]))
    allowed = await client.post("/admin/users", json=payload(within), headers=hdr(actor["token"]))

    assert refused.status_code == 403
    assert refusal_code(refused) == "GRANT_CEILING_EXCEEDED"
    assert allowed.status_code == 201, allowed.text

    staged = await client.post(
        "/admin/users",
        json={"username": f"stg_{tag()}", "display_name": "Staged", "password": "TestPass123!",
              "is_active": True, "is_staged": True, "can_login": False},
        headers=hdr(auth_token),
    )
    assert staged.status_code == 201, staged.text
    provision_body = {"role_assignment": assign_body(beyond), "can_login": False}
    refused_provision = await client.post(
        f"/admin/users/{staged.json()['user_id']}/provision",
        json=provision_body, headers=hdr(actor["token"]),
    )
    assert refused_provision.status_code == 403
    assert refusal_code(refused_provision) == "GRANT_CEILING_EXCEEDED"


async def test_extra_permissions_can_only_add_what_the_caller_holds(client, auth_token):
    actor = await manager(client, auth_token)
    target = await target_user(client, auth_token)
    url = f"/admin/users/{target['user_id']}/permission-overrides"

    refused = await client.put(
        url, json={"permission_codes": ["reports.view", "payroll.approve"]}, headers=hdr(actor["token"]),
    )
    allowed = await client.put(
        url, json={"permission_codes": ["reports.view"]}, headers=hdr(actor["token"]),
    )

    assert refused.status_code == 403
    assert refusal_code(refused) == "GRANT_CEILING_EXCEEDED"
    assert refused.json()["detail"]["permission_codes"] == ["payroll.approve"]
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == ["reports.view"]


async def test_extra_permissions_the_caller_lacks_can_be_removed_but_not_re_added(client, auth_token):
    actor = await manager(client, auth_token)
    target = await target_user(client, auth_token)
    url = f"/admin/users/{target['user_id']}/permission-overrides"
    seeded = await client.put(
        url, json={"permission_codes": ["payroll.view", "payroll.approve"]}, headers=hdr(auth_token),
    )
    assert seeded.status_code == 200, seeded.text

    # Keeping what is already there is not a new grant, and removing is always allowed.
    kept = await client.put(
        url, json={"permission_codes": ["payroll.view", "payroll.approve", "reports.view"]},
        headers=hdr(actor["token"]),
    )
    removed = await client.put(url, json={"permission_codes": ["payroll.view"]}, headers=hdr(actor["token"]))
    readded = await client.put(
        url, json={"permission_codes": ["payroll.view", "payroll.approve"]}, headers=hdr(actor["token"]),
    )
    cleared = await client.put(url, json={"permission_codes": []}, headers=hdr(actor["token"]))

    assert kept.status_code == 200, kept.text
    assert removed.status_code == 200, removed.text
    assert readded.status_code == 403
    assert refusal_code(readded) == "GRANT_CEILING_EXCEEDED"
    assert cleared.status_code == 200, cleared.text


async def test_the_company_owner_can_grant_any_extra_permission(client, auth_token):
    target = await target_user(client, auth_token)

    response = await client.put(
        f"/admin/users/{target['user_id']}/permission-overrides",
        json={"permission_codes": ["payroll.view", "payroll.approve"]}, headers=hdr(auth_token),
    )

    assert response.status_code == 200, response.text


async def test_role_permissions_can_only_add_what_the_caller_holds(client, auth_token):
    actor = await manager(client, auth_token)
    role_id = await role_with(client, auth_token, ["payroll.view"])
    url = f"/admin/company-roles/{role_id}/permissions"

    refused = await client.put(
        url, json={"permission_codes": ["payroll.view", "payroll.finalize"]}, headers=hdr(actor["token"]),
    )
    allowed = await client.put(
        url, json={"permission_codes": ["payroll.view", "reports.view"]}, headers=hdr(actor["token"]),
    )

    assert refused.status_code == 403
    assert refusal_code(refused) == "GRANT_CEILING_EXCEEDED"
    assert refused.json()["detail"]["permission_codes"] == ["payroll.finalize"]
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["permission_codes"] == ["payroll.view", "reports.view"]


async def test_role_permissions_the_role_already_had_are_kept_without_being_held(client, auth_token):
    actor = await manager(client, auth_token)
    role_id = await role_with(client, auth_token, ["payroll.view", "payroll.approve"])
    url = f"/admin/company-roles/{role_id}/permissions"

    kept = await client.put(
        url, json={"permission_codes": ["payroll.view", "payroll.approve", "reports.view"]},
        headers=hdr(actor["token"]),
    )
    removed = await client.put(url, json={"permission_codes": ["payroll.view"]}, headers=hdr(actor["token"]))

    assert kept.status_code == 200, kept.text
    assert removed.status_code == 200, removed.text
    assert removed.json()["permission_codes"] == ["payroll.view"]


async def test_the_company_owner_can_add_any_permission_to_a_role(client, auth_token):
    role_id = await role_with(client, auth_token, ["payroll.view"])

    response = await client.put(
        f"/admin/company-roles/{role_id}/permissions",
        json={"permission_codes": ["payroll.view", "payroll.approve", "payroll.finalize"]},
        headers=hdr(auth_token),
    )

    assert response.status_code == 200, response.text


async def test_a_role_you_hold_cannot_be_edited_by_you(client, auth_token):
    actor = await manager(client, auth_token)
    url = f"/admin/company-roles/{actor['role_id']}/permissions"

    removing = await client.put(
        url, json={"permission_codes": ["users.view", "users.edit", "roles.view", "roles.edit"]},
        headers=hdr(actor["token"]),
    )
    unchanged = await client.put(url, json={"permission_codes": MANAGER_CODES}, headers=hdr(actor["token"]))

    for response in (removing, unchanged):
        assert response.status_code == 403, response.text
        assert refusal_code(response) == "OWN_ROLE_PERMISSIONS_EDIT_FORBIDDEN"
    stored = await client.get(url, headers=hdr(auth_token))
    assert sorted(stored.json()["permission_codes"]) == sorted(MANAGER_CODES)


async def test_someone_else_can_edit_a_role_you_hold(client, auth_token):
    actor = await manager(client, auth_token)
    other = await manager(client, auth_token)

    response = await client.put(
        f"/admin/company-roles/{actor['role_id']}/permissions",
        json={"permission_codes": ["users.view", "users.edit", "roles.view", "roles.edit"]},
        headers=hdr(other["token"]),
    )

    assert response.status_code == 200, response.text


async def test_the_company_owner_can_edit_any_custom_role(client, auth_token):
    actor = await manager(client, auth_token)

    response = await client.put(
        f"/admin/company-roles/{actor['role_id']}/permissions",
        json={"permission_codes": MANAGER_CODES}, headers=hdr(auth_token),
    )

    assert response.status_code == 200, response.text
