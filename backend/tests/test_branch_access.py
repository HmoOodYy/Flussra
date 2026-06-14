"""
Integration tests for branch-scope enforcement and action-level permission checks.

Users tested
------------
admin       AllCompanyBranches scope, PAYROLL_ADMIN role (all write permissions)
branch_user SpecificBranch=HQ scope, PAYROLL_VIEWER role (no write permissions)

Branch scope tests
------------------
branch_user cannot access PAYTEST branch resources.
branch_user CAN access HQ branch resources (positive test).

Permission tests
----------------
branch_user HAS branch access to HQ but LACKS write permissions.
These verify that branch access alone is not sufficient — the action-level
permission code must also be granted.

Permission codes exercised
--------------------------
payroll.period.create — create payroll period (create_period, migration 0030)
payroll.entry         — enter payroll lines / open / submit periods
drivers.manage        — create driver (create_driver)
payroll.approve_rate  — approve / void a rate (approve_rate / void_rate)
"""
import pytest
import pytest_asyncio
import httpx


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Session-scoped fixture: a Draft period on the PAYTEST branch
# Used by branch-scope denial tests that need a PAYTEST resource to be denied.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def paytest_draft_period_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Create one Draft period on PAYTEST at session start."""
    resp = await session_client.post(
        "/payroll/periods",
        json={
            "branch_id":   paytest_branch_id,
            "period_type": "Week",
            "start_date":  "2044-01-06",
            "end_date":    "2044-01-12",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"PAYTEST period seed failed: {resp.text}"
    return resp.json()["payroll_period_id"]


# ---------------------------------------------------------------------------
# Branch scope denial tests
# ---------------------------------------------------------------------------

class TestBranchScopeDenial:
    """branch_user has SpecificBranch access to HQ only — PAYTEST is out of scope."""

    async def test_list_periods_with_paytest_filter_gives_403(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        paytest_branch_id: int,
    ):
        """Filtering GET /payroll/periods to PAYTEST branch returns 403."""
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": paytest_branch_id},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_get_paytest_period_gives_403(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        paytest_draft_period_id: int,
    ):
        """GET /payroll/periods/{id} for a PAYTEST period returns 403."""
        resp = await client.get(
            f"/payroll/periods/{paytest_draft_period_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_branch_user_can_list_hq_periods(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        """Positive test: branch_user CAN list periods on the HQ branch."""
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": hq_branch_id},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 200
        # Every returned period must be on HQ (not PAYTEST or any other branch)
        for p in resp.json():
            assert p["branch_id"] == hq_branch_id

    async def test_branch_user_cannot_get_paytest_driver(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        paytest_driver_id: int,
    ):
        """GET /core/drivers/{id} for a driver on PAYTEST returns 403."""
        resp = await client.get(
            f"/core/drivers/{paytest_driver_id}",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_branch_user_cannot_create_period_on_paytest(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        paytest_branch_id: int,
    ):
        """
        POST /payroll/periods with PAYTEST branch_id.
        Branch check fires before permission check → 403 for branch access denial.
        """
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_branch_id,
                "period_type": "Week",
                "start_date":  "2040-03-03",
                "end_date":    "2040-03-09",
            },
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Permission denial tests
# ---------------------------------------------------------------------------

class TestPermissionDenial:
    """
    branch_user HAS SpecificBranch access to HQ but PAYROLL_VIEWER role carries
    no write permissions.  The branch check passes; the action-level permission
    check fires and returns 403.
    """

    async def test_viewer_cannot_create_period_on_hq(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        """
        POST /payroll/periods on HQ branch.
        Branch access check passes; payroll.period.create permission check fires → 403.
        """
        resp = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   hq_branch_id,
                "period_type": "Week",
                "start_date":  "2041-05-05",
                "end_date":    "2041-05-11",
            },
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403
        assert "permission" in resp.json()["detail"].lower()

    async def test_viewer_cannot_create_driver(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        """
        POST /core/drivers on HQ branch.
        Branch access check passes; drivers.manage permission check fires → 403.
        """
        resp = await client.post(
            "/core/drivers",
            json={
                "branch_id":      hq_branch_id,
                "full_name":      "Viewer Test Driver",
                "preferred_name": "VTD",
                "driver_code":    "VTD-VIEWER-001",
                "cdl_number":     "CDL-VTD-001",
                "email":          "vtd_viewer@example.com",
            },
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403
        assert "permission" in resp.json()["detail"].lower()

    async def test_viewer_cannot_approve_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        created_driver_id: int,
        paytest_rate_type_id: int,
    ):
        """
        Admin creates a PendingApproval rate for the HQ driver (created_driver_id).

        Positive test (read):
          branch_user CAN read the rate — it is on HQ which they have access to.

        Negative test (approve):
          branch_user CANNOT approve — lacks payroll.approve_rate → 403.

        Cleanup: admin voids the rate (admin has payroll.approve_rate).
        """
        headers_admin = auth(auth_token)

        # Admin creates a PendingApproval rate for an HQ driver
        r = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      created_driver_id,
                "rate_type_id":   paytest_rate_type_id,
                "amount":         "12.50",
                "effective_from": "2045-06-01",
            },
            headers=headers_admin,
        )
        assert r.status_code == 201, f"Rate creation failed: {r.text}"
        rate_id = r.json()["driver_rate_id"]

        try:
            # Fix 2: branch_user now also requires payrates.view to read rates.
            # PAYROLL_VIEWER has no payrates.* permissions → 403 on read too.
            read_resp = await session_client.get(
                f"/payroll/rates/{rate_id}",
                headers=auth(branch_user_token),
            )
            assert read_resp.status_code == 403, (
                f"branch_user lacks payrates.view — expected 403, got {read_resp.status_code}: {read_resp.text}"
            )

            # Negative: branch_user CANNOT approve — lacks payrates.edit → 403
            approve_resp = await session_client.post(
                f"/payroll/rates/{rate_id}/approve",
                headers=auth(branch_user_token),
            )
            assert approve_resp.status_code == 403
            assert "permission" in approve_resp.json()["detail"].lower()

        finally:
            # Cleanup: admin voids the rate so it doesn't linger
            await session_client.delete(
                f"/payroll/rates/{rate_id}",
                headers=headers_admin,
            )

    async def test_viewer_cannot_change_period_status(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        """
        branch_user has branch access to HQ but no payroll.entry permission.
        PATCH /payroll/periods/{id}/status on an HQ period must return 403
        from the action-level permission gate.
        """
        # Admin creates a Draft period on HQ for this test
        create = await session_client.post(
            "/payroll/periods",
            json={
                "branch_id":   hq_branch_id,
                "period_type": "Week",
                "start_date":  "2042-03-03",
                "end_date":    "2042-03-09",
            },
            headers=auth(auth_token),
        )
        assert create.status_code == 201, f"Period creation failed: {create.text}"
        pid = create.json()["payroll_period_id"]

        try:
            # branch_user CAN read the period (branch access passes)
            read_resp = await session_client.get(
                f"/payroll/periods/{pid}",
                headers=auth(branch_user_token),
            )
            assert read_resp.status_code == 200

            # branch_user CANNOT change its status — lacks payroll.entry
            status_resp = await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Open"},
                headers=auth(branch_user_token),
            )
            assert status_resp.status_code == 403
            assert "permission" in status_resp.json()["detail"].lower()

        finally:
            # Cleanup: cancel the period so it doesn't linger
            await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Cancelled"},
                headers=auth(auth_token),
            )


# ---------------------------------------------------------------------------
# Stale token safety
# ---------------------------------------------------------------------------

class TestStaleTokenRejection:
    """
    Verify that a valid JWT for a deactivated user / user with login disabled
    is rejected at the service layer via _check_branch_access.

    The JWT is cryptographically valid and not expired, but the DB view now
    filters userisactive=TRUE and canlogin=TRUE, so the user gets 403.
    """

    async def test_deactivated_user_token_rejected(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        Create a user with AllCompanyBranches access, login to get a token,
        deactivate the account, then verify the old token returns 403.
        """
        # 1. Create a user and assign AllCompanyBranches role
        create_resp = await session_client.post(
            "/admin/users",
            json={
                "username":             "stale_token_user",
                "display_name":         "Stale Token Test",
                "password":             "StalePass123!",
                "must_change_password": False,
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        stale_uid = create_resp.json()["user_id"]

        # 2. Assign AllCompanyBranches role (need a role_id)
        roles = await session_client.get("/admin/roles", headers=auth(auth_token))
        admin_role_id = next(
            r["role_id"] for r in roles.json() if r["role_code"] == "PAYROLL_ADMIN"
        )
        await session_client.post(
            f"/admin/users/{stale_uid}/roles",
            json={"role_id": admin_role_id, "scope_type": "AllCompanyBranches"},
            headers=auth(auth_token),
        )

        # 3. Login as the new user to get their token
        login = await session_client.post("/auth/login", json={
            "username":     "stale_token_user",
            "password":     "StalePass123!",
            "company_code": "DEMO",
        })
        assert login.status_code == 200
        stale_token = login.json()["access_token"]

        # 4. Verify the token works while the account is active
        active_resp = await session_client.get(
            "/core/branches",
            headers=auth(stale_token),
        )
        assert active_resp.status_code == 200

        # 5. Deactivate the account via admin API
        await session_client.patch(
            f"/admin/users/{stale_uid}",
            json={"is_active": False},
            headers=auth(auth_token),
        )

        # 6. The old (still cryptographically valid) token must now return 403
        stale_resp = await session_client.get(
            "/core/branches",
            headers=auth(stale_token),
        )
        assert stale_resp.status_code == 403

    async def test_login_disabled_token_rejected(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Disabling CanLogin on an account should immediately invalidate
        the user's ability to call protected endpoints.
        """
        # Create, assign role, login
        create_resp = await session_client.post(
            "/admin/users",
            json={
                "username":             "no_login_user",
                "display_name":         "No Login Test",
                "password":             "NoLoginPass123!",
                "must_change_password": False,
            },
            headers=auth(auth_token),
        )
        assert create_resp.status_code == 201
        uid = create_resp.json()["user_id"]

        roles = await session_client.get("/admin/roles", headers=auth(auth_token))
        admin_role_id = next(
            r["role_id"] for r in roles.json() if r["role_code"] == "PAYROLL_ADMIN"
        )
        await session_client.post(
            f"/admin/users/{uid}/roles",
            json={"role_id": admin_role_id, "scope_type": "AllCompanyBranches"},
            headers=auth(auth_token),
        )

        login = await session_client.post("/auth/login", json={
            "username": "no_login_user", "password": "NoLoginPass123!", "company_code": "DEMO",
        })
        assert login.status_code == 200
        token = login.json()["access_token"]

        # Disable login
        await session_client.patch(
            f"/admin/users/{uid}",
            json={"can_login": False},
            headers=auth(auth_token),
        )

        # Old token must now fail
        resp = await session_client.get("/core/branches", headers=auth(token))
        assert resp.status_code == 403
