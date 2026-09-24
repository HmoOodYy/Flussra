"""Integration tests for payroll status keys and legacy setup authentication."""
import pytest
import pytest_asyncio
import httpx


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Session-scoped branch used by status-key tests.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def setup_branch_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
) -> int:
    """Create an Active branch isolated from payroll-entry test branches."""
    resp = await session_client.post(
        "/settings/branches",
        json={
            "branch_name": "Payroll Setup Test Branch",
            "branch_code": "PSTB",
            "status":      "Active",
            "is_default":  False,
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"Setup branch seed failed: {resp.text}"
    return resp.json()["branch_id"]


class TestGetPayrollSetup:

    async def test_unauthenticated_rejected(
        self,
        client: httpx.AsyncClient,
        setup_branch_id: int,
    ):
        response = await client.get(
            f"/settings/branches/{setup_branch_id}/payroll-setup"
        )
        assert response.status_code == 401


# Session-scoped fixture: one status key created for read/update/delete tests.

@pytest_asyncio.fixture(scope="session")
async def setup_branch_key_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    setup_branch_id: int,
) -> int:
    """Create one status key on the setup branch at session start."""
    resp = await session_client.post(
        f"/settings/branches/{setup_branch_id}/status-keys",
        json={
            "key_name": "Vacation",
            "hours_value": "8.00",
            "is_off_reason": True,
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"Status key seed failed: {resp.text}"
    return resp.json()["status_key_id"]


# ---------------------------------------------------------------------------
# TestGetStatusKeys
# ---------------------------------------------------------------------------

class TestGetStatusKeys:

    async def test_empty_list_on_new_branch(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """A brand-new branch has no status keys."""
        br = await client.post(
            "/settings/branches",
            json={"branch_name": "Empty Keys Branch 2", "branch_code": "EKB2"},
            headers=auth(auth_token),
        )
        assert br.status_code == 201
        bid = br.json()["branch_id"]

        resp = await client.get(
            f"/settings/branches/{bid}/status-keys",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_includes_created_key(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
        setup_branch_key_id: int,
    ):
        """After the seed fixture creates a key, it must appear in the list."""
        resp = await client.get(
            f"/settings/branches/{setup_branch_id}/status-keys",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        ids = [k["status_key_id"] for k in resp.json()]
        assert setup_branch_key_id in ids

    async def test_list_sorted_by_key_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Keys must come back sorted A-Z by key_name."""
        # Create three keys with names that would be out of order if sorted by creation time.
        for name in ["Zzz Last", "Aaa First", "Mmm Middle"]:
            await client.post(
                f"/settings/branches/{setup_branch_id}/status-keys",
                json={"key_name": name},
                headers=auth(auth_token),
            )
        resp = await client.get(
            f"/settings/branches/{setup_branch_id}/status-keys",
            headers=auth(auth_token),
        )
        names = [k["key_name"] for k in resp.json()]
        assert names == sorted(names, key=str.lower), (
            f"Keys not sorted alphabetically: {names}"
        )

    async def test_include_inactive_flag(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """
        Create an inactive key, verify it's absent by default and present
        when include_inactive=true.
        """
        create = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Temp Inactive Key", "hours_value": "0"},
            headers=auth(auth_token),
        )
        assert create.status_code == 201
        kid = create.json()["status_key_id"]

        await client.delete(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            headers=auth(auth_token),
        )

        # Default list must exclude it.
        active_resp = await client.get(
            f"/settings/branches/{setup_branch_id}/status-keys",
            headers=auth(auth_token),
        )
        assert kid not in [k["status_key_id"] for k in active_resp.json()]

        # With flag, must include it.
        all_resp = await client.get(
            f"/settings/branches/{setup_branch_id}/status-keys?include_inactive=true",
            headers=auth(auth_token),
        )
        assert kid in [k["status_key_id"] for k in all_resp.json()]

    async def test_branch_user_denied_other_branch(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        setup_branch_id: int,
    ):
        resp = await client.get(
            f"/settings/branches/{setup_branch_id}/status-keys",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_branch_user_can_read_hq_keys(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        """branch_user is scoped to HQ — reading HQ keys must succeed."""
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/status-keys",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestCreateStatusKey
# ---------------------------------------------------------------------------

class TestCreateStatusKey:

    async def test_create_with_key_name_only(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Only key_name is required; defaults apply to everything else."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "On Leave"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["key_name"]                         == "On Leave"
        assert float(body["hours_value"])               == 0.0
        assert body["is_off_reason"]                    is True
        assert body["deducts_from_yearly_allowance"]    is False
        assert body["allowance_category"]               is None
        assert body["is_active"]                        is True

    async def test_create_generates_sk_code(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Backend generates SK_XXXXXXXX status_code — not user-supplied."""
        import re
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Personal Day"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert re.fullmatch(r"SK_[A-Z0-9]{8}", body["status_code"]), (
            f"Expected SK_XXXXXXXX, got: {body['status_code']}"
        )

    async def test_two_keys_have_different_codes(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Every key gets a distinct generated status_code."""
        r1 = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Alpha Leave"},
            headers=auth(auth_token),
        )
        r2 = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Beta Leave"},
            headers=auth(auth_token),
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["status_code"] != r2.json()["status_code"]

    async def test_status_code_not_user_overridable(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Passing status_code in the payload is silently ignored."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Override Test", "status_code": "MANUAL_CODE"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        # The returned code must be SK_XXXXXXXX, not the user-provided one
        import re
        assert re.fullmatch(r"SK_[A-Z0-9]{8}", resp.json()["status_code"])

    async def test_create_full_key(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """deducts_from_yearly_allowance is guarded — full payload without it succeeds."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={
                "key_name":      "Sick Day",
                "hours_value":   "8.00",
                "is_off_reason": True,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["key_name"]                         == "Sick Day"
        assert float(body["hours_value"])               == 8.0
        assert body["is_off_reason"]                    is True
        assert body["deducts_from_yearly_allowance"]    is False
        assert body["allowance_category"]               is None
        assert body["is_active"]                        is True

    async def test_hours_above_24_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Overtime", "hours_value": "25"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_hours_zero_allowed(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Zero Hours Day", "hours_value": "0"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        assert float(resp.json()["hours_value"]) == 0.0

    async def test_deducts_true_rejected_future_guard(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """deducts_from_yearly_allowance=true is rejected by the future-only guard."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={
                "key_name": "Deduct No Cat",
                "is_off_reason": True,
                "deducts_from_yearly_allowance": True,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "not available yet" in str(detail).lower()

    async def test_invalid_allowance_category_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """allowance_category schema validator rejects unknown values regardless of deducts."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={
                "key_name": "Bad Cat",
                "is_off_reason": True,
                "deducts_from_yearly_allowance": False,
                "allowance_category": "Weekend",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_blank_key_name_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "   "},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_missing_key_name_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Payload with no key_name at all must be rejected."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"hours_value": "8"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_branch_user_denied(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        setup_branch_id: int,
    ):
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "BranchUserAttempt"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestStatusKeyUsageLimits
# ---------------------------------------------------------------------------

class TestStatusKeyUsageLimits:
    """Usage limits are stored and returned correctly; enforcement is deferred."""

    async def test_create_with_usage_limits(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={
                "key_name": "Limited Key",
                "limit_uses_per_period_enabled": True,
                "limit_uses_per_period": 5,
                "limit_uses_per_driver_enabled": True,
                "limit_uses_per_driver": 2,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["limit_uses_per_period_enabled"]    is True
        assert body["limit_uses_per_period"]            == 5
        assert body["limit_uses_per_driver_enabled"]    is True
        assert body["limit_uses_per_driver"]            == 2
        assert body["limit_uses_across_drivers_enabled"] is False
        assert body["limit_uses_per_day_enabled"]       is False

    async def test_create_limit_enabled_zero_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Enabled limit with value 0 must be rejected."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={
                "key_name": "Bad Limit",
                "limit_uses_per_period_enabled": True,
                "limit_uses_per_period": 0,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_limit_enabled_negative_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={
                "key_name": "Neg Limit",
                "limit_uses_per_day_enabled": True,
                "limit_uses_per_day": -1,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_limit_disabled_null_ok(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Disabled limit with null value is valid."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={
                "key_name": "No Limit Key",
                "limit_uses_per_period_enabled": False,
                "limit_uses_per_period": None,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["limit_uses_per_period"] is None

    async def test_patch_updates_usage_limits(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """PATCH can enable a usage limit after creation."""
        create = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Patchable Limit Key"},
            headers=auth(auth_token),
        )
        kid = create.json()["status_key_id"]

        patch_resp = await client.patch(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            json={
                "limit_uses_across_drivers_enabled": True,
                "limit_uses_across_drivers": 10,
            },
            headers=auth(auth_token),
        )
        assert patch_resp.status_code == 200
        body = patch_resp.json()
        assert body["limit_uses_across_drivers_enabled"] is True
        assert body["limit_uses_across_drivers"]         == 10

    async def test_get_returns_usage_limits(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """GET list returns usage limit fields for each key."""
        resp = await client.get(
            f"/settings/branches/{setup_branch_id}/status-keys",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        for key in resp.json():
            assert "limit_uses_per_period_enabled"    in key
            assert "limit_uses_per_driver_enabled"    in key
            assert "limit_uses_across_drivers_enabled" in key
            assert "limit_uses_per_day_enabled"        in key

    async def test_patch_limit_enabled_zero_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """PATCH must reject enabling a limit with value 0."""
        create = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "BadPatch Limit"},
            headers=auth(auth_token),
        )
        kid = create.json()["status_key_id"]

        resp = await client.patch(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            json={"limit_uses_per_day_enabled": True, "limit_uses_per_day": 0},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TestUpdateStatusKey
# ---------------------------------------------------------------------------

class TestUpdateStatusKey:

    async def test_patch_hours(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
        setup_branch_key_id: int,
    ):
        """Patch only hours_value; all other fields must be unchanged."""
        before = (
            await client.get(
                f"/settings/branches/{setup_branch_id}/status-keys",
                headers=auth(auth_token),
            )
        ).json()
        before_key = next(k for k in before if k["status_key_id"] == setup_branch_key_id)

        resp = await client.patch(
            f"/settings/branches/{setup_branch_id}/status-keys/{setup_branch_key_id}",
            json={"hours_value": "6.50"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert float(body["hours_value"]) == 6.5
        # Everything else preserved.
        assert body["key_name"]       == before_key["key_name"]
        assert body["is_off_reason"]  == before_key["is_off_reason"]
        assert body["is_active"]      == before_key["is_active"]

    async def test_patch_key_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Patch key_name updates the user-facing label; status_code is unchanged."""
        create = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Original Name"},
            headers=auth(auth_token),
        )
        kid = create.json()["status_key_id"]
        old_code = create.json()["status_code"]

        resp = await client.patch(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            json={"key_name": "Updated Name"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["key_name"]    == "Updated Name"
        # Internal code is immutable
        assert resp.json()["status_code"] == old_code

    async def test_patch_not_found(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        resp = await client.patch(
            f"/settings/branches/{setup_branch_id}/status-keys/999999",
            json={"hours_value": "4"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_branch_user_denied(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        setup_branch_id: int,
        setup_branch_key_id: int,
    ):
        resp = await client.patch(
            f"/settings/branches/{setup_branch_id}/status-keys/{setup_branch_key_id}",
            json={"hours_value": "1"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestDeleteStatusKey
# ---------------------------------------------------------------------------

class TestDeleteStatusKey:

    async def test_soft_delete(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Create a key, delete it, verify is_active=False."""
        create = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "To Be Deleted", "hours_value": "0"},
            headers=auth(auth_token),
        )
        assert create.status_code == 201
        kid = create.json()["status_key_id"]

        resp = await client.delete(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        assert resp.json()["status_key_id"] == kid

    async def test_delete_excluded_from_default_list(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """A deactivated key must not appear in the default (active-only) list."""
        create = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Will Be Gone", "hours_value": "0"},
            headers=auth(auth_token),
        )
        assert create.status_code == 201
        kid = create.json()["status_key_id"]

        await client.delete(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            headers=auth(auth_token),
        )

        active_ids = [
            k["status_key_id"]
            for k in (
                await client.get(
                    f"/settings/branches/{setup_branch_id}/status-keys",
                    headers=auth(auth_token),
                )
            ).json()
        ]
        assert kid not in active_ids

    async def test_delete_idempotent(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """Calling DELETE on an already-inactive key returns 200 unchanged."""
        create = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Idempotent Del", "hours_value": "0"},
            headers=auth(auth_token),
        )
        assert create.status_code == 201
        kid = create.json()["status_key_id"]

        r1 = await client.delete(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            headers=auth(auth_token),
        )
        assert r1.status_code == 200

        r2 = await client.delete(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            headers=auth(auth_token),
        )
        assert r2.status_code == 200
        assert r2.json()["is_active"] is False

    async def test_delete_not_found(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        resp = await client.delete(
            f"/settings/branches/{setup_branch_id}/status-keys/999999",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_delete_branch_user_denied(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        setup_branch_id: int,
        setup_branch_key_id: int,
    ):
        resp = await client.delete(
            f"/settings/branches/{setup_branch_id}/status-keys/{setup_branch_key_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_deleted_key_can_reuse_same_name(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """
        After a key is deactivated, a new key with the same key_name may be
        created (no uniqueness constraint on key_name).  Each will get its own
        distinct SK_ code.
        """
        create = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Reusable Name", "hours_value": "4"},
            headers=auth(auth_token),
        )
        assert create.status_code == 201
        first_code = create.json()["status_code"]
        kid = create.json()["status_key_id"]

        await client.delete(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            headers=auth(auth_token),
        )

        reuse = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "Reusable Name", "hours_value": "4"},
            headers=auth(auth_token),
        )
        assert reuse.status_code == 201
        # New SK_ code is different from the old one
        assert reuse.json()["status_code"] != first_code
        assert reuse.json()["key_name"] == "Reusable Name"


# ---------------------------------------------------------------------------
# TestYearlyAllowanceGuard
# ---------------------------------------------------------------------------

class TestYearlyAllowanceGuard:
    """
    deducts_from_yearly_allowance is a future-only feature.
    Creating or updating a status key with deducts_from_yearly_allowance=true
    must be rejected with 422.  Setting it to false (or omitting it) must succeed.
    """

    async def test_create_with_deducts_true_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """POST with deducts_from_yearly_allowance=true → 422."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={
                "key_name": "YA Guard Create True",
                "is_off_reason": True,
                "deducts_from_yearly_allowance": True,
                "allowance_category": "Vacation",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail = str(resp.json()["detail"]).lower()
        assert "not available yet" in detail

    async def test_create_with_deducts_false_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """POST with deducts_from_yearly_allowance=false (explicit) → 201."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={
                "key_name": "YA Guard Create False",
                "is_off_reason": True,
                "deducts_from_yearly_allowance": False,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["deducts_from_yearly_allowance"] is False

    async def test_create_omitting_deducts_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """POST without deducts_from_yearly_allowance → 201 with default False."""
        resp = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "YA Guard Create Omit"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["deducts_from_yearly_allowance"] is False

    async def test_patch_deducts_true_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """PATCH with deducts_from_yearly_allowance=true → 422."""
        create = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "YA Guard Patch Target", "is_off_reason": True},
            headers=auth(auth_token),
        )
        assert create.status_code == 201
        kid = create.json()["status_key_id"]

        resp = await client.patch(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            json={"deducts_from_yearly_allowance": True, "allowance_category": "Sick"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail = str(resp.json()["detail"]).lower()
        assert "not available yet" in detail

    async def test_patch_deducts_false_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        setup_branch_id: int,
    ):
        """PATCH with deducts_from_yearly_allowance=false (explicit no-op) → 200."""
        create = await client.post(
            f"/settings/branches/{setup_branch_id}/status-keys",
            json={"key_name": "YA Guard Patch False", "is_off_reason": True},
            headers=auth(auth_token),
        )
        assert create.status_code == 201
        kid = create.json()["status_key_id"]

        resp = await client.patch(
            f"/settings/branches/{setup_branch_id}/status-keys/{kid}",
            json={"deducts_from_yearly_allowance": False},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["deducts_from_yearly_allowance"] is False
