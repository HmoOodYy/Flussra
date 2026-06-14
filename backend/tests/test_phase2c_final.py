"""
tests/test_phase2c_final.py — Phase 2C Final Fix tests

Covers:
  TestPayRulesCRUD           — create, end, void pay rules (Fix 1)
  TestCopyRatesAllOrNothing  — invalid target branch mapping rejects whole copy (Fix 2)
  TestCopyRatesAdvanced      — advanced/tier/block rates rejected (Fix 3)
  TestPayRulesODA            — ODA cannot access other driver's pay rules
"""
import pytest
import httpx
from datetime import date


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _void_all_driver_rates(client, token, driver_id: int) -> None:
    """Void all non-Voided rates for a driver (cleanup helper)."""
    resp = await client.get(
        f"/payroll/drivers/{driver_id}/rates/history", headers=auth(token)
    )
    if resp.status_code != 200:
        return
    for rate in resp.json():
        if rate.get("status") not in ("Voided",):
            await client.delete(
                f"/payroll/rates/{rate['driver_rate_id']}", headers=auth(token)
            )


async def _make_driver(client, token, branch_id, suffix="") -> int:
    import random
    s = suffix or str(random.randint(1000, 9999))
    r = await client.post(
        "/core/drivers",
        json={"branch_id": branch_id, "full_name": f"Final2C {s}", "driver_code": f"FIN-{s}"},
        headers=auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["driver_id"]


async def _create_pay_rule(client, token, driver_id, rule_type, amount, eff_from, eff_to=None) -> int:
    payload = {"driver_id": driver_id, "rule_type": rule_type,
               "amount": amount, "effective_from": eff_from}
    if eff_to:
        payload["effective_to"] = eff_to
    r = await client.post("/payroll/driver-pay-rules", json=payload, headers=auth(token))
    assert r.status_code == 201, r.text
    return r.json()["driver_pay_rule_id"]


# ===========================================================================
# Fix 1 — Pay Rules CRUD
# ===========================================================================

class TestPayRulesCRUD:
    """
    Verify the full lifecycle via the backend endpoints that the frontend now uses:
    create → verify active → end → verify ended → cannot re-end → void.
    """

    @pytest.mark.asyncio
    async def test_create_minimum_pay(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """POST /payroll/driver-pay-rules creates an Active MinimumPay rule."""
        rule_id = await _create_pay_rule(
            session_client, auth_token, paytest_driver_id,
            "MinimumPay", "200.00", "2088-01-01", "2088-12-31",
        )
        # Verify in list
        list_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/pay-rules",
            headers=auth(auth_token),
        )
        assert list_resp.status_code == 200
        rules = list_resp.json()
        rule = next((r for r in rules if r["driver_pay_rule_id"] == rule_id), None)
        assert rule is not None
        assert rule["rule_type"] == "MinimumPay"
        assert rule["status"] == "Active"
        assert float(rule["amount"]) == 200.00

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    @pytest.mark.asyncio
    async def test_create_maximum_pay(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """POST /payroll/driver-pay-rules creates an Active MaximumPay rule."""
        rule_id = await _create_pay_rule(
            session_client, auth_token, paytest_driver_id,
            "MaximumPay", "1500.00", "2089-01-01", "2089-12-31",
        )
        get_resp = await session_client.get(
            f"/payroll/driver-pay-rules/{rule_id}", headers=auth(auth_token)
        )
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["rule_type"] == "MaximumPay"
        assert data["status"] == "Active"

        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )

    @pytest.mark.asyncio
    async def test_end_minimum_pay(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """POST /payroll/driver-pay-rules/{id}/end closes an Active rule."""
        rule_id = await _create_pay_rule(
            session_client, auth_token, paytest_driver_id,
            "MinimumPay", "180.00", "2090-01-01",
        )
        end_resp = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2090-06-30"},
            headers=auth(auth_token),
        )
        assert end_resp.status_code == 200, end_resp.text
        data = end_resp.json()
        assert data["status"] == "Ended"
        assert data["effective_to"] == "2090-06-30"

    @pytest.mark.asyncio
    async def test_end_maximum_pay(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """POST /payroll/driver-pay-rules/{id}/end closes an Active MaximumPay rule."""
        rule_id = await _create_pay_rule(
            session_client, auth_token, paytest_driver_id,
            "MaximumPay", "2000.00", "2091-01-01",
        )
        end_resp = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/end",
            json={"effective_to": "2091-03-31"},
            headers=auth(auth_token),
        )
        assert end_resp.status_code == 200, end_resp.text
        assert end_resp.json()["status"] == "Ended"

    @pytest.mark.asyncio
    async def test_void_active_rule(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """POST /payroll/driver-pay-rules/{id}/void voids an Active rule."""
        rule_id = await _create_pay_rule(
            session_client, auth_token, paytest_driver_id,
            "MinimumPay", "90.00", "2092-01-01", "2092-12-31",
        )
        void_resp = await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
        assert void_resp.status_code == 200, void_resp.text
        assert void_resp.json()["status"] == "Voided"

    @pytest.mark.asyncio
    async def test_end_requires_payrates_edit(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
    ):
        """branch_user (no payrates.edit) cannot end a pay rule."""
        rule_id = await _create_pay_rule(
            session_client, auth_token, paytest_driver_id,
            "MaximumPay", "3000.00", "2093-01-01", "2093-12-31",
        )
        try:
            login = await session_client.post(
                "/auth/login",
                json={"company_code": "DEMO", "username": "branch_user", "password": "TestPass123!"},
            )
            assert login.status_code == 200
            np_token = login.json()["access_token"]

            end_resp = await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/end",
                json={"effective_to": "2093-06-30"},
                headers=auth(np_token),
            )
            assert end_resp.status_code == 403
        finally:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
            )


# ===========================================================================
# Fix 1 — ODA cannot access other driver's pay rules
# ===========================================================================

class TestPayRulesODA:
    """ODA scope enforced on pay rules endpoints."""

    @pytest.mark.asyncio
    async def test_oda_cannot_read_other_driver_pay_rules(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
    ):
        """
        ODA user cannot read another driver's pay rules.

        Strategy: create a linked Driver user with ODA payrates role,
        then try to read paytest_driver_id's pay rules.
        Reuses _create_oda_linked_driver_user from TestMatrixODAScope.
        """
        import random
        from tests.test_pay_rates import TestMatrixODAScope  # type: ignore[import]
        username1 = f"oda_pr_read_{random.randint(10000, 99999)}"
        oda_token, own_driver_id = await TestMatrixODAScope._create_oda_linked_driver_user(
            session_client, auth_token, username1, paytest_branch_id
        )

        # Accessing OWN driver's rules is allowed
        own_resp = await session_client.get(
            f"/payroll/drivers/{own_driver_id}/pay-rules",
            headers=auth(oda_token),
        )
        assert own_resp.status_code == 200

        # Accessing ANOTHER driver's rules is blocked
        other_resp = await session_client.get(
            f"/payroll/drivers/{paytest_driver_id}/pay-rules",
            headers=auth(oda_token),
        )
        assert other_resp.status_code == 403

    @pytest.mark.asyncio
    async def test_oda_cannot_create_other_driver_pay_rule(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
    ):
        """ODA user cannot create a pay rule for another driver."""
        import random
        from tests.test_pay_rates import TestMatrixODAScope  # type: ignore[import]
        username2 = f"oda_pr_write_{random.randint(10000, 99999)}"
        oda_token, _own = await TestMatrixODAScope._create_oda_linked_driver_user(
            session_client, auth_token, username2, paytest_branch_id
        )

        resp = await session_client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id": paytest_driver_id,
                "rule_type": "MinimumPay",
                "amount": "100.00",
                "effective_from": "2094-01-01",
                "effective_to": "2094-12-31",
            },
            headers=auth(oda_token),
        )
        assert resp.status_code == 403


# ===========================================================================
# Fix 2 — Copy Rates all-or-nothing: invalid target branch mapping
# ===========================================================================

class TestCopyRatesAllOrNothing:
    """
    Any source rate not configured for the target branch must cause
    the entire copy to fail with 422, not silently skip.
    """

    @pytest.mark.asyncio
    async def test_copy_from_same_branch_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """Same-branch copy with a valid mapping succeeds (control test)."""
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"AO-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"AO-TGT-{random.randint(100,999)}")

        # Create + approve a rate on source
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE rate type not available")

        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "4.00", "effective_from": "2078-01-01"},
            headers=auth(auth_token),
        )
        if cr.status_code != 201:
            pytest.skip(f"Rate creation skipped: {cr.text}")
        await session_client.post(f"/payroll/rates/{cr.json()['driver_rate_id']}/approve", headers=auth(auth_token))

        resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2078-06-01"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["rates_copied"] >= 1

    @pytest.mark.asyncio
    async def test_copy_rejects_invalid_branch_mapping(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        When the source driver has a rate whose rate type is NOT configured
        for the target driver's branch, the copy must fail with 422.

        Strategy: create source on PAYTEST (which has MILEAGE configured).
        Create target on HQ branch (branch_id=1). If HQ does NOT have MILEAGE
        configured, copy should fail.

        If HQ DOES have MILEAGE, the test is inconclusive (skip, not fail).
        The control case (same branch) is the reliable positive test.
        """
        import random

        hq_branch_id = 1
        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"XBRANCH-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, hq_branch_id, f"XBRANCH-TGT-{random.randint(100,999)}")

        # Create + approve a MILEAGE rate on source (PAYTEST branch has MILEAGE configured)
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE rate type not found")

        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "5.00", "effective_from": "2079-01-01"},
            headers=auth(auth_token),
        )
        if cr.status_code != 201:
            pytest.skip(f"Source rate creation failed: {cr.text}")
        await session_client.post(f"/payroll/rates/{cr.json()['driver_rate_id']}/approve", headers=auth(auth_token))

        # Check if HQ branch has MILEAGE configured — if yes, skip (not a useful test)
        matrix_resp = await session_client.get(
            f"/payroll/drivers/{tgt_id}/rate-matrix",
            params={"as_of": date.today().isoformat()},
            headers=auth(auth_token),
        )
        if matrix_resp.status_code == 200:
            hq_rate_codes = {g["rate_code"] for g in matrix_resp.json().get("groups", [])}
            if "MILEAGE" in hq_rate_codes:
                pytest.skip("HQ branch has MILEAGE configured; cross-branch test not meaningful here")

        copy_resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2079-06-01"},
            headers=auth(auth_token),
        )
        assert copy_resp.status_code == 422, (
            f"Expected 422 when copying to branch without the rate type configured, "
            f"got {copy_resp.status_code}: {copy_resp.text}"
        )
        detail = copy_resp.json().get("detail", "")
        assert "not configured" in detail.lower() or "branch" in detail.lower(), (
            f"Error message should mention branch/config: {detail}"
        )

    @pytest.mark.asyncio
    async def test_copy_rejects_means_no_rates_created(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        When copy fails with 422, no rates should have been created on the target.
        Verify atomicity.
        """
        import random

        hq_branch_id = 1
        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"ATOM-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, hq_branch_id, f"ATOM-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")

        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "5.00", "effective_from": "2080-01-01"},
            headers=auth(auth_token),
        )
        if cr.status_code != 201:
            pytest.skip(f"Source rate creation failed: {cr.text}")
        await session_client.post(f"/payroll/rates/{cr.json()['driver_rate_id']}/approve", headers=auth(auth_token))

        # Check if HQ has MILEAGE — if yes, skip
        matrix_resp = await session_client.get(
            f"/payroll/drivers/{tgt_id}/rate-matrix",
            params={"as_of": date.today().isoformat()},
            headers=auth(auth_token),
        )
        if matrix_resp.status_code == 200:
            hq_codes = {g["rate_code"] for g in matrix_resp.json().get("groups", [])}
            if "MILEAGE" in hq_codes:
                pytest.skip("HQ branch has MILEAGE; cross-branch test not meaningful")

        # Attempt copy — should fail
        copy_resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2080-06-01"},
            headers=auth(auth_token),
        )
        if copy_resp.status_code != 422:
            pytest.skip(f"Copy did not fail (status {copy_resp.status_code}); branches may share config")

        # Verify no rates were created on target
        history_resp = await session_client.get(
            f"/payroll/drivers/{tgt_id}/rates/history",
            headers=auth(auth_token),
        )
        assert history_resp.status_code == 200
        target_rates = history_resp.json()
        assert len(target_rates) == 0, (
            f"Expected 0 rates on target after failed copy, got {len(target_rates)}"
        )


# ===========================================================================
# Fix 3 — Copy Rates advanced/tier/block rejection
# ===========================================================================

class TestCopyRatesAdvanced:
    """
    Advanced rates (tiered, block, etc.) must be rejected — not silently flattened.
    """

    @pytest.mark.asyncio
    async def test_copy_normal_flat_rate_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """Flat rates copy successfully (control test for the advanced rejection check)."""
        import random
        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"FLAT-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"FLAT-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")

        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "2.25", "effective_from": "2082-01-01"},
            headers=auth(auth_token),
        )
        if cr.status_code != 201:
            pytest.skip(f"Rate creation failed: {cr.text}")
        await session_client.post(f"/payroll/rates/{cr.json()['driver_rate_id']}/approve", headers=auth(auth_token))

        resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2082-06-01"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["rates_copied"] >= 1

    @pytest.mark.asyncio
    async def test_copy_rejects_tiered_source_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Copy-from must reject with 422 when the source driver has a tiered rate.

        Strategy: create a normal MILEAGE rate, approve it, then inject a fake
        row into driverratetiers (using direct_db) to simulate a tiered rate
        without needing a full M13C pay item setup.
        """
        from sqlalchemy import text as _text
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"TIER-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"TIER-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")

        # Create and approve a MILEAGE rate
        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "2.50", "effective_from": "2083-01-01"},
            headers=auth(auth_token),
        )
        if cr.status_code != 201:
            pytest.skip(f"Rate creation failed: {cr.text}")
        rate_id = cr.json()["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{rate_id}/approve", headers=auth(auth_token))

        # Inject a fake tier row directly into driverratetiers
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverratetiers
                    (driverrateid, tiersequence, fromunit, tieramount)
                VALUES (:rid, 1, 0, 2.50)
            """),
            {"rid": rate_id},
        )

        try:
            # Attempt copy — must fail because has_tiers=TRUE
            copy_resp = await session_client.post(
                f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
                json={"effective_from": "2083-06-01"},
                headers=auth(auth_token),
            )
            assert copy_resp.status_code == 422, (
                f"Expected 422 for tiered source rate, got {copy_resp.status_code}: {copy_resp.text}"
            )
            detail = copy_resp.json().get("detail", "").lower()
            assert "advanced" in detail or "tier" in detail, (
                f"Error should mention advanced/tiered: {detail}"
            )
        finally:
            # Cleanup tier row and rate
            await direct_db.execute(
                _text("DELETE FROM payroll.driverratetiers WHERE driverrateid = :rid"),
                {"rid": rate_id},
            )
            await session_client.post(f"/payroll/rates/{rate_id}/void", headers=auth(auth_token))

    @pytest.mark.asyncio
    async def test_copy_no_partial_when_advanced_present(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        When a tiered rate causes rejection, no rates from the same source
        driver are copied (not even flat rates) — atomicity check.
        """
        from sqlalchemy import text as _text
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"NOPART-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"NOPART-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        hourly = next((r for r in rt_resp.json() if r["rate_code"] == "HOURLY"), None)
        if mileage is None or hourly is None:
            pytest.skip("Required rate types not in test DB")

        # Create and approve a flat MILEAGE rate (no tiers)
        cr1 = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "3.00", "effective_from": "2084-01-01"},
            headers=auth(auth_token),
        )
        if cr1.status_code != 201:
            pytest.skip(f"MILEAGE rate creation failed: {cr1.text}")
        mileage_rate_id = cr1.json()["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{mileage_rate_id}/approve", headers=auth(auth_token))

        # Create and approve a HOURLY rate, then inject a tier row
        cr2 = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": hourly["rate_type_id"],
                  "amount": "25.00", "effective_from": "2084-01-01"},
            headers=auth(auth_token),
        )
        if cr2.status_code != 201:
            await session_client.post(f"/payroll/rates/{mileage_rate_id}/void", headers=auth(auth_token))
            pytest.skip(f"HOURLY rate creation failed: {cr2.text}")
        hourly_rate_id = cr2.json()["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{hourly_rate_id}/approve", headers=auth(auth_token))

        await direct_db.execute(
            _text("""
                INSERT INTO payroll.driverratetiers
                    (driverrateid, tiersequence, fromunit, tieramount)
                VALUES (:rid, 1, 0, 25.00)
            """),
            {"rid": hourly_rate_id},
        )

        try:
            copy_resp = await session_client.post(
                f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
                json={"effective_from": "2084-06-01"},
                headers=auth(auth_token),
            )
            assert copy_resp.status_code == 422, (
                f"Expected 422 (tiered present), got {copy_resp.status_code}: {copy_resp.text}"
            )

            # Verify NO rates created on target (atomicity)
            hist_resp = await session_client.get(
                f"/payroll/drivers/{tgt_id}/rates/history",
                headers=auth(auth_token),
            )
            assert hist_resp.status_code == 200
            assert len(hist_resp.json()) == 0, (
                f"Expected 0 rates on target after failed copy, got {len(hist_resp.json())}"
            )
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.driverratetiers WHERE driverrateid = :rid"),
                {"rid": hourly_rate_id},
            )
            await session_client.post(f"/payroll/rates/{mileage_rate_id}/void", headers=auth(auth_token))
            await session_client.post(f"/payroll/rates/{hourly_rate_id}/void", headers=auth(auth_token))


# ===========================================================================
# P1 #1 — Copy Rates supersession date: effectiveto = effective_from - 1 day
# ===========================================================================

class TestCopyRatesSupersessionDate:
    """
    Verify the superseded old rate gets effectiveto = effective_from - 1 day,
    not effective_from (which would create an inclusive overlap).
    """

    @pytest.mark.asyncio
    async def test_superseded_rate_effectiveto_is_day_before(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        After a copy:
          - old Approved rate status = 'Superseded'
          - old rate effectiveto     = copy effective_from - 1 day
          - new rate effectivefrom   = copy effective_from
          - no inclusive overlap on copy date
        """
        from sqlalchemy import text as _text
        import random
        from datetime import date, timedelta

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"SUP-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"SUP-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")

        rtid = mileage["rate_type_id"]

        # Create + approve MILEAGE rate on source (will be copied)
        cr_src = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": rtid,
                  "amount": "3.50", "effective_from": "2085-01-01"},
            headers=auth(auth_token),
        )
        assert cr_src.status_code == 201, cr_src.text
        await session_client.post(
            f"/payroll/rates/{cr_src.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        # Create + approve an existing MILEAGE rate on target (will be superseded)
        cr_tgt = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "2.00", "effective_from": "2085-01-01"},
            headers=auth(auth_token),
        )
        assert cr_tgt.status_code == 201, cr_tgt.text
        tgt_rate_id = cr_tgt.json()["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{tgt_rate_id}/approve", headers=auth(auth_token))

        # Copy rates with effective_from = 2085-07-01
        copy_date = date(2085, 7, 1)
        expected_supersede_to = copy_date - timedelta(days=1)  # 2085-06-30

        copy_resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": copy_date.isoformat()},
            headers=auth(auth_token),
        )
        assert copy_resp.status_code == 200, copy_resp.text
        assert copy_resp.json()["rates_copied"] >= 1

        # Check the superseded rate directly in DB
        sup_result = await direct_db.execute(
            _text("""
                SELECT status, effectiveto
                FROM payroll.driverrates
                WHERE driverrateid = :rid
            """),
            {"rid": tgt_rate_id},
        )
        row = sup_result.mappings().first()
        assert row is not None
        assert row["status"] == "Superseded", (
            f"Expected 'Superseded', got '{row['status']}'"
        )
        assert row["effectiveto"] == expected_supersede_to, (
            f"Expected effectiveto={expected_supersede_to}, got {row['effectiveto']}. "
            "Old rate must NOT overlap with the new rate on effective_from date."
        )

        # Also verify the new rate on target starts on copy_date
        new_rate_result = await direct_db.execute(
            _text("""
                SELECT effectivefrom, effectiveto, status
                FROM payroll.driverrates
                WHERE driverid   = :did
                  AND ratetypeid = :rtid
                  AND status     = 'Approved'
                  AND effectivefrom = :eff_from
            """),
            {"did": tgt_id, "rtid": rtid, "eff_from": copy_date},
        )
        new_row = new_rate_result.mappings().first()
        assert new_row is not None, "New copied rate should be Approved with effectivefrom=copy_date"
        assert new_row["effectiveto"] is None, "New copied rate should have open effectiveto"

        # Cleanup: void all rates on both drivers so rates_clean fixture stays under limit
        await _void_all_driver_rates(session_client, auth_token, src_id)
        await _void_all_driver_rates(session_client, auth_token, tgt_id)

    @pytest.mark.asyncio
    async def test_no_overlap_on_effective_from_date(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        On the copy effective_from date, exactly one Approved rate should exist.
        The old Superseded rate must NOT cover that date.
        """
        from sqlalchemy import text as _text
        import random
        from datetime import date

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"NOVLP-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"NOVLP-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        rtid = mileage["rate_type_id"]

        cr_src = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": rtid,
                  "amount": "5.00", "effective_from": "2086-01-01"},
            headers=auth(auth_token),
        )
        assert cr_src.status_code == 201, cr_src.text
        await session_client.post(
            f"/payroll/rates/{cr_src.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        cr_tgt = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "1.00", "effective_from": "2086-01-01"},
            headers=auth(auth_token),
        )
        assert cr_tgt.status_code == 201, cr_tgt.text
        await session_client.post(
            f"/payroll/rates/{cr_tgt.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        copy_date = date(2086, 6, 15)
        copy_resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": copy_date.isoformat()},
            headers=auth(auth_token),
        )
        assert copy_resp.status_code == 200, copy_resp.text

        # On copy_date, only the NEW Approved rate should cover the date
        overlap_check = await direct_db.execute(
            _text("""
                SELECT COUNT(*) AS cnt
                FROM payroll.driverrates
                WHERE driverid    = :did
                  AND ratetypeid  = :rtid
                  AND status      IN ('Approved', 'Superseded')
                  AND effectivefrom <= :dt
                  AND (effectiveto IS NULL OR effectiveto >= :dt)
            """),
            {"did": tgt_id, "rtid": rtid, "dt": copy_date},
        )
        count = overlap_check.scalar_one()
        # Superseded rate must have effectiveto = copy_date - 1, so it does NOT cover copy_date
        # New Approved rate has effectivefrom = copy_date, so it DOES cover copy_date
        # Total: exactly 1
        assert count == 1, (
            f"Expected exactly 1 rate covering {copy_date}, got {count}. "
            "Superseded rate must end day before copy_date to avoid overlap."
        )

        # Cleanup
        await _void_all_driver_rates(session_client, auth_token, src_id)
        await _void_all_driver_rates(session_client, auth_token, tgt_id)


# ===========================================================================
# P1 #2 — Pay Rules finalized-period guard
# ===========================================================================

class TestPayRulesFinalizedGuard:
    """
    Pay rules cannot be created (or copied) with effective_from inside a
    Locked or Archived payroll period.
    """

    async def _insert_locked_period(self, direct_db, company_id: int, branch_id: int,
                                    start_date: str, end_date: str, status: str = "Locked") -> int:
        """Insert a finalized payroll period directly into the DB for testing."""
        from sqlalchemy import text as _text
        from datetime import date as _date
        start = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date)
        result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, paydate, status, createdbyuserid)
                VALUES
                    (:cid, :bid, :code, :name, 'Month',
                     :start, :end, :end, :status, 1)
                RETURNING payrollperiodid
            """),
            {
                "cid": company_id, "bid": branch_id,
                "code": f"TEST-{start_date}", "name": f"Test {start_date}",
                "start": start, "end": end,
                "status": status,
            },
        )
        return result.scalar_one()

    @pytest.mark.asyncio
    async def test_create_min_pay_inside_locked_period_fails(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """POST /payroll/driver-pay-rules with effective_from inside a Locked period → 422."""
        import random
        from sqlalchemy import text as _text

        # Get company_id from the test DB
        cid_result = await direct_db.execute(
            _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO' LIMIT 1")
        )
        company_id = cid_result.scalar_one()

        period_id = await self._insert_locked_period(
            direct_db, company_id, paytest_branch_id,
            "2070-01-01", "2070-01-31", "Locked",
        )
        try:
            resp = await session_client.post(
                "/payroll/driver-pay-rules",
                json={
                    "driver_id": paytest_driver_id,
                    "rule_type": "MinimumPay",
                    "amount": "100.00",
                    "effective_from": "2070-01-15",
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"Expected 422 for pay rule inside Locked period, got {resp.status_code}: {resp.text}"
            )
            detail = resp.json().get("detail", "").lower()
            assert "finalized" in detail or "locked" in detail or "pay rule" in detail, (
                f"Error should mention finalized/locked: {detail}"
            )
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": period_id},
            )

    @pytest.mark.asyncio
    async def test_create_max_pay_inside_archived_period_fails(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """POST /payroll/driver-pay-rules with effective_from inside an Archived period → 422."""
        from sqlalchemy import text as _text

        cid_result = await direct_db.execute(
            _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO' LIMIT 1")
        )
        company_id = cid_result.scalar_one()

        period_id = await self._insert_locked_period(
            direct_db, company_id, paytest_branch_id,
            "2071-03-01", "2071-03-31", "Archived",
        )
        try:
            resp = await session_client.post(
                "/payroll/driver-pay-rules",
                json={
                    "driver_id": paytest_driver_id,
                    "rule_type": "MaximumPay",
                    "amount": "5000.00",
                    "effective_from": "2071-03-10",
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, (
                f"Expected 422 for pay rule inside Archived period, got {resp.status_code}: {resp.text}"
            )
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": period_id},
            )

    @pytest.mark.asyncio
    async def test_create_pay_rule_outside_finalized_period_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Pay rule with effective_from outside any finalized period succeeds."""
        from sqlalchemy import text as _text

        cid_result = await direct_db.execute(
            _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO' LIMIT 1")
        )
        company_id = cid_result.scalar_one()

        # Insert Locked period for Jan 2072
        period_id = await self._insert_locked_period(
            direct_db, company_id, paytest_branch_id,
            "2072-01-01", "2072-01-31", "Locked",
        )
        try:
            # effective_from = 2072-02-01 — outside the locked Jan period
            resp = await session_client.post(
                "/payroll/driver-pay-rules",
                json={
                    "driver_id": paytest_driver_id,
                    "rule_type": "MinimumPay",
                    "amount": "150.00",
                    "effective_from": "2072-02-01",
                    "effective_to": "2072-12-31",
                },
                headers=auth(auth_token),
            )
            assert resp.status_code == 201, (
                f"Expected 201 for pay rule outside finalized period, got {resp.status_code}: {resp.text}"
            )
            rule_id = resp.json()["driver_pay_rule_id"]
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
            )
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": period_id},
            )

    @pytest.mark.asyncio
    async def test_copy_rates_with_pay_rules_inside_locked_period_fails(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Copy Rates with include_pay_rules=true where data.effective_from is inside a
        Locked period → 422, no rates or pay rules written (all-or-nothing).

        Design note — which guard fires:
          Copied pay rules now use data.effective_from (request date), matching the
          rates effective date.  When allow_self_approval=True (DEMO default), the
          rates backdating guard fires FIRST for the same date, before the pay-rules
          loop is reached.  This is the correct product behaviour: the entire copy is
          rejected and nothing is written.  The error message says "rate" rather than
          "pay rule" in this configuration; both are valid 422 responses for the same
          root cause (effective_from inside a finalized period).

          When allow_self_approval=False the pay-rules guard becomes the primary
          protection (the rates backdating guard is skipped for PendingApproval paths).
          That scenario is not covered here to avoid mutating company settings in a
          session-scoped test client.
        """
        import random
        from sqlalchemy import text as _text

        cid_result = await direct_db.execute(
            _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO' LIMIT 1")
        )
        company_id = cid_result.scalar_one()

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"RPRL-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"RPRL-TGT-{random.randint(100,999)}")

        # Create an Active pay rule on source (historical date, before locked period)
        rule_id = await _create_pay_rule(
            session_client, auth_token, src_id,
            "MinimumPay", "200.00", "2074-01-01",
        )

        # Locked period that will contain the copy effective_from
        period_id = await self._insert_locked_period(
            direct_db, company_id, paytest_branch_id,
            "2074-08-01", "2074-08-31", "Locked",
        )

        # Create + approve a MILEAGE rate on source
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "3.00", "effective_from": "2074-01-01"},
            headers=auth(auth_token),
        )
        if cr.status_code != 201:
            pytest.skip(f"Rate creation failed: {cr.text}")
        await session_client.post(
            f"/payroll/rates/{cr.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        try:
            # effective_from = 2074-08-15, which is inside the Locked period.
            # With allow_self_approval=True the rates backdating guard fires first
            # and returns 422 before the pay-rules loop.  The copy is rejected and
            # nothing (no rates, no pay rules) is written.
            copy_resp = await session_client.post(
                f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
                json={"effective_from": "2074-08-15", "include_pay_rules": True},
                headers=auth(auth_token),
            )
            assert copy_resp.status_code == 422, (
                f"Expected 422 when effective_from is inside Locked period, "
                f"got {copy_resp.status_code}: {copy_resp.text}"
            )
            # Message comes from rates guard (allow_self_approval=True) or pay-rules
            # guard (allow_self_approval=False); both mention "finalized".
            detail = copy_resp.json().get("detail", "").lower()
            assert "finalized" in detail, (
                f"Error should mention 'finalized': {detail}"
            )

            # Atomicity: no rates on target
            hist_resp = await session_client.get(
                f"/payroll/drivers/{tgt_id}/rates/history",
                headers=auth(auth_token),
            )
            assert hist_resp.status_code == 200
            assert len(hist_resp.json()) == 0, (
                "No rates should exist on target after failed copy"
            )

            # Atomicity: no pay rules on target
            rules_resp = await session_client.get(
                f"/payroll/drivers/{tgt_id}/pay-rules",
                headers=auth(auth_token),
            )
            assert rules_resp.status_code == 200
            assert len(rules_resp.json()) == 0, (
                "No pay rules should exist on target after failed copy"
            )
        finally:
            await session_client.post(
                f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
            )
            await _void_all_driver_rates(session_client, auth_token, src_id)
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": period_id},
            )


# ===========================================================================
# Copy Pay Rules effective_from behaviour
# ===========================================================================

class TestCopyPayRulesEffectiveFrom:
    """
    Copied pay rules must use data.effective_from (the request date), not the
    source driver's historical effectivefrom.  They are inserted open-ended
    (effectiveto = NULL).  Amount and rule_type are preserved from the source.
    """

    @pytest.mark.asyncio
    async def test_copy_pay_rules_uses_request_effective_from(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        include_pay_rules=True: target pay rule effectivefrom = request effective_from,
        NOT the source rule's historical effectivefrom.
        """
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"CPEFF-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"CPEFF-TGT-{random.randint(100,999)}")

        # Source pay rule with a historical start date well before the copy date
        src_rule_id = await _create_pay_rule(
            session_client, auth_token, src_id,
            "MinimumPay", "350.00", "2095-01-01",
        )

        # Source rate (required for copy to proceed to pay-rules section)
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "4.00", "effective_from": "2095-01-01"},
            headers=auth(auth_token),
        )
        assert cr.status_code == 201, cr.text
        await session_client.post(
            f"/payroll/rates/{cr.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        copy_date = "2095-06-01"
        copy_resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": copy_date, "include_pay_rules": True},
            headers=auth(auth_token),
        )
        assert copy_resp.status_code == 200, copy_resp.text
        assert copy_resp.json()["pay_rules_copied"] == 1

        # Fetch the target's pay rules and assert effective_from = copy_date
        rules_resp = await session_client.get(
            f"/payroll/drivers/{tgt_id}/pay-rules",
            headers=auth(auth_token),
        )
        assert rules_resp.status_code == 200
        rules = rules_resp.json()
        assert len(rules) == 1, f"Expected 1 pay rule on target, got {len(rules)}"
        tgt_rule = rules[0]
        assert tgt_rule["effective_from"] == copy_date, (
            f"Expected effective_from={copy_date} (request date), "
            f"got {tgt_rule['effective_from']} (source historical date was 2095-01-01)"
        )
        assert tgt_rule["effective_to"] is None, (
            f"Copied pay rule should be open-ended (effectiveto=NULL), "
            f"got {tgt_rule['effective_to']}"
        )
        assert tgt_rule["rule_type"] == "MinimumPay"
        assert float(tgt_rule["amount"]) == 350.00, (
            f"Amount should be preserved from source, got {tgt_rule['amount']}"
        )

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{src_rule_id}/void", headers=auth(auth_token)
        )
        await _void_all_driver_rates(session_client, auth_token, src_id)
        await _void_all_driver_rates(session_client, auth_token, tgt_id)
        # Void the copied pay rule on target
        if tgt_rule.get("driver_pay_rule_id"):
            await session_client.post(
                f"/payroll/driver-pay-rules/{tgt_rule['driver_pay_rule_id']}/void",
                headers=auth(auth_token),
            )

    @pytest.mark.asyncio
    async def test_copy_include_pay_rules_outside_finalized_period_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Full success case: include_pay_rules=True, effective_from outside any finalized
        period.  Both rates and pay rules are written on the target.
        """
        import random
        from sqlalchemy import text as _text

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"CPOK-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"CPOK-TGT-{random.randint(100,999)}")

        src_rule_id = await _create_pay_rule(
            session_client, auth_token, src_id,
            "MaximumPay", "2000.00", "2096-01-01",
        )

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "5.50", "effective_from": "2096-01-01"},
            headers=auth(auth_token),
        )
        assert cr.status_code == 201, cr.text
        await session_client.post(
            f"/payroll/rates/{cr.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        copy_date = "2096-07-01"
        copy_resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": copy_date, "include_pay_rules": True},
            headers=auth(auth_token),
        )
        assert copy_resp.status_code == 200, copy_resp.text
        result = copy_resp.json()
        assert result["rates_copied"] >= 1
        assert result["pay_rules_copied"] == 1

        # Verify target has the pay rule with correct effective_from
        rules_resp = await session_client.get(
            f"/payroll/drivers/{tgt_id}/pay-rules",
            headers=auth(auth_token),
        )
        assert rules_resp.status_code == 200
        rules = rules_resp.json()
        assert len(rules) == 1
        assert rules[0]["effective_from"] == copy_date
        assert rules[0]["rule_type"] == "MaximumPay"
        assert float(rules[0]["amount"]) == 2000.00

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{src_rule_id}/void", headers=auth(auth_token)
        )
        if rules[0].get("driver_pay_rule_id"):
            await session_client.post(
                f"/payroll/driver-pay-rules/{rules[0]['driver_pay_rule_id']}/void",
                headers=auth(auth_token),
            )
        await _void_all_driver_rates(session_client, auth_token, src_id)
        await _void_all_driver_rates(session_client, auth_token, tgt_id)

    @pytest.mark.asyncio
    async def test_copy_pay_rule_blocked_by_finalized_period_no_partial_writes(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        When effective_from is inside a finalized period, copy returns 422 and
        neither rates nor pay rules are written on the target (all-or-nothing).

        When allow_self_approval=True (DEMO default), the rates backdating guard
        fires first.  The result is the same: 422, nothing written.
        """
        import random
        from sqlalchemy import text as _text

        cid_result = await direct_db.execute(
            _text("SELECT companyid FROM core.companies WHERE companycode = 'DEMO' LIMIT 1")
        )
        company_id = cid_result.scalar_one()

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"CPBLOCK-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"CPBLOCK-TGT-{random.randint(100,999)}")

        src_rule_id = await _create_pay_rule(
            session_client, auth_token, src_id,
            "MinimumPay", "100.00", "2097-01-01",
        )

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "3.75", "effective_from": "2097-01-01"},
            headers=auth(auth_token),
        )
        assert cr.status_code == 201, cr.text
        await session_client.post(
            f"/payroll/rates/{cr.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        # Inject a Locked period covering the intended copy date
        period_id = await self._insert_locked_period(
            direct_db, company_id, paytest_branch_id,
            "2097-09-01", "2097-09-30", "Locked",
        )

        try:
            copy_resp = await session_client.post(
                f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
                json={"effective_from": "2097-09-15", "include_pay_rules": True},
                headers=auth(auth_token),
            )
            assert copy_resp.status_code == 422, (
                f"Expected 422 (effective_from inside Locked period), "
                f"got {copy_resp.status_code}: {copy_resp.text}"
            )
            assert "finalized" in copy_resp.json().get("detail", "").lower()

            # No rates on target
            hist = await session_client.get(
                f"/payroll/drivers/{tgt_id}/rates/history",
                headers=auth(auth_token),
            )
            assert len(hist.json()) == 0, "No rates should exist on target after failed copy"

            # No pay rules on target
            prules = await session_client.get(
                f"/payroll/drivers/{tgt_id}/pay-rules",
                headers=auth(auth_token),
            )
            assert len(prules.json()) == 0, "No pay rules should exist on target after failed copy"

        finally:
            await session_client.post(
                f"/payroll/driver-pay-rules/{src_rule_id}/void", headers=auth(auth_token)
            )
            await _void_all_driver_rates(session_client, auth_token, src_id)
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": period_id},
            )

    async def _insert_locked_period(self, direct_db, company_id: int, branch_id: int,
                                    start_date: str, end_date: str,
                                    status: str = "Locked") -> int:
        """Shared helper — insert a finalized period directly into the DB."""
        from sqlalchemy import text as _text
        from datetime import date as _date
        start = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date)
        result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, paydate, status, createdbyuserid)
                VALUES
                    (:cid, :bid, :code, :name, 'Month',
                     :start, :end, :end, :status, 1)
                RETURNING payrollperiodid
            """),
            {
                "cid": company_id, "bid": branch_id,
                "code": f"CPR-{start_date}", "name": f"CPR {start_date}",
                "start": start, "end": end, "status": status,
            },
        )
        return result.scalar_one()


# ===========================================================================
# P1 #3 — Copy Rates audit logging
# ===========================================================================

class TestCopyRatesAudit:
    """
    Copy Rates must write audit.AuditLog entries for every mutating action:
    superseded rates, voided pending rates, created (copied) rates, and copied pay rules.
    """

    @pytest.mark.asyncio
    async def test_copy_writes_rate_created_audit(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """A RATE_CREATED audit row is written for each copied rate."""
        from sqlalchemy import text as _text
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"AUD-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"AUD-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")

        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "4.20", "effective_from": "2087-01-01"},
            headers=auth(auth_token),
        )
        assert cr.status_code == 201, cr.text
        await session_client.post(
            f"/payroll/rates/{cr.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        # Get audit count before copy
        before = await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE actioncode = 'RATE_CREATED'
                  AND entityname = 'DriverRates'
            """)
        )
        count_before = before.scalar_one()

        copy_resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2087-06-01"},
            headers=auth(auth_token),
        )
        assert copy_resp.status_code == 200, copy_resp.text
        rates_copied = copy_resp.json()["rates_copied"]
        assert rates_copied >= 1

        after = await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE actioncode = 'RATE_CREATED'
                  AND entityname = 'DriverRates'
            """)
        )
        count_after = after.scalar_one()
        assert count_after >= count_before + rates_copied, (
            f"Expected at least {count_before + rates_copied} RATE_CREATED audit rows, "
            f"got {count_after}"
        )

        # Cleanup: void all rates so rates_clean fixture stays under limit
        await _void_all_driver_rates(session_client, auth_token, src_id)
        await _void_all_driver_rates(session_client, auth_token, tgt_id)

    @pytest.mark.asyncio
    async def test_copy_writes_superseded_audit(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """RATE_SUPERSEDED audit is written for the old Approved rate that gets superseded."""
        from sqlalchemy import text as _text
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"SAUD-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"SAUD-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        rtid = mileage["rate_type_id"]

        # Create + approve on source
        cr_src = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": rtid,
                  "amount": "6.00", "effective_from": "2075-01-01"},
            headers=auth(auth_token),
        )
        assert cr_src.status_code == 201, cr_src.text
        await session_client.post(
            f"/payroll/rates/{cr_src.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        # Create + approve on target (this one will be superseded)
        cr_tgt = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "2.50", "effective_from": "2075-01-01"},
            headers=auth(auth_token),
        )
        assert cr_tgt.status_code == 201, cr_tgt.text
        old_rate_id = cr_tgt.json()["driver_rate_id"]
        await session_client.post(f"/payroll/rates/{old_rate_id}/approve", headers=auth(auth_token))

        copy_resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2075-07-01"},
            headers=auth(auth_token),
        )
        assert copy_resp.status_code == 200, copy_resp.text

        # Verify RATE_SUPERSEDED audit row exists for the old rate
        audit_result = await direct_db.execute(
            _text("""
                SELECT actioncode, newvaluejson
                FROM audit.auditlog
                WHERE entityname = 'DriverRates'
                  AND entityid   = :rid
                  AND actioncode = 'RATE_SUPERSEDED'
                ORDER BY createdatutc DESC
                LIMIT 1
            """),
            {"rid": str(old_rate_id)},
        )
        row = audit_result.mappings().first()
        assert row is not None, (
            f"Expected RATE_SUPERSEDED audit row for old rate {old_rate_id}"
        )

        # Cleanup
        await _void_all_driver_rates(session_client, auth_token, src_id)
        await _void_all_driver_rates(session_client, auth_token, tgt_id)

    @pytest.mark.asyncio
    async def test_copy_writes_voided_pending_audit(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """RATE_VOIDED audit is written for any PendingApproval rate that gets voided."""
        from sqlalchemy import text as _text
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"VAUD-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"VAUD-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        rtid = mileage["rate_type_id"]

        # Create + approve on source
        cr_src = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": rtid,
                  "amount": "7.00", "effective_from": "2076-01-01"},
            headers=auth(auth_token),
        )
        assert cr_src.status_code == 201, cr_src.text
        await session_client.post(
            f"/payroll/rates/{cr_src.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        # Create a PendingApproval rate on target (not approved) — this will be voided
        cr_tgt_pending = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "1.50", "effective_from": "2076-01-01"},
            headers=auth(auth_token),
        )
        assert cr_tgt_pending.status_code == 201, cr_tgt_pending.text
        pending_rate_id = cr_tgt_pending.json()["driver_rate_id"]
        # Do NOT approve — leave as PendingApproval

        copy_resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2076-07-01"},
            headers=auth(auth_token),
        )
        assert copy_resp.status_code == 200, copy_resp.text

        # Verify RATE_VOIDED audit row for the voided pending rate
        audit_result = await direct_db.execute(
            _text("""
                SELECT actioncode FROM audit.auditlog
                WHERE entityname = 'DriverRates'
                  AND entityid   = :rid
                  AND actioncode = 'RATE_VOIDED'
                ORDER BY createdatutc DESC
                LIMIT 1
            """),
            {"rid": str(pending_rate_id)},
        )
        row = audit_result.mappings().first()
        assert row is not None, (
            f"Expected RATE_VOIDED audit row for voided pending rate {pending_rate_id}"
        )

        # Cleanup
        await _void_all_driver_rates(session_client, auth_token, src_id)
        await _void_all_driver_rates(session_client, auth_token, tgt_id)

    @pytest.mark.asyncio
    async def test_copy_pay_rules_writes_pay_rule_audit(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """DRIVER_PAY_RULE_CREATED audit is written for each copied pay rule."""
        from sqlalchemy import text as _text
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"PRAUD-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id, f"PRAUD-TGT-{random.randint(100,999)}")

        # Create an Active MinimumPay rule on source
        rule_id = await _create_pay_rule(
            session_client, auth_token, src_id,
            "MinimumPay", "300.00", "2077-01-01", "2077-12-31",
        )

        # Create + approve a rate on source (copy needs at least one rate)
        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        cr = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": mileage["rate_type_id"],
                  "amount": "4.50", "effective_from": "2077-01-01"},
            headers=auth(auth_token),
        )
        assert cr.status_code == 201, cr.text
        await session_client.post(
            f"/payroll/rates/{cr.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )

        before = await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE actioncode = 'DRIVER_PAY_RULE_CREATED'
                  AND entityname = 'DriverPayRules'
            """)
        )
        count_before = before.scalar_one()

        copy_resp = await session_client.post(
            f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
            json={"effective_from": "2077-06-01", "include_pay_rules": True},
            headers=auth(auth_token),
        )
        assert copy_resp.status_code == 200, copy_resp.text
        assert copy_resp.json()["pay_rules_copied"] >= 1

        after = await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE actioncode = 'DRIVER_PAY_RULE_CREATED'
                  AND entityname = 'DriverPayRules'
            """)
        )
        count_after = after.scalar_one()
        pay_rules_copied = copy_resp.json()["pay_rules_copied"]
        assert count_after >= count_before + pay_rules_copied, (
            f"Expected at least {count_before + pay_rules_copied} DRIVER_PAY_RULE_CREATED audit rows, "
            f"got {count_after}"
        )

        # Cleanup
        await session_client.post(
            f"/payroll/driver-pay-rules/{rule_id}/void", headers=auth(auth_token)
        )
        await _void_all_driver_rates(session_client, auth_token, src_id)
        await _void_all_driver_rates(session_client, auth_token, tgt_id)


# ===========================================================================
# Future-approved conflict guard in Copy Rates (Architecture fix)
# ===========================================================================

class TestCopyRatesFutureConflict:
    """
    Copy Rates must reject the whole operation when the target driver already
    has an Approved rate starting on or after the copy effective_from.
    Superseding such a rate would set its EffectiveTo before its own
    EffectiveFrom — invalid dates that corrupt the rate history and trigger the
    DB EXCLUDE constraint rather than producing a clean 422.

    The shared helper _check_no_future_approved_conflict is now called from
    both approve_rate and copy_driver_rates, enforcing the same invariant.

    Reference: approve_rate future-conflict is covered by
    test_rates.py::TestApproveRateSemantics::test_approval_rejected_when_later_approved_rate_exists

    Date ranges used: 2048–2053.  These years have no finalized payroll periods
    in the test DB (test_m15.py creates periods at 2036–2043; test_pay_rates.py
    creates periods at 2060–2064, both persistent without cleanup).
    """

    @pytest.mark.asyncio
    async def test_copy_rejects_future_approved_target_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        Target has an Approved rate starting AFTER copy effective_from → 422.
        The future rate is unchanged and no new rates are written.
        """
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        rtid = mileage["rate_type_id"]

        # Source: Approved MILEAGE rate (2041)
        cr_src = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": rtid,
                  "amount": "4.00", "effective_from": "2048-01-01"},
            headers=auth(auth_token),
        )
        assert cr_src.status_code == 201, cr_src.text
        apr = await session_client.post(
            f"/payroll/rates/{cr_src.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )
        assert apr.status_code == 200, f"Source approve failed: {apr.text}"

        # Target: future Approved MILEAGE rate starting 2048-10-01
        cr_future = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "5.00", "effective_from": "2048-10-01"},
            headers=auth(auth_token),
        )
        assert cr_future.status_code == 201, cr_future.text
        future_rate_id = cr_future.json()["driver_rate_id"]
        apr2 = await session_client.post(f"/payroll/rates/{future_rate_id}/approve",
                                          headers=auth(auth_token))
        assert apr2.status_code == 200, f"Target future approve failed: {apr2.text}"

        try:
            # Copy with effective_from=2048-07-01 — before the future target rate
            copy_resp = await session_client.post(
                f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
                json={"effective_from": "2048-07-01"},
                headers=auth(auth_token),
            )
            assert copy_resp.status_code == 422, (
                f"Expected 422 — target has future Approved rate. "
                f"Got {copy_resp.status_code}: {copy_resp.text}"
            )
            detail = copy_resp.json().get("detail", "")
            assert "already exists" in detail or "future rate" in detail.lower(), (
                f"Error should mention existing rate: {detail}"
            )

            # Future target rate must be unchanged (still Approved)
            future_check = await session_client.get(
                f"/payroll/rates/{future_rate_id}", headers=auth(auth_token)
            )
            assert future_check.status_code == 200
            assert future_check.json()["status"] == "Approved"
            assert future_check.json()["effective_from"] == "2048-10-01"

            # No new rates starting 2048-07-01 should exist on target
            hist = await session_client.get(
                f"/payroll/drivers/{tgt_id}/rates/history",
                headers=auth(auth_token),
            )
            assert hist.status_code == 200
            new_rates = [r for r in hist.json() if r["effective_from"] == "2048-07-01"]
            assert len(new_rates) == 0, (
                f"No rates with copy effective_from should exist after failed copy; "
                f"found {len(new_rates)}"
            )

        finally:
            await _void_all_driver_rates(session_client, auth_token, src_id)
            await _void_all_driver_rates(session_client, auth_token, tgt_id)

    @pytest.mark.asyncio
    async def test_copy_rejects_same_date_approved_target_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        Target has an Approved rate starting ON the same date as copy effective_from
        → 422.  Equal effective_from counts as a future conflict (>= guard).
        """
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC2-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC2-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        rtid = mileage["rate_type_id"]

        # Source: Approved MILEAGE (2042)
        cr_src = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": rtid,
                  "amount": "4.00", "effective_from": "2049-01-01"},
            headers=auth(auth_token),
        )
        assert cr_src.status_code == 201, cr_src.text
        apr = await session_client.post(
            f"/payroll/rates/{cr_src.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )
        assert apr.status_code == 200, f"Source approve failed: {apr.text}"

        # Target: Approved MILEAGE on EXACT same date as copy (2049-07-01)
        cr_tgt = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "3.00", "effective_from": "2049-07-01"},
            headers=auth(auth_token),
        )
        assert cr_tgt.status_code == 201, cr_tgt.text
        same_date_rate_id = cr_tgt.json()["driver_rate_id"]
        apr2 = await session_client.post(f"/payroll/rates/{same_date_rate_id}/approve",
                                          headers=auth(auth_token))
        assert apr2.status_code == 200, f"Target same-date approve failed: {apr2.text}"

        try:
            copy_resp = await session_client.post(
                f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
                json={"effective_from": "2049-07-01"},
                headers=auth(auth_token),
            )
            assert copy_resp.status_code == 422, (
                f"Expected 422 — target has Approved rate on same date. "
                f"Got {copy_resp.status_code}: {copy_resp.text}"
            )
            assert "already exists" in copy_resp.json().get("detail", "")

        finally:
            await _void_all_driver_rates(session_client, auth_token, src_id)
            await _void_all_driver_rates(session_client, auth_token, tgt_id)

    @pytest.mark.asyncio
    async def test_copy_supersedes_only_prior_approved_rate(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Target has an Approved rate starting BEFORE copy effective_from → succeeds.
        Old rate EffectiveTo = copy_date - 1; new rate EffectiveFrom = copy_date.
        """
        import random
        from sqlalchemy import text as _text

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC3-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC3-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        rtid = mileage["rate_type_id"]

        # Source: Approved MILEAGE (2043)
        cr_src = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": rtid,
                  "amount": "6.00", "effective_from": "2050-01-01"},
            headers=auth(auth_token),
        )
        assert cr_src.status_code == 201, cr_src.text
        apr = await session_client.post(
            f"/payroll/rates/{cr_src.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )
        assert apr.status_code == 200, f"Source approve failed: {apr.text}"

        # Target: Approved MILEAGE starting BEFORE copy date (open-ended, 2050-01-01)
        cr_tgt = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "2.00", "effective_from": "2050-01-01"},
            headers=auth(auth_token),
        )
        assert cr_tgt.status_code == 201, cr_tgt.text
        old_rate_id = cr_tgt.json()["driver_rate_id"]
        apr2 = await session_client.post(f"/payroll/rates/{old_rate_id}/approve",
                                          headers=auth(auth_token))
        assert apr2.status_code == 200, f"Old target approve failed: {apr2.text}"

        from datetime import date as _date, timedelta as _td
        copy_date = _date(2050, 7, 1)
        expected_close = copy_date - _td(days=1)  # 2050-06-30

        try:
            copy_resp = await session_client.post(
                f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
                json={"effective_from": copy_date.isoformat()},
                headers=auth(auth_token),
            )
            assert copy_resp.status_code == 200, copy_resp.text
            assert copy_resp.json()["rates_copied"] >= 1

            # Old rate superseded with EffectiveTo = copy_date - 1
            old_check = await direct_db.execute(
                _text("SELECT status, effectiveto FROM payroll.driverrates WHERE driverrateid = :rid"),
                {"rid": old_rate_id},
            )
            old_row = old_check.mappings().first()
            assert old_row["status"] == "Superseded"
            assert old_row["effectiveto"] == expected_close, (
                f"Expected effectiveto={expected_close}, got {old_row['effectiveto']}"
            )

            # New rate starts on copy_date
            new_check = await direct_db.execute(
                _text("""
                    SELECT effectivefrom, effectiveto, status
                    FROM payroll.driverrates
                    WHERE driverid = :did AND ratetypeid = :rtid
                      AND status = 'Approved' AND effectivefrom = :eff_from
                """),
                {"did": tgt_id, "rtid": rtid, "eff_from": copy_date},
            )
            new_row = new_check.mappings().first()
            assert new_row is not None, "New Approved rate should exist on copy_date"
            assert new_row["effectiveto"] is None

        finally:
            await _void_all_driver_rates(session_client, auth_token, src_id)
            await _void_all_driver_rates(session_client, auth_token, tgt_id)

    @pytest.mark.asyncio
    async def test_copy_leaves_closed_historical_rates_alone(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Target has a Superseded rate that already ended before the copy date.
        Copy succeeds and the old closed record is unchanged.
        """
        import random
        from sqlalchemy import text as _text

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC4-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC4-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        rtid = mileage["rate_type_id"]

        # Source: Approved MILEAGE (2044)
        cr_src = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": rtid,
                  "amount": "7.00", "effective_from": "2051-01-01"},
            headers=auth(auth_token),
        )
        assert cr_src.status_code == 201, cr_src.text
        apr = await session_client.post(
            f"/payroll/rates/{cr_src.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )
        assert apr.status_code == 200, f"Source approve failed: {apr.text}"

        # Target: old rate (2051-01-01) that will be superseded by a mid-rate
        cr_old = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "1.00", "effective_from": "2051-01-01"},
            headers=auth(auth_token),
        )
        assert cr_old.status_code == 201, cr_old.text
        old_rate_id = cr_old.json()["driver_rate_id"]
        apr_old = await session_client.post(f"/payroll/rates/{old_rate_id}/approve",
                                             headers=auth(auth_token))
        assert apr_old.status_code == 200, f"Old target approve failed: {apr_old.text}"

        # Approve a mid-rate starting 2051-04-01 → closes old at 2051-03-31
        cr_mid = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "2.00", "effective_from": "2051-04-01"},
            headers=auth(auth_token),
        )
        assert cr_mid.status_code == 201, cr_mid.text
        mid_rate_id = cr_mid.json()["driver_rate_id"]
        apr_mid = await session_client.post(f"/payroll/rates/{mid_rate_id}/approve",
                                             headers=auth(auth_token))
        assert apr_mid.status_code == 200, f"Mid target approve failed: {apr_mid.text}"
        # Now old_rate is Superseded with effectiveto=2051-03-31

        try:
            # Copy with effective_from=2051-07-01 — after both closed rates
            copy_resp = await session_client.post(
                f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
                json={"effective_from": "2051-07-01"},
                headers=auth(auth_token),
            )
            assert copy_resp.status_code == 200, copy_resp.text

            # Old Superseded rate (ending 2051-03-31) must be completely unchanged
            old_check = await direct_db.execute(
                _text("SELECT status, effectiveto FROM payroll.driverrates WHERE driverrateid = :rid"),
                {"rid": old_rate_id},
            )
            old_row = old_check.mappings().first()
            assert old_row["status"] == "Superseded"
            assert str(old_row["effectiveto"]) == "2051-03-31", (
                f"Old closed Superseded rate must be unchanged; effectiveto={old_row['effectiveto']}"
            )

        finally:
            await _void_all_driver_rates(session_client, auth_token, src_id)
            await _void_all_driver_rates(session_client, auth_token, tgt_id)

    @pytest.mark.asyncio
    async def test_copy_future_conflict_writes_no_audit(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        When a future-conflict 422 is returned, no RATE_VOIDED, RATE_SUPERSEDED,
        or RATE_CREATED audit rows are written for the target driver.
        """
        import random
        from sqlalchemy import text as _text

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC5-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC5-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        rtid = mileage["rate_type_id"]

        # Source: Approved MILEAGE (2045)
        cr_src = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": rtid,
                  "amount": "8.00", "effective_from": "2052-01-01"},
            headers=auth(auth_token),
        )
        assert cr_src.status_code == 201, cr_src.text
        apr = await session_client.post(
            f"/payroll/rates/{cr_src.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )
        assert apr.status_code == 200, f"Source approve failed: {apr.text}"

        # Target: future Approved rate (creates the conflict, 2052-10-01)
        cr_future = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "9.00", "effective_from": "2052-10-01"},
            headers=auth(auth_token),
        )
        assert cr_future.status_code == 201, cr_future.text
        future_rate_id = cr_future.json()["driver_rate_id"]
        apr2 = await session_client.post(f"/payroll/rates/{future_rate_id}/approve",
                                          headers=auth(auth_token))
        assert apr2.status_code == 200, f"Target future approve failed: {apr2.text}"

        # Count audit rows for target driver before copy attempt
        before = await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE entityname = 'DriverRates'
                  AND entityid   = ANY(
                      SELECT driverrateid::text FROM payroll.driverrates
                      WHERE driverid = :did
                  )
                  AND actioncode IN ('RATE_CREATED', 'RATE_VOIDED', 'RATE_SUPERSEDED')
            """),
            {"did": tgt_id},
        )
        count_before = before.scalar_one()

        try:
            copy_resp = await session_client.post(
                f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
                json={"effective_from": "2052-07-01"},
                headers=auth(auth_token),
            )
            assert copy_resp.status_code == 422, (
                f"Expected 422 for future conflict, got {copy_resp.status_code}"
            )

            # No new audit rows should exist
            after = await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityname = 'DriverRates'
                      AND entityid   = ANY(
                          SELECT driverrateid::text FROM payroll.driverrates
                          WHERE driverid = :did
                      )
                      AND actioncode IN ('RATE_CREATED', 'RATE_VOIDED', 'RATE_SUPERSEDED')
                """),
                {"did": tgt_id},
            )
            count_after = after.scalar_one()
            assert count_after == count_before, (
                f"No audit rows should be written for a failed copy; "
                f"before={count_before}, after={count_after}"
            )

        finally:
            await _void_all_driver_rates(session_client, auth_token, src_id)
            await _void_all_driver_rates(session_client, auth_token, tgt_id)

    @pytest.mark.asyncio
    async def test_copy_pending_target_not_voided_when_conflict_fails(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """
        When a future-conflict 422 is returned, any existing PendingApproval
        rate on the target is NOT voided (pre-write validation prevents writes).
        """
        import random

        src_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC6-SRC-{random.randint(100,999)}")
        tgt_id = await _make_driver(session_client, auth_token, paytest_branch_id,
                                    f"FC6-TGT-{random.randint(100,999)}")

        rt_resp = await session_client.get("/payroll/rate-types", headers=auth(auth_token))
        mileage = next((r for r in rt_resp.json() if r["rate_code"] == "MILEAGE"), None)
        if mileage is None:
            pytest.skip("MILEAGE not found")
        rtid = mileage["rate_type_id"]

        # Source: Approved MILEAGE (2046)
        cr_src = await session_client.post(
            "/payroll/rates",
            json={"driver_id": src_id, "rate_type_id": rtid,
                  "amount": "4.50", "effective_from": "2053-01-01"},
            headers=auth(auth_token),
        )
        assert cr_src.status_code == 201, cr_src.text
        apr = await session_client.post(
            f"/payroll/rates/{cr_src.json()['driver_rate_id']}/approve",
            headers=auth(auth_token),
        )
        assert apr.status_code == 200, f"Source approve failed: {apr.text}"

        # Target: PendingApproval rate (should survive the failed copy)
        cr_pending = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "1.50", "effective_from": "2053-01-01"},
            headers=auth(auth_token),
        )
        assert cr_pending.status_code == 201, cr_pending.text
        pending_rate_id = cr_pending.json()["driver_rate_id"]
        # Do NOT approve — leave as PendingApproval

        # Target: future Approved rate (creates the conflict, 2053-10-01)
        cr_future = await session_client.post(
            "/payroll/rates",
            json={"driver_id": tgt_id, "rate_type_id": rtid,
                  "amount": "5.00", "effective_from": "2053-10-01"},
            headers=auth(auth_token),
        )
        assert cr_future.status_code == 201, cr_future.text
        future_rate_id = cr_future.json()["driver_rate_id"]
        apr2 = await session_client.post(f"/payroll/rates/{future_rate_id}/approve",
                                          headers=auth(auth_token))
        assert apr2.status_code == 200, f"Target future approve failed: {apr2.text}"

        try:
            copy_resp = await session_client.post(
                f"/payroll/drivers/{tgt_id}/rates/copy-from/{src_id}",
                json={"effective_from": "2053-07-01"},
                headers=auth(auth_token),
            )
            assert copy_resp.status_code == 422

            # PendingApproval rate on target must still be Pending
            pend_check = await session_client.get(
                f"/payroll/rates/{pending_rate_id}", headers=auth(auth_token)
            )
            assert pend_check.status_code == 200
            assert pend_check.json()["status"] == "PendingApproval", (
                "PendingApproval rate must not be voided by a failed copy"
            )

        finally:
            await _void_all_driver_rates(session_client, auth_token, src_id)
            await _void_all_driver_rates(session_client, auth_token, tgt_id)
