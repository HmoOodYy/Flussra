"""
Integration tests for bulk pay item config update.

Endpoint:
    PATCH /settings/branches/pay-items/{item_id}/bulk-config

Tests
-----
1.  AllBranches — updates every active branch successfully
2.  SelectedBranches — updates only the listed branches
3.  SpecificBranch user cannot bulk-update (403)
4.  Missing setup.manage cannot bulk-update (403)
5.  Unknown branch_id in SelectedBranches → 422, no writes
6.  effective_from in the past → 422 (schema validator)
7.  One branch blocked by open period → entire request fails, no partial writes
8.  effective_from=null with open period → auto-scheduled after period, succeeds
9.  Retired pay item → 404
10. Audit record written (PAY_ITEM_BULK_CONFIG exists in audit log)
11. Response schema complete: pay_item_id, pay_item_code, target,
    requested_branch_count, updated_branch_count, results[].config_id / .status
12. SelectedBranches with empty branch_ids → 422 (schema validation)
"""
import pytest
import pytest_asyncio
import httpx
from datetime import date, timedelta
from unittest.mock import patch, AsyncMock

from app.settings import service as settings_service


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


BULK_URL = "/settings/branches/pay-items/{item_id}/bulk-config"


# ---------------------------------------------------------------------------
# Fixture: get a real pay item id by code (HOURS is always present)
# ---------------------------------------------------------------------------

async def _get_pay_item_id(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    code: str = "HOURS",
) -> int:
    resp = await client.get(
        f"/settings/branches/{branch_id}/pay-items",
        headers=auth(token),
    )
    assert resp.status_code == 200, resp.text
    item = next((i for i in resp.json() if i["pay_item_code"] == code), None)
    assert item is not None, f"Pay item {code} not found"
    return item["pay_item_id"]


# ---------------------------------------------------------------------------
# 1. AllBranches — happy path
# ---------------------------------------------------------------------------

class TestBulkAllBranches:

    async def test_all_branches_success(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """AllBranches target updates every active branch; response includes all."""
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "MILES")

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={
                "target": "AllBranches",
                "is_active": True,
                "notes": "bulk test",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["pay_item_id"] == item_id
        assert body["pay_item_code"] == "MILES"
        assert body["target"] == "AllBranches"
        assert body["requested_branch_count"] >= 1
        assert body["updated_branch_count"] == body["requested_branch_count"]
        assert len(body["results"]) == body["updated_branch_count"]

    async def test_all_branches_response_schema(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Every result row must contain the required fields (test 11)."""
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "BONUS")

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={"target": "AllBranches", "is_active": True},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for field in ("pay_item_id", "pay_item_code", "target",
                      "requested_branch_count", "updated_branch_count", "results"):
            assert field in body, f"Top-level field missing: {field}"

        for row in body["results"]:
            for field in ("branch_id", "branch_name", "status", "config_id", "effective_from"):
                assert field in row, f"Result field missing: {field}"
            assert row["status"] in ("Created", "Updated", "Versioned")
            assert row["config_id"] > 0

    async def test_all_branches_inactive(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        Deactivate an item across all branches.

        Uses SILOS (not PALLETS) to avoid polluting HQ PALLETS state which
        test_settings_payitems.py::test_pending_config_with_future_effective_from
        requires to start with is_using_default=True.
        SILOS is only used in a validation test that never writes (returns 422).
        """
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "SILOS")

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={"target": "AllBranches", "is_active": False, "notes": "deactivated bulk"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["updated_branch_count"] >= 1


# ---------------------------------------------------------------------------
# 2. SelectedBranches — happy path
# ---------------------------------------------------------------------------

class TestBulkSelectedBranches:

    async def test_selected_branches_only_updates_listed(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
    ):
        """SelectedBranches updates exactly the listed branches."""
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "ADJUSTMENT")

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={
                "target": "SelectedBranches",
                "branch_ids": [hq_branch_id],
                "is_active": True,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["requested_branch_count"] == 1
        assert body["updated_branch_count"] == 1
        assert body["results"][0]["branch_id"] == hq_branch_id

    async def test_selected_branches_multi(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
    ):
        """SelectedBranches with two branch ids updates both."""
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "LOADS")

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={
                "target": "SelectedBranches",
                "branch_ids": [hq_branch_id, paytest_branch_id],
                "is_active": True,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["requested_branch_count"] == 2
        assert body["updated_branch_count"] == 2
        result_branch_ids = {r["branch_id"] for r in body["results"]}
        assert hq_branch_id in result_branch_ids
        assert paytest_branch_id in result_branch_ids


# ---------------------------------------------------------------------------
# 3. Permission: SpecificBranch user cannot bulk-update
# ---------------------------------------------------------------------------

class TestBulkPermissions:

    async def test_branch_user_cannot_bulk_update(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
        auth_token: str,
    ):
        """SpecificBranch scope → 403 (test 3)."""
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "HOURS")

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={"target": "AllBranches", "is_active": True},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_unauthenticated_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """No token → 401."""
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "HOURS")
        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={"target": "AllBranches", "is_active": True},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 4. Validation: invalid branch_id → 422, no writes
# ---------------------------------------------------------------------------

class TestBulkValidation:

    async def test_unknown_branch_id_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Non-existent branch_id causes 422 before any write (test 5)."""
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "SILOS")

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={
                "target": "SelectedBranches",
                "branch_ids": [hq_branch_id, 9999999],
                "is_active": True,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "9999999" in str(detail)

    async def test_effective_from_in_past_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """effective_from in the past → 422 from schema validator (test 6)."""
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "HOURS")
        yesterday = (date.today() - timedelta(days=1)).isoformat()

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={"target": "AllBranches", "is_active": True, "effective_from": yesterday},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_empty_branch_ids_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """SelectedBranches with no branch_ids → 422 (schema model_validator, test 12)."""
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "HOURS")

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={"target": "SelectedBranches", "branch_ids": [], "is_active": True},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_retired_pay_item_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Non-existent / retired pay item → 404 (test 9)."""
        resp = await client.patch(
            BULK_URL.format(item_id=9999999),
            json={"target": "AllBranches", "is_active": True},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_all_branches_with_branch_ids_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        branch_ids must be omitted (null) when target=AllBranches.
        Sending branch_ids with AllBranches is a schema error (422).
        """
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "HOURS")

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={
                "target": "AllBranches",
                "branch_ids": [hq_branch_id],   # must not be sent with AllBranches
                "is_active": True,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_duplicate_branch_ids_deduplicated(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        Duplicate branch_ids in SelectedBranches are silently deduplicated:
        requested_branch_count must equal the unique count (1), not the
        raw list length (2).

        Uses MILES (not HOURS) to avoid creating a config row on HOURS, which
        test_settings_payitems.py::test_hours_default_active expects to have
        is_using_default=True (no config row).
        """
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "MILES")

        resp = await client.patch(
            BULK_URL.format(item_id=item_id),
            json={
                "target": "SelectedBranches",
                "branch_ids": [hq_branch_id, hq_branch_id],   # duplicate
                "is_active": True,
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["requested_branch_count"] == 1, (
            "Duplicate branch_ids must be deduplicated before processing"
        )
        assert body["updated_branch_count"] == 1


# ---------------------------------------------------------------------------
# 5. Period-protection — one blocked branch fails entire request (test 7)
# ---------------------------------------------------------------------------

class TestBulkPeriodProtection:

    async def test_one_branch_blocked_fails_all(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
    ):
        """
        When one target branch has an open payroll period whose end date is
        on or after the supplied effective_from, the whole request is rejected
        with 422 and branch_errors.  No writes occur on any branch (test 7).
        """
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "OVERNIGHT")

        # Capture the state of the PAYTEST branch before the attempt
        before_resp = await client.get(
            f"/settings/branches/{paytest_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert before_resp.status_code == 200
        before_item = next(
            (i for i in before_resp.json() if i["pay_item_id"] == item_id), None
        )
        before_config = before_item["current_config"] if before_item else None

        # Simulate an open period on HQ branch by mocking the period-end helper
        future_date = date.today() + timedelta(days=30)

        async def mock_period_end(branch_id: int, company_id: int, db) -> "date | None":  # noqa: F821
            if branch_id == hq_branch_id:
                return date.today() + timedelta(days=14)  # open period ends in 14 days
            return None

        with patch.object(settings_service, "_get_current_open_period_max_end",
                          side_effect=mock_period_end):
            resp = await client.patch(
                BULK_URL.format(item_id=item_id),
                json={
                    "target": "SelectedBranches",
                    "branch_ids": [hq_branch_id, paytest_branch_id],
                    "is_active": False,
                    # effective_from is today — inside the mocked open period for HQ
                    "effective_from": date.today().isoformat(),
                },
                headers=auth(auth_token),
            )

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "branch_errors" in detail
        assert any(e["branch_id"] == hq_branch_id for e in detail["branch_errors"])

        # PAYTEST branch must NOT have been modified (atomicity — test 7)
        after_resp = await client.get(
            f"/settings/branches/{paytest_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert after_resp.status_code == 200
        after_item = next(
            (i for i in after_resp.json() if i["pay_item_id"] == item_id), None
        )
        after_config = after_item["current_config"] if after_item else None
        # Config must be unchanged (same config_id or both None)
        assert after_config == before_config, (
            "PAYTEST branch config changed despite the bulk request failing — "
            "partial write occurred!"
        )

    async def test_null_effective_from_auto_schedules(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        When effective_from is null and a branch has an open period, the backend
        auto-schedules to day after period end.  Request succeeds (test 8).
        """
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "WAIT_TIME")
        period_end = date.today() + timedelta(days=7)
        expected_eff = period_end + timedelta(days=1)

        async def mock_period_end(branch_id: int, company_id: int, db) -> "date | None":  # noqa: F821
            return period_end

        with patch.object(settings_service, "_get_current_open_period_max_end",
                          side_effect=mock_period_end):
            resp = await client.patch(
                BULK_URL.format(item_id=item_id),
                json={"target": "AllBranches", "is_active": True},
                headers=auth(auth_token),
            )

        assert resp.status_code == 200, resp.text
        for row in resp.json()["results"]:
            assert row["effective_from"] == expected_eff.isoformat(), (
                f"Expected {expected_eff.isoformat()} but got {row['effective_from']}"
            )


# ---------------------------------------------------------------------------
# 5b. Write-phase failure → full rollback (no partial writes)
# ---------------------------------------------------------------------------

class TestBulkWritePhaseRollback:

    async def test_write_phase_failure_rolls_back(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        paytest_branch_id: int,
    ):
        """
        If _apply_pay_item_config_to_branch raises on the second branch write,
        the implicit engine.begin() transaction must roll back the first write too.
        Verifies true atomicity beyond the pre-write validation phase.
        """
        item_id = await _get_pay_item_id(client, auth_token, hq_branch_id, "ADJUSTMENT")

        # Capture PAYTEST config before the attempt
        before_resp = await client.get(
            f"/settings/branches/{paytest_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert before_resp.status_code == 200
        before_item = next(
            (i for i in before_resp.json() if i["pay_item_id"] == item_id), None
        )
        before_config = before_item["current_config"] if before_item else None

        # Also capture HQ config before the attempt
        before_hq_resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert before_hq_resp.status_code == 200
        before_hq_item = next(
            (i for i in before_hq_resp.json() if i["pay_item_id"] == item_id), None
        )
        before_hq_config = before_hq_item["current_config"] if before_hq_item else None

        call_count = 0
        original_apply = settings_service._apply_pay_item_config_to_branch

        async def mock_apply(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise RuntimeError("Simulated write failure on second branch")
            return await original_apply(*args, **kwargs)

        # ASGITransport re-raises server-side exceptions in the test process,
        # so we catch RuntimeError here rather than asserting on a 500 status.
        request_raised = False
        with patch.object(
            settings_service,
            "_apply_pay_item_config_to_branch",
            side_effect=mock_apply,
        ):
            try:
                await client.patch(
                    BULK_URL.format(item_id=item_id),
                    json={
                        "target": "SelectedBranches",
                        "branch_ids": [hq_branch_id, paytest_branch_id],
                        "is_active": True,
                    },
                    headers=auth(auth_token),
                )
            except RuntimeError as exc:
                assert "Simulated write failure" in str(exc)
                request_raised = True

        assert request_raised, "Expected RuntimeError to propagate from write-phase failure"

        # Both branches must be unchanged (full rollback)
        after_hq_resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert after_hq_resp.status_code == 200
        after_hq_item = next(
            (i for i in after_hq_resp.json() if i["pay_item_id"] == item_id), None
        )
        after_hq_config = after_hq_item["current_config"] if after_hq_item else None
        assert after_hq_config == before_hq_config, (
            "HQ config changed despite write-phase failure — partial write was NOT rolled back!"
        )

        after_resp = await client.get(
            f"/settings/branches/{paytest_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert after_resp.status_code == 200
        after_item = next(
            (i for i in after_resp.json() if i["pay_item_id"] == item_id), None
        )
        after_config = after_item["current_config"] if after_item else None
        assert after_config == before_config, (
            "PAYTEST config changed despite write-phase failure — partial write was NOT rolled back!"
        )


# ---------------------------------------------------------------------------
# 6. Audit record written (test 10)
# ---------------------------------------------------------------------------

class TestBulkAudit:

    async def test_audit_record_written(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
        direct_db,
    ):
        """
        After a successful bulk update a PAY_ITEM_BULK_CONFIG audit record
        must exist in audit.auditlog (test 10).
        """
        item_id = await _get_pay_item_id(session_client, auth_token, hq_branch_id, "PTO_STATUS")

        resp = await session_client.patch(
            BULK_URL.format(item_id=item_id),
            json={"target": "AllBranches", "is_active": True, "notes": "audit test"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text

        from sqlalchemy import text as sa_text
        result = await direct_db.execute(
            sa_text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE  actioncode = 'PAY_ITEM_BULK_CONFIG'
                  AND  entityid   LIKE 'bulk:%'
            """)
        )
        count = result.scalar_one()
        assert count >= 1, "No PAY_ITEM_BULK_CONFIG audit record found"
