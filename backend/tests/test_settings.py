"""
Integration tests for the settings domain — /settings/company and
/settings/branches.

Users tested
------------
admin        AllCompanyBranches scope, PAYROLL_ADMIN role (all permissions)
branch_user  SpecificBranch=HQ scope, PAYROLL_VIEWER role (no write permissions)

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
"""
import re
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from app.settings import service as settings_service


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
            "branch_name": "Settings Test Branch",
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
        assert "timezone_name" in body
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
                "timezone_name": "Africa/Cairo",
                "notes":         "Test note",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["company_name"] == "Demo Logistics Updated"
        assert body["legal_name"]   == "Demo Logistics Ltd Updated"
        assert body["notes"]        == "Test note"

    async def test_update_restores_original(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Restore the company name after the previous update test."""
        resp = await client.patch(
            "/settings/company",
            json={
                "company_name":  "Demo Logistics",
                "legal_name":    "Demo Logistics Ltd",
                "timezone_name": "Africa/Cairo",
                "notes":         None,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["company_name"] == "Demo Logistics"

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

    async def test_timezone_preserved_when_omitted(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """When timezone_name is None, the existing value must be preserved."""
        current = (
            await client.get("/settings/company", headers=auth(auth_token))
        ).json()["timezone_name"]

        resp = await client.patch(
            "/settings/company",
            json={"company_name": "Demo Logistics", "timezone_name": None},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["timezone_name"] == current

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
        """Each branch object must have the 5 metric fields (can be None but must exist)."""
        resp = await client.get("/settings/branches", headers=auth(auth_token))
        assert resp.status_code == 200
        for branch in resp.json():
            assert "payroll_setup_done"    in branch
            assert "status_keys_count"     in branch
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
            json={"branch_name": "AutoCode Branch Alpha"},
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
                json={"branch_name": f"Alpha Beta Gamma {i}"},
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
            json={"branch_name": "Explicit Code Branch", "branch_code": "EXP"},
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
            json={"branch_name": "Should Fail"},
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
            json={"branch_name": "Duplicate Code Branch", "branch_code": "HQ"},
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
            json={"branch_name": "Headquarters"},   # HQ's full name isn't HQ but PAYTEST would work
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
            json={"branch_name": "   "},
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
            json={"branch_name": "Bad Status Branch", "status": "Pending"},
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
                "branch_name": "Temp Default Branch",
                "branch_code": "TMPD",
                "status":      "Active",
                "is_default":  True,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        new_id = resp.json()["branch_id"]
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
            json={"branch_name": "No Auth"},
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
            json={"branch_name": "EmptyCode Branch", "branch_code": ""},
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
            json={"branch_name": "WhitespaceCode Branch", "branch_code": "   "},
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
            json={"branch_name": "NullCode Branch", "branch_code": None},
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

        original_gen = settings_service._generate_branch_code

        def _patched_gen():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return collision_code
            return unique_code

        # Pre-create a branch whose code will be the "collision" value.
        pre = await client.post(
            "/settings/branches",
            json={"branch_name": "Collision Seed Branch", "branch_code": collision_code},
            headers=auth(auth_token),
        )
        assert pre.status_code == 201, f"Seed branch failed: {pre.text}"

        with patch.object(settings_service, "_generate_branch_code", _patched_gen):
            resp = await client.post(
                "/settings/branches",
                json={"branch_name": "Retry Success Branch"},
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
            json={"branch_name": "Exhaustion Seed Branch", "branch_code": always_same_code},
            headers=auth(auth_token),
        )
        assert pre.status_code == 201, f"Seed branch failed: {pre.text}"

        with patch.object(settings_service, "_generate_branch_code", lambda: always_same_code):
            resp = await client.post(
                "/settings/branches",
                json={"branch_name": "Should Exhaust Retries Branch"},
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
            json={"branch_name": "Headquarters"},
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
            json={"branch_name": ""},
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
        When _write_settings_audit raises inside update_company_profile, the
        UPDATE to core.companies must roll back — the name must be unchanged.
        """
        # Record the current name.
        before = (
            await client.get("/settings/company", headers=auth(auth_token))
        ).json()["company_name"]

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on company update")

        with patch.object(settings_service, "_write_settings_audit", _raise):
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
        When _write_settings_audit raises inside create_branch, the INSERT into
        core.branches must roll back — the branch must not appear in the list.
        """
        branch_name = "Rollback Test Branch Audit"

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure on branch create")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure on branch create"):
                await client.post(
                    "/settings/branches",
                    json={"branch_name": branch_name, "branch_code": "RTBA"},
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
        When _write_settings_audit raises inside update_branch, the UPDATE to
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

        with patch.object(settings_service, "_write_settings_audit", _raise):
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
