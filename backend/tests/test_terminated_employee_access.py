"""A terminated Employee's sign-in and access end with the termination.

Termination sets the Employee to Terminated immediately, whatever its date, so the linked
account loses login, role assignments and extra permissions in the same transaction, and a
terminated Employee can no longer be linked to, or given access through, an account.
"""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from tests.builders.access import create_actor_with_permissions, create_provisioned_test_user
from tests.builders.workforce import create_driver_employee_record

pytestmark = pytest.mark.asyncio

PASSWORD = "TestPass123!"


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def tag() -> str:
    return uuid4().hex[:8]


async def role_id(client: httpx.AsyncClient, token: str, code: str) -> int:
    response = await client.get("/admin/company-roles", headers=hdr(token))
    assert response.status_code == 200, response.text
    return next(r["company_role_id"] for r in response.json() if r["role_code"] == code)


async def staff_employee(client: httpx.AsyncClient, token: str) -> dict:
    response = await client.post(
        "/workforce/employees",
        json={"branch_id": 1, "full_name": f"Safety Staff {tag()}", "hire_date": "2000-01-01"},
        headers=hdr(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def with_login(client: httpx.AsyncClient, token: str, employee_id: int, extras=()) -> dict:
    """A signed-in-capable account linked to the Employee, optionally with extra permissions."""
    username = f"term_{tag()}"
    user = await create_provisioned_test_user(
        client, token, username, await role_id(client, token, "PAYROLL_VIEWER_CO"),
        employee_id=employee_id, password=PASSWORD,
    )
    if extras:
        granted = await client.put(
            f"/admin/users/{user['user_id']}/permission-overrides",
            json={"permission_codes": list(extras)}, headers=hdr(token),
        )
        assert granted.status_code == 200, granted.text
    login = await client.post(
        "/auth/login", json={"username": username, "password": PASSWORD, "company_code": "DEMO"},
    )
    assert login.status_code == 200, login.text
    return {**user, "username": username, "token": login.json()["access_token"]}


async def terminate_staff(client: httpx.AsyncClient, token: str, employee_id: int, day="2025-05-31"):
    return await client.patch(
        f"/workforce/employees/{employee_id}",
        json={"employment_status": "Terminated", "termination_date": day},
        headers=hdr(token),
    )


async def account_state(db, user_id: int) -> dict:
    user = (await db.execute(text(
        "SELECT isactive, canlogin, isstaged FROM sec.users WHERE userid = :uid"
    ), {"uid": user_id})).mappings().one()
    roles = (await db.execute(text(
        "SELECT COUNT(*) FROM sec.userbranchroles WHERE userid = :uid AND isactive"
    ), {"uid": user_id})).scalar_one()
    revoked_roles = (await db.execute(text(
        "SELECT COUNT(*) FROM sec.userbranchroles"
        " WHERE userid = :uid AND NOT isactive AND revokedatutc IS NOT NULL"
    ), {"uid": user_id})).scalar_one()
    overrides = (await db.execute(text(
        "SELECT COUNT(*) FROM sec.userpermissionoverrides WHERE userid = :uid AND isactive"
    ), {"uid": user_id})).scalar_one()
    revoked_overrides = (await db.execute(text(
        "SELECT COUNT(*) FROM sec.userpermissionoverrides"
        " WHERE userid = :uid AND NOT isactive AND revokedatutc IS NOT NULL"
    ), {"uid": user_id})).scalar_one()
    return {**user, "active_roles": roles, "revoked_roles": revoked_roles,
            "active_overrides": overrides, "revoked_overrides": revoked_overrides}


async def audit_actions(db, user_id: int) -> set[str]:
    rows = (await db.execute(text("""
        SELECT actioncode FROM audit.auditlog
        WHERE entityid = :uid AND entityname IN ('Users', 'UserPermissionOverrides')
    """), {"uid": str(user_id)})).scalars().all()
    return set(rows)


async def assert_cannot_sign_in(client: httpx.AsyncClient, account: dict) -> None:
    login = await client.post(
        "/auth/login",
        json={"username": account["username"], "password": PASSWORD, "company_code": "DEMO"},
    )
    assert login.status_code in (401, 403), login.text
    session = await client.get("/auth/me", headers=hdr(account["token"]))
    assert session.status_code in (401, 403), session.text


async def test_terminating_an_employee_ends_login_roles_and_extra_permissions(client, auth_token, db_conn):
    employee = await staff_employee(client, auth_token)
    account = await with_login(client, auth_token, employee["employee_id"], extras=["payroll.view", "reports.view"])

    terminated = await terminate_staff(client, auth_token, employee["employee_id"])

    assert terminated.status_code == 200, terminated.text
    state = await account_state(db_conn, account["user_id"])
    assert state["canlogin"] is False
    assert state["isactive"] is False
    assert state["isstaged"] is True
    assert state["active_roles"] == 0 and state["revoked_roles"] == 1
    assert state["active_overrides"] == 0 and state["revoked_overrides"] == 2
    await assert_cannot_sign_in(client, account)


async def test_ending_access_on_termination_is_audited(client, auth_token, db_conn):
    employee = await staff_employee(client, auth_token)
    account = await with_login(client, auth_token, employee["employee_id"], extras=["reports.view"])

    terminated = await terminate_staff(client, auth_token, employee["employee_id"])

    assert terminated.status_code == 200, terminated.text
    assert {
        "USER_ACCESS_ENDED_BY_TERMINATION", "USER_PERMISSION_OVERRIDES_SET",
    } <= await audit_actions(db_conn, account["user_id"])
    revoked = (await db_conn.execute(text("""
        SELECT COUNT(*) FROM audit.auditlog
        WHERE actioncode = 'COMPANY_ROLE_REVOKED' AND oldvaluejson::jsonb ->> 'target_user_id' = :uid
    """), {"uid": str(account["user_id"])})).scalar_one()
    assert revoked == 1


async def test_terminating_a_driver_ends_the_drivers_login_and_role(client, auth_token, db_conn):
    employee = await create_driver_employee_record(
        client, auth_token, branch_id=1, full_name=f"Safety Driver {tag()}",
        driver_number=f"SAF-{tag()}", hire_date="2000-01-01",
        effective_from_at="2000-01-01T00:00:00+00:00",
    )
    username = f"drv_{tag()}"
    account = await create_provisioned_test_user(
        client, auth_token, username, await role_id(client, auth_token, "DRIVER"),
        scope_type="Self", employee_id=employee["employee_id"], password=PASSWORD,
    )
    login = await client.post(
        "/auth/login", json={"username": username, "password": PASSWORD, "company_code": "DEMO"},
    )
    assert login.status_code == 200, login.text
    yesterday = (await db_conn.execute(text("SELECT CURRENT_DATE - 1"))).scalar_one()

    terminated = await client.post(
        f"/workforce/employees/{employee['employee_id']}/terminate",
        json={"termination_date": yesterday.isoformat(), "reason": "Left the company"},
        headers=hdr(auth_token),
    )

    assert terminated.status_code == 200, terminated.text
    state = await account_state(db_conn, account["user_id"])
    assert (state["canlogin"], state["isactive"], state["active_roles"]) == (False, False, 0)
    await assert_cannot_sign_in(
        client, {"username": username, "token": login.json()["access_token"]},
    )
    assert "USER_ACCESS_ENDED_BY_TERMINATION" in await audit_actions(db_conn, account["user_id"])


async def test_a_future_dated_termination_ends_access_straight_away(client, auth_token, db_conn):
    employee = await create_driver_employee_record(
        client, auth_token, branch_id=1, full_name=f"Safety Later {tag()}",
        driver_number=f"SAF-{tag()}", hire_date="2000-01-01",
        effective_from_at="2000-01-01T00:00:00+00:00",
    )
    account = await with_login(client, auth_token, employee["employee_id"])
    later = (await db_conn.execute(text("SELECT CURRENT_DATE + 30"))).scalar_one()

    terminated = await client.post(
        f"/workforce/employees/{employee['employee_id']}/terminate",
        json={"termination_date": later.isoformat(), "reason": "Notice served"},
        headers=hdr(auth_token),
    )

    assert terminated.status_code == 200, terminated.text
    assert terminated.json()["employment_status"] == "Terminated"
    assert (await account_state(db_conn, account["user_id"]))["canlogin"] is False


async def test_terminating_an_employee_without_an_account_still_works(client, auth_token):
    employee = await staff_employee(client, auth_token)

    terminated = await terminate_staff(client, auth_token, employee["employee_id"])

    assert terminated.status_code == 200, terminated.text


async def test_other_people_keep_their_access_when_someone_is_terminated(client, auth_token, db_conn):
    leaving = await staff_employee(client, auth_token)
    staying = await staff_employee(client, auth_token)
    leaver = await with_login(client, auth_token, leaving["employee_id"])
    stayer = await with_login(client, auth_token, staying["employee_id"], extras=["reports.view"])

    await terminate_staff(client, auth_token, leaving["employee_id"])

    state = await account_state(db_conn, stayer["user_id"])
    assert (state["canlogin"], state["isactive"], state["active_roles"], state["active_overrides"]) == (
        True, True, 1, 1)
    assert (await client.get("/auth/me", headers=hdr(stayer["token"]))).status_code == 200
    assert (await account_state(db_conn, leaver["user_id"]))["canlogin"] is False


async def test_the_company_owners_employee_cannot_be_terminated(client, auth_token, db_conn):
    employee = await staff_employee(client, auth_token)
    me = await client.get("/auth/me", headers=hdr(auth_token))
    owner_id = me.json()["user_id"]
    linked = await client.put(
        f"/admin/users/{owner_id}/employee-link",
        json={"employee_id": employee["employee_id"]}, headers=hdr(auth_token),
    )
    assert linked.status_code == 200, linked.text
    try:
        refused = await terminate_staff(client, auth_token, employee["employee_id"])
    finally:
        await client.delete(f"/admin/users/{owner_id}/employee-link", headers=hdr(auth_token))

    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "COMPANY_OWNER_TERMINATION_FORBIDDEN"
    status = (await db_conn.execute(text(
        "SELECT employmentstatus FROM core.employees WHERE employeeid = :id"
    ), {"id": employee["employee_id"]})).scalar_one()
    assert status == "Active"
    assert (await client.get("/auth/me", headers=hdr(auth_token))).status_code == 200


async def test_nobody_can_terminate_their_own_employee_record(client, auth_token, db_conn):
    employee = await staff_employee(client, auth_token)
    actor = await create_actor_with_permissions(
        client, auth_token, f"self_term_{tag()}",
        ["employees.view", "employees.manage", "users.view", "users.edit"],
    )
    linked = await client.put(
        f"/admin/users/{actor['user_id']}/employee-link",
        json={"employee_id": employee["employee_id"]}, headers=hdr(auth_token),
    )
    assert linked.status_code == 200, linked.text

    refused = await terminate_staff(client, actor["token"], employee["employee_id"])

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "OWN_EMPLOYEE_TERMINATION_FORBIDDEN"
    assert (await account_state(db_conn, actor["user_id"]))["canlogin"] is True


async def test_a_terminated_employee_cannot_be_linked_to_an_account(client, auth_token):
    employee = await staff_employee(client, auth_token)
    assert (await terminate_staff(client, auth_token, employee["employee_id"])).status_code == 200
    staged = await client.post(
        "/admin/users",
        json={"username": f"stg_{tag()}", "display_name": "Staged", "password": PASSWORD,
              "is_active": True, "is_staged": True, "can_login": False},
        headers=hdr(auth_token),
    )
    assert staged.status_code == 201, staged.text

    linked = await client.put(
        f"/admin/users/{staged.json()['user_id']}/employee-link",
        json={"employee_id": employee["employee_id"]}, headers=hdr(auth_token),
    )
    created = await client.post(
        "/admin/users",
        json={"username": f"stg_{tag()}", "display_name": "Staged", "password": PASSWORD,
              "is_active": True, "is_staged": True, "can_login": False,
              "employee_id": employee["employee_id"]},
        headers=hdr(auth_token),
    )

    for response in (linked, created):
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "EMPLOYEE_TERMINATED"


async def test_a_terminated_employees_account_cannot_be_provisioned_again(client, auth_token):
    employee = await staff_employee(client, auth_token)
    account = await with_login(client, auth_token, employee["employee_id"])
    assert (await terminate_staff(client, auth_token, employee["employee_id"])).status_code == 200

    provisioned = await client.post(
        f"/admin/users/{account['user_id']}/provision",
        json={"role_assignment": {
            "company_role_id": await role_id(client, auth_token, "PAYROLL_VIEWER_CO"),
            "scope_type": "AllCompanyBranches"}, "can_login": True},
        headers=hdr(auth_token),
    )

    assert provisioned.status_code == 422, provisioned.text
    assert provisioned.json()["detail"]["code"] == "EMPLOYEE_TERMINATED"


async def test_access_cannot_be_given_to_an_account_whose_employee_is_terminated(client, auth_token, db_conn):
    # Accounts left behind by an earlier termination (before access ended with it) still hold
    # their role and login, so the admin endpoints must refuse to extend or re-enable them.
    employee = await staff_employee(client, auth_token)
    account = await with_login(client, auth_token, employee["employee_id"])
    await db_conn.execute(text(
        "UPDATE core.employees SET employmentstatus = 'Terminated', terminationdate = '2025-05-31'"
        " WHERE employeeid = :id"
    ), {"id": employee["employee_id"]})
    await db_conn.execute(text("UPDATE sec.users SET canlogin = FALSE WHERE userid = :uid"),
                          {"uid": account["user_id"]})
    other_role = await role_id(client, auth_token, "PAYROLL_VIEWER_CO")

    assigned = await client.post(
        f"/admin/users/{account['user_id']}/company-role-assignments",
        json={"company_role_id": other_role, "scope_type": "AllCompanyBranches"},
        headers=hdr(auth_token),
    )
    enabled = await client.patch(
        f"/admin/users/{account['user_id']}", json={"can_login": True}, headers=hdr(auth_token),
    )
    extras = await client.put(
        f"/admin/users/{account['user_id']}/permission-overrides",
        json={"permission_codes": ["reports.view"]}, headers=hdr(auth_token),
    )

    for response in (assigned, enabled, extras):
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "EMPLOYEE_TERMINATED"


async def test_a_terminated_employees_account_cannot_be_unlinked(client, auth_token, db_conn):
    employee = await staff_employee(client, auth_token)
    account = await with_login(client, auth_token, employee["employee_id"])
    assert (await terminate_staff(client, auth_token, employee["employee_id"])).status_code == 200

    unlinked = await client.delete(f"/admin/users/{account['user_id']}/employee-link", headers=hdr(auth_token))

    assert unlinked.status_code == 422, unlinked.text
    assert unlinked.json()["detail"]["code"] == "EMPLOYEE_TERMINATED"
    linked_to = (await db_conn.execute(text(
        "SELECT employeeid FROM sec.users WHERE userid = :uid"
    ), {"uid": account["user_id"]})).scalar_one()
    assert linked_to == employee["employee_id"]


async def test_a_terminated_employees_account_cannot_be_moved_to_another_employee(client, auth_token):
    employee = await staff_employee(client, auth_token)
    account = await with_login(client, auth_token, employee["employee_id"])
    other = await staff_employee(client, auth_token)
    assert (await terminate_staff(client, auth_token, employee["employee_id"])).status_code == 200

    moved = await client.put(
        f"/admin/users/{account['user_id']}/employee-link",
        json={"employee_id": other["employee_id"]}, headers=hdr(auth_token),
    )

    assert moved.status_code == 409, moved.text


async def test_an_active_employees_account_can_still_be_unlinked(client, auth_token):
    employee = await staff_employee(client, auth_token)
    account = await with_login(client, auth_token, employee["employee_id"])

    unlinked = await client.delete(f"/admin/users/{account['user_id']}/employee-link", headers=hdr(auth_token))

    assert unlinked.status_code == 200, unlinked.text
    assert unlinked.json()["employee_id"] is None
