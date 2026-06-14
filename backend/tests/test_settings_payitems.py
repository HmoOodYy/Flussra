"""
Integration tests for M11 (fixed) — Pay Items & Branch Configuration API.

Endpoints tested
----------------
GET   /settings/branches/{id}/pay-items
PATCH /settings/branches/{id}/pay-items/{item_id}
GET   /settings/branches/{id}/pay-items/{item_id}/history
GET   /settings/branches/{id}/pay-items/missing

Issues addressed
----------------
#1  Alembic wiring — 0002/0003 Python wrappers now exist in migrations/versions/
#2  Effective-dated versioning — PATCH closes the old row and opens a new one
#3  Period-safety warning — has_open_periods flag in PATCH response
#4  explicit effective_from accepted in PATCH body
#5  Branch validation — unknown branch or wrong-company branch returns 404
#6  Race protection on first INSERT — SAIntegrityError → clean 422
#7  Audit: separate create/version/update codes; old-state snapshot captured
#8  Schema now exposes current_config, pending_config, line/rate type maps,
    plus the new history endpoint

Users tested
------------
admin        AllCompanyBranches scope, PAYROLL_ADMIN (all permissions)
branch_user  SpecificBranch=HQ, PAYROLL_VIEWER (no write permissions)
"""
import pytest
import pytest_asyncio
import httpx
from datetime import date, timedelta
from unittest.mock import patch
from app.settings import service as settings_service


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _item_by_code(
    client: httpx.AsyncClient,
    auth_token: str,
    branch_id: int,
    code: str,
) -> dict:
    resp = await client.get(
        f"/settings/branches/{branch_id}/pay-items",
        headers=auth(auth_token),
    )
    assert resp.status_code == 200, resp.text
    item = next((i for i in resp.json() if i["pay_item_code"] == code), None)
    assert item is not None, f"{code} not found — check seed migration 0003"
    return item


# ===========================================================================
# TestListPayItems
# ===========================================================================

class TestListPayItems:

    async def test_requires_auth(
        self, client: httpx.AsyncClient, hq_branch_id: int
    ):
        resp = await client.get(f"/settings/branches/{hq_branch_id}/pay-items")
        assert resp.status_code == 401

    async def test_branch_user_can_read_own_branch(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 200

    async def test_branch_user_denied_other_branch(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        paytest_branch_id: int,
    ):
        resp = await client.get(
            f"/settings/branches/{paytest_branch_id}/pay-items",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_unknown_branch_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Branch not in this company → 404 (fix #5)."""
        resp = await client.get(
            "/settings/branches/9999999/pay-items",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_returns_all_seeded_items(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        codes = {i["pay_item_code"] for i in resp.json()}
        for expected in ("HOURS", "MILES", "LOADS", "OVERNIGHT", "WAIT_TIME",
                         "PALLETS", "SILOS", "PTO_STATUS", "BONUS",
                         "ADJUSTMENT", "GUARANTEED_MINIMUM"):
            assert expected in codes, f"{expected} missing from pay items list"

    async def test_schema_complete(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        item = resp.json()[0]
        for field in (
            "pay_item_id", "pay_item_code", "pay_item_name",
            "category", "data_type", "sort_order",
            "appears_in_payroll_entry", "appears_in_ledger",
            "is_system_standard", "item_scope", "rate_behavior",
            "is_active", "is_using_default",
            "current_config", "pending_config",       # fix #8 schema
            "line_type_mappings", "rate_type_mappings",
            "has_open_periods",
        ):
            assert field in item, f"Missing field: {field}"

    async def test_hours_default_active(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        hours = await _item_by_code(client, auth_token, hq_branch_id, "HOURS")
        assert hours["is_active"]        is True
        assert hours["is_using_default"] is True
        assert hours["current_config"]   is None
        assert hours["pending_config"]   is None

    async def test_overnight_default_inactive(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        item = await _item_by_code(client, auth_token, hq_branch_id, "OVERNIGHT")
        assert item["is_active"]        is False
        assert item["is_using_default"] is True


# ===========================================================================
# TestUpdatePayItemConfig  (fix #2, #3, #4, #5, #6, #7)
# ===========================================================================

class TestUpdatePayItemConfig:

    async def test_requires_auth(
        self, client: httpx.AsyncClient, hq_branch_id: int
    ):
        resp = await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/1",
            json={"is_active": True},
        )
        assert resp.status_code == 401

    async def test_branch_user_denied(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        resp = await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/1",
            json={"is_active": True},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_unknown_branch_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Branch not in this company → 404 (fix #5)."""
        resp = await client.patch(
            "/settings/branches/9999999/pay-items/1",
            json={"is_active": True},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_unknown_item_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        resp = await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/9999999",
            json={"is_active": True},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_effective_from_in_past_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """fix #4 — past dates must be rejected with 422."""
        past = str(date.today() - timedelta(days=5))
        item = await _item_by_code(client, auth_token, hq_branch_id, "SILOS")
        resp = await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{item['pay_item_id']}",
            json={"is_active": True, "effective_from": past},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_config_first_time(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """First PATCH creates a new open config row (fix #2 — not UPDATE-in-place).

        Uses GUARANTEED_MINIMUM (Period scope, IsDefaultBranchActive=FALSE) which
        is never touched by the session autouse fixture, so it reliably starts with
        no config row (is_using_default=True).
        """
        item = await _item_by_code(client, auth_token, paytest_branch_id, "GUARANTEED_MINIMUM")
        assert item["is_using_default"] is True, "GUARANTEED_MINIMUM should start with no config"
        iid = item["pay_item_id"]

        resp = await client.patch(
            f"/settings/branches/{paytest_branch_id}/pay-items/{iid}",
            json={"is_active": True, "notes": "Enabled for PAYTEST"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_active"]        is True
        assert body["is_using_default"] is False
        assert body["current_config"]   is not None
        assert body["current_config"]["is_active"] is True
        assert body["current_config"]["notes"]      == "Enabled for PAYTEST"
        assert body["pending_config"]  is None

    async def test_same_day_amend_updates_in_place(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        Two PATCHes on the same day with the same effective_from (today) must
        UPDATE in place — not create two open rows (fix #2 versioning semantics).
        """
        item = await _item_by_code(client, auth_token, paytest_branch_id, "OVERNIGHT")
        iid = item["pay_item_id"]

        r1 = await client.patch(
            f"/settings/branches/{paytest_branch_id}/pay-items/{iid}",
            json={"is_active": True, "notes": "v1"},
            headers=auth(auth_token),
        )
        assert r1.status_code == 200
        config_id_1 = r1.json()["current_config"]["config_id"]

        r2 = await client.patch(
            f"/settings/branches/{paytest_branch_id}/pay-items/{iid}",
            json={"is_active": False, "notes": "v2"},
            headers=auth(auth_token),
        )
        assert r2.status_code == 200
        config_id_2 = r2.json()["current_config"]["config_id"]

        # Same config_id = updated in place, not a new row
        assert config_id_1 == config_id_2
        assert r2.json()["is_active"]        is False
        assert r2.json()["current_config"]["notes"] == "v2"

    async def test_future_effective_from_creates_pending_config(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        A PATCH with effective_from > today leaves is_using_default=True
        (no current config) and populates pending_config (fix #2 + #4).
        """
        item = await _item_by_code(client, auth_token, hq_branch_id, "PALLETS")
        iid = item["pay_item_id"]
        future = str(date.today() + timedelta(days=30))

        resp = await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{iid}",
            json={"is_active": True, "notes": "Goes live next month", "effective_from": future},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["is_using_default"]  is True   # not yet in effect
        assert body["current_config"]    is None
        assert body["pending_config"]    is not None
        assert body["pending_config"]["effective_from"] == future
        assert body["pending_config"]["is_active"]      is True

    async def test_new_future_version_closes_existing_row(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        PATCH with a future effective_from on an already-configured item
        must close the existing open row and open a new one (fix #2 versioning).
        """
        item = await _item_by_code(client, auth_token, hq_branch_id, "WAIT_TIME")
        iid = item["pay_item_id"]

        # Establish today's config
        r1 = await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{iid}",
            json={"is_active": True, "notes": "current"},
            headers=auth(auth_token),
        )
        assert r1.status_code == 200
        old_config_id = r1.json()["current_config"]["config_id"]

        # Schedule a future change
        future = str(date.today() + timedelta(days=14))
        r2 = await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{iid}",
            json={"is_active": False, "notes": "going offline", "effective_from": future},
            headers=auth(auth_token),
        )
        assert r2.status_code == 200
        body = r2.json()
        # Current config still in effect
        assert body["current_config"]  is not None
        assert body["current_config"]["config_id"]     == old_config_id
        assert body["current_config"]["effective_to"]  == str(
            date.today() + timedelta(days=13)
        )
        # New pending change
        assert body["pending_config"] is not None
        assert body["pending_config"]["is_active"]      is False
        assert body["pending_config"]["effective_from"] == future

    async def test_notes_can_be_set_and_cleared(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        item = await _item_by_code(client, auth_token, hq_branch_id, "BONUS")
        iid = item["pay_item_id"]

        r_set = await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{iid}",
            json={"is_active": True, "notes": "Bonus note"},
            headers=auth(auth_token),
        )
        assert r_set.json()["current_config"]["notes"] == "Bonus note"

        r_clear = await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{iid}",
            json={"is_active": True, "notes": None},
            headers=auth(auth_token),
        )
        assert r_clear.json()["current_config"]["notes"] is None

    async def test_change_blocked_inside_current_open_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        When effective_from falls inside a currently-running open period,
        the request must be rejected with 422.

        A 'current' period means startdate <= today <= enddate.
        """
        today = date.today()

        # Create a branch + a period that contains today
        br = await client.post(
            "/settings/branches",
            json={"branch_name": "Period Protection Branch A", "branch_code": "PPBA"},
            headers=auth(auth_token),
        )
        assert br.status_code == 201
        bid = br.json()["branch_id"]

        period = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   bid,
                "period_type": "Week",
                "start_date":  str(today - timedelta(days=1)),
                "end_date":    str(today + timedelta(days=5)),
            },
            headers=auth(auth_token),
        )
        assert period.status_code == 201
        pid = period.json()["payroll_period_id"]
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=auth(auth_token))

        try:
            item = await _item_by_code(client, auth_token, bid, "HOURS")
            # Providing today as effective_from is inside the open period → 422
            resp = await client.patch(
                f"/settings/branches/{bid}/pay-items/{item['pay_item_id']}",
                json={"is_active": False, "effective_from": str(today)},
                headers=auth(auth_token),
            )
            assert resp.status_code == 422
            assert "open" in resp.json()["detail"].lower() or "period" in resp.json()["detail"].lower()
        finally:
            await client.patch(f"/payroll/periods/{pid}/status",
                               json={"status": "Cancelled"}, headers=auth(auth_token))

    async def test_auto_pushed_past_current_open_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        When effective_from is omitted and there is a currently-running open
        period, the config must be auto-scheduled to period.end_date + 1.

        The response shows pending_config (not current_config) and
        has_open_periods = True.
        """
        today = date.today()

        br = await client.post(
            "/settings/branches",
            json={"branch_name": "Period Protection Branch B", "branch_code": "PPBB"},
            headers=auth(auth_token),
        )
        assert br.status_code == 201
        bid = br.json()["branch_id"]
        period_end = today + timedelta(days=5)

        period = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   bid,
                "period_type": "Week",
                "start_date":  str(today - timedelta(days=1)),
                "end_date":    str(period_end),
            },
            headers=auth(auth_token),
        )
        assert period.status_code == 201
        pid = period.json()["payroll_period_id"]
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=auth(auth_token))

        try:
            item = await _item_by_code(client, auth_token, bid, "MILES")
            resp = await client.patch(
                f"/settings/branches/{bid}/pay-items/{item['pay_item_id']}",
                json={"is_active": False},   # no effective_from provided
                headers=auth(auth_token),
            )
            assert resp.status_code == 200
            body = resp.json()

            expected_date = str(period_end + timedelta(days=1))
            assert body["has_open_periods"]              is True
            assert body["current_config"]                is None  # not in effect yet
            assert body["pending_config"]                is not None
            assert body["pending_config"]["effective_from"] == expected_date
            assert body["pending_config"]["is_active"]      is False
        finally:
            await client.patch(f"/payroll/periods/{pid}/status",
                               json={"status": "Cancelled"}, headers=auth(auth_token))

    async def test_explicit_date_after_open_period_allowed(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Providing effective_from = period.end_date + 1 explicitly must be
        accepted even when a current open period exists.
        """
        today = date.today()

        br = await client.post(
            "/settings/branches",
            json={"branch_name": "Period Protection Branch C", "branch_code": "PPBC"},
            headers=auth(auth_token),
        )
        assert br.status_code == 201
        bid = br.json()["branch_id"]
        period_end = today + timedelta(days=3)

        period = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   bid,
                "period_type": "Week",
                "start_date":  str(today - timedelta(days=1)),
                "end_date":    str(period_end),
            },
            headers=auth(auth_token),
        )
        assert period.status_code == 201
        pid = period.json()["payroll_period_id"]
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=auth(auth_token))

        try:
            item = await _item_by_code(client, auth_token, bid, "LOADS")
            next_start = str(period_end + timedelta(days=1))
            resp = await client.patch(
                f"/settings/branches/{bid}/pay-items/{item['pay_item_id']}",
                json={"is_active": True, "effective_from": next_start},
                headers=auth(auth_token),
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["has_open_periods"] is True
            assert body["pending_config"]["effective_from"] == next_start
        finally:
            await client.patch(f"/payroll/periods/{pid}/status",
                               json={"status": "Cancelled"}, headers=auth(auth_token))

    async def test_future_period_does_not_block_today_change(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        An open period whose startdate > today does NOT block a change
        applied effective today.  Only currently-running periods (containing
        today in their date range) trigger the protection.
        """
        today = date.today()

        br = await client.post(
            "/settings/branches",
            json={"branch_name": "Period Protection Branch D", "branch_code": "PPBD"},
            headers=auth(auth_token),
        )
        assert br.status_code == 201
        bid = br.json()["branch_id"]

        # Period that starts in the future — does NOT contain today
        future_start = today + timedelta(days=30)
        period = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   bid,
                "period_type": "Week",
                "start_date":  str(future_start),
                "end_date":    str(future_start + timedelta(days=6)),
            },
            headers=auth(auth_token),
        )
        assert period.status_code == 201
        pid = period.json()["payroll_period_id"]
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Open"}, headers=auth(auth_token))

        try:
            item = await _item_by_code(client, auth_token, bid, "OVERNIGHT")
            # No effective_from → defaults to today, which is NOT inside the future period
            resp = await client.patch(
                f"/settings/branches/{bid}/pay-items/{item['pay_item_id']}",
                json={"is_active": True},
                headers=auth(auth_token),
            )
            assert resp.status_code == 200
            body = resp.json()
            # Change took effect today — current_config, not pending
            assert body["has_open_periods"]  is False   # future period doesn't count
            assert body["current_config"]    is not None
            assert body["current_config"]["is_active"] is True
        finally:
            await client.patch(f"/payroll/periods/{pid}/status",
                               json={"status": "Cancelled"}, headers=auth(auth_token))

    # Note: "no open period → effective_from defaults to today" is already covered by
    # test_create_config_first_time (uses PAYTEST branch with no active period).

    async def test_audit_rollback_on_write_failure(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """Simulated audit failure rolls back the INSERT/UPDATE (fix #7 + transaction safety)."""
        item = await _item_by_code(client, auth_token, hq_branch_id, "SILOS")
        iid = item["pay_item_id"]
        before_active = item["is_active"]

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure")

        with patch.object(settings_service, "_write_settings_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure"):
                await client.patch(
                    f"/settings/branches/{hq_branch_id}/pay-items/{iid}",
                    json={"is_active": not before_active},
                    headers=auth(auth_token),
                )

        after = await _item_by_code(client, auth_token, hq_branch_id, "SILOS")
        assert after["is_active"] == before_active


# ===========================================================================
# TestPayItemConfigHistory  (fix #8)
# ===========================================================================

class TestPayItemConfigHistory:

    async def test_requires_auth(
        self, client: httpx.AsyncClient, hq_branch_id: int
    ):
        resp = await client.get(f"/settings/branches/{hq_branch_id}/pay-items/1/history")
        assert resp.status_code == 401

    async def test_branch_user_can_read_own_branch(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items/1/history",
            headers=auth(branch_user_token),
        )
        # May be 200 (empty list) or 200 with history rows
        assert resp.status_code == 200

    async def test_history_grows_with_each_version(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """
        Three PATCHes with increasing effective dates must produce three history rows.
        The first PATCH sets today's config; the next two schedule future versions
        (each closing the previous open row).
        """
        item = await _item_by_code(client, auth_token, hq_branch_id, "PTO_STATUS")
        iid = item["pay_item_id"]
        today = date.today()

        # Version 1 — today
        await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{iid}",
            json={"is_active": True, "notes": "v1"},
            headers=auth(auth_token),
        )

        # Version 2 — 10 days from now (closes v1 in 9 days)
        v2_date = str(today + timedelta(days=10))
        await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{iid}",
            json={"is_active": False, "notes": "v2", "effective_from": v2_date},
            headers=auth(auth_token),
        )

        # Version 3 — 20 days from now (closes v2 in 9 days)
        v3_date = str(today + timedelta(days=20))
        await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{iid}",
            json={"is_active": True, "notes": "v3", "effective_from": v3_date},
            headers=auth(auth_token),
        )

        history = (
            await client.get(
                f"/settings/branches/{hq_branch_id}/pay-items/{iid}/history",
                headers=auth(auth_token),
            )
        ).json()

        # Must have 3 rows; the open row is the newest, ordered DESC
        assert len(history) >= 3
        assert history[0]["effective_to"] is None  # newest is still open

        notes_in_history = {r["notes"] for r in history}
        assert "v1" in notes_in_history
        assert "v2" in notes_in_history
        assert "v3" in notes_in_history

    async def test_history_schema(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        item = await _item_by_code(client, auth_token, hq_branch_id, "HOURS")
        iid = item["pay_item_id"]

        # Ensure there is at least one history row
        await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{iid}",
            json={"is_active": True},
            headers=auth(auth_token),
        )

        history = (
            await client.get(
                f"/settings/branches/{hq_branch_id}/pay-items/{iid}/history",
                headers=auth(auth_token),
            )
        ).json()

        assert len(history) >= 1
        row = history[0]
        for field in ("config_id", "is_active", "effective_from", "created_at_utc"):
            assert field in row, f"Missing field: {field}"


# ===========================================================================
# TestMissingPayItemConfigs  (fix #5 branch validation)
# ===========================================================================

class TestMissingPayItemConfigs:

    async def test_requires_auth(
        self, client: httpx.AsyncClient, hq_branch_id: int
    ):
        resp = await client.get(f"/settings/branches/{hq_branch_id}/pay-items/missing")
        assert resp.status_code == 401

    async def test_branch_user_denied(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
        hq_branch_id: int,
    ):
        resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items/missing",
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403

    async def test_unknown_branch_returns_404(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/settings/branches/9999999/pay-items/missing",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_fresh_branch_has_all_items_missing(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        br = await client.post(
            "/settings/branches",
            json={"branch_name": "Missing Items Test Branch 2", "branch_code": "MITB2"},
            headers=auth(auth_token),
        )
        assert br.status_code == 201
        bid = br.json()["branch_id"]

        resp = await client.get(
            f"/settings/branches/{bid}/pay-items/missing",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        missing = resp.json()
        assert len(missing) >= 11
        assert "HOURS" in missing

    async def test_configured_item_not_in_missing(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        item = await _item_by_code(client, auth_token, hq_branch_id, "ADJUSTMENT")
        await client.patch(
            f"/settings/branches/{hq_branch_id}/pay-items/{item['pay_item_id']}",
            json={"is_active": True},
            headers=auth(auth_token),
        )

        missing = (
            await client.get(
                f"/settings/branches/{hq_branch_id}/pay-items/missing",
                headers=auth(auth_token),
            )
        ).json()
        assert "ADJUSTMENT" not in missing


# ===========================================================================
# TestPayItemDeleteSafety — Phase 3D fix: DriverRates must prevent physical delete
# ===========================================================================

class TestPayItemDeleteSafety:
    """
    Verify that _compute_usage counts DriverRates, and that delete_custom_pay_item
    retires (rather than physically deletes) items with associated driver rates.

    Uses direct_db for inserting DriverRates to bypass the branch-activation
    check in create_rate (which would require complex branch setup for each test).
    The DriverRate record is inserted via payitemratetypemap → driverrates join,
    matching exactly the query added to _compute_usage.
    """

    # -------------------------------------------------------------------------
    # Helper: create a custom Daily/PerUnit pay item
    # -------------------------------------------------------------------------

    async def _create_custom_item(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        *,
        name: str = "Delete Safety Test Item",
        code: str | None = None,
    ) -> dict:
        payload = {
            "pay_item_name": name,
            "item_scope": "Daily",
            "rate_behavior": "PerUnit",
            "category": "Wage",
            "unit": "Stop",
        }
        if code:
            payload["pay_item_code"] = code
        resp = await client.post(
            "/settings/pay-items",
            json=payload,
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    # -------------------------------------------------------------------------
    # Helper: assign a rate type to the custom item
    # -------------------------------------------------------------------------

    async def _assign_rate_type(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        item_id: int,
        rate_type_id: int,
    ) -> int:
        """POST /settings/pay-items/{id}/rate-type-map. Returns the mapping ID."""
        resp = await client.post(
            f"/settings/pay-items/{item_id}/rate-type-map",
            json={"rate_type_id": rate_type_id},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["pay_item_rate_type_map_id"]

    # -------------------------------------------------------------------------
    # Test 1: item with no usage is physically deleted
    # -------------------------------------------------------------------------

    async def test_custom_item_with_no_usage_physically_deletes(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """A custom item with no draft lines, final lines, or driver rates → physical delete."""
        item = await self._create_custom_item(
            client, auth_token, name="No-Usage Delete Test"
        )
        item_id = item["pay_item_id"]

        resp = await client.delete(
            f"/settings/pay-items/{item_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deletion_type"] == "physical", (
            f"Expected physical delete but got: {body['deletion_type']}"
        )
        assert body["pay_item_id"] is None  # physically removed

        # Subsequent GET must return 404
        get_resp = await client.get(
            f"/settings/pay-items/{item_id}",
            headers=auth(auth_token),
        )
        assert get_resp.status_code == 404

    # -------------------------------------------------------------------------
    # Test 2: item WITH driver rates is retired, not physically deleted
    # -------------------------------------------------------------------------

    async def test_custom_item_with_driver_rates_retires_not_deletes(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
        paytest_branch_id: int,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        """
        The key correctness test for Phase 3D:

        Given a custom pay item with a PayItemRateTypeMap entry and at least one
        DriverRate row that references the mapped rate type, DELETE must retire
        (not physically delete) the item — preserving the DriverRate record.
        """
        from sqlalchemy import text as _text

        item = await self._create_custom_item(
            client, auth_token, name="Has Driver Rates Test"
        )
        item_id = item["pay_item_id"]

        # Map this custom item to the HOURLY rate type
        await self._assign_rate_type(client, auth_token, item_id, paytest_rate_type_id)

        # Insert a DriverRate directly (bypasses branch-activation check which would
        # require complex branch/item activation setup not relevant to this test).
        # The rate is inserted in PendingApproval status — still counts for usage.
        insert_result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverrates
                    (companyid, branchid, driverid, ratetypeid,
                     amount, effectivefrom, status, createdbyuserid)
                SELECT
                    d.companyid, d.branchid, d.driverid, :rtid,
                    15.00, CURRENT_DATE, 'PendingApproval', NULL
                FROM core.drivers d
                WHERE d.driverid = :did
                RETURNING driverrateid
            """),
            {"rtid": paytest_rate_type_id, "did": paytest_driver_id},
        )
        driver_rate_id = insert_result.scalar_one()

        try:
            # DELETE the pay item — must retire because DriverRates exist
            del_resp = await client.delete(
                f"/settings/pay-items/{item_id}",
                headers=auth(auth_token),
            )
            assert del_resp.status_code == 200, del_resp.text
            body = del_resp.json()
            assert body["deletion_type"] == "retired", (
                f"Expected retire but got physical delete. "
                f"DriverRates were not counted by _compute_usage. Body: {body}"
            )
            assert body["pay_item_id"] == item_id  # still exists in DB

            # Subsequent GET must return the item with status Retired
            get_resp = await client.get(
                f"/settings/pay-items/{item_id}",
                headers=auth(auth_token),
            )
            assert get_resp.status_code == 200, get_resp.text
            assert get_resp.json()["status"] == "Retired"

            # The DriverRate record must still exist (not orphaned)
            rate_check = await direct_db.execute(
                _text("SELECT driverrateid FROM payroll.driverrates WHERE driverrateid = :id"),
                {"id": driver_rate_id},
            )
            assert rate_check.fetchone() is not None, (
                "DriverRate was deleted during pay item retirement — data loss!"
            )

        finally:
            # Cleanup: remove the DriverRate we inserted
            await direct_db.execute(
                _text("DELETE FROM payroll.driverrates WHERE driverrateid = :id"),
                {"id": driver_rate_id},
            )

    # -------------------------------------------------------------------------
    # Test 3: usage endpoint reports driver_rates_count correctly
    # -------------------------------------------------------------------------

    async def test_usage_endpoint_reports_driver_rates_count(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        """
        GET /settings/pay-items/{id}/usage should report driver_rates_count > 0
        when DriverRates exist for a rate type mapped to this item.
        """
        from sqlalchemy import text as _text

        item = await self._create_custom_item(
            client, auth_token, name="Usage Count Test"
        )
        item_id = item["pay_item_id"]

        # Check usage before linking any rate type
        usage_before = (
            await client.get(
                f"/settings/pay-items/{item_id}/usage",
                headers=auth(auth_token),
            )
        ).json()
        assert usage_before["driver_rates_count"] == 0
        assert usage_before["can_physical_delete"] is True
        assert usage_before["deletion_would_retire"] is False

        # Map item → rate type
        await self._assign_rate_type(client, auth_token, item_id, paytest_rate_type_id)

        # Insert a DriverRate
        insert_result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverrates
                    (companyid, branchid, driverid, ratetypeid,
                     amount, effectivefrom, status, createdbyuserid)
                SELECT
                    d.companyid, d.branchid, d.driverid, :rtid,
                    20.00, CURRENT_DATE - INTERVAL '1 day', 'PendingApproval', NULL
                FROM core.drivers d
                WHERE d.driverid = :did
                RETURNING driverrateid
            """),
            {"rtid": paytest_rate_type_id, "did": paytest_driver_id},
        )
        driver_rate_id = insert_result.scalar_one()

        try:
            usage_after = (
                await client.get(
                    f"/settings/pay-items/{item_id}/usage",
                    headers=auth(auth_token),
                )
            ).json()
            assert usage_after["driver_rates_count"] >= 1
            assert usage_after["can_physical_delete"] is False
            assert usage_after["deletion_would_retire"] is True

        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.driverrates WHERE driverrateid = :id"),
                {"id": driver_rate_id},
            )
            # Physical delete the pay item (now that driver rate is removed)
            await client.delete(
                f"/settings/pay-items/{item_id}",
                headers=auth(auth_token),
            )

    # -------------------------------------------------------------------------
    # Test 4: system item delete is blocked (422)
    # -------------------------------------------------------------------------

    async def test_system_item_delete_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        hq_branch_id: int,
    ):
        """DELETE on a system pay item must return 422."""
        # Get a system item
        items_resp = await client.get(
            f"/settings/branches/{hq_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert items_resp.status_code == 200
        system_items = [i for i in items_resp.json() if i.get("is_system_standard")]
        assert system_items, "No system items found — check seed data"
        system_id = system_items[0]["pay_item_id"]

        resp = await client.delete(
            f"/settings/pay-items/{system_id}",
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, (
            f"Expected 422 for system item delete but got {resp.status_code}"
        )

    # -------------------------------------------------------------------------
    # Test 5: DriverRates are preserved after pay item retirement
    # -------------------------------------------------------------------------

    async def test_driver_rates_preserved_after_pay_item_retirement(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
        paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        """
        Historical preservation: after a pay item is retired, any DriverRate rows
        linked via PayItemRateTypeMap must remain intact in the database.
        Physical delete of the item must NOT cascade to DriverRates.
        """
        from sqlalchemy import text as _text

        item = await self._create_custom_item(
            client, auth_token, name="Preservation Test"
        )
        item_id = item["pay_item_id"]
        await self._assign_rate_type(client, auth_token, item_id, paytest_rate_type_id)

        # Insert two DriverRate rows
        rate_ids = []
        for amount in ["11.00", "12.50"]:
            r = await direct_db.execute(
                _text("""
                    INSERT INTO payroll.driverrates
                        (companyid, branchid, driverid, ratetypeid,
                         amount, effectivefrom, status, createdbyuserid)
                    SELECT
                        d.companyid, d.branchid, d.driverid, :rtid,
                        :amt, CURRENT_DATE - INTERVAL '5 days', 'PendingApproval', NULL
                    FROM core.drivers d
                    WHERE d.driverid = :did
                    RETURNING driverrateid
                """),
                {"rtid": paytest_rate_type_id, "did": paytest_driver_id, "amt": amount},
            )
            rate_ids.append(r.scalar_one())

        try:
            # Retire the item via delete
            del_resp = await client.delete(
                f"/settings/pay-items/{item_id}",
                headers=auth(auth_token),
            )
            assert del_resp.status_code == 200
            assert del_resp.json()["deletion_type"] == "retired"

            # Both DriverRate rows must still exist
            for rid in rate_ids:
                row = await direct_db.execute(
                    _text("SELECT driverrateid FROM payroll.driverrates WHERE driverrateid = :id"),
                    {"id": rid},
                )
                assert row.fetchone() is not None, (
                    f"DriverRate {rid} was orphaned after pay item retirement!"
                )

        finally:
            for rid in rate_ids:
                await direct_db.execute(
                    _text("DELETE FROM payroll.driverrates WHERE driverrateid = :id"),
                    {"id": rid},
                )
