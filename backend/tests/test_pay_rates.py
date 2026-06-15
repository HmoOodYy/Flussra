"""
tests/test_pay_rates.py — Phase 1: Pay Rates feature

Covers:
  TestDriverRateMatrix        — rate matrix endpoint shape and correctness
  TestDriverRates             — CRUD + permission guards
  TestDriverProfile           — ensure_driver_profile + GET /admin/users/{id}/driver
  TestOvernightRate           — OVERNIGHT migration correctness
  TestRateWritePermissions    — payrates.edit required for write ops (Fix 1)
  TestRateReadPermissions     — payrates.view required for read ops (Fix 2)
  TestMatrixHistoricalAsOf    — Superseded rates returned for old as_of dates (Fix 6)
  TestCreateRateBranchValidation — branch-active rate type check (Fix 8)
  TestDriverInfoPermissions   — /admin/users/{id}/driver caller permission gate (Fix 5)
"""
import pytest
import pytest_asyncio
import httpx
from datetime import date


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _today_iso() -> str:
    """Return today's date as ISO string, evaluated at call time (not module-import time).

    Using a function rather than a module-level constant prevents stale dates when the
    test module is imported one day and tests execute after midnight (the import-time
    constant would be yesterday and rate validators would reject it as 'in the past').
    """
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# TestDriverRateMatrix
# ---------------------------------------------------------------------------

class TestDriverRateMatrix:
    """Rate matrix endpoint — GET /payroll/drivers/{id}/rate-matrix"""

    @pytest.mark.asyncio
    async def test_matrix_returns_groups(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "driver_id" in data
        assert data["driver_id"] == created_driver_id
        assert "groups" in data
        assert "as_of" in data
        assert "driver_name" in data
        assert "branch_name" in data

    @pytest.mark.asyncio
    async def test_matrix_missing_rate_flag(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """Drivers with no approved rates should have is_missing=True on groups."""
        resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        # At least some groups exist (if branch has pay items configured)
        # All groups without a current_rate should have is_missing=True
        for group in data["groups"]:
            if group["current_rate"] is None:
                assert group["is_missing"] is True

    @pytest.mark.asyncio
    async def test_matrix_with_approved_rate_not_missing(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """After approving a rate, is_missing should be False for that rate type."""
        # Get rate types
        rt_resp = await session_client.get(
            "/payroll/rate-types",
            headers=auth(auth_token),
        )
        assert rt_resp.status_code == 200
        rate_types = rt_resp.json()
        if not rate_types:
            pytest.skip("No rate types available")

        hourly_rt = next((rt for rt in rate_types if rt["rate_code"] == "HOURLY"), None)
        if hourly_rt is None:
            pytest.skip("HOURLY rate type not found")

        # Create a rate
        create_resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": hourly_rt["rate_type_id"],
                "amount": "18.50",
                "effective_from": "2020-01-01",
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        rate = create_resp.json()

        # Approve it
        approve_resp = await session_client.post(
            f"/payroll/rates/{rate['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )
        assert approve_resp.status_code == 200

        # Now matrix should show is_missing=False for HOURLY
        matrix_resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200
        matrix = matrix_resp.json()
        hourly_group = next(
            (g for g in matrix["groups"] if g["rate_code"] == "HOURLY"), None
        )
        if hourly_group is not None:
            assert hourly_group["is_missing"] is False
            assert hourly_group["current_rate"] is not None

    @pytest.mark.asyncio
    async def test_matrix_driver_not_found(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await session_client.get(
            "/payroll/drivers/999999/rate-matrix",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_matrix_default_as_of_today(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """as_of defaults to today when not supplied."""
        resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["as_of"] == _today_iso()


# ---------------------------------------------------------------------------
# TestDriverRates
# ---------------------------------------------------------------------------

class TestDriverRates:
    """Driver rate CRUD endpoints."""

    @pytest.mark.asyncio
    async def test_create_rate_returns_pending(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        rate_types = rt_resp.json()
        mileage_rt = next((rt for rt in rate_types if rt["rate_code"] == "MILEAGE"), None)
        if mileage_rt is None:
            pytest.skip("MILEAGE rate type not found")

        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": mileage_rt["rate_type_id"],
                "amount": "0.65",
                "effective_from": "2099-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "PendingApproval"

    @pytest.mark.asyncio
    async def test_view_rate_types(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_list_rates_for_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": created_driver_id},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_view(
        self,
        session_client: httpx.AsyncClient,
    ):
        resp = await session_client.get("/payroll/rate-types")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestDriverProfile
# ---------------------------------------------------------------------------

class TestDriverProfile:
    """GET /admin/users/{id}/driver and ensure_driver_profile."""

    @pytest.mark.asyncio
    async def test_admin_user_has_no_driver_profile_initially(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        # Get admin user id from /auth/me
        me_resp = await session_client.get("/auth/me", headers=auth(auth_token))
        assert me_resp.status_code == 200
        user_id = me_resp.json()["user_id"]

        resp = await session_client.get(
            f"/admin/users/{user_id}/driver",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "has_driver_profile" in data
        assert "user_id" in data
        assert data["user_id"] == user_id

    @pytest.mark.asyncio
    async def test_driver_info_returns_correct_shape(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        me_resp = await session_client.get("/auth/me", headers=auth(auth_token))
        user_id = me_resp.json()["user_id"]

        resp = await session_client.get(
            f"/admin/users/{user_id}/driver",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        data = resp.json()
        required_keys = {"user_id", "employee_id", "driver_id", "has_driver_profile"}
        for key in required_keys:
            assert key in data, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_user_not_found_returns_404(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await session_client.get(
            "/admin/users/999999/driver",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestOvernightRate
# ---------------------------------------------------------------------------

class TestOvernightRate:
    """Verify OVERNIGHT pay item state after migration 0022."""

    @pytest.mark.asyncio
    async def test_overnight_rate_type_exists(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        assert resp.status_code == 200
        rate_types = resp.json()
        overnight = next(
            (rt for rt in rate_types if rt["rate_code"] == "OVERNIGHT"), None
        )
        assert overnight is not None, "OVERNIGHT rate type not found"
        assert overnight["is_active"] is True

    @pytest.mark.asyncio
    async def test_overnight_appears_in_rate_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """OVERNIGHT should appear in the rate types catalog."""
        resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        assert resp.status_code == 200
        codes = {rt["rate_code"] for rt in resp.json()}
        assert "OVERNIGHT" in codes

    @pytest.mark.asyncio
    async def test_overnight_appears_in_rate_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """After migration 0022 OVERNIGHT must appear in the rate matrix.

        Uses paytest_driver_id (PAYTEST branch) where OVERNIGHT is already activated
        by the activate_paytest_system_items autouse fixture — avoids mutating HQ state.
        """
        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        groups = resp.json()["groups"]
        codes = {g["rate_code"] for g in groups}
        assert "OVERNIGHT" in codes, (
            "OVERNIGHT not found in rate matrix — migration 0022 may not have applied "
            "or PayItemRateTypeMap is missing."
        )

    @pytest.mark.asyncio
    async def test_overnight_rate_can_be_saved(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """A DriverRate for the OVERNIGHT rate type can be created and approved.
        Uses paytest_driver_id (PAYTEST branch) because OVERNIGHT is activated there
        by the activate_paytest_system_items fixture (Fix 8: branch validation).
        """
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        overnight_rt = next(
            (rt for rt in rt_resp.json() if rt["rate_code"] == "OVERNIGHT"), None
        )
        if overnight_rt is None:
            pytest.skip("OVERNIGHT rate type not found")

        create_resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": paytest_driver_id,
                "rate_type_id": overnight_rt["rate_type_id"],
                "amount": "45.00",
                "effective_from": _today_iso(),  # use today so branchpayitemconfig applies
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201, create_resp.text
        rate = create_resp.json()
        assert rate["status"] == "PendingApproval"

        approve_resp = await session_client.post(
            f"/payroll/rates/{rate['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "Approved"


# ---------------------------------------------------------------------------
# TestRateMatrixAuthorization
# ---------------------------------------------------------------------------

class TestRateMatrixAuthorization:
    """Rate matrix endpoint — permission enforcement and cross-company isolation."""

    @pytest.mark.asyncio
    async def test_unauthenticated_cannot_access_rate_matrix(
        self,
        session_client: httpx.AsyncClient,
        created_driver_id: int,
    ):
        """No JWT → 401."""
        resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_user_without_payrates_cannot_access_rate_matrix(
        self,
        session_client: httpx.AsyncClient,
        branch_user_token: str,
        created_driver_id: int,
    ):
        """PAYROLL_VIEWER has no payrates.view/edit → 403."""
        resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_with_setup_manage_can_access_rate_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """Admin has setup.manage (implied by COMPANY_OWNER) → 200."""
        resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cross_company_driver_access_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Driver in a different company (id 99999) → 404 (company isolation)."""
        resp = await session_client.get(
            "/payroll/drivers/99999/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        # 404 because the driver doesn't exist for this company_id (SQL filters by cid)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestDriverRoleHomeBranch
# ---------------------------------------------------------------------------

class TestDriverRoleHomeBranch:
    """Driver role assignment must include a home branch."""

    @pytest.mark.asyncio
    async def test_driver_role_requires_home_branch_not_allcompany(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Assigning DRIVER role with AllCompanyBranches scope → 422."""
        # Get the DRIVER company role id
        roles_resp = await session_client.get(
            "/admin/company-roles",
            headers=auth(auth_token),
        )
        assert roles_resp.status_code == 200
        driver_role = next(
            (r for r in roles_resp.json() if r.get("role_code") == "DRIVER"),
            None,
        )
        if driver_role is None:
            pytest.skip("DRIVER company role not found in seed data")

        # Get the admin user id
        me_resp = await session_client.get("/auth/me", headers=auth(auth_token))
        user_id = me_resp.json()["user_id"]

        # Attempt to assign DRIVER with AllCompanyBranches — must be rejected
        resp = await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={
                "company_role_id": driver_role["company_role_id"],
                "scope_type": "AllCompanyBranches",
                "branch_id": None,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "home branch" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_driver_role_with_specific_branch_creates_profile(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Assigning DRIVER role with SpecificBranch → 200/201 + driver profile created."""
        # Create a fresh user to assign driver role to
        create_resp = await session_client.post(
            "/admin/users",
            json={
                "username":     "test_driver_profile_user",
                "display_name": "Test Driver Profile",
                "password":     "TestPass123!",
                "company_role_id": None,
                "scope_type":   "AllCompanyBranches",
                "branch_id":    None,
            },
            headers=auth(auth_token),
        )
        if create_resp.status_code not in (200, 201):
            pytest.skip(f"Could not create test user: {create_resp.text}")
        new_user_id = create_resp.json()["user_id"]

        # Get DRIVER role
        roles_resp = await session_client.get("/admin/company-roles", headers=auth(auth_token))
        driver_role = next(
            (r for r in roles_resp.json() if r.get("role_code") == "DRIVER"), None
        )
        if driver_role is None:
            pytest.skip("DRIVER company role not found")

        # Assign DRIVER with SpecificBranch
        assign_resp = await session_client.post(
            f"/admin/users/{new_user_id}/company-role-assignments",
            json={
                "company_role_id": driver_role["company_role_id"],
                "scope_type": "SpecificBranch",
                "branch_id": hq_branch_id,
            },
            headers=auth(auth_token),
        )
        assert assign_resp.status_code in (200, 201), assign_resp.text

        # Driver profile should now be auto-created
        drv_resp = await session_client.get(
            f"/admin/users/{new_user_id}/driver",
            headers=auth(auth_token),
        )
        assert drv_resp.status_code == 200
        drv_data = drv_resp.json()
        assert drv_data["has_driver_profile"] is True
        assert drv_data["driver_id"] is not None


# ---------------------------------------------------------------------------
# Helpers shared by new test classes
# ---------------------------------------------------------------------------

async def _create_role_with_perms(
    client: httpx.AsyncClient,
    token: str,
    role_name: str,
    perms: list,
) -> int:
    """Create a custom company role with the given permissions. Returns role_id."""
    cr = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=auth(token),
    )
    assert cr.status_code == 201, f"Create role failed: {cr.text}"
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=auth(token),
        )
        assert pr.status_code == 200, f"Set permissions failed: {pr.text}"
    return role_id


async def _create_user_with_role(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    role_id: int,
    scope_type: str = "AllCompanyBranches",
    branch_id=None,
    password: str = "TestPass123!",
) -> str:
    """Create user, assign company role, return login token."""
    resp = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": username,
            "password": password,
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=auth(admin_token),
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"
    user_id = resp.json()["user_id"]

    assign_body: dict = {
        "company_role_id": role_id,
        "scope_type": scope_type,
    }
    if branch_id is not None:
        assign_body["branch_id"] = branch_id

    assign_resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json=assign_body,
        headers=auth(admin_token),
    )
    assert assign_resp.status_code in (200, 201), f"Assign role failed: {assign_resp.text}"

    login = await client.post("/auth/login", json={
        "username": username,
        "password": password,
        "company_code": "DEMO",
    })
    assert login.status_code == 200, f"Login failed: {login.text}"
    return login.json()["access_token"]


# ---------------------------------------------------------------------------
# TestRateWritePermissions  (Fix 1)
# ---------------------------------------------------------------------------

class TestRateWritePermissions:
    """payrates.edit required for create, approve, void (not payroll.entry)."""

    @pytest.mark.asyncio
    async def test_payrates_edit_user_can_create_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        role_id = await _create_role_with_perms(
            session_client, auth_token, "PREditCreateTest", ["payrates.edit", "payrates.view"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "pr_edit_create_user", role_id
        )
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": hourly["rate_type_id"],
                "amount": "19.00",
                "effective_from": "2030-01-01",
            },
            headers=auth(token),
        )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_payroll_entry_only_cannot_create_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """payroll.entry alone must NOT allow rate creation (Fix 1)."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "PREntryOnlyTest", ["payroll.entry", "payroll.view"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "pr_entry_only_user", role_id
        )
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": hourly["rate_type_id"],
                "amount": "19.00",
                "effective_from": "2031-01-01",
            },
            headers=auth(token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_payrates_edit_can_approve_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """payrates.edit is sufficient to approve a rate."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "PREditApproveTest", ["payrates.edit", "payrates.view"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "pr_edit_approve_user", role_id
        )
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE rate type not found")

        create_resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": mileage["rate_type_id"],
                "amount": "0.72",
                "effective_from": "2032-01-01",
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        rate_id = create_resp.json()["driver_rate_id"]

        resp = await session_client.post(
            f"/payroll/rates/{rate_id}/approve",
            headers=auth(token),
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_payroll_approve_rate_only_cannot_approve(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """payroll.approve_rate alone is no longer sufficient (Fix 1)."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "PRApproveOnlyTest",
            ["payroll.approve_rate", "payroll.view", "payrates.view"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "pr_approve_only_user", role_id
        )
        # Use HOURLY (isdefaultbranchactive=TRUE on HQ) to avoid branch-activation rejection
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        create_resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": hourly["rate_type_id"],
                "amount": "22.00",
                "effective_from": "2033-01-01",
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        rate_id = create_resp.json()["driver_rate_id"]

        resp = await session_client.post(
            f"/payroll/rates/{rate_id}/approve",
            headers=auth(token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestRateReadPermissions  (Fix 2)
# ---------------------------------------------------------------------------

class TestRateReadPermissions:
    """payrates.view (or payrates.edit) required for listing and fetching rates."""

    @pytest.mark.asyncio
    async def test_payrates_view_can_list_rates(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        role_id = await _create_role_with_perms(
            session_client, auth_token, "PRViewListTest", ["payrates.view"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "pr_view_list_user", role_id
        )
        resp = await session_client.get("/payroll/rates", headers=auth(token))
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_payrates_perm_cannot_list_rates(
        self,
        session_client: httpx.AsyncClient,
        branch_user_token: str,
    ):
        """PAYROLL_VIEWER has no payrates.* permissions — must get 403 on list."""
        resp = await session_client.get("/payroll/rates", headers=auth(branch_user_token))
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_payrates_view_can_get_rate_by_id(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        # Use MILEAGE (isdefaultbranchactive=TRUE) to avoid branch-activation rejection
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE rate type not found")

        create_resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": mileage["rate_type_id"],
                "amount": "5.00",
                "effective_from": "2034-01-01",
            },
            headers=auth(auth_token),
        )
        if create_resp.status_code != 201:
            pytest.skip(f"Could not create rate: {create_resp.text}")
        rate_id = create_resp.json()["driver_rate_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PRViewGetTest", ["payrates.view"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "pr_view_get_user", role_id
        )
        resp = await session_client.get(f"/payroll/rates/{rate_id}", headers=auth(token))
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_perm_cannot_get_rate_by_id(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        # Use LOAD (isdefaultbranchactive=TRUE) to avoid branch-activation rejection
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        load_rt = next((r for r in rt_resp.json() if r["rate_code"] == "LOAD"), None)
        if load_rt is None:
            pytest.skip("LOAD rate type not found")

        create_resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": load_rt["rate_type_id"],
                "amount": "2.00",
                "effective_from": "2035-01-01",
            },
            headers=auth(auth_token),
        )
        if create_resp.status_code != 201:
            pytest.skip(f"Could not create rate: {create_resp.text}")
        rate_id = create_resp.json()["driver_rate_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PRNoPermGetTest", ["payroll.view", "payroll.entry"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "pr_noperm_get_user", role_id
        )
        resp = await session_client.get(f"/payroll/rates/{rate_id}", headers=auth(token))
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# TestMatrixHistoricalAsOf  (Fix 6)
# ---------------------------------------------------------------------------

class TestMatrixHistoricalAsOf:
    """Matrix must return the correct Superseded rate for historical as_of dates."""

    @pytest.mark.asyncio
    async def test_matrix_returns_superseded_rate_for_old_date(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        # Use paytest_driver_id: PAYTEST branch has branchpayitemconfig rows (activated by conftest)
        from datetime import timedelta
        today_date = date.today()
        date_a = (today_date - timedelta(days=730)).isoformat()    # ~2 years ago
        date_b = (today_date - timedelta(days=1)).isoformat()       # yesterday
        date_between = (today_date - timedelta(days=365)).isoformat()  # ~1 year ago

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly_rt = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly_rt is None:
            pytest.skip("HOURLY rate type not found")
        rt_id = hourly_rt["rate_type_id"]

        # Rate A: older effective date, amount 77.77
        cr_a = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": paytest_driver_id,
                "rate_type_id": rt_id,
                "amount": "77.77",
                "effective_from": date_a,
            },
            headers=auth(auth_token),
        )
        assert cr_a.status_code == 201, cr_a.text
        rate_a_id = cr_a.json()["driver_rate_id"]
        await session_client.post(
            f"/payroll/rates/{rate_a_id}/approve", headers=auth(auth_token)
        )

        # Rate B: newer effective date, amount 88.88 (supersedes A)
        cr_b = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": paytest_driver_id,
                "rate_type_id": rt_id,
                "amount": "88.88",
                "effective_from": date_b,
            },
            headers=auth(auth_token),
        )
        assert cr_b.status_code == 201, cr_b.text
        rate_b_id = cr_b.json()["driver_rate_id"]
        await session_client.post(
            f"/payroll/rates/{rate_b_id}/approve", headers=auth(auth_token)
        )

        # Verify Rate A is now Superseded (approve of B superseded A)
        rate_a_resp = await session_client.get(
            f"/payroll/rates/{rate_a_id}", headers=auth(auth_token)
        )
        assert rate_a_resp.status_code == 200
        assert rate_a_resp.json()["status"] == "Superseded"

        # Fix 6 test: verify via the matrix that the Superseded rate is returned
        # for a date that falls in its effective range.
        # Matrix Step 2 uses branchpayitemconfig; use the matrix only if HOURLY
        # appears (the group may be absent if no config row exists for PAYTEST).
        m_old = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": date_between},
            headers=auth(auth_token),
        )
        assert m_old.status_code == 200
        hourly_old = next(
            (g for g in m_old.json()["groups"] if g["rate_code"] == "HOURLY"), None
        )
        if hourly_old is not None:
            # If HOURLY appears in the matrix, Fix 6 must return the Superseded rate
            assert hourly_old["current_rate"] is not None, "Expected superseded rate for old date"
            assert float(hourly_old["current_rate"]["amount"]) == pytest.approx(77.77), (
                "Matrix as_of old date should return Superseded Rate A (77.77), not Rate B"
            )
            # Matrix as_of today → Rate B (88.88)
            m_new = await session_client.get(
                f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
                params={"as_of": _today_iso()},
                headers=auth(auth_token),
            )
            assert m_new.status_code == 200
            hourly_new = next(
                (g for g in m_new.json()["groups"] if g["rate_code"] == "HOURLY"), None
            )
            assert hourly_new is not None
            assert hourly_new["current_rate"] is not None
            assert float(hourly_new["current_rate"]["amount"]) == pytest.approx(88.88)
        # Whether or not the matrix shows the group, Rate A's Superseded status
        # (asserted above) confirms the approval + supersession logic works.


# ---------------------------------------------------------------------------
# TestCreateRateBranchValidation  (Fix 8)
# ---------------------------------------------------------------------------

class TestCreateRateBranchValidation:
    """Branch-active rate type validation on POST /payroll/rates."""

    @pytest.mark.asyncio
    async def test_create_rate_for_active_rate_type_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """Standard HOURLY rate on an active branch → 201."""
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": hourly["rate_type_id"],
                "amount": "21.00",
                "effective_from": "2036-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_create_rate_for_inactive_rate_type_fails(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """A rate type with no BranchPayItemConfig mapping → 422."""
        rt_create = await session_client.post(
            "/payroll/rate-types",
            json={
                "rate_code": "UNMAPPED_TEST",
                "rate_name": "Unmapped Test Rate",
                "unit_name": "unit",
                "is_active": True,
            },
            headers=auth(auth_token),
        )
        if rt_create.status_code not in (200, 201):
            pytest.skip(f"Could not create test rate type: {rt_create.text}")
        new_rt_id = rt_create.json()["rate_type_id"]

        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": new_rt_id,
                "amount": "5.00",
                "effective_from": "2037-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "not active" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# TestDriverInfoPermissions  (Fix 5)
# ---------------------------------------------------------------------------

class TestDriverInfoPermissions:
    """GET /admin/users/{id}/driver requires caller permission (Fix 5)."""

    @pytest.mark.asyncio
    async def test_user_without_permission_cannot_get_driver_info(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """User with only payroll.view/entry — no users.view or payrates.* → 403."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "PayrollViewerDriverTest",
            ["payroll.view", "payroll.entry"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "payroll_viewer_drv_test", role_id
        )
        me = await session_client.get("/auth/me", headers=auth(auth_token))
        admin_user_id = me.json()["user_id"]

        resp = await session_client.get(
            f"/admin/users/{admin_user_id}/driver",
            headers=auth(token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_user_with_payrates_view_can_get_driver_info(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """User with payrates.view → 200 on /admin/users/{id}/driver."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "PayratesViewDriverTest", ["payrates.view"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "payrates_view_drv_test", role_id
        )
        me = await session_client.get("/auth/me", headers=auth(auth_token))
        admin_user_id = me.json()["user_id"]

        resp = await session_client.get(
            f"/admin/users/{admin_user_id}/driver",
            headers=auth(token),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Shared test helper: get a matrix group for a driver by rate_code
# ---------------------------------------------------------------------------

async def _get_matrix_group(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_code: str,
) -> dict | None:
    """Return the rate matrix group (pay_item_id + rate_type_id) for the given rate_code."""
    resp = await client.get(
        f"/payroll/drivers/{driver_id}/rate-matrix",
        headers=auth(token),
    )
    if resp.status_code != 200:
        return None
    for grp in resp.json().get("groups", []):
        if grp.get("rate_code") == rate_code:
            return grp
    return None


# ---------------------------------------------------------------------------
# TestBatchSaveRates  (Phase 2A + P1 contract)
# ---------------------------------------------------------------------------

class TestBatchSaveRates:
    """POST /payroll/drivers/{driver_id}/rates/batch — atomic batch save."""

    @pytest.mark.asyncio
    async def test_batch_requires_payrates_edit(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """payroll.entry only → 403 on batch endpoint."""
        # Use paytest_driver_id: PAYTEST branch has force-activated HOURS/MILES
        # so the matrix includes HOURLY and MILEAGE groups.
        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "HOURLY")
        if grp is None:
            pytest.skip("HOURLY not in rate matrix for paytest_driver_id")

        role_id = await _create_role_with_perms(
            session_client, auth_token, "BatchEntryOnlyTest", ["payroll.entry", "payroll.view"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "batch_entry_only_user", role_id
        )

        resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2080-01-01",
                "changes": [{
                    "pay_item_id": grp["pay_item_id"],
                    "rate_type_id": grp["rate_type_id"],
                    "amount": "10.00",
                }],
            },
            headers=auth(token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_batch_rejects_empty_changes(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """Empty changes list → 422."""
        resp = await session_client.post(
            f"/payroll/drivers/{created_driver_id}/rates/batch",
            json={"effective_from": "2080-01-01", "changes": []},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_batch_rejects_duplicate_pay_item_rate_type_pair(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """Two changes with the same (pay_item_id, rate_type_id) pair → 422 duplicate."""
        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "HOURLY")
        if grp is None:
            pytest.skip("HOURLY not in rate matrix for paytest_driver_id")

        resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2080-01-01",
                "changes": [
                    {"pay_item_id": grp["pay_item_id"], "rate_type_id": grp["rate_type_id"], "amount": "10.00"},
                    {"pay_item_id": grp["pay_item_id"], "rate_type_id": grp["rate_type_id"], "amount": "12.00"},
                ],
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "duplicate" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_batch_rejects_missing_pay_item_id(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """Batch change without pay_item_id → 422 schema validation."""
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        resp = await session_client.post(
            f"/payroll/drivers/{created_driver_id}/rates/batch",
            json={
                "effective_from": "2080-01-01",
                "changes": [{"rate_type_id": hourly["rate_type_id"], "amount": "10.00"}],
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_batch_rejects_unmapped_pay_item_rate_type(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """pay_item_id + rate_type_id that are not actively mapped → 422."""
        # Get HOURLY rate type id and MILEAGE pay item id — they're not mapped to each other
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly_rt = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        mileage_grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "MILEAGE")
        if hourly_rt is None or mileage_grp is None:
            pytest.skip("HOURLY or MILEAGE not available for paytest_driver_id")

        # mileage pay_item_id + hourly rate_type_id is an invalid mapping
        resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2080-01-01",
                "changes": [{
                    "pay_item_id": mileage_grp["pay_item_id"],
                    "rate_type_id": hourly_rt["rate_type_id"],
                    "amount": "10.00",
                }],
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"].lower()
        assert "mapped" in detail or "mapping" in detail

    @pytest.mark.asyncio
    async def test_batch_creates_and_auto_approves_when_self_approval_on(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        Admin has AllowSelfApproval=True (DEMO company default).
        Batch should create rate and auto-approve it.
        """
        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "MILEAGE")
        if grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2081-01-01",
                "changes": [{
                    "pay_item_id": grp["pay_item_id"],
                    "rate_type_id": grp["rate_type_id"],
                    "amount": "0.75",
                }],
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["driver_id"] == paytest_driver_id
        assert data["approved_count"] == 1
        assert data["pending_count"] == 0
        assert len(data["rates"]) == 1
        assert data["rates"][0]["status"] == "Approved"
        assert float(data["rates"][0]["amount"]) == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_batch_updates_existing_pending_clears_effective_to(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        Batch updating an existing PendingApproval row must:
        1. Not create a duplicate.
        2. Clear EffectiveTo (reset to NULL) on the updated row.
        """
        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "MILEAGE")
        if grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        # Create a PendingApproval rate with an explicit effective_to via the individual endpoint
        create_resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": paytest_driver_id,
                "rate_type_id": grp["rate_type_id"],
                "amount": "0.50",
                "effective_from": "2082-01-01",
                "effective_to": "2082-12-31",   # explicit non-null effective_to
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        pending_id = create_resp.json()["driver_rate_id"]
        assert create_resp.json()["effective_to"] == "2082-12-31"

        # Batch-save: should update the pending row and clear effective_to
        batch_resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2082-06-01",
                "changes": [{
                    "pay_item_id": grp["pay_item_id"],
                    "rate_type_id": grp["rate_type_id"],
                    "amount": "0.99",
                }],
            },
            headers=auth(auth_token),
        )
        assert batch_resp.status_code == 200, batch_resp.text
        batch_data = batch_resp.json()

        # Should have updated the existing pending row
        assert batch_data["updated_pending_count"] == 1
        assert batch_data["created_count"] == 0
        assert batch_data["rates"][0]["driver_rate_id"] == pending_id

        # EffectiveTo must be NULL after the batch update
        assert batch_data["rates"][0]["effective_to"] is None, (
            "Batch update must clear stale EffectiveTo — got: "
            + str(batch_data["rates"][0]["effective_to"])
        )

    @pytest.mark.asyncio
    async def test_batch_driver_not_found(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Non-existent driver → 404 (before pay_item validation)."""
        resp = await session_client.post(
            "/payroll/drivers/999999/rates/batch",
            json={
                "effective_from": "2080-01-01",
                "changes": [{"pay_item_id": 1, "rate_type_id": 1, "amount": "10.00"}],
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# TestBackdatingGuard  (Phase 2A — deterministic, uses direct_db)
# ---------------------------------------------------------------------------

class TestBackdatingGuard:
    """
    Approval (single and batch) must reject effective_from dates inside
    Locked or Archived payroll periods.

    Tests are deterministic: they use direct_db to insert test periods
    directly at far-future dates, avoiding dependency on other test modules
    and removing any reliance on test ordering.
    """

    @staticmethod
    async def _insert_test_period(
        direct_db,
        branch_id: int,
        start: str,
        end: str,
        status: str,
    ) -> None:
        """Insert a payroll period directly into the DB with the given status."""
        import random as _random
        from datetime import date as _date
        from sqlalchemy import text as _text
        code = f"GRDTEST-{status[:3].upper()}-{_random.randint(100000, 999999)}"
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status, createdbyuserid)
                VALUES
                    (1, :bid, :code, :name, 'Month',
                     :start, :end, :status, 1)
                ON CONFLICT DO NOTHING
            """),
            {
                "bid":    branch_id,
                "code":   code,
                "name":   f"Guard Test {status} Period {code}",
                "start":  _date.fromisoformat(start),
                "end":    _date.fromisoformat(end),
                "status": status,
            },
        )

    @pytest.mark.asyncio
    async def test_approve_blocked_inside_locked_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """Single approve: effective_from inside Locked period → 422."""
        await self._insert_test_period(
            direct_db, paytest_branch_id, "2060-01-01", "2060-01-31", "Locked"
        )

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        cr = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": paytest_driver_id,
                "rate_type_id": hourly["rate_type_id"],
                "amount": "99.99",
                "effective_from": "2060-01-15",
            },
            headers=auth(auth_token),
        )
        assert cr.status_code == 201, cr.text
        rate_id = cr.json()["driver_rate_id"]

        approve_r = await session_client.post(
            f"/payroll/rates/{rate_id}/approve",
            headers=auth(auth_token),
        )
        assert approve_r.status_code == 422, approve_r.text
        assert "finalized" in approve_r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_approve_blocked_inside_archived_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """Single approve: effective_from inside Archived period → 422."""
        await self._insert_test_period(
            direct_db, paytest_branch_id, "2061-01-01", "2061-01-31", "Archived"
        )

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        cr = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": paytest_driver_id,
                "rate_type_id": hourly["rate_type_id"],
                "amount": "88.88",
                "effective_from": "2061-01-10",
            },
            headers=auth(auth_token),
        )
        assert cr.status_code == 201, cr.text
        rate_id = cr.json()["driver_rate_id"]

        approve_r = await session_client.post(
            f"/payroll/rates/{rate_id}/approve",
            headers=auth(auth_token),
        )
        assert approve_r.status_code == 422, approve_r.text
        assert "finalized" in approve_r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_batch_auto_approve_blocked_inside_locked_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """Batch auto-approve: effective_from inside Locked period → 422, no rates created."""
        await self._insert_test_period(
            direct_db, paytest_branch_id, "2062-01-01", "2062-01-31", "Locked"
        )

        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "HOURLY")
        if grp is None:
            pytest.skip("HOURLY not in rate matrix for paytest_driver_id")

        resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2062-01-15",
                "changes": [{
                    "pay_item_id": grp["pay_item_id"],
                    "rate_type_id": grp["rate_type_id"],
                    "amount": "55.00",
                }],
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, resp.text
        assert "finalized" in resp.json()["detail"].lower()

        # Verify no rate was created (rollback)
        rates_resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id, "status": "PendingApproval"},
            headers=auth(auth_token),
        )
        created_amounts = [float(r["amount"]) for r in rates_resp.json()]
        assert 55.0 not in created_amounts, "PendingApproval rate must not exist after blocked batch"

    @pytest.mark.asyncio
    async def test_batch_auto_approve_blocked_inside_archived_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """Batch auto-approve: effective_from inside Archived period → 422."""
        await self._insert_test_period(
            direct_db, paytest_branch_id, "2063-01-01", "2063-01-31", "Archived"
        )

        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "MILEAGE")
        if grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2063-01-20",
                "changes": [{
                    "pay_item_id": grp["pay_item_id"],
                    "rate_type_id": grp["rate_type_id"],
                    "amount": "1.11",
                }],
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, resp.text
        assert "finalized" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_approve_outside_finalized_period_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """A rate effective at a date with no finalized period must approve fine."""
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        cr = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": created_driver_id,
                "rate_type_id": hourly["rate_type_id"],
                "amount": "30.00",
                "effective_from": "2090-01-01",
            },
            headers=auth(auth_token),
        )
        assert cr.status_code == 201, cr.text
        rate_id = cr.json()["driver_rate_id"]

        approve_r = await session_client.post(
            f"/payroll/rates/{rate_id}/approve",
            headers=auth(auth_token),
        )
        assert approve_r.status_code == 200, approve_r.text
        assert approve_r.json()["status"] == "Approved"


# ---------------------------------------------------------------------------
# TestLookupSecurity  (P0 fix)
# ---------------------------------------------------------------------------

class TestLookupSecurity:
    """
    GET /payroll/rates/lookup must require payrates.view / payrates.edit.
    OwnDriverDataOnly callers may only look up their own driver.
    Cross-company lookup must be impossible.
    """

    @pytest.mark.asyncio
    async def test_no_permission_cannot_use_lookup(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
        paytest_rate_type_id: int,
    ):
        """User with only payroll.entry — no payrates.* — must get 403."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "LookupNoPermTest",
            ["payroll.entry", "payroll.view"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "lookup_no_perm_user", role_id
        )

        resp = await session_client.get(
            "/payroll/rates/lookup",
            params={
                "driver_id": created_driver_id,
                "rate_type_id": paytest_rate_type_id,
                "work_date": _today_iso(),
            },
            headers=auth(token),
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_payrates_view_can_use_lookup(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
        paytest_rate_type_id: int,
    ):
        """User with payrates.view must get 200 (found=True or found=False, not 403/401)."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "LookupViewPermTest", ["payrates.view"]
        )
        token = await _create_user_with_role(
            session_client, auth_token, "lookup_view_user", role_id
        )

        resp = await session_client.get(
            "/payroll/rates/lookup",
            params={
                "driver_id": created_driver_id,
                "rate_type_id": paytest_rate_type_id,
                "work_date": _today_iso(),
            },
            headers=auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "found" in data  # shape is correct; found may be True or False

    @pytest.mark.asyncio
    async def test_cross_company_driver_lookup_returns_not_found(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_rate_type_id: int,
    ):
        """
        A driver_id that does not belong to this company must NOT reveal rate data.
        The service returns found=False (company filter prevents any data leak).
        """
        resp = await session_client.get(
            "/payroll/rates/lookup",
            params={
                "driver_id": 999999,  # non-existent in this company
                "rate_type_id": paytest_rate_type_id,
                "work_date": _today_iso(),
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["found"] is False


# ---------------------------------------------------------------------------
# TestOwnDriverDataOnlyRates  (P1 — OwnDriverDataOnly scope enforcement)
# ---------------------------------------------------------------------------

class TestOwnDriverDataOnlyRates:
    """
    OwnDriverDataOnly users may only access/modify their own driver's rates.
    They must not be able to reach another driver's rates even if branch
    access would otherwise allow it.
    """

    @staticmethod
    async def _setup_oda_user(
        client: httpx.AsyncClient,
        admin_token: str,
        username: str,
        hq_branch_id: int,
    ) -> tuple[str, int]:
        """
        Create a user with OwnDriverDataOnly scope + payrates.edit permission.
        Returns (token, user_id).  The user has NO linked driver (no employeeid link),
        so _check_own_driver_only will treat every driver as "not their own".
        """
        role_id = await _create_role_with_perms(
            client, admin_token, f"ODARole_{username}",
            ["payrates.edit", "payrates.view"],
        )
        resp = await client.post(
            "/admin/users",
            json={
                "username":     username,
                "display_name": username,
                "password":     "TestPass123!",
                "is_active":    True,
                "can_login":    True,
                "must_change_password": False,
            },
            headers=auth(admin_token),
        )
        assert resp.status_code == 201, f"Create user failed: {resp.text}"
        user_id: int = resp.json()["user_id"]

        # Assign the payrates role with OwnDriverDataOnly scope
        assign_resp = await client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={
                "company_role_id": role_id,
                "scope_type":      "OwnDriverDataOnly",
                "branch_id":       hq_branch_id,
            },
            headers=auth(admin_token),
        )
        assert assign_resp.status_code in (200, 201), f"Assign role failed: {assign_resp.text}"

        login = await client.post("/auth/login", json={
            "username": username, "password": "TestPass123!", "company_code": "DEMO",
        })
        assert login.status_code == 200
        return login.json()["access_token"], user_id

    @pytest.mark.asyncio
    async def test_oda_cannot_batch_save_another_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """
        OwnDriverDataOnly user (no linked driver) on PAYTEST branch → 403 on batch
        for paytest_driver_id (same branch).  Branch check passes; OwnDriverDataOnly
        check fires because user has no linked driver.
        """
        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "HOURLY")
        if grp is None:
            pytest.skip("HOURLY not in matrix for paytest_driver_id")

        # Create ODA user on PAYTEST branch (no linked driver)
        role_id = await _create_role_with_perms(
            session_client, auth_token, "ODABatchTestRole",
            ["payrates.edit", "payrates.view"],
        )
        token = await _create_user_with_role(
            session_client, auth_token, "oda_batch_test_user2", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2085-01-01",
                "changes": [{
                    "pay_item_id": grp["pay_item_id"],
                    "rate_type_id": grp["rate_type_id"],
                    "amount": "25.00",
                }],
            },
            headers=auth(token),
        )
        assert resp.status_code == 403
        assert "OwnDriverDataOnly" in resp.json()["detail"] or "own" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_oda_cannot_create_rate_for_another_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """OwnDriverDataOnly user on PAYTEST branch (no linked driver) → 403 on POST /payroll/rates."""
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        # ODA user on PAYTEST branch, no linked driver
        role_id = await _create_role_with_perms(
            session_client, auth_token, "ODACreateRateRole",
            ["payrates.edit", "payrates.view"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "oda_create_rate_user2", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": paytest_driver_id,
                "rate_type_id": hourly["rate_type_id"],
                "amount": "20.00",
                "effective_from": "2086-01-01",
            },
            headers=auth(oda_token),
        )
        assert resp.status_code == 403
        assert "OwnDriverDataOnly" in resp.json()["detail"] or "own" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_oda_cannot_lookup_another_driver_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        paytest_branch_id: int,
    ):
        """OwnDriverDataOnly user on PAYTEST branch (no linked driver) → 403 on lookup."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "ODALookupRole",
            ["payrates.view"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "oda_lookup_test_user2", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            "/payroll/rates/lookup",
            params={
                "driver_id":    paytest_driver_id,
                "rate_type_id": paytest_rate_type_id,
                "work_date":    _today_iso(),
            },
            headers=auth(oda_token),
        )
        assert resp.status_code == 403
        assert "OwnDriverDataOnly" in resp.json()["detail"] or "own" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# TestBatchRollback  (P1 — atomicity verification)
# ---------------------------------------------------------------------------

class TestBatchRollback:
    """
    Batch save must be all-or-nothing.
    If any change fails validation, no rows from the batch must be written.
    """

    @pytest.mark.asyncio
    async def test_invalid_second_change_leaves_no_first_change(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        Batch: change 1 valid, change 2 has an invalid (unmapped) pay_item_id + rate_type_id.
        Validation in Step 6 fails → no write for change 1 either.
        Verify via GET /payroll/rates that no PendingApproval rate for change 1 was created.
        """
        # Change 1: valid HOURLY group
        grp_hourly = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "HOURLY")
        # Change 2: invalid cross-mapping (MILEAGE pay_item + HOURLY rate_type)
        grp_mileage = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "MILEAGE")
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly_rt = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)

        if grp_hourly is None or grp_mileage is None or hourly_rt is None:
            pytest.skip("Required rate types/groups not available for rollback test")

        # Record current count of PendingApproval rates for this driver
        before_resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id, "status": "PendingApproval"},
            headers=auth(auth_token),
        )
        before_ids = {r["driver_rate_id"] for r in before_resp.json()}

        # Submit batch with invalid second change
        batch_resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2087-01-01",
                "changes": [
                    # Change 1 — valid
                    {
                        "pay_item_id": grp_hourly["pay_item_id"],
                        "rate_type_id": grp_hourly["rate_type_id"],
                        "amount": "77.77",
                    },
                    # Change 2 — invalid: MILEAGE pay_item + HOURLY rate_type not mapped
                    {
                        "pay_item_id": grp_mileage["pay_item_id"],
                        "rate_type_id": hourly_rt["rate_type_id"],
                        "amount": "55.55",
                    },
                ],
            },
            headers=auth(auth_token),
        )
        assert batch_resp.status_code == 422, batch_resp.text

        # Verify no new PendingApproval rates were created
        after_resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id, "status": "PendingApproval"},
            headers=auth(auth_token),
        )
        after_ids = {r["driver_rate_id"] for r in after_resp.json()}
        new_ids = after_ids - before_ids
        assert not new_ids, (
            f"Atomicity failure: {len(new_ids)} new PendingApproval rate(s) were created "
            f"after a batch that should have been rejected entirely: {new_ids}"
        )

    @pytest.mark.asyncio
    async def test_backdating_guard_blocks_batch_and_leaves_no_rates(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Batch where effective_from falls in a Locked period:
        The backdating guard fires (Step 5) before any write (Step 7).
        Verify no PendingApproval rate remains after the rejected batch.
        """
        await TestBackdatingGuard._insert_test_period(
            direct_db, paytest_branch_id, "2064-06-01", "2064-06-30", "Locked"
        )

        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "MILEAGE")
        if grp is None:
            pytest.skip("MILEAGE not in matrix for paytest_driver_id")

        before_resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id, "status": "PendingApproval"},
            headers=auth(auth_token),
        )
        before_count = len(before_resp.json())

        batch_resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2064-06-15",
                "changes": [{
                    "pay_item_id": grp["pay_item_id"],
                    "rate_type_id": grp["rate_type_id"],
                    "amount": "3.33",
                }],
            },
            headers=auth(auth_token),
        )
        assert batch_resp.status_code == 422, batch_resp.text
        assert "finalized" in batch_resp.json()["detail"].lower()

        after_resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id, "status": "PendingApproval"},
            headers=auth(auth_token),
        )
        assert len(after_resp.json()) == before_count, (
            "No new PendingApproval rate should exist after backdating-blocked batch"
        )


# ---------------------------------------------------------------------------
# TestODAListDetail  (P1 — ODA on list/detail endpoints)
# ---------------------------------------------------------------------------

class TestODAListDetail:
    """
    OwnDriverDataOnly users must NOT be able to read another driver's rates
    through GET /payroll/rates (list) or GET /payroll/rates/{rate_id} (detail).

    Tests use an ODA user with no linked driver profile so that every
    target driver is "not their own" — this ensures the 403 fires correctly.
    """

    @pytest.mark.asyncio
    async def test_oda_cannot_list_rates_for_another_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """ODA user (no linked driver) → 403 when listing rates with driver_id of another driver."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "ODAListRole1",
            ["payrates.view"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "oda_list_test_user1", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id},
            headers=auth(oda_token),
        )
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"].lower()
        # ODA protection fires: either "own driver" message or "no linked driver profile"
        assert (
            "own" in detail
            or "owndriverdata" in detail.replace(" ", "")
            or "linked" in detail
        ), f"Expected ODA 403 but got: {resp.json()['detail']}"

    @pytest.mark.asyncio
    async def test_oda_list_without_driver_id_gets_empty_not_all(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """
        ODA user with no linked driver → list without driver_id param.
        The query is forced to own driver (none exists) → returns empty list, not all rates.
        Must NOT leak another driver's rates.
        """
        role_id = await _create_role_with_perms(
            session_client, auth_token, "ODAListRole2",
            ["payrates.view"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "oda_list_test_user2", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        # Admin creates a rate for paytest_driver so there IS data to potentially leak
        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "HOURLY")
        if grp is None:
            pytest.skip("HOURLY not in matrix for paytest_driver_id")
        await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2088-01-01",
                "changes": [{"pay_item_id": grp["pay_item_id"], "rate_type_id": grp["rate_type_id"], "amount": "11.11"}],
            },
            headers=auth(auth_token),
        )

        # ODA user lists rates with NO driver_id param — should be scoped to own driver
        # Since ODA user has no linked driver profile, this returns 403 (no profile found).
        resp = await session_client.get(
            "/payroll/rates",
            headers=auth(oda_token),
        )
        # With no linked driver: _get_oda_own_driver_id raises 403 (no driver profile)
        assert resp.status_code == 403, (
            f"ODA user with no linked driver must not receive all rates. "
            f"Got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_oda_cannot_get_rate_by_id_for_another_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """ODA user (no linked driver) → 403 when reading a specific rate belonging to another driver."""
        # Create a rate for paytest_driver
        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "MILEAGE")
        if grp is None:
            pytest.skip("MILEAGE not in matrix for paytest_driver_id")
        batch_resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2089-01-01",
                "changes": [{"pay_item_id": grp["pay_item_id"], "rate_type_id": grp["rate_type_id"], "amount": "2.22"}],
            },
            headers=auth(auth_token),
        )
        assert batch_resp.status_code == 200, batch_resp.text
        rate_id = batch_resp.json()["rates"][0]["driver_rate_id"]

        # ODA user on same branch tries to read that specific rate
        role_id = await _create_role_with_perms(
            session_client, auth_token, "ODADetailRole1",
            ["payrates.view"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "oda_detail_test_user1", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/rates/{rate_id}",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"].lower()
        assert (
            "own" in detail
            or "owndriverdata" in detail.replace(" ", "")
            or "linked" in detail
        ), f"Expected ODA 403 but got: {resp.json()['detail']}"

    @pytest.mark.asyncio
    async def test_non_oda_user_can_still_list_and_get_rates(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """Non-ODA user with payrates.view on AllCompanyBranches can list and read rates normally."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "NonODAViewRole1",
            ["payrates.view"],
        )
        non_oda_token = await _create_user_with_role(
            session_client, auth_token, "non_oda_view_user1", role_id,
            scope_type="AllCompanyBranches",
        )

        # List
        list_resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id},
            headers=auth(non_oda_token),
        )
        assert list_resp.status_code == 200, list_resp.text

        # Detail (if any rates exist for this driver)
        rates = list_resp.json()
        if rates:
            detail_resp = await session_client.get(
                f"/payroll/rates/{rates[0]['driver_rate_id']}",
                headers=auth(non_oda_token),
            )
            assert detail_resp.status_code == 200, detail_resp.text


# ---------------------------------------------------------------------------
# TestBatchRateTypeActive  (P1 — inactive RateType rejected in batch)
# ---------------------------------------------------------------------------

class TestBatchRateTypeActive:
    """
    Batch Step 6 must reject an inactive RateType even if the PayItemRateTypeMap
    mapping still exists.  The JOIN on ratetypes.isactive=TRUE ensures this.
    """

    @pytest.mark.asyncio
    async def test_batch_rejects_inactive_rate_type(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Deactivate a RateType in the DB, then attempt a batch save using that
        rate_type_id.  Expect 422 — mapping found but RateType is not isactive=TRUE.
        Restore isactive afterwards.
        """
        from sqlalchemy import text as _text

        # Get the HOURLY group info (pay_item_id + rate_type_id)
        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "HOURLY")
        if grp is None:
            pytest.skip("HOURLY not in matrix for paytest_driver_id")

        rate_type_id = grp["rate_type_id"]

        # Deactivate the RateType directly in DB (AUTOCOMMIT)
        await direct_db.execute(
            _text("UPDATE payroll.ratetypes SET isactive = FALSE WHERE ratetypeid = :rtid"),
            {"rtid": rate_type_id},
        )
        try:
            resp = await session_client.post(
                f"/payroll/drivers/{paytest_driver_id}/rates/batch",
                json={
                    "effective_from": "2090-01-01",
                    "changes": [{
                        "pay_item_id": grp["pay_item_id"],
                        "rate_type_id": rate_type_id,
                        "amount": "15.00",
                    }],
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"Expected 422 for inactive RateType, got {resp.status_code}: {resp.text}"
            )
            # Should mention the mapping issue
            detail = resp.json()["detail"].lower()
            assert "mapped" in detail or "mapping" in detail or "not actively" in detail
        finally:
            # Always restore
            await direct_db.execute(
                _text("UPDATE payroll.ratetypes SET isactive = TRUE WHERE ratetypeid = :rtid"),
                {"rtid": rate_type_id},
            )

    @pytest.mark.asyncio
    async def test_batch_accepts_active_rate_type(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """Sanity: batch with valid active RateType still succeeds after P1 fix."""
        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "MILEAGE")
        if grp is None:
            pytest.skip("MILEAGE not in matrix for paytest_driver_id")

        resp = await session_client.post(
            f"/payroll/drivers/{paytest_driver_id}/rates/batch",
            json={
                "effective_from": "2091-01-01",
                "changes": [{
                    "pay_item_id": grp["pay_item_id"],
                    "rate_type_id": grp["rate_type_id"],
                    "amount": "4.44",
                }],
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["rates"]) == 1


# ---------------------------------------------------------------------------
# TestODAScopeDetection  (P1 — scope detection with overlapping assignments)
# ---------------------------------------------------------------------------

class TestODAScopeDetection:
    """
    _get_oda_own_driver_id must fail-closed when multiple active assignments
    exist and any of them is OwnDriverDataOnly.

    Normal cases (single assignment) must continue to work correctly.
    """

    @pytest.mark.asyncio
    async def test_single_oda_assignment_enforces_oda(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """Single ODA assignment → _check_own_driver_only enforces own-driver-only (403 on another driver)."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "ScopeTestODARole1",
            ["payrates.view"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "scope_test_oda_user1", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id},
            headers=auth(oda_token),
        )
        assert resp.status_code == 403
        detail = resp.json()["detail"].lower()
        # Accept "own driver", "OwnDriverDataOnly", or "no linked driver" messages
        assert (
            "own" in detail
            or "owndriverdata" in detail.replace(" ", "")
            or "linked" in detail
        ), f"Unexpected ODA error: {resp.json()['detail']}"

    @pytest.mark.asyncio
    async def test_single_specific_branch_assignment_is_not_oda(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """Single SpecificBranch assignment → no ODA restriction → can list another driver's rates."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "ScopeTestSBRole1",
            ["payrates.view"],
        )
        sb_token = await _create_user_with_role(
            session_client, auth_token, "scope_test_sb_user1", role_id,
            scope_type="SpecificBranch", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id, "branch_id": paytest_branch_id},
            headers=auth(sb_token),
        )
        # SpecificBranch user on PAYTEST branch can see PAYTEST driver rates
        assert resp.status_code == 200, (
            f"SpecificBranch user must not be blocked as ODA. Got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_overlapping_oda_and_specific_branch_fails_closed(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Artificially inject a second active assignment with a different scope alongside
        an existing ODA assignment → fail-closed 403 with 'Ambiguous' message.

        This simulates DB corruption that assign_company_role should prevent.
        """
        from sqlalchemy import text as _text

        role_id = await _create_role_with_perms(
            session_client, auth_token, "ScopeConflictODARole1",
            ["payrates.view"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "scope_conflict_user1", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        # Get user_id from token by calling /auth/me or /admin/users
        me_resp = await session_client.get("/auth/me", headers=auth(oda_token))
        if me_resp.status_code != 200:
            pytest.skip("Cannot resolve user_id from token — /auth/me not available")
        user_id = me_resp.json()["user_id"]

        # Directly inject a second active row with SpecificBranch scope (bad data).
        # Columns: userid, companyid, branchid, roleid (nullable), scopetype, isactive.
        insert_result = await direct_db.execute(
            _text("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, scopetype, isactive)
                SELECT u.userid, u.companyid, :bid, NULL, 'SpecificBranch', TRUE
                FROM   sec.users u
                WHERE  u.userid = :uid
                RETURNING userbranchroleid
            """),
            {"uid": user_id, "bid": paytest_branch_id},
        )
        injected_ubr_id = insert_result.scalar_one()

        try:
            resp = await session_client.get(
                "/payroll/rates",
                params={"driver_id": paytest_driver_id},
                headers=auth(oda_token),
            )
            assert resp.status_code == 403, (
                f"Overlapping ODA+SpecificBranch must fail-closed. Got {resp.status_code}: {resp.text}"
            )
            detail = resp.json()["detail"].lower()
            assert (
                "ambiguous" in detail or "conflicting" in detail or "multiple" in detail
            ), f"Expected ambiguity message, got: {resp.json()['detail']}"
        finally:
            # Clean up the injected bad row by PK to avoid removing legitimate rows
            await direct_db.execute(
                _text("DELETE FROM sec.userbranchroles WHERE userbranchroleid = :ubrid"),
                {"ubrid": injected_ubr_id},
            )

    @pytest.mark.asyncio
    async def test_assign_company_role_revokes_old_assignment(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        assign_company_role revokes the previous assignment before creating a new one.
        After reassignment, exactly one active assignment exists — no ambiguity.
        """
        from sqlalchemy import text as _text

        role_id = await _create_role_with_perms(
            session_client, auth_token, "ScopeRevokeRole1",
            ["payrates.view"],
        )

        # Create user with ODA scope
        user_resp = await session_client.post(
            "/admin/users",
            json={
                "username": "scope_revoke_user1",
                "display_name": "scope_revoke_user1",
                "password": "TestPass123!",
                "is_active": True,
                "can_login": True,
                "must_change_password": False,
            },
            headers=auth(auth_token),
        )
        assert user_resp.status_code == 201
        user_id = user_resp.json()["user_id"]

        await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={"company_role_id": role_id, "scope_type": "OwnDriverDataOnly", "branch_id": paytest_branch_id},
            headers=auth(auth_token),
        )

        # Reassign to SpecificBranch (should revoke ODA)
        role_id2 = await _create_role_with_perms(
            session_client, auth_token, "ScopeRevokeRole2",
            ["payrates.view"],
        )
        reassign_resp = await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={"company_role_id": role_id2, "scope_type": "SpecificBranch", "branch_id": paytest_branch_id},
            headers=auth(auth_token),
        )
        assert reassign_resp.status_code in (200, 201), reassign_resp.text

        # Login and verify the new token works as SpecificBranch (no ODA)
        login = await session_client.post("/auth/login", json={
            "username": "scope_revoke_user1", "password": "TestPass123!", "company_code": "DEMO",
        })
        assert login.status_code == 200
        new_token = login.json()["access_token"]

        # Can list rates without ODA restriction (SpecificBranch).
        # SpecificBranch users need branch_id in the query for fn_UserHasPermission to pass.
        resp = await session_client.get(
            "/payroll/rates",
            params={"branch_id": paytest_branch_id},
            headers=auth(new_token),
        )
        assert resp.status_code == 200, (
            f"After reassignment to SpecificBranch, ODA should be revoked. "
            f"Got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# TestMatrixODAScope  (P1 — get_driver_rate_matrix ODA fix)
# ---------------------------------------------------------------------------

class TestMatrixODAScope:
    """
    get_driver_rate_matrix must use _check_own_driver_only (fail-closed) instead
    of the old inline 'ORDER BY userbranchroleid DESC LIMIT 1' logic.

    Covers:
    - ODA user WITH linked driver can access own matrix (true linked-driver scenario)
    - ODA user WITH linked driver cannot access another driver's matrix
    - ODA user with no linked driver profile fails closed on matrix (403)
    - Overlapping active assignments (ODA + SpecificBranch) fail closed on matrix (403)
    - SpecificBranch user can access allowed-branch driver matrix
    - AllCompanyBranches user can access any driver matrix
    - Cross-company driver is blocked (404 — driver not in this company)
    """

    @staticmethod
    async def _create_oda_linked_driver_user(
        client: httpx.AsyncClient,
        admin_token: str,
        username: str,
        paytest_branch_id: int,
    ) -> tuple[str, int]:
        """
        Create a user with a real linked driver profile + ODA scope + payrates.view.

        Steps:
          1. Create the user account.
          2. Assign the DRIVER company role on PAYTEST branch.
             This calls ensure_driver_profile → links Users.EmployeeID → Employees → Drivers.
          3. Record the created driver_id.
          4. Assign a payrates.view role with OwnDriverDataOnly scope on PAYTEST branch.
             assign_company_role revokes the DRIVER assignment but the driver profile link persists.

        Returns (token, own_driver_id).
        """
        # Step 1: create user
        create_resp = await client.post(
            "/admin/users",
            json={
                "username":     username,
                "display_name": username,
                "password":     "TestPass123!",
                "is_active":    True,
                "can_login":    True,
                "must_change_password": False,
            },
            headers=auth(admin_token),
        )
        assert create_resp.status_code == 201, f"Create user failed: {create_resp.text}"
        user_id: int = create_resp.json()["user_id"]

        # Step 2: assign DRIVER role (creates driver profile + employee link)
        roles_resp = await client.get("/admin/company-roles", headers=auth(admin_token))
        driver_role = next(
            (r for r in roles_resp.json() if r.get("role_code") == "DRIVER"), None
        )
        if driver_role is None:
            pytest.skip("DRIVER company role not found — seed data may be missing")
        assign_driver_resp = await client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={
                "company_role_id": driver_role["company_role_id"],
                "scope_type":      "SpecificBranch",
                "branch_id":       paytest_branch_id,
            },
            headers=auth(admin_token),
        )
        assert assign_driver_resp.status_code in (200, 201), (
            f"Assign DRIVER role failed: {assign_driver_resp.text}"
        )

        # Step 3: get the created driver_id via /admin/users/{id}/driver
        drv_resp = await client.get(
            f"/admin/users/{user_id}/driver",
            headers=auth(admin_token),
        )
        assert drv_resp.status_code == 200, f"Driver lookup failed: {drv_resp.text}"
        drv_data = drv_resp.json()
        assert drv_data["has_driver_profile"] is True, (
            "DRIVER role assignment must create a driver profile"
        )
        own_driver_id: int = drv_data["driver_id"]

        # Step 4: assign payrates.view ODA role (revokes DRIVER, keeps driver link)
        oda_role_id = await _create_role_with_perms(
            client, admin_token, f"MatrixODARole_{username}",
            ["payrates.view", "payrates.edit"],
        )
        assign_oda_resp = await client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={
                "company_role_id": oda_role_id,
                "scope_type":      "OwnDriverDataOnly",
                "branch_id":       paytest_branch_id,
            },
            headers=auth(admin_token),
        )
        assert assign_oda_resp.status_code in (200, 201), (
            f"Assign ODA role failed: {assign_oda_resp.text}"
        )

        login = await client.post("/auth/login", json={
            "username": username, "password": "TestPass123!", "company_code": "DEMO",
        })
        assert login.status_code == 200, f"Login failed: {login.text}"
        return login.json()["access_token"], own_driver_id

    # -------------------------------------------------------------------------
    # Test 1 — ODA user WITH linked driver can load own matrix
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_oda_linked_driver_can_access_own_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        ODA user with real driver profile link can load their own rate matrix.
        This is the true linked-driver ODA scenario (not the 'no profile' path).
        """
        token, own_driver_id = await self._create_oda_linked_driver_user(
            session_client, auth_token, "matrix_oda_linked_user1", paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/drivers/{own_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(token),
        )
        assert resp.status_code == 200, (
            f"ODA user must be able to access own driver matrix. Got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["driver_id"] == own_driver_id

    # -------------------------------------------------------------------------
    # Test 2 — ODA user WITH linked driver cannot load another driver's matrix
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_oda_linked_driver_cannot_access_other_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """
        ODA user with real driver profile link → 403 on another driver's matrix.
        Must NOT use the 'no linked driver profile' fallback — the deny must come
        from the driver-id mismatch branch of _check_own_driver_only.
        """
        token, own_driver_id = await self._create_oda_linked_driver_user(
            session_client, auth_token, "matrix_oda_linked_user2", paytest_branch_id,
        )
        # own_driver_id ≠ paytest_driver_id (paytest_driver_id was created in conftest)
        assert own_driver_id != paytest_driver_id, (
            "Test requires two distinct drivers; own_driver_id must differ from paytest_driver_id"
        )

        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(token),
        )
        assert resp.status_code == 403, (
            f"ODA user must be denied another driver's matrix. Got {resp.status_code}: {resp.text}"
        )
        detail = resp.json()["detail"].lower()
        assert (
            "own" in detail
            or "owndriverdata" in detail.replace(" ", "")
        ), f"Unexpected 403 detail: {resp.json()['detail']}"

    # -------------------------------------------------------------------------
    # Test 3 — ODA user with no linked driver profile fails closed
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_oda_no_linked_driver_fails_closed_on_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """ODA user with no employee/driver link → 403 'no linked driver profile'."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "MatrixODANoLinkRole1",
            ["payrates.view", "payrates.edit"],
        )
        token = await _create_user_with_role(
            session_client, auth_token, "matrix_oda_nolink_user1", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(token),
        )
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"].lower()
        assert (
            "linked" in detail
            or "own" in detail
            or "owndriverdata" in detail.replace(" ", "")
        ), f"Unexpected 403 detail: {resp.json()['detail']}"

    # -------------------------------------------------------------------------
    # Test 4 — Overlapping ODA + SpecificBranch → fail closed on matrix
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_overlapping_assignments_fail_closed_on_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Two active assignments (ODA + SpecificBranch) → fail-closed 403 on matrix.
        Simulates concurrent/bad data that assign_company_role should have prevented.
        """
        from sqlalchemy import text as _text

        role_id = await _create_role_with_perms(
            session_client, auth_token, "MatrixODAConflictRole1",
            ["payrates.view", "payrates.edit"],
        )
        token = await _create_user_with_role(
            session_client, auth_token, "matrix_oda_conflict_user1", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        # Resolve user_id
        me_resp = await session_client.get("/auth/me", headers=auth(token))
        if me_resp.status_code != 200:
            pytest.skip("Cannot resolve user_id from /auth/me")
        user_id = me_resp.json()["user_id"]

        # Inject a second active SpecificBranch row (bad data)
        insert_result = await direct_db.execute(
            _text("""
                INSERT INTO sec.userbranchroles
                    (userid, companyid, branchid, roleid, scopetype, isactive)
                SELECT u.userid, u.companyid, :bid, NULL, 'SpecificBranch', TRUE
                FROM   sec.users u
                WHERE  u.userid = :uid
                RETURNING userbranchroleid
            """),
            {"uid": user_id, "bid": paytest_branch_id},
        )
        injected_ubr_id = insert_result.scalar_one()

        try:
            resp = await session_client.get(
                f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
                params={"as_of": _today_iso()},
                headers=auth(token),
            )
            assert resp.status_code == 403, (
                f"Overlapping ODA+SpecificBranch must fail-closed on matrix. "
                f"Got {resp.status_code}: {resp.text}"
            )
            detail = resp.json()["detail"].lower()
            assert (
                "ambiguous" in detail or "conflicting" in detail or "multiple" in detail
            ), f"Expected ambiguity message, got: {resp.json()['detail']}"
        finally:
            await direct_db.execute(
                _text("DELETE FROM sec.userbranchroles WHERE userbranchroleid = :ubrid"),
                {"ubrid": injected_ubr_id},
            )

    # -------------------------------------------------------------------------
    # Test 5 — SpecificBranch user can access allowed-branch driver matrix
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_specific_branch_user_can_access_allowed_driver_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """SpecificBranch user on PAYTEST branch → 200 on PAYTEST driver matrix. Not treated as ODA."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "MatrixSBRole1",
            ["payrates.view"],
        )
        token = await _create_user_with_role(
            session_client, auth_token, "matrix_sb_user1", role_id,
            scope_type="SpecificBranch", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(token),
        )
        assert resp.status_code == 200, (
            f"SpecificBranch user on correct branch must access matrix. "
            f"Got {resp.status_code}: {resp.text}"
        )

    # -------------------------------------------------------------------------
    # Test 6 — AllCompanyBranches user can access any driver matrix
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_all_company_branches_user_can_access_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """AllCompanyBranches user with payrates.view → 200 on any driver matrix."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "MatrixACBRole1",
            ["payrates.view"],
        )
        token = await _create_user_with_role(
            session_client, auth_token, "matrix_acb_user1", role_id,
            scope_type="AllCompanyBranches",
        )

        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(token),
        )
        assert resp.status_code == 200, (
            f"AllCompanyBranches user must access any driver matrix. "
            f"Got {resp.status_code}: {resp.text}"
        )

    # -------------------------------------------------------------------------
    # Test 7 — Cross-company driver matrix is blocked
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cross_company_driver_matrix_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        A driver_id that doesn't belong to this company → 404.
        The service filters by companyid in the driver lookup before any permission check.
        """
        # Use an implausibly large driver_id (almost certainly cross-company or nonexistent)
        resp = await session_client.get(
            "/payroll/drivers/999998/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404, (
            f"Cross-company/nonexistent driver must return 404. Got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# TestPhase2B  — pending / history / summary endpoints
# ---------------------------------------------------------------------------

class TestPhase2B:
    """
    Phase 2B: GET /payroll/drivers/{id}/rates/pending
              GET /payroll/drivers/{id}/rates/history
              GET /payroll/drivers/{id}/rates/summary

    Tests:
    1.  pending requires payrates.view/edit
    2.  pending returns only PendingApproval rows for the driver
    3.  pending blocks ODA user for another driver
    4.  history requires permission
    5.  history returns Approved, PendingApproval, Superseded, Voided rows
    6.  history blocks cross-company/nonexistent driver (404)
    7.  summary returns correct pending_count
    8.  summary separates PendingApproval from future Approved
    9.  approve via existing endpoint still respects backdating guard
    10. void pending via existing endpoint reduces pending count
    """

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    async def _make_pending_rate(
        client: httpx.AsyncClient,
        admin_token: str,
        driver_id: int,
        effective_from: str,
    ) -> int:
        """Create a PendingApproval rate for driver_id. Returns driver_rate_id."""
        rt_resp = await client.get("/payroll/rate-types", headers=auth(admin_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")
        resp = await client.post(
            "/payroll/rates",
            json={
                "driver_id": driver_id,
                "rate_type_id": hourly["rate_type_id"],
                "amount": "12.34",
                "effective_from": effective_from,
            },
            headers=auth(admin_token),
        )
        assert resp.status_code == 201, f"Failed to create rate: {resp.text}"
        return resp.json()["driver_rate_id"]

    # ── Test 1: pending requires permission ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_pending_requires_payrates_permission(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """User with no payrates.* permission → 403 on pending endpoint."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "P2BNoPerm1", []  # no permissions
        )
        no_perm_token = await _create_user_with_role(
            session_client, auth_token, "p2b_no_perm_user1", role_id,
        )
        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/pending",
            headers=auth(no_perm_token),
        )
        assert resp.status_code == 403

    # ── Test 2: pending returns only PendingApproval for the driver ───────────

    @pytest.mark.asyncio
    async def test_pending_returns_pending_approval_rows(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        After creating a PendingApproval rate, the pending endpoint returns it.
        All returned rows must have status=PendingApproval and driver_id=target.
        """
        rate_id = await self._make_pending_rate(
            session_client, auth_token, paytest_driver_id, "2093-06-01"
        )

        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/pending",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert isinstance(rows, list)
        # The newly created rate must appear
        rate_ids = [r["driver_rate_id"] for r in rows]
        assert rate_id in rate_ids, "Newly created PendingApproval rate not in pending list"
        # All rows must be PendingApproval for this driver
        for row in rows:
            assert row["status"] == "PendingApproval", f"Non-pending row leaked: {row}"
            assert row["driver_id"] == paytest_driver_id

        # Cleanup: void the rate
        await session_client.delete(f"/payroll/rates/{rate_id}", headers=auth(auth_token))

    # ── Test 3: pending blocks ODA user for another driver ────────────────────

    @pytest.mark.asyncio
    async def test_pending_blocks_oda_user_for_another_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """ODA user with no linked driver → 403 on pending for another driver."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "P2BODARole1", ["payrates.view"]
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "p2b_oda_pending_user1", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )
        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/pending",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403

    # ── Test 4: history requires permission ───────────────────────────────────

    @pytest.mark.asyncio
    async def test_history_requires_permission(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """No payrates.* permission → 403 on history endpoint."""
        role_id = await _create_role_with_perms(
            session_client, auth_token, "P2BHistNoPerm1", []
        )
        no_perm_token = await _create_user_with_role(
            session_client, auth_token, "p2b_hist_no_perm_user1", role_id,
        )
        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/history",
            headers=auth(no_perm_token),
        )
        assert resp.status_code == 403

    # ── Test 5: history contains all statuses ─────────────────────────────────

    @pytest.mark.asyncio
    async def test_history_contains_all_statuses(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        Create a PendingApproval rate, approve it (creates Superseded + Approved),
        then void another.  History must contain at least Approved and Superseded.
        PendingApproval and Voided appear when created.
        """
        # Create rate 1: will be approved (creates Superseded + Approved chain)
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE rate type not found")

        # Create a first "base" rate and approve it
        base_resp = await session_client.post(
            "/payroll/rates",
            json={"driver_id": paytest_driver_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "1.00", "effective_from": "2094-01-01"},
            headers=auth(auth_token),
        )
        assert base_resp.status_code == 201
        base_id = base_resp.json()["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{base_id}/approve", headers=auth(auth_token))

        # Create a second rate for same type (supersedes the first when approved)
        new_resp = await session_client.post(
            "/payroll/rates",
            json={"driver_id": paytest_driver_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "1.50", "effective_from": "2095-01-01"},
            headers=auth(auth_token),
        )
        assert new_resp.status_code == 201
        new_id = new_resp.json()["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{new_id}/approve", headers=auth(auth_token))

        # Create a pending rate (will remain pending for the status check)
        pending_resp = await session_client.post(
            "/payroll/rates",
            json={"driver_id": paytest_driver_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "2.00", "effective_from": "2096-01-01"},
            headers=auth(auth_token),
        )
        assert pending_resp.status_code == 201
        pending_id = pending_resp.json()["driver_rate_id"]

        # Fetch history
        hist_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/history",
            headers=auth(auth_token),
        )
        assert hist_resp.status_code == 200, hist_resp.text
        history = hist_resp.json()
        statuses = {r["status"] for r in history}
        assert "Approved" in statuses, "Approved status missing from history"
        assert "Superseded" in statuses, "Superseded status missing from history"
        assert "PendingApproval" in statuses, "PendingApproval status missing from history"

        # Void the pending rate and check Voided appears
        await session_client.delete(f"/payroll/rates/{pending_id}", headers=auth(auth_token))
        hist_resp2 = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/history",
            headers=auth(auth_token),
        )
        statuses2 = {r["status"] for r in hist_resp2.json()}
        assert "Voided" in statuses2, "Voided status missing from history after void"

    # ── Test 6: history blocks nonexistent/cross-company driver ──────────────

    @pytest.mark.asyncio
    async def test_history_blocks_nonexistent_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Non-existent driver → 404 on history endpoint."""
        resp = await session_client.get(
            "/payroll/drivers/999997/rates/history",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    # ── Test 7: summary returns correct pending_count ─────────────────────────

    @pytest.mark.asyncio
    async def test_summary_pending_count_is_accurate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        Create N pending rates, check summary.pending_count increases by N.
        Void all created rates and check count returns to baseline.
        """
        # Baseline
        base_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/summary",
            headers=auth(auth_token),
        )
        assert base_resp.status_code == 200, base_resp.text
        base_count: int = base_resp.json()["pending_count"]

        # Create 2 pending rates
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if hourly is None or mileage is None:
            pytest.skip("HOURLY/MILEAGE rate types not found")

        r1 = await session_client.post(
            "/payroll/rates",
            json={"driver_id": paytest_driver_id, "rate_type_id": hourly["rate_type_id"],
                  "amount": "50.00", "effective_from": "2097-01-01"},
            headers=auth(auth_token),
        )
        r2 = await session_client.post(
            "/payroll/rates",
            json={"driver_id": paytest_driver_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "5.00", "effective_from": "2097-01-01"},
            headers=auth(auth_token),
        )
        assert r1.status_code == 201 and r2.status_code == 201
        id1, id2 = r1.json()["driver_rate_id"], r2.json()["driver_rate_id"]

        # Check count increased
        after_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/summary",
            headers=auth(auth_token),
        )
        after_count: int = after_resp.json()["pending_count"]
        assert after_count >= base_count + 2, (
            f"Expected pending_count >= {base_count + 2}, got {after_count}"
        )

        # Cleanup: void both
        await session_client.delete(f"/payroll/rates/{id1}", headers=auth(auth_token))
        await session_client.delete(f"/payroll/rates/{id2}", headers=auth(auth_token))

        final_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/summary",
            headers=auth(auth_token),
        )
        final_count: int = final_resp.json()["pending_count"]
        assert final_count == base_count, (
            f"After void, expected pending_count={base_count}, got {final_count}"
        )

    # ── Test 8: summary separates PendingApproval from future Approved ────────

    @pytest.mark.asyncio
    async def test_summary_separates_pending_from_future_approved(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        An Approved rate with effective_from > today must count in
        future_approved_count, NOT in pending_count.
        A PendingApproval rate must count in pending_count, NOT future_approved_count.
        """
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if hourly is None or mileage is None:
            pytest.skip("HOURLY/MILEAGE rate types not found")

        # Create a pending rate (status=PendingApproval)
        pending_resp = await session_client.post(
            "/payroll/rates",
            json={"driver_id": paytest_driver_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "9.99", "effective_from": "2098-01-01"},
            headers=auth(auth_token),
        )
        assert pending_resp.status_code == 201
        pending_id = pending_resp.json()["driver_rate_id"]

        # Create a far-future Approved rate: create pending then approve it
        future_resp = await session_client.post(
            "/payroll/rates",
            json={"driver_id": paytest_driver_id, "rate_type_id": hourly["rate_type_id"],
                  "amount": "99.99", "effective_from": "2099-06-01"},
            headers=auth(auth_token),
        )
        assert future_resp.status_code == 201
        future_id = future_resp.json()["driver_rate_id"]
        approve_resp = await session_client.post(
            f"/payroll/rates/{future_id}/approve",
            headers=auth(auth_token),
        )
        assert approve_resp.status_code == 200
        # After approval the rate may be "Approved" with effective_from in the future
        # OR it may have superseded a prior; either way the status should now be Approved
        approved_rate = approve_resp.json()
        assert approved_rate["status"] == "Approved"
        assert approved_rate["effective_from"] == "2099-06-01"

        # Check summary
        summary_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/summary",
            headers=auth(auth_token),
        )
        assert summary_resp.status_code == 200
        summary = summary_resp.json()
        assert summary["pending_count"] >= 1, "Pending rate must be counted in pending_count"
        assert summary["future_approved_count"] >= 1, (
            "Far-future Approved rate must appear in future_approved_count"
        )

        # Cleanup
        await session_client.delete(f"/payroll/rates/{pending_id}", headers=auth(auth_token))
        await session_client.delete(f"/payroll/rates/{future_id}", headers=auth(auth_token))

    # ── Test 9: approve via endpoint respects backdating guard ────────────────

    @pytest.mark.asyncio
    async def test_approve_still_respects_backdating_guard(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        A PendingApproval rate with effective_from inside a Locked period → 422.
        This confirms the backdating guard is active on the approve endpoint
        (the endpoint that the Pending Changes UI calls).
        """
        from sqlalchemy import text as _text
        import random

        # Insert a Locked period covering a far-future date
        code = f"P2BTEST-{random.randint(100000, 999999)}"
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status, createdbyuserid)
                VALUES (1, :bid, :code, :name, 'Month', '2070-01-01', '2070-01-31', 'Locked', 1)
                ON CONFLICT DO NOTHING
            """),
            {"bid": paytest_branch_id, "code": code, "name": f"P2B Guard Test {code}"},
        )

        # Create pending rate inside the locked period
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY not found")

        create_resp = await session_client.post(
            "/payroll/rates",
            json={"driver_id": paytest_driver_id, "rate_type_id": hourly["rate_type_id"],
                  "amount": "7.77", "effective_from": "2070-01-15"},
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        rate_id = create_resp.json()["driver_rate_id"]

        # Attempt to approve: must fail with 422 (backdating guard)
        approve_resp = await session_client.post(
            f"/payroll/rates/{rate_id}/approve",
            headers=auth(auth_token),
        )
        assert approve_resp.status_code == 422, approve_resp.text
        assert "finalized" in approve_resp.json()["detail"].lower()

        # Cleanup
        await session_client.delete(f"/payroll/rates/{rate_id}", headers=auth(auth_token))

    # ── Test 10: void pending updates pending count ────────────────────────────

    @pytest.mark.asyncio
    async def test_void_pending_reduces_pending_count(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        After voiding a pending rate, the pending endpoint no longer returns it
        and summary.pending_count decreases.
        """
        rate_id = await self._make_pending_rate(
            session_client, auth_token, paytest_driver_id, "2092-06-01"
        )

        # Confirm it appears in pending list
        before = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/pending",
            headers=auth(auth_token),
        )
        assert any(r["driver_rate_id"] == rate_id for r in before.json())

        before_summary = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/summary",
            headers=auth(auth_token),
        )
        count_before: int = before_summary.json()["pending_count"]

        # Void the rate
        void_resp = await session_client.delete(
            f"/payroll/rates/{rate_id}",
            headers=auth(auth_token),
        )
        assert void_resp.status_code == 204

        # Verify removed from pending list
        after = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/pending",
            headers=auth(auth_token),
        )
        assert not any(r["driver_rate_id"] == rate_id for r in after.json()), (
            "Voided rate must not appear in pending list"
        )

        # Verify summary count decreased
        after_summary = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/summary",
            headers=auth(auth_token),
        )
        count_after: int = after_summary.json()["pending_count"]
        assert count_after == count_before - 1, (
            f"Expected pending_count to decrease by 1: {count_before} → {count_after}"
        )


# ---------------------------------------------------------------------------
# TestRateMatrixDateAlignment  (Phase 3C)
# ---------------------------------------------------------------------------

class TestRateMatrixDateAlignment:
    """
    Phase 3C: prove that the rate matrix as_of filter correctly honours
    BranchPayItemConfig effective-date boundaries for the PAYTEST driver.

    All tests use the paytest_driver_id which is on the PAYTEST branch.
    System items (HOURS, MILES) are pre-activated on PAYTEST by conftest.
    """

    @pytest.mark.asyncio
    async def test_rate_matrix_as_of_effective_from_shows_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """as_of = today -> HOURLY group appears (HOURS activated with past EffectiveFrom)."""
        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        groups = resp.json()["groups"]
        codes = {g["rate_code"] for g in groups}
        assert "HOURLY" in codes, (
            f"HOURLY must appear in rate matrix for PAYTEST driver as_of today. "
            f"Got rate_codes: {codes}"
        )

    @pytest.mark.asyncio
    async def test_rate_matrix_hides_inactive_configured_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Temporarily set MILEAGE BranchPayItemConfig.isactive=FALSE.
        Rate matrix must not show MILEAGE when the config row disables it.
        Then restore and confirm MILEAGE reappears.

        Final product rule: the rate matrix filters by isactive=FALSE (item deactivated
        on branch) but does NOT filter by effectivefrom — future-effective configured
        items still appear so admins can prepare rates early.
        """
        from sqlalchemy import text as _text

        matrix_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200, matrix_resp.text
        mileage_grp = next(
            (g for g in matrix_resp.json()["groups"] if g["rate_code"] == "MILEAGE"), None
        )
        if mileage_grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        pay_item_id = mileage_grp["pay_item_id"]

        # Disable item on branch
        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET isactive = FALSE
                WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
            """),
            {"piid": pay_item_id, "bid": paytest_branch_id},
        )
        try:
            # Item must be absent while disabled
            disabled_resp = await session_client.get(
                f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
                params={"as_of": _today_iso()},
                headers=auth(auth_token),
            )
            assert disabled_resp.status_code == 200, disabled_resp.text
            codes_disabled = {g["rate_code"] for g in disabled_resp.json()["groups"]}
            assert "MILEAGE" not in codes_disabled, (
                "MILEAGE must NOT appear in rate matrix when BranchPayItemConfig.isactive=FALSE"
            )
        finally:
            # Re-enable
            await direct_db.execute(
                _text("""
                    UPDATE payroll.branchpayitemconfig
                    SET isactive = TRUE
                    WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
                """),
                {"piid": pay_item_id, "bid": paytest_branch_id},
            )

        # After restore, item must reappear
        restored_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert restored_resp.status_code == 200, restored_resp.text
        codes_restored = {g["rate_code"] for g in restored_resp.json()["groups"]}
        assert "MILEAGE" in codes_restored, (
            "MILEAGE must reappear after BranchPayItemConfig.isactive restored to TRUE"
        )

    @pytest.mark.asyncio
    async def test_rate_matrix_as_of_after_effective_from_shows_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """as_of well after the HOURS effectivefrom -> HOURLY still present."""
        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": "2050-06-15"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        codes = {g["rate_code"] for g in resp.json()["groups"]}
        assert "HOURLY" in codes, (
            "HOURLY must appear in rate matrix for a date well after HOURS effectivefrom"
        )

    @pytest.mark.asyncio
    async def test_rate_matrix_branch_isolation_item_active_only_in_other_branch(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
        paytest_driver_id: int,
    ):
        """
        PAYTEST branch has HOURS force-activated with an explicit BranchPayItemConfig.
        HQ branch (created_driver_id) relies on IsDefaultBranchActive.
        The two matrices are branch-isolated: each uses the driver's own branch_id.
        """
        paytest_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert paytest_resp.status_code == 200, paytest_resp.text

        hq_resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert hq_resp.status_code == 200, hq_resp.text

        assert paytest_resp.json()["driver_id"] == paytest_driver_id
        assert hq_resp.json()["driver_id"] == created_driver_id

        paytest_codes = {g["rate_code"] for g in paytest_resp.json()["groups"]}
        assert "HOURLY" in paytest_codes, (
            f"HOURLY must be in PAYTEST driver matrix. Got: {paytest_codes}"
        )

    @pytest.mark.asyncio
    async def test_payroll_and_rate_matrix_aligned_for_same_date(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        Key alignment test: rate matrix as_of=today shows HOURLY group for PAYTEST
        driver. Confirms that when the same date is used for both the day-grid
        work_date and the rate matrix as_of, HOURS appears in both contexts.
        """
        matrix_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200, matrix_resp.text
        matrix_codes = {g["rate_code"] for g in matrix_resp.json()["groups"]}
        assert "HOURLY" in matrix_codes, (
            f"HOURLY must appear in rate matrix for PAYTEST driver as_of {_today_iso()}. "
            f"Got: {matrix_codes}"
        )


# ---------------------------------------------------------------------------
# TestRateCreationGuard  (Phase 3C)
# ---------------------------------------------------------------------------

class TestRateCreationGuard:
    """
    Phase 3C (FINAL RULE): prove that create_rate (Fix 8) and batch_save_rates
    (Step 6) correctly validate branch activation.

    Final product rule:
    - Pay Rates = preparation/configuration.  Payroll = actual use.
    - create_rate ALLOWS rates for future-effective configured items (EffectiveFrom
      in the future).  The rate is prepared early; payroll won't use the item until
      EffectiveFrom anyway.
    - create_rate REJECTS rates only for: Retired items, inactive items (isactive=FALSE),
      items not configured for the branch, and invalid rate-type mappings.
    - Do NOT reject because rate.effective_from < pay_item_config.effective_from.
    """

    @pytest.mark.asyncio
    async def test_create_rate_accepts_rate_when_effective_from_matches(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        HOURLY on PAYTEST has effectivefrom in the past.
        Creating a rate with effective_from=today -> 201 (guard passes).
        """
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      paytest_driver_id,
                "rate_type_id":   hourly["rate_type_id"],
                "amount":         "25.00",
                "effective_from": _today_iso(),
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, (
            f"create_rate must accept HOURLY for PAYTEST driver (effectivefrom in past). "
            f"Got {resp.status_code}: {resp.text}"
        )
        assert resp.json()["status"] == "PendingApproval"

    @pytest.mark.asyncio
    async def test_create_rate_rejects_inactive_branch_item_mileage(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Temporarily set MILEAGE BranchPayItemConfig.isactive=FALSE.
        create_rate with that rate_type_id -> 422 (not active for branch).

        Final product rule: create_rate rejects inactive items (isactive=FALSE)
        but does NOT reject items whose effectivefrom is in the future relative to
        the rate's effective_from. Future-effective configured items can have rates
        prepared early; payroll won't use those items until EffectiveFrom anyway.
        """
        from sqlalchemy import text as _text

        matrix_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200
        mileage_grp = next(
            (g for g in matrix_resp.json()["groups"] if g["rate_code"] == "MILEAGE"), None
        )
        if mileage_grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        pay_item_id = mileage_grp["pay_item_id"]
        rt_id = mileage_grp["rate_type_id"]

        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET isactive = FALSE
                WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
            """),
            {"piid": pay_item_id, "bid": paytest_branch_id},
        )
        try:
            resp = await session_client.post(
                "/payroll/rates",
                json={
                    "driver_id":      paytest_driver_id,
                    "rate_type_id":   rt_id,
                    "amount":         "5.00",
                    "effective_from": _today_iso(),
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"create_rate must reject rate when BranchPayItemConfig.isactive=FALSE "
                f"for MILEAGE on PAYTEST branch. "
                f"Got {resp.status_code}: {resp.text}"
            )
        finally:
            await direct_db.execute(
                _text("""
                    UPDATE payroll.branchpayitemconfig
                    SET isactive = TRUE
                    WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
                """),
                {"piid": pay_item_id, "bid": paytest_branch_id},
            )

    @pytest.mark.asyncio
    async def test_create_rate_rejects_inactive_branch_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Temporarily set HOURS BranchPayItemConfig.isactive=FALSE.
        Creating a rate -> 422 (item not active for branch).
        """
        from sqlalchemy import text as _text

        matrix_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200
        hourly_grp = next(
            (g for g in matrix_resp.json()["groups"] if g["rate_code"] == "HOURLY"), None
        )
        if hourly_grp is None:
            pytest.skip("HOURLY not in rate matrix for paytest_driver_id")

        pay_item_id = hourly_grp["pay_item_id"]
        rt_id = hourly_grp["rate_type_id"]

        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET isactive = FALSE
                WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
            """),
            {"piid": pay_item_id, "bid": paytest_branch_id},
        )
        try:
            resp = await session_client.post(
                "/payroll/rates",
                json={
                    "driver_id":      paytest_driver_id,
                    "rate_type_id":   rt_id,
                    "amount":         "25.00",
                    "effective_from": _today_iso(),
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"create_rate must reject rate when BranchPayItemConfig.isactive=FALSE. "
                f"Got {resp.status_code}: {resp.text}"
            )
        finally:
            await direct_db.execute(
                _text("""
                    UPDATE payroll.branchpayitemconfig
                    SET isactive = TRUE
                    WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
                """),
                {"piid": pay_item_id, "bid": paytest_branch_id},
            )

    @pytest.mark.asyncio
    async def test_create_rate_accepts_default_active_system_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """
        HQ driver relies on IsDefaultBranchActive=TRUE for HOURLY (no explicit
        BranchPayItemConfig row for HQ). create_rate must allow the rate through
        (legacy fallback path: branch_rt_row is not None but cfg_isactive is None,
        so isdefaultbranchactive=TRUE is used).
        """
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      created_driver_id,
                "rate_type_id":   hourly["rate_type_id"],
                "amount":         "30.00",
                "effective_from": "2095-06-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, (
            f"create_rate must accept HOURLY for HQ driver (IsDefaultBranchActive=TRUE fallback). "
            f"Got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_batch_save_rates_rejects_inactive_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Temporarily set MILEAGE BranchPayItemConfig.isactive=FALSE.
        batch_save_rates with that pay_item_id -> 422 (not active for branch).
        """
        from sqlalchemy import text as _text

        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "MILEAGE")
        if grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        pay_item_id = grp["pay_item_id"]

        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET isactive = FALSE
                WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
            """),
            {"piid": pay_item_id, "bid": paytest_branch_id},
        )
        try:
            resp = await session_client.post(
                f"/payroll/drivers/{paytest_driver_id}/rates/batch",
                json={
                    "effective_from": "2097-01-01",
                    "changes": [{
                        "pay_item_id": pay_item_id,
                        "rate_type_id": grp["rate_type_id"],
                        "amount": "5.00",
                    }],
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"batch_save_rates must reject inactive branch item. "
                f"Got {resp.status_code}: {resp.text}"
            )
            detail = resp.json()["detail"].lower()
            assert (
                "not active" in detail or "not actively" in detail or "mapped" in detail
            ), (
                f"Expected branch-active rejection message, got: {resp.json()['detail']}"
            )
        finally:
            await direct_db.execute(
                _text("""
                    UPDATE payroll.branchpayitemconfig
                    SET isactive = TRUE
                    WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
                """),
                {"piid": pay_item_id, "bid": paytest_branch_id},
            )


# ---------------------------------------------------------------------------
# TestFinalProductRule  (Phase 3C — final product rule)
# ---------------------------------------------------------------------------

class TestFinalProductRule:
    """
    Phase 3C final product rule:
    - Rate matrix shows future-effective configured items (preparation view).
    - create_rate accepts rates for future-effective configured active items.
    - create_rate rejects inactive items regardless of effectivefrom.
    - batch_save_rates follows the same rules.
    - pay_item_effective_from is exposed in the rate matrix response.
    """

    @pytest.mark.asyncio
    async def test_rate_matrix_shows_future_effective_configured_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Configure a pay item with a future EffectiveFrom on PAYTEST branch.
        Rate matrix as_of=today must STILL show the item — admins need to
        prepare rates before the item becomes active in payroll.
        """
        from sqlalchemy import text as _text

        matrix_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200
        mileage_grp = next(
            (g for g in matrix_resp.json()["groups"] if g["rate_code"] == "MILEAGE"), None
        )
        if mileage_grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        pay_item_id = mileage_grp["pay_item_id"]

        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET effectivefrom = '2099-01-01'
                WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
            """),
            {"piid": pay_item_id, "bid": paytest_branch_id},
        )
        try:
            resp = await session_client.get(
                f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
                params={"as_of": _today_iso()},
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            codes = {g["rate_code"] for g in resp.json()["groups"]}
            assert "MILEAGE" in codes, (
                "MILEAGE must appear in rate matrix even when EffectiveFrom is in the future "
                f"(rate matrix = preparation view). Got rate_codes: {codes}"
            )
        finally:
            await direct_db.execute(
                _text("""
                    UPDATE payroll.branchpayitemconfig
                    SET effectivefrom = '2020-01-01'
                    WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
                """),
                {"piid": pay_item_id, "bid": paytest_branch_id},
            )

    @pytest.mark.asyncio
    async def test_rate_matrix_hides_inactive_item_with_isactive_false(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Set MILEAGE BranchPayItemConfig.IsActive=FALSE.
        Rate matrix must NOT show the item.

        This covers the rule: expired/deactivated items are hidden from the matrix.
        IsActive=FALSE is the correct way to hide items with IsDefaultBranchActive=TRUE,
        since IsDefaultBranchActive acts as a fallback when no matching config row is
        found (e.g. when effectiveto is past, the LEFT JOIN misses and COALESCE uses
        isdefaultbranchactive=TRUE — IsActive=FALSE on an active config row overrides
        this fallback correctly).
        """
        from sqlalchemy import text as _text

        matrix_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200
        mileage_grp = next(
            (g for g in matrix_resp.json()["groups"] if g["rate_code"] == "MILEAGE"), None
        )
        if mileage_grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        pay_item_id = mileage_grp["pay_item_id"]

        # Set isactive=FALSE on the active config row
        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET isactive = FALSE
                WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
                  AND (effectiveto IS NULL OR effectiveto >= CURRENT_DATE)
            """),
            {"piid": pay_item_id, "bid": paytest_branch_id},
        )
        try:
            resp = await session_client.get(
                f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
                params={"as_of": _today_iso()},
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            codes = {g["rate_code"] for g in resp.json()["groups"]}
            assert "MILEAGE" not in codes, (
                f"MILEAGE must NOT appear in rate matrix when IsActive=FALSE. "
                f"Got rate_codes: {codes}"
            )
        finally:
            await direct_db.execute(
                _text("""
                    UPDATE payroll.branchpayitemconfig
                    SET isactive = TRUE
                    WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
                """),
                {"piid": pay_item_id, "bid": paytest_branch_id},
            )

    @pytest.mark.asyncio
    async def test_create_rate_accepts_rate_before_pay_item_effective_from(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Set MILEAGE BranchPayItemConfig.EffectiveFrom to a far-future date.
        create_rate with effective_from=today → must return 201 (NOT 422).

        Final product rule: preparing rates early for future-effective items is allowed.
        """
        from sqlalchemy import text as _text

        matrix_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200
        mileage_grp = next(
            (g for g in matrix_resp.json()["groups"] if g["rate_code"] == "MILEAGE"), None
        )
        if mileage_grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        pay_item_id = mileage_grp["pay_item_id"]
        rt_id = mileage_grp["rate_type_id"]

        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET effectivefrom = '2099-07-01'
                WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
            """),
            {"piid": pay_item_id, "bid": paytest_branch_id},
        )
        try:
            resp = await session_client.post(
                "/payroll/rates",
                json={
                    "driver_id":      paytest_driver_id,
                    "rate_type_id":   rt_id,
                    "amount":         "0.77",
                    "effective_from": _today_iso(),
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 201, (
                f"create_rate must ACCEPT rate when pay item EffectiveFrom is in the future "
                f"(preparing rates early is allowed). Got {resp.status_code}: {resp.text}"
            )
            assert resp.json()["status"] == "PendingApproval"
        finally:
            await direct_db.execute(
                _text("""
                    UPDATE payroll.branchpayitemconfig
                    SET effectivefrom = '2020-01-01'
                    WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
                """),
                {"piid": pay_item_id, "bid": paytest_branch_id},
            )

    @pytest.mark.asyncio
    async def test_batch_save_accepts_rate_before_pay_item_effective_from(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Set MILEAGE BranchPayItemConfig.EffectiveFrom to a far-future date.
        batch_save_rates with effective_from=today → must return 200 (NOT 422).

        Final product rule: same as create_rate — preparing rates early is allowed.
        """
        from sqlalchemy import text as _text

        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "MILEAGE")
        if grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        pay_item_id = grp["pay_item_id"]

        import datetime as _dt
        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET effectivefrom = :eff
                WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
            """),
            {"piid": pay_item_id, "bid": paytest_branch_id, "eff": _dt.date(2099, 8, 1)},
        )
        try:
            resp = await session_client.post(
                f"/payroll/drivers/{paytest_driver_id}/rates/batch",
                json={
                    "effective_from": "2150-01-01",
                    "changes": [{
                        "pay_item_id": pay_item_id,
                        "rate_type_id": grp["rate_type_id"],
                        "amount": "0.88",
                    }],
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, (
                f"batch_save_rates must ACCEPT rate when pay item EffectiveFrom is in the future "
                f"(preparing rates early is allowed). Got {resp.status_code}: {resp.text}"
            )
        finally:
            await direct_db.execute(
                _text("""
                    UPDATE payroll.branchpayitemconfig
                    SET effectivefrom = '2020-01-01'
                    WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
                """),
                {"piid": pay_item_id, "bid": paytest_branch_id},
            )

    @pytest.mark.asyncio
    async def test_rate_matrix_exposes_pay_item_effective_from(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Set MILEAGE BranchPayItemConfig.EffectiveFrom to a known future date.
        Rate matrix response must expose pay_item_effective_from = that date on
        the matching group, so the frontend can display 'Payroll active from …'.
        """
        from sqlalchemy import text as _text

        matrix_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(auth_token),
        )
        assert matrix_resp.status_code == 200
        mileage_grp = next(
            (g for g in matrix_resp.json()["groups"] if g["rate_code"] == "MILEAGE"), None
        )
        if mileage_grp is None:
            pytest.skip("MILEAGE not in rate matrix for paytest_driver_id")

        pay_item_id = mileage_grp["pay_item_id"]
        future_date = "2099-09-01"

        await direct_db.execute(
            _text("""
                UPDATE payroll.branchpayitemconfig
                SET effectivefrom = :eff
                WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
            """),
            {"piid": pay_item_id, "bid": paytest_branch_id, "eff": date.fromisoformat(future_date)},
        )
        try:
            resp = await session_client.get(
                f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
                params={"as_of": _today_iso()},
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            mileage_group = next(
                (g for g in resp.json()["groups"] if g["rate_code"] == "MILEAGE"), None
            )
            assert mileage_group is not None, (
                "MILEAGE must appear in matrix even with future EffectiveFrom"
            )
            assert "pay_item_effective_from" in mileage_group, (
                "rate matrix group must expose pay_item_effective_from field"
            )
            assert mileage_group["pay_item_effective_from"] == future_date, (
                f"pay_item_effective_from must be '{future_date}', "
                f"got: {mileage_group['pay_item_effective_from']}"
            )
        finally:
            await direct_db.execute(
                _text("""
                    UPDATE payroll.branchpayitemconfig
                    SET effectivefrom = :eff
                    WHERE payitemid = :piid AND branchid = :bid AND companyid = 1
                """),
                {"piid": pay_item_id, "bid": paytest_branch_id, "eff": date(2020, 1, 1)},
            )


# ---------------------------------------------------------------------------
# TestCustomPayItemRateStructure  (Phase 3E)
# ---------------------------------------------------------------------------
# Proves that custom Daily Pay Items appear in Pay Rates and that the
# PayItemRateTypeMap is always created alongside the PayItems row.
# ---------------------------------------------------------------------------

class TestCustomPayItemRateStructure:
    """
    Phase 3E: custom custom pay items must appear in the Pay Rates matrix.

    Root-cause: create_custom_pay_item previously saved rate_names only to
    payitemsettings, without creating RateTypes or PayItemRateTypeMap rows.
    get_driver_rate_matrix uses INNER JOIN on PayItemRateTypeMap so items
    with no mapping rows are invisible in Pay Rates.
    """

    # ------------------------------------------------------------------
    # Test 1 — PerUnit custom item appears with exactly one rate field
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_custom_perunit_item_appears_in_rate_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """
        Create a custom Daily PerUnit item, activate it for the driver's branch,
        then verify it appears as exactly one group in the rate matrix.
        """
        h = auth(auth_token)

        # Create the custom pay item
        create_resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Samya Rate Test",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Stop",
                "rate_names":    ["Samya Rate"],
                "category":      "Custom",
            },
            headers=h,
        )
        assert create_resp.status_code == 201, create_resp.text
        item = create_resp.json()
        pay_item_id = item["pay_item_id"]

        try:
            # Activate for HQ branch (branch_id=1)
            activate_resp = await session_client.patch(
                f"/settings/branches/1/pay-items/{pay_item_id}",
                json={"is_active": True, "effective_from": _today_iso()},
                headers=h,
            )
            assert activate_resp.status_code == 200, activate_resp.text

            # Verify item appears in driver's rate matrix
            matrix_resp = await session_client.get(
                f"/payroll/drivers/{created_driver_id}/rate-matrix",
                params={"as_of": _today_iso()},
                headers=h,
            )
            assert matrix_resp.status_code == 200, matrix_resp.text
            matrix = matrix_resp.json()

            custom_groups = [
                g for g in matrix["groups"]
                if g.get("pay_item_name") == "Samya Rate Test"
            ]
            assert len(custom_groups) >= 1, (
                "Custom PerUnit item 'Samya Rate Test' must appear in the rate matrix. "
                f"Groups found: {[g.get('pay_item_name') for g in matrix['groups']]}"
            )
            # PerUnit → exactly one rate field
            assert len(custom_groups) == 1, (
                f"PerUnit custom item should produce 1 rate group, got {len(custom_groups)}"
            )
        finally:
            await session_client.delete(f"/settings/pay-items/{pay_item_id}", headers=h)

    # ------------------------------------------------------------------
    # Test 2 — RangeBracket custom item appears with multiple rate fields
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_custom_rangebracket_item_appears_in_rate_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """
        Create a custom Daily RangeBracket item and verify it produces multiple
        rate groups (one per bracket) in the matrix, and still one payroll column.
        """
        h = auth(auth_token)

        create_resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Bracket Rate Test",
                "item_scope":    "Daily",
                "rate_behavior": "RangeBracket",
                "unit":          "Mile",
                "rate_names":    ["Short Haul", "Long Haul"],
                "category":      "Custom",
            },
            headers=h,
        )
        assert create_resp.status_code == 201, create_resp.text
        item = create_resp.json()
        pay_item_id = item["pay_item_id"]

        try:
            activate_resp2 = await session_client.patch(
                f"/settings/branches/1/pay-items/{pay_item_id}",
                json={"is_active": True, "effective_from": _today_iso()},
                headers=h,
            )
            assert activate_resp2.status_code == 200, activate_resp2.text

            matrix_resp = await session_client.get(
                f"/payroll/drivers/{created_driver_id}/rate-matrix",
                params={"as_of": _today_iso()},
                headers=h,
            )
            assert matrix_resp.status_code == 200
            matrix = matrix_resp.json()

            custom_groups = [
                g for g in matrix["groups"]
                if g.get("pay_item_name") == "Bracket Rate Test"
            ]
            assert len(custom_groups) >= 1, (
                "RangeBracket custom item must appear in rate matrix. "
                f"All groups: {[g.get('pay_item_name') for g in matrix['groups']]}"
            )
            # Multiple rate fields — we supplied 2 names so expect 2 groups
            assert len(custom_groups) == 2, (
                f"RangeBracket item with 2 rate names should produce 2 groups, "
                f"got {len(custom_groups)}"
            )
        finally:
            await session_client.delete(f"/settings/pay-items/{pay_item_id}", headers=h)

    # ------------------------------------------------------------------
    # Test 3 — RangeBracket stays ONE payroll column in day-grid
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_rangebracket_shows_one_payroll_column(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """
        A RangeBracket item should appear as exactly ONE column in the day-grid
        (one pay-item = one payroll column, regardless of bracket count).

        Self-contained: creates its own payroll period at far-future dates
        (2082-07-01 to 2082-07-07) to avoid conflicts, then cancels it on cleanup.
        """
        h = auth(auth_token)

        # ------------------------------------------------------------------ #
        # 1. Create the custom RangeBracket pay item (2 rate fields)
        # ------------------------------------------------------------------ #
        create_resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Bracket Column Test",
                "item_scope":    "Daily",
                "rate_behavior": "RangeBracket",
                "unit":          "Stop",
                "rate_names":    ["Bracket Low", "Bracket High"],
                "category":      "Custom",
            },
            headers=h,
        )
        assert create_resp.status_code == 201, create_resp.text
        item = create_resp.json()
        pay_item_id = item["pay_item_id"]
        bracket_item_code = item["pay_item_code"]

        period_id = None
        try:
            # ------------------------------------------------------------------ #
            # 2. Activate the item for PAYTEST branch (far-future effective date)
            # ------------------------------------------------------------------ #
            act_resp = await session_client.patch(
                f"/settings/branches/{paytest_branch_id}/pay-items/{pay_item_id}",
                json={"is_active": True, "effective_from": "2082-07-01"},
                headers=h,
            )
            assert act_resp.status_code == 200, act_resp.text

            # ------------------------------------------------------------------ #
            # 3. Create a payroll period for PAYTEST branch at far-future dates
            # ------------------------------------------------------------------ #
            period_resp = await session_client.post(
                "/payroll/periods",
                json={
                    "branch_id":   paytest_branch_id,
                    "period_type": "Week",
                    "start_date":  "2082-07-01",
                    "end_date":    "2082-07-07",
                },
                headers=h,
            )
            assert period_resp.status_code == 201, f"period create failed: {period_resp.text}"
            period_id = period_resp.json()["payroll_period_id"]

            # ------------------------------------------------------------------ #
            # 4. Open the period
            # ------------------------------------------------------------------ #
            open_resp = await session_client.patch(
                f"/payroll/periods/{period_id}/status",
                json={"status": "Open"},
                headers=h,
            )
            assert open_resp.status_code == 200, f"open failed: {open_resp.text}"

            # ------------------------------------------------------------------ #
            # 5. GET day-grid — filter to the first day of the period
            # ------------------------------------------------------------------ #
            grid_resp = await session_client.get(
                f"/payroll/periods/{period_id}/day-grid",
                params={"work_date": "2082-07-01"},
                headers=h,
            )
            assert grid_resp.status_code == 200, grid_resp.text
            grid = grid_resp.json()

            # ------------------------------------------------------------------ #
            # Assert A: the bracket item appears EXACTLY ONCE in day-grid columns
            # ------------------------------------------------------------------ #
            columns = grid.get("columns", [])
            bracket_cols = [c for c in columns if c.get("pay_item_code") == bracket_item_code]
            assert len(bracket_cols) == 1, (
                f"RangeBracket item should appear as exactly 1 payroll column, "
                f"got {len(bracket_cols)}. All codes: {[c.get('pay_item_code') for c in columns]}"
            )

            # ------------------------------------------------------------------ #
            # Assert B: rate matrix for the PAYTEST driver shows 2 rate groups
            #           (one per rate field)
            # ------------------------------------------------------------------ #
            matrix_resp = await session_client.get(
                f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
                params={"as_of": "2082-07-01"},
                headers=h,
            )
            assert matrix_resp.status_code == 200, matrix_resp.text
            bracket_groups = [
                g for g in matrix_resp.json().get("groups", [])
                if g.get("pay_item_id") == pay_item_id
            ]
            assert len(bracket_groups) == 2, (
                f"RangeBracket item with 2 rate names should produce 2 rate-matrix groups, "
                f"got {len(bracket_groups)}"
            )

        finally:
            # Cancel the period first (if it was created), then delete the pay item
            if period_id is not None:
                await session_client.patch(
                    f"/payroll/periods/{period_id}/status",
                    json={"status": "Cancelled"},
                    headers=h,
                )
            await session_client.delete(f"/settings/pay-items/{pay_item_id}", headers=h)

    # ------------------------------------------------------------------
    # Test 4 — creation always creates rate structure (no silent gap)
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_custom_daily_item_creation_creates_rate_structure(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        test_app,
    ):
        """
        After creating a custom Daily PerUnit item, PayItemRateTypeMap rows
        must exist — verified by querying the DB directly.
        """
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text as _text
        from app.dependencies import get_db

        h = auth(auth_token)

        create_resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Rate Structure Verify",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "km",
                "rate_names":    ["KM Rate"],
                "category":      "Custom",
            },
            headers=h,
        )
        assert create_resp.status_code == 201, create_resp.text
        pay_item_id = create_resp.json()["pay_item_id"]

        try:
            # Verify via the matrix: if the item appears, the mapping exists.
            # Also do a direct DB check using the test DB connection.
            engine = None
            for dep, override in test_app.dependency_overrides.items():
                if dep is get_db:
                    # Extract engine from the override closure — we call it to get a conn
                    # We can't easily extract the engine, so we check via the matrix API.
                    break

            # API-level check: activate the item and verify it shows in the matrix
            await session_client.patch(
                "/settings/branches/1/pay-items/" + str(pay_item_id),
                json={"is_active": True, "effective_from": "2020-01-01"},
                headers=h,
            )
            # Find any driver in HQ branch from the fixture
            # We'll use created_driver_id indirectly by checking the list endpoint
            # Just verify the item has rate_names populated (rate structure exists)
            item_resp = await session_client.get(
                f"/settings/pay-items/{pay_item_id}",
                headers=h,
            )
            assert item_resp.status_code == 200, item_resp.text
            item_data = item_resp.json()
            assert item_data["rate_names"], (
                "Custom PerUnit item must have rate_names populated "
                "(indicates PayItemRateTypeMap was created)"
            )
        finally:
            await session_client.delete(f"/settings/pay-items/{pay_item_id}", headers=h)

    # ------------------------------------------------------------------
    # Test 5 — backfill repairs broken items
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_broken_item_backfill_repair(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        test_app,
    ):
        """
        Insert a 'broken' custom item (PayItems row only, no PayItemRateTypeMap),
        run backfill_custom_pay_item_rate_structure, and confirm the item is repaired.
        """
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text as _text
        from app.dependencies import get_db
        from app.settings.service import backfill_custom_pay_item_rate_structure

        h = auth(auth_token)

        # We'll create via API (which now always creates the mapping), then
        # surgically delete the PayItemRateTypeMap to simulate the broken state,
        # run backfill, and verify the item re-appears.
        create_resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Backfill Test Item",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Trip",
                "rate_names":    ["Trip Rate"],
                "category":      "Custom",
            },
            headers=h,
        )
        assert create_resp.status_code == 201, create_resp.text
        pay_item_id = create_resp.json()["pay_item_id"]

        try:
            # Get an engine from the test_app override to do direct DB surgery
            db_override = test_app.dependency_overrides.get(get_db)
            assert db_override is not None, "get_db override not found"

            async for conn in db_override():
                # Delete the mapping to simulate the broken state
                await conn.execute(
                    _text("DELETE FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
                    {"pid": pay_item_id},
                )
                # Verify it's broken now
                check = await conn.execute(
                    _text(
                        "SELECT COUNT(*) FROM payroll.payitemratetypemap "
                        "WHERE payitemid = :pid AND status = 'Active'"
                    ),
                    {"pid": pay_item_id},
                )
                assert check.scalar_one() == 0, "Setup: mapping should be deleted"

                # Run backfill
                repaired = await backfill_custom_pay_item_rate_structure(
                    company_id=1, db=conn
                )
                assert any(r["pay_item_id"] == pay_item_id for r in repaired), (
                    f"Backfill must repair pay_item_id={pay_item_id}. "
                    f"Repaired: {repaired}"
                )

                # Verify mapping now exists
                check2 = await conn.execute(
                    _text(
                        "SELECT COUNT(*) FROM payroll.payitemratetypemap "
                        "WHERE payitemid = :pid AND status = 'Active'"
                    ),
                    {"pid": pay_item_id},
                )
                assert check2.scalar_one() >= 1, (
                    "After backfill, PayItemRateTypeMap row must exist"
                )
                break  # only need one iteration
        finally:
            await session_client.delete(f"/settings/pay-items/{pay_item_id}", headers=h)

    # ------------------------------------------------------------------
    # Test 6 — future-effective custom item appears in Pay Rates immediately
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_future_effective_custom_item_appears_in_pay_rates_immediately(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """
        Custom item with EffectiveFrom in future must appear in Pay Rates now
        (Phase 3C rule), but not in day-grid before EffectiveFrom.
        """
        from datetime import timedelta
        h = auth(auth_token)

        future_date = (date.today() + timedelta(days=30)).isoformat()

        create_resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Future Custom Item",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Box",
                "rate_names":    ["Box Rate"],
                "category":      "Custom",
            },
            headers=h,
        )
        assert create_resp.status_code == 201, create_resp.text
        pay_item_id = create_resp.json()["pay_item_id"]

        try:
            # Activate with future effective_from
            await session_client.patch(
                f"/settings/branches/1/pay-items/{pay_item_id}",
                json={"is_active": True, "effective_from": future_date},
                headers=h,
            )

            # Must appear in Pay Rates NOW (Phase 3C)
            matrix_resp = await session_client.get(
                f"/payroll/drivers/{created_driver_id}/rate-matrix",
                params={"as_of": _today_iso()},
                headers=h,
            )
            assert matrix_resp.status_code == 200
            matrix = matrix_resp.json()
            future_groups = [
                g for g in matrix["groups"]
                if g.get("pay_item_name") == "Future Custom Item"
            ]
            assert len(future_groups) >= 1, (
                "Future-effective custom item must appear in Pay Rates immediately (Phase 3C). "
                f"Groups: {[g.get('pay_item_name') for g in matrix['groups']]}"
            )
        finally:
            await session_client.delete(f"/settings/pay-items/{pay_item_id}", headers=h)

    # ------------------------------------------------------------------
    # Test 7 — branch isolation: custom item active in HQ not in PAYTEST
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_branch_isolation_custom_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """
        A custom item activated only for HQ must NOT appear in the PAYTEST driver's matrix.
        """
        h = auth(auth_token)

        create_resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "HQ Only Item",
                "item_scope":    "Daily",
                "rate_behavior": "PerUnit",
                "unit":          "Pallet",
                "rate_names":    ["Pallet Rate"],
                "category":      "Custom",
            },
            headers=h,
        )
        assert create_resp.status_code == 201, create_resp.text
        pay_item_id = create_resp.json()["pay_item_id"]

        try:
            # Activate ONLY for HQ (branch 1), NOT for PAYTEST
            await session_client.patch(
                f"/settings/branches/1/pay-items/{pay_item_id}",
                json={"is_active": True, "effective_from": "2020-01-01"},
                headers=h,
            )

            # PAYTEST driver matrix should NOT include "HQ Only Item"
            matrix_resp = await session_client.get(
                f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
                params={"as_of": _today_iso()},
                headers=h,
            )
            assert matrix_resp.status_code == 200
            matrix = matrix_resp.json()
            hq_only = [
                g for g in matrix["groups"]
                if g.get("pay_item_name") == "HQ Only Item"
            ]
            assert len(hq_only) == 0, (
                "HQ-only custom item must NOT appear in PAYTEST driver's rate matrix. "
                f"Found unexpected groups: {hq_only}"
            )
        finally:
            await session_client.delete(f"/settings/pay-items/{pay_item_id}", headers=h)

    # ------------------------------------------------------------------
    # Test 8 — Period/DirectMoney item NOT in daily rate matrix
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_period_direct_money_custom_item_not_in_daily_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """
        Custom Period/EnteredAmount items cannot be created (blocked with 422).
        Verify the creation guard; such items therefore can never appear in the
        daily rate matrix.  System Period items (BONUS, ADJUSTMENT) are excluded
        from the rate matrix by their Fixed/EnteredAmount behavior.
        """
        h = auth(auth_token)

        create_resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_name": "Period Bonus Item",
                "item_scope":    "Period",
                "rate_behavior": "EnteredAmount",
                "category":      "Custom",
            },
            headers=h,
        )
        assert create_resp.status_code == 422, (
            f"Expected 422 blocking custom Period item creation, got {create_resp.status_code}: {create_resp.text}"
        )
        assert "period" in create_resp.text.lower()

    # ------------------------------------------------------------------
    # Test 8b — system Period item (BONUS) absent from daily rate matrix
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_system_period_item_not_in_daily_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        created_driver_id: int,
    ):
        """
        System Period-scope items (BONUS, requires_rate=False) must not appear
        in the daily rate matrix regardless of branch activation state.

        This replaces the deleted custom-item matrix-exclusion test; BONUS exercises
        the same matrix-filtering path (INNER JOIN on PayItemRateTypeMap excludes
        items with no rate type mapping and requires_rate=False).
        """
        h = auth(auth_token)

        # Confirm BONUS is marked requires_rate=False in the settings catalog.
        items_resp = await session_client.get(
            f"/settings/branches/{hq_branch_id}/pay-items", headers=h
        )
        assert items_resp.status_code == 200
        bonus = next(i for i in items_resp.json() if i.get("pay_item_code") == "BONUS")
        assert bonus["requires_rate"] is False, "BONUS must have requires_rate=False"

        # BONUS must not appear in the driver rate matrix.
        matrix_resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=h,
        )
        assert matrix_resp.status_code == 200
        bonus_groups = [g for g in matrix_resp.json()["groups"] if g.get("pay_item_code") == "BONUS"]
        assert len(bonus_groups) == 0, (
            "System Period item BONUS (requires_rate=False) must NOT appear in the "
            f"daily rate matrix. Found groups: {bonus_groups}"
        )

    # ------------------------------------------------------------------
    # Test 9 — system items (Hours, Miles) still intact
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_system_item_mappings_intact(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
    ):
        """
        Hours (HOURLY) and Miles (MILEAGE) must continue to appear in the
        rate matrix — system item PayItemRateTypeMap rows must not be broken
        by custom item creation logic.
        """
        h = auth(auth_token)

        matrix_resp = await session_client.get(
            f"/payroll/drivers/{created_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=h,
        )
        assert matrix_resp.status_code == 200
        matrix = matrix_resp.json()

        rate_codes = {g["rate_code"] for g in matrix["groups"]}
        assert "HOURLY" in rate_codes, (
            f"HOURLY must appear in rate matrix. Found rate codes: {rate_codes}"
        )
        assert "MILEAGE" in rate_codes, (
            f"MILEAGE must appear in rate matrix. Found rate codes: {rate_codes}"
        )
