"""
Tests for M15: Driver Pay Rules (Minimum / Maximum Pay).

Covers:
  - DriverPayRules CRUD (create, read, end, void, notes-only patch)
  - Validation: amount > 0, overlapping ranges, ended rules require EffectiveTo
  - Lifecycle: Active → Ended, Active → Voided, Ended → Voided (if no finalized)
  - Retroactive guards: end date cannot precede latest finalized period
  - Void guard: blocked when finalized period exists in range
  - Finalization integration: SYS_MIN_TOPUP and SYS_MAX_CAP
  - Finalization: earned from PayrollFinalLines (step 3b logic)
  - Min > Max blocks finalization
  - Ended rules still apply historically at finalization
  - Voided rules do not apply
  - As-of-date: uses period.start_date not CURRENT_DATE
  - SYS_MIN_TOPUP / SYS_MAX_CAP blocked from manual daily and period-pay entry
  - Audit rollback tests for all write paths
"""
import pytest
import pytest_asyncio
import httpx
from decimal import Decimal
from sqlalchemy import text as _text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient, token: str, branch_id: int
) -> None:
    headers = auth(token)
    for s in ("Draft", "Open", "InReview", "Approved"):
        resp = await client.get(
            "/payroll/periods", params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        if resp.status_code != 200:
            continue
        for p in resp.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"}, headers=headers,
            )


async def _open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str,
    end: str,
) -> dict:
    await _cancel_active_periods(client, token, branch_id)
    p_resp = await client.post(
        "/payroll/periods",
        json={"branch_id": branch_id, "period_type": "Week",
              "start_date": start, "end_date": end},
        headers=auth(token),
    )
    assert p_resp.status_code == 201, f"period create failed: {p_resp.text}"
    period = p_resp.json()
    open_resp = await client.patch(
        f"/payroll/periods/{period['payroll_period_id']}/status",
        json={"status": "Open"}, headers=auth(token),
    )
    assert open_resp.status_code == 200
    return period


async def _finalize_period(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
) -> dict:
    """Move an Open period to Locked (Open→InReview→Approved via review→Finalize)."""
    headers = auth(token)
    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"}, headers=headers,
    )
    assert r.status_code == 200, f"Status transition to InReview failed: {r.text}"
    # Approve via review flow
    review_resp = await client.get("/review/items", headers=headers)
    assert review_resp.status_code == 200
    review_item = next(
        (i for i in review_resp.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert review_item is not None, f"No pending review item for period {period_id}"
    decide = await client.post(
        f"/review/items/{review_item['review_item_id']}/decide",
        headers=headers, json={"decision": "Approved"},
    )
    assert decide.status_code == 200, f"Approval failed: {decide.text}"
    fin = await client.post(
        f"/payroll/periods/{period_id}/finalize", headers=headers
    )
    assert fin.status_code == 200, f"Finalize failed: {fin.text}"
    return fin.json()


async def _void_all_rules_for_driver(
    client: httpx.AsyncClient, token: str, driver_id: int
) -> None:
    """Helper: void all Active/Ended rules for a driver (cleanup)."""
    resp = await client.get(
        f"/payroll/drivers/{driver_id}/pay-rules", headers=auth(token)
    )
    if resp.status_code != 200:
        return
    for rule in resp.json():
        if rule["status"] in ("Active", "Ended"):
            await client.post(
                f"/payroll/driver-pay-rules/{rule['driver_pay_rule_id']}/void",
                headers=auth(token),
            )


# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def m15_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Create a dedicated driver for M15 tests."""
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":    paytest_branch_id,
            "full_name":    "M15 Driver",
            "driver_code":  "M15DRV",
            "status":       "Active",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"m15_driver_id create failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture(scope="session")
async def m15_driver2_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Second driver for multi-driver tests."""
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":    paytest_branch_id,
            "full_name":    "M15B Driver",
            "driver_code":  "M15DRV2",
            "status":       "Active",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"m15_driver2_id create failed: {resp.text}"
    return resp.json()["driver_id"]


@pytest_asyncio.fixture(scope="session")
async def m15_bonus_activated(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> None:
    """Activate system BONUS item on PAYTEST branch."""
    items_resp = await session_client.get(
        f"/settings/branches/{paytest_branch_id}/pay-items",
        headers=auth(auth_token),
    )
    for item in items_resp.json():
        if item["pay_item_code"] == "BONUS":
            if not item.get("is_active", False):
                await session_client.patch(
                    f"/settings/branches/{paytest_branch_id}/pay-items/{item['pay_item_id']}",
                    json={"is_active": True},
                    headers=auth(auth_token),
                )
            return
    all_resp = await session_client.get("/settings/pay-items", headers=auth(auth_token))
    for item in all_resp.json():
        if item.get("pay_item_code") == "BONUS":
            await session_client.patch(
                f"/settings/branches/{paytest_branch_id}/pay-items/{item['pay_item_id']}",
                json={"is_active": True},
                headers=auth(auth_token),
            )
            return


# ---------------------------------------------------------------------------
# Function fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def m15_open_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> dict:
    """Open a fresh period for M15 tests (2036-01-01 to 2036-01-07)."""
    return await _open_period(
        session_client, auth_token, paytest_branch_id,
        start="2036-01-01", end="2036-01-07",
    )


# ===========================================================================
# TestDriverPayRulesCreate
# ===========================================================================

class TestDriverPayRulesCreate:
    """Happy path + validation for POST /payroll/driver-pay-rules."""

    async def test_create_minimum_pay_rule(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """POST MinimumPay rule → 201, Status=Active."""
        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2036-02-01",
                "effective_to":   "2036-02-28",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        rule = resp.json()
        assert rule["rule_type"] == "MinimumPay"
        assert rule["status"] == "Active"
        assert Decimal(rule["amount"]) == Decimal("500.00")
        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule['driver_pay_rule_id']}/void",
            headers=auth(auth_token),
        )

    async def test_create_maximum_pay_rule(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """POST MaximumPay rule → 201."""
        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MaximumPay",
                "amount":         "2000.00",
                "effective_from": "2036-03-01",
                "effective_to":   "2036-03-31",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        rule = resp.json()
        assert rule["rule_type"] == "MaximumPay"
        assert rule["status"] == "Active"
        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule['driver_pay_rule_id']}/void",
            headers=auth(auth_token),
        )

    async def test_create_amount_zero_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """amount=0 → 422."""
        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "0",
                "effective_from": "2036-04-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_amount_negative_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """amount<0 → 422."""
        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "-100.00",
                "effective_from": "2036-04-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_effective_to_before_from_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """effective_to < effective_from → 422."""
        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2036-05-15",
                "effective_to":   "2036-05-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_create_overlapping_active_rule_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """Creating a second MinimumPay rule that overlaps an existing Active one → 422."""
        # Create first rule
        r1 = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2036-06-01",
                "effective_to":   "2036-06-30",
            },
            headers=auth(auth_token),
        )
        assert r1.status_code == 201
        rule1_id = r1.json()["driver_pay_rule_id"]

        # Create overlapping rule
        r2 = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "600.00",
                "effective_from": "2036-06-15",
                "effective_to":   "2036-07-15",
            },
            headers=auth(auth_token),
        )
        assert r2.status_code == 422
        assert "overlap" in r2.text.lower() or "overlaps" in r2.text.lower()

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule1_id}/void",
            headers=auth(auth_token),
        )

    async def test_create_overlapping_ended_rule_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """Ended rule blocks an overlapping new rule → 422."""
        r1 = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2036-07-01",
                "effective_to":   "2036-07-31",
            },
            headers=auth(auth_token),
        )
        assert r1.status_code == 201
        rule1_id = r1.json()["driver_pay_rule_id"]
        # End the rule
        end_r = await session_client.post(
            f"/payroll/driver-pay-rules/{rule1_id}/end",
            json={"effective_to": "2036-07-20"},
            headers=auth(auth_token),
        )
        assert end_r.status_code == 200

        # Try to create overlapping rule
        r2 = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "600.00",
                "effective_from": "2036-07-10",
                "effective_to":   "2036-07-25",
            },
            headers=auth(auth_token),
        )
        assert r2.status_code == 422

        # Cleanup: void the ended rule
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule1_id}/void",
            headers=auth(auth_token),
        )

    async def test_create_contiguous_ranges_allowed(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """Two contiguous (non-overlapping) rules are both accepted → 201 each."""
        r1 = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2036-08-01",
                "effective_to":   "2036-08-31",
            },
            headers=auth(auth_token),
        )
        assert r1.status_code == 201, r1.text
        r1_id = r1.json()["driver_pay_rule_id"]
        # End first rule
        await session_client.post(
            f"/payroll/driver-pay-rules/{r1_id}/end",
            json={"effective_to": "2036-08-31"},
            headers=auth(auth_token),
        )

        r2 = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "550.00",
                "effective_from": "2036-09-01",
            },
            headers=auth(auth_token),
        )
        assert r2.status_code == 201, r2.text
        r2_id = r2.json()["driver_pay_rule_id"]

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{r1_id}/void",
            headers=auth(auth_token),
        )
        await session_client.post(
            f"/payroll/driver-pay-rules/{r2_id}/void",
            headers=auth(auth_token),
        )

    async def test_create_unknown_driver_404(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Unknown driver → 404."""
        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      999999,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2036-10-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_create_no_setup_manage_permission_403(
        self,
        session_client: httpx.AsyncClient,
        branch_user_token: str,
        m15_driver_id: int,
    ):
        """User without setup.manage → 403."""
        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2036-11-01",
            },
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403


# ===========================================================================
# TestDriverPayRulesRead
# ===========================================================================

class TestDriverPayRulesRead:
    """GET /payroll/drivers/{id}/pay-rules and /payroll/driver-pay-rules/{id}."""

    async def test_list_all_rules_for_driver(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """List returns all rules for driver."""
        # Create two rules
        r1 = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "400.00",
                "effective_from": "2037-01-01",
                "effective_to":   "2037-01-31",
            },
            headers=auth(auth_token),
        )
        assert r1.status_code == 201
        r2 = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MaximumPay",
                "amount":         "2000.00",
                "effective_from": "2037-01-01",
                "effective_to":   "2037-01-31",
            },
            headers=auth(auth_token),
        )
        assert r2.status_code == 201

        list_resp = await session_client.get(
            f"/payroll/drivers/{m15_driver_id}/pay-rules",
            headers=auth(auth_token),
        )
        assert list_resp.status_code == 200
        rules = list_resp.json()
        rule_ids = {r["driver_pay_rule_id"] for r in rules}
        assert r1.json()["driver_pay_rule_id"] in rule_ids
        assert r2.json()["driver_pay_rule_id"] in rule_ids

        # Cleanup
        for rule in [r1.json(), r2.json()]:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule['driver_pay_rule_id']}/void",
                headers=auth(auth_token),
            )

    async def test_list_filtered_by_type(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """Filter by rule_type returns only matching rules."""
        r1 = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "400.00",
                "effective_from": "2037-02-01",
                "effective_to":   "2037-02-28",
            },
            headers=auth(auth_token),
        )
        assert r1.status_code == 201
        r2 = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MaximumPay",
                "amount":         "2000.00",
                "effective_from": "2037-02-01",
                "effective_to":   "2037-02-28",
            },
            headers=auth(auth_token),
        )
        assert r2.status_code == 201

        list_resp = await session_client.get(
            f"/payroll/drivers/{m15_driver_id}/pay-rules",
            params={"rule_type": "MinimumPay"},
            headers=auth(auth_token),
        )
        assert list_resp.status_code == 200
        types = {r["rule_type"] for r in list_resp.json()}
        assert types == {"MinimumPay"} or all(t == "MinimumPay" for t in types)

        # Cleanup
        for rule in [r1.json(), r2.json()]:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule['driver_pay_rule_id']}/void",
                headers=auth(auth_token),
            )

    async def test_get_single_rule(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """GET /driver-pay-rules/{id} returns the rule."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "450.00",
                "effective_from": "2037-03-01",
                "effective_to":   "2037-03-31",
                "notes":          "test note",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        get_resp = await session_client.get(
            f"/payroll/driver-pay-rules/{rule_id}", headers=auth(auth_token)
        )
        assert get_resp.status_code == 200
        rule = get_resp.json()
        assert rule["driver_pay_rule_id"] == rule_id
        assert Decimal(rule["amount"]) == Decimal("450.00")
        assert rule["notes"] == "test note"

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    async def test_get_nonexistent_rule_404(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
    ):
        """GET nonexistent rule → 404."""
        resp = await session_client.get(
            "/payroll/driver-pay-rules/999999", headers=auth(auth_token)
        )
        assert resp.status_code == 404


# ===========================================================================
# TestDriverPayRulesEnd
# ===========================================================================

class TestDriverPayRulesEnd:
    """POST /payroll/driver-pay-rules/{id}/end."""

    async def test_end_active_open_ended_rule(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """End an open-ended Active rule → Status=Ended, EffectiveTo set."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2037-04-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        end_resp = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2037-04-30"},
            headers=auth(auth_token),
        )
        assert end_resp.status_code == 200
        rule = end_resp.json()
        assert rule["status"] == "Ended"
        assert rule["effective_to"] == "2037-04-30"

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    async def test_end_active_rule_with_explicit_effective_to(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """End rule with a specific EffectiveTo date → 200."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MaximumPay",
                "amount":         "2000.00",
                "effective_from": "2037-05-01",
                "effective_to":   "2037-05-31",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        end_resp = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2037-05-15"},
            headers=auth(auth_token),
        )
        assert end_resp.status_code == 200
        assert end_resp.json()["status"] == "Ended"
        assert end_resp.json()["effective_to"] == "2037-05-15"

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    async def test_end_already_ended_rule_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """Ending an already-Ended rule → 422."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2037-06-01",
                "effective_to":   "2037-06-30",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]
        # End it first
        e1 = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2037-06-15"},
            headers=auth(auth_token),
        )
        assert e1.status_code == 200
        # Try to end again
        e2 = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2037-06-20"},
            headers=auth(auth_token),
        )
        assert e2.status_code == 422
        assert "active" in e2.text.lower()

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    async def test_end_voided_rule_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """Ending a Voided rule → 422."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2037-07-01",
                "effective_to":   "2037-07-31",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]
        # Void it
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
        # Try to end voided
        end_r = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2037-07-15"},
            headers=auth(auth_token),
        )
        assert end_r.status_code == 422

    async def test_end_before_finalized_period_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Cannot end a rule before the latest finalized period start."""
        # Create rule covering 2037-08-01 to 2037-08-31
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "1.00",  # very low so no top-up needed
                "effective_from": "2037-08-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        # Finalize a period starting 2037-08-14 (within rule range)
        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2037-08-14", end="2037-08-20",
        )
        pid = period["payroll_period_id"]
        # Add a line so finalization has something to process
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        # Try to end rule before 2037-08-14 → must be blocked
        end_r = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2037-08-01"},
            headers=auth(auth_token),
        )
        assert end_r.status_code == 422
        assert "finalized" in end_r.text.lower() or "relied" in end_r.text.lower()

        # Cleanup rule (cannot void since finalized period exists)
        # Just end it at a valid date
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2037-08-31"},
            headers=auth(auth_token),
        )

    async def test_end_on_or_after_finalized_period_allowed(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Ending a rule on or after the latest finalized period start → 200."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MaximumPay",
                "amount":         "99999.00",
                "effective_from": "2037-09-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        # Finalize a period starting 2037-09-10
        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2037-09-10", end="2037-09-16",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "50.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        # End the rule on 2037-09-10 (the finalized period start) → must succeed
        end_r = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2037-09-10"},
            headers=auth(auth_token),
        )
        assert end_r.status_code == 200


# ===========================================================================
# TestDriverPayRulesVoid
# ===========================================================================

class TestDriverPayRulesVoid:
    """POST /payroll/driver-pay-rules/{id}/void."""

    async def test_void_rule_no_finalized_periods(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """Void rule with no finalized periods → 200, Status=Voided."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2038-01-01",
                "effective_to":   "2038-01-31",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        void_r = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
        assert void_r.status_code == 200
        assert void_r.json()["status"] == "Voided"

    async def test_void_rule_with_finalized_period_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Void rule that governed a finalized period → 422."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "1.00",
                "effective_from": "2038-02-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2038-02-01", end="2038-02-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "50.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        void_r = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
        assert void_r.status_code == 422
        assert "finalized" in void_r.text.lower() or "governed" in void_r.text.lower()

        # Cleanup: end the rule so it doesn't block subsequent tests (can't void — has finalized period)
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2038-02-28"},
            headers=auth(auth_token),
        )

    async def test_void_already_voided_rule_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """Voiding an already-Voided rule → 422."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2038-03-01",
                "effective_to":   "2038-03-31",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]
        # First void
        v1 = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
        assert v1.status_code == 200
        # Second void
        v2 = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
        assert v2.status_code == 422

    async def test_void_ended_rule_no_finalized_periods(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """Void an Ended rule with no finalized periods → 200."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2038-04-01",
                "effective_to":   "2038-04-30",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]
        # End it
        end_r = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2038-04-15"},
            headers=auth(auth_token),
        )
        assert end_r.status_code == 200
        # Void it
        void_r = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
        assert void_r.status_code == 200
        assert void_r.json()["status"] == "Voided"

    async def test_void_ended_rule_with_finalized_period_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Void an Ended rule that governed a finalized period → 422."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "1.00",
                "effective_from": "2038-05-01",
                "effective_to":   "2038-05-31",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2038-05-01", end="2038-05-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "50.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        # End the rule
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2038-05-31"},
            headers=auth(auth_token),
        )

        # Try to void → blocked
        void_r = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
        assert void_r.status_code == 422

    async def test_void_requires_setup_manage(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        m15_driver_id: int,
    ):
        """User without setup.manage cannot void → 403."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2038-06-01",
                "effective_to":   "2038-06-30",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        void_r = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void",
            headers=auth(branch_user_token),
        )
        assert void_r.status_code == 403

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )


# ===========================================================================
# TestDriverPayRulesNotesUpdate
# ===========================================================================

class TestDriverPayRulesNotesUpdate:
    """PATCH /payroll/driver-pay-rules/{id}."""

    async def test_update_notes(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """PATCH notes → 200, notes updated."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2039-01-01",
                "effective_to":   "2039-01-31",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        patch_r = await session_client.patch(
            f"/payroll/driver-pay-rules/{rule_id}",
            json={"notes": "Updated note"},
            headers=auth(auth_token),
        )
        assert patch_r.status_code == 200
        assert patch_r.json()["notes"] == "Updated note"
        # Amount and dates unchanged
        assert Decimal(patch_r.json()["amount"]) == Decimal("500.00")
        assert patch_r.json()["effective_from"] == "2039-01-01"

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    async def test_notes_only_not_amount_or_dates(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """PATCH body only accepts notes — schema rejects extra fields gracefully."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2039-02-01",
                "effective_to":   "2039-02-28",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        # PATCH with notes only
        patch_r = await session_client.patch(
            f"/payroll/driver-pay-rules/{rule_id}",
            json={"notes": "Only notes"},
            headers=auth(auth_token),
        )
        assert patch_r.status_code == 200
        assert Decimal(patch_r.json()["amount"]) == Decimal("500.00")

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    async def test_notes_update_voided_rule_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
    ):
        """PATCH notes on a Voided rule → 422."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2039-03-01",
                "effective_to":   "2039-03-31",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
        patch_r = await session_client.patch(
            f"/payroll/driver-pay-rules/{rule_id}",
            json={"notes": "Cannot update voided"},
            headers=auth(auth_token),
        )
        assert patch_r.status_code == 422

    async def test_notes_update_requires_setup_manage(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        m15_driver_id: int,
    ):
        """User without setup.manage cannot update notes → 403."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2039-04-01",
                "effective_to":   "2039-04-30",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        patch_r = await session_client.patch(
            f"/payroll/driver-pay-rules/{rule_id}",
            json={"notes": "No permission"},
            headers=auth(branch_user_token),
        )
        assert patch_r.status_code == 403

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )


# ===========================================================================
# TestMinimumPayFinalization
# ===========================================================================

class TestMinimumPayFinalization:
    """SYS_MIN_TOPUP in finalization."""

    async def test_earned_below_minimum_produces_topup(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Earned < minimum → SYS_MIN_TOPUP in final lines with correct positive amount."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2040-01-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2040-01-01", end="2040-01-07",
        )
        pid = period["payroll_period_id"]
        # Add line worth 300 (< 500 minimum)
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "300.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        final_lines = final_resp.json()
        topup_lines = [fl for fl in final_lines if fl["line_type"] == "SYS_MIN_TOPUP"]
        assert len(topup_lines) == 1, f"Expected 1 SYS_MIN_TOPUP, got: {topup_lines}"
        assert Decimal(topup_lines[0]["final_amount"]) == Decimal("200.00")  # 500 - 300

        # Cleanup rule
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2040-01-07"},
            headers=auth(auth_token),
        )

    async def test_earned_exactly_minimum_no_topup(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Earned == minimum → no SYS_MIN_TOPUP."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "300.00",
                "effective_from": "2040-02-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2040-02-01", end="2040-02-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "300.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        topup_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MIN_TOPUP"]
        assert len(topup_lines) == 0, "No top-up should be created when earned == minimum"

        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2040-02-07"},
            headers=auth(auth_token),
        )

    async def test_earned_above_minimum_no_topup(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Earned > minimum → no SYS_MIN_TOPUP."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "200.00",
                "effective_from": "2040-03-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2040-03-01", end="2040-03-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "500.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        topup_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MIN_TOPUP"]
        assert len(topup_lines) == 0

        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2040-03-07"},
            headers=auth(auth_token),
        )

    async def test_no_minimum_rule_no_topup(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """No MinimumPay rule → no SYS_MIN_TOPUP."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2040-04-01", end="2040-04-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        topup_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MIN_TOPUP"]
        assert len(topup_lines) == 0

    async def test_sys_min_topup_metadata(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """SYS_MIN_TOPUP has DraftLineID=None, LineScope='Period', SourceType='System'."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "1000.00",
                "effective_from": "2040-05-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2040-05-01", end="2040-05-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        topup_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MIN_TOPUP"]
        assert len(topup_lines) == 1
        topup = topup_lines[0]
        assert topup["draft_line_id"] is None
        assert topup["line_scope"] == "Period"
        assert topup["source_type"] == "System"

        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2040-05-07"},
            headers=auth(auth_token),
        )


# ===========================================================================
# TestMaximumPayFinalization
# ===========================================================================

class TestMaximumPayFinalization:
    """SYS_MAX_CAP in finalization."""

    async def test_earned_above_maximum_produces_cap(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Earned > maximum → SYS_MAX_CAP in final lines with correct negative amount."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MaximumPay",
                "amount":         "400.00",
                "effective_from": "2041-01-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2041-01-01", end="2041-01-07",
        )
        pid = period["payroll_period_id"]
        # Earn 600 (> 400 max)
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "600.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        cap_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MAX_CAP"]
        assert len(cap_lines) == 1, f"Expected 1 SYS_MAX_CAP, got: {cap_lines}"
        # SYS_MAX_CAP = max - earned = 400 - 600 = -200
        assert Decimal(cap_lines[0]["final_amount"]) == Decimal("-200.00")

        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2041-01-07"},
            headers=auth(auth_token),
        )

    async def test_earned_exactly_maximum_no_cap(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Earned == maximum → no SYS_MAX_CAP."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MaximumPay",
                "amount":         "500.00",
                "effective_from": "2041-02-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2041-02-01", end="2041-02-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "500.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        cap_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MAX_CAP"]
        assert len(cap_lines) == 0, "No cap when earned == maximum"

        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2041-02-07"},
            headers=auth(auth_token),
        )

    async def test_earned_below_maximum_no_cap(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Earned < maximum → no SYS_MAX_CAP."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MaximumPay",
                "amount":         "1000.00",
                "effective_from": "2041-03-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2041-03-01", end="2041-03-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "200.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        cap_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MAX_CAP"]
        assert len(cap_lines) == 0

        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2041-03-07"},
            headers=auth(auth_token),
        )

    async def test_no_maximum_rule_no_cap(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """No MaximumPay rule → no SYS_MAX_CAP."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2041-04-01", end="2041-04-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "9999.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        cap_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MAX_CAP"]
        assert len(cap_lines) == 0

    async def test_sys_max_cap_metadata(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """SYS_MAX_CAP has DraftLineID=None, LineScope='Period', SourceType='System'."""
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MaximumPay",
                "amount":         "100.00",
                "effective_from": "2041-05-01",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2041-05-01", end="2041-05-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "500.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        cap_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MAX_CAP"]
        assert len(cap_lines) == 1
        cap = cap_lines[0]
        assert cap["draft_line_id"] is None
        assert cap["line_scope"] == "Period"
        assert cap["source_type"] == "System"

        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2041-05-07"},
            headers=auth(auth_token),
        )


# ===========================================================================
# TestMinMaxBothRules
# ===========================================================================

class TestMinMaxBothRules:
    """Both MinimumPay and MaximumPay rules together."""

    async def test_earned_between_min_and_max_no_sys_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Min=300, Max=700, earned=500 → no system lines."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)
        r_min = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MinimumPay",
                  "amount": "300.00", "effective_from": "2042-01-01"},
            headers=auth(auth_token),
        )
        assert r_min.status_code == 201
        r_max = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MaximumPay",
                  "amount": "700.00", "effective_from": "2042-01-01"},
            headers=auth(auth_token),
        )
        assert r_max.status_code == 201

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2042-01-01", end="2042-01-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "500.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        sys_lines = [fl for fl in final_resp.json()
                     if fl["line_type"] in ("SYS_MIN_TOPUP", "SYS_MAX_CAP")]
        assert len(sys_lines) == 0

        # Cleanup
        for rule_id in [r_min.json()["driver_pay_rule_id"], r_max.json()["driver_pay_rule_id"]]:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/end",
                json={"effective_to": "2042-01-07"},
                headers=auth(auth_token),
            )

    async def test_earned_below_min_topup_brings_to_minimum(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Min=400, Max=700, earned=200 → SYS_MIN_TOPUP=200, final total=400."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)
        r_min = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MinimumPay",
                  "amount": "400.00", "effective_from": "2042-02-01"},
            headers=auth(auth_token),
        )
        assert r_min.status_code == 201
        r_max = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MaximumPay",
                  "amount": "700.00", "effective_from": "2042-02-01"},
            headers=auth(auth_token),
        )
        assert r_max.status_code == 201

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2042-02-01", end="2042-02-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "200.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        final_lines = final_resp.json()
        topup_lines = [fl for fl in final_lines if fl["line_type"] == "SYS_MIN_TOPUP"]
        cap_lines = [fl for fl in final_lines if fl["line_type"] == "SYS_MAX_CAP"]
        assert len(topup_lines) == 1
        assert Decimal(topup_lines[0]["final_amount"]) == Decimal("200.00")
        assert len(cap_lines) == 0
        # Total should be 400
        total = sum(Decimal(str(fl["final_amount"])) for fl in final_lines)
        assert total == Decimal("400.00")

        for rule_id in [r_min.json()["driver_pay_rule_id"], r_max.json()["driver_pay_rule_id"]]:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/end",
                json={"effective_to": "2042-02-07"},
                headers=auth(auth_token),
            )

    async def test_earned_above_max_cap_brings_to_maximum(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Min=300, Max=500, earned=800 → SYS_MAX_CAP=-300, final total=500."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)
        r_min = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MinimumPay",
                  "amount": "300.00", "effective_from": "2042-03-01"},
            headers=auth(auth_token),
        )
        assert r_min.status_code == 201
        r_max = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MaximumPay",
                  "amount": "500.00", "effective_from": "2042-03-01"},
            headers=auth(auth_token),
        )
        assert r_max.status_code == 201

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2042-03-01", end="2042-03-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "800.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        final_lines = final_resp.json()
        cap_lines = [fl for fl in final_lines if fl["line_type"] == "SYS_MAX_CAP"]
        topup_lines = [fl for fl in final_lines if fl["line_type"] == "SYS_MIN_TOPUP"]
        assert len(cap_lines) == 1
        assert Decimal(cap_lines[0]["final_amount"]) == Decimal("-300.00")
        assert len(topup_lines) == 0
        total = sum(Decimal(str(fl["final_amount"])) for fl in final_lines)
        assert total == Decimal("500.00")

        for rule_id in [r_min.json()["driver_pay_rule_id"], r_max.json()["driver_pay_rule_id"]]:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/end",
                json={"effective_to": "2042-03-07"},
                headers=auth(auth_token),
            )

    async def test_min_greater_than_max_blocks_finalization(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
        direct_db,
    ):
        """Min > Max at finalization → 422."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)
        r_min = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MinimumPay",
                  "amount": "800.00", "effective_from": "2042-04-01"},
            headers=auth(auth_token),
        )
        assert r_min.status_code == 201
        r_max = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MaximumPay",
                  "amount": "500.00", "effective_from": "2042-04-01"},
            headers=auth(auth_token),
        )
        assert r_max.status_code == 201

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2042-04-01", end="2042-04-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "600.00"},
            headers=auth(auth_token),
        )
        # Advance to Approved via review flow then try finalize
        r_ir = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"}, headers=auth(auth_token),
        )
        assert r_ir.status_code == 200, f"InReview failed: {r_ir.text}"
        review_resp = await session_client.get("/review/items", headers=auth(auth_token))
        review_item = next(
            i for i in review_resp.json()
            if i.get("entity_name") == "PayrollPeriods"
            and i.get("entity_id") == str(pid)
            and i.get("status") == "Pending"
        )
        await session_client.post(
            f"/review/items/{review_item['review_item_id']}/decide",
            headers=auth(auth_token), json={"decision": "Approved"},
        )
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin.status_code == 422
        assert "minimum" in fin.text.lower() or "maximum" in fin.text.lower()

        # Cleanup period
        await direct_db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        for rule_id in [r_min.json()["driver_pay_rule_id"], r_max.json()["driver_pay_rule_id"]]:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/void",
                headers=auth(auth_token),
            )

    async def test_min_equals_max_earned_below_topup(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Min == Max == 400, earned=200 → SYS_MIN_TOPUP=200, no cap."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)
        r_min = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MinimumPay",
                  "amount": "400.00", "effective_from": "2042-05-01"},
            headers=auth(auth_token),
        )
        assert r_min.status_code == 201
        r_max = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MaximumPay",
                  "amount": "400.00", "effective_from": "2042-05-01"},
            headers=auth(auth_token),
        )
        assert r_max.status_code == 201

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2042-05-01", end="2042-05-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "200.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        final_lines = final_resp.json()
        topup_lines = [fl for fl in final_lines if fl["line_type"] == "SYS_MIN_TOPUP"]
        cap_lines = [fl for fl in final_lines if fl["line_type"] == "SYS_MAX_CAP"]
        assert len(topup_lines) == 1
        assert Decimal(topup_lines[0]["final_amount"]) == Decimal("200.00")
        assert len(cap_lines) == 0

        for rule_id in [r_min.json()["driver_pay_rule_id"], r_max.json()["driver_pay_rule_id"]]:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/end",
                json={"effective_to": "2042-05-07"},
                headers=auth(auth_token),
            )

    async def test_min_equals_max_earned_above_cap(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Min == Max == 400, earned=600 → SYS_MAX_CAP=-200, no topup."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)
        r_min = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MinimumPay",
                  "amount": "400.00", "effective_from": "2042-06-01"},
            headers=auth(auth_token),
        )
        assert r_min.status_code == 201
        r_max = await session_client.post(
            "/payroll/driver-pay-rules",
            json={"driver_id": m15_driver_id, "rule_type": "MaximumPay",
                  "amount": "400.00", "effective_from": "2042-06-01"},
            headers=auth(auth_token),
        )
        assert r_max.status_code == 201

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2042-06-01", end="2042-06-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "600.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        final_lines = final_resp.json()
        topup_lines = [fl for fl in final_lines if fl["line_type"] == "SYS_MIN_TOPUP"]
        cap_lines = [fl for fl in final_lines if fl["line_type"] == "SYS_MAX_CAP"]
        assert len(topup_lines) == 0
        assert len(cap_lines) == 1
        assert Decimal(cap_lines[0]["final_amount"]) == Decimal("-200.00")

        for rule_id in [r_min.json()["driver_pay_rule_id"], r_max.json()["driver_pay_rule_id"]]:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/end",
                json={"effective_to": "2042-06-07"},
                headers=auth(auth_token),
            )


# ===========================================================================
# TestMinMaxAsOfDate
# ===========================================================================

class TestMinMaxAsOfDate:
    """Correct as-of-date behaviour (uses period.start_date)."""

    async def test_ended_rule_covering_period_start_applies(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Ended rule with EffectiveTo >= period.start_date → still applies."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2043-01-01",
                "effective_to":   "2043-01-31",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]
        # End rule at 2043-01-15 (period start is 2043-01-10 — within range)
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2043-01-15"},
            headers=auth(auth_token),
        )

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2043-01-10", end="2043-01-16",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        topup_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MIN_TOPUP"]
        assert len(topup_lines) == 1
        assert Decimal(topup_lines[0]["final_amount"]) == Decimal("400.00")  # 500 - 100

    async def test_ended_rule_before_period_start_does_not_apply(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Ended rule with EffectiveTo < period.start_date → does not apply."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "9999.00",
                "effective_from": "2043-02-01",
                "effective_to":   "2043-02-10",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]
        # End rule at 2043-02-10 (period start is 2043-02-15 — after rule ends)
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2043-02-10"},
            headers=auth(auth_token),
        )

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2043-02-15", end="2043-02-21",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        topup_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MIN_TOPUP"]
        assert len(topup_lines) == 0, "Ended rule should not apply when effective_to < period.start_date"

    async def test_active_rule_starting_after_period_start_does_not_apply(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Active rule with effective_from > period.start_date → does not apply."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "9999.00",
                "effective_from": "2043-03-15",  # starts after period start (2043-03-01)
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2043-03-01", end="2043-03-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        topup_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MIN_TOPUP"]
        assert len(topup_lines) == 0, "Rule starting after period start should not apply"

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    async def test_voided_rule_does_not_apply(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        paytest_branch_id: int,
        m15_bonus_activated,
    ):
        """Voided rule → does not apply at finalization."""
        await _void_all_rules_for_driver(session_client, auth_token, m15_driver_id)
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "9999.00",
                "effective_from": "2043-04-01",
                "effective_to":   "2043-04-30",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]
        # Void it
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

        period = await _open_period(
            session_client, auth_token, paytest_branch_id,
            start="2043-04-01", end="2043-04-07",
        )
        pid = period["payroll_period_id"]
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": m15_driver_id, "line_type": "Bonus", "amount": "50.00"},
            headers=auth(auth_token),
        )
        await _finalize_period(session_client, auth_token, pid)

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        topup_lines = [fl for fl in final_resp.json() if fl["line_type"] == "SYS_MIN_TOPUP"]
        assert len(topup_lines) == 0, "Voided rule should not apply"


# ===========================================================================
# TestMinMaxManualEntryBlocked
# ===========================================================================

class TestMinMaxManualEntryBlocked:
    """SYS_MIN_TOPUP and SYS_MAX_CAP cannot be entered manually."""

    async def test_sys_min_topup_blocked_from_daily_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m15_open_period: dict,
    ):
        """POST SYS_MIN_TOPUP to /periods/{id}/lines → 422."""
        pid = m15_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id":   paytest_driver_id,
                "line_type":   "SYS_MIN_TOPUP",
                "quantity":    "1",
                "rate_amount": "100.00",
                "work_date":   "2036-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "system" in resp.text.lower() or "finalization" in resp.text.lower()

    async def test_sys_max_cap_blocked_from_daily_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m15_open_period: dict,
    ):
        """POST SYS_MAX_CAP to /periods/{id}/lines → 422."""
        pid = m15_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id":   paytest_driver_id,
                "line_type":   "SYS_MAX_CAP",
                "quantity":    "1",
                "rate_amount": "100.00",
                "work_date":   "2036-01-01",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_sys_min_topup_blocked_from_period_pay(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m15_open_period: dict,
    ):
        """POST SYS_MIN_TOPUP to /periods/{id}/period-pay → 422."""
        pid = m15_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={
                "driver_id": paytest_driver_id,
                "line_type": "SYS_MIN_TOPUP",
                "amount":    "100.00",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_sys_max_cap_blocked_from_period_pay(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m15_open_period: dict,
    ):
        """POST SYS_MAX_CAP to /periods/{id}/period-pay → 422."""
        pid = m15_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={
                "driver_id": paytest_driver_id,
                "line_type": "SYS_MAX_CAP",
                "amount":    "100.00",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 422


# ===========================================================================
# TestAuditRollback
# ===========================================================================

class TestAuditRollback:
    """
    Verify that audit write failures roll back the data changes.

    Note: ASGITransport re-raises server exceptions, so we use pytest.raises
    to absorb the RuntimeError from the test client and then check DB state.
    """

    async def test_create_rule_audit_rollback(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        direct_db,
        monkeypatch,
    ):
        """If _write_pay_rule_audit raises during create, the rule must not persist."""
        import app.payroll.service as svc
        import pytest

        async def _failing_audit(*args, **kwargs):
            raise RuntimeError("Audit write intentionally failed")

        monkeypatch.setattr(svc, "_write_pay_rule_audit", _failing_audit)

        # ASGITransport re-raises server exceptions — catch it
        with pytest.raises(Exception):
            await session_client.post(
                "/payroll/driver-pay-rules",
                json={
                    "driver_id":      m15_driver_id,
                    "rule_type":      "MinimumPay",
                    "amount":         "500.00",
                    "effective_from": "2044-01-01",
                    "effective_to":   "2044-01-31",
                },
                headers=auth(auth_token),
            )

        # Rule must not exist in DB (transaction was rolled back)
        row = await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.driverpayrules
                WHERE driverid = :did AND effectivefrom = '2044-01-01'
            """),
            {"did": m15_driver_id},
        )
        count = row.scalar_one()
        assert count == 0, "Rule must not exist after audit rollback"

    async def test_end_rule_audit_rollback(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        direct_db,
        monkeypatch,
    ):
        """If audit write fails during end, rule status must remain Active."""
        import app.payroll.service as svc
        import pytest

        # Create rule
        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2044-02-01",
                "effective_to":   "2044-02-28",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        original = svc._write_pay_rule_audit

        async def _failing_audit(*args, **kwargs):
            raise RuntimeError("Audit write intentionally failed")

        monkeypatch.setattr(svc, "_write_pay_rule_audit", _failing_audit)

        with pytest.raises(Exception):
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/end",
                json={"effective_to": "2044-02-15"},
                headers=auth(auth_token),
            )

        # Restore BEFORE any further service calls
        monkeypatch.setattr(svc, "_write_pay_rule_audit", original)

        # Rule status must still be Active
        row = await direct_db.execute(
            _text("SELECT status FROM payroll.driverpayrules WHERE driverpayruleid = :rid"),
            {"rid": rule_id},
        )
        status = row.scalar_one()
        assert status == "Active", f"Rule status must remain Active, got: {status}"

        # Cleanup (audit is restored now)
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    async def test_void_rule_audit_rollback(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        direct_db,
        monkeypatch,
    ):
        """If audit write fails during void, rule status must remain Active."""
        import app.payroll.service as svc
        import pytest

        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2044-03-01",
                "effective_to":   "2044-03-31",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        original = svc._write_pay_rule_audit

        async def _failing_audit(*args, **kwargs):
            raise RuntimeError("Audit write intentionally failed")

        monkeypatch.setattr(svc, "_write_pay_rule_audit", _failing_audit)

        with pytest.raises(Exception):
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/void",
                headers=auth(auth_token),
            )

        # Restore BEFORE any further service calls
        monkeypatch.setattr(svc, "_write_pay_rule_audit", original)

        # Status must still be Active
        row = await direct_db.execute(
            _text("SELECT status FROM payroll.driverpayrules WHERE driverpayruleid = :rid"),
            {"rid": rule_id},
        )
        status = row.scalar_one()
        assert status == "Active", f"Rule status must remain Active after void rollback, got: {status}"

        # Cleanup (audit is restored now)
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    async def test_notes_update_audit_rollback(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m15_driver_id: int,
        direct_db,
        monkeypatch,
    ):
        """If audit write fails during notes update, notes must remain unchanged."""
        import app.payroll.service as svc
        import pytest

        r = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      m15_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "500.00",
                "effective_from": "2044-04-01",
                "effective_to":   "2044-04-30",
                "notes":          "original note",
            },
            headers=auth(auth_token),
        )
        assert r.status_code == 201
        rule_id = r.json()["driver_pay_rule_id"]

        original = svc._write_pay_rule_audit

        async def _failing_audit(*args, **kwargs):
            raise RuntimeError("Audit write intentionally failed")

        monkeypatch.setattr(svc, "_write_pay_rule_audit", _failing_audit)

        with pytest.raises(Exception):
            await session_client.patch(
                f"/payroll/driver-pay-rules/{rule_id}",
                json={"notes": "new note that should not persist"},
                headers=auth(auth_token),
            )

        # Restore BEFORE any further service calls
        monkeypatch.setattr(svc, "_write_pay_rule_audit", original)

        # Notes must remain "original note"
        row = await direct_db.execute(
            _text("SELECT notes FROM payroll.driverpayrules WHERE driverpayruleid = :rid"),
            {"rid": rule_id},
        )
        notes = row.scalar_one()
        assert notes == "original note", f"Notes must remain unchanged, got: {notes}"

        # Cleanup (audit is restored now)
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
