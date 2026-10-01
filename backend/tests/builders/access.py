"""Shared Access setup builders for authenticated HTTP integration tests."""

import httpx


def _require_status(response: httpx.Response, expected: int, operation: str) -> None:
    if response.status_code != expected:
        raise RuntimeError(
            f"{operation} expected HTTP {expected}, got {response.status_code}: {response.text}"
        )


async def create_company_role_with_permissions(
    client: httpx.AsyncClient,
    admin_token: str,
    role_name: str,
    permission_codes: list[str],
) -> int:
    """Create a company role and set its permission codes through Admin APIs."""
    headers = {"Authorization": f"Bearer {admin_token}"}
    created = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=headers,
    )
    _require_status(created, 201, "Create company role")
    role_id = created.json()["company_role_id"]
    if permission_codes:
        updated = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": permission_codes},
            headers=headers,
        )
        _require_status(updated, 200, "Set company role permissions")
    return role_id


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
    _require_status(roles_response, 200, "List company roles")
    neutral = next(
        (role for role in roles_response.json() if role["role_code"] == "PAYROLL_VIEWER_CO"),
        None,
    )
    if neutral is None:
        raise LookupError("Seeded PAYROLL_VIEWER_CO company role was not found")
    return await create_provisioned_test_user(
        client,
        admin_token,
        username,
        neutral["company_role_id"],
        password=password,
        display_name=display_name,
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
    """Provision a test account, preserving the current Driver/ODA setup path."""
    auth_headers = {"Authorization": f"Bearer {admin_token}"}
    roles_response = await client.get("/admin/company-roles", headers=auth_headers)
    _require_status(roles_response, 200, "List company roles")
    roles = roles_response.json()
    requested_role = next(
        (role for role in roles if role["company_role_id"] == role_id),
        None,
    )
    if requested_role is None:
        raise LookupError(f"Company role {role_id} was not found")

    legacy_assignment = (
        requested_role["role_code"] == "DRIVER" or scope_type == "OwnDriverDataOnly"
    )
    initial_role_id = role_id
    initial_scope = scope_type
    initial_branch_id = branch_id
    if legacy_assignment:
        neutral_role = next(
            (role for role in roles if role["role_code"] == "PAYROLL_VIEWER_CO"),
            None,
        )
        if neutral_role is None:
            raise LookupError("Seeded PAYROLL_VIEWER_CO company role was not found")
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
    _require_status(response, 201, "Create provisioned test user")
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
        if assigned.status_code not in (200, 201):
            raise RuntimeError(
                "Assign legacy test role expected HTTP 200 or 201, "
                f"got {assigned.status_code}: {assigned.text}"
            )

    return user


async def create_user_with_role_token(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    role_id: int,
    scope_type: str = "AllCompanyBranches",
    branch_id: int | None = None,
    password: str = "TestPass123!",
) -> str:
    """Provision a role-scoped test user and return its authenticated token."""
    await create_provisioned_test_user(
        client,
        admin_token,
        username,
        role_id,
        scope_type=scope_type,
        branch_id=branch_id,
        password=password,
    )
    login = await client.post(
        "/auth/login",
        json={"username": username, "password": password, "company_code": "DEMO"},
    )
    _require_status(login, 200, "Test user login")
    return login.json()["access_token"]
