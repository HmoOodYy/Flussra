"""
tests/test_driver_transfer.py — Driver branch reassignment guard tests.

Covers Part D of Foundation Lockdown Phase 2.1:
  - Driver with no history → branch reassignment succeeds (regression)
  - Driver with DraftLines → reassignment returns 422
  - Driver with FinalLines → reassignment returns 422
  - Driver with DriverRates → reassignment returns 422
  - Driver with DriverPayRules → reassignment returns 422
  - Error message is clear and branch is not mutated

The reassignment trigger: POST /admin/users/{id}/company-role-assignments
with a DRIVER company role on a different branch causes ensure_driver_profile
to attempt a BranchID update. The guard in ensure_driver_profile intercepts
this and returns 422 if payroll/rate history exists.
"""
import random
import pytest
import httpx
from sqlalchemy import text as _text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create_user(
    client: httpx.AsyncClient,
    token: str,
    username: str,
) -> int:
    resp = await client.post(
        "/admin/users",
        json={
            "username": username,
            "display_name": f"Transfer Test {username}",
            "password": "TestPass1234!",
            "is_active": True,
            "can_login": True,
            "must_change_password": False,
        },
        headers=auth(token),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["user_id"]


async def _get_driver_role_id(client: httpx.AsyncClient, token: str) -> int:
    resp = await client.get("/admin/company-roles", headers=auth(token))
    assert resp.status_code == 200, resp.text
    for r in resp.json():
        if r.get("role_code") == "DRIVER":
            return r["company_role_id"]
    pytest.skip("DRIVER company role not found — skipping driver transfer tests")


async def _assign_driver_role(
    client: httpx.AsyncClient,
    token: str,
    user_id: int,
    driver_role_id: int,
    branch_id: int,
) -> dict:
    resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json={
            "company_role_id": driver_role_id,
            "scope_type": "SpecificBranch",
            "branch_id": branch_id,
        },
        headers=auth(token),
    )
    return resp


async def _get_driver_profile(
    client: httpx.AsyncClient,
    token: str,
    user_id: int,
) -> dict:
    resp = await client.get(f"/admin/users/{user_id}/driver", headers=auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _get_company_id(direct_db, branch_id: int) -> int:
    row = await direct_db.execute(
        _text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
        {"bid": branch_id},
    )
    return row.scalar_one()


async def _get_any_period_for_branch(direct_db, branch_id: int) -> int | None:
    row = await direct_db.execute(
        _text("""
            SELECT payrollperiodid FROM payroll.payrollperiods
            WHERE branchid = :bid
            LIMIT 1
        """),
        {"bid": branch_id},
    )
    r = row.first()
    return r[0] if r else None


async def _get_any_rate_type_id(direct_db) -> int:
    row = await direct_db.execute(
        _text("SELECT ratetypeid FROM payroll.ratetypes LIMIT 1")
    )
    return row.scalar_one()


# ---------------------------------------------------------------------------
# Helper: create a fresh driver user on a branch, return (user_id, driver_id)
# ---------------------------------------------------------------------------

async def _make_fresh_driver(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    driver_role_id: int,
) -> tuple[int, int]:
    suffix = random.randint(100000, 999999)
    user_id = await _create_user(client, token, f"xfr_{suffix}")
    r = await _assign_driver_role(client, token, user_id, driver_role_id, branch_id)
    assert r.status_code == 201, f"role assignment failed: {r.text}"
    profile = await _get_driver_profile(client, token, user_id)
    assert profile["has_driver_profile"] is True
    return user_id, profile["driver_id"]


# ===========================================================================
# Tests
# ===========================================================================

@pytest.mark.asyncio
class TestDriverBranchReassignment:

    async def test_driver_no_history_can_be_reassigned(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
    ):
        """Driver with zero history can be directly reassigned to another branch."""
        driver_role_id = await _get_driver_role_id(session_client, auth_token)
        user_id, driver_id = await _make_fresh_driver(
            session_client, auth_token, hq_branch_id, driver_role_id
        )

        # Reassign to PAYTEST — no history, must succeed
        r = await _assign_driver_role(
            session_client, auth_token, user_id, driver_role_id, paytest_branch_id
        )
        assert r.status_code == 201, (
            f"Expected 201 for history-free driver reassignment, got {r.status_code}: {r.text}"
        )

        # Confirm branch updated
        profile = await _get_driver_profile(session_client, auth_token, user_id)
        assert profile["branch_id"] == paytest_branch_id
        assert profile["driver_id"] == driver_id

    async def test_driver_with_draft_lines_cannot_be_reassigned(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
        direct_db,
        created_period_id: int,
    ):
        """Driver with DraftLines returns 422 when reassigned to another branch."""
        driver_role_id = await _get_driver_role_id(session_client, auth_token)
        user_id, driver_id = await _make_fresh_driver(
            session_client, auth_token, hq_branch_id, driver_role_id
        )
        company_id = await _get_company_id(direct_db, hq_branch_id)

        # Inject a DraftLine for this driver (consistent branch)
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid,
                     linetype, quantity, sourcetype)
                VALUES
                    (:cid, :bid, :period_id, :driver_id, 'REGULAR', 1, 'TransferTest')
            """),
            {
                "cid": company_id,
                "bid": hq_branch_id,
                "period_id": created_period_id,
                "driver_id": driver_id,
            },
        )

        # Attempt reassignment → must fail with 422
        r = await _assign_driver_role(
            session_client, auth_token, user_id, driver_role_id, paytest_branch_id
        )
        assert r.status_code == 422, (
            f"Expected 422 when driver has DraftLines, got {r.status_code}: {r.text}"
        )
        assert "payroll" in r.text.lower() or "transfer" in r.text.lower(), (
            f"Error message should mention payroll history or transfer: {r.text}"
        )

        # Branch must NOT have changed
        profile = await _get_driver_profile(session_client, auth_token, user_id)
        assert profile["branch_id"] == hq_branch_id, (
            f"Branch must remain {hq_branch_id} after failed reassignment"
        )

    async def test_driver_with_final_lines_cannot_be_reassigned(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
        direct_db,
        created_period_id: int,
    ):
        """Driver with FinalLines returns 422 when reassigned to another branch."""
        driver_role_id = await _get_driver_role_id(session_client, auth_token)
        user_id, driver_id = await _make_fresh_driver(
            session_client, auth_token, hq_branch_id, driver_role_id
        )
        company_id = await _get_company_id(direct_db, hq_branch_id)

        # Inject a FinalLine (Phase 6: authorise via session-level GUC for test setup)
        await direct_db.execute(
            _text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)")
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid,
                     linetype, quantity, finalamount, sourcetype)
                VALUES
                    (:cid, :bid, :period_id, :driver_id, 'REGULAR', 1, 100, 'TransferTest')
            """),
            {
                "cid": company_id,
                "bid": hq_branch_id,
                "period_id": created_period_id,
                "driver_id": driver_id,
            },
        )

        r = await _assign_driver_role(
            session_client, auth_token, user_id, driver_role_id, paytest_branch_id
        )
        assert r.status_code == 422, (
            f"Expected 422 when driver has FinalLines, got {r.status_code}: {r.text}"
        )

        profile = await _get_driver_profile(session_client, auth_token, user_id)
        assert profile["branch_id"] == hq_branch_id

    async def test_driver_with_rates_cannot_be_reassigned(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """Driver with DriverRates returns 422 when reassigned to another branch."""
        driver_role_id = await _get_driver_role_id(session_client, auth_token)
        user_id, driver_id = await _make_fresh_driver(
            session_client, auth_token, hq_branch_id, driver_role_id
        )
        company_id = await _get_company_id(direct_db, hq_branch_id)
        rate_type_id = await _get_any_rate_type_id(direct_db)

        await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverrates
                    (companyid, branchid, driverid, ratetypeid,
                     amount, effectivefrom, status)
                VALUES
                    (:cid, :bid, :driver_id, :rt_id, 22.00, '2030-01-01', 'PendingApproval')
            """),
            {
                "cid": company_id,
                "bid": hq_branch_id,
                "driver_id": driver_id,
                "rt_id": rate_type_id,
            },
        )

        r = await _assign_driver_role(
            session_client, auth_token, user_id, driver_role_id, paytest_branch_id
        )
        assert r.status_code == 422, (
            f"Expected 422 when driver has DriverRates, got {r.status_code}: {r.text}"
        )

        profile = await _get_driver_profile(session_client, auth_token, user_id)
        assert profile["branch_id"] == hq_branch_id

    async def test_driver_with_pay_rules_cannot_be_reassigned(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """Driver with DriverPayRules returns 422 when reassigned to another branch."""
        driver_role_id = await _get_driver_role_id(session_client, auth_token)
        user_id, driver_id = await _make_fresh_driver(
            session_client, auth_token, hq_branch_id, driver_role_id
        )
        company_id = await _get_company_id(direct_db, hq_branch_id)

        await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverpayrules
                    (companyid, branchid, driverid, ruletype,
                     amount, effectivefrom, status)
                VALUES
                    (:cid, :bid, :driver_id, 'MinimumPay', 600.00, '2030-01-01', 'Active')
            """),
            {
                "cid": company_id,
                "bid": hq_branch_id,
                "driver_id": driver_id,
            },
        )

        r = await _assign_driver_role(
            session_client, auth_token, user_id, driver_role_id, paytest_branch_id
        )
        assert r.status_code == 422, (
            f"Expected 422 when driver has DriverPayRules, got {r.status_code}: {r.text}"
        )

        profile = await _get_driver_profile(session_client, auth_token, user_id)
        assert profile["branch_id"] == hq_branch_id

    async def test_reassignment_error_message_is_clear(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
        direct_db,
        created_period_id: int,
    ):
        """422 detail mentions payroll/rate history and transfer workflow."""
        driver_role_id = await _get_driver_role_id(session_client, auth_token)
        user_id, driver_id = await _make_fresh_driver(
            session_client, auth_token, hq_branch_id, driver_role_id
        )
        company_id = await _get_company_id(direct_db, hq_branch_id)

        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid,
                     linetype, quantity, sourcetype)
                VALUES
                    (:cid, :bid, :period_id, :driver_id, 'REGULAR', 1, 'MsgTest')
            """),
            {
                "cid": company_id,
                "bid": hq_branch_id,
                "period_id": created_period_id,
                "driver_id": driver_id,
            },
        )

        r = await _assign_driver_role(
            session_client, auth_token, user_id, driver_role_id, paytest_branch_id
        )
        assert r.status_code == 422
        detail = r.json().get("detail", "")
        assert "transfer" in detail.lower(), (
            f"422 detail should mention 'Transfer': {detail}"
        )
