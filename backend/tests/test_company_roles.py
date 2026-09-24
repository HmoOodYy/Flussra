"""
Tests for company-scoped role management.

Coverage:
  - Default roles (Company Owner, Driver) seeded automatically
  - Company Owner has all permissions; Driver has minimal permissions
  - Custom role CRUD
  - Permissions set/replace with protected-role guard
  - Login gate (no active role -> 403)
  - /auth/me returns active_permissions
  - fn_UserHasPermission uses CompanyRolePermissions
  - GET /company-roles/{id}/users endpoint
"""
import re

import pytest

# -- Helpers ------------------------------------------------------------------

def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# Default roles seeded per company
# =============================================================================

class TestDefaultRolesSeeded:
    """Every company gets COMPANY_OWNER and DRIVER on first run."""

    @pytest.mark.asyncio
    async def test_company_owner_exists(self, client, auth_token):
        resp = await client.get("/admin/company-roles", headers=auth_headers(auth_token))
        assert resp.status_code == 200
        roles = resp.json()
        codes = [r["role_code"] for r in roles]
        assert "COMPANY_OWNER" in codes

    @pytest.mark.asyncio
    async def test_driver_exists(self, client, auth_token):
        resp = await client.get("/admin/company-roles", headers=auth_headers(auth_token))
        assert resp.status_code == 200
        codes = [r["role_code"] for r in resp.json()]
        assert "DRIVER" in codes

    @pytest.mark.asyncio
    async def test_company_owner_is_protected(self, client, auth_token):
        resp = await client.get("/admin/company-roles", headers=auth_headers(auth_token))
        owner = next(r for r in resp.json() if r["role_code"] == "COMPANY_OWNER")
        assert owner["is_protected"] is True
        assert owner["is_default"] is True

    @pytest.mark.asyncio
    async def test_driver_is_protected(self, client, auth_token):
        resp = await client.get("/admin/company-roles", headers=auth_headers(auth_token))
        driver = next(r for r in resp.json() if r["role_code"] == "DRIVER")
        assert driver["is_protected"] is True
        assert driver["is_default"] is True

    @pytest.mark.asyncio
    async def test_no_legacy_mirrored_roles_in_list(self, client, auth_token):
        """Auto-mirrored legacy roles (PAYROLL_ADMIN, PAYROLL_VIEWER, etc.) must not appear."""
        resp = await client.get("/admin/company-roles", headers=auth_headers(auth_token))
        assert resp.status_code == 200
        codes = {r["role_code"] for r in resp.json()}
        # These are global sec.roles codes — should not appear as CompanyRoles
        for legacy_code in ("PAYROLL_ADMIN", "PAYROLL_VIEWER"):
            assert legacy_code not in codes, (
                f"Legacy mirrored role '{legacy_code}' should not appear in company roles list"
            )


# =============================================================================
# Company Owner permissions
# =============================================================================

class TestCompanyOwnerPermissions:
    """Company Owner must hold all known permission codes."""

    @pytest.mark.asyncio
    async def test_company_owner_has_all_permissions(self, client, auth_token):
        # Get all UI-visible permission codes
        perms_resp = await client.get("/admin/permissions", headers=auth_headers(auth_token))
        assert perms_resp.status_code == 200
        all_codes = {p["permission_code"] for p in perms_resp.json()}

        # Get Company Owner role
        roles_resp = await client.get("/admin/company-roles", headers=auth_headers(auth_token))
        owner = next(r for r in roles_resp.json() if r["role_code"] == "COMPANY_OWNER")
        role_id = owner["company_role_id"]

        # Get its permissions
        perm_resp = await client.get(
            f"/admin/company-roles/{role_id}/permissions",
            headers=auth_headers(auth_token),
        )
        assert perm_resp.status_code == 200
        owner_codes = set(perm_resp.json()["permission_codes"])

        # Owner must have every UI-visible permission (and more — legacy included)
        assert all_codes <= owner_codes, (
            f"Company Owner is missing: {all_codes - owner_codes}"
        )

    @pytest.mark.asyncio
    async def test_company_owner_permissions_cannot_be_modified(self, client, auth_token):
        """PUT /permissions on Company Owner must be rejected entirely."""
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        owner_id = next(r["company_role_id"] for r in roles if r["role_code"] == "COMPANY_OWNER")

        resp = await client.put(
            f"/admin/company-roles/{owner_id}/permissions",
            json={"permission_codes": ["reports.view"]},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422
        assert "Company Owner" in resp.json()["detail"]
        assert "cannot be modified" in resp.json()["detail"].lower() or \
               "managed automatically" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_company_owner_permissions_blocked_even_with_all_perms(self, client, auth_token):
        """Even passing all permissions is rejected for Company Owner (fully locked)."""
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        owner_id = next(r["company_role_id"] for r in roles if r["role_code"] == "COMPANY_OWNER")

        all_perms = [p["permission_code"] for p in
                     (await client.get("/admin/permissions", headers=auth_headers(auth_token))).json()]
        resp = await client.put(
            f"/admin/company-roles/{owner_id}/permissions",
            json={"permission_codes": all_perms},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422


# =============================================================================
# Driver permissions
# =============================================================================

class TestDriverPermissions:
    """Driver should have minimal permissions (drivers.view only by default)."""

    @pytest.mark.asyncio
    async def test_driver_has_minimal_permissions(self, client, auth_token):
        roles_resp = await client.get("/admin/company-roles", headers=auth_headers(auth_token))
        driver = next(r for r in roles_resp.json() if r["role_code"] == "DRIVER")

        perm_resp = await client.get(
            f"/admin/company-roles/{driver['company_role_id']}/permissions",
            headers=auth_headers(auth_token),
        )
        assert perm_resp.status_code == 200
        driver_codes = set(perm_resp.json()["permission_codes"])
        assert "drivers.view" in driver_codes
        # Driver should NOT have admin-level permissions by default
        assert "settings.manage" not in driver_codes
        assert "roles.delete" not in driver_codes


# =============================================================================
# Custom role CRUD
# =============================================================================

class TestCustomRoleCRUD:
    """Create, read, update, and delete custom company roles."""

    @pytest.mark.asyncio
    async def test_create_custom_role_generates_code(self, client, auth_token):
        """Role code is auto-generated in CR_XXXXXXXX format."""
        resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Payroll Clerk Test"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["role_name"] == "Payroll Clerk Test"
        # Code must match CR_ prefix + 8 uppercase alphanumeric chars
        assert re.fullmatch(r"CR_[A-Z0-9]{8}", data["role_code"]), (
            f"Unexpected role_code format: {data['role_code']}"
        )
        assert data["is_custom"] is True
        assert data["is_protected"] is False
        assert data["is_default"] is False

    @pytest.mark.asyncio
    async def test_create_two_roles_get_different_codes(self, client, auth_token):
        """Two custom roles must have different auto-generated codes."""
        r1 = await client.post(
            "/admin/company-roles",
            json={"role_name": "Alpha Role"},
            headers=auth_headers(auth_token),
        )
        r2 = await client.post(
            "/admin/company-roles",
            json={"role_name": "Beta Role"},
            headers=auth_headers(auth_token),
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["role_code"] != r2.json()["role_code"]

    @pytest.mark.asyncio
    async def test_get_company_role(self, client, auth_token):
        create_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Get Test Role"},
            headers=auth_headers(auth_token),
        )
        assert create_resp.status_code == 201
        role_id = create_resp.json()["company_role_id"]
        role_name = create_resp.json()["role_name"]

        get_resp = await client.get(
            f"/admin/company-roles/{role_id}",
            headers=auth_headers(auth_token),
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["role_name"] == role_name
        assert get_resp.json()["company_role_id"] == role_id

    @pytest.mark.asyncio
    async def test_update_role_name(self, client, auth_token):
        create_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Update Me"},
            headers=auth_headers(auth_token),
        )
        role_id = create_resp.json()["company_role_id"]

        patch_resp = await client.patch(
            f"/admin/company-roles/{role_id}",
            json={"role_name": "Updated Name"},
            headers=auth_headers(auth_token),
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["role_name"] == "Updated Name"

    @pytest.mark.asyncio
    async def test_delete_custom_role_no_users(self, client, auth_token):
        """Archiving a custom role with no users returns 204 and hides it from the API."""
        create_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Delete Me"},
            headers=auth_headers(auth_token),
        )
        role_id = create_resp.json()["company_role_id"]

        del_resp = await client.delete(
            f"/admin/company-roles/{role_id}",
            headers=auth_headers(auth_token),
        )
        assert del_resp.status_code == 204

        # Archived role is invisible through the normal API
        get_resp = await client.get(
            f"/admin/company-roles/{role_id}",
            headers=auth_headers(auth_token),
        )
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_nonexistent_role_returns_404(self, client, auth_token):
        resp = await client.get(
            "/admin/company-roles/99999999",
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_role_with_notes(self, client, auth_token):
        resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Noted Role", "notes": "Internal use only"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["notes"] == "Internal use only"


# =============================================================================
# Role rename hardening
# =============================================================================

class TestRoleRename:
    """Guards on the PATCH /company-roles/{id} rename path."""

    async def _create_role(self, client, auth_token, name: str) -> dict:
        resp = await client.post(
            "/admin/company-roles",
            json={"role_name": name},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 201
        return resp.json()

    @pytest.mark.asyncio
    async def test_rename_custom_role_succeeds(self, client, auth_token):
        role = await self._create_role(client, auth_token, "Rename Source")
        resp = await client.patch(
            f"/admin/company-roles/{role['company_role_id']}",
            json={"role_name": "Rename Target"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["role_name"] == "Rename Target"

    @pytest.mark.asyncio
    async def test_rename_custom_role_preserves_role_code(self, client, auth_token):
        role = await self._create_role(client, auth_token, "Code Preserve Test")
        original_code = role["role_code"]
        resp = await client.patch(
            f"/admin/company-roles/{role['company_role_id']}",
            json={"role_name": "Code Preserve Test Renamed"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["role_code"] == original_code

    @pytest.mark.asyncio
    async def test_rename_to_own_name_succeeds(self, client, auth_token):
        """Renaming to the same current name must not trigger duplicate error."""
        role = await self._create_role(client, auth_token, "Self Rename Test")
        resp = await client.patch(
            f"/admin/company-roles/{role['company_role_id']}",
            json={"role_name": "Self Rename Test"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["role_name"] == "Self Rename Test"

    @pytest.mark.asyncio
    async def test_rename_to_own_name_case_insensitive_succeeds(self, client, auth_token):
        """LOWER(self) == LOWER(self) — self-exclusion must use id, not name."""
        role = await self._create_role(client, auth_token, "CaseMe Role")
        resp = await client.patch(
            f"/admin/company-roles/{role['company_role_id']}",
            json={"role_name": "CASEME ROLE"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_rename_duplicate_returns_422(self, client, auth_token):
        await self._create_role(client, auth_token, "Existing Unique Role")
        second = await self._create_role(client, auth_token, "Second Unique Role")
        resp = await client.patch(
            f"/admin/company-roles/{second['company_role_id']}",
            json={"role_name": "Existing Unique Role"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422
        assert "already exists" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_rename_duplicate_case_insensitive_returns_422(self, client, auth_token):
        await self._create_role(client, auth_token, "Dupe Case Role")
        second = await self._create_role(client, auth_token, "Second Case Role")
        resp = await client.patch(
            f"/admin/company-roles/{second['company_role_id']}",
            json={"role_name": "DUPE CASE ROLE"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_rename_company_owner_returns_422(self, client, auth_token):
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        owner_id = next(r["company_role_id"] for r in roles if r["role_code"] == "COMPANY_OWNER")
        resp = await client.patch(
            f"/admin/company-roles/{owner_id}",
            json={"role_name": "Hacked Owner"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422
        assert "cannot be renamed" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_rename_driver_returns_422(self, client, auth_token):
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        driver_id = next(r["company_role_id"] for r in roles if r["role_code"] == "DRIVER")
        resp = await client.patch(
            f"/admin/company-roles/{driver_id}",
            json={"role_name": "Hacked Driver"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422
        assert "cannot be renamed" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_rename_without_permission_returns_403(self, client, branch_user_token):
        resp = await client.patch(
            "/admin/company-roles/1",
            json={"role_name": "Sneaky Rename"},
            headers=auth_headers(branch_user_token),
        )
        assert resp.status_code in (403, 422)

    @pytest.mark.asyncio
    async def test_rename_audit_log_written(self, client, auth_token):
        """_write_admin_audit must be called with COMPANY_ROLE_UPDATED on a successful rename."""
        from unittest.mock import patch

        import app.admin.service as admin_service

        role = await self._create_role(client, auth_token, "Audit Rename Role")
        audit_calls = []

        original = admin_service._write_admin_audit

        async def _capture(*args, **kwargs):
            audit_calls.append(kwargs)
            return await original(*args, **kwargs)

        with patch.object(admin_service, "_write_admin_audit", _capture):
            resp = await client.patch(
                f"/admin/company-roles/{role['company_role_id']}",
                json={"role_name": "Audit Rename Role v2"},
                headers=auth_headers(auth_token),
            )

        assert resp.status_code == 200
        assert any(c.get("action_code") == "COMPANY_ROLE_UPDATED" for c in audit_calls), (
            f"COMPANY_ROLE_UPDATED audit not logged; calls were: {audit_calls}"
        )


# =============================================================================
# Protected role guards
# =============================================================================

class TestProtectedRoleGuards:
    """Protected roles cannot be deleted or have permissions modified."""

    @pytest.mark.asyncio
    async def test_cannot_delete_company_owner(self, client, auth_token):
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        owner_id = next(r["company_role_id"] for r in roles if r["role_code"] == "COMPANY_OWNER")

        resp = await client.delete(
            f"/admin/company-roles/{owner_id}",
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422
        assert "protected" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_cannot_delete_driver_role(self, client, auth_token):
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        driver_id = next(r["company_role_id"] for r in roles if r["role_code"] == "DRIVER")

        resp = await client.delete(
            f"/admin/company-roles/{driver_id}",
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_cannot_modify_company_owner_permissions(self, client, auth_token):
        """Any PUT /permissions on Company Owner must be rejected — fully locked."""
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        owner_id = next(r["company_role_id"] for r in roles if r["role_code"] == "COMPANY_OWNER")

        resp = await client.put(
            f"/admin/company-roles/{owner_id}/permissions",
            json={"permission_codes": ["reports.view"]},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422
        assert "Company Owner" in resp.json()["detail"]


# =============================================================================
# Role archive / soft-delete
# =============================================================================

class TestRoleArchive:
    """DELETE endpoint archives (soft-deletes) roles rather than physically removing them."""

    async def _create_role(self, client, auth_token, name: str) -> dict:
        resp = await client.post(
            "/admin/company-roles",
            json={"role_name": name},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 201
        return resp.json()

    # ── Protected roles cannot be archived ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_archive_company_owner_returns_422(self, client, auth_token):
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        owner_id = next(r["company_role_id"] for r in roles if r["role_code"] == "COMPANY_OWNER")
        resp = await client.delete(f"/admin/company-roles/{owner_id}", headers=auth_headers(auth_token))
        assert resp.status_code == 422
        assert "protected" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_archive_driver_returns_422(self, client, auth_token):
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        driver_id = next(r["company_role_id"] for r in roles if r["role_code"] == "DRIVER")
        resp = await client.delete(f"/admin/company-roles/{driver_id}", headers=auth_headers(auth_token))
        assert resp.status_code == 422
        assert "protected" in resp.json()["detail"].lower()

    # ── Roles with active assignments cannot be archived ─────────────────────

    @pytest.mark.asyncio
    async def test_archive_role_with_active_users_returns_422(self, client, auth_token, direct_db):
        from sqlalchemy import text as sa_text
        role = await self._create_role(client, auth_token, "Assigned Archive Test")
        rid = role["company_role_id"]

        # Fetch a real user to assign (the company owner user)
        roles_r = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        owner_role = next(r for r in roles_r if r["role_code"] == "COMPANY_OWNER")
        users_r = await client.get(
            f"/admin/company-roles/{owner_role['company_role_id']}/users",
            headers=auth_headers(auth_token),
        )
        admin_user = users_r.json()[0]
        uid = admin_user["user_id"]

        # Manually insert an active assignment for this role
        cid_r = await direct_db.execute(sa_text(
            "SELECT companyid FROM sec.companyroles WHERE companyroleid = :rid"
        ), {"rid": rid})
        cid = cid_r.scalar()

        await direct_db.execute(sa_text("""
            INSERT INTO sec.userbranchroles
                (userid, companyid, branchid, companyroleId, scopetype, isactive)
            VALUES
                (:uid, :cid, NULL, :rid, 'AllCompanyBranches', TRUE)
            ON CONFLICT DO NOTHING
        """), {"uid": uid, "cid": cid, "rid": rid})
        await direct_db.commit()

        resp = await client.delete(f"/admin/company-roles/{rid}", headers=auth_headers(auth_token))
        assert resp.status_code == 422
        assert "active" in resp.json()["detail"].lower() or "assigned" in resp.json()["detail"].lower()

        # Clean up
        await direct_db.execute(sa_text(
            "DELETE FROM sec.userbranchroles WHERE companyroleId = :rid AND userid = :uid"
        ), {"rid": rid, "uid": uid})
        await direct_db.commit()

    # ── Custom role with no active users archives cleanly ────────────────────

    @pytest.mark.asyncio
    async def test_archive_custom_role_returns_204(self, client, auth_token):
        role = await self._create_role(client, auth_token, "Archive Me Role")
        resp = await client.delete(
            f"/admin/company-roles/{role['company_role_id']}",
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_archived_role_absent_from_list(self, client, auth_token):
        role = await self._create_role(client, auth_token, "List Disappear Role")
        rid = role["company_role_id"]
        await client.delete(f"/admin/company-roles/{rid}", headers=auth_headers(auth_token))

        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        ids = [r["company_role_id"] for r in roles]
        assert rid not in ids, "Archived role must not appear in the roles list"

    @pytest.mark.asyncio
    async def test_archived_role_returns_404_on_get(self, client, auth_token):
        role = await self._create_role(client, auth_token, "Get 404 Role")
        rid = role["company_role_id"]
        await client.delete(f"/admin/company-roles/{rid}", headers=auth_headers(auth_token))

        resp = await client.get(f"/admin/company-roles/{rid}", headers=auth_headers(auth_token))
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_archived_role_returns_404_on_patch(self, client, auth_token):
        role = await self._create_role(client, auth_token, "Patch 404 Role")
        rid = role["company_role_id"]
        await client.delete(f"/admin/company-roles/{rid}", headers=auth_headers(auth_token))

        resp = await client.patch(
            f"/admin/company-roles/{rid}",
            json={"role_name": "Ghost Rename"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_archived_role_returns_404_on_permissions_put(self, client, auth_token):
        role = await self._create_role(client, auth_token, "Perms 404 Role")
        rid = role["company_role_id"]
        await client.delete(f"/admin/company-roles/{rid}", headers=auth_headers(auth_token))

        resp = await client.put(
            f"/admin/company-roles/{rid}/permissions",
            json={"permission_codes": ["reports.view"]},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_archived_role_preserved_in_database(self, client, auth_token, direct_db):
        """Row must remain in DB with isarchived=TRUE and archivedat set."""
        from sqlalchemy import text as sa_text
        role = await self._create_role(client, auth_token, "DB Preserved Role")
        rid = role["company_role_id"]
        await client.delete(f"/admin/company-roles/{rid}", headers=auth_headers(auth_token))

        row = (await direct_db.execute(
            sa_text("SELECT isarchived, archivedat, rolecode FROM sec.companyroles WHERE companyroleid = :rid"),
            {"rid": rid},
        )).mappings().first()

        assert row is not None, "Row must not be physically deleted"
        assert row["isarchived"] is True, "isarchived must be TRUE after archive"
        assert row["archivedat"] is not None, "archivedat must be set"

    @pytest.mark.asyncio
    async def test_archived_role_code_unchanged(self, client, auth_token, direct_db):
        """role_code must remain unchanged after archive."""
        from sqlalchemy import text as sa_text
        role = await self._create_role(client, auth_token, "Code Frozen Role")
        rid = role["company_role_id"]
        original_code = role["role_code"]
        await client.delete(f"/admin/company-roles/{rid}", headers=auth_headers(auth_token))

        row = (await direct_db.execute(
            sa_text("SELECT rolecode FROM sec.companyroles WHERE companyroleid = :rid"),
            {"rid": rid},
        )).mappings().first()
        assert row["rolecode"] == original_code

    @pytest.mark.asyncio
    async def test_archive_audit_log_written(self, client, auth_token):
        """_write_admin_audit must be called with COMPANY_ROLE_ARCHIVED on archive."""
        from unittest.mock import patch

        import app.admin.service as admin_service

        role = await self._create_role(client, auth_token, "Audit Archive Role")
        audit_calls = []
        original = admin_service._write_admin_audit

        async def _capture(*args, **kwargs):
            audit_calls.append(kwargs)
            return await original(*args, **kwargs)

        with patch.object(admin_service, "_write_admin_audit", _capture):
            await client.delete(
                f"/admin/company-roles/{role['company_role_id']}",
                headers=auth_headers(auth_token),
            )

        assert any(c.get("action_code") == "COMPANY_ROLE_ARCHIVED" for c in audit_calls), (
            f"COMPANY_ROLE_ARCHIVED audit not logged; calls: {audit_calls}"
        )

    @pytest.mark.asyncio
    async def test_rename_hardening_still_passes_after_archive_changes(self, client, auth_token):
        """Smoke: rename guards still work correctly alongside archive behavior."""
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        owner_id = next(r["company_role_id"] for r in roles if r["role_code"] == "COMPANY_OWNER")
        resp = await client.patch(
            f"/admin/company-roles/{owner_id}",
            json={"role_name": "Hacked Owner"},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422
        assert "cannot be renamed" in resp.json()["detail"].lower()


# =============================================================================
# Permissions CRUD
# =============================================================================

class TestCompanyRolePermissions:
    """GET and PUT permissions for custom roles."""

    @pytest.mark.asyncio
    async def test_get_permissions_empty_custom_role(self, client, auth_token):
        create_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Perm Test Role"},
            headers=auth_headers(auth_token),
        )
        role_id = create_resp.json()["company_role_id"]

        perm_resp = await client.get(
            f"/admin/company-roles/{role_id}/permissions",
            headers=auth_headers(auth_token),
        )
        assert perm_resp.status_code == 200
        assert perm_resp.json()["permission_codes"] == []

    @pytest.mark.asyncio
    async def test_set_permissions_custom_role(self, client, auth_token):
        create_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Perm Set Role"},
            headers=auth_headers(auth_token),
        )
        role_id = create_resp.json()["company_role_id"]

        put_resp = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": ["payroll.view", "payroll.edit", "reports.view"]},
            headers=auth_headers(auth_token),
        )
        assert put_resp.status_code == 200
        codes = set(put_resp.json()["permission_codes"])
        assert codes == {"payroll.view", "payroll.edit", "reports.view"}

    @pytest.mark.asyncio
    async def test_set_permissions_auto_adds_parent(self, client, auth_token):
        """Backend auto-adds view parent when only child (edit) is sent."""
        create_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Dep Test Role"},
            headers=auth_headers(auth_token),
        )
        role_id = create_resp.json()["company_role_id"]

        # Send only payroll.edit — backend should also include payroll.view
        put_resp = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": ["payroll.edit"]},
            headers=auth_headers(auth_token),
        )
        assert put_resp.status_code == 200
        codes = set(put_resp.json()["permission_codes"])
        assert "payroll.edit" in codes
        assert "payroll.view" in codes  # auto-added

    @pytest.mark.asyncio
    async def test_unknown_permission_code_rejected(self, client, auth_token):
        create_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Bad Perm Role"},
            headers=auth_headers(auth_token),
        )
        role_id = create_resp.json()["company_role_id"]

        resp = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": ["totally.fake.code"]},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_put_permissions_is_replace(self, client, auth_token):
        """PUT replaces all permissions — old codes not in the new list are removed."""
        create_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Replace Test"},
            headers=auth_headers(auth_token),
        )
        role_id = create_resp.json()["company_role_id"]

        # Set initial permissions
        await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": ["payroll.view", "reports.view"]},
            headers=auth_headers(auth_token),
        )

        # Replace with a different set
        put_resp = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": ["drivers.view"]},
            headers=auth_headers(auth_token),
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["permission_codes"] == ["drivers.view"]

    @pytest.mark.asyncio
    async def test_put_empty_permissions(self, client, auth_token):
        """Custom roles can have zero permissions."""
        create_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Empty Perm Role"},
            headers=auth_headers(auth_token),
        )
        role_id = create_resp.json()["company_role_id"]

        # First set some permissions
        await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": ["reports.view"]},
            headers=auth_headers(auth_token),
        )
        # Then clear them
        put_resp = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": []},
            headers=auth_headers(auth_token),
        )
        assert put_resp.status_code == 200
        assert put_resp.json()["permission_codes"] == []


# =============================================================================
# Permission dependency normalisation
# =============================================================================

class TestPermissionDependencyNormalization:
    """Backend auto-adds payroll.view when any payroll child permission is saved."""

    async def _make_role(self, client, auth_token, name: str) -> int:
        resp = await client.post(
            "/admin/company-roles",
            json={"role_name": name},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 201
        return resp.json()["company_role_id"]

    async def _set_perms(self, client, auth_token, role_id: int, codes: list[str]) -> set[str]:
        resp = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": codes},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 200
        return set(resp.json()["permission_codes"])

    @pytest.mark.asyncio
    async def test_payroll_entry_auto_adds_payroll_view(self, client, auth_token):
        rid = await self._make_role(client, auth_token, "Dep payroll.entry")
        codes = await self._set_perms(client, auth_token, rid, ["payroll.entry"])
        assert "payroll.entry" in codes
        assert "payroll.view" in codes, "payroll.view must be auto-added when payroll.entry is saved"

    @pytest.mark.asyncio
    async def test_payroll_period_create_auto_adds_payroll_view(self, client, auth_token):
        rid = await self._make_role(client, auth_token, "Dep payroll.period.create")
        codes = await self._set_perms(client, auth_token, rid, ["payroll.period.create"])
        assert "payroll.period.create" in codes
        assert "payroll.view" in codes, "payroll.view must be auto-added when payroll.period.create is saved"

    @pytest.mark.asyncio
    async def test_review_decide_does_not_auto_add_payroll_view(self, client, auth_token):
        """review.decide is a standalone permission; payroll.view is NOT implied.
        A user who can only decide review items must not gain payroll_ops dashboard access."""
        rid = await self._make_role(client, auth_token, "Dep review.decide")
        codes = await self._set_perms(client, auth_token, rid, ["review.decide"])
        assert "review.decide" in codes
        assert "payroll.view" not in codes, (
            "payroll.view must NOT be auto-added when review.decide is saved — "
            "this would grant unintended payroll_ops dashboard access"
        )

    @pytest.mark.asyncio
    async def test_payroll_approve_auto_adds_payroll_view(self, client, auth_token):
        """Existing rule — payroll.approve -> payroll.view must still hold."""
        rid = await self._make_role(client, auth_token, "Dep payroll.approve")
        codes = await self._set_perms(client, auth_token, rid, ["payroll.approve"])
        assert "payroll.approve" in codes
        assert "payroll.view" in codes

    @pytest.mark.asyncio
    async def test_payroll_finalize_auto_adds_payroll_view(self, client, auth_token):
        """Existing rule — payroll.finalize -> payroll.view must still hold."""
        rid = await self._make_role(client, auth_token, "Dep payroll.finalize")
        codes = await self._set_perms(client, auth_token, rid, ["payroll.finalize"])
        assert "payroll.finalize" in codes
        assert "payroll.view" in codes

    @pytest.mark.asyncio
    async def test_payroll_view_alone_needs_no_parent(self, client, auth_token):
        """payroll.view has no parent — saving it alone must not inject anything extra."""
        rid = await self._make_role(client, auth_token, "Dep payroll.view alone")
        codes = await self._set_perms(client, auth_token, rid, ["payroll.view"])
        assert codes == {"payroll.view"}

    @pytest.mark.asyncio
    async def test_multiple_payroll_children_single_view(self, client, auth_token):
        """Multiple payroll children each depend on payroll.view — only one copy added."""
        rid = await self._make_role(client, auth_token, "Dep multi payroll")
        codes = await self._set_perms(
            client, auth_token, rid,
            ["payroll.entry", "payroll.approve", "review.decide"],
        )
        assert "payroll.view" in codes
        # Exactly one payroll.view (set semantics)
        raw = (await client.get(
            f"/admin/company-roles/{rid}/permissions",
            headers=auth_headers(auth_token),
        )).json()["permission_codes"]
        assert raw.count("payroll.view") == 1

    @pytest.mark.asyncio
    async def test_unknown_permission_still_rejected(self, client, auth_token):
        rid = await self._make_role(client, auth_token, "Dep unknown check")
        resp = await client.put(
            f"/admin/company-roles/{rid}/permissions",
            json={"permission_codes": ["payroll.entry", "totally.fake"]},
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 422


# =============================================================================
# GET /company-roles/{id}/users
# =============================================================================

class TestCompanyRoleUsers:
    """GET /admin/company-roles/{id}/users endpoint."""

    @pytest.mark.asyncio
    async def test_get_users_for_nonexistent_role_returns_404(self, client, auth_token):
        resp = await client.get(
            "/admin/company-roles/99999999/users",
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_company_owner_users_includes_admin(self, client, auth_token):
        """The seeded admin user is assigned to COMPANY_OWNER company role."""
        roles = (await client.get("/admin/company-roles", headers=auth_headers(auth_token))).json()
        owner = next((r for r in roles if r["role_code"] == "COMPANY_OWNER"), None)
        if owner is None:
            pytest.skip("COMPANY_OWNER role not found in test seed")
        role_id = owner["company_role_id"]

        resp = await client.get(
            f"/admin/company-roles/{role_id}/users",
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 200
        users = resp.json()
        assert isinstance(users, list)
        assert len(users) > 0

        # Each user has the required fields
        for u in users:
            assert "user_id" in u
            assert "username" in u
            assert "display_name" in u
            assert "scope_type" in u
            assert "is_active" in u

    @pytest.mark.asyncio
    async def test_empty_custom_role_has_no_users(self, client, auth_token):
        create_resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "No Users Role"},
            headers=auth_headers(auth_token),
        )
        role_id = create_resp.json()["company_role_id"]

        resp = await client.get(
            f"/admin/company-roles/{role_id}/users",
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_branch_user_cannot_get_role_users(self, client, branch_user_token):
        resp = await client.get(
            "/admin/company-roles/1/users",
            headers=auth_headers(branch_user_token),
        )
        assert resp.status_code == 403


# =============================================================================
# Permissions catalogue filtering
# =============================================================================

class TestPermissionsListFiltering:
    """GET /admin/permissions with ui_only filtering."""

    @pytest.mark.asyncio
    async def test_default_list_excludes_legacy_modules(self, client, auth_token):
        """Default (ui_only=true) must exclude core and capitalised legacy modules.
        Note: 'review' is intentionally included — review.decide is actively enforced
        and must be assignable in the Roles UI."""
        resp = await client.get("/admin/permissions", headers=auth_headers(auth_token))
        assert resp.status_code == 200
        module_codes = {p["module_code"] for p in resp.json()}
        # Capitalised / truly legacy modules must not appear
        for bad_module in ("core", "Core", "Review", "Payroll", "Settings"):
            assert bad_module not in module_codes, (
                f"Legacy module '{bad_module}' should not appear with ui_only=true"
            )

    @pytest.mark.asyncio
    async def test_review_decide_visible_in_default_list(self, client, auth_token):
        """review.decide must appear in the default (ui_only=true) permissions list
        because it is actively enforced and must be assignable via the Roles UI."""
        resp = await client.get("/admin/permissions", headers=auth_headers(auth_token))
        assert resp.status_code == 200
        codes = {p["permission_code"] for p in resp.json()}
        assert "review.decide" in codes, (
            "review.decide must be visible in the default permissions list"
        )

    @pytest.mark.asyncio
    async def test_ui_only_false_includes_legacy(self, client, auth_token):
        """ui_only=false should expose legacy codes beyond the default set."""
        resp = await client.get(
            "/admin/permissions?ui_only=false",
            headers=auth_headers(auth_token),
        )
        assert resp.status_code == 200
        codes = {p["permission_code"] for p in resp.json()}
        # At least one truly-legacy code (not in default list) should appear
        assert any(c in codes for c in ("setup.manage", "drivers.manage")), (
            "Expected at least one legacy code with ui_only=false"
        )


# =============================================================================
# Login gate -- no active role -> 403
# =============================================================================

class TestLoginGate:
    """
    A user with no active role assignments cannot log in.

    We create a bare user (no role assignment) in the DB via a direct SQL
    fixture, then verify the login is blocked with the correct error code.
    """

    @pytest.mark.asyncio
    async def test_user_with_no_role_cannot_login(self, client, direct_db, auth_token):
        from sqlalchemy import text as _text

        from app.auth.security import hash_password

        pw = hash_password("TestPass123!")
        # Insert a user with no role assignment in the DEMO company
        await direct_db.execute(
            _text("""
                INSERT INTO sec.users
                    (companyid, username, displayname, passwordhash, isactive, canlogin)
                SELECT c.companyid, 'norole_user', 'No Role User', :pw, TRUE, TRUE
                FROM core.companies c WHERE c.companycode = 'DEMO'
            """),
            {"pw": pw},
        )

        resp = await client.post("/auth/login", json={
            "username": "norole_user",
            "password": "TestPass123!",
            "company_code": "DEMO",
        })
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        # detail is a dict with "code" and "message"
        assert isinstance(detail, dict)
        assert detail["code"] == "no_active_role"

    @pytest.mark.asyncio
    async def test_user_with_active_role_can_login(self, client, auth_token):
        """Sanity check: the seeded admin user (with role) can log in normally."""
        resp = await client.post("/auth/login", json={
            "username": "admin",
            "password": "TestPass123!",
            "company_code": "DEMO",
        })
        assert resp.status_code == 200
        assert "access_token" in resp.json()


# =============================================================================
# /auth/me returns active_permissions
# =============================================================================

class TestActivPermissionsInMe:
    """/auth/me must include active_permissions for the logged-in user."""

    @pytest.mark.asyncio
    async def test_me_includes_active_permissions(self, client, auth_token):
        resp = await client.get("/auth/me", headers=auth_headers(auth_token))
        assert resp.status_code == 200
        data = resp.json()
        assert "active_permissions" in data
        perms = data["active_permissions"]
        assert isinstance(perms, list)
        assert len(perms) > 0

    @pytest.mark.asyncio
    async def test_admin_has_setup_manage_permission(self, client, auth_token):
        """The seeded admin has a company role with all permissions seeded."""
        resp = await client.get("/auth/me", headers=auth_headers(auth_token))
        perms = resp.json()["active_permissions"]
        # Should have at least the legacy code and the new settings code
        assert "setup.manage" in perms or "settings.manage" in perms

    @pytest.mark.asyncio
    async def test_login_response_includes_active_permissions(self, client):
        """POST /auth/login also returns active_permissions in the user object."""
        resp = await client.post("/auth/login", json={
            "username": "admin",
            "password": "TestPass123!",
            "company_code": "DEMO",
        })
        assert resp.status_code == 200
        user = resp.json()["user"]
        assert "active_permissions" in user
        assert isinstance(user["active_permissions"], list)
        assert len(user["active_permissions"]) > 0

    @pytest.mark.asyncio
    async def test_branch_user_has_empty_or_minimal_permissions(self, client, branch_user_token):
        """branch_user has PAYROLL_VIEWER role (no permissions) -- list is empty or minimal."""
        resp = await client.get("/auth/me", headers=auth_headers(branch_user_token))
        assert resp.status_code == 200
        perms = resp.json()["active_permissions"]
        # PAYROLL_VIEWER has no permissions seeded -- active_permissions should be empty
        assert isinstance(perms, list)


# =============================================================================
# fn_UserHasPermission uses CompanyRolePermissions
# =============================================================================

class TestPermissionFunctionNewPath:
    """
    The test admin user has CompanyRoleID set (seeded in conftest).
    fn_UserHasPermission should find permissions via the new path.
    """

    @pytest.mark.asyncio
    async def test_admin_can_access_admin_endpoints(self, client, auth_token):
        """
        All admin endpoints call _ensure_admin() which calls fn_UserHasPermission.
        If the new path works, admin user can list users.
        """
        resp = await client.get("/admin/users", headers=auth_headers(auth_token))
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_branch_user_blocked_from_admin_endpoints(self, client, branch_user_token):
        """branch_user has no setup.manage permission -- should get 403."""
        resp = await client.get("/admin/users", headers=auth_headers(branch_user_token))
        assert resp.status_code == 403


# =============================================================================
# Scope enforcement on company-role endpoints
# =============================================================================

class TestCompanyRolesScopeEnforcement:
    """All company-role endpoints require AllCompanyBranches scope."""

    @pytest.mark.asyncio
    async def test_branch_user_cannot_list_company_roles(self, client, branch_user_token):
        resp = await client.get("/admin/company-roles", headers=auth_headers(branch_user_token))
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_branch_user_cannot_create_company_role(self, client, branch_user_token):
        resp = await client.post(
            "/admin/company-roles",
            json={"role_name": "Sneaky Role"},
            headers=auth_headers(branch_user_token),
        )
        assert resp.status_code == 403
