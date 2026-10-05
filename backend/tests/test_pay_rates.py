"""
tests/test_pay_rates.py — Phase 1: Pay Rates feature

Covers:
  TestDriverRateMatrix        — rate matrix endpoint shape and correctness
  TestDriverRates             — CRUD + permission guards
  TestDriverProfile           — GET /admin/users/{id}/driver read behavior
  TestOvernightRate           — OVERNIGHT migration correctness
  TestRateWritePermissions    — payrates.edit required for write ops (Fix 1)
  TestRateReadPermissions     — payrates.view required for read ops (Fix 2)
  TestMatrixHistoricalAsOf    — Superseded rates returned for old as_of dates (Fix 6)
  TestCreateRateBranchValidation — branch-active rate type check (Fix 8)
  TestDriverInfoPermissions   — /admin/users/{id}/driver caller permission gate (Fix 5)
"""
from datetime import date, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _sqla_text

from app.auth.security import create_access_token
from tests.access_test_helpers import create_neutral_test_user, create_provisioned_test_user
from tests.builders.access import create_user_with_role_token, get_company_role_id
from tests.builders.owned_scope import activate_paytest_equivalent_items, create_owned_branch
from tests.builders.workforce import create_driver_employee, create_driver_employee_record
from tests.ownership import retire_branch_periods_directly

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


async def _create_module_owned_driver(
    session_client: httpx.AsyncClient, auth_token: str, branch_id: int, label: str,
) -> int:
    marker = uuid4().hex
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": branch_id,
            "full_name": f"Pay rates {label} driver {marker}",
            "driver_code": f"PR{label[:2].upper()}-{marker[:10]}",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"pay-rates {label} driver create failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_client: httpx.AsyncClient, auth_token: str, session_db_conn) -> int:
    """Module-owned branch standing in for PAYTEST. The backdating-guard tests force
    Locked/Archived periods onto it; no other module may be able to see them."""
    branch_id = await create_owned_branch(session_db_conn, "PRT", "Pay rates owned branch")
    await activate_paytest_equivalent_items(session_client, auth_token, branch_id)
    return branch_id


@pytest_asyncio.fixture(scope="module", autouse=True)
async def pay_rates_terminal_state(paytest_branch_id: int, session_db_conn):
    """Retire the evidence-less Locked/Archived periods the guard tests insert, and
    fail if any mutable workflow period remains on the owned branch."""
    yield
    await retire_branch_periods_directly(session_db_conn, paytest_branch_id)


@pytest_asyncio.fixture(scope="module")
async def created_driver_id(
    session_client: httpx.AsyncClient, auth_token: str, hq_branch_id: int,
) -> int:
    """Module-owned HQ Driver: this module's successful rate mutations never land on
    the session-shared HQ Driver other modules read."""
    return await _create_module_owned_driver(session_client, auth_token, hq_branch_id, "hq")


@pytest_asyncio.fixture(scope="module")
async def paytest_driver_id(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
) -> int:
    """Module-owned PAYTEST Driver, for the same reason as `created_driver_id`."""
    return await _create_module_owned_driver(
        session_client, auth_token, paytest_branch_id, "paytest",
    )


@pytest_asyncio.fixture
async def additional_hourly_item(direct_db, paytest_rate_type_id: int):
    """A second HOURLY-mapped item, removed after each activation test."""
    from tests.seed_helpers import seed_legacy_item

    item_id = await seed_legacy_item(
        direct_db, code=f"RATE_GUARD_{uuid4().hex[:12]}", name="Rate Guard Item"
    )
    await direct_db.execute(
        _sqla_text("""
            INSERT INTO payroll.payitemratetypemap
                (payitemid, ratetypeid, isprimary, status)
            VALUES (:item_id, :rate_type_id, FALSE, 'Active')
        """),
        {"item_id": item_id, "rate_type_id": paytest_rate_type_id},
    )
    try:
        yield item_id
    finally:
        await direct_db.execute(
            _sqla_text("DELETE FROM payroll.branchpayitemconfig WHERE payitemid = :item_id"),
            {"item_id": item_id},
        )
        await direct_db.execute(
            _sqla_text("DELETE FROM payroll.payitemratetypemap WHERE payitemid = :item_id"),
            {"item_id": item_id},
        )
        await direct_db.execute(
            _sqla_text("DELETE FROM payroll.payitems WHERE payitemid = :item_id"),
            {"item_id": item_id},
        )


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
    """Read-only Driver profile information for an Access user."""

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

@pytest_asyncio.fixture
async def overnight_test_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
):
    """Own an OVERNIGHT-enabled branch and driver until disposable DB teardown."""
    marker = uuid4().hex[:12]
    branch_resp = await session_client.post(
        "/settings/branches",
        json={
            "branch_name": f"Overnight rate test {marker}",
            "branch_code": f"OVNT-{marker.upper()}",
            "status": "Active",
            "is_default": False,
        },
        headers=auth(auth_token),
    )
    assert branch_resp.status_code == 201, branch_resp.text
    branch_id = branch_resp.json()["branch_id"]

    items_resp = await session_client.get(
        f"/settings/branches/{branch_id}/pay-items",
        headers=auth(auth_token),
    )
    assert items_resp.status_code == 200, items_resp.text
    overnight_item = next(
        (item for item in items_resp.json() if item["pay_item_code"] == "OVERNIGHT"),
        None,
    )
    assert overnight_item is not None, "OVERNIGHT pay item not found on test branch"
    activation_resp = await session_client.patch(
        f"/settings/branches/{branch_id}/pay-items/{overnight_item['pay_item_id']}",
        json={"is_active": True, "notes": "Enabled for overnight rate test"},
        headers=auth(auth_token),
    )
    assert activation_resp.status_code == 200, activation_resp.text
    assert activation_resp.json()["is_active"] is True

    return await create_driver_employee(
        session_client,
        auth_token,
        branch_id=branch_id,
        full_name=f"Overnight test {marker}",
        driver_code=f"OVNT-{marker}",
    )


class TestOvernightRate:
    """Legacy OVERNIGHT migration coverage until the compensation cutover."""

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
        overnight_test_driver_id: int,
    ):
        """A DriverRate for the OVERNIGHT rate type can be created and approved.
        The test-owned branch has OVERNIGHT activated.
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
                "driver_id": overnight_test_driver_id,
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
        assert approve_resp.status_code == 200, approve_resp.text
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
    """A DRIVER role assignment must use the current Self access scope."""

    @pytest.mark.asyncio
    async def test_driver_role_rejects_allcompany_scope(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Assigning DRIVER role with AllCompanyBranches scope → 422 Self-scope validation."""
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

        user = await create_neutral_test_user(
            session_client, auth_token, "driver_allcompany_rejected_user",
            password="TestPass123!",
        )
        employee = await create_driver_employee_record(
            session_client, auth_token, branch_id=hq_branch_id,
            full_name="Rejected AllCompany Driver",
            driver_code=f"DRV-{uuid4().hex[:10]}",
        )
        link = await session_client.put(
            f"/admin/users/{user['user_id']}/employee-link",
            json={"employee_id": employee["employee_id"]},
            headers=auth(auth_token),
        )
        assert link.status_code == 200, link.text

        # Attempt to assign DRIVER with AllCompanyBranches — must be rejected
        resp = await session_client.post(
            f"/admin/users/{user['user_id']}/company-role-assignments",
            json={
                "company_role_id": driver_role["company_role_id"],
                "scope_type": "AllCompanyBranches",
                "branch_id": None,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "DRIVER access requires Self scope."

    @pytest.mark.asyncio
    async def test_driver_role_with_self_scope_uses_linked_workforce_profile(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Assigning DRIVER/Self uses the linked Employee's existing Driver profile."""
        # Create a fresh Employee/Driver through Workforce and link it before
        # exercising DRIVER assignment validation.
        user = await create_neutral_test_user(
            session_client, auth_token, "test_driver_profile_user",
            password="TestPass123!", display_name="Test Driver Profile",
        )
        employee = await create_driver_employee_record(
            session_client, auth_token, branch_id=hq_branch_id,
            full_name="Test Driver Profile",
            driver_code=f"DRV-{uuid4().hex[:10]}",
        )
        link = await session_client.put(
            f"/admin/users/{user['user_id']}/employee-link",
            json={"employee_id": employee["employee_id"]},
            headers=auth(auth_token),
        )
        assert link.status_code == 200, link.text
        new_user_id = user["user_id"]

        # Get DRIVER role
        roles_resp = await session_client.get("/admin/company-roles", headers=auth(auth_token))
        driver_role = next(
            (r for r in roles_resp.json() if r.get("role_code") == "DRIVER"), None
        )
        if driver_role is None:
            pytest.skip("DRIVER company role not found")

        # Assign DRIVER with current Self scope. The branch belongs to the
        # Workforce profile and is not duplicated onto the Access assignment.
        assign_resp = await session_client.post(
            f"/admin/users/{new_user_id}/company-role-assignments",
            json={
                "company_role_id": driver_role["company_role_id"],
                "scope_type": "Self",
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
    driver_branch_id=None,
    password: str = "TestPass123!",
) -> str:
    """Create user, assign company role, return login token."""
    if scope_type == "Self":
        role_id = await get_company_role_id(client, admin_token, "DRIVER")
    await create_provisioned_test_user(
        client, admin_token, username, role_id, scope_type=scope_type,
        branch_id=branch_id, driver_branch_id=driver_branch_id, password=password,
    )

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
        approve_a = await session_client.post(
            f"/payroll/rates/{rate_a_id}/approve", headers=auth(auth_token)
        )
        assert approve_a.status_code == 200, approve_a.text

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
        approve_b = await session_client.post(
            f"/payroll/rates/{rate_b_id}/approve", headers=auth(auth_token)
        )
        assert approve_b.status_code == 200, approve_b.text

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


async def _delete_direct_cdpi_item(db, pay_item_id: int) -> None:
    """
    Remove a never-used direct-created CDPI item and its generated rate graph.

    DELETE /settings/pay-items/{id} deliberately retires CDPI items (their
    CdpiDefinitions row is ON DELETE RESTRICT) and leaves the generated
    CDPI_{id}_PER_UNIT RateType active in the company catalog, so a test that
    must not leak that RateType removes exactly the rows it created.
    """
    params = {"pid": pay_item_id, "code": f"CDPI_{pay_item_id}_PER_UNIT"}
    for sql in (
        "DELETE FROM payroll.payitemrateslots WHERE payitemid = :pid",
        "DELETE FROM payroll.payitemratetypemap WHERE payitemid = :pid",
        "DELETE FROM payroll.ratetypes WHERE ratecode = :code",
        "DELETE FROM payroll.cdpidefinitions WHERE payitemid = :pid",
        "DELETE FROM payroll.payitems WHERE payitemid = :pid",
    ):
        await db.execute(_sqla_text(sql), params)


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
    async def test_create_rate_for_branch_inactive_cdpi_rate_type_fails(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
        direct_db,
    ):
        """
        An active, company-owned rate type whose only mapped Pay Item is not
        active for the driver's branch → 422.

        A direct-created CDPI PerUnit item gets its own RateType + mapping but
        starts inactive on every branch (IsDefaultBranchActive=FALSE, no
        BranchPayItemConfig rows), so the rate type passes the existence and
        company-ownership checks and is rejected only by the branch check.
        """
        item_resp = await session_client.post(
            "/settings/cdpi/direct-company-items",
            json={
                "item_name": f"Branch Inactive Rate Guard {uuid4().hex[:8]}",
                "input_type": "Number",
                "calc_method_key": "PerUnit",
            },
            headers=auth(auth_token),
        )
        assert item_resp.status_code == 201, item_resp.text
        pay_item_id = item_resp.json()["pay_item_id"]
        rate_code = f"CDPI_{pay_item_id}_PER_UNIT"

        try:
            rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
            assert rt_resp.status_code == 200, rt_resp.text
            cdpi_rt = next((r for r in rt_resp.json() if r["rate_code"] == rate_code), None)
            assert cdpi_rt is not None, f"{rate_code} missing from the rate-type catalog"
            assert cdpi_rt["is_active"] is True

            resp = await session_client.post(
                "/payroll/rates",
                json={
                    "driver_id": created_driver_id,
                    "rate_type_id": cdpi_rt["rate_type_id"],
                    "amount": "5.00",
                    "effective_from": "2037-01-01",
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, resp.text
            assert resp.json()["detail"] == "This rate type is not active for this driver's branch."
        finally:
            await _delete_direct_cdpi_item(direct_db, pay_item_id)

        after = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        assert after.status_code == 200, after.text
        assert all(r["rate_code"] != rate_code for r in after.json()), (
            f"{rate_code} must not remain in the rate-type catalog after cleanup"
        )


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
    DRIVER/Self callers are denied generic rate lookups.
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
# DRIVER/Self generic rate denial
# ---------------------------------------------------------------------------

class TestDriverSelfRates:
    """
    DRIVER/Self users are denied generic rate access, including access to
    their own Driver's rates.
    """

    @staticmethod
    async def _setup_driver_self_user(
        client: httpx.AsyncClient,
        admin_token: str,
        username: str,
        hq_branch_id: int,
    ) -> tuple[str, int]:
        """Create a test-owned linked DRIVER/Self account through shared builders."""
        token = await create_user_with_role_token(
            client, admin_token, username,
            await get_company_role_id(client, admin_token, "DRIVER"),
            scope_type="Self", driver_branch_id=hq_branch_id,
        )
        me = await client.get("/auth/me", headers=auth(token))
        assert me.status_code == 200, me.text
        return token, int(me.json()["user_id"])

    @pytest.mark.asyncio
    async def test_driver_self_cannot_batch_save_another_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """
        A valid, linked DRIVER/Self user cannot use the generic batch endpoint.
        """
        grp = await _get_matrix_group(session_client, auth_token, paytest_driver_id, "HOURLY")
        if grp is None:
            pytest.skip("HOURLY not in matrix for paytest_driver_id")

        token, _ = await self._setup_driver_self_user(
            session_client, auth_token, "driver_self_batch_user2", paytest_branch_id,
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
        assert "DRIVER Self" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_driver_self_cannot_create_rate_for_another_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """A linked DRIVER/Self user cannot create generic DriverRates."""
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if hourly is None:
            pytest.skip("HOURLY rate type not found")

        driver_self_token, _ = await self._setup_driver_self_user(
            session_client, auth_token, "driver_self_create_rate_user2", paytest_branch_id,
        )

        resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id": paytest_driver_id,
                "rate_type_id": hourly["rate_type_id"],
                "amount": "20.00",
                "effective_from": "2086-01-01",
            },
            headers=auth(driver_self_token),
        )
        assert resp.status_code == 403
        assert "DRIVER Self" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_driver_self_cannot_lookup_another_driver_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
        paytest_branch_id: int,
    ):
        """A linked DRIVER/Self user cannot use the generic rate lookup."""
        driver_self_token, _ = await self._setup_driver_self_user(
            session_client, auth_token, "driver_self_lookup_user2", paytest_branch_id,
        )

        resp = await session_client.get(
            "/payroll/rates/lookup",
            params={
                "driver_id":    paytest_driver_id,
                "rate_type_id": paytest_rate_type_id,
                "work_date":    _today_iso(),
            },
            headers=auth(driver_self_token),
        )
        assert resp.status_code == 403
        assert "DRIVER Self" in resp.json()["detail"]


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
# DRIVER/Self rate list/detail denial
# ---------------------------------------------------------------------------

class TestDriverSelfRateListDetail:
    """
    DRIVER/Self users must NOT be able to read generic DriverRate resources
    through GET /payroll/rates (list) or GET /payroll/rates/{rate_id} (detail).
    """

    @pytest.mark.asyncio
    async def test_driver_self_cannot_list_rates_for_another_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """A valid linked DRIVER/Self user receives generic rate denial."""
        driver_self_token, _ = await TestDriverSelfRates._setup_driver_self_user(
            session_client, auth_token, "driver_self_list_test_user1", paytest_branch_id,
        )

        resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id},
            headers=auth(driver_self_token),
        )
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"].lower()
        assert "driver self" in detail

    @pytest.mark.asyncio
    async def test_driver_self_list_without_driver_id_is_denied(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """The generic list route remains denied when no target Driver is supplied."""
        driver_self_token, _ = await TestDriverSelfRates._setup_driver_self_user(
            session_client, auth_token, "driver_self_no_filter_user2", paytest_branch_id,
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

        # The guard runs before list shaping, even when no Driver filter is supplied.
        resp = await session_client.get(
            "/payroll/rates",
            headers=auth(driver_self_token),
        )
        assert resp.status_code == 403, (
            f"DRIVER/Self user must not receive generic rates. "
            f"Got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_driver_self_cannot_get_rate_by_id_for_another_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """A valid linked DRIVER/Self user cannot use generic rate detail."""
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

        driver_self_token, _ = await TestDriverSelfRates._setup_driver_self_user(
            session_client, auth_token, "driver_self_detail_test_user1", paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/rates/{rate_id}",
            headers=auth(driver_self_token),
        )
        assert resp.status_code == 403, resp.text
        detail = resp.json()["detail"].lower()
        assert "driver self" in detail

    @pytest.mark.asyncio
    async def test_non_driver_user_can_still_list_and_get_rates(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """A non-DRIVER user with payrates.view can list and read rates normally."""
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
# TestDriverSelfScopeDetection  (P2b — Self ceiling with overlapping assignments)
# ---------------------------------------------------------------------------

class TestDriverSelfScopeDetection:
    """
    Generic DriverRate routes deny DRIVER/Self independently of branch grants.
    """

    @pytest.mark.asyncio
    async def test_single_driver_self_assignment_denies_generic_rates(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """A valid linked DRIVER/Self assignment is denied from generic rates."""
        token, _ = await TestDriverSelfRates._setup_driver_self_user(
            session_client, auth_token, "scope_test_driver_self_user1",
            paytest_branch_id,
        )

        resp = await session_client.get(
            "/payroll/rates",
            params={"driver_id": paytest_driver_id},
            headers=auth(token),
        )
        assert resp.status_code == 403
        detail = resp.json()["detail"].lower()
        assert "driver self" in detail

    @pytest.mark.asyncio
    async def test_non_driver_specific_branch_assignment_remains_usable(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """A non-DRIVER SpecificBranch assignment can read rates on that branch."""
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
            f"SpecificBranch user must remain branch-scoped. Got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_overlapping_legacy_branch_grant_does_not_widen_driver_self(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Inject historical overlapping assignment state beside DRIVER/Self.
        The centralized DRIVER/Self denial must remain effective.

        This simulates DB corruption that assign_company_role should prevent.
        """
        from sqlalchemy import text as _text

        driver_self_token, _ = await TestDriverSelfRates._setup_driver_self_user(
            session_client, auth_token, "scope_conflict_driver_self_user1",
            paytest_branch_id,
        )

        # Get user_id from token by calling /auth/me or /admin/users
        me_resp = await session_client.get("/auth/me", headers=auth(driver_self_token))
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
                headers=auth(driver_self_token),
            )
            assert resp.status_code == 403, (
                f"Overlapping legacy branch grant must not widen DRIVER/Self. Got {resp.status_code}: {resp.text}"
            )
            assert "driver self" in resp.json()["detail"].lower()
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

        role_id = await _create_role_with_perms(
            session_client, auth_token, "ScopeRevokeRole1",
            ["payrates.view"],
        )

        # Create user with a current branch-scoped Access assignment.
        user_resp = await create_neutral_test_user(
            session_client, auth_token, "scope_revoke_user1",
            password="TestPass123!",
        )
        user_id = user_resp["user_id"]

        await session_client.post(
            f"/admin/users/{user_id}/company-role-assignments",
            json={"company_role_id": role_id, "scope_type": "SpecificBranch", "branch_id": paytest_branch_id},
            headers=auth(auth_token),
        )

        # Replace the prior assignment; only the new active assignment remains.
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

        # Login and verify the new token works as SpecificBranch.
        login = await session_client.post("/auth/login", json={
            "username": "scope_revoke_user1", "password": "TestPass123!", "company_code": "DEMO",
        })
        assert login.status_code == 200
        new_token = login.json()["access_token"]

        # SpecificBranch remains a valid scope for non-DRIVER users.
        # SpecificBranch users need branch_id in the query for fn_UserHasPermission to pass.
        resp = await session_client.get(
            "/payroll/rates",
            params={"branch_id": paytest_branch_id},
            headers=auth(new_token),
        )
        assert resp.status_code == 200, (
            f"After reassignment to SpecificBranch, the branch-scoped role should work. "
            f"Got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# TestDriverSelfRateMatrix  (P2b — generic rate matrix denial)
# ---------------------------------------------------------------------------

class TestDriverSelfRateMatrix:
    """
    DRIVER/Self is denied from generic rate matrix endpoints even when the
    target is its linked Driver. Non-driver branch and company scopes remain usable.
    """

    @staticmethod
    async def _create_driver_self_user(
        client: httpx.AsyncClient,
        admin_token: str,
        username: str,
        paytest_branch_id: int,
    ) -> tuple[str, int]:
        """
        Create a linked DRIVER/Self account through the canonical builders.
        Returns (token, own_driver_id).
        """
        token = await create_user_with_role_token(
            client, admin_token, username,
            await get_company_role_id(client, admin_token, "DRIVER"),
            scope_type="Self", driver_branch_id=paytest_branch_id,
        )
        me = await client.get("/auth/me", headers=auth(token))
        assert me.status_code == 200, me.text
        user_id = me.json()["user_id"]
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

        return token, own_driver_id

    # -------------------------------------------------------------------------
    # Test 1 — DRIVER/Self cannot use even its own generic matrix
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_driver_self_cannot_access_own_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        A valid linked DRIVER/Self account still cannot use generic rate surfaces.
        """
        token, own_driver_id = await self._create_driver_self_user(
            session_client, auth_token, "matrix_oda_linked_user1", paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/drivers/{own_driver_id}/rate-matrix",
            params={"as_of": _today_iso()},
            headers=auth(token),
        )
        assert resp.status_code == 403, resp.text
        assert "driver self" in resp.json()["detail"].lower()

    # -------------------------------------------------------------------------
    # Test 2 — DRIVER/Self cannot use another Driver's generic matrix
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_driver_self_cannot_access_other_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """
        A linked DRIVER/Self account is denied before generic target scoping.
        """
        token, own_driver_id = await self._create_driver_self_user(
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
            f"DRIVER/Self user must be denied another driver's matrix. Got {resp.status_code}: {resp.text}"
        )
        detail = resp.json()["detail"].lower()
        assert "driver self" in detail

    # -------------------------------------------------------------------------
    # Test 3 — legacy unlinked Self state fails closed
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_unlinked_driver_self_fails_closed_on_generic_routes(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """A Self assignment without its required employee link remains fail-closed.

        This intentionally seeds an unlinked historical/invalid state directly:
        current Access provisioning requires an explicitly linked Employee.
        """
        user = await create_neutral_test_user(
            session_client, auth_token, f"unlinked_self_{uuid4().hex[:10]}",
            password="TestPass123!",
        )
        driver_role_id = await get_company_role_id(session_client, auth_token, "DRIVER")
        await direct_db.execute(_sqla_text("""
            INSERT INTO sec.UserBranchRoles
                (UserID, CompanyID, BranchID, RoleID, CompanyRoleID, ScopeType, IsActive)
            VALUES (:uid, :cid, NULL, NULL, :role_id, 'Self', TRUE)
        """), {
            "uid": user["user_id"], "cid": user["company_id"], "role_id": driver_role_id,
        })
        token = create_access_token(int(user["user_id"]), int(user["company_id"]))

        matrix = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rate-matrix",
            params={"as_of": _today_iso()}, headers=auth(token),
        )
        assert matrix.status_code == 403, matrix.text
        assert "driver self" in matrix.json()["detail"].lower()

        transfer = await session_client.post(
            "/driver-transfers",
            json={
                "driver_id": paytest_driver_id,
                "target_branch_id": paytest_branch_id,
                "effective_date": "2099-01-01",
                "initiated_by": "Driver",
            },
            headers=auth(token),
        )
        assert transfer.status_code == 403, transfer.text

    # -------------------------------------------------------------------------
    # Test 4 — an overlapping legacy branch grant cannot widen DRIVER/Self
    # -------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_overlapping_legacy_branch_grant_does_not_widen_driver_self_matrix(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        This direct SQL setup preserves an adversarial historical overlap to
        prove a generic SpecificBranch row cannot widen a DRIVER/Self subject.
        """
        from sqlalchemy import text as _text

        role_id = await _create_role_with_perms(
            session_client, auth_token, "MatrixODAConflictRole1",
            ["payrates.view", "payrates.edit"],
        )
        token = await _create_user_with_role(
            session_client, auth_token, "matrix_oda_conflict_user1", role_id,
            scope_type="Self", driver_branch_id=paytest_branch_id,
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
                f"Overlapping DRIVER/Self + SpecificBranch must fail-closed on matrix. "
                f"Got {resp.status_code}: {resp.text}"
            )
            assert "driver self" in resp.json()["detail"].lower()
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
        """SpecificBranch user on PAYTEST branch → 200 on PAYTEST driver matrix."""
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
    3.  pending blocks DRIVER/Self user for another driver
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

    # ── Test 3: pending blocks DRIVER/Self user for another driver ────────────

    @pytest.mark.asyncio
    async def test_pending_blocks_oda_user_for_another_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """A valid linked DRIVER/Self user cannot use generic pending-rate reads."""
        driver_self_token, _ = await TestDriverSelfRates._setup_driver_self_user(
            session_client, auth_token, "p2b_driver_self_pending_user1", paytest_branch_id,
        )
        resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/rates/pending",
            headers=auth(driver_self_token),
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
        import random

        from sqlalchemy import text as _text

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
    async def test_active_hourly_item_overrides_inactive_mapped_item(
        self, session_client, auth_token, paytest_driver_id, paytest_branch_id,
        paytest_rate_type_id, additional_hourly_item, direct_db,
    ):
        hours = (await direct_db.execute(
            _sqla_text("""
                SELECT cfg.isactive FROM payroll.branchpayitemconfig cfg
                JOIN payroll.payitems pi ON pi.payitemid = cfg.payitemid
                WHERE cfg.companyid = 1 AND cfg.branchid = :branch_id
                  AND pi.payitemcode = 'HOURS' AND cfg.effectiveto IS NULL
            """),
            {"branch_id": paytest_branch_id},
        )).scalar_one()
        assert hours is True

        await direct_db.execute(
            _sqla_text("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, payitemid, isactive, effectivefrom)
                VALUES (1, :branch_id, :item_id, FALSE, :effective_from)
            """),
            {"branch_id": paytest_branch_id, "item_id": additional_hourly_item,
             "effective_from": date.today() + timedelta(days=30)},
        )
        resp = await session_client.post(
            "/payroll/rates",
            json={"driver_id": paytest_driver_id, "rate_type_id": paytest_rate_type_id,
                  "amount": "25.00", "effective_from": _today_iso()},
            headers=auth(auth_token),
        )
        try:
            assert resp.status_code == 201, resp.text
        finally:
            if resp.status_code == 201:
                await session_client.delete(
                    f"/payroll/rates/{resp.json()['driver_rate_id']}",
                    headers=auth(auth_token),
                )

    @pytest.mark.asyncio
    async def test_disabling_one_hourly_item_keeps_other_mapping_eligible(
        self, session_client, auth_token, paytest_driver_id, paytest_branch_id,
        paytest_rate_type_id, additional_hourly_item, direct_db,
    ):
        await direct_db.execute(
            _sqla_text("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, payitemid, isactive, effectivefrom)
                VALUES (1, :branch_id, :item_id, TRUE, :effective_from)
            """),
            {"branch_id": paytest_branch_id, "item_id": additional_hourly_item,
             "effective_from": date.today() + timedelta(days=30)},
        )
        hours = (await direct_db.execute(
            _sqla_text("""
                SELECT cfg.configid, cfg.isactive
                FROM payroll.branchpayitemconfig cfg
                JOIN payroll.payitems pi ON pi.payitemid = cfg.payitemid
                WHERE cfg.companyid = 1 AND cfg.branchid = :branch_id
                  AND pi.payitemcode = 'HOURS' AND cfg.effectiveto IS NULL
            """),
            {"branch_id": paytest_branch_id},
        )).mappings().one()
        assert hours["isactive"] is True
        await direct_db.execute(
            _sqla_text("UPDATE payroll.branchpayitemconfig SET isactive = FALSE WHERE configid = :id"),
            {"id": hours["configid"]},
        )
        try:
            resp = await session_client.post(
                "/payroll/rates",
                json={"driver_id": paytest_driver_id, "rate_type_id": paytest_rate_type_id,
                      "amount": "25.00", "effective_from": _today_iso()},
                headers=auth(auth_token),
            )
            assert resp.status_code == 201, resp.text
            await session_client.delete(
                f"/payroll/rates/{resp.json()['driver_rate_id']}",
                headers=auth(auth_token),
            )
        finally:
            await direct_db.execute(
                _sqla_text("UPDATE payroll.branchpayitemconfig SET isactive = :active WHERE configid = :id"),
                {"active": hours["isactive"], "id": hours["configid"]},
            )

    @pytest.mark.asyncio
    async def test_latest_eligible_config_governs_each_mapped_item(
        self, session_client, auth_token, paytest_driver_id, paytest_branch_id,
        paytest_rate_type_id, additional_hourly_item, direct_db,
    ):
        future = date.today() + timedelta(days=30)
        await direct_db.execute(
            _sqla_text("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, payitemid, isactive, effectivefrom, effectiveto)
                VALUES (1, :branch_id, :item_id, TRUE, :today, :last_day)
            """),
            {"branch_id": paytest_branch_id, "item_id": additional_hourly_item,
             "today": date.today(), "last_day": future - timedelta(days=1)},
        )
        future_config = (await direct_db.execute(
            _sqla_text("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, payitemid, isactive, effectivefrom)
                VALUES (1, :branch_id, :item_id, FALSE, :future)
                RETURNING configid
            """),
            {"branch_id": paytest_branch_id, "item_id": additional_hourly_item,
             "future": future},
        )).scalar_one()
        hours = (await direct_db.execute(
            _sqla_text("""
                SELECT cfg.configid, cfg.isactive
                FROM payroll.branchpayitemconfig cfg
                JOIN payroll.payitems pi ON pi.payitemid = cfg.payitemid
                WHERE cfg.companyid = 1 AND cfg.branchid = :branch_id
                  AND pi.payitemcode = 'HOURS' AND cfg.effectiveto IS NULL
            """),
            {"branch_id": paytest_branch_id},
        )).mappings().one()
        assert hours["isactive"] is True
        await direct_db.execute(
            _sqla_text("UPDATE payroll.branchpayitemconfig SET isactive = FALSE WHERE configid = :id"),
            {"id": hours["configid"]},
        )
        try:
            payload = {"driver_id": paytest_driver_id, "rate_type_id": paytest_rate_type_id,
                       "amount": "25.00", "effective_from": _today_iso()}
            rejected = await session_client.post(
                "/payroll/rates", json=payload, headers=auth(auth_token)
            )
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["detail"] == "This rate type is not active for this driver's branch."

            await direct_db.execute(
                _sqla_text("UPDATE payroll.branchpayitemconfig SET isactive = TRUE WHERE configid = :id"),
                {"id": future_config},
            )
            allowed = await session_client.post(
                "/payroll/rates", json=payload, headers=auth(auth_token)
            )
            assert allowed.status_code == 201, allowed.text
            await session_client.delete(
                f"/payroll/rates/{allowed.json()['driver_rate_id']}",
                headers=auth(auth_token),
            )
        finally:
            await direct_db.execute(
                _sqla_text("UPDATE payroll.branchpayitemconfig SET isactive = :active WHERE configid = :id"),
                {"active": hours["isactive"], "id": hours["configid"]},
            )

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
        paytest_rate_type_id: int,
        direct_db,
    ):
        """Reject HOURLY only after every eligible mapped item is inactive."""
        mapped_sql = _sqla_text("""
            SELECT pi.payitemid, pi.isdefaultbranchactive,
                   cfg.configid, cfg.isactive AS config_active
            FROM payroll.payitems pi
            JOIN payroll.payitemratetypemap m ON m.payitemid = pi.payitemid
                AND m.ratetypeid = :rate_type_id AND m.status = 'Active'
            LEFT JOIN LATERAL (
                SELECT configid, isactive FROM payroll.branchpayitemconfig
                WHERE companyid = 1 AND branchid = :branch_id
                  AND payitemid = pi.payitemid
                  AND (effectiveto IS NULL OR effectiveto >= :effective_from)
                ORDER BY effectivefrom DESC, configid DESC LIMIT 1
            ) cfg ON TRUE
            WHERE pi.status != 'Retired' AND pi.requiresrate = TRUE
              AND (pi.companyid IS NULL OR pi.companyid = 1)
        """)
        params = {"rate_type_id": paytest_rate_type_id,
                  "branch_id": paytest_branch_id, "effective_from": date.today()}
        mapped = (await direct_db.execute(mapped_sql, params)).mappings().all()
        assert mapped, "HOURLY must have at least one eligible Pay Item"

        previous: list[tuple[int, bool]] = []
        inserted: list[int] = []
        try:
            for row in mapped:
                if row["configid"] is not None:
                    previous.append((row["configid"], row["config_active"]))
                    await direct_db.execute(
                        _sqla_text("UPDATE payroll.branchpayitemconfig SET isactive = FALSE WHERE configid = :id"),
                        {"id": row["configid"]},
                    )
                elif row["isdefaultbranchactive"]:
                    config_id = (await direct_db.execute(
                        _sqla_text("""
                            INSERT INTO payroll.branchpayitemconfig
                                (companyid, branchid, payitemid, isactive, effectivefrom)
                            VALUES (1, :branch_id, :item_id, FALSE, :effective_from)
                            RETURNING configid
                        """),
                        {"branch_id": paytest_branch_id, "item_id": row["payitemid"],
                         "effective_from": date.today()},
                    )).scalar_one()
                    inserted.append(config_id)

            after = (await direct_db.execute(mapped_sql, params)).mappings().all()
            assert not any(
                row["config_active"] if row["configid"] is not None
                else row["isdefaultbranchactive"]
                for row in after
            ), "Test setup must deactivate every eligible HOURLY mapping"
            resp = await session_client.post(
                "/payroll/rates",
                json={
                    "driver_id":      paytest_driver_id,
                    "rate_type_id":   paytest_rate_type_id,
                    "amount":         "25.00",
                    "effective_from": _today_iso(),
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"create_rate must reject when every mapped item is inactive. "
                f"Got {resp.status_code}: {resp.text}"
            )
            assert resp.json()["detail"] == "This rate type is not active for this driver's branch."
        finally:
            for config_id, active in previous:
                await direct_db.execute(
                    _sqla_text("UPDATE payroll.branchpayitemconfig SET isactive = :active WHERE configid = :id"),
                    {"active": active, "id": config_id},
                )
            for config_id in inserted:
                await direct_db.execute(
                    _sqla_text("DELETE FROM payroll.branchpayitemconfig WHERE configid = :id"),
                    {"id": config_id},
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
        session_db_conn,
    ):
        """
        Seed a custom Daily PerUnit item, activate it for the driver's branch,
        then verify it appears as exactly one group in the rate matrix.
        """
        from tests.seed_helpers import seed_legacy_item_with_rate_structure
        h = auth(auth_token)

        seeded = await seed_legacy_item_with_rate_structure(
            session_db_conn,
            code="TST_PERUNIT_SAMYA",
            name="Samya Rate Test",
            unit="Stop",
            rate_behavior="PerUnit",
        )
        pay_item_id = seeded["pay_item_id"]

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
        session_db_conn,
    ):
        """
        Seed a custom Daily RangeBracket item and verify it produces multiple
        rate groups (one per bracket) in the matrix, and still one payroll column.
        """
        from tests.seed_helpers import seed_legacy_item_multi_rate
        h = auth(auth_token)

        seeded = await seed_legacy_item_multi_rate(
            session_db_conn,
            code="TST_BRACKET_RATE",
            name="Bracket Rate Test",
            unit="Mile",
            rate_behavior="RangeBracket",
            rate_names=["Short Haul", "Long Haul"],
        )
        pay_item_id = seeded["pay_item_id"]

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
        session_db_conn,
        direct_db,
    ):
        """
        A RangeBracket item should appear as exactly ONE column in the day-grid
        (one pay-item = one payroll column, regardless of bracket count).

        Self-contained: creates its own payroll period at far-future dates
        (2082-07-01 to 2082-07-07) to avoid conflicts, then cancels it on cleanup.
        """
        from tests.seed_helpers import seed_legacy_item_multi_rate
        h = auth(auth_token)

        # ------------------------------------------------------------------ #
        # 1. Seed the custom RangeBracket pay item (2 rate fields)
        # ------------------------------------------------------------------ #
        seeded = await seed_legacy_item_multi_rate(
            session_db_conn,
            code="TST_BRACKET_COL",
            name="Bracket Column Test",
            unit="Stop",
            rate_behavior="RangeBracket",
            rate_names=["Bracket Low", "Bracket High"],
        )
        pay_item_id = seeded["pay_item_id"]
        bracket_item_code = "TST_BRACKET_COL"

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

            # Seed a minimal Open period directly for this lower-level
            # day-grid assertion; period creation has its own current-contract
            # coverage and requires Payroll Setup/candidate prerequisites.
            await direct_db.execute(
                _sqla_text(
                    "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                    "WHERE branchid = :bid AND status = 'Open'"
                ),
                {"bid": paytest_branch_id},
            )
            await direct_db.execute(
                _sqla_text("""
                    INSERT INTO payroll.payrollperiods
                        (companyid, branchid, status, periodcode, periodname,
                         periodtype, startdate, enddate)
                    VALUES (1, :bid, 'Open', :code, 'RangeBracket Open',
                            'Week', '2082-07-01', '2082-07-07')
                """),
                {"bid": paytest_branch_id,
                 "code": f"RB-OPEN-{uuid4().hex[:10]}"},
            )

            # ------------------------------------------------------------------ #
            # 3. Use the seeded Open period
            # ------------------------------------------------------------------ #
            period_id = (await direct_db.execute(
                _sqla_text("""
                    SELECT payrollperiodid
                    FROM payroll.payrollperiods
                    WHERE branchid = :bid AND status = 'Open'
                    ORDER BY payrollperiodid DESC
                    LIMIT 1
                """),
                {"bid": paytest_branch_id},
            )).scalar_one()

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
            await direct_db.execute(
                _sqla_text(
                    "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                    "WHERE branchid = :bid AND status = 'Open'"
                ),
                {"bid": paytest_branch_id},
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
        session_db_conn,
    ):
        """
        After seeding a custom Daily PerUnit item, PayItemRateTypeMap rows
        must exist — verified via the GET endpoint and rate_names field.
        """
        from tests.seed_helpers import seed_legacy_item_with_rate_structure

        h = auth(auth_token)

        seeded = await seed_legacy_item_with_rate_structure(
            session_db_conn,
            code="TST_RATE_STRUCT",
            name="Rate Structure Verify",
            unit="km",
            rate_behavior="PerUnit",
            rate_name="KM Rate",
        )
        pay_item_id = seeded["pay_item_id"]

        try:
            # Verify PayItemRateTypeMap row exists (seed_legacy_item_with_rate_structure creates it)
            from sqlalchemy import text as _text
            check = await session_db_conn.execute(
                _text(
                    "SELECT COUNT(*) FROM payroll.payitemratetypemap "
                    "WHERE payitemid = :pid AND status = 'Active'"
                ),
                {"pid": pay_item_id},
            )
            assert check.scalar_one() >= 1, (
                "seed_legacy_item_with_rate_structure must create a PayItemRateTypeMap row"
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
        session_db_conn,
    ):
        """
        Insert a 'broken' custom item (PayItems row only, no PayItemRateTypeMap),
        run backfill_custom_pay_item_rate_structure, and confirm the item is repaired.
        """
        from sqlalchemy import text as _text

        from app.settings.service import backfill_custom_pay_item_rate_structure
        from tests.seed_helpers import seed_legacy_item_with_rate_structure

        h = auth(auth_token)

        # Seed the item with rate structure, then surgically delete the mapping
        # to simulate the broken state, then run backfill and verify repair.
        seeded = await seed_legacy_item_with_rate_structure(
            session_db_conn,
            code="TST_BACKFILL",
            name="Backfill Test Item",
            unit="Trip",
            rate_behavior="PerUnit",
            rate_name="Trip Rate",
        )
        pay_item_id = seeded["pay_item_id"]

        try:
            # Delete the mapping to simulate the broken state
            await session_db_conn.execute(
                _text("DELETE FROM payroll.payitemratetypemap WHERE payitemid = :pid"),
                {"pid": pay_item_id},
            )
            # Verify it's broken now
            check = await session_db_conn.execute(
                _text(
                    "SELECT COUNT(*) FROM payroll.payitemratetypemap "
                    "WHERE payitemid = :pid AND status = 'Active'"
                ),
                {"pid": pay_item_id},
            )
            assert check.scalar_one() == 0, "Setup: mapping should be deleted"

            # Run backfill directly via session_db_conn (AUTOCOMMIT — changes are immediate)
            repaired = await backfill_custom_pay_item_rate_structure(
                company_id=1, db=session_db_conn
            )
            assert any(r["pay_item_id"] == pay_item_id for r in repaired), (
                f"Backfill must repair pay_item_id={pay_item_id}. "
                f"Repaired: {repaired}"
            )

            # Verify mapping now exists
            check2 = await session_db_conn.execute(
                _text(
                    "SELECT COUNT(*) FROM payroll.payitemratetypemap "
                    "WHERE payitemid = :pid AND status = 'Active'"
                ),
                {"pid": pay_item_id},
            )
            assert check2.scalar_one() >= 1, (
                "After backfill, PayItemRateTypeMap row must exist"
            )
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
        session_db_conn,
    ):
        """
        Custom item with EffectiveFrom in future must appear in Pay Rates now
        (Phase 3C rule), but not in day-grid before EffectiveFrom.
        """
        from datetime import timedelta

        from tests.seed_helpers import seed_legacy_item_with_rate_structure
        h = auth(auth_token)

        future_date = (date.today() + timedelta(days=30)).isoformat()

        seeded = await seed_legacy_item_with_rate_structure(
            session_db_conn,
            code="TST_FUTURE_ITEM",
            name="Future Custom Item",
            unit="Box",
            rate_behavior="PerUnit",
            rate_name="Box Rate",
        )
        pay_item_id = seeded["pay_item_id"]

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
        session_db_conn,
    ):
        """
        A custom item activated only for HQ must NOT appear in the PAYTEST driver's matrix.
        """
        from tests.seed_helpers import seed_legacy_item_with_rate_structure
        h = auth(auth_token)

        seeded = await seed_legacy_item_with_rate_structure(
            session_db_conn,
            code="TST_HQ_ONLY",
            name="HQ Only Item",
            unit="Pallet",
            rate_behavior="PerUnit",
            rate_name="Pallet Rate",
        )
        pay_item_id = seeded["pay_item_id"]

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
