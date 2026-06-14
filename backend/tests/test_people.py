"""
Tests for the People / company-role-assignment / permission-override endpoints.

Covers:
- GET /admin/users  — enriched with company_role_*, driver_id, extra_permission_codes
- POST /admin/users/{id}/company-role-assignments
- DELETE /admin/users/{id}/company-role-assignments/{assignment_id}
- POST /admin/company-owner/transfer
- GET /admin/users/{id}/permission-overrides
- PUT /admin/users/{id}/permission-overrides

Design notes:
- All mutating tests create their own users (function-scoped) so they
  do not interfere with the session-scoped admin user.
- The ownership transfer tests carefully transfer back to admin so the
  session-scoped auth_token remains valid for subsequent test files.
"""
import pytest
import pytest_asyncio
import httpx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_test_user(
    client: httpx.AsyncClient,
    token: str,
    *,
    username: str,
    display_name: str,
    can_login: bool = True,
    is_active: bool = True,
) -> dict:
    resp = await client.post(
        "/admin/users",
        json={
            "username":             username,
            "display_name":         display_name,
            "password":             "TestPass1234!",
            "is_active":            is_active,
            "can_login":            can_login,
            "must_change_password": False,
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 201, f"Failed to create test user: {resp.text}"
    return resp.json()


async def _get_driver_company_role_id(
    client: httpx.AsyncClient,
    token: str,
) -> int:
    resp = await client.get("/admin/company-roles", headers=_hdr(token))
    assert resp.status_code == 200
    for r in resp.json():
        if r["role_code"] == "DRIVER":
            return r["company_role_id"]
    raise AssertionError("DRIVER company role not found")


async def _get_owner_company_role_id(
    client: httpx.AsyncClient,
    token: str,
) -> int:
    resp = await client.get("/admin/company-roles", headers=_hdr(token))
    assert resp.status_code == 200
    for r in resp.json():
        if r["role_code"] == "COMPANY_OWNER":
            return r["company_role_id"]
    raise AssertionError("COMPANY_OWNER company role not found")


# ---------------------------------------------------------------------------
# GET /admin/users — enriched response
# ---------------------------------------------------------------------------

class TestEnrichedUserList:
    async def test_list_users_has_company_role_fields(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.get("/admin/users", headers=_hdr(auth_token))
        assert resp.status_code == 200
        users = resp.json()
        assert len(users) >= 1
        # Admin user has COMPANY_OWNER
        admin = next((u for u in users if u["username"] == "admin"), None)
        assert admin is not None
        assert "company_role_id" in admin
        assert "company_role_code" in admin
        assert "company_role_name" in admin
        assert "company_role_scope" in admin
        assert "driver_id" in admin

    async def test_admin_user_has_company_owner_role(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        resp = await client.get("/admin/users", headers=_hdr(auth_token))
        assert resp.status_code == 200
        admin = next((u for u in resp.json() if u["username"] == "admin"), None)
        assert admin is not None
        assert admin["company_role_code"] == "COMPANY_OWNER"
        assert admin["company_role_name"] == "Company Owner"
        assert admin["company_role_scope"] == "AllCompanyBranches"

    async def test_user_without_company_role_has_null_fields(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Create a bare user with no company role — fields should be None."""
        user = await _create_test_user(
            client, auth_token, username="bare_user_enum", display_name="Bare Enum User"
        )
        resp = await client.get(f"/admin/users/{user['user_id']}", headers=_hdr(auth_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["company_role_id"] is None
        assert data["company_role_code"] is None
        assert data["driver_id"] is None


# ---------------------------------------------------------------------------
# POST /admin/users/{id}/company-role-assignments
# ---------------------------------------------------------------------------

class TestAssignCompanyRole:
    async def test_assign_driver_role_all_branches_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """DRIVER role with AllCompanyBranches scope must be rejected (no home branch)."""
        user = await _create_test_user(
            client, auth_token, username="test_assign_dr1", display_name="Assign Driver 1"
        )
        driver_role_id = await _get_driver_company_role_id(client, auth_token)

        resp = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={"company_role_id": driver_role_id, "scope_type": "AllCompanyBranches"},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422
        assert "home branch" in resp.json()["detail"].lower()

    async def test_assign_specific_branch_scope(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        user = await _create_test_user(
            client, auth_token, username="test_assign_br1", display_name="Assign Branch 1"
        )
        driver_role_id = await _get_driver_company_role_id(client, auth_token)

        resp = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={
                "company_role_id": driver_role_id,
                "scope_type": "SpecificBranch",
                "branch_id": hq_branch_id,
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["scope_type"] == "SpecificBranch"
        assert data["branch_id"] == hq_branch_id

    async def test_cannot_assign_company_owner(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token, username="test_no_owner1", display_name="No Owner 1"
        )
        owner_role_id = await _get_owner_company_role_id(client, auth_token)

        resp = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={"company_role_id": owner_role_id, "scope_type": "AllCompanyBranches"},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422
        assert "transfer" in resp.json()["detail"].lower()

    async def test_specific_branch_requires_branch_id(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token, username="test_scope_br2", display_name="Scope Branch 2"
        )
        driver_role_id = await _get_driver_company_role_id(client, auth_token)

        resp = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={"company_role_id": driver_role_id, "scope_type": "SpecificBranch"},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422
        assert "branch_id" in resp.json()["detail"].lower()

    async def test_all_company_branches_rejects_branch_id(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        user = await _create_test_user(
            client, auth_token, username="test_scope_acb1", display_name="Scope ACB 1"
        )
        driver_role_id = await _get_driver_company_role_id(client, auth_token)

        resp = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={
                "company_role_id": driver_role_id,
                "scope_type": "AllCompanyBranches",
                "branch_id": hq_branch_id,
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422

    async def test_assigning_new_role_revokes_existing(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """Assigning a second company role should revoke the first."""
        user = await _create_test_user(
            client, auth_token, username="test_revoke_old", display_name="Revoke Old"
        )
        driver_role_id = await _get_driver_company_role_id(client, auth_token)
        driver_payload = {
            "company_role_id": driver_role_id,
            "scope_type": "SpecificBranch",
            "branch_id": hq_branch_id,
        }

        # Assign driver role
        r1 = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json=driver_payload,
            headers=_hdr(auth_token),
        )
        assert r1.status_code == 201
        first_assignment_id = r1.json()["assignment_id"]

        # Re-assign DRIVER (new row should revoke the first)
        r2 = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json=driver_payload,
            headers=_hdr(auth_token),
        )
        assert r2.status_code == 201
        assert r2.json()["assignment_id"] != first_assignment_id

        # Verify enriched user now shows the new assignment
        user_resp = await client.get(
            f"/admin/users/{user['user_id']}", headers=_hdr(auth_token)
        )
        assert user_resp.json()["company_role_assignment_id"] == r2.json()["assignment_id"]

    async def test_enriched_user_shows_assigned_role(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        user = await _create_test_user(
            client, auth_token, username="test_enriched_dr", display_name="Enriched Driver"
        )
        driver_role_id = await _get_driver_company_role_id(client, auth_token)

        await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={
                "company_role_id": driver_role_id,
                "scope_type": "SpecificBranch",
                "branch_id": hq_branch_id,
            },
            headers=_hdr(auth_token),
        )

        resp = await client.get(f"/admin/users/{user['user_id']}", headers=_hdr(auth_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["company_role_code"] == "DRIVER"
        assert data["company_role_scope"] == "SpecificBranch"

    async def test_assign_role_to_nonexistent_user_404(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        driver_role_id = await _get_driver_company_role_id(client, auth_token)
        resp = await client.post(
            "/admin/users/999999/company-role-assignments",
            json={"company_role_id": driver_role_id, "scope_type": "AllCompanyBranches"},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 404

    async def test_branch_user_cannot_assign_roles(
        self, client: httpx.AsyncClient, branch_user_token: str, auth_token: str
    ):
        """branch_user has SpecificBranch scope — _ensure_admin should block."""
        user = await _create_test_user(
            client, auth_token, username="test_forbid_u1", display_name="Forbid User 1"
        )
        driver_role_id = await _get_driver_company_role_id(client, auth_token)
        resp = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={"company_role_id": driver_role_id, "scope_type": "AllCompanyBranches"},
            headers=_hdr(branch_user_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /admin/users/{id}/company-role-assignments/{assignment_id}
# ---------------------------------------------------------------------------

class TestRevokeCompanyRoleAssignment:
    async def test_revoke_assignment(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        user = await _create_test_user(
            client, auth_token, username="test_revoke_asgn", display_name="Revoke Assign"
        )
        driver_role_id = await _get_driver_company_role_id(client, auth_token)

        create_resp = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={
                "company_role_id": driver_role_id,
                "scope_type": "SpecificBranch",
                "branch_id": hq_branch_id,
            },
            headers=_hdr(auth_token),
        )
        assert create_resp.status_code == 201
        aid = create_resp.json()["assignment_id"]

        del_resp = await client.delete(
            f"/admin/users/{user['user_id']}/company-role-assignments/{aid}",
            headers=_hdr(auth_token),
        )
        assert del_resp.status_code == 200
        assert del_resp.json()["is_active"] is False

    async def test_cannot_revoke_company_owner_directly(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """The admin user has a COMPANY_OWNER assignment — revoke must be blocked."""
        users_resp = await client.get("/admin/users", headers=_hdr(auth_token))
        admin = next(u for u in users_resp.json() if u["username"] == "admin")
        owner_assignment_id = admin["company_role_assignment_id"]
        assert owner_assignment_id is not None

        resp = await client.delete(
            f"/admin/users/{admin['user_id']}/company-role-assignments/{owner_assignment_id}",
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422
        assert "transfer" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# POST /admin/company-owner/transfer
# ---------------------------------------------------------------------------

class TestOwnerTransfer:
    async def test_transfer_requires_transfer_confirmation(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token, username="test_tfr_wrong", display_name="Transfer Wrong"
        )
        resp = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": user["user_id"],
                "confirmation": "WRONG",
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422

    async def test_transfer_to_self_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        users = await client.get("/admin/users", headers=_hdr(auth_token))
        admin = next(u for u in users.json() if u["username"] == "admin")
        resp = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": admin["user_id"],
                "confirmation": "TRANSFER",
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422
        assert "yourself" in resp.json()["detail"].lower()

    async def test_transfer_to_inactive_user_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token,
            username="test_tfr_inactive",
            display_name="Transfer Inactive",
            is_active=False,
        )
        resp = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": user["user_id"],
                "confirmation": "TRANSFER",
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code in (404, 422)

    async def test_transfer_to_no_login_user_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token,
            username="test_tfr_nologin",
            display_name="Transfer No Login",
            can_login=False,
        )
        resp = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": user["user_id"],
                "confirmation": "TRANSFER",
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422
        assert "login" in resp.json()["detail"].lower()

    async def test_transfer_succeeds_and_restores(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """
        Transfer ownership to a new user, verify, then transfer back to admin.

        Notes on the session-level admin user state:
        - In the conftest seed, admin has ONE userbranchroles row that stores
          BOTH the legacy roleid (PAYROLL_ADMIN) AND the new companyroleId
          (COMPANY_OWNER) together.
        - When the transfer revokes admin's COMPANY_OWNER assignment it
          revokes that single row, leaving admin with no active assignments
          until the reverse transfer creates a new COMPANY_OWNER row for admin.
        - Therefore we must NOT use auth_token to call admin endpoints
          between the two transfers — we use the target user's token instead.
        """
        # Create transfer target
        target = await _create_test_user(
            client, auth_token,
            username="test_tfr_target",
            display_name="Transfer Target",
            can_login=True,
            is_active=True,
        )
        target_id = target["user_id"]

        # Find admin user id BEFORE the transfer
        users = await client.get("/admin/users", headers=_hdr(auth_token))
        admin = next(u for u in users.json() if u["username"] == "admin")
        admin_id = admin["user_id"]

        # Step 1: transfer from admin → target
        resp1 = await client.post(
            "/admin/company-owner/transfer",
            json={"target_user_id": target_id, "confirmation": "TRANSFER"},
            headers=_hdr(auth_token),
        )
        assert resp1.status_code == 200
        result1 = resp1.json()
        assert result1["new_owner_user_id"] == target_id
        assert result1["previous_owner_user_id"] == admin_id

        # Step 2: log in as target (now the owner) and verify state
        login_resp = await client.post("/auth/login", json={
            "username": "test_tfr_target",
            "password": "TestPass1234!",
            "company_code": "DEMO",
        })
        assert login_resp.status_code == 200, f"Target login failed: {login_resp.text}"
        target_token = login_resp.json()["access_token"]

        # Verify target now has COMPANY_OWNER (use target's own token)
        target_self = await client.get(f"/admin/users/{target_id}", headers=_hdr(target_token))
        assert target_self.status_code == 200
        assert target_self.json()["company_role_code"] == "COMPANY_OWNER"

        # Verify admin no longer has COMPANY_OWNER (use target's token)
        admin_during = await client.get(f"/admin/users/{admin_id}", headers=_hdr(target_token))
        assert admin_during.status_code == 200
        assert admin_during.json()["company_role_code"] != "COMPANY_OWNER"

        # Step 3: target transfers back to admin
        resp2 = await client.post(
            "/admin/company-owner/transfer",
            json={"target_user_id": admin_id, "confirmation": "TRANSFER"},
            headers=_hdr(target_token),
        )
        assert resp2.status_code == 200
        result2 = resp2.json()
        assert result2["new_owner_user_id"] == admin_id
        assert result2["previous_owner_user_id"] == target_id

        # Verify admin is Company Owner again (admin's new COMPANY_OWNER row exists)
        admin_final = await client.get(f"/admin/users/{admin_id}", headers=_hdr(auth_token))
        assert admin_final.status_code == 200
        assert admin_final.json()["company_role_code"] == "COMPANY_OWNER"

    async def test_non_owner_cannot_transfer(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """A user without COMPANY_OWNER cannot call the transfer endpoint."""
        # Create a non-owner user
        user = await _create_test_user(
            client, auth_token, username="test_non_owner_tfr", display_name="Non Owner Transfer"
        )
        driver_role_id = await _get_driver_company_role_id(client, auth_token)
        await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={"company_role_id": driver_role_id, "scope_type": "SpecificBranch", "branch_id": hq_branch_id},
            headers=_hdr(auth_token),
        )
        # Login as that user
        login_resp = await client.post("/auth/login", json={
            "username": "test_non_owner_tfr",
            "password": "TestPass1234!",
            "company_code": "DEMO",
        })
        # User may fail _ensure_admin if DRIVER role doesn't have setup.manage,
        # which is correct — they should be blocked either at scope or permission level.
        # Either 403 is acceptable.
        if login_resp.status_code == 200:
            non_owner_token = login_resp.json()["access_token"]
            users = await client.get("/admin/users", headers=_hdr(auth_token))
            admin = next(u for u in users.json() if u["username"] == "admin")
            resp = await client.post(
                "/admin/company-owner/transfer",
                json={"target_user_id": admin["user_id"], "confirmation": "TRANSFER"},
                headers=_hdr(non_owner_token),
            )
            assert resp.status_code in (403, 422)

    async def test_transfer_with_replacement_role(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Transfer ownership and assign a replacement role to the old owner."""
        # Create a custom role to use as replacement
        custom_role_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Transfer Test Role"},
            headers=_hdr(auth_token),
        )
        assert custom_role_resp.status_code == 201
        replacement_role_id = custom_role_resp.json()["company_role_id"]

        target = await _create_test_user(
            client, auth_token,
            username="test_tfr_repl_tgt",
            display_name="Transfer Replace Target",
        )
        target_id = target["user_id"]
        users = await client.get("/admin/users", headers=_hdr(auth_token))
        admin = next(u for u in users.json() if u["username"] == "admin")
        admin_id = admin["user_id"]

        # Transfer with replacement role
        resp = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": target_id,
                "replacement_company_role_id": replacement_role_id,
                "confirmation": "TRANSFER",
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["previous_owner_new_role_id"] == replacement_role_id

        # Transfer back: log in as target (who is now owner)
        login_resp = await client.post("/auth/login", json={
            "username": "test_tfr_repl_tgt",
            "password": "TestPass1234!",
            "company_code": "DEMO",
        })
        assert login_resp.status_code == 200
        target_token = login_resp.json()["access_token"]

        # Admin should now have the replacement role (use target token since admin lost owner)
        admin_data = (await client.get(f"/admin/users/{admin_id}", headers=_hdr(target_token))).json()
        assert admin_data["company_role_id"] == replacement_role_id

        resp2 = await client.post(
            "/admin/company-owner/transfer",
            json={"target_user_id": admin_id, "confirmation": "TRANSFER"},
            headers=_hdr(target_token),
        )
        assert resp2.status_code == 200

        # Admin must be owner again
        admin_final = (await client.get(f"/admin/users/{admin_id}", headers=_hdr(auth_token))).json()
        assert admin_final["company_role_code"] == "COMPANY_OWNER"


# ---------------------------------------------------------------------------
# GET & PUT /admin/users/{id}/permission-overrides
# ---------------------------------------------------------------------------

class TestPermissionOverrides:
    async def test_get_overrides_empty(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token, username="test_pov_empty", display_name="POV Empty"
        )
        resp = await client.get(
            f"/admin/users/{user['user_id']}/permission-overrides",
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_set_overrides_basic(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token, username="test_pov_basic", display_name="POV Basic"
        )
        resp = await client.put(
            f"/admin/users/{user['user_id']}/permission-overrides",
            json={"permission_codes": ["payroll.view", "reports.view"]},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200
        codes = resp.json()
        assert "payroll.view" in codes
        assert "reports.view" in codes

    async def test_set_overrides_auto_adds_parent(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """Sending payroll.edit should auto-add payroll.view."""
        user = await _create_test_user(
            client, auth_token, username="test_pov_dep", display_name="POV Dep"
        )
        resp = await client.put(
            f"/admin/users/{user['user_id']}/permission-overrides",
            json={"permission_codes": ["payroll.edit"]},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200
        codes = set(resp.json())
        assert "payroll.edit" in codes
        assert "payroll.view" in codes  # auto-added

    async def test_set_overrides_replaces_existing(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token, username="test_pov_replace", display_name="POV Replace"
        )
        uid = user["user_id"]
        await client.put(
            f"/admin/users/{uid}/permission-overrides",
            json={"permission_codes": ["payroll.view", "reports.view"]},
            headers=_hdr(auth_token),
        )
        # Replace with different set
        resp2 = await client.put(
            f"/admin/users/{uid}/permission-overrides",
            json={"permission_codes": ["settings.view"]},
            headers=_hdr(auth_token),
        )
        assert resp2.status_code == 200
        codes = resp2.json()
        assert "settings.view" in codes
        assert "payroll.view" not in codes
        assert "reports.view" not in codes

    async def test_set_overrides_clear(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token, username="test_pov_clear", display_name="POV Clear"
        )
        uid = user["user_id"]
        await client.put(
            f"/admin/users/{uid}/permission-overrides",
            json={"permission_codes": ["payroll.view"]},
            headers=_hdr(auth_token),
        )
        resp = await client.put(
            f"/admin/users/{uid}/permission-overrides",
            json={"permission_codes": []},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_set_overrides_invalid_code_rejected(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token, username="test_pov_bad_code", display_name="POV Bad Code"
        )
        resp = await client.put(
            f"/admin/users/{user['user_id']}/permission-overrides",
            json={"permission_codes": ["not.a.real.permission"]},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422

    async def test_company_owner_overrides_blocked(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        users = await client.get("/admin/users", headers=_hdr(auth_token))
        admin = next(u for u in users.json() if u["username"] == "admin")
        resp = await client.put(
            f"/admin/users/{admin['user_id']}/permission-overrides",
            json={"permission_codes": ["payroll.view"]},
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 422
        assert "company owner" in resp.json()["detail"].lower()

    async def test_overrides_appear_in_user_detail(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token, username="test_pov_detail", display_name="POV Detail"
        )
        uid = user["user_id"]
        await client.put(
            f"/admin/users/{uid}/permission-overrides",
            json={"permission_codes": ["payroll.view", "reports.view"]},
            headers=_hdr(auth_token),
        )
        resp = await client.get(f"/admin/users/{uid}", headers=_hdr(auth_token))
        assert resp.status_code == 200
        data = resp.json()
        assert "extra_permission_codes" in data
        assert "payroll.view" in data["extra_permission_codes"]
        assert "reports.view" in data["extra_permission_codes"]

    async def test_overrides_appear_in_list(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        user = await _create_test_user(
            client, auth_token, username="test_pov_list", display_name="POV List"
        )
        uid = user["user_id"]
        await client.put(
            f"/admin/users/{uid}/permission-overrides",
            json={"permission_codes": ["drivers.view"]},
            headers=_hdr(auth_token),
        )
        users_resp = await client.get("/admin/users?include_inactive=true", headers=_hdr(auth_token))
        assert users_resp.status_code == 200
        user_data = next((u for u in users_resp.json() if u["user_id"] == uid), None)
        assert user_data is not None
        assert "drivers.view" in user_data["extra_permission_codes"]

    async def test_overrides_included_in_auth_me(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """Extra permissions must appear in /auth/me active_permissions."""
        # Create a user with Driver role (gets drivers.view from role)
        # then add payroll.view as an extra override
        user = await _create_test_user(
            client, auth_token, username="test_pov_authme", display_name="POV AuthMe",
            can_login=True, is_active=True,
        )
        uid = user["user_id"]
        driver_role_id = await _get_driver_company_role_id(client, auth_token)
        await client.post(
            f"/admin/users/{uid}/company-role-assignments",
            json={"company_role_id": driver_role_id, "scope_type": "SpecificBranch", "branch_id": hq_branch_id},
            headers=_hdr(auth_token),
        )
        # Reset password so we can login
        await client.post(
            f"/admin/users/{uid}/reset-password",
            json={"new_password": "TestPass1234!", "must_change_password": False},
            headers=_hdr(auth_token),
        )
        await client.put(
            f"/admin/users/{uid}/permission-overrides",
            json={"permission_codes": ["payroll.view"]},
            headers=_hdr(auth_token),
        )
        # Login as the new user
        login_resp = await client.post("/auth/login", json={
            "username": "test_pov_authme",
            "password": "TestPass1234!",
            "company_code": "DEMO",
        })
        assert login_resp.status_code == 200
        login_data = login_resp.json()
        # payroll.view should appear (extra override), drivers.view from role
        assert "payroll.view" in login_data["user"]["active_permissions"]
        assert "drivers.view" in login_data["user"]["active_permissions"]

    async def test_role_permission_codes_in_user_detail(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        user = await _create_test_user(
            client, auth_token, username="test_role_perm", display_name="Role Perm"
        )
        uid = user["user_id"]
        driver_role_id = await _get_driver_company_role_id(client, auth_token)
        await client.post(
            f"/admin/users/{uid}/company-role-assignments",
            json={"company_role_id": driver_role_id, "scope_type": "SpecificBranch", "branch_id": hq_branch_id},
            headers=_hdr(auth_token),
        )
        resp = await client.get(f"/admin/users/{uid}", headers=_hdr(auth_token))
        data = resp.json()
        assert "role_permission_codes" in data
        assert "drivers.view" in data["role_permission_codes"]  # DRIVER role has drivers.view
