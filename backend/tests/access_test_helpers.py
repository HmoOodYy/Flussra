"""Small helpers for creating test accounts under the P2a lifecycle."""

import httpx


async def create_neutral_test_user(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    *,
    password: str = "TestPass123!",
    display_name: str | None = None,
) -> dict:
    """Create a usable test account with the seeded non-Driver base role."""
    headers = {"Authorization": f"Bearer {admin_token}"}
    roles_response = await client.get("/admin/company-roles", headers=headers)
    assert roles_response.status_code == 200, roles_response.text
    neutral = next(
        role for role in roles_response.json()
        if role["role_code"] == "PAYROLL_VIEWER_CO"
    )
    return await create_provisioned_test_user(
        client, admin_token, username, neutral["company_role_id"],
        password=password, display_name=display_name,
    )


async def create_provisioned_test_user(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    role_id: int,
    *,
    scope_type: str = "AllCompanyBranches",
    branch_id: int | None = None,
    password: str = "TestPass123!",
    display_name: str | None = None,
) -> dict:
    """Create an account with its usable role, retaining legacy Driver setup."""
    auth_headers = {"Authorization": f"Bearer {admin_token}"}
    roles_response = await client.get("/admin/company-roles", headers=auth_headers)
    assert roles_response.status_code == 200, roles_response.text
    roles = roles_response.json()
    requested_role = next(role for role in roles if role["company_role_id"] == role_id)

    legacy_assignment = (
        requested_role["role_code"] == "DRIVER" or scope_type == "OwnDriverDataOnly"
    )
    initial_role_id = role_id
    initial_scope = scope_type
    initial_branch_id = branch_id
    if legacy_assignment:
        neutral_role = next(role for role in roles if role["role_code"] == "PAYROLL_VIEWER_CO")
        initial_role_id = neutral_role["company_role_id"]
        initial_scope = "AllCompanyBranches"
        initial_branch_id = None

    assignment = {"company_role_id": initial_role_id, "scope_type": initial_scope}
    if initial_branch_id is not None:
        assignment["branch_id"] = initial_branch_id

    response = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": display_name or username,
            "password": password,
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
            "role_assignment": assignment,
        },
        headers=auth_headers,
    )
    assert response.status_code == 201, f"Create user failed: {response.text}"
    user = response.json()

    if legacy_assignment:
        legacy_body = {"company_role_id": role_id, "scope_type": scope_type}
        if branch_id is not None:
            legacy_body["branch_id"] = branch_id
        assigned = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json=legacy_body,
            headers=auth_headers,
        )
        assert assigned.status_code in (200, 201), f"Assign role failed: {assigned.text}"

    return user
