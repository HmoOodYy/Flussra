"""Regression coverage for Access writers staying outside Workforce authority."""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from tests.builders.access import create_company_role_with_permissions, get_company_role_id
from tests.builders.workforce import create_driver_employee_record


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create_role_user(
    client: httpx.AsyncClient,
    token: str,
    role_id: int,
    *,
    employee_id: int | None = None,
    can_login: bool = False,
) -> dict:
    tag = uuid4().hex[:12]
    payload = {
        "username": f"p2c_{tag}",
        "display_name": f"P2c User {tag}",
        "password": "TestPass123!",
        "is_active": True,
        "can_login": can_login,
        "is_staged": False,
        "role_assignment": {
            "company_role_id": role_id,
            "scope_type": "AllCompanyBranches",
        },
    }
    if employee_id is not None:
        payload["employee_id"] = employee_id
    response = await client.post("/admin/users", json=payload, headers=_headers(token))
    assert response.status_code == 201, response.text
    return response.json()


async def _create_staged_user(client: httpx.AsyncClient, token: str) -> dict:
    tag = uuid4().hex[:12]
    response = await client.post(
        "/admin/users",
        json={
            "username": f"p2c_staged_{tag}",
            "display_name": f"P2c Staged {tag}",
            "password": "TestPass123!",
            "is_active": True,
            "can_login": False,
            "is_staged": True,
        },
        headers=_headers(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_employee_without_driver(
    client: httpx.AsyncClient, token: str,
) -> dict:
    tag = uuid4().hex[:8]
    response = await client.post(
        "/workforce/employees",
        json={
            "branch_id": 1,
            "full_name": f"P2c Employee {tag}",
            "hire_date": "2000-01-01",
        },
        headers=_headers(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _workforce_snapshot(db) -> tuple[list[str], list[str]]:
    employees = await db.execute(
        text("SELECT to_jsonb(e)::text FROM core.Employees e ORDER BY EmployeeID")
    )
    drivers = await db.execute(
        text("SELECT to_jsonb(d)::text FROM core.Drivers d ORDER BY DriverID")
    )
    return employees.scalars().all(), drivers.scalars().all()


async def _linked_employee_id(db, user_id: int) -> int | None:
    result = await db.execute(
        text("SELECT EmployeeID FROM sec.Users WHERE UserID = :user_id"),
        {"user_id": user_id},
    )
    return result.scalar_one()


async def _employee_and_driver_counts(db, employee_id: int) -> tuple[int, int]:
    result = await db.execute(
        text("""
            SELECT
                (SELECT COUNT(*) FROM core.Employees WHERE EmployeeID = :employee_id),
                (SELECT COUNT(*) FROM core.Drivers WHERE EmployeeID = :employee_id)
        """),
        {"employee_id": employee_id},
    )
    return result.one()


async def _snapshot_access_account(db, user_id: int) -> dict:
    account = await db.execute(
        text("""
            SELECT UserID, IsActive, IsStaged, CanLogin, EmployeeID, UpdatedAtUtc
            FROM sec.Users WHERE UserID = :user_id
        """),
        {"user_id": user_id},
    )
    assignments = await db.execute(
        text("""
            SELECT UserBranchRoleID, IsActive, RevokedAtUtc
            FROM sec.UserBranchRoles
            WHERE UserID = :user_id AND CompanyID = 1
            ORDER BY UserBranchRoleID
        """),
        {"user_id": user_id},
    )
    return {
        "account": dict(account.mappings().one()),
        "assignments": [dict(row) for row in assignments.mappings().all()],
    }


async def _restore_access_account(db, user_id: int, snapshot: dict) -> None:
    await db.execute(
        text("""
            UPDATE sec.UserBranchRoles
            SET IsActive = FALSE, RevokedAtUtc = COALESCE(RevokedAtUtc, NOW())
            WHERE UserID = :user_id AND CompanyID = 1
        """),
        {"user_id": user_id},
    )
    for assignment in snapshot["assignments"]:
        await db.execute(
            text("""
                UPDATE sec.UserBranchRoles
                SET IsActive = :is_active, RevokedAtUtc = :revoked_at
                WHERE UserBranchRoleID = :assignment_id
            """),
            {
                "assignment_id": assignment["userbranchroleid"],
                "is_active": assignment["isactive"],
                "revoked_at": assignment["revokedatutc"],
            },
        )
    account = snapshot["account"]
    await db.execute(
        text("""
            UPDATE sec.Users
            SET IsActive = :is_active, IsStaged = :is_staged,
                CanLogin = :can_login, EmployeeID = :employee_id,
                UpdatedAtUtc = :updated_at
            WHERE UserID = :user_id
        """),
        {
            "user_id": user_id,
            "is_active": account["isactive"],
            "is_staged": account["isstaged"],
            "can_login": account["canlogin"],
            "employee_id": account["employeeid"],
            "updated_at": account["updatedatutc"],
        },
    )


@pytest.mark.asyncio
async def test_company_role_swap_and_revoke_leave_workforce_unchanged(
    client: httpx.AsyncClient,
    auth_token: str,
    direct_db,
):
    initial_role = await get_company_role_id(client, auth_token, "PAYROLL_VIEWER_CO")
    replacement_role = await create_company_role_with_permissions(
        client, auth_token, f"P2c replacement {uuid4().hex[:8]}", [],
    )
    user = await _create_role_user(client, auth_token, initial_role)
    user_id = user["user_id"]
    before, link_before = await _workforce_snapshot(direct_db), await _linked_employee_id(direct_db, user_id)

    swapped = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json={"company_role_id": replacement_role, "scope_type": "AllCompanyBranches"},
        headers=_headers(auth_token),
    )
    assert swapped.status_code == 201, swapped.text
    assert swapped.json()["company_role_id"] == replacement_role
    assert await _workforce_snapshot(direct_db) == before
    assert link_before is None
    assert await _linked_employee_id(direct_db, user_id) is None

    revoked = await client.delete(
        f"/admin/users/{user_id}/company-role-assignments/{swapped.json()['assignment_id']}",
        headers=_headers(auth_token),
    )
    assert revoked.status_code == 200, revoked.text
    account = await client.get(f"/admin/users/{user_id}", headers=_headers(auth_token))
    assert account.status_code == 200
    assert account.json()["is_staged"] is True
    assert await _workforce_snapshot(direct_db) == before
    assert await _linked_employee_id(direct_db, user_id) is None


@pytest.mark.asyncio
async def test_unlinked_driver_assignment_and_provisioning_fail_without_workforce_creation(
    client: httpx.AsyncClient,
    auth_token: str,
    direct_db,
):
    neutral_role = await get_company_role_id(client, auth_token, "PAYROLL_VIEWER_CO")
    driver_role = await get_company_role_id(client, auth_token, "DRIVER")
    user = await _create_role_user(client, auth_token, neutral_role)
    before = await _workforce_snapshot(direct_db)

    assigned = await client.post(
        f"/admin/users/{user['user_id']}/company-role-assignments",
        json={"company_role_id": driver_role, "scope_type": "Self"},
        headers=_headers(auth_token),
    )
    assert assigned.status_code == 422, assigned.text
    assert await _linked_employee_id(direct_db, user["user_id"]) is None

    staged = await _create_staged_user(client, auth_token)
    provisioned = await client.post(
        f"/admin/users/{staged['user_id']}/provision",
        json={"role_assignment": {"company_role_id": driver_role, "scope_type": "Self"}},
        headers=_headers(auth_token),
    )
    assert provisioned.status_code == 422, provisioned.text
    refreshed = await client.get(f"/admin/users/{staged['user_id']}", headers=_headers(auth_token))
    assert refreshed.json()["is_staged"] is True
    assert refreshed.json()["role_assignments"] == []
    assert await _linked_employee_id(direct_db, staged["user_id"]) is None
    assert await _workforce_snapshot(direct_db) == before


@pytest.mark.asyncio
async def test_linked_employee_without_current_driver_is_not_manufactured(
    client: httpx.AsyncClient,
    auth_token: str,
    direct_db,
):
    neutral_role = await get_company_role_id(client, auth_token, "PAYROLL_VIEWER_CO")
    driver_role = await get_company_role_id(client, auth_token, "DRIVER")
    employee = await _create_employee_without_driver(client, auth_token)
    user = await _create_role_user(client, auth_token, neutral_role)
    linked = await client.put(
        f"/admin/users/{user['user_id']}/employee-link",
        json={"employee_id": employee["employee_id"]},
        headers=_headers(auth_token),
    )
    assert linked.status_code == 200, linked.text
    before = await _workforce_snapshot(direct_db)

    assigned = await client.post(
        f"/admin/users/{user['user_id']}/company-role-assignments",
        json={"company_role_id": driver_role, "scope_type": "Self"},
        headers=_headers(auth_token),
    )
    assert assigned.status_code == 422, assigned.text
    assert await _employee_and_driver_counts(direct_db, employee["employee_id"]) == (1, 0)
    assert await _linked_employee_id(direct_db, user["user_id"]) == employee["employee_id"]
    assert await _workforce_snapshot(direct_db) == before

    staged = await _create_staged_user(client, auth_token)
    linked_staged = await client.put(
        f"/admin/users/{staged['user_id']}/employee-link",
        json={"employee_id": employee["employee_id"]},
        headers=_headers(auth_token),
    )
    assert linked_staged.status_code == 409, linked_staged.text

    second_employee = await _create_employee_without_driver(client, auth_token)
    linked_staged = await client.put(
        f"/admin/users/{staged['user_id']}/employee-link",
        json={"employee_id": second_employee["employee_id"]},
        headers=_headers(auth_token),
    )
    assert linked_staged.status_code == 200, linked_staged.text
    before_provision = await _workforce_snapshot(direct_db)
    provisioned = await client.post(
        f"/admin/users/{staged['user_id']}/provision",
        json={"role_assignment": {"company_role_id": driver_role, "scope_type": "Self"}},
        headers=_headers(auth_token),
    )
    assert provisioned.status_code == 422, provisioned.text
    assert await _employee_and_driver_counts(direct_db, second_employee["employee_id"]) == (1, 0)
    assert await _linked_employee_id(direct_db, staged["user_id"]) == second_employee["employee_id"]
    assert await _workforce_snapshot(direct_db) == before_provision


@pytest.mark.asyncio
async def test_valid_driver_role_swap_and_revoke_preserve_workforce_and_link(
    client: httpx.AsyncClient,
    auth_token: str,
    direct_db,
):
    driver = await create_driver_employee_record(
        client, auth_token, branch_id=1,
        full_name=f"P2c linked Driver {uuid4().hex[:8]}",
        driver_code=f"P2C-{uuid4().hex[:10]}",
        hire_date="2000-01-01",
    )
    employee_id = driver["employee_id"]
    driver_id = driver["current_or_pending_driver"]["driver_id"]
    neutral_role = await get_company_role_id(client, auth_token, "PAYROLL_VIEWER_CO")
    driver_role = await get_company_role_id(client, auth_token, "DRIVER")
    user = await _create_role_user(client, auth_token, neutral_role)
    link = await client.put(
        f"/admin/users/{user['user_id']}/employee-link",
        json={"employee_id": employee_id},
        headers=_headers(auth_token),
    )
    assert link.status_code == 200, link.text
    before = await _workforce_snapshot(direct_db)

    assigned = await client.post(
        f"/admin/users/{user['user_id']}/company-role-assignments",
        json={"company_role_id": driver_role, "scope_type": "Self"},
        headers=_headers(auth_token),
    )
    assert assigned.status_code == 201, assigned.text
    assert assigned.json()["scope_type"] == "Self"
    assert assigned.json()["branch_id"] is None
    assert assigned.json()["company_role_code"] == "DRIVER"
    assert await _linked_employee_id(direct_db, user["user_id"]) == employee_id
    assert await _workforce_snapshot(direct_db) == before
    assert await _employee_and_driver_counts(direct_db, employee_id) == (1, 1)
    current_driver = await direct_db.execute(text("SELECT DriverID FROM core.Drivers WHERE EmployeeID = :employee_id"), {"employee_id": employee_id})
    assert current_driver.scalar_one() == driver_id

    disabled = await client.patch(
        f"/admin/users/{user['user_id']}",
        json={"can_login": False},
        headers=_headers(auth_token),
    )
    assert disabled.status_code == 200, disabled.text
    revoked = await client.delete(
        f"/admin/users/{user['user_id']}/company-role-assignments/{assigned.json()['assignment_id']}",
        headers=_headers(auth_token),
    )
    assert revoked.status_code == 200, revoked.text
    account = await client.get(f"/admin/users/{user['user_id']}", headers=_headers(auth_token))
    assert account.json()["is_staged"] is True
    assert await _linked_employee_id(direct_db, user["user_id"]) == employee_id
    assert await _workforce_snapshot(direct_db) == before


@pytest.mark.asyncio
async def test_driver_provisioning_uses_an_explicit_link_to_existing_workforce(
    client: httpx.AsyncClient,
    auth_token: str,
    direct_db,
):
    driver = await create_driver_employee_record(
        client, auth_token, branch_id=1,
        full_name=f"P2c provisioned Driver {uuid4().hex[:8]}",
        driver_code=f"P2C-{uuid4().hex[:10]}",
        hire_date="2000-01-01",
    )
    staged = await _create_staged_user(client, auth_token)
    link = await client.put(
        f"/admin/users/{staged['user_id']}/employee-link",
        json={"employee_id": driver["employee_id"]},
        headers=_headers(auth_token),
    )
    assert link.status_code == 200, link.text
    before = await _workforce_snapshot(direct_db)
    driver_role = await get_company_role_id(client, auth_token, "DRIVER")

    provisioned = await client.post(
        f"/admin/users/{staged['user_id']}/provision",
        json={"role_assignment": {"company_role_id": driver_role, "scope_type": "Self"}},
        headers=_headers(auth_token),
    )
    assert provisioned.status_code == 200, provisioned.text
    assert provisioned.json()["is_staged"] is False
    assert provisioned.json()["company_role_code"] == "DRIVER"
    assert await _linked_employee_id(direct_db, staged["user_id"]) == driver["employee_id"]
    assert await _workforce_snapshot(direct_db) == before


@pytest.mark.asyncio
async def test_legacy_role_assign_and_revoke_are_access_only(
    client: httpx.AsyncClient,
    auth_token: str,
    direct_db,
):
    driver = await create_driver_employee_record(
        client, auth_token, branch_id=1,
        full_name=f"P2c legacy-linked Driver {uuid4().hex[:8]}",
        driver_code=f"P2C-{uuid4().hex[:10]}",
        hire_date="2000-01-01",
    )
    neutral_role = await get_company_role_id(client, auth_token, "PAYROLL_VIEWER_CO")
    user = await _create_role_user(client, auth_token, neutral_role)
    link = await client.put(
        f"/admin/users/{user['user_id']}/employee-link",
        json={"employee_id": driver["employee_id"]},
        headers=_headers(auth_token),
    )
    assert link.status_code == 200, link.text
    before = await _workforce_snapshot(direct_db)

    roles = await client.get("/admin/roles", headers=_headers(auth_token))
    assert roles.status_code == 200, roles.text
    legacy_viewer = next(row for row in roles.json() if row["role_code"] == "PAYROLL_VIEWER")
    assigned = await client.post(
        f"/admin/users/{user['user_id']}/roles",
        json={"role_id": legacy_viewer["role_id"], "scope_type": "AllCompanyBranches"},
        headers=_headers(auth_token),
    )
    assert assigned.status_code == 201, assigned.text
    assert await _workforce_snapshot(direct_db) == before
    assert await _linked_employee_id(direct_db, user["user_id"]) == driver["employee_id"]

    # The global Role API intentionally has no creation authority. Seed the
    # legacy-only DRIVER identity here to verify the retained exact-code guard.
    legacy_driver = await direct_db.execute(
        text("SELECT RoleID FROM sec.Roles WHERE RoleCode = 'DRIVER'")
    )
    legacy_driver_id = legacy_driver.scalar_one_or_none()
    created_legacy_driver = legacy_driver_id is None
    if created_legacy_driver:
        legacy_driver_id = (
            await direct_db.execute(
                text("""
                    INSERT INTO sec.Roles (RoleCode, RoleName, IsSystemRole, Notes)
                    VALUES ('DRIVER', 'Legacy Driver (P2c test)', TRUE,
                            'Historical legacy assignment guard test')
                    RETURNING RoleID
                """)
            )
        ).scalar_one()
    try:
        forbidden_driver = await client.post(
            f"/admin/users/{user['user_id']}/roles",
            json={"role_id": legacy_driver_id, "scope_type": "AllCompanyBranches"},
            headers=_headers(auth_token),
        )
        assert forbidden_driver.status_code == 422, forbidden_driver.text
        assert await _workforce_snapshot(direct_db) == before
    finally:
        if created_legacy_driver:
            await direct_db.execute(
                text("DELETE FROM sec.Roles WHERE RoleID = :role_id"),
                {"role_id": legacy_driver_id},
            )

    revoked = await client.delete(
        f"/admin/users/{user['user_id']}/roles/{assigned.json()['assignment_id']}",
        headers=_headers(auth_token),
    )
    assert revoked.status_code == 200, revoked.text
    assert await _workforce_snapshot(direct_db) == before
    assert await _linked_employee_id(direct_db, user["user_id"]) == driver["employee_id"]


@pytest.mark.asyncio
async def test_owner_transfer_and_replacement_role_leave_workforce_unchanged(
    client: httpx.AsyncClient,
    auth_token: str,
    direct_db,
):
    driver = await create_driver_employee_record(
        client, auth_token, branch_id=1,
        full_name=f"P2c owner-linked Driver {uuid4().hex[:8]}",
        driver_code=f"P2C-{uuid4().hex[:10]}",
        hire_date="2000-01-01",
    )
    neutral_role = await get_company_role_id(client, auth_token, "PAYROLL_VIEWER_CO")
    target = await _create_role_user(
        client, auth_token, neutral_role, can_login=True,
    )
    link = await client.put(
        f"/admin/users/{target['user_id']}/employee-link",
        json={"employee_id": driver["employee_id"]},
        headers=_headers(auth_token),
    )
    assert link.status_code == 200, link.text
    replacement_role = await create_company_role_with_permissions(
        client, auth_token, f"P2c owner replacement {uuid4().hex[:8]}", [],
    )
    owner_id = await direct_db.execute(
        text("SELECT UserID FROM sec.Users WHERE Username = 'admin'")
    )
    owner_id = owner_id.scalar_one()
    before = await _workforce_snapshot(direct_db)
    owner_access_before = await _snapshot_access_account(direct_db, owner_id)
    target_access_before = await _snapshot_access_account(direct_db, target["user_id"])
    target_password = "TestPass123!"
    try:
        response = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": target["user_id"],
                "replacement_company_role_id": replacement_role,
                "confirmation": "TRANSFER",
            },
            headers=_headers(auth_token),
        )
        assert response.status_code == 200, response.text
        assert await _workforce_snapshot(direct_db) == before
        assert await _linked_employee_id(direct_db, target["user_id"]) == driver["employee_id"]

        login = await client.post(
            "/auth/login",
            json={
                "company_code": "DEMO",
                "username": target["username"],
                "password": target_password,
            },
        )
        assert login.status_code == 200, login.text
        target_token = login.json()["access_token"]
        restore = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": owner_id,
                "replacement_company_role_id": replacement_role,
                "confirmation": "TRANSFER",
            },
            headers=_headers(target_token),
        )
        assert restore.status_code == 200, restore.text
        assert await _workforce_snapshot(direct_db) == before
        assert await _linked_employee_id(direct_db, target["user_id"]) == driver["employee_id"]
    finally:
        await _restore_access_account(direct_db, owner_id, owner_access_before)
        await _restore_access_account(direct_db, target["user_id"], target_access_before)
