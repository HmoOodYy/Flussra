"""
Integration tests for M10 — Users & Permissions admin API.

Endpoints tested
----------------
GET    /admin/users
GET    /admin/users/{user_id}
POST   /admin/users
PATCH  /admin/users/{user_id}
POST   /admin/users/{user_id}/reset-password
GET    /admin/users/{user_id}/company-role-assignments
POST   /admin/users/{user_id}/company-role-assignments
DELETE /admin/users/{user_id}/company-role-assignments/{assignment_id}
GET    /admin/company-roles
GET    /admin/permissions

Users tested
------------
admin        AllCompanyBranches scope — full admin access
branch_user  SpecificBranch=HQ scope — no admin access, every endpoint must 403

Test classes
------------
TestListUsers        — returns seeded users, include_inactive flag, scope guard
TestGetUser          — 200, 404, 403
TestCreateUser       — full/minimal create, dup username, short password, scope guard
TestUpdateUser       — patch fields, self-deactivation blocked, scope guard
TestResetPassword    — resets hash, clears lock, new password works, scope guard
TestLegacyRoleRoutesRetired — retired global-role APIs are not mounted
TestListPermissions  — returns seeded permissions, scope guard
TestAdminAudit       — rollback when audit fails on create_user and CompanyRole assignment
"""
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import text
import pytest_asyncio

from app.admin import audit as admin_audit


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Session-scoped fixture: a dedicated target user for update / password tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def target_user_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
) -> int:
    """Create a test user that update/password/role tests operate on."""
    company_roles = await session_client.get("/admin/company-roles", headers=auth(auth_token))
    viewer_role_id = next(role["company_role_id"] for role in company_roles.json() if role["role_code"] == "PAYROLL_VIEWER_CO")
    resp = await session_client.post(
        "/admin/users",
        json={
            "username":     "target_user",
            "display_name": "Target User",
            "password":     "TestPass123!",
            "email":        "target@example.com",
            "role_assignment": {"company_role_id": viewer_role_id, "scope_type": "AllCompanyBranches"},
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"target_user seed failed: {resp.text}"
    return resp.json()["user_id"]


# ===========================================================================
# TestListUsers
# ===========================================================================

class TestListUsers:

    async def test_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.get("/admin/users")
        assert resp.status_code == 401

    async def test_branch_user_denied(
        self, client: httpx.AsyncClient, branch_user_token: str
    ):
        resp = await client.get("/admin/users", headers=auth(branch_user_token))
        assert resp.status_code == 403

    async def test_returns_seeded_users(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get("/admin/users", headers=auth(auth_token))
        assert resp.status_code == 200
        usernames = {u["username"] for u in resp.json()}
        # admin and branch_user are seeded in conftest
        assert "admin" in usernames
        assert "branch_user" in usernames

    async def test_default_excludes_inactive(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get("/admin/users", headers=auth(auth_token))
        assert resp.status_code == 200
        for user in resp.json():
            assert user["is_active"] is True

    async def test_include_inactive_flag(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        # Deactivate the target user first
        await client.patch(
            f"/admin/users/{target_user_id}",
            json={"is_active": False},
            headers=auth(auth_token),
        )

        # Default list must exclude it
        active_resp = await client.get("/admin/users", headers=auth(auth_token))
        active_ids = [u["user_id"] for u in active_resp.json()]
        assert target_user_id not in active_ids

        # With flag, must include it
        all_resp = await client.get(
            "/admin/users?include_inactive=true", headers=auth(auth_token)
        )
        all_ids = [u["user_id"] for u in all_resp.json()]
        assert target_user_id in all_ids

        # Restore active state for subsequent tests
        await client.patch(
            f"/admin/users/{target_user_id}",
            json={"is_active": True},
            headers=auth(auth_token),
        )

    async def test_response_includes_role_assignments(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get("/admin/users", headers=auth(auth_token))
        assert resp.status_code == 200
        admin_entry = next(u for u in resp.json() if u["username"] == "admin")
        assert admin_entry["company_role_code"] == "COMPANY_OWNER"
        assert admin_entry["company_role_assignment_id"] is not None


# ===========================================================================
# TestGetUser
# ===========================================================================

class TestGetUser:

    async def test_requires_auth(
        self, client: httpx.AsyncClient, target_user_id: int
    ):
        resp = await client.get(f"/admin/users/{target_user_id}")
        assert resp.status_code == 401

    async def test_branch_user_denied(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        target_user_id: int,
    ):
        resp = await client.get(
            f"/admin/users/{target_user_id}", headers=auth(branch_user_token)
        )
        assert resp.status_code == 403

    async def test_returns_correct_user(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        resp = await client.get(
            f"/admin/users/{target_user_id}", headers=auth(auth_token)
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"]      == target_user_id
        assert data["username"]     == "target_user"
        assert data["display_name"] == "Target User"
        assert data["email"]        == "target@example.com"
        assert data["company_role_assignment_id"] is not None
        assert data["company_role_code"] == "PAYROLL_VIEWER_CO"

    async def test_schema_complete(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        resp = await client.get(
            f"/admin/users/{target_user_id}", headers=auth(auth_token)
        )
        data = resp.json()
        for field in (
            "user_id", "company_id", "username", "display_name",
            "is_active", "can_login", "must_change_password",
            "created_at_utc", "company_role_assignment_id", "company_role_id",
            "company_role_code", "company_role_scope",
        ):
            assert field in data, f"Missing field: {field}"

    async def test_not_found(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.get("/admin/users/9999999", headers=auth(auth_token))
        assert resp.status_code == 404


# ===========================================================================
# TestCreateUser
# ===========================================================================

class TestCreateUser:

    async def test_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/admin/users",
            json={"username": "x", "display_name": "X", "password": "Pass1234!"},
        )
        assert resp.status_code == 401

    async def test_branch_user_denied(
        self, client: httpx.AsyncClient, branch_user_token: str
    ):
        resp = await client.post(
            "/admin/users",
            json={"username": "y", "display_name": "Y", "password": "Pass1234!"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_create_minimal(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.post(
            "/admin/users",
            json={
                "username":     "newuser_minimal",
                "display_name": "New Minimal User",
                "password":     "Minimal123!",
                "is_staged":    True,
                "can_login":    False,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["username"]            == "newuser_minimal"
        assert data["display_name"]        == "New Minimal User"
        assert data["is_active"]           is True
        assert data["can_login"]           is False
        assert data["is_staged"]            is True
        assert data["must_change_password"] is True   # default
        assert data["email"]               is None
        assert data["company_role_assignment_id"] is None
        assert data["company_role_id"] is None

    async def test_create_with_all_fields(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.post(
            "/admin/users",
            json={
                "username":             "newuser_full",
                "display_name":         "New Full User",
                "password":             "FullPass123!",
                "email":                "full@example.com",
                "phone":                "+1-555-0100",
                "is_active":            True,
                "can_login":            True,
                "must_change_password": False,
                "role_assignment": {"company_role_id": next(r["company_role_id"] for r in (await client.get("/admin/company-roles", headers=auth(auth_token))).json() if r["role_code"] == "PAYROLL_VIEWER_CO"), "scope_type": "AllCompanyBranches"},
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"]               == "full@example.com"
        assert data["phone"]               == "+1-555-0100"
        assert data["must_change_password"] is False

    async def test_duplicate_username_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Creating a second user with the same username must return 422."""
        resp = await client.post(
            "/admin/users",
            json={
                "username":     "newuser_minimal",   # already created above
                "display_name": "Dup User",
                "password":     "Pass1234!",
                "is_staged":    True,
                "can_login":    False,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "already exists" in resp.json()["detail"]

    async def test_short_password_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.post(
            "/admin/users",
            json={"username": "shortpw", "display_name": "Short", "password": "abc"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_blank_username_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.post(
            "/admin/users",
            json={"username": "   ", "display_name": "Blank", "password": "Pass1234!"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_blank_display_name_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.post(
            "/admin/users",
            json={"username": "someuser99", "display_name": "  ", "password": "Pass1234!"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422


# ===========================================================================
# TestUpdateUser
# ===========================================================================

class TestUpdateUser:

    async def test_requires_auth(
        self, client: httpx.AsyncClient, target_user_id: int
    ):
        resp = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"display_name": "Changed"},
        )
        assert resp.status_code == 401

    async def test_branch_user_denied(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        target_user_id: int,
    ):
        resp = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"display_name": "Changed"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_patch_display_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        resp = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"display_name": "Updated Name"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Updated Name"

    async def test_patch_email_and_phone(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        resp = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"email": "newemail@example.com", "phone": "+1-555-9999"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "newemail@example.com"
        assert resp.json()["phone"] == "+1-555-9999"

    async def test_cannot_deactivate_self(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """The calling admin cannot deactivate their own account."""
        # Get the admin's own user_id from /auth/me
        me = await client.get("/auth/me", headers=auth(auth_token))
        admin_uid = me.json()["user_id"]

        resp = await client.patch(
            f"/admin/users/{admin_uid}",
            json={"is_active": False},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "deactivate" in resp.json()["detail"].lower()

    async def test_cannot_remove_own_login(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        me = await client.get("/auth/me", headers=auth(auth_token))
        admin_uid = me.json()["user_id"]

        resp = await client.patch(
            f"/admin/users/{admin_uid}",
            json={"can_login": False},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "login" in resp.json()["detail"].lower()

    async def test_patch_display_name_leaves_email_phone_unchanged(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        """Omitting email/phone while updating display_name must not clear them."""
        # First ensure the user has known email/phone values.
        setup = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"email": "keep@example.com", "phone": "+1-555-0001"},
            headers=auth(auth_token),
        )
        assert setup.status_code == 200

        # Now patch only display_name — email and phone must be unchanged.
        resp = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"display_name": "Name Only Change"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["display_name"] == "Name Only Change"
        assert data["email"] == "keep@example.com"
        assert data["phone"] == "+1-555-0001"

    async def test_clear_email_with_null(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        """Explicitly sending email: null must clear the email column."""
        # Ensure a value exists first.
        setup = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"email": "will_be_cleared@example.com"},
            headers=auth(auth_token),
        )
        assert setup.status_code == 200
        assert setup.json()["email"] == "will_be_cleared@example.com"

        # Clear it.
        resp = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"email": None},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["email"] is None

    async def test_clear_phone_with_null(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        """Explicitly sending phone: null must clear the phone column."""
        setup = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"phone": "+1-555-9999"},
            headers=auth(auth_token),
        )
        assert setup.status_code == 200
        assert setup.json()["phone"] == "+1-555-9999"

        resp = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"phone": None},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["phone"] is None

    async def test_omit_email_leaves_existing_unchanged(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        """Omitting email from the PATCH body must not change the stored value."""
        setup = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"email": "persist@example.com"},
            headers=auth(auth_token),
        )
        assert setup.status_code == 200

        # Patch only phone — email must be untouched.
        resp = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"phone": "+1-555-7777"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "persist@example.com"
        assert data["phone"] == "+1-555-7777"

    async def test_omit_phone_leaves_existing_unchanged(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        """Omitting phone from the PATCH body must not change the stored value."""
        setup = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"phone": "+1-555-3333"},
            headers=auth(auth_token),
        )
        assert setup.status_code == 200

        # Patch only display_name — phone must be untouched.
        resp = await client.patch(
            f"/admin/users/{target_user_id}",
            json={"display_name": "Phone Persist Test"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["phone"] == "+1-555-3333"

    async def test_not_found(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.patch(
            "/admin/users/9999999",
            json={"display_name": "Ghost"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404


# ===========================================================================
# TestResetPassword
# ===========================================================================

class TestResetPassword:

    async def test_requires_auth(
        self, client: httpx.AsyncClient, target_user_id: int
    ):
        resp = await client.post(
            f"/admin/users/{target_user_id}/reset-password",
            json={"new_password": "NewPass123!"},
        )
        assert resp.status_code == 401

    async def test_branch_user_denied(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        target_user_id: int,
    ):
        resp = await client.post(
            f"/admin/users/{target_user_id}/reset-password",
            json={"new_password": "NewPass123!"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_reset_sets_must_change(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        resp = await client.post(
            f"/admin/users/{target_user_id}/reset-password",
            json={"new_password": "ResetPass123!", "must_change_password": True},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["must_change_password"] is True

    async def test_new_password_works_for_login(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        After a password reset the user can log in with the new password.

        Uses a dedicated isolated user (not target_user_id) to avoid
        conflicting with TestRoleAssignments which also operates on target_user.

        The provisioned account is created with its CompanyRole assignment.
        """
        # Create a user dedicated to this test
        create_resp = await client.post(
            "/admin/users",
            json={
                "username":     "pw_reset_login_test",
                "display_name": "PW Reset Login Test",
                "password":     "InitialPass1!",
                "can_login":    True,
                "role_assignment": {
                    "company_role_id": next(
                        role["company_role_id"] for role in (await client.get("/admin/company-roles", headers=auth(auth_token))).json()
                        if role["role_code"] == "PAYROLL_VIEWER_CO"
                    ),
                    "scope_type": "AllCompanyBranches",
                },
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        new_user_id = create_resp.json()["user_id"]

        # Reset the password
        new_pw = "AfterReset789!"
        await client.post(
            f"/admin/users/{new_user_id}/reset-password",
            json={"new_password": new_pw, "must_change_password": False},
            headers=auth(auth_token),
        )

        # Login should now succeed with the new password
        login_resp = await client.post("/auth/login", json={
            "username":     "pw_reset_login_test",
            "password":     new_pw,
            "company_code": "DEMO",
        })
        assert login_resp.status_code == 200
        assert "access_token" in login_resp.json()

    async def test_short_password_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
    ):
        resp = await client.post(
            f"/admin/users/{target_user_id}/reset-password",
            json={"new_password": "short"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_not_found(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.post(
            "/admin/users/9999999/reset-password",
            json={"new_password": "ValidPass123!"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404


# ===========================================================================
# TestRoleAssignments
# ===========================================================================

class TestLegacyRoleRoutesRetired:

    async def test_global_role_routes_are_not_mounted(self, client, auth_token, target_user_id):
        headers = auth(auth_token)
        responses = [
            await client.get("/admin/roles", headers=headers),
            await client.get(f"/admin/users/{target_user_id}/roles", headers=headers),
            await client.post(f"/admin/users/{target_user_id}/roles", json={}, headers=headers),
            await client.delete(f"/admin/users/{target_user_id}/roles/1", headers=headers),
        ]
        assert [response.status_code for response in responses] == [404, 404, 404, 404]
class TestListPermissions:

    async def test_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.get("/admin/permissions")
        assert resp.status_code == 401

    async def test_branch_user_denied(
        self, client: httpx.AsyncClient, branch_user_token: str
    ):
        resp = await client.get("/admin/permissions", headers=auth(branch_user_token))
        assert resp.status_code == 403

    async def test_returns_seeded_permissions(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        # Default (ui_only=true) returns permissions whose modulecode is in the
        # UI-visible set: company, roles, users, payroll, payitems, payrates,
        # drivers, dispatch, reports, settings, payroll_setup, review.
        # payroll.entry (modulecode='payroll') is visible.
        # review.decide (modulecode='review') is visible — 'review' was added
        # to the ui_only filter so the Roles UI can assign it to custom roles.
        # True legacy-only codes (setup.manage, drivers.manage, payroll.approve_rate)
        # remain available only via ui_only=false.
        resp_ui = await client.get("/admin/permissions", headers=auth(auth_token))
        assert resp_ui.status_code == 200
        ui_codes = {p["permission_code"] for p in resp_ui.json()}
        # Core modern codes
        assert "payroll.view"          in ui_codes
        assert "payroll.finalize"      in ui_codes
        assert "settings.manage"       in ui_codes
        assert {
            "payroll_setup.view", "payroll_setup.manage",
            "payroll_setup.publish", "payroll_setup.assign",
        } <= ui_codes
        # Enforced codes now catalogued and visible to the Roles UI
        assert "payroll.entry"         in ui_codes, (
            "payroll.entry must be visible (migration 0041, modulecode='payroll')"
        )
        assert "payroll.period.create" in ui_codes, (
            "payroll.period.create must be visible (migration 0030)"
        )
        assert "review.decide"         in ui_codes, (
            "review.decide must be visible ('review' added to ui_only filter)"
        )

        # All codes including review module and legacy codes
        resp_all = await client.get("/admin/permissions?ui_only=false", headers=auth(auth_token))
        assert resp_all.status_code == 200
        all_codes = {p["permission_code"] for p in resp_all.json()}
        assert "payroll.entry"  in all_codes
        assert "review.decide"  in all_codes
        assert "setup.manage"   in all_codes

    async def test_grouped_by_module(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.get("/admin/permissions", headers=auth(auth_token))
        assert resp.status_code == 200
        modules = {p["module_code"] for p in resp.json()}
        assert "payroll"  in modules
        assert "settings" in modules

    async def test_schema(self, client: httpx.AsyncClient, auth_token: str):
        resp = await client.get("/admin/permissions", headers=auth(auth_token))
        assert resp.status_code == 200
        perm = resp.json()[0]
        for field in ("permission_id", "permission_code", "permission_name", "module_code"):
            assert field in perm, f"Missing field: {field}"


# ===========================================================================
# TestAdminAudit — rollback verification
# ===========================================================================

class TestAdminAudit:

    async def test_create_user_rolls_back_when_audit_fails(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        If write_admin_audit raises during create_user(), the INSERT into
        sec.users must roll back.  The username must not exist afterwards.
        """
        unique_name = "audit_rollback_user"

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on create_user")

        with patch.object(admin_audit, "write_admin_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure on create_user"):
                await client.post(
                    "/admin/users",
                    json={
                        "username":     unique_name,
                        "display_name": "Audit Rollback Test",
                        "password":     "RollbackPass123!",
                        "is_staged":    True,
                        "can_login":    False,
                    },
                    headers=auth(auth_token),
                )

        # Verify the user was NOT committed
        list_resp = await client.get(
            "/admin/users?include_inactive=true",
            headers=auth(auth_token),
        )
        usernames = {u["username"] for u in list_resp.json()}
        assert unique_name not in usernames

    async def test_company_role_assignment_rolls_back_when_audit_fails(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        target_user_id: int,
        db_conn,
    ):
        """
        If write_admin_audit raises during CompanyRole assignment, the INSERT into
        sec.userbranchroles must roll back.
        """
        roles_resp = await client.get("/admin/company-roles", headers=auth(auth_token))
        viewer_id = next(
            r["company_role_id"] for r in roles_resp.json()
            if r["role_code"] == "PAYROLL_VIEWER_CO"
        )

        # Get the PAYTEST branch
        branches_resp = await client.get("/core/branches", headers=auth(auth_token))
        branch_id = next(
            b["branch_id"] for b in branches_resp.json() if b["branch_code"] == "PAYTEST"
        )

        before = (await db_conn.execute(text("""
            SELECT COUNT(*) FROM sec.UserBranchRoles
            WHERE UserID=:uid AND CompanyRoleID=:role_id
              AND BranchID=:branch_id AND IsActive
        """), {"uid": target_user_id, "role_id": viewer_id, "branch_id": branch_id})).scalar_one()

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on CompanyRole assignment")

        with patch.object(admin_audit, "write_admin_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure on CompanyRole assignment"):
                await client.post(
                    f"/admin/users/{target_user_id}/company-role-assignments",
                    json={
                        "company_role_id": viewer_id,
                        "scope_type": "SpecificBranch",
                        "branch_id":  branch_id,
                        "notes":      "audit rollback test",
                    },
                    headers=auth(auth_token),
                )

        after = (await db_conn.execute(text("""
            SELECT COUNT(*) FROM sec.UserBranchRoles
            WHERE UserID=:uid AND CompanyRoleID=:role_id
              AND BranchID=:branch_id AND IsActive
        """), {"uid": target_user_id, "role_id": viewer_id, "branch_id": branch_id})).scalar_one()
        assert after == before
