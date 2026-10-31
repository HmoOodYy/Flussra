"""
Integration tests for the settings domain — /settings/company and
/settings/branches.

Users tested
------------
admin        AllCompanyBranches scope, COMPANY_OWNER role (all permissions)
branch_user  SpecificBranch=HQ scope, PAYROLL_VIEWER_CO role (no write permissions)

Test classes
------------
TestGetCompanyProfile   — GET /settings/company (read access)
TestUpdateCompanyProfile — PATCH /settings/company (write, scope guard, audit rollback)
TestListBranches        — GET /settings/branches (all-scope vs. branch-scope)
TestGetBranch           — GET /settings/branches/{id} (200, 404, 403)
TestCreateBranch        — POST /settings/branches (create, auto-code, conflicts, scope, audit)
TestUpdateBranch        — PATCH /settings/branches/{id} (partial update, default guard, conflicts)
TestSetDefaultBranch    — POST /settings/branches/{id}/set-default (promotion, idempotent, 422)
TestSettingsAudit       — audit-log rollback: company update and branch create roll back on audit failure
TestSettingsAdminAuthority — company-wide settings.manage administers settings and nothing else; branch scope, other permissions and the retired setup.manage do not
"""
import re
from unittest.mock import patch
from uuid import uuid4

import httpx
import psycopg2
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.settings import audit as settings_audit
from app.settings import branches


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Session-scoped fixture: one "scratch" branch created for read/update tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def settings_branch_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
) -> int:
    """
    Create one branch at session start for settings read/update tests.
    Kept as Inactive so it doesn't interfere with HQ-default assumptions.
    """
    resp = await session_client.post(
        "/settings/branches",
        json={
            "branch_name": "Settings Test Branch", "time_zone_id": "UTC",
            "branch_code": "STB",
            "status":      "Inactive",
            "is_default":  False,
            "city":        "Cairo",
            "country":     "Egypt",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"Settings branch seed failed: {resp.text}"
    return resp.json()["branch_id"]


# ---------------------------------------------------------------------------
# TestGetCompanyProfile
# ---------------------------------------------------------------------------

class TestGetCompanyProfile:

    async def test_admin_gets_profile(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get("/settings/company", headers=auth(auth_token))
        assert resp.status_code == 200
        body = resp.json()
        assert body["company_code"] == "DEMO"
        assert body["company_name"] == "Demo Logistics"
        assert "company_id" in body
        assert "status" in body
        assert "is_suspended" in body
        assert "created_at_utc" in body

    async def test_branch_user_can_read_profile(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
    ):
        """Read access is not restricted to company admins."""
        resp = await client.get("/settings/company", headers=auth(branch_user_token))
        assert resp.status_code == 200
        assert resp.json()["company_code"] == "DEMO"

    async def test_unauthenticated_rejected(self, client: httpx.AsyncClient):
        resp = await client.get("/settings/company")
        assert resp.status_code == 401

    async def test_default_branch_name_present(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Profile should include the default branch name (HQ is seeded as default)."""
        resp = await client.get("/settings/company", headers=auth(auth_token))
        assert resp.status_code == 200
        body = resp.json()
        # default_branch_name may be None if no default is set, but our seed sets HQ
        assert body.get("default_branch_name") is not None


# ---------------------------------------------------------------------------
# TestUpdateCompanyProfile
# ---------------------------------------------------------------------------

class TestUpdateCompanyProfile:

    @pytest_asyncio.fixture(autouse=True)
    async def _restore_company_profile(self, direct_db):
        """PATCH /settings/company overwrites the company-wide profile (name, legal name,
        notes, time zone, self-approval policy) that every other module reads. Capture
        its exact state, and restore and verify it however the test ends, so no test
        here depends on another test (or on execution order) to repair shared state."""
        read = text(
            "SELECT companyname, legalname, notes, allowselfapproval, updatedatutc "
            "FROM core.companies WHERE companycode = 'DEMO'"
        )
        original = (await direct_db.execute(read)).mappings().one()
        yield
        await direct_db.execute(
            text("""
                UPDATE core.companies
                SET companyname = :name, legalname = :legal,
                    notes = :notes, allowselfapproval = :allow_self, updatedatutc = :updated
                WHERE companycode = 'DEMO'
            """),
            {"name": original["companyname"], "legal": original["legalname"],
             "notes": original["notes"],
             "allow_self": original["allowselfapproval"], "updated": original["updatedatutc"]},
        )
        restored = (await direct_db.execute(read)).mappings().one()
        assert dict(restored) == dict(original), (
            f"company profile not restored: {dict(original)} -> {dict(restored)}"
        )

    async def test_admin_updates_profile(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.patch(
            "/settings/company",
            json={
                "company_name":  "Demo Logistics Updated",
                "legal_name":    "Demo Logistics Ltd Updated",
                "notes":         "Test note",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["company_name"] == "Demo Logistics Updated"
        assert body["legal_name"]   == "Demo Logistics Ltd Updated"
        assert body["notes"]        == "Test note"
        # The update is real, not just echoed: a fresh read sees it.
        reread = (await client.get("/settings/company", headers=auth(auth_token))).json()
        assert reread["company_name"] == "Demo Logistics Updated"
        assert reread["legal_name"] == "Demo Logistics Ltd Updated"
        assert reread["notes"] == "Test note"

    async def test_branch_user_denied(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
    ):
        """branch_user has SpecificBranch scope — not AllCompanyBranches."""
        resp = await client.patch(
            "/settings/company",
            json={"company_name": "Should Not Work"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_blank_company_name_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.patch(
            "/settings/company",
            json={"company_name": "   "},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_obsolete_company_timezone_is_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.patch(
            "/settings/company",
            json={"company_name": "Demo Logistics", "timezone_name": "Africa/Cairo"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_unauthenticated_rejected(self, client: httpx.AsyncClient):
        resp = await client.patch(
            "/settings/company",
            json={"company_name": "X"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestListBranches
# ---------------------------------------------------------------------------

class TestListBranches:

    async def test_admin_sees_all_branches(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
        settings_branch_id: int,
    ):
        """AllCompanyBranches — must see HQ, PAYTEST, and the settings test branch."""
        resp = await client.get("/settings/branches", headers=auth(auth_token))
        assert resp.status_code == 200
        ids = [b["branch_id"] for b in resp.json()]
        assert hq_branch_id       in ids
        assert paytest_branch_id  in ids
        assert settings_branch_id in ids

    async def test_branch_user_sees_only_hq(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
    ):
        """branch_user is scoped to HQ only — must NOT see PAYTEST."""
        resp = await client.get("/settings/branches", headers=auth(branch_user_token))
        assert resp.status_code == 200
        ids = [b["branch_id"] for b in resp.json()]
        assert hq_branch_id      in ids
        assert paytest_branch_id not in ids

    async def test_metrics_present_on_each_branch(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Each branch object must have its metric fields (can be None but must exist). The Status
        catalog belongs to the Company, so a Branch has no Status key count."""
        resp = await client.get("/settings/branches", headers=auth(auth_token))
        assert resp.status_code == 200
        for branch in resp.json():
            assert "payroll_setup_done"    in branch
            assert "status_keys_count"     not in branch
            assert "total_people_count"    in branch
            assert "active_drivers_count"  in branch
            assert "pending_approvals_count" in branch

    async def test_unauthenticated_rejected(self, client: httpx.AsyncClient):
        resp = await client.get("/settings/branches")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestGetBranch
# ---------------------------------------------------------------------------

class TestGetBranch:

    async def test_admin_gets_hq(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["branch_id"]   == hq_branch_id
        assert body["branch_code"] == "HQ"
        assert body["status"]      == "Active"
        assert body["is_default"]  is True

    async def test_branch_user_can_read_own_branch(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 200

    async def test_branch_user_denied_other_branch(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        paytest_branch_id: int,
    ):
        """branch_user is scoped to HQ — PAYTEST should be 403."""
        resp = await client.get(
            f"/settings/branches/{paytest_branch_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_not_found(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get("/settings/branches/999999", headers=auth(auth_token))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestCreateBranch
# ---------------------------------------------------------------------------

class TestCreateBranch:

    async def test_create_minimal(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Minimal payload (only branch_name) — branch_code auto-generated as BR_XXXXXXXX."""
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "AutoCode Branch Alpha", "time_zone_id": "UTC"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["branch_name"] == "AutoCode Branch Alpha"
        # Auto-generated code follows BR_XXXXXXXX format (8 uppercase alphanumeric chars).
        assert re.fullmatch(r"BR_[A-Z0-9]{8}", body["branch_code"]), (
            f"Expected BR_XXXXXXXX format, got: {body['branch_code']!r}"
        )
        assert body["status"]      == "Active"
        assert body["is_default"]  is False

    async def test_create_minimal_same_initials_both_succeed(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Two branches with the same initials must both succeed (no collisions)."""
        for i in range(1, 3):
            resp = await client.post(
                "/settings/branches",
                json={"branch_name": f"Alpha Beta Gamma {i}", "time_zone_id": "UTC"},
                headers=auth(auth_token),
            )
            assert resp.status_code == 201, (
                f"Branch {i} failed: {resp.text}"
            )
            code = resp.json()["branch_code"]
            assert re.fullmatch(r"BR_[A-Z0-9]{8}", code), (
                f"Expected BR_XXXXXXXX format, got: {code!r}"
            )

    async def test_create_explicit_code(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """When branch_code is supplied, it must be used (uppercased)."""
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "Explicit Code Branch", "time_zone_id": "UTC", "branch_code": "EXP"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["branch_code"] == "EXP"

    async def test_branch_user_denied(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
    ):
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "Should Fail", "time_zone_id": "UTC"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_duplicate_code_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Branch code must be unique per company."""
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "Duplicate Code Branch", "time_zone_id": "UTC", "branch_code": "HQ"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "Branch code" in resp.json()["detail"]

    async def test_duplicate_name_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Branch name must be unique per company."""
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "Headquarters", "time_zone_id": "UTC"},   # HQ's full name isn't HQ but PAYTEST would work
            headers=auth(auth_token),
        )
        # "Headquarters" is the seeded HQ branch name from conftest
        assert resp.status_code == 422
        assert "Branch name" in resp.json()["detail"]

    async def test_blank_name_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "   ", "time_zone_id": "UTC"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_invalid_status_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "Bad Status Branch", "time_zone_id": "UTC", "status": "Pending"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_as_default_clears_old_default(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        Creating a branch with is_default=True should demote the current default.
        Then restore HQ as the default so later tests are not affected.
        """
        resp = await client.post(
            "/settings/branches",
            json={
                "branch_name": "Temp Default Branch", "time_zone_id": "UTC",
                "branch_code": "TMPD",
                "status":      "Active",
                "is_default":  True,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["is_default"] is True

        # Verify HQ is no longer the default.
        hq_resp = await client.get(
            f"/settings/branches/{hq_branch_id}",
            headers=auth(auth_token),
        )
        assert hq_resp.json()["is_default"] is False

        # Restore HQ as default for subsequent tests.
        restore = await client.post(
            f"/settings/branches/{hq_branch_id}/set-default",
            headers=auth(auth_token),
        )
        assert restore.status_code == 200

    async def test_unauthenticated_rejected(self, client: httpx.AsyncClient):
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "No Auth", "time_zone_id": "UTC"},
        )
        assert resp.status_code == 401

    async def test_empty_string_branch_code_triggers_auto_generate(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """branch_code='' (empty string) should auto-generate, not 422."""
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "EmptyCode Branch", "time_zone_id": "UTC", "branch_code": ""},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert re.fullmatch(r"BR_[A-Z0-9]{8}", body["branch_code"]), (
            f"Expected BR_XXXXXXXX auto-generated code, got: {body['branch_code']!r}"
        )

    async def test_whitespace_branch_code_triggers_auto_generate(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """branch_code='   ' (whitespace only) should auto-generate, not 422."""
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "WhitespaceCode Branch", "time_zone_id": "UTC", "branch_code": "   "},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert re.fullmatch(r"BR_[A-Z0-9]{8}", body["branch_code"]), (
            f"Expected BR_XXXXXXXX auto-generated code, got: {body['branch_code']!r}"
        )

    async def test_null_branch_code_triggers_auto_generate(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """branch_code=null should auto-generate."""
        resp = await client.post(
            "/settings/branches",
            json={"branch_name": "NullCode Branch", "time_zone_id": "UTC", "branch_code": None},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert re.fullmatch(r"BR_[A-Z0-9]{8}", body["branch_code"]), (
            f"Expected BR_XXXXXXXX auto-generated code, got: {body['branch_code']!r}"
        )

    async def test_branch_code_collision_retries_and_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        When _generate_branch_code returns a colliding code on the first call
        and a unique code on the second, branch creation must succeed and the
        returned branch_code must match the second (unique) generated code.
        """
        collision_code = "BR_COLLISION"
        unique_code    = "BR_UNIQUEONE"
        call_count = {"n": 0}

        def _patched_gen():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return collision_code
            return unique_code

        # Pre-create a branch whose code will be the "collision" value.
        pre = await client.post(
            "/settings/branches",
            json={"branch_name": "Collision Seed Branch", "time_zone_id": "UTC", "branch_code": collision_code},
            headers=auth(auth_token),
        )
        assert pre.status_code == 201, f"Seed branch failed: {pre.text}"

        with patch.object(branches, "_generate_branch_code", _patched_gen):
            resp = await client.post(
                "/settings/branches",
                json={"branch_name": "Retry Success Branch", "time_zone_id": "UTC"},
                headers=auth(auth_token),
            )

        assert resp.status_code == 201, f"Expected 201 after retry, got: {resp.text}"
        assert resp.json()["branch_code"] == unique_code
        assert call_count["n"] >= 2, "Expected at least two calls to _generate_branch_code"

    async def test_branch_code_retry_exhaustion_returns_500(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        When _generate_branch_code always returns the same code (matching an
        existing branch), all retries are exhausted and the service must return
        HTTP 500 with the documented detail message.
        """
        always_same_code = "BR_EXHAUST1"

        # Pre-create a branch with this code so every generated attempt collides.
        pre = await client.post(
            "/settings/branches",
            json={"branch_name": "Exhaustion Seed Branch", "time_zone_id": "UTC", "branch_code": always_same_code},
            headers=auth(auth_token),
        )
        assert pre.status_code == 201, f"Seed branch failed: {pre.text}"

        with patch.object(branches, "_generate_branch_code", lambda: always_same_code):
            resp = await client.post(
                "/settings/branches",
                json={"branch_name": "Should Exhaust Retries Branch", "time_zone_id": "UTC"},
                headers=auth(auth_token),
            )

        assert resp.status_code == 500
        assert resp.json()["detail"] == (
            "Could not generate a unique branch code. Please try again."
        )


# ---------------------------------------------------------------------------
# TestUpdateBranch
# ---------------------------------------------------------------------------

class TestUpdateBranch:

    async def test_patch_city(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        settings_branch_id: int,
    ):
        """Patching only city must leave all other fields unchanged."""
        before = (
            await client.get(
                f"/settings/branches/{settings_branch_id}",
                headers=auth(auth_token),
            )
        ).json()

        resp = await client.patch(
            f"/settings/branches/{settings_branch_id}",
            json={"city": "Alexandria"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["city"]        == "Alexandria"
        assert body["branch_name"] == before["branch_name"]
        assert body["branch_code"] == before["branch_code"]
        assert body["status"]      == before["status"]

    async def test_patch_status_of_non_default_branch(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        settings_branch_id: int,
    ):
        """Non-default branch can be freely activated/deactivated."""
        resp = await client.patch(
            f"/settings/branches/{settings_branch_id}",
            json={"status": "Active"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "Active"

        # Restore to Inactive.
        resp2 = await client.patch(
            f"/settings/branches/{settings_branch_id}",
            json={"status": "Inactive"},
            headers=auth(auth_token),
        )
        assert resp2.status_code == 200

    async def test_patch_default_branch_to_inactive_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """HQ is the default branch — deactivating it must return 422."""
        resp = await client.patch(
            f"/settings/branches/{hq_branch_id}",
            json={"status": "Inactive"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "default" in resp.json()["detail"].lower()

    async def test_patch_requires_all_scope(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        settings_branch_id: int,
    ):
        resp = await client.patch(
            f"/settings/branches/{settings_branch_id}",
            json={"city": "Alexandria"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_patch_not_found(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.patch(
            "/settings/branches/999999",
            json={"city": "Nowhere"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_patch_code_conflict_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        settings_branch_id: int,
    ):
        """Patching branch_code to one already in use → 422."""
        resp = await client.patch(
            f"/settings/branches/{settings_branch_id}",
            json={"branch_code": "HQ"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "Branch code" in resp.json()["detail"]

    async def test_patch_name_conflict_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        settings_branch_id: int,
    ):
        """Patching branch_name to one already in use → 422."""
        resp = await client.patch(
            f"/settings/branches/{settings_branch_id}",
            json={"branch_name": "Headquarters", "time_zone_id": "UTC"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "Branch name" in resp.json()["detail"]

    async def test_patch_blank_name_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        settings_branch_id: int,
    ):
        resp = await client.patch(
            f"/settings/branches/{settings_branch_id}",
            json={"branch_name": "", "time_zone_id": "UTC"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_unauthenticated_rejected(
        self,
        client: httpx.AsyncClient,
        settings_branch_id: int,
    ):
        resp = await client.patch(
            f"/settings/branches/{settings_branch_id}",
            json={"city": "X"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestSetDefaultBranch
# ---------------------------------------------------------------------------

class TestSetDefaultBranch:

    async def test_set_default_promotes_branch(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
    ):
        """
        Promote PAYTEST to default → PAYTEST.is_default=True, HQ.is_default=False.
        Then restore HQ so subsequent tests are not affected.
        """
        resp = await client.post(
            f"/settings/branches/{paytest_branch_id}/set-default",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["is_default"] is True
        assert resp.json()["branch_id"]  == paytest_branch_id

        # HQ should no longer be the default.
        hq = await client.get(
            f"/settings/branches/{hq_branch_id}",
            headers=auth(auth_token),
        )
        assert hq.json()["is_default"] is False

        # Restore HQ as default.
        restore = await client.post(
            f"/settings/branches/{hq_branch_id}/set-default",
            headers=auth(auth_token),
        )
        assert restore.status_code == 200
        assert restore.json()["is_default"] is True

    async def test_set_default_idempotent(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Calling set-default on the already-default branch is a no-op."""
        resp = await client.post(
            f"/settings/branches/{hq_branch_id}/set-default",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["is_default"] is True

    async def test_set_default_inactive_branch_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        settings_branch_id: int,
    ):
        """settings_branch_id is Inactive — must return 422."""
        resp = await client.post(
            f"/settings/branches/{settings_branch_id}/set-default",
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "active" in resp.json()["detail"].lower()

    async def test_set_default_requires_all_scope(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        paytest_branch_id: int,
    ):
        resp = await client.post(
            f"/settings/branches/{paytest_branch_id}/set-default",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_set_default_not_found(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/settings/branches/999999/set-default",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestSettingsAudit — rollback verification
# ---------------------------------------------------------------------------

class TestSettingsAudit:

    async def test_company_update_rolls_back_when_audit_fails(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        When audit.write_settings_audit raises inside update_company_profile, the
        UPDATE to core.companies must roll back — the name must be unchanged.
        """
        # Record the current name.
        before = (
            await client.get("/settings/company", headers=auth(auth_token))
        ).json()["company_name"]

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on company update")

        with patch.object(settings_audit, "write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure on company update"):
                await client.patch(
                    "/settings/company",
                    json={"company_name": "Should Not Persist"},
                    headers=auth(auth_token),
                )

        # The name must be unchanged after the rollback.
        after = (
            await client.get("/settings/company", headers=auth(auth_token))
        ).json()["company_name"]
        assert after == before

    async def test_branch_create_rolls_back_when_audit_fails(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        When audit.write_settings_audit raises inside create_branch, the INSERT into
        core.branches must roll back — the branch must not appear in the list.
        """
        branch_name = "Rollback Test Branch Audit"

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on branch create")

        with patch.object(settings_audit, "write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure on branch create"):
                await client.post(
                    "/settings/branches",
                    json={"branch_name": branch_name, "branch_code": "RTBA",
                          "time_zone_id": "UTC"},
                    headers=auth(auth_token),
                )

        # Verify the branch is NOT in the list.
        resp = await client.get("/settings/branches", headers=auth(auth_token))
        assert resp.status_code == 200
        names = [b["branch_name"] for b in resp.json()]
        assert branch_name not in names

    async def test_branch_update_rolls_back_when_audit_fails(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        settings_branch_id: int,
    ):
        """
        When audit.write_settings_audit raises inside update_branch, the UPDATE to
        core.branches must roll back — the notes must be unchanged.
        """
        before = (
            await client.get(
                f"/settings/branches/{settings_branch_id}",
                headers=auth(auth_token),
            )
        ).json()["notes"]

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on branch update")

        with patch.object(settings_audit, "write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure on branch update"):
                await client.patch(
                    f"/settings/branches/{settings_branch_id}",
                    json={"notes": "This note must not persist due to rollback"},
                    headers=auth(auth_token),
                )

        after = (
            await client.get(
                f"/settings/branches/{settings_branch_id}",
                headers=auth(auth_token),
            )
        ).json()["notes"]
        assert after == before


# ---------------------------------------------------------------------------
# TestSettingsAdminAuthority
# ---------------------------------------------------------------------------

async def _login_as_role_holder(
    client: httpx.AsyncClient,
    admin_token: str,
    *,
    permissions: list[str],
    scope_type: str,
    branch_id: int | None = None,
) -> str:
    """Create a one-off role with exactly *permissions*, a user holding only that
    role at *scope_type*, and return that user's access token."""
    token, _ = await _role_holder(
        client, admin_token, permissions=permissions, scope_type=scope_type, branch_id=branch_id,
    )
    return token


async def _role_holder(
    client: httpx.AsyncClient,
    admin_token: str,
    *,
    permissions: list[str],
    scope_type: str,
    branch_id: int | None = None,
) -> tuple[str, int]:
    """Like _login_as_role_holder, also returning the one-off role's id."""
    suffix = uuid4().hex[:10]
    role = await client.post(
        "/admin/company-roles", json={"role_name": f"Settings Authority {suffix}"},
        headers=auth(admin_token),
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["company_role_id"]
    granted = await client.put(
        f"/admin/company-roles/{role_id}/permissions",
        json={"permission_codes": permissions}, headers=auth(admin_token),
    )
    assert granted.status_code == 200, granted.text

    username = f"settings_authority_{suffix}"
    created = await client.post(
        "/admin/users",
        json={
            "username": username, "display_name": "Settings Authority",
            "password": "TestPass1234!", "is_active": True, "can_login": True,
            "must_change_password": False, "is_staged": False,
            "role_assignment": {
                "company_role_id": role_id, "scope_type": scope_type, "branch_id": branch_id,
            },
        },
        headers=auth(admin_token),
    )
    assert created.status_code == 201, created.text

    login = await client.post(
        "/auth/login",
        json={"username": username, "password": "TestPass1234!", "company_code": "DEMO"},
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"], role_id


_NON_SETTINGS_READS = [
    "/admin/users",
    "/admin/company-roles",
    "/admin/permissions",
    "/admin/employee-link-candidates",
    "/compensation/pay-definitions",
]


async def _statuses(client: httpx.AsyncClient, token: str, paths: list[str]) -> list[int]:
    return [(await client.get(path, headers=auth(token))).status_code for path in paths]


class TestSettingsAdminAuthority:
    """Company settings administration requires company-wide settings.manage, and
    settings.manage authorizes nothing outside company settings. The retired
    setup.manage code authorizes nothing at all."""

    async def test_company_wide_settings_manage_can_administer_company_settings(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        owned_branch_id: int,
    ):
        token = await _login_as_role_holder(
            client, auth_token, permissions=["settings.manage"], scope_type="AllCompanyBranches",
        )

        company = (await client.get("/settings/company", headers=auth(token))).json()
        patched_company = await client.patch(
            "/settings/company", json={"company_name": company["company_name"]},
            headers=auth(token),
        )
        assert patched_company.status_code == 200, patched_company.text

        patched_branch = await client.patch(
            f"/settings/branches/{owned_branch_id}", json={"city": "Giza"}, headers=auth(token),
        )
        assert patched_branch.status_code == 200, patched_branch.text
        assert patched_branch.json()["city"] == "Giza"

        created_key = await client.post(
            "/settings/status-keys",
            json={"key_name": f"Authority {uuid4().hex[:10]}", "category": "Vacation"},
            headers=auth(token),
        )
        assert created_key.status_code == 201, created_key.text

        currencies = await client.get("/settings/currencies", headers=auth(token))
        assert currencies.status_code == 200, currencies.text

        created_column = await client.post(
            "/settings/status-rate-columns",
            json={"column_name": f"Authority {uuid4().hex[:10]}"},
            headers=auth(token),
        )
        assert created_column.status_code in (200, 201), created_column.text

    async def test_settings_manage_alone_cannot_manage_users_roles_rates_or_pay_items(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        token = await _login_as_role_holder(
            client, auth_token, permissions=["settings.manage"], scope_type="AllCompanyBranches",
        )

        assert await _statuses(client, token, _NON_SETTINGS_READS) == [403] * len(_NON_SETTINGS_READS)
        created_role = await client.post(
            "/admin/company-roles", json={"role_name": "Not Allowed"}, headers=auth(token),
        )
        assert created_role.status_code == 403
        created_user = await client.post(
            "/admin/users",
            json={
                "username": f"refused_{uuid4().hex[:10]}", "display_name": "Refused",
                "password": "TestPass1234!", "is_active": True, "can_login": False,
                "must_change_password": False, "is_staged": True,
            },
            headers=auth(token),
        )
        assert created_user.status_code == 403

    def test_pay_rate_and_pay_item_guards_do_not_accept_the_company_settings_code(self):
        from app.compensation.guards import _DEFINITION_READ, RATE_EDIT, RATE_READ

        for codes in (RATE_EDIT, RATE_READ, _DEFINITION_READ):
            assert "settings.manage" not in codes
            assert "setup.manage" not in codes

    async def test_setup_manage_grants_nothing(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        pg_instance,
        owned_branch_id: int,
    ):
        token, role_id = await _role_holder(
            client, auth_token, permissions=["users.view"], scope_type="AllCompanyBranches",
        )
        # The role editor cannot grant a code outside the catalogue; a database that still
        # holds a legacy setup.manage row must nevertheless gain nothing from it.
        conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sec.companyrolepermissions (companyroleid, permissioncode) "
                    "VALUES (%s, 'setup.manage') ON CONFLICT DO NOTHING", (role_id,),
                )
                cur.execute(
                    "DELETE FROM sec.companyrolepermissions "
                    "WHERE companyroleid = %s AND permissioncode = 'users.view'", (role_id,),
                )
        finally:
            conn.close()

        settings_attempts = [
            await client.patch(
                "/settings/company", json={"company_name": "Not Allowed"}, headers=auth(token)),
            await client.patch(
                f"/settings/branches/{owned_branch_id}", json={"city": "Nowhere"},
                headers=auth(token)),
            await client.post(
                "/settings/status-keys",
                json={"key_name": f"Refused {uuid4().hex[:10]}", "category": "Vacation"},
                headers=auth(token)),
            await client.get("/settings/currencies", headers=auth(token)),
        ]
        assert [r.status_code for r in settings_attempts] == [403] * 4
        assert await _statuses(client, token, _NON_SETTINGS_READS) == [403] * len(_NON_SETTINGS_READS)

    async def test_branch_scoped_settings_manage_cannot_administer_settings(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        owned_branch_id: int,
    ):
        token = await _login_as_role_holder(
            client, auth_token, permissions=["settings.manage"],
            scope_type="SpecificBranch", branch_id=hq_branch_id,
        )

        attempts = [
            await client.patch(
                "/settings/company", json={"company_name": "Not Allowed"}, headers=auth(token)),
            await client.patch(
                f"/settings/branches/{owned_branch_id}", json={"city": "Nowhere"},
                headers=auth(token)),
            await client.post(
                "/settings/status-keys",
                json={"key_name": f"Refused {uuid4().hex[:10]}", "category": "Vacation"},
                headers=auth(token)),
            await client.get("/settings/currencies", headers=auth(token)),
        ]
        assert [r.status_code for r in attempts] == [403, 403, 403, 403]

    async def test_company_wide_user_without_settings_manage_cannot_administer_settings(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        owned_branch_id: int,
    ):
        token = await _login_as_role_holder(
            client, auth_token, permissions=["users.view"], scope_type="AllCompanyBranches",
        )

        attempts = [
            await client.patch(
                "/settings/company", json={"company_name": "Not Allowed"}, headers=auth(token)),
            await client.patch(
                f"/settings/branches/{owned_branch_id}", json={"city": "Nowhere"},
                headers=auth(token)),
            await client.post(
                "/settings/status-keys",
                json={"key_name": f"Refused {uuid4().hex[:10]}", "category": "Vacation"},
                headers=auth(token)),
            await client.get("/settings/currencies", headers=auth(token)),
        ]
        assert [r.status_code for r in attempts] == [403, 403, 403, 403]
