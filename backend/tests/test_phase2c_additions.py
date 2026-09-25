"""
tests/test_phase2c_additions.py — Phase 2C tests

Covers:
  TestDriverBranchSync     — branch sync bug fix (ensure_driver_profile)
  TestCopyRates            — copy-from endpoint
  TestBulkDriverSummary    — bulk rates-summary endpoint
  TestPayRulesSecurity     — pay rules now use payrates.view/edit
"""
from datetime import date

import httpx
import pytest

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
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["user_id"]


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
    import random
    suffix = name_suffix or str(random.randint(1000, 9999))
    resp = await client.post(
        "/core/drivers",
        json={
            "branch_id": branch_id,
            "full_name": f"Phase2C Driver {suffix}",
            "driver_code": f"P2C-{suffix}",
        },
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["driver_id"]


# ===========================================================================
# Part A — Branch Sync
# ===========================================================================

class TestDriverBranchSync:
    """
    ensure_driver_profile now syncs core.Drivers.BranchID and
    core.Employees.BranchID when a Driver role is re-assigned to a
    different branch.
    """

    @pytest.mark.asyncio
    async def test_branch_sync_on_reassignment(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        1. Create user + assign DRIVER role on branch 1 (HQ).
        2. Verify driver profile → branch 1.
        3. Reassign to PAYTEST branch.
        4. Verify driver profile → PAYTEST branch.
        5. No duplicate driver row.
        6. DriverID unchanged.
        """
        import random
        username = f"bsync_{random.randint(10000, 99999)}"
        user_id = await _create_user(session_client, auth_token, username, "Branch Sync Test")

        driver_role_id = await _get_driver_role_id(session_client, auth_token)
        if driver_role_id is None:
            pytest.skip("DRIVER company role not found")

        hq_branch_id = 1

        # Assign to HQ
        r1 = await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={"company_role_id": driver_role_id,
                  "scope_type": "SpecificBranch", "branch_id": hq_branch_id},
            headers=auth(auth_token),
        )
        assert r1.status_code == 201, r1.text

        # Get driver info — should be on HQ branch
        info1 = await session_client.get(f"/admin/users/{user_id}/driver", headers=auth(auth_token))
        assert info1.status_code == 200
        data1 = info1.json()
        assert data1["has_driver_profile"] is True
        original_driver_id = data1["driver_id"]
        assert data1["branch_id"] == hq_branch_id

        # Reassign to PAYTEST branch
        r2 = await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={"company_role_id": driver_role_id,
                  "scope_type": "SpecificBranch", "branch_id": paytest_branch_id},
            headers=auth(auth_token),
        )
        assert r2.status_code == 201, r2.text

        # Verify branch updated
        info2 = await session_client.get(f"/admin/users/{user_id}/driver", headers=auth(auth_token))
        assert info2.status_code == 200
        data2 = info2.json()
        assert data2["branch_id"] == paytest_branch_id, (
            f"Driver profile branch should be {paytest_branch_id}, got {data2['branch_id']}"
        )

        # DriverID must be unchanged (no duplicate created)
        assert data2["driver_id"] == original_driver_id

    @pytest.mark.asyncio
    async def test_non_driver_role_no_profile_created(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Assigning a non-Driver company role must not trigger ensure_driver_profile.

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

    @pytest.mark.asyncio
    async def test_pay_rates_matrix_uses_new_branch(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """Rate matrix reflects the driver's new branch after sync."""
        import random
        username = f"matbsync_{random.randint(10000, 99999)}"
        user_id = await _create_user(session_client, auth_token, username, "Matrix Branch Sync")

        driver_role_id = await _get_driver_role_id(session_client, auth_token)
        if driver_role_id is None:
            pytest.skip("DRIVER company role not found")

        hq_branch_id = 1

        await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={"company_role_id": driver_role_id,
                  "scope_type": "SpecificBranch", "branch_id": hq_branch_id},
            headers=auth(auth_token),
        )
        info = await session_client.get(f"/admin/users/{user_id}/driver", headers=auth(auth_token))
        driver_id = info.json()["driver_id"]

        # Reassign to paytest
        await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={"company_role_id": driver_role_id,
                  "scope_type": "SpecificBranch", "branch_id": paytest_branch_id},
            headers=auth(auth_token),
        )

        matrix_resp = await session_client.get(
            f"/payroll/drivers/{driver_id}/rate-matrix",
            params={"as_of": date.today().isoformat()},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200
        assert matrix_resp.json()["branch_id"] == paytest_branch_id


# ===========================================================================
# Part C — Copy Rates
# ===========================================================================

class TestCopyRates:
    """POST /payroll/drivers/{target}/rates/copy-from/{source}"""

    @pytest.mark.asyncio
    async def test_copy_rates_same_branch(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """Copy approved rates between two drivers on the same branch."""
        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, "CPY-SRC")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, "CPY-TGT")

        # Create and approve a rate on source
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage_rt = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage_rt is None:
            pytest.skip("MILEAGE rate type not available")

        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage_rt["rate_type_id"],
                  "amount": "3.11", "effective_from": "2076-01-01"},
            headers=auth(auth_token),
        )
        if cr.status_code != 201:
            pytest.skip(f"Could not create test rate: {cr.text}")
        rate_id = cr.json()["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rate_id}/approve", headers=auth(auth_token))

        # Copy
        resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2076-06-01", "include_pay_rules": False},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["source_driver_id"] == src_id
        assert data["target_driver_id"] == tgt_id
        assert data["rates_copied"] >= 1

    @pytest.mark.asyncio
    async def test_copy_rates_unknown_source_404(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """Non-existent source driver → 404."""
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, "CPY-404TGT")
        resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/999996",
            json={"effective_from": "2076-06-01"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_copy_rates_unknown_target_404(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """Non-existent target driver → 404."""
        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, "CPY-404SRC")
        resp = await session_client.post(
            f"/payroll/drivers/999995/rates/copy-from/{src_id}",
            json={"effective_from": "2076-06-01"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_copy_rates_no_approved_source_rates_zero_copied(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """Source with no approved rates → rates_copied=0, not error."""
        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, "CPY-NOSRC")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, "CPY-NOTGT")
        resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2076-06-01"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["rates_copied"] == 0

    @pytest.mark.asyncio
    async def test_copy_rates_requires_payrates_edit(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        User with no payrates.edit permission gets 403.

        Uses the seeded 'branch_user' (PAYROLL_VIEWER + DRIVER company role,
        SpecificBranch HQ scope, password 'TestPass123!').
        branch_user has no payrates.edit or payrates.view permission.
        """
        login = await session_client.post(
            "/auth/login",
            json={"company_code": "DEMO", "username": "branch_user", "password": "TestPass123!"},
        )
        assert login.status_code == 200, f"branch_user login failed: {login.text}"
        np_token = login.json()["access_token"]

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, "CPY-NPERM")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, "CPY-NPERM2")
        resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2076-06-01"},
            headers=auth(np_token),
        )
        assert resp.status_code == 403


# ===========================================================================
# Part D — Bulk Summary
# ===========================================================================

class TestBulkDriverSummary:
    """GET /payroll/drivers/rates-summary"""

    @pytest.mark.asyncio
    async def test_bulk_summary_returns_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Admin receives a list."""
        resp = await session_client.get("/payroll/drivers/rates-summary", headers=auth(auth_token))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_bulk_summary_shape(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """Each entry has the required fields."""
        resp = await session_client.get("/payroll/drivers/rates-summary", headers=auth(auth_token))
        assert resp.status_code == 200
        data = resp.json()
        if not data:
            pytest.skip("No drivers")
        entry = data[0]
        for field in ("driver_id", "pending_count", "future_approved_count"):
            assert field in entry, f"Missing field: {field}"

    @pytest.mark.asyncio
    async def test_bulk_summary_no_permission_403(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        User with no payrates.view permission gets 403.

        Uses seeded 'branch_user' (PAYROLL_VIEWER + DRIVER company role,
        SpecificBranch HQ scope, password 'TestPass123!').
        branch_user has no payrates.view/edit permission, so the
        _check_any_permission call in get_bulk_driver_rates_summary raises 403.
        """
        login = await session_client.post(
            "/auth/login",
            json={"company_code": "DEMO", "username": "branch_user", "password": "TestPass123!"},
        )
        assert login.status_code == 200, f"branch_user login failed: {login.text}"
        np_token = login.json()["access_token"]
        resp = await session_client.get("/payroll/drivers/rates-summary", headers=auth(np_token))
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_bulk_summary_branch_filter_accepted(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """branch_id query param is accepted and returns 200."""
        resp = await session_client.get(
            "/payroll/drivers/rates-summary",
            params={"branch_id": paytest_branch_id},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_bulk_summary_pending_count_accurate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        After creating a PendingApproval rate, the driver's entry in bulk summary
        should show pending_count >= 1.
        """
        # Create a pending rate (far future to avoid backdating issues)
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY not found")

        cr_resp = await session_client.post(
            "/payroll/rates",
            json={"driver_id": paytest_driver_id, "rate_type_id": hourly["rate_type_id"],
                  "amount": "5.55", "effective_from": "2094-01-01"},
            headers=auth(auth_token),
        )
        if cr_resp.status_code != 201:
            pytest.skip(f"Could not create pending rate: {cr_resp.text}")
        rate_id = cr_resp.json()["driver_rate_id"]

        try:
            bulk = await session_client.get("/payroll/drivers/rates-summary", headers=auth(auth_token))
            assert bulk.status_code == 200
            driver_entry = next(
                (e for e in bulk.json() if e["driver_id"] == paytest_driver_id), None
            )
            assert driver_entry is not None, "paytest_driver_id not in bulk summary"
            assert driver_entry["pending_count"] >= 1
        finally:
            await session_client.delete(f"/payroll/rates/{rate_id}", headers=auth(auth_token))


# ===========================================================================
# Part B — Pay Rules Security Fix
# ===========================================================================

class TestPayRulesSecurity:
    """
    Pay Rules now use payrates.view for reads and payrates.edit for writes
    (previously payroll.entry / setup.manage).
    ODA scope check is also applied.
    """

    @pytest.mark.asyncio
    async def test_list_pay_rules_admin_200(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """Admin with settings.manage (includes payrates.view) gets 200."""
        resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/pay-rules",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_list_pay_rules_no_perm_403(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """
        User with no payrates.view permission gets 403.

        Uses seeded 'branch_user' (no payrates.* permissions).
        """
        login = await session_client.post(
            "/auth/login",
            json={"company_code": "DEMO", "username": "branch_user", "password": "TestPass123!"},
        )
        assert login.status_code == 200, f"branch_user login failed: {login.text}"
        np_token = login.json()["access_token"]

        resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/pay-rules",
            headers=auth(np_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_create_pay_rule_admin_201(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """Admin can create a pay rule (payrates.edit via settings.manage)."""
        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id": paytest_driver_id,
                "rule_type": "MinimumPay",
                "amount": "150.00",
                "effective_from": "2086-01-01",
                "effective_to": "2086-12-31",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["rule_type"] == "MinimumPay"
        assert data["status"] == "Active"

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{data['driver_pay_rule_id']}/void",
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_create_pay_rule_no_perm_403(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        User with no payrates.edit permission cannot create pay rules.

        Uses seeded 'branch_user' (no payrates.* permissions).
        """
        login = await session_client.post(
            "/auth/login",
            json={"company_code": "DEMO", "username": "branch_user", "password": "TestPass123!"},
        )
        assert login.status_code == 200, f"branch_user login failed: {login.text}"
        np_token = login.json()["access_token"]

        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id": paytest_driver_id,
                "rule_type": "MinimumPay",
                "amount": "100.00",
                "effective_from": "2087-01-01",
            },
            headers=auth(np_token),
        )
        assert resp.status_code == 403
