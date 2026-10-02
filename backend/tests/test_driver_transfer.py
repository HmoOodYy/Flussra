"""
tests/test_driver_transfer.py — Driver branch immutability guard tests.

Protects the current DRIVER/Self boundary:
  - legacy branch-scoped DRIVER assignments return 422
  - the linked Workforce DriverID and branch remain unchanged

The current Access authority requires a linked Employee and Self scope for
DRIVER assignments. Legacy branch-scoped requests are rejected before they
can change the Workforce profile.
"""
import random

import httpx
import pytest
from sqlalchemy import text as _text

from tests.builders.access import create_provisioned_test_user

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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
    user = await create_provisioned_test_user(
        client, token, f"xfr_{suffix}", driver_role_id,
        scope_type="Self", driver_branch_id=branch_id,
    )
    user_id = int(user["user_id"])
    profile = await _get_driver_profile(client, token, user_id)
    assert profile["has_driver_profile"] is True
    return user_id, profile["driver_id"]


# ===========================================================================
# Tests
# ===========================================================================

@pytest.mark.asyncio
class TestDriverBranchReassignment:

    async def test_driver_without_history_cannot_be_reassigned(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
    ):
        """A valid DRIVER/Self identity rejects a legacy branch assignment."""
        driver_role_id = await _get_driver_role_id(session_client, auth_token)
        user_id, driver_id = await _make_fresh_driver(
            session_client, auth_token, hq_branch_id, driver_role_id
        )

        # DRIVER/Self cannot be changed to the retired branch-scoped shape.
        r = await _assign_driver_role(
            session_client, auth_token, user_id, driver_role_id, paytest_branch_id
        )
        assert r.status_code == 422, (
            f"Expected 422 for direct branch reassignment, got {r.status_code}: {r.text}"
        )
        assert "self scope" in r.text.lower(), "Error should explain the DRIVER Self requirement."

        # Confirm the original profile remains authoritative.
        profile = await _get_driver_profile(session_client, auth_token, user_id)
        assert profile["branch_id"] == hq_branch_id
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
        assert "self scope" in r.text.lower(), (
            f"Error message should explain the DRIVER Self requirement: {r.text}"
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
        """422 detail explains that branch changes require Driver Transfer."""
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
        assert "self scope" in detail.lower(), (
            f"422 detail should explain the DRIVER Self requirement: {detail}"
        )
