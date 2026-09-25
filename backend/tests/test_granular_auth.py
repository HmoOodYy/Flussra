"""
Tests for granular permission enforcement and DB-level owner uniqueness.

Covers:
- Fix 1: _ensure_any_perm — granular permission gates on admin endpoints
- Fix 2: DB trigger preventing duplicate active Company Owner assignments
- Fix 5: Company Owner dynamic full permissions in /auth/me
"""
import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(
    client: httpx.AsyncClient,
    username: str,
    password: str = "TestPass1234!",
    company_code: str = "DEMO",
) -> str:
    resp = await client.post("/auth/login", json={
        "username": username,
        "password": password,
        "company_code": company_code,
    })
    assert resp.status_code == 200, f"Login failed for {username!r}: {resp.text}"
    return resp.json()["access_token"]


async def _create_user(
    client: httpx.AsyncClient,
    token: str,
    username: str,
    display_name: str = "Test User",
) -> dict:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": display_name,
            "password": "TestPass1234!",
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=_hdr(token),
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"
    return resp.json()


async def _create_role_with_perms(
    client: httpx.AsyncClient,
    token: str,
    role_name: str,
    perms: list[str],
) -> int:
    """Create a custom company role and grant it the given permissions. Returns role ID."""
    cr = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=_hdr(token),
    )
    assert cr.status_code == 201, f"Create role failed: {cr.text}"
    role_id = cr.json()["company_role_id"]

    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=_hdr(token),
        )
        assert pr.status_code == 200, f"Set permissions failed: {pr.text}"

    return role_id


async def _assign_role(
    client: httpx.AsyncClient,
    token: str,
    user_id: int,
    role_id: int,
    scope: str = "AllCompanyBranches",
    branch_id: int | None = None,
) -> None:
    payload: dict = {"company_role_id": role_id, "scope_type": scope}
    if branch_id is not None:
        payload["branch_id"] = branch_id
    resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=payload,
        headers=_hdr(token),
    )
    assert resp.status_code == 201, f"Assign role failed: {resp.text}"


# ---------------------------------------------------------------------------
# Fix 1 — Granular permission enforcement
# ---------------------------------------------------------------------------

class TestGranularAuth:
    """
    Each test creates a minimal role with exactly the permissions it needs,
    creates a user, assigns the role, logs in, and verifies access.
    """

    async def test_users_view_can_list_users(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """users.view is sufficient to GET /admin/users."""
        role_id = await _create_role_with_perms(client, auth_token, "GA ViewUsers", ["users.view"])
        user = await _create_user(client, auth_token, "ga_view_users")
        await _assign_role(client, auth_token, user["user_id"], role_id)

        tok = await _login(client, "ga_view_users")
        resp = await client.get("/admin/users", headers=_hdr(tok))
        assert resp.status_code == 200

    async def test_users_view_cannot_create_user(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """users.view alone is NOT sufficient to POST /admin/users (needs users.create)."""
        role_id = await _create_role_with_perms(client, auth_token, "GA ViewOnly", ["users.view"])
        user = await _create_user(client, auth_token, "ga_view_only")
        await _assign_role(client, auth_token, user["user_id"], role_id)

        tok = await _login(client, "ga_view_only")
        resp = await client.post(
            "/admin/users",
            json={"username": "ga_should_fail", "display_name": "X", "password": "TestPass1234!"},
            headers=_hdr(tok),
        )
        assert resp.status_code == 403

    async def test_users_create_can_create_user(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """users.create (with users.view dep) is sufficient to POST /admin/users."""
        role_id = await _create_role_with_perms(
            client, auth_token, "GA CreateUsers", ["users.view", "users.create"]
        )
        user = await _create_user(client, auth_token, "ga_create_users")
        await _assign_role(client, auth_token, user["user_id"], role_id)

        tok = await _login(client, "ga_create_users")
        resp = await client.post(
            "/admin/users",
            json={
                "username": "ga_created_by_custom",
                "display_name": "Created By Custom",
                "password": "TestPass1234!",
                "is_active": True,
                "can_login": True,
                "must_change_password": False,
            },
            headers=_hdr(tok),
        )
        assert resp.status_code == 201

    async def test_users_edit_can_edit_user(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """users.edit is sufficient to PATCH /admin/users/{id}."""
        role_id = await _create_role_with_perms(
            client, auth_token, "GA EditUsers", ["users.view", "users.edit"]
        )
        caller = await _create_user(client, auth_token, "ga_edit_caller")
        target = await _create_user(client, auth_token, "ga_edit_target")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        tok = await _login(client, "ga_edit_caller")
        resp = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"display_name": "Edited Name"},
            headers=_hdr(tok),
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Edited Name"

    async def test_users_view_cannot_edit_user(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """users.view alone is NOT sufficient to PATCH /admin/users/{id}."""
        role_id = await _create_role_with_perms(client, auth_token, "GA ViewNoEdit", ["users.view"])
        caller = await _create_user(client, auth_token, "ga_view_noedit")
        target = await _create_user(client, auth_token, "ga_view_noedit_tgt")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        tok = await _login(client, "ga_view_noedit")
        resp = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"display_name": "Should Fail"},
            headers=_hdr(tok),
        )
        assert resp.status_code == 403

    async def test_roles_view_can_list_roles(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """roles.view is sufficient to GET /admin/company-roles."""
        role_id = await _create_role_with_perms(client, auth_token, "GA ViewRoles", ["roles.view"])
        user = await _create_user(client, auth_token, "ga_view_roles")
        await _assign_role(client, auth_token, user["user_id"], role_id)

        tok = await _login(client, "ga_view_roles")
        resp = await client.get("/admin/company-roles", headers=_hdr(tok))
        assert resp.status_code == 200

    async def test_roles_view_cannot_edit_role_perms(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """roles.view alone is NOT sufficient to PUT /admin/company-roles/{id}/permissions."""
        # Create the custom role to try editing
        target_role_id = await _create_role_with_perms(
            client, auth_token, "GA RoleToEdit", ["users.view"]
        )
        role_id = await _create_role_with_perms(client, auth_token, "GA ViewRolesOnly", ["roles.view"])
        user = await _create_user(client, auth_token, "ga_roles_view_only")
        await _assign_role(client, auth_token, user["user_id"], role_id)

        tok = await _login(client, "ga_roles_view_only")
        resp = await client.put(
            f"/admin/company-roles/{target_role_id}/permissions",
            json={"permission_codes": ["users.view"]},
            headers=_hdr(tok),
        )
        assert resp.status_code == 403

    async def test_roles_edit_can_edit_role_perms(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """roles.edit (with roles.view dep) is sufficient to PUT role permissions."""
        target_role_id = await _create_role_with_perms(
            client, auth_token, "GA TargetRole", ["users.view"]
        )
        role_id = await _create_role_with_perms(
            client, auth_token, "GA EditRoles", ["roles.view", "roles.edit"]
        )
        user = await _create_user(client, auth_token, "ga_roles_editor")
        await _assign_role(client, auth_token, user["user_id"], role_id)

        tok = await _login(client, "ga_roles_editor")
        resp = await client.put(
            f"/admin/company-roles/{target_role_id}/permissions",
            json={"permission_codes": ["users.view"]},
            headers=_hdr(tok),
        )
        assert resp.status_code == 200

    async def test_setup_manage_still_works_as_fallback(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """setup.manage (legacy) is still sufficient for all admin operations."""
        role_id = await _create_role_with_perms(
            client, auth_token, "GA LegacyAdmin", ["settings.view", "settings.manage"]
        )
        user = await _create_user(client, auth_token, "ga_legacy_admin")
        await _assign_role(client, auth_token, user["user_id"], role_id)

        tok = await _login(client, "ga_legacy_admin")

        # Can list users
        r1 = await client.get("/admin/users", headers=_hdr(tok))
        assert r1.status_code == 200

        # Can list roles
        r2 = await client.get("/admin/company-roles", headers=_hdr(tok))
        assert r2.status_code == 200

        # Can edit permissions on a role
        target_role_id = await _create_role_with_perms(
            client, auth_token, "GA LegacyTarget", ["users.view"]
        )
        r3 = await client.put(
            f"/admin/company-roles/{target_role_id}/permissions",
            json={"permission_codes": ["users.view"]},
            headers=_hdr(tok),
        )
        assert r3.status_code == 200

    async def test_no_permission_gets_403(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """A role with zero permissions cannot access any admin endpoint."""
        role_id = await _create_role_with_perms(client, auth_token, "GA EmptyRole", [])
        user = await _create_user(client, auth_token, "ga_no_perms")
        await _assign_role(client, auth_token, user["user_id"], role_id)

        tok = await _login(client, "ga_no_perms")

        resp = await client.get("/admin/users", headers=_hdr(tok))
        assert resp.status_code == 403

    async def test_specific_branch_scope_gets_403(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """
        A user with users.view but SpecificBranch scope cannot reach admin endpoints
        (AllCompanyBranches scope required by _ensure_scope).
        """
        # Find HQ branch
        branches_r = await client.get("/core/branches", headers=_hdr(auth_token))
        assert branches_r.status_code == 200
        hq = next(b for b in branches_r.json() if b["branch_code"] == "HQ")

        role_id = await _create_role_with_perms(
            client, auth_token, "GA SpecificBranchRole", ["users.view"]
        )
        user = await _create_user(client, auth_token, "ga_specific_branch")
        # Assign SpecificBranch scope
        resp = await client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={
                "company_role_id": role_id,
                "scope_type": "SpecificBranch",
                "branch_id": hq["branch_id"],
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 201

        tok = await _login(client, "ga_specific_branch")
        resp = await client.get("/admin/users", headers=_hdr(tok))
        assert resp.status_code == 403

    async def test_users_deactivate_can_toggle_active(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """users.deactivate (with users.view dep) is sufficient to PATCH is_active."""
        role_id = await _create_role_with_perms(
            client, auth_token, "GA DeactivateRole", ["users.view", "users.deactivate"]
        )
        caller = await _create_user(client, auth_token, "ga_deactivate_caller")
        target = await _create_user(client, auth_token, "ga_deactivate_target")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        tok = await _login(client, "ga_deactivate_caller")
        resp = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"is_active": False},
            headers=_hdr(tok),
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False


# ---------------------------------------------------------------------------
# Fix 2 — DB-level uniqueness for active Company Owner
# ---------------------------------------------------------------------------

class TestCompanyOwnerUnique:
    async def _get_owner_role_id(
        self, client: httpx.AsyncClient, auth_token: str
    ) -> int:
        resp = await client.get("/admin/company-roles", headers=_hdr(auth_token))
        assert resp.status_code == 200
        for r in resp.json():
            if r["role_code"] == "COMPANY_OWNER":
                return r["company_role_id"]
        raise AssertionError("COMPANY_OWNER role not found")

    async def test_direct_insert_second_owner_fails(
        self, client: httpx.AsyncClient, auth_token: str, pg_instance
    ):
        """
        Directly inserting a second active COMPANY_OWNER assignment via the DB
        must fail with a unique_violation from the trigger.
        Uses psycopg2 so the first INSERT (the seed owner) is visible.
        """
        import psycopg2
        import psycopg2.errors

        # Create a target user via API
        target = await _create_user(client, auth_token, "cou_direct_target")
        owner_role_id = await self._get_owner_role_id(client, auth_token)

        conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
        try:
            cur = conn.cursor()
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    """
                    INSERT INTO sec.userbranchroles
                        (userid, companyid, branchid, roleid, companyroleId, scopetype, isactive)
                    SELECT %s, cr.companyid, NULL, NULL, cr.companyroleid,
                           'AllCompanyBranches', TRUE
                    FROM   sec.companyroles cr
                    WHERE  cr.companyroleid = %s
                    """,
                    (target["user_id"], owner_role_id),
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    async def test_transfer_succeeds_despite_trigger(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """
        The ownership transfer endpoint still works correctly with the trigger in place.
        Transfer revokes old owner's assignment first, so there is never a second
        active COMPANY_OWNER row during the operation.
        """
        # Create a replacement role and a target user
        repl_role_id = await _create_role_with_perms(
            client, auth_token, "COU ReplacementRole", ["users.view"]
        )
        target = await _create_user(client, auth_token, "cou_transfer_target")
        driver_role_id_r = await client.get("/admin/company-roles", headers=_hdr(auth_token))
        driver_role_id = next(
            r["company_role_id"]
            for r in driver_role_id_r.json()
            if r["role_code"] == "DRIVER"
        )
        await _assign_role(
            client, auth_token, target["user_id"], driver_role_id,
            scope="SpecificBranch", branch_id=hq_branch_id,
        )

        # Transfer ownership to target
        resp = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": target["user_id"],
                "replacement_company_role_id": repl_role_id,
                "confirmation": "TRANSFER",
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["new_owner_user_id"] == target["user_id"]

        # Transfer back — use target's token (admin no longer has COMPANY_OWNER)
        target_token = await _login(client, "cou_transfer_target")
        admin_resp = await client.get("/admin/users", headers=_hdr(target_token))
        admin_user = next(u for u in admin_resp.json() if u["username"] == "admin")

        restore = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": admin_user["user_id"],
                "replacement_company_role_id": None,
                "confirmation": "TRANSFER",
            },
            headers=_hdr(target_token),
        )
        assert restore.status_code == 200
        assert restore.json()["new_owner_user_id"] == admin_user["user_id"]

    async def test_no_two_owner_state_after_transfer(
        self, client: httpx.AsyncClient, auth_token: str, hq_branch_id: int
    ):
        """After transfer, exactly one active COMPANY_OWNER exists."""
        target = await _create_user(client, auth_token, "cou_one_owner_tgt")
        dr_r = await client.get("/admin/company-roles", headers=_hdr(auth_token))
        dr_id = next(r["company_role_id"] for r in dr_r.json() if r["role_code"] == "DRIVER")
        await _assign_role(
            client, auth_token, target["user_id"], dr_id,
            scope="SpecificBranch", branch_id=hq_branch_id,
        )

        # Do a transfer and verify only one COMPANY_OWNER exists
        resp = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": target["user_id"],
                "replacement_company_role_id": None,
                "confirmation": "TRANSFER",
            },
            headers=_hdr(auth_token),
        )
        assert resp.status_code == 200

        target_token = await _login(client, "cou_one_owner_tgt")

        users_r = await client.get(
            "/admin/users?include_inactive=true", headers=_hdr(target_token)
        )
        owners = [
            u for u in users_r.json()
            if u["company_role_code"] == "COMPANY_OWNER"
        ]
        assert len(owners) == 1, f"Expected exactly 1 COMPANY_OWNER, found {len(owners)}"

        # Transfer back so admin retains ownership for subsequent tests
        admin_id = next(u["user_id"] for u in users_r.json() if u["username"] == "admin")
        restore = await client.post(
            "/admin/company-owner/transfer",
            json={
                "target_user_id": admin_id,
                "replacement_company_role_id": None,
                "confirmation": "TRANSFER",
            },
            headers=_hdr(target_token),
        )
        assert restore.status_code == 200


# ---------------------------------------------------------------------------
# Fix 5 — Company Owner always has ALL permissions dynamically
# ---------------------------------------------------------------------------

class TestCompanyOwnerDynamicPerms:
    async def test_owner_auth_me_includes_new_permission(
        self, client: httpx.AsyncClient, auth_token: str, pg_instance
    ):
        """
        A permission added to sec.permissions AFTER seed appears in Company Owner's
        active_permissions (via /auth/me) without any change to companyrolepermissions.
        Uses psycopg2 to commit the insert into the shared test DB, exactly like
        the conftest seed approach.
        """
        import psycopg2

        new_perm_code = "test.dynamic_owner_perm"

        conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sec.permissions (permissioncode, permissionname, modulecode) "
            "VALUES (%s, 'Dynamic Owner Test', 'settings') ON CONFLICT DO NOTHING",
            (new_perm_code,),
        )
        conn.close()

        try:
            # /auth/me re-queries DB fresh — should include the new permission
            resp = await client.get("/auth/me", headers=_hdr(auth_token))
            assert resp.status_code == 200
            perms = resp.json()["active_permissions"]
            assert new_perm_code in perms, (
                f"Company Owner should have {new_perm_code!r} automatically, "
                f"but active_permissions are: {perms}"
            )
        finally:
            # Cleanup
            conn2 = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
            conn2.autocommit = True
            cur2 = conn2.cursor()
            cur2.execute(
                "DELETE FROM sec.permissions WHERE permissioncode = %s",
                (new_perm_code,),
            )
            conn2.close()

    async def test_owner_role_perms_in_detail_includes_all(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """
        GET /admin/users/{id} for the Company Owner includes all permission codes
        in role_permission_codes (no manual seed required for new perms).
        """
        users_r = await client.get("/admin/users", headers=_hdr(auth_token))
        assert users_r.status_code == 200
        admin = next(u for u in users_r.json() if u["username"] == "admin")

        resp = await client.get(f"/admin/users/{admin['user_id']}", headers=_hdr(auth_token))
        assert resp.status_code == 200
        role_perms = resp.json()["role_permission_codes"]

        assert "users.view" in role_perms
        assert "roles.edit" in role_perms
        assert "payroll.approve" in role_perms
        assert "settings.manage" in role_perms

    async def test_owner_endpoint_passes_for_new_permission(
        self, client: httpx.AsyncClient, auth_token: str, pg_instance
    ):
        """
        After inserting a new permission into sec.Permissions (no CompanyRolePermissions row),
        fn_UserHasPermission returns TRUE for Company Owner via Path A.
        Verified indirectly: a custom role with that code can be set, and admin can do it.
        """
        import psycopg2

        new_code = "test.owner_fn_perm"
        conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sec.permissions (permissioncode, permissionname, modulecode) "
            "VALUES (%s, 'Owner Fn Test', 'settings') ON CONFLICT DO NOTHING",
            (new_code,),
        )
        conn.close()

        try:
            # Create a role and try to set the new permission on it — admin (Owner) must be authorized
            role_r = await client.post(
                "/admin/company-roles",
                json={"role_name": "Owner Fn Test Role"},
                headers=_hdr(auth_token),
            )
            assert role_r.status_code == 201
            role_id = role_r.json()["company_role_id"]

            # Set the new permission on the role (requires roles.edit → Owner passes via fn Path A)
            pr = await client.put(
                f"/admin/company-roles/{role_id}/permissions",
                json={"permission_codes": [new_code, "roles.view"]},
                headers=_hdr(auth_token),
            )
            assert pr.status_code == 200
        finally:
            conn3 = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
            conn3.autocommit = True
            cur3 = conn3.cursor()
            cur3.execute("DELETE FROM sec.permissions WHERE permissioncode = %s", (new_code,))
            conn3.close()


# ---------------------------------------------------------------------------
# Fix 1 — Extra overrides authorize backend endpoints
# ---------------------------------------------------------------------------

class TestOverridesAuthorizeEndpoints:
    """
    Proves that sec.fn_UserHasPermission now checks UserPermissionOverrides
    (Path D), so a user whose role lacks a permission but has an ALLOW override
    can reach endpoints requiring that permission.
    """

    async def test_override_allows_endpoint(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """
        User has a role with only users.view.
        Admin grants a users.edit override.
        User can now PATCH another user's display_name.
        """
        # Create a role with just users.view
        role_id = await _create_role_with_perms(
            client, auth_token, "OVR ViewOnlyRole", ["users.view"]
        )
        caller = await _create_user(client, auth_token, "ovr_caller")
        target = await _create_user(client, auth_token, "ovr_target")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        # Confirm endpoint is denied before override
        tok = await _login(client, "ovr_caller")
        resp_before = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"display_name": "Should Fail"},
            headers=_hdr(tok),
        )
        assert resp_before.status_code == 403, "Should fail before override"

        # Grant users.edit as an extra override
        ov = await client.put(
            f"/admin/users/{caller['user_id']}/permission-overrides",
            json={"permission_codes": ["users.edit", "users.view"]},
            headers=_hdr(auth_token),
        )
        assert ov.status_code == 200

        # Now the endpoint should succeed
        tok2 = await _login(client, "ovr_caller")
        resp_after = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"display_name": "Override Worked"},
            headers=_hdr(tok2),
        )
        assert resp_after.status_code == 200, f"Should succeed after override: {resp_after.text}"
        assert resp_after.json()["display_name"] == "Override Worked"

    async def test_revoking_override_denies_endpoint(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """After revoking the override (PUT with empty list), the endpoint is denied again."""
        role_id = await _create_role_with_perms(
            client, auth_token, "OVR RevokeRole", ["users.view"]
        )
        caller = await _create_user(client, auth_token, "ovr_revoke_caller")
        target = await _create_user(client, auth_token, "ovr_revoke_target")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        # Grant override
        await client.put(
            f"/admin/users/{caller['user_id']}/permission-overrides",
            json={"permission_codes": ["users.edit", "users.view"]},
            headers=_hdr(auth_token),
        )
        tok = await _login(client, "ovr_revoke_caller")
        r1 = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"display_name": "Has Override"},
            headers=_hdr(tok),
        )
        assert r1.status_code == 200

        # Revoke override
        await client.put(
            f"/admin/users/{caller['user_id']}/permission-overrides",
            json={"permission_codes": []},
            headers=_hdr(auth_token),
        )
        tok2 = await _login(client, "ovr_revoke_caller")
        r2 = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"display_name": "No Override"},
            headers=_hdr(tok2),
        )
        assert r2.status_code == 403, "Should fail after override revoked"


# ---------------------------------------------------------------------------
# Fix 3 — Field-sensitive PATCH /admin/users authorization
# ---------------------------------------------------------------------------

class TestFieldSensitivePatch:
    async def test_deactivate_only_can_toggle_is_active(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """users.deactivate can set is_active=False."""
        role_id = await _create_role_with_perms(
            client, auth_token, "FSP DeactivateRole", ["users.view", "users.deactivate"]
        )
        caller = await _create_user(client, auth_token, "fsp_deact_caller")
        target = await _create_user(client, auth_token, "fsp_deact_target")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        tok = await _login(client, "fsp_deact_caller")
        resp = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"is_active": False},
            headers=_hdr(tok),
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    async def test_deactivate_only_cannot_edit_display_name(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """users.deactivate alone cannot update display_name (needs users.edit)."""
        role_id = await _create_role_with_perms(
            client, auth_token, "FSP DeactNoEdit", ["users.view", "users.deactivate"]
        )
        caller = await _create_user(client, auth_token, "fsp_deact_noedit")
        target = await _create_user(client, auth_token, "fsp_deact_noedit_tgt")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        tok = await _login(client, "fsp_deact_noedit")
        resp = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"display_name": "Should Fail"},
            headers=_hdr(tok),
        )
        assert resp.status_code == 403

    async def test_deactivate_only_cannot_change_can_login(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """users.deactivate alone cannot update can_login (needs users.edit)."""
        role_id = await _create_role_with_perms(
            client, auth_token, "FSP DeactNoLogin", ["users.view", "users.deactivate"]
        )
        caller = await _create_user(client, auth_token, "fsp_deact_nologin")
        target = await _create_user(client, auth_token, "fsp_deact_nologin_tgt")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        tok = await _login(client, "fsp_deact_nologin")
        resp = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"can_login": False},
            headers=_hdr(tok),
        )
        assert resp.status_code == 403

    async def test_users_edit_can_edit_profile_fields(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """users.edit can update display_name, email, phone."""
        role_id = await _create_role_with_perms(
            client, auth_token, "FSP EditProfile", ["users.view", "users.edit"]
        )
        caller = await _create_user(client, auth_token, "fsp_edit_profile")
        target = await _create_user(client, auth_token, "fsp_edit_profile_tgt")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        tok = await _login(client, "fsp_edit_profile")
        resp = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"display_name": "Edited", "email": "edited@test.com"},
            headers=_hdr(tok),
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Edited"

    async def test_mixed_payload_requires_users_edit(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """
        Mixed payload (is_active + email) requires users.edit.
        users.deactivate alone is insufficient.
        """
        role_id = await _create_role_with_perms(
            client, auth_token, "FSP DeactMixed", ["users.view", "users.deactivate"]
        )
        caller = await _create_user(client, auth_token, "fsp_mixed_caller")
        target = await _create_user(client, auth_token, "fsp_mixed_target")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        tok = await _login(client, "fsp_mixed_caller")
        resp = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"is_active": False, "email": "mixed@test.com"},
            headers=_hdr(tok),
        )
        assert resp.status_code == 403

    async def test_fallback_can_do_all_fields(
        self, client: httpx.AsyncClient, auth_token: str
    ):
        """settings.manage (fallback) can update any combination of fields."""
        role_id = await _create_role_with_perms(
            client, auth_token, "FSP FallbackAll", ["settings.view", "settings.manage"]
        )
        caller = await _create_user(client, auth_token, "fsp_fallback_all")
        target = await _create_user(client, auth_token, "fsp_fallback_tgt")
        await _assign_role(client, auth_token, caller["user_id"], role_id)

        tok = await _login(client, "fsp_fallback_all")
        resp = await client.patch(
            f"/admin/users/{target['user_id']}",
            json={"is_active": False, "display_name": "Fallback Edit"},
            headers=_hdr(tok),
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Fallback Edit"


# ---------------------------------------------------------------------------
# Fix 5 / Fix 2 — Owner trigger UPDATE path
# ---------------------------------------------------------------------------

class TestCompanyOwnerTriggerUpdate:
    """
    Tests for the BEFORE UPDATE path of the company owner uniqueness trigger.
    """

    async def test_update_activating_second_owner_fails(
        self, client: httpx.AsyncClient, auth_token: str, pg_instance
    ):
        """
        UPDATE-based duplicate: setting isactive=TRUE on a revoked COMPANY_OWNER
        row when another active owner exists should fail with unique_violation.
        """
        import psycopg2
        import psycopg2.errors

        # Create a second user and insert a revoked COMPANY_OWNER row for them
        other = await _create_user(client, auth_token, "trigg_upd_other")

        conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
        conn.autocommit = False
        try:
            cur = conn.cursor()
            # Insert a REVOKED COMPANY_OWNER row for 'other'
            cur.execute(
                """
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, companyroleId, scopetype, isactive)
                SELECT %s, cr.companyid, NULL, NULL, cr.companyroleid,
                       'AllCompanyBranches', FALSE   -- starts revoked
                FROM   sec.companyroles cr
                WHERE  cr.rolecode = 'COMPANY_OWNER'
                  AND  cr.companyid = (SELECT companyid FROM sec.users WHERE userid = %s)
                """,
                (other["user_id"], other["user_id"]),
            )
            conn.commit()

            # Now try to UPDATE that revoked row to isactive=TRUE
            # (another active COMPANY_OWNER already exists — admin)
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cur.execute(
                    """
                    UPDATE sec.userbranchroles
                    SET    isactive = TRUE
                    WHERE  userid   = %s
                      AND  isactive = FALSE
                    """,
                    (other["user_id"],),
                )
                conn.commit()
        finally:
            conn.rollback()
            conn.close()

    async def test_update_non_owner_does_not_fail(
        self, client: httpx.AsyncClient, auth_token: str, pg_instance, hq_branch_id: int
    ):
        """
        Updating a non-COMPANY_OWNER assignment (e.g. DRIVER role) should not
        be blocked by the trigger.
        """
        import psycopg2

        role_r = await client.get("/admin/company-roles", headers=_hdr(auth_token))
        dr_id = next(r["company_role_id"] for r in role_r.json() if r["role_code"] == "DRIVER")

        user = await _create_user(client, auth_token, "trigg_nonowner")
        await _assign_role(
            client, auth_token, user["user_id"], dr_id,
            scope="SpecificBranch", branch_id=hq_branch_id,
        )

        conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
        conn.autocommit = False
        try:
            cur = conn.cursor()
            # Update the DRIVER assignment notes — should succeed
            cur.execute(
                """
                UPDATE sec.userbranchroles
                SET    notes = 'trigger test update'
                WHERE  userid = %s AND isactive = TRUE
                """,
                (user["user_id"],),
            )
            conn.commit()
            # No exception = trigger did not block non-owner update
        finally:
            conn.rollback()
            conn.close()
