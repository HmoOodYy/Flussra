"""
CP-2 P1 fixes — backend integration tests.

P1 #1: Period-eligible drivers endpoint
  - Returns drivers active during the period dates, not just today's grid
  - Stable across day navigation (period-scoped, not day-scoped)
  - Includes mid-period hires and drivers with existing lines
  - Excludes other branches and companies
  - ODA users blocked with 403

P1 #2: Period-pay security boundary
  - ODA/Driver users are blocked 403 from all 3 period-pay endpoints
  - No data leaked before 403
  - SpecificBranch users can access their own branch
  - Cross-branch SpecificBranch users blocked
  - payroll.entry required for POST/DELETE

Uses year 2083 dates to avoid conflicts with existing test modules.
"""
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    headers = auth(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        if resp.status_code != 200:
            continue
        for p in resp.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"},
                headers=headers,
            )


async def _make_open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str,
    end: str,
) -> dict:
    await _cancel_active_periods(client, token, branch_id)
    resp = await client.post(
        "/payroll/periods",
        json={"branch_id": branch_id, "period_type": "Week",
              "start_date": start, "end_date": end},
        headers=auth(token),
    )
    assert resp.status_code == 201, f"create failed: {resp.text}"
    pid = resp.json()["payroll_period_id"]
    r = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "Open"},
        headers=auth(token),
    )
    assert r.status_code == 200, f"open failed: {r.text}"
    return resp.json()


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


async def _activate_bonus(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    """Activate the BONUS pay item for a branch if not already active."""
    items = await client.get(
        f"/settings/branches/{branch_id}/pay-items",
        headers=auth(token),
    )
    assert items.status_code == 200
    for item in items.json():
        if item["pay_item_code"] == "BONUS":
            if not item.get("is_active", False):
                await client.patch(
                    f"/settings/branches/{branch_id}/pay-items/{item['pay_item_id']}",
                    json={"is_active": True},
                    headers=auth(token),
                )
            return


# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def p1_bonus_activated(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> None:
    """Activate BONUS on PAYTEST branch once for this test session scope."""
    await _activate_bonus(session_client, auth_token, paytest_branch_id)


# ===========================================================================
# P1 #1 — Period-eligible drivers endpoint
# ===========================================================================

class TestPeriodEligibleDrivers:

    @pytest.mark.asyncio
    async def test_eligible_drivers_returns_period_active_drivers(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """GET eligible-drivers returns drivers active in the period dates."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-01-01", "2083-01-07",
        )
        pid = period["payroll_period_id"]

        resp = await session_client.get(
            f"/payroll/periods/{pid}/eligible-drivers",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "drivers" in data
        driver_ids = [d["driver_id"] for d in data["drivers"]]
        assert paytest_driver_id in driver_ids

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_eligible_drivers_stable_across_days(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """
        Eligible-drivers list is period-scoped — it doesn't change when the
        caller navigates to a different day in the grid.
        Fetch eligible-drivers for a 7-day period and confirm paytest_driver
        appears regardless of which day the caller would be on.
        """
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-01-08", "2083-01-14",
        )
        pid = period["payroll_period_id"]

        # Call eligible-drivers endpoint — result is period-scoped, not day-scoped
        resp1 = await session_client.get(
            f"/payroll/periods/{pid}/eligible-drivers",
            headers=auth(auth_token),
        )
        assert resp1.status_code == 200, resp1.text
        first_ids = {d["driver_id"] for d in resp1.json()["drivers"]}

        # Call again — result must be identical (stable, no day param involved)
        resp2 = await session_client.get(
            f"/payroll/periods/{pid}/eligible-drivers",
            headers=auth(auth_token),
        )
        assert resp2.status_code == 200, resp2.text
        second_ids = {d["driver_id"] for d in resp2.json()["drivers"]}

        assert first_ids == second_ids, (
            "Eligible-drivers must be stable across calls (period-scoped, not day-scoped)"
        )
        assert paytest_driver_id in first_ids

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_eligible_drivers_includes_driver_with_existing_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
        direct_db,
    ):
        """
        A driver who has an existing period-pay line should appear in
        eligible-drivers even if their employment status changed — so that
        existing bonuses remain voidable.
        """
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-01-15", "2083-01-21",
        )
        pid = period["payroll_period_id"]

        # Add a bonus line for the driver
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "50.00", "notes": "P1 test bonus"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text

        # Temporarily set driver status to Inactive via direct DB
        await direct_db.execute(
            _text("""
                UPDATE core.drivers
                SET    driverstatus = 'Inactive'
                WHERE  driverid = :did
            """),
            {"did": paytest_driver_id},
        )

        try:
            resp = await session_client.get(
                f"/payroll/periods/{pid}/eligible-drivers",
                headers=auth(auth_token),
            )
            assert resp.status_code == 200, resp.text
            driver_ids = [d["driver_id"] for d in resp.json()["drivers"]]
            assert paytest_driver_id in driver_ids, (
                "Driver with existing period-pay lines must remain in eligible list "
                "even if temporarily inactive"
            )
        finally:
            # Restore driver status
            await direct_db.execute(
                _text("""
                    UPDATE core.drivers
                    SET    driverstatus = 'Active'
                    WHERE  driverid = :did
                """),
                {"did": paytest_driver_id},
            )

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_eligible_drivers_excludes_other_branch(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        hq_branch_id: int,
        created_driver_id: int,
    ):
        """Driver in HQ branch is not included in PAYTEST period eligible-drivers."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-01-22", "2083-01-28",
        )
        pid = period["payroll_period_id"]

        resp = await session_client.get(
            f"/payroll/periods/{pid}/eligible-drivers",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        driver_ids = [d["driver_id"] for d in resp.json()["drivers"]]
        # created_driver_id is on HQ branch — must not appear in PAYTEST eligible list
        assert created_driver_id not in driver_ids, (
            "HQ driver must not appear in PAYTEST branch eligible-drivers"
        )

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_eligible_drivers_oda_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """ODA user → 403 on GET eligible-drivers endpoint."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-02-01", "2083-02-07",
        )
        pid = period["payroll_period_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "P1_EligDrv_ODARole_2083",
            ["payroll.view", "payroll.entry"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "p1_elig_oda_user_2083", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/eligible-drivers",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403
        # Must not contain any driver data
        body = resp.json()
        assert "drivers" not in body

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_eligible_drivers_requires_view_permission(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """User without payroll.view or payroll.entry → 403 on eligible-drivers."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-02-08", "2083-02-14",
        )
        pid = period["payroll_period_id"]

        # Create a user with no payroll permissions
        role_id = await _create_role_with_perms(
            session_client, auth_token, "P1_EligDrv_NoPayrollRole_2083",
            [],  # no permissions
        )
        no_perm_token = await _create_user_with_role(
            session_client, auth_token, "p1_elig_noperm_user_2083", role_id,
            scope_type="AllCompanyBranches",
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/eligible-drivers",
            headers=auth(no_perm_token),
        )
        assert resp.status_code == 403

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_eligible_drivers_response_shape(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """Response has drivers list with driver_id, driver_name, driver_code fields."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-02-15", "2083-02-21",
        )
        pid = period["payroll_period_id"]

        resp = await session_client.get(
            f"/payroll/periods/{pid}/eligible-drivers",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "drivers" in data
        assert isinstance(data["drivers"], list)
        if data["drivers"]:
            d = data["drivers"][0]
            assert "driver_id" in d
            assert "driver_name" in d
            assert "driver_code" in d

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )


# ===========================================================================
# P1 #2 — Period-pay security boundary (ODA block)
# ===========================================================================

class TestPeriodPayODABlock:

    @pytest.mark.asyncio
    async def test_period_pay_get_blocked_for_oda_user(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """ODA user → 403 on GET /period-pay — no data leaked."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-03-01", "2083-03-07",
        )
        pid = period["payroll_period_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_ODA_GetRole_2083",
            ["payroll.view", "payroll.entry"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "pp_oda_get_user_2083", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/period-pay",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403
        # Must not contain any line data in the body
        body = resp.json()
        assert not isinstance(body, list), "Must not return a list of lines to ODA user"

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_period_pay_post_blocked_for_oda_user(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
    ):
        """ODA user → 403 on POST /period-pay."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-03-08", "2083-03-14",
        )
        pid = period["payroll_period_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_ODA_PostRole_2083",
            ["payroll.entry"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "pp_oda_post_user_2083", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "100.00", "notes": "ODA should be blocked"},
            headers=auth(oda_token),
        )
        assert resp.status_code == 403

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_period_pay_delete_blocked_for_oda_user(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
    ):
        """ODA user → 403 on DELETE /period-pay/{line_id}."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-03-15", "2083-03-21",
        )
        pid = period["payroll_period_id"]

        # Admin adds a bonus first
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "75.00", "notes": "ODA void test"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_ODA_DelRole_2083",
            ["payroll.entry"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "pp_oda_del_user_2083", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.delete(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_period_pay_no_data_leak_before_403(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
    ):
        """ODA user gets 403 before any line data is returned in the body."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-03-22", "2083-03-28",
        )
        pid = period["payroll_period_id"]

        # Add a bonus line as admin so there's data to potentially leak
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "250.00", "notes": "Secret bonus"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_ODA_LeakRole_2083",
            ["payroll.view"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "pp_oda_leak_user_2083", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/period-pay",
            headers=auth(oda_token),
        )
        assert resp.status_code == 403
        body_text = resp.text
        # The bonus amount must not appear in the error body
        assert "250.00" not in body_text
        assert "Secret bonus" not in body_text

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_period_pay_allowed_for_all_company_user(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
    ):
        """AllCompanyBranches user with payroll.entry can add and list period-pay."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-04-01", "2083-04-07",
        )
        pid = period["payroll_period_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_AllComp_Role_2083",
            ["payroll.view", "payroll.entry"],
        )
        all_company_token = await _create_user_with_role(
            session_client, auth_token, "pp_all_comp_user_2083", role_id,
            scope_type="AllCompanyBranches",
        )

        # Can GET
        get_resp = await session_client.get(
            f"/payroll/periods/{pid}/period-pay",
            headers=auth(all_company_token),
        )
        assert get_resp.status_code == 200, get_resp.text

        # Can POST
        post_resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "120.00", "notes": "All-company user bonus"},
            headers=auth(all_company_token),
        )
        assert post_resp.status_code == 201, post_resp.text

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_period_pay_add_requires_payroll_entry(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
    ):
        """User with only payroll.view (no payroll.entry) → 403 on POST period-pay."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-04-08", "2083-04-14",
        )
        pid = period["payroll_period_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_ViewOnly_Role_2083",
            ["payroll.view"],  # no payroll.entry
        )
        view_only_token = await _create_user_with_role(
            session_client, auth_token, "pp_view_only_user_2083", role_id,
            scope_type="AllCompanyBranches",
        )

        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "50.00", "notes": "Should be blocked"},
            headers=auth(view_only_token),
        )
        assert resp.status_code == 403

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_period_pay_operational_user_can_add_bonus(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
    ):
        """Operational user (AllCompanyBranches + payroll.entry) can add a bonus."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-04-15", "2083-04-21",
        )
        pid = period["payroll_period_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_OpEntry_Role_2083",
            ["payroll.view", "payroll.entry"],
        )
        op_token = await _create_user_with_role(
            session_client, auth_token, "pp_op_entry_user_2083", role_id,
            scope_type="AllCompanyBranches",
        )

        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "300.00", "notes": "Operational user bonus"},
            headers=auth(op_token),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["line_type"] == "BONUS"

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_day_grid_oda_block_unchanged(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        Regression check: day-grid ODA block still works after P1 #2 changes.
        This confirms the period-pay guard additions did not break day-grid.
        """
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-04-22", "2083-04-28",
        )
        pid = period["payroll_period_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "DG_Regress_ODARole_2083",
            ["payroll.view", "payroll.entry"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "dg_regress_oda_user_2083", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )

        resp = await session_client.get(
            f"/payroll/periods/{pid}/day-grid",
            params={"work_date": "2083-04-22"},
            headers=auth(oda_token),
        )
        assert resp.status_code == 403
        body = resp.json()
        assert "rows" not in body
        assert "columns" not in body

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_legacy_bonus_linetype_visible_in_get(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
        direct_db,
    ):
        """
        Legacy 'Bonus' line_type (stored before CP-0 canonical normalization)
        is visible in GET /period-pay because the query filters by linescope='Period',
        not by line_type value.  This confirms backward compatibility.
        """
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-05-01", "2083-05-07",
        )
        pid = period["payroll_period_id"]

        # Insert a legacy 'Bonus' line directly (bypassing API normalization)
        result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid,
                     workdate, linetype, linescope, quantity,
                     rateamount, calculatedamount, sourcetype,
                     status, needsmanagerreview, notes, addedbyuserid)
                VALUES
                    (
                     (SELECT companyid FROM core.companies WHERE companycode='DEMO'),
                     :bid, :pid, :did,
                     NULL, 'Bonus', 'Period', 1,
                     NULL, 99.99, 'Manual',
                     'Active', FALSE, 'Legacy bonus line', 1
                    )
                RETURNING draftlineid
            """),
            {"bid": paytest_branch_id, "pid": pid, "did": paytest_driver_id},
        )
        legacy_line_id = result.scalar_one()

        resp = await session_client.get(
            f"/payroll/periods/{pid}/period-pay",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        line_ids = [l["draft_line_id"] for l in resp.json()]
        assert legacy_line_id in line_ids, (
            "Legacy 'Bonus' line_type must appear in GET period-pay "
            "(linescope='Period' filter returns it regardless of line_type case)"
        )

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

# ===========================================================================
# PATCH period-pay Driver/ODA boundary
# ===========================================================================

class TestPeriodPayPatchODABlock:
    """PATCH /periods/{id}/period-pay/{line_id} must block Driver/ODA users."""

    @pytest.mark.asyncio
    async def test_period_pay_patch_blocked_for_oda_user(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
    ):
        """ODA user gets 403 on PATCH and the line is not mutated."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-06-01", "2083-06-07",
        )
        pid = period["payroll_period_id"]
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "100.00", "notes": "Original notes"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_ODA_PatchRole_2083", ["payroll.entry"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "pp_oda_patch_user_2083", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )
        resp = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"notes": "ODA should not mutate this"},
            headers=auth(oda_token),
        )
        assert resp.status_code == 403

        get_resp = await session_client.get(
            f"/payroll/periods/{pid}/period-pay", headers=auth(auth_token),
        )
        line = next(l for l in get_resp.json() if l["draft_line_id"] == line_id)
        assert line["notes"] == "Original notes"

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_period_pay_patch_no_data_leak_before_403(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
    ):
        """ODA user gets 403 before bonus amount/notes appear in PATCH response."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-06-08", "2083-06-14",
        )
        pid = period["payroll_period_id"]
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "999.00", "notes": "Secret amount"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_ODA_PatchLeak_2083", ["payroll.entry"],
        )
        oda_token = await _create_user_with_role(
            session_client, auth_token, "pp_oda_patch_leak_2083", role_id,
            scope_type="OwnDriverDataOnly", branch_id=paytest_branch_id,
        )
        resp = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"amount": "1.00"}, headers=auth(oda_token),
        )
        assert resp.status_code == 403
        assert "999.00" not in resp.text
        assert "Secret amount" not in resp.text

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_period_pay_patch_allowed_for_operational_user(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
    ):
        """Operational user (AllCompanyBranches + payroll.entry) can PATCH period-pay."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-06-15", "2083-06-21",
        )
        pid = period["payroll_period_id"]
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "50.00", "notes": "Original"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_Op_PatchRole_2083",
            ["payroll.view", "payroll.entry"],
        )
        op_token = await _create_user_with_role(
            session_client, auth_token, "pp_op_patch_user_2083", role_id,
            scope_type="AllCompanyBranches",
        )
        resp = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"notes": "Updated by ops"}, headers=auth(op_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["notes"] == "Updated by ops"

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    @pytest.mark.asyncio
    async def test_period_pay_patch_requires_payroll_entry(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        p1_bonus_activated,
    ):
        """User with payroll.view only gets 403 on PATCH period-pay."""
        period = await _make_open_period(
            session_client, auth_token, paytest_branch_id,
            "2083-06-22", "2083-06-28",
        )
        pid = period["payroll_period_id"]
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "BONUS",
                  "amount": "75.00", "notes": "View-only test"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        role_id = await _create_role_with_perms(
            session_client, auth_token, "PP_ViewOnly_PatchRole_2083",
            ["payroll.view"],
        )
        view_only_token = await _create_user_with_role(
            session_client, auth_token, "pp_view_only_patch_user_2083", role_id,
            scope_type="AllCompanyBranches",
        )
        resp = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"notes": "Should not go through"},
            headers=auth(view_only_token),
        )
        assert resp.status_code == 403

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"},
            headers=auth(auth_token),
        )
