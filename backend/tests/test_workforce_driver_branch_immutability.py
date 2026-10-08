"""Driver branch immutability under role assignment (Workforce-owned identity).

Restored from the retired Phase-2C additions: the contract is independent of the
PayItem/DriverRate runtime and stays live across the P4b cutover.
"""
from uuid import uuid4

import httpx
import pytest

from tests.access_test_helpers import create_neutral_test_user
from tests.builders.workforce import create_driver_employee_record

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_user(
    client: httpx.AsyncClient,
    token: str,
    username: str,
    display_name: str = "Test User",
) -> int:
    user = await create_neutral_test_user(
        client, token, username, password="TestPass1234!",
        display_name=display_name,
    )
    return user["user_id"]


async def _get_driver_role_id(client: httpx.AsyncClient, token: str) -> int | None:
    resp = await client.get("/admin/company-roles", headers=auth(token))
    for r in resp.json():
        if r.get("role_code") == "DRIVER":
            return r["company_role_id"]
    return None


async def _make_driver(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    name_suffix: str = "",
) -> int:
    suffix = name_suffix or uuid4().hex[:8]
    record = await create_driver_employee_record(
        client, token, branch_id=branch_id,
        full_name=f"Phase2C Driver {suffix}",
        driver_code=f"P2C-{suffix}-{uuid4().hex[:8]}",
    )
    return int(record["current_or_pending_driver"]["driver_id"])


async def _link_driver_profile(
    client: httpx.AsyncClient, token: str, user_id: int, branch_id: int, label: str,
) -> int:
    record = await create_driver_employee_record(
        client, token, branch_id=branch_id,
        full_name=f"Phase2C {label}",
        driver_code=f"P2C-{uuid4().hex[:12]}",
    )
    response = await client.put(
        f"/admin/users/{user_id}/employee-link",
        json={"employee_id": record["employee_id"]},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    return int(record["current_or_pending_driver"]["driver_id"])


# ===========================================================================
# Driver branch immutability
# ===========================================================================

class TestDriverBranchImmutability:
    """
    DRIVER uses a Workforce-owned Self identity; legacy branch-scoped
    assignments cannot mutate the linked Driver profile.
    """

    @pytest.mark.asyncio
    async def test_role_reassignment_rejects_driver_branch_movement(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        1. Create user and Workforce profile on branch 1 (HQ).
        2. Link the Employee and assign DRIVER with Self scope.
        3. Attempt a legacy branch-scoped assignment and require 422.
        4. Verify the existing DriverID and HQ branch remain unchanged.
        """
        import random
        username = f"bsync_{random.randint(10000, 99999)}"
        user_id = await _create_user(session_client, auth_token, username, "Branch Sync Test")

        driver_role_id = await _get_driver_role_id(session_client, auth_token)
        if driver_role_id is None:
            pytest.skip("DRIVER company role not found")

        hq_branch_id = 1

        # Link a test-owned Workforce profile, then assign the valid DRIVER/Self role.
        original_driver_id = await _link_driver_profile(
            session_client, auth_token, user_id, hq_branch_id, "Branch Sync Test",
        )
        r1 = await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={"company_role_id": driver_role_id, "scope_type": "Self"},
            headers=auth(auth_token),
        )
        assert r1.status_code == 201, r1.text

        # Get driver info — should be on HQ branch
        info1 = await session_client.get(f"/admin/users/{user_id}/driver", headers=auth(auth_token))
        assert info1.status_code == 200
        data1 = info1.json()
        assert data1["has_driver_profile"] is True
        assert data1["driver_id"] == original_driver_id
        assert data1["branch_id"] == hq_branch_id

        # Attempt the retired branch-scoped assignment shape.
        r2 = await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={"company_role_id": driver_role_id,
                  "scope_type": "SpecificBranch", "branch_id": paytest_branch_id},
            headers=auth(auth_token),
        )
        assert r2.status_code == 422, r2.text
        assert "self scope" in r2.text.lower()

        # Verify the rejected role reassignment left the profile unchanged.
        info2 = await session_client.get(f"/admin/users/{user_id}/driver", headers=auth(auth_token))
        assert info2.status_code == 200
        data2 = info2.json()
        assert data2["branch_id"] == hq_branch_id

        # DriverID must be unchanged (no duplicate created)
        assert data2["driver_id"] == original_driver_id

    @pytest.mark.asyncio
    async def test_non_driver_role_no_profile_created(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Assigning a non-Driver company role must not create or mutate Workforce records.

        Strategy: create a custom company role (not DRIVER), assign it to a new user,
        then verify the user has no driver profile.

        The test DEMO company only seeds DRIVER and COMPANY_OWNER company roles, so
        we create a transient custom role for this test.
        """
        import random

        # Create a custom company role with a non-DRIVER name
        role_name = f"Non-Driver Role {random.randint(1000, 9999)}"
        cr_resp = await session_client.post(
            "/admin/company-roles",
            json={"role_name": role_name},
            headers=auth(auth_token),
        )
        assert cr_resp.status_code == 201, cr_resp.text
        custom_role_id = cr_resp.json()["company_role_id"]

        username = f"nondrv_{random.randint(10000, 99999)}"
        user_id = await _create_user(session_client, auth_token, username, "Non-Driver Test")

        # Assign the custom (non-driver) role
        assign_resp = await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={"company_role_id": custom_role_id,
                  "scope_type": "AllCompanyBranches", "branch_id": None},
            headers=auth(auth_token),
        )
        assert assign_resp.status_code == 201, assign_resp.text

        # Admin checks driver info for the new user — should have no driver profile
        info = await session_client.get(f"/admin/users/{user_id}/driver", headers=auth(auth_token))
        assert info.status_code == 200
        assert info.json()["has_driver_profile"] is False, (
            "Assigning a non-DRIVER role must not create a driver profile"
        )
