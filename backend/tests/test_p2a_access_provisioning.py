import asyncio
from datetime import date, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth.security import create_access_token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def user_payload(*, staged: bool = True, **values) -> dict:
    tag = uuid4().hex[:12]
    return {
        "username": f"p2a_{tag}", "display_name": "P2a Test User",
        "password": "TestPass123!", "is_active": True,
        "is_staged": staged, "can_login": False,
        **values,
    }


async def employee(client: httpx.AsyncClient, token: str, *, driver: bool = False, future: bool = False) -> dict:
    profile = None
    if driver:
        profile = {"driver_code": f"P2A-{uuid4().hex[:10]}"}
        if future:
            profile["effective_from"] = (date.today() + timedelta(days=365)).isoformat()
        else:
            profile["effective_from"] = "2000-01-01"
    response = await client.post(
        "/workforce/employees",
        json={"branch_id": 1, "full_name": f"P2a Employee {uuid4().hex[:8]}",
              "hire_date": "2000-01-01", "driver_profile": profile},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def company_role(client: httpx.AsyncClient, token: str, code: str) -> int:
    response = await client.get("/admin/company-roles", headers=auth(token))
    assert response.status_code == 200, response.text
    return next(item["company_role_id"] for item in response.json() if item["role_code"] == code)


async def staged_user(client: httpx.AsyncClient, token: str, *, employee_id: int | None = None) -> dict:
    payload = user_payload(employee_id=employee_id)
    response = await client.post("/admin/users", json=payload, headers=auth(token))
    assert response.status_code == 201, response.text
    return response.json()


async def provision(client: httpx.AsyncClient, token: str, user_id: int, role_id: int, *, scope: str = "AllCompanyBranches", branch_id: int | None = None, can_login: bool = False) -> httpx.Response:
    return await client.post(
        f"/admin/users/{user_id}/provision",
        json={"role_assignment": {"company_role_id": role_id, "scope_type": scope, "branch_id": branch_id},
              "can_login": can_login},
        headers=auth(token),
    )


@pytest.mark.asyncio
async def test_staged_account_link_lifecycle_and_constraints(client, auth_token, db_conn):
    first_employee = await employee(client, auth_token)
    second_employee = await employee(client, auth_token)
    first_user = await staged_user(client, auth_token)
    second_user = await staged_user(client, auth_token)
    first_id, second_id = first_employee["employee_id"], second_employee["employee_id"]
    user_id, other_user_id = first_user["user_id"], second_user["user_id"]

    linked = await client.put(f"/admin/users/{user_id}/employee-link", json={"employee_id": first_id}, headers=auth(auth_token))
    assert linked.status_code == 200 and linked.json()["employee_id"] == first_id
    retry = await client.put(f"/admin/users/{user_id}/employee-link", json={"employee_id": first_id}, headers=auth(auth_token))
    assert retry.status_code == 200
    conflict = await client.put(f"/admin/users/{user_id}/employee-link", json={"employee_id": second_id}, headers=auth(auth_token))
    assert conflict.status_code == 409
    conflict = await client.put(f"/admin/users/{other_user_id}/employee-link", json={"employee_id": first_id}, headers=auth(auth_token))
    assert conflict.status_code == 409

    await client.patch(f"/admin/users/{user_id}", json={"is_active": False}, headers=auth(auth_token))
    row = (await db_conn.execute(text("SELECT EmployeeID FROM sec.Users WHERE UserID=:uid"), {"uid": user_id})).first()
    assert row[0] == first_id
    held = await client.put(f"/admin/users/{other_user_id}/employee-link", json={"employee_id": first_id}, headers=auth(auth_token))
    assert held.status_code == 409
    blocked = await client.delete(f"/admin/users/{user_id}/employee-link", headers=auth(auth_token))
    assert blocked.status_code == 200
    linked_again = await client.put(f"/admin/users/{other_user_id}/employee-link", json={"employee_id": first_id}, headers=auth(auth_token))
    assert linked_again.status_code == 200

    duplicate = user_payload(employee_id=first_id)
    with pytest.raises(IntegrityError):
        await db_conn.execute(text("""
            INSERT INTO sec.Users (CompanyID, EmployeeID, Username, DisplayName, IsActive, CanLogin, IsStaged)
            VALUES (1, :eid, :username, 'duplicate link', TRUE, FALSE, TRUE)
        """), {"eid": first_id, "username": duplicate["username"]})

    cross_company_id = (await db_conn.execute(text("""
        INSERT INTO core.Companies (CompanyCode, CompanyName, LegalName, Status, IsSuspended, TimeZoneName)
        VALUES (:code, 'P2a Company', 'P2a Company', 'Active', FALSE, 'UTC') RETURNING CompanyID
    """), {"code": f"P2A{uuid4().hex[:8]}"})).scalar_one()
    cross_branch_id = (await db_conn.execute(text("""
        INSERT INTO core.Branches (CompanyID, BranchCode, BranchName, Status, IsDefault)
        VALUES (:cid, 'P2A', 'P2a Branch', 'Active', TRUE) RETURNING BranchID
    """), {"cid": cross_company_id})).scalar_one()
    cross_employee_id = (await db_conn.execute(text("""
        INSERT INTO core.Employees (CompanyID, BranchID, FullName, EmploymentStatus)
        VALUES (:cid, :bid, 'Cross Company Employee', 'Active') RETURNING EmployeeID
    """), {"cid": cross_company_id, "bid": cross_branch_id})).scalar_one()
    cross_link = await client.put(
        f"/admin/users/{second_user['user_id']}/employee-link",
        json={"employee_id": cross_employee_id}, headers=auth(auth_token),
    )
    assert cross_link.status_code in {409, 422}
    with pytest.raises(IntegrityError):
        await db_conn.execute(text("""
            INSERT INTO sec.Users (CompanyID, EmployeeID, Username, DisplayName, IsActive, CanLogin, IsStaged)
            VALUES (:cid, :eid, :username, 'cross-company', TRUE, FALSE, TRUE)
        """), {"cid": cross_company_id, "eid": first_id, "username": f"cross_{uuid4().hex[:8]}"})

    audits = await db_conn.execute(text("""
        SELECT ActionCode FROM audit.AuditLog
        WHERE EntitySchema='sec' AND EntityName='Users' AND EntityID=:user_id
          AND ActionCode IN ('USER_EMPLOYEE_LINKED', 'USER_EMPLOYEE_UNLINKED')
    """), {"user_id": str(user_id)})
    audit_actions = {row[0] for row in audits.fetchall()}
    assert audit_actions == {"USER_EMPLOYEE_LINKED", "USER_EMPLOYEE_UNLINKED"}


@pytest.mark.asyncio
async def test_account_lifecycle_endpoints_keep_existing_access_permission_gate(client, branch_user_token, auth_token):
    staged = await staged_user(client, auth_token)
    link = await client.put(
        f"/admin/users/{staged['user_id']}/employee-link", json={"employee_id": 1},
        headers=auth(branch_user_token),
    )
    provisioned = await client.post(
        f"/admin/users/{staged['user_id']}/provision",
        json={"role_assignment": {"company_role_id": 1, "scope_type": "AllCompanyBranches"}},
        headers=auth(branch_user_token),
    )
    assert link.status_code == 403
    assert provisioned.status_code == 403


@pytest.mark.asyncio
async def test_staged_login_role_guard_and_password_reset_preserve_state(client, auth_token, db_conn):
    user = await staged_user(client, auth_token)
    uid = user["user_id"]
    assert user["is_staged"] is True and user["can_login"] is False and not user["role_assignments"]
    login = await client.post("/auth/login", json={"username": user["username"], "password": "TestPass123!", "company_code": "DEMO"})
    assert login.status_code == 403 and login.json()["detail"]["code"] == "account_staged"
    token = create_access_token(user_id=uid, company_id=user["company_id"])
    me = await client.get("/auth/me", headers=auth(token))
    assert me.status_code == 403 and me.json()["detail"]["code"] == "account_staged"

    role_id = await company_role(client, auth_token, "PAYROLL_VIEWER_CO")
    legacy_role = (await client.get("/admin/roles", headers=auth(auth_token))).json()[0]["role_id"]
    direct = await client.post(f"/admin/users/{uid}/company-role-assignments", json={"company_role_id": role_id, "scope_type": "AllCompanyBranches"}, headers=auth(auth_token))
    assert direct.status_code == 422
    direct = await client.post(f"/admin/users/{uid}/roles", json={"role_id": legacy_role, "scope_type": "AllCompanyBranches"}, headers=auth(auth_token))
    assert direct.status_code == 422
    enabled = await client.patch(f"/admin/users/{uid}", json={"can_login": True}, headers=auth(auth_token))
    assert enabled.status_code == 422
    await db_conn.execute(text("UPDATE sec.Users SET IsStaged = FALSE WHERE UserID=:uid"), {"uid": uid})
    no_role_enable = await client.patch(f"/admin/users/{uid}", json={"can_login": True}, headers=auth(auth_token))
    assert no_role_enable.status_code == 422
    await db_conn.execute(text("UPDATE sec.Users SET IsStaged = TRUE WHERE UserID=:uid"), {"uid": uid})
    forbidden_field = await client.patch(f"/admin/users/{uid}", json={"is_staged": False}, headers=auth(auth_token))
    assert forbidden_field.status_code == 422

    reset = await client.post(f"/admin/users/{uid}/reset-password", json={"new_password": "ResetPass123!"}, headers=auth(auth_token))
    assert reset.status_code == 200
    state = (await db_conn.execute(text("SELECT IsStaged, CanLogin, EmployeeID FROM sec.Users WHERE UserID=:uid"), {"uid": uid})).one()
    assert state == (True, False, None)
    works = await client.post("/auth/login", json={"username": user["username"], "password": "ResetPass123!", "company_code": "DEMO"})
    assert works.status_code == 403 and works.json()["detail"]["code"] == "account_staged"


@pytest.mark.asyncio
async def test_provisioning_atomicity_and_final_role_lifecycle(client, auth_token, db_conn):
    user = await staged_user(client, auth_token)
    uid = user["user_id"]
    role_id = await company_role(client, auth_token, "PAYROLL_VIEWER_CO")
    invalid = await provision(client, auth_token, uid, 999999)
    assert invalid.status_code == 422
    row = (await db_conn.execute(text("SELECT IsStaged, CanLogin, (SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID=:uid AND IsActive) FROM sec.Users WHERE UserID=:uid"), {"uid": uid})).one()
    assert row == (True, False, 0)
    response = await provision(client, auth_token, uid, role_id, can_login=True)
    assert response.status_code == 200 and response.json()["is_staged"] is False and response.json()["can_login"] is True
    assignment_id = response.json()["company_role_assignment_id"]
    disabled = await client.patch(f"/admin/users/{uid}", json={"can_login": False}, headers=auth(auth_token))
    assert disabled.status_code == 200
    revoked = await client.delete(f"/admin/users/{uid}/company-role-assignments/{assignment_id}", headers=auth(auth_token))
    assert revoked.status_code == 200
    after = await client.get(f"/admin/users/{uid}", headers=auth(auth_token))
    assert after.json()["is_staged"] is True and after.json()["employee_id"] is None
    assert after.json()["company_role_assignment_id"] is None
    rejected = await provision(client, auth_token, uid, role_id)
    assert rejected.status_code == 200
    not_staged = await provision(client, auth_token, uid, role_id)
    assert not_staged.status_code == 422


@pytest.mark.asyncio
async def test_provisioned_user_requires_login_disable_before_final_role_revoke(client, auth_token):
    role_id = await company_role(client, auth_token, "PAYROLL_VIEWER_CO")
    user = await client.post("/admin/users", json=user_payload(staged=False, role_assignment={"company_role_id": role_id, "scope_type": "AllCompanyBranches"}, can_login=True), headers=auth(auth_token))
    assert user.status_code == 201, user.text
    uid = user.json()["user_id"]
    assignment_id = user.json()["company_role_assignment_id"]
    global_roles = (await client.get("/admin/roles", headers=auth(auth_token))).json()
    legacy_role = next(role["role_id"] for role in global_roles if role["role_code"] == "PAYROLL_VIEWER")
    legacy = await client.post(f"/admin/users/{uid}/roles", json={"role_id": legacy_role, "scope_type": "AllCompanyBranches"}, headers=auth(auth_token))
    assert legacy.status_code == 201, legacy.text
    swapped = await client.delete(f"/admin/users/{uid}/company-role-assignments/{assignment_id}", headers=auth(auth_token))
    assert swapped.status_code == 200
    rejected = await client.delete(f"/admin/users/{uid}/roles/{legacy.json()['assignment_id']}", headers=auth(auth_token))
    assert rejected.status_code == 422
    await client.patch(f"/admin/users/{uid}", json={"can_login": False}, headers=auth(auth_token))
    revoked = await client.delete(f"/admin/users/{uid}/roles/{legacy.json()['assignment_id']}", headers=auth(auth_token))
    assert revoked.status_code == 200
    still_staged = await client.get(f"/admin/users/{uid}", headers=auth(auth_token))
    assert still_staged.json()["is_staged"] is True


@pytest.mark.asyncio
async def test_create_modes_and_driver_boundary_are_access_only(client, auth_token, db_conn):
    role_id = await company_role(client, auth_token, "PAYROLL_VIEWER_CO")
    missing_assignment = await client.post("/admin/users", json=user_payload(staged=False), headers=auth(auth_token))
    assert missing_assignment.status_code == 422
    staged_conflict = await client.post("/admin/users", json=user_payload(staged=True, can_login=True), headers=auth(auth_token))
    assert staged_conflict.status_code == 422
    role_conflict = await client.post("/admin/users", json=user_payload(staged=True, role_assignment={"company_role_id": role_id, "scope_type": "AllCompanyBranches"}), headers=auth(auth_token))
    assert role_conflict.status_code == 422

    current = await employee(client, auth_token, driver=True)
    assert current["linked_user"] is None
    future = await employee(client, auth_token, driver=True, future=True)
    specific_employee = await employee(client, auth_token, driver=True)
    all_branch_employee = await employee(client, auth_token, driver=True)
    driver_role = await company_role(client, auth_token, "DRIVER")
    counts_before = (await db_conn.execute(text("SELECT (SELECT COUNT(*) FROM core.Employees), (SELECT COUNT(*) FROM core.Drivers)"))).one()

    staged = await staged_user(client, auth_token, employee_id=future["employee_id"])
    pending = await provision(client, auth_token, staged["user_id"], driver_role, scope="Self")
    assert pending.status_code == 422
    still_staged = await client.get(f"/admin/users/{staged['user_id']}", headers=auth(auth_token))
    assert still_staged.json()["is_staged"] is True and not still_staged.json()["role_assignments"]

    staged_current = await staged_user(client, auth_token, employee_id=current["employee_id"])
    accepted = await provision(client, auth_token, staged_current["user_id"], driver_role, scope="Self")
    assert accepted.status_code == 200, accepted.text
    driver_unlink = await client.delete(f"/admin/users/{staged_current['user_id']}/employee-link", headers=auth(auth_token))
    assert driver_unlink.status_code == 422
    swapped = await client.post(
        f"/admin/users/{staged_current['user_id']}/company-role-assignments",
        json={"company_role_id": role_id, "scope_type": "AllCompanyBranches"},
        headers=auth(auth_token),
    )
    assert swapped.status_code == 201, swapped.text
    disabled = await client.patch(f"/admin/users/{staged_current['user_id']}", json={"can_login": False}, headers=auth(auth_token))
    assert disabled.status_code == 200
    linked_state = await db_conn.execute(text("SELECT EmployeeID, IsStaged FROM sec.Users WHERE UserID=:uid"), {"uid": staged_current["user_id"]})
    assert linked_state.one() == (current["employee_id"], False)
    invalid_scope = await staged_user(client, auth_token)
    linked = await client.put(f"/admin/users/{invalid_scope['user_id']}/employee-link", json={"employee_id": current["employee_id"]}, headers=auth(auth_token))
    assert linked.status_code == 409

    non_driver = await staged_user(client, auth_token)
    invalid_self = await provision(client, auth_token, non_driver["user_id"], role_id, scope="Self")
    assert invalid_self.status_code == 422
    specific_driver = await staged_user(client, auth_token, employee_id=specific_employee["employee_id"])
    bad_scope = await provision(client, auth_token, specific_driver["user_id"], driver_role, scope="SpecificBranch", branch_id=1)
    assert bad_scope.status_code == 422
    all_branches = await staged_user(client, auth_token, employee_id=all_branch_employee["employee_id"])
    bad_scope = await provision(client, auth_token, all_branches["user_id"], driver_role, scope="AllCompanyBranches")
    assert bad_scope.status_code == 422

    counts_after = (await db_conn.execute(text("SELECT (SELECT COUNT(*) FROM core.Employees), (SELECT COUNT(*) FROM core.Drivers)"))).one()
    assert counts_after == counts_before


@pytest.mark.asyncio
async def test_duplicate_username_and_invalid_link_leave_no_user(client, auth_token, db_conn):
    original = await staged_user(client, auth_token)
    duplicate = await client.post("/admin/users", json=user_payload(username=original["username"]), headers=auth(auth_token))
    assert duplicate.status_code == 422
    employee_id = 99999999
    invalid_payload = user_payload(employee_id=employee_id)
    invalid = await client.post("/admin/users", json=invalid_payload, headers=auth(auth_token))
    assert invalid.status_code == 422
    check = await db_conn.execute(text("SELECT COUNT(*) FROM sec.Users WHERE Username=:username"), {"username": invalid_payload["username"]})
    assert check.scalar_one() == 0


@pytest.mark.asyncio
async def test_normal_creation_is_atomic_and_creates_access_assignment_only(client, auth_token, db_conn):
    role_id = await company_role(client, auth_token, "PAYROLL_VIEWER_CO")
    before = (await db_conn.execute(text("SELECT COUNT(*) FROM core.Employees"))).scalar_one()
    payload = user_payload(
        staged=False,
        role_assignment={"company_role_id": role_id, "scope_type": "AllCompanyBranches"},
        can_login=True,
    )
    created = await client.post("/admin/users", json=payload, headers=auth(auth_token))
    assert created.status_code == 201, created.text
    assert created.json()["is_staged"] is False and created.json()["can_login"] is True
    assert created.json()["company_role_id"] == role_id
    employee_count = (await db_conn.execute(text("SELECT COUNT(*) FROM core.Employees"))).scalar_one()
    assert employee_count == before

    invalid = user_payload(
        staged=False,
        role_assignment={"company_role_id": 999999, "scope_type": "AllCompanyBranches"},
    )
    rejected = await client.post("/admin/users", json=invalid, headers=auth(auth_token))
    assert rejected.status_code == 422
    remains = await db_conn.execute(text("SELECT COUNT(*) FROM sec.Users WHERE Username=:username"), {"username": invalid["username"]})
    assert remains.scalar_one() == 0


@pytest.mark.asyncio
async def test_admin_workforce_audit_failure_rolls_back_account(monkeypatch, client, auth_token, db_conn):
    from app.admin import service as admin_service

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit test failure")

    monkeypatch.setattr(admin_service, "_write_admin_audit", fail_audit)
    payload = user_payload()
    transport = httpx.ASGITransport(app=client._transport.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as isolated:
        response = await isolated.post("/admin/users", json=payload, headers=auth(auth_token))
    assert response.status_code == 500
    rows = await db_conn.execute(text("SELECT COUNT(*) FROM sec.Users WHERE Username=:username"), {"username": payload["username"]})
    assert rows.scalar_one() == 0


@pytest.mark.asyncio
async def test_provisioned_create_requires_user_and_role_assignment_permissions(client, auth_token, db_conn):
    role_response = await client.post("/admin/company-roles", json={"role_name": "P2a Create Only"}, headers=auth(auth_token))
    assert role_response.status_code == 201, role_response.text
    create_only_role = role_response.json()["company_role_id"]
    permissions = await client.put(
        f"/admin/company-roles/{create_only_role}/permissions",
        json={"permission_codes": ["users.create"]}, headers=auth(auth_token),
    )
    assert permissions.status_code == 200, permissions.text

    viewer_role = await company_role(client, auth_token, "PAYROLL_VIEWER_CO")
    caller_payload = user_payload(
        staged=False,
        role_assignment={"company_role_id": create_only_role, "scope_type": "AllCompanyBranches"},
        can_login=True,
    )
    caller_response = await client.post("/admin/users", json=caller_payload, headers=auth(auth_token))
    assert caller_response.status_code == 201, caller_response.text
    caller = caller_response.json()
    login = await client.post("/auth/login", json={
        "username": caller["username"], "password": "TestPass123!", "company_code": "DEMO",
    })
    assert login.status_code == 200, login.text
    caller_token = login.json()["access_token"]
    assert "users.create" in caller["role_permission_codes"]
    assert "users.edit" not in caller["role_permission_codes"]
    assert "roles.edit" not in caller["role_permission_codes"]

    staged_payload = user_payload()
    staged = await client.post("/admin/users", json=staged_payload, headers=auth(caller_token))
    assert staged.status_code == 201, staged.text

    rejected_payload = user_payload(
        staged=False,
        role_assignment={"company_role_id": viewer_role, "scope_type": "AllCompanyBranches"},
    )
    rejected = await client.post("/admin/users", json=rejected_payload, headers=auth(caller_token))
    assert rejected.status_code == 403
    untouched = await db_conn.execute(text("""
        SELECT (SELECT COUNT(*) FROM sec.Users WHERE Username=:username),
               (SELECT COUNT(*) FROM sec.UserBranchRoles ubr
                JOIN sec.Users u ON u.UserID=ubr.UserID
                WHERE u.Username=:username)
    """), {"username": rejected_payload["username"]})
    assert untouched.one() == (0, 0)

    authorized_payload = user_payload(
        staged=False,
        role_assignment={"company_role_id": viewer_role, "scope_type": "AllCompanyBranches"},
    )
    authorized = await client.post("/admin/users", json=authorized_payload, headers=auth(auth_token))
    assert authorized.status_code == 201, authorized.text
    assert authorized.json()["company_role_id"] == viewer_role


@pytest.mark.asyncio
async def test_archived_company_role_rejected_by_all_access_assignment_surfaces(client, auth_token, db_conn):
    created_role = await client.post("/admin/company-roles", json={"role_name": "P2a Archived Role"}, headers=auth(auth_token))
    assert created_role.status_code == 201, created_role.text
    archived_role_id = created_role.json()["company_role_id"]
    archived = await client.delete(f"/admin/company-roles/{archived_role_id}", headers=auth(auth_token))
    assert archived.status_code == 204
    archive_state = await db_conn.execute(
        text("SELECT IsActive, IsArchived FROM sec.CompanyRoles WHERE CompanyRoleID=:rid"),
        {"rid": archived_role_id},
    )
    assert archive_state.one() == (True, True)

    create_payload = user_payload(
        staged=False,
        role_assignment={"company_role_id": archived_role_id, "scope_type": "AllCompanyBranches"},
    )
    rejected_create = await client.post("/admin/users", json=create_payload, headers=auth(auth_token))
    assert rejected_create.status_code == 422
    no_created_user = await db_conn.execute(text("SELECT COUNT(*) FROM sec.Users WHERE Username=:username"), {"username": create_payload["username"]})
    assert no_created_user.scalar_one() == 0

    staged = await staged_user(client, auth_token)
    rejected_provision = await provision(client, auth_token, staged["user_id"], archived_role_id)
    assert rejected_provision.status_code == 422
    staged_state = await db_conn.execute(text("""
        SELECT IsStaged, CanLogin,
               (SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID=:uid AND IsActive)
        FROM sec.Users WHERE UserID=:uid
    """), {"uid": staged["user_id"]})
    assert staged_state.one() == (True, False, 0)

    viewer_role = await company_role(client, auth_token, "PAYROLL_VIEWER_CO")
    assigned = await client.post("/admin/users", json=user_payload(
        staged=False, role_assignment={"company_role_id": viewer_role, "scope_type": "AllCompanyBranches"},
    ), headers=auth(auth_token))
    assert assigned.status_code == 201, assigned.text
    target_id = assigned.json()["user_id"]
    before_assignments = await db_conn.execute(text("""
        SELECT CompanyRoleID FROM sec.UserBranchRoles
        WHERE UserID=:uid AND IsActive ORDER BY UserBranchRoleID
    """), {"uid": target_id})
    assert [row[0] for row in before_assignments.fetchall()] == [viewer_role]
    direct = await client.post(
        f"/admin/users/{target_id}/company-role-assignments",
        json={"company_role_id": archived_role_id, "scope_type": "AllCompanyBranches"},
        headers=auth(auth_token),
    )
    assert direct.status_code == 422
    after_assignments = await db_conn.execute(text("""
        SELECT CompanyRoleID FROM sec.UserBranchRoles
        WHERE UserID=:uid AND IsActive ORDER BY UserBranchRoleID
    """), {"uid": target_id})
    assert [row[0] for row in after_assignments.fetchall()] == [viewer_role]

    owner = await client.get("/admin/users", headers=auth(auth_token))
    owner_id = next(item["user_id"] for item in owner.json() if item["username"] == "admin")
    transfer = await client.post("/admin/company-owner/transfer", json={
        "target_user_id": target_id, "replacement_company_role_id": archived_role_id,
        "confirmation": "TRANSFER",
    }, headers=auth(auth_token))
    assert transfer.status_code == 422
    ownership = await db_conn.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM sec.UserBranchRoles ubr JOIN sec.CompanyRoles cr USING (CompanyRoleID)
           WHERE ubr.UserID=:owner AND ubr.IsActive AND cr.RoleCode='COMPANY_OWNER'),
          (SELECT COUNT(*) FROM sec.UserBranchRoles ubr JOIN sec.CompanyRoles cr USING (CompanyRoleID)
           WHERE ubr.UserID=:target AND ubr.IsActive AND cr.RoleCode='COMPANY_OWNER'),
          (SELECT COUNT(*) FROM sec.UserBranchRoles
           WHERE UserID=:owner AND CompanyRoleID=:archived AND IsActive)
    """), {"owner": owner_id, "target": target_id, "archived": archived_role_id})
    assert ownership.one() == (1, 0, 0)
    target_after = await db_conn.execute(text("""
        SELECT CompanyRoleID FROM sec.UserBranchRoles
        WHERE UserID=:uid AND IsActive ORDER BY UserBranchRoleID
    """), {"uid": target_id})
    assert [row[0] for row in target_after.fetchall()] == [viewer_role]


@pytest.mark.asyncio
async def test_role_archive_waits_for_inflight_provision_assignment(monkeypatch, client, auth_token, db_conn):
    from app.admin import service as admin_service

    created_role = await client.post("/admin/company-roles", json={"role_name": "P2a Concurrent Role"}, headers=auth(auth_token))
    assert created_role.status_code == 201, created_role.text
    role_id = created_role.json()["company_role_id"]
    staged = await staged_user(client, auth_token)

    validation_complete = asyncio.Event()
    allow_assignment_to_continue = asyncio.Event()
    original_validation = admin_service._validate_p2a_assignment

    async def pause_after_validation(company_id, employee_id, assignment, connection):
        result = await original_validation(company_id, employee_id, assignment, connection)
        if assignment.company_role_id == role_id:
            validation_complete.set()
            await asyncio.wait_for(allow_assignment_to_continue.wait(), timeout=5)
        return result

    monkeypatch.setattr(admin_service, "_validate_p2a_assignment", pause_after_validation)
    assignment_task = asyncio.create_task(provision(client, auth_token, staged["user_id"], role_id))
    await asyncio.wait_for(validation_complete.wait(), timeout=5)
    archive_task = asyncio.create_task(client.delete(f"/admin/company-roles/{role_id}", headers=auth(auth_token)))
    await asyncio.sleep(0.1)
    archive_waited = not archive_task.done()

    allow_assignment_to_continue.set()
    assigned, archived = await asyncio.gather(assignment_task, archive_task)
    assert archive_waited, "archive must wait for the assignment's shared CompanyRole lock"
    assert assigned.status_code == 200, assigned.text
    assert archived.status_code == 422, archived.text
    state = await db_conn.execute(text("""
        SELECT cr.IsArchived,
               (SELECT COUNT(*) FROM sec.UserBranchRoles ubr
                WHERE ubr.CompanyRoleID=cr.CompanyRoleID AND ubr.UserID=:uid AND ubr.IsActive)
        FROM sec.CompanyRoles cr WHERE cr.CompanyRoleID=:rid
    """), {"uid": staged["user_id"], "rid": role_id})
    assert state.one() == (False, 1)


@pytest.mark.asyncio
async def test_owner_transfer_rejects_driver_and_final_role_removal(client, auth_token, db_conn):
    viewer_role = await company_role(client, auth_token, "PAYROLL_VIEWER_CO")
    driver_role = await company_role(client, auth_token, "DRIVER")
    target_payload = user_payload(
        staged=False,
        role_assignment={"company_role_id": viewer_role, "scope_type": "AllCompanyBranches"},
        can_login=True,
    )
    target_response = await client.post("/admin/users", json=target_payload, headers=auth(auth_token))
    assert target_response.status_code == 201, target_response.text
    target_id = target_response.json()["user_id"]
    owner_response = await client.get("/admin/users", headers=auth(auth_token))
    owner_id = next(item["user_id"] for item in owner_response.json() if item["username"] == "admin")

    driver_rejected = await client.post("/admin/company-owner/transfer", json={
        "target_user_id": target_id, "replacement_company_role_id": driver_role,
        "confirmation": "TRANSFER",
    }, headers=auth(auth_token))
    assert driver_rejected.status_code == 422
    state = await db_conn.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM sec.UserBranchRoles ubr JOIN sec.CompanyRoles cr USING (CompanyRoleID)
           WHERE ubr.UserID=:owner AND ubr.IsActive AND cr.RoleCode='COMPANY_OWNER'),
          (SELECT COUNT(*) FROM sec.UserBranchRoles ubr JOIN sec.CompanyRoles cr USING (CompanyRoleID)
           WHERE ubr.UserID=:target AND ubr.IsActive AND cr.RoleCode='COMPANY_OWNER'),
          (SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID=:owner AND CompanyRoleID=:driver AND IsActive),
          (SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID=:target AND CompanyRoleID=:viewer AND IsActive)
    """), {"owner": owner_id, "target": target_id, "driver": driver_role, "viewer": viewer_role})
    assert state.one() == (1, 0, 0, 1)

    await db_conn.execute(text("""
        UPDATE sec.UserBranchRoles
        SET IsActive=FALSE, RevokedAtUtc=NOW()
        WHERE UserID=:owner AND CompanyID=1 AND IsActive AND CompanyRoleID IS DISTINCT FROM (
            SELECT CompanyRoleID FROM sec.CompanyRoles WHERE CompanyID=1 AND RoleCode='COMPANY_OWNER'
        )
    """), {"owner": owner_id})
    final_role = await db_conn.execute(text("""
        SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID=:owner AND IsActive
    """), {"owner": owner_id})
    assert final_role.scalar_one() == 1
    no_replacement = await client.post("/admin/company-owner/transfer", json={
        "target_user_id": target_id, "confirmation": "TRANSFER",
    }, headers=auth(auth_token))
    assert no_replacement.status_code == 422
    state_after = await db_conn.execute(text("""
        SELECT
          (SELECT COUNT(*) FROM sec.UserBranchRoles ubr JOIN sec.CompanyRoles cr USING (CompanyRoleID)
           WHERE ubr.UserID=:owner AND ubr.IsActive AND cr.RoleCode='COMPANY_OWNER'),
          (SELECT COUNT(*) FROM sec.UserBranchRoles ubr JOIN sec.CompanyRoles cr USING (CompanyRoleID)
           WHERE ubr.UserID=:target AND ubr.IsActive AND cr.RoleCode='COMPANY_OWNER'),
          (SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID=:target AND CompanyRoleID=:viewer AND IsActive)
    """), {"owner": owner_id, "target": target_id, "viewer": viewer_role})
    assert state_after.one() == (1, 0, 1)
    owner_state = await db_conn.execute(text("SELECT IsStaged, CanLogin FROM sec.Users WHERE UserID=:uid"), {"uid": owner_id})
    assert owner_state.one() == (False, True)


@pytest.mark.asyncio
async def test_disabled_final_owner_is_staged_by_owner_transfer(client, auth_token, db_conn):
    owner_response = await client.get("/admin/users", headers=auth(auth_token))
    owner = next(item for item in owner_response.json() if item["username"] == "admin")
    owner_id = owner["user_id"]
    original_employee_id = owner["employee_id"]
    original_is_staged = owner["is_staged"]
    original_can_login = owner["can_login"]
    employee_row = await employee(client, auth_token)
    linked = await client.put(
        f"/admin/users/{owner_id}/employee-link",
        json={"employee_id": employee_row["employee_id"]}, headers=auth(auth_token),
    )
    assert linked.status_code == 200, linked.text

    owner_assignments = await db_conn.execute(text("""
        SELECT UserBranchRoleID FROM sec.UserBranchRoles
        WHERE UserID=:uid AND CompanyID=1 AND IsActive
    """), {"uid": owner_id})
    original_assignment_ids = [row[0] for row in owner_assignments.fetchall()]
    assert original_assignment_ids
    owner_company_role = await company_role(client, auth_token, "COMPANY_OWNER")
    owner_role_assignment = await db_conn.execute(text("""
        SELECT UserBranchRoleID FROM sec.UserBranchRoles
        WHERE UserID=:uid AND CompanyRoleID=:rid AND IsActive
    """), {"uid": owner_id, "rid": owner_company_role})
    owner_assignment_id = owner_role_assignment.scalar_one()
    target_role = await company_role(client, auth_token, "PAYROLL_VIEWER_CO")
    target_response = await client.post("/admin/users", json=user_payload(
        staged=False,
        role_assignment={"company_role_id": target_role, "scope_type": "AllCompanyBranches"},
        can_login=True,
    ), headers=auth(auth_token))
    assert target_response.status_code == 201, target_response.text
    target_id = target_response.json()["user_id"]
    target_assignment_id = target_response.json()["company_role_assignment_id"]

    try:
        await db_conn.execute(text("""
            UPDATE sec.UserBranchRoles SET IsActive=FALSE, RevokedAtUtc=NOW()
            WHERE UserID=:uid AND CompanyID=1 AND IsActive AND UserBranchRoleID<>:owner_assignment
        """), {"uid": owner_id, "owner_assignment": owner_assignment_id})
        await db_conn.execute(text("UPDATE sec.Users SET CanLogin=FALSE WHERE UserID=:uid"), {"uid": owner_id})

        transferred = await client.post("/admin/company-owner/transfer", json={
            "target_user_id": target_id, "confirmation": "TRANSFER",
        }, headers=auth(auth_token))
        assert transferred.status_code == 200, transferred.text

        state = await db_conn.execute(text("""
            SELECT u.IsStaged, u.CanLogin, u.EmployeeID,
                   (SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID=u.UserID AND IsActive),
                   (SELECT COUNT(*) FROM sec.UserBranchRoles ubr JOIN sec.CompanyRoles cr USING (CompanyRoleID)
                    WHERE ubr.UserID=:target AND ubr.IsActive AND cr.RoleCode='COMPANY_OWNER')
            FROM sec.Users u WHERE u.UserID=:owner
        """), {"owner": owner_id, "target": target_id})
        assert state.one() == (True, False, employee_row["employee_id"], 0, 1)
        staged_audit = await db_conn.execute(text("""
            SELECT COUNT(*) FROM audit.AuditLog
            WHERE EntitySchema='sec' AND EntityName='Users' AND EntityID=:uid AND ActionCode='USER_STAGED'
        """), {"uid": str(owner_id)})
        assert staged_audit.scalar_one() == 1
    finally:
        await db_conn.execute(text("""
            UPDATE sec.UserBranchRoles SET IsActive=FALSE, RevokedAtUtc=NOW()
            WHERE UserID=:target AND IsActive
        """), {"target": target_id})
        await db_conn.execute(text("""
            UPDATE sec.UserBranchRoles SET IsActive=TRUE, RevokedAtUtc=NULL
            WHERE UserBranchRoleID=:assignment_id
        """), {"assignment_id": target_assignment_id})
        for assignment_id in original_assignment_ids:
            await db_conn.execute(text("""
                UPDATE sec.UserBranchRoles SET IsActive=TRUE, RevokedAtUtc=NULL
                WHERE UserBranchRoleID=:assignment_id
            """), {"assignment_id": assignment_id})
        await db_conn.execute(text("""
            UPDATE sec.Users SET IsStaged=:is_staged, CanLogin=:can_login, EmployeeID=:employee_id
            WHERE UserID=:uid
        """), {"uid": owner_id, "is_staged": original_is_staged,
              "can_login": original_can_login, "employee_id": original_employee_id})


@pytest.mark.asyncio
async def test_provision_audit_failure_rolls_back_assignment_and_account_state(monkeypatch, client, auth_token, db_conn):
    from app.admin import service as admin_service

    staged = await staged_user(client, auth_token)
    role_id = await company_role(client, auth_token, "PAYROLL_VIEWER_CO")
    original_writer = admin_service._write_admin_audit

    async def fail_provision_audit(*args, **kwargs):
        if kwargs.get("action_code") == "USER_PROVISIONED":
            raise RuntimeError("provision audit failure")
        await original_writer(*args, **kwargs)

    monkeypatch.setattr(admin_service, "_write_admin_audit", fail_provision_audit)
    transport = httpx.ASGITransport(app=client._transport.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as isolated:
        response = await provision(isolated, auth_token, staged["user_id"], role_id, can_login=True)
    assert response.status_code == 500
    state = await db_conn.execute(text("""
        SELECT IsStaged, CanLogin,
               (SELECT COUNT(*) FROM sec.UserBranchRoles WHERE UserID=:uid AND IsActive)
        FROM sec.Users WHERE UserID=:uid
    """), {"uid": staged["user_id"]})
    assert state.one() == (True, False, 0)
