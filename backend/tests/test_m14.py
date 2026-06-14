"""
M14 integration tests — Period Pay.

Period Pay lines are stored in PayrollDraftLines with WorkDate = NULL.
They represent period-level lump-sum amounts (Bonus, Adjustment, custom items).

Test structure
--------------
Session-scoped fixtures create:
  - A custom EnteredAmount Period pay item (M14_BONUS_CUSTOM).

Function-scoped fixtures create/cancel periods around each test.

All entry tests use PAYTEST branch on period dates in 2034 (avoids any
conflicts with M13a/M13b/M13c tests which use 2026-2033 dates).

System period items used:
  - "Bonus"      → system BONUS (RateBehavior='Fixed', ItemScope='Period')
  - "Adjustment" → system ADJUSTMENT (RateBehavior='Fixed', ItemScope='Period')
Both default to IsDefaultBranchActive=FALSE so tests activate them via the
session fixture.
"""
import pytest
import pytest_asyncio
import httpx
from decimal import Decimal


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
    start: str = "2034-01-01",
    end: str = "2034-01-07",
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


async def _approve_period_via_review(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
) -> None:
    """Submit an Open period for review and approve it via the review flow."""
    headers = auth(token)
    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"}, headers=headers,
    )
    assert r.status_code == 200, f"InReview failed: {r.text}"
    review_resp = await client.get("/review/items", headers=headers)
    review_item = next(
        i for i in review_resp.json()
        if i.get("entity_name") == "PayrollPeriods"
        and i.get("entity_id") == str(period_id)
        and i.get("status") == "Pending"
    )
    decide = await client.post(
        f"/review/items/{review_item['review_item_id']}/decide",
        headers=headers, json={"decision": "Approved"},
    )
    assert decide.status_code == 200, f"Approval failed: {decide.text}"


async def _create_custom_period_item(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    code: str,
    name: str,
) -> dict:
    resp = await client.post(
        "/settings/pay-items",
        json={
            "pay_item_code": code,
            "pay_item_name": name,
            "item_scope":    "Period",
            "rate_behavior": "EnteredAmount",
            "category":      "Bonus",
        },
        headers=auth(token),
    )
    assert resp.status_code == 201, f"create item failed: {resp.text}"
    item = resp.json()
    # Activate for branch
    act = await client.patch(
        f"/settings/branches/{branch_id}/pay-items/{item['pay_item_id']}",
        json={"is_active": True},
        headers=auth(token),
    )
    assert act.status_code == 200, f"activate item failed: {act.text}"
    return item


async def _activate_system_period_item(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    code: str,          # DB code e.g. 'BONUS', 'ADJUSTMENT'
) -> None:
    """Activate a system period item (BONUS / ADJUSTMENT) for a branch."""
    items_resp = await client.get(
        f"/settings/branches/{branch_id}/pay-items",
        headers=auth(token),
    )
    assert items_resp.status_code == 200
    for item in items_resp.json():
        if item["pay_item_code"] == code:
            if not item.get("is_active", False):
                await client.patch(
                    f"/settings/branches/{branch_id}/pay-items/{item['pay_item_id']}",
                    json={"is_active": True},
                    headers=auth(token),
                )
            return
    # Item not in branch list yet — find it globally and activate
    all_resp = await client.get("/settings/pay-items", headers=auth(token))
    for item in all_resp.json():
        if item.get("pay_item_code") == code:
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
async def m14_bonus_activated(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> None:
    """Activate system BONUS item on PAYTEST branch once per test session."""
    await _activate_system_period_item(
        session_client, auth_token, paytest_branch_id, "BONUS"
    )


@pytest_asyncio.fixture(scope="session")
async def m14_adjustment_activated(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> None:
    """Activate system ADJUSTMENT item on PAYTEST branch once per test session."""
    await _activate_system_period_item(
        session_client, auth_token, paytest_branch_id, "ADJUSTMENT"
    )


@pytest_asyncio.fixture(scope="session")
async def m14_custom_period_item(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> dict:
    """Custom EnteredAmount Period item (M14_CUSTOM_BONUS) — created once per session."""
    return await _create_custom_period_item(
        session_client, auth_token, paytest_branch_id,
        "M14_CUSTOM_BONUS", "M14 Custom Period Bonus",
    )


# ---------------------------------------------------------------------------
# Function fixture — fresh open period per test
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def m14_open_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> dict:
    """Open a fresh weekly period (2034-01-01 to 2034-01-07). Cancelled after test."""
    return await _open_period(
        session_client, auth_token, paytest_branch_id,
        start="2034-01-01", end="2034-01-07",
    )


# ===========================================================================
# TestPeriodPayCreate
# ===========================================================================

class TestPeriodPayCreate:
    """Happy path + validation for POST /periods/{id}/period-pay."""

    async def test_add_bonus_period_line(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """POST Bonus period pay line → 201, WorkDate=None, calc=amount, review=False."""
        pid = m14_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "250.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        line = resp.json()
        assert line["work_date"] is None
        assert Decimal(line["calculated_amount"]) == Decimal("250.00")
        assert line["quantity"] == "1.0000"
        assert line["rate_amount"] is None
        assert line["needs_manager_review"] is False
        assert line["status"] == "Active"

    async def test_add_adjustment_negative_amount(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
    ):
        """POST Adjustment → 422 (manual Adjustment is blocked in this version)."""
        pid = m14_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Adjustment", "amount": "-75.50"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, resp.text
        assert "adjustment" in resp.json()["detail"].lower()

    async def test_add_canonical_adjustment_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
    ):
        """POST ADJUSTMENT (canonical all-caps) → 422 (same block applies to both casings)."""
        pid = m14_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "ADJUSTMENT", "amount": "50.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, resp.text
        assert "adjustment" in resp.json()["detail"].lower()

    async def test_add_custom_period_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_custom_period_item: dict,
    ):
        """POST a custom EnteredAmount Period item → 201."""
        pid = m14_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id,
                  "line_type": "M14_CUSTOM_BONUS",
                  "amount": "100.00",
                  "notes": "Safety bonus Q1"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        line = resp.json()
        assert Decimal(line["calculated_amount"]) == Decimal("100.00")
        assert line["notes"] == "Safety bonus Q1"
        assert line["work_date"] is None

    async def test_zero_amount_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """amount=0 is rejected with 422 (schema-level validation)."""
        pid = m14_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "0"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "non-zero" in resp.text.lower() or "zero" in resp.text.lower()

    async def test_daily_item_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
    ):
        """Passing a Daily-scope item ('Miles') to the period-pay endpoint → 422."""
        pid = m14_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Miles", "amount": "50.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "daily" in resp.text.lower() or "period" in resp.text.lower()

    async def test_guaranteed_minimum_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
    ):
        """GUARANTEED_MINIMUM uses Calculated behavior — blocked in M14 with clear message."""
        pid = m14_open_period["payroll_period_id"]
        for code in ("GuaranteedMinimum", "GUARANTEED_MINIMUM"):
            resp = await session_client.post(
                f"/payroll/periods/{pid}/period-pay",
                json={"driver_id": paytest_driver_id, "line_type": code, "amount": "500.00"},
                headers=auth(auth_token),
            )
            assert resp.status_code == 422, f"Expected 422 for {code!r}: {resp.text}"
            detail = resp.text.lower()
            assert "calculated" in detail or "pay-rule" in detail or "automated" in detail

    async def test_locked_period_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
        m14_adjustment_activated,
        direct_db,
    ):
        """Period in non-entry status (Draft) → 422 (cannot add period pay lines)."""
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p_resp = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-02-01", "end_date": "2034-02-07"},
            headers=auth(auth_token),
        )
        assert p_resp.status_code == 201
        pid = p_resp.json()["payroll_period_id"]
        # Period is in Draft status — not Open or InReview

        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "open" in resp.text.lower() or "inreview" in resp.text.lower()

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_inactive_item_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """A Period item that has not been activated for the branch → 422."""
        from decimal import Decimal as D

        # Create a custom period item but do NOT activate it for the branch
        resp = await session_client.post(
            "/settings/pay-items",
            json={
                "pay_item_code": "M14_INACTIVE",
                "pay_item_name": "M14 Inactive Period Item",
                "item_scope":    "Period",
                "rate_behavior": "EnteredAmount",
                "category":      "Bonus",
            },
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        # Do NOT activate — leave as IsDefaultBranchActive=FALSE

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p_resp = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-03-01", "end_date": "2034-03-07"},
            headers=auth(auth_token),
        )
        assert p_resp.status_code == 201
        pid = p_resp.json()["payroll_period_id"]
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        add_resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "M14_INACTIVE", "amount": "50.00"},
            headers=auth(auth_token),
        )
        assert add_resp.status_code == 422
        assert "active" in add_resp.text.lower()

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_period_pay_excluded_from_daily_lines_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """Period pay lines (WorkDate=NULL) do not appear in GET /periods/{id}/lines."""
        pid = m14_open_period["payroll_period_id"]

        # Add a period pay line
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "50.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]

        # The daily lines endpoint must NOT include this period pay line
        daily = await session_client.get(
            f"/payroll/periods/{pid}/lines", headers=auth(auth_token)
        )
        assert daily.status_code == 200
        daily_ids = [l["draft_line_id"] for l in daily.json()]
        assert line_id not in daily_ids, (
            "Period pay line (WorkDate=NULL) must not appear in /lines endpoint"
        )

        # But it must appear in GET /period-pay
        pp = await session_client.get(
            f"/payroll/periods/{pid}/period-pay", headers=auth(auth_token)
        )
        assert pp.status_code == 200
        pp_ids = [l["draft_line_id"] for l in pp.json()]
        assert line_id in pp_ids


# ===========================================================================
# TestPeriodPayUpdate
# ===========================================================================

class TestPeriodPayUpdate:
    """PATCH /periods/{id}/period-pay/{line_id}."""

    async def test_update_amount_recalculates(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """PATCH amount → CalculatedAmount updated to new value immediately."""
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]
        assert Decimal(add.json()["calculated_amount"]) == Decimal("100.00")

        patch = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"amount": "200.00"},
            headers=auth(auth_token),
        )
        assert patch.status_code == 200, patch.text
        assert Decimal(patch.json()["calculated_amount"]) == Decimal("200.00")

    async def test_update_to_zero_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """PATCH amount=0 is rejected (schema-level validation)."""
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]

        patch = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"amount": "0"},
            headers=auth(auth_token),
        )
        assert patch.status_code == 422

    async def test_update_notes_only(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """PATCH notes only → amount unchanged, notes updated."""
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "25.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]
        original_calc = add.json()["calculated_amount"]

        patch = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"notes": "Fuel bonus correction"},
            headers=auth(auth_token),
        )
        assert patch.status_code == 200
        assert patch.json()["calculated_amount"] == original_calc
        assert patch.json()["notes"] == "Fuel bonus correction"

    async def test_update_nonexistent_line_is_404(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        m14_open_period: dict,
    ):
        """PATCH a non-existent line_id → 404."""
        pid = m14_open_period["payroll_period_id"]
        resp = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/999999",
            json={"amount": "50.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_update_legacy_adjustment_line_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
        direct_db,
    ):
        """
        PATCH on a line with linetype='Adjustment' must return 422.
        Legacy ADJUSTMENT rows can exist in the DB but cannot be modified via
        the API — they predate the current per-item workflow.
        """
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p_resp = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-11-01", "end_date": "2034-11-07"},
            headers=auth(auth_token),
        )
        assert p_resp.status_code == 201
        pid = p_resp.json()["payroll_period_id"]
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Inject a legacy Adjustment line directly (API blocks creation of ADJUSTMENT lines)
        result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid,
                     workdate, linetype, linescope, calculatedamount,
                     sourcetype, status, needsmanagerreview, addedbyuserid)
                VALUES
                    ((SELECT companyid FROM core.branches WHERE branchid = :bid),
                     :bid, :pid, :did,
                     NULL, 'Adjustment', 'Period', 50.00,
                     'Manual', 'Active', FALSE,
                     (SELECT userid FROM sec.users WHERE username = 'admin' LIMIT 1))
                RETURNING draftlineid
            """),
            {"bid": paytest_branch_id, "pid": pid, "did": paytest_driver_id},
        )
        row = result.mappings().first()
        assert row is not None, "Failed to inject legacy Adjustment line"
        line_id = row["draftlineid"]

        resp = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"amount": "75.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, (
            f"Legacy ADJUSTMENT line update must be blocked with 422; got {resp.status_code}: {resp.text}"
        )
        assert "adjustment" in resp.text.lower(), (
            f"422 detail must mention ADJUSTMENT; got: {resp.text}"
        )

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_update_canonical_adjustment_line_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        PATCH on a line with linetype='ADJUSTMENT' (canonical all-caps form) must
        return 422.  Canonical rows can be created by the finalization/SYS pipeline
        or by future migrations — the guard must block both mixed-case and all-caps.
        """
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p_resp = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-12-01", "end_date": "2034-12-07"},
            headers=auth(auth_token),
        )
        assert p_resp.status_code == 201
        pid = p_resp.json()["payroll_period_id"]
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Inject a canonical ADJUSTMENT line directly
        result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid,
                     workdate, linetype, linescope, calculatedamount,
                     sourcetype, status, needsmanagerreview, addedbyuserid)
                VALUES
                    ((SELECT companyid FROM core.branches WHERE branchid = :bid),
                     :bid, :pid, :did,
                     NULL, 'ADJUSTMENT', 'Period', 50.00,
                     'Manual', 'Active', FALSE,
                     (SELECT userid FROM sec.users WHERE username = 'admin' LIMIT 1))
                RETURNING draftlineid
            """),
            {"bid": paytest_branch_id, "pid": pid, "did": paytest_driver_id},
        )
        row = result.mappings().first()
        assert row is not None, "Failed to inject canonical ADJUSTMENT line"
        line_id = row["draftlineid"]

        resp = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"amount": "75.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, (
            f"Canonical ADJUSTMENT line update must be blocked with 422; got {resp.status_code}: {resp.text}"
        )
        assert "adjustment" in resp.text.lower(), (
            f"422 detail must mention ADJUSTMENT; got: {resp.text}"
        )

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_update_bonus_line_still_works(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
        m14_open_period: dict,
    ):
        """
        Confirm the ADJUSTMENT block does not regress BONUS line updates.
        A normal BONUS line must still be patchable (regression guard).
        """
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, f"Create Bonus line: {add.text}"
        line_id = add.json()["draft_line_id"]

        patch = await session_client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"amount": "150.00"},
            headers=auth(auth_token),
        )
        assert patch.status_code == 200, (
            f"BONUS line update must still work after ADJUSTMENT block; got {patch.status_code}: {patch.text}"
        )
        assert Decimal(patch.json()["calculated_amount"]) == Decimal("150.00")


# ===========================================================================
# TestPeriodPayVoid
# ===========================================================================

class TestPeriodPayVoid:
    """DELETE /periods/{id}/period-pay/{line_id}."""

    async def test_void_period_pay_line(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """DELETE a period pay line → status becomes 'Void'."""
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "50.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]

        void_resp = await session_client.delete(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            headers=auth(auth_token),
        )
        assert void_resp.status_code == 200
        assert void_resp.json()["status"] == "Void"

    async def test_void_idempotent(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """Voiding an already-voided line succeeds without error (idempotent)."""
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "30.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]

        # First void
        r1 = await session_client.delete(
            f"/payroll/periods/{pid}/period-pay/{line_id}", headers=auth(auth_token)
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "Void"

        # Second void — must not error
        r2 = await session_client.delete(
            f"/payroll/periods/{pid}/period-pay/{line_id}", headers=auth(auth_token)
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "Void"


# ===========================================================================
# TestPeriodPayFinalization
# ===========================================================================

class TestPeriodPayFinalization:
    """Period Pay lines finalize correctly and appear in PayrollFinalLines."""

    async def test_finalization_includes_bonus_period_line(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
    ):
        """
        A period with a Bonus Period Pay line finalizes correctly.
        The bonus appears in /final-lines with WorkDate=None and correct FinalAmount.
        """
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p_resp = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-04-01", "end_date": "2034-04-07"},
            headers=auth(auth_token),
        )
        assert p_resp.status_code == 201
        pid = p_resp.json()["payroll_period_id"]

        # Open → add Bonus line → InReview → Approved → Finalize
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "300.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201

        await _approve_period_via_review(session_client, auth_token, pid)

        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin.status_code == 200, f"Finalize failed: {fin.text}"

        # Check final lines
        final_lines_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        assert final_lines_resp.status_code == 200
        final_lines = final_lines_resp.json()
        bonus_lines = [
            fl for fl in final_lines
            if fl["line_type"] == "BONUS" and fl["work_date"] is None
        ]
        assert len(bonus_lines) == 1, f"Expected 1 Bonus final line, got: {bonus_lines}"
        assert Decimal(bonus_lines[0]["final_amount"]) == Decimal("300.00")

    async def test_adjustment_blocked_cannot_reach_finalization(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """ADJUSTMENT is blocked at creation time and cannot reach finalization."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p_resp = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-05-01", "end_date": "2034-05-07"},
            headers=auth(auth_token),
        )
        assert p_resp.status_code == 201
        pid = p_resp.json()["payroll_period_id"]

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Adjustment", "amount": "-45.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 422, add.text
        assert "adjustment" in add.json()["detail"].lower()

        # Cleanup: cancel the empty period
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_voided_period_line_excluded_from_finalization(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
    ):
        """
        A voided Period Pay line does not appear in PayrollFinalLines.
        A second non-voided Bonus line keeps the period financeable and
        is used to confirm finalization succeeds; only it appears in final-lines.
        """
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p_resp = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-06-01", "end_date": "2034-06-07"},
            headers=auth(auth_token),
        )
        assert p_resp.status_code == 201
        pid = p_resp.json()["payroll_period_id"]

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Line 1: Bonus $150 — will be voided
        add_bonus = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "150.00"},
            headers=auth(auth_token),
        )
        assert add_bonus.status_code == 201
        bonus_line_id = add_bonus.json()["draft_line_id"]

        # Line 2: Bonus $10 — kept active so the period can be finalized
        add_keep = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "10.00"},
            headers=auth(auth_token),
        )
        assert add_keep.status_code == 201

        # Void the first Bonus before finalizing
        void_resp = await session_client.delete(
            f"/payroll/periods/{pid}/period-pay/{bonus_line_id}", headers=auth(auth_token)
        )
        assert void_resp.status_code == 200

        await _approve_period_via_review(session_client, auth_token, pid)
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin.status_code == 200, fin.text

        final_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        final_lines = final_resp.json()

        # Voided $150 Bonus must NOT appear
        voided_final = [
            fl for fl in final_lines
            if fl["line_type"] == "BONUS" and fl["work_date"] is None
            and Decimal(str(fl["final_amount"])) == Decimal("150.00")
        ]
        assert len(voided_final) == 0, "Voided period pay line must not appear in final lines"

        # Active $10 Bonus MUST appear
        kept_final = [
            fl for fl in final_lines
            if fl["line_type"] == "BONUS" and fl["work_date"] is None
            and Decimal(str(fl["final_amount"])) == Decimal("10.00")
        ]
        assert len(kept_final) == 1, "Active period pay line must appear in final lines"


# ===========================================================================
# TestPeriodPaySafetyGuards
# ===========================================================================

class TestPeriodPaySafetyGuards:
    """
    Guards: approval and finalization must block malformed Period Pay lines
    (calculatedamount=NULL via direct-DB bypass) and must not silently
    produce zero-dollar final lines.
    """

    async def test_period_with_period_pay_approves_cleanly(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
    ):
        """A well-formed Period Pay line does not interfere with InReview→Approved."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p_resp = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-07-01", "end_date": "2034-07-07"},
            headers=auth(auth_token),
        )
        pid = p_resp.json()["payroll_period_id"]

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "200.00"},
            headers=auth(auth_token),
        )
        await _approve_period_via_review(session_client, auth_token, pid)
        # Well-formed period pay line must not block the review flow (approve succeeded)

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_malformed_period_pay_line_blocks_finalization(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
        direct_db,
    ):
        """
        A Period Pay line where calculatedamount was NULL'd via direct DB
        bypass must block /finalize even when period is forced to Approved.
        Silent zero finalization (COALESCE fallback) must never occur.
        """
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p_resp = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-08-01", "end_date": "2034-08-07"},
            headers=auth(auth_token),
        )
        assert p_resp.status_code == 201
        pid = p_resp.json()["payroll_period_id"]

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "500.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]

        # Corrupt the line: set calculatedamount=NULL (keep linescope='Period', review=FALSE)
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET    calculatedamount   = NULL,
                       needsmanagerreview = FALSE
                WHERE  draftlineid = :lid
            """),
            {"lid": line_id},
        )

        # Force period to Approved bypassing the approval guard
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollperiods
                SET    status = 'Approved'
                WHERE  payrollperiodid = :pid
            """),
            {"pid": pid},
        )

        # /finalize must block — silent zero is not allowed for period pay lines
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin.status_code == 422, (
            f"Expected 422 (malformed period pay line) but got {fin.status_code}: {fin.text}"
        )
        assert "resolved" in fin.text.lower() or "zero" in fin.text.lower()

        # Cleanup
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollperiods
                SET    status = 'Cancelled'
                WHERE  payrollperiodid = :pid
            """),
            {"pid": pid},
        )

    async def test_malformed_period_pay_line_blocks_submission(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
        direct_db,
    ):
        """
        A Period Pay line where calculatedamount=NULL and needsmanagerreview=FALSE
        (direct-DB bypass state) must block Open→InReview (M16: guard moved here).
        """
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p_resp = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-09-01", "end_date": "2034-09-07"},
            headers=auth(auth_token),
        )
        assert p_resp.status_code == 201
        pid = p_resp.json()["payroll_period_id"]

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "250.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        line_id = add.json()["draft_line_id"]

        # Corrupt via direct DB
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET    calculatedamount   = NULL,
                       needsmanagerreview = FALSE
                WHERE  draftlineid = :lid
            """),
            {"lid": line_id},
        )

        # Open→InReview must be blocked (M16: guard moved here)
        blocked = await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "InReview"}, headers=auth(auth_token)
        )
        assert blocked.status_code == 422, (
            f"Expected 422 (malformed period pay line) but got {blocked.status_code}: {blocked.text}"
        )

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )


# ===========================================================================
# TestM14SafetyFixes  (Codex review round 2)
# ===========================================================================

class TestM14SafetyFixes:
    """
    Fix 1: LineScope preserved in PayrollFinalLines (migration 0011).
    Fix 2: Period Pay branch activation uses period.start_date, not CURRENT_DATE.
    """

    # -----------------------------------------------------------------------
    # Fix 1: LineScope in the final ledger
    # -----------------------------------------------------------------------

    async def test_period_pay_final_line_has_period_scope(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
    ):
        """Finalized Period Pay Bonus line has line_scope='Period' in final-lines."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-10-01", "end_date": "2034-10-07"},
            headers=auth(auth_token),
        )
        assert p.status_code == 201
        pid = p.json()["payroll_period_id"]
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "175.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        assert add.json()["line_scope"] == "Period"    # draft line carries scope
        await _approve_period_via_review(session_client, auth_token, pid)
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin.status_code == 200, fin.text
        fl = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        bonus = [x for x in fl.json() if x["line_type"] == "BONUS"]
        assert len(bonus) == 1
        assert bonus[0]["line_scope"] == "Period", f"Expected Period scope, got: {bonus[0]}"
        assert bonus[0]["work_date"] is None

    async def test_daily_final_line_has_daily_scope(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """Finalized daily Miles line has line_scope='Daily' in final-lines."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-11-01", "end_date": "2034-11-07"},
            headers=auth(auth_token),
        )
        assert p.status_code == 201
        pid = p.json()["payroll_period_id"]
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )
        # Phase 4C: use PTO_STATUS (non-PerUnit) instead of Miles+rate_amount.
        # The scope test (Daily) applies to any daily line type.
        add = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "PTO_STATUS",
                  "quantity": "1", "work_date": "2034-11-01"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201
        assert add.json()["line_scope"] == "Daily"
        await _approve_period_via_review(session_client, auth_token, pid)
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin.status_code == 200, fin.text
        fl = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        daily_lines = [x for x in fl.json() if x["line_type"] == "PTO_STATUS"]
        assert len(daily_lines) == 1
        assert daily_lines[0]["line_scope"] == "Daily", f"Expected Daily scope, got: {daily_lines[0]}"

    async def test_mixed_period_preserves_scope_in_final_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
    ):
        """Period with daily + period-pay lines: each final line gets its correct scope."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2034-12-01", "end_date": "2034-12-07"},
            headers=auth(auth_token),
        )
        assert p.status_code == 201
        pid = p.json()["payroll_period_id"]
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )
        # Phase 4C: use PTO_STATUS (non-PerUnit) instead of Miles+rate_amount.
        await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "PTO_STATUS",
                  "quantity": "1", "work_date": "2034-12-01"},
            headers=auth(auth_token),
        )
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "100.00"},
            headers=auth(auth_token),
        )
        await _approve_period_via_review(session_client, auth_token, pid)
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin.status_code == 200, fin.text
        fl = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        rows = fl.json()
        assert len(rows) == 2
        by_type = {r["line_type"]: r["line_scope"] for r in rows}
        assert by_type["PTO_STATUS"] == "Daily",  f"PTO_STATUS scope wrong: {by_type}"
        assert by_type["BONUS"]      == "Period", f"Bonus scope wrong: {by_type}"

    # -----------------------------------------------------------------------
    # Fix 2: branch activation uses period.start_date, not CURRENT_DATE
    # -----------------------------------------------------------------------

    async def test_period_pay_activation_respects_period_start_date(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        An item whose effectivefrom > period.start_date is rejected.
        Proves the check uses period.start_date not CURRENT_DATE: if today's date
        were used the item (activated today) would wrongly pass for a 2035 period.
        """
        from sqlalchemy import text as _text

        item_resp = await session_client.post(
            "/settings/pay-items",
            json={"pay_item_code": "M14_DATED",
                  "pay_item_name": "M14 Dated Activation Test",
                  "item_scope": "Period", "rate_behavior": "EnteredAmount",
                  "category": "Bonus"},
            headers=auth(auth_token),
        )
        assert item_resp.status_code == 201, item_resp.text
        item_id = item_resp.json()["pay_item_id"]

        # Activate now (effectivefrom = today by default)
        await session_client.patch(
            f"/settings/branches/{paytest_branch_id}/pay-items/{item_id}",
            json={"is_active": True}, headers=auth(auth_token),
        )
        # Push effectivefrom to 2035-06-01 so the item is NOT active for
        # a period starting 2035-01-01 but IS active today.
        await direct_db.execute(
            _text(
                "UPDATE payroll.branchpayitemconfig "
                "SET effectivefrom = '2035-06-01' "
                "WHERE payitemid = :piid AND branchid = :bid"
            ),
            {"piid": item_id, "bid": paytest_branch_id},
        )

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2035-01-01", "end_date": "2035-01-07"},
            headers=auth(auth_token),
        )
        assert p.status_code == 201
        pid = p.json()["payroll_period_id"]
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Must fail: item not active as of period.start_date 2035-01-01
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "M14_DATED", "amount": "50.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 422, (
            f"Expected 422 (not active at period.start_date) but got {add.status_code}: {add.text}"
        )
        assert "active" in add.text.lower()

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_period_pay_accepted_when_active_at_period_start(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_custom_period_item: dict,
    ):
        """An item active as of period.start_date is accepted, even for future periods."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2035-02-01", "end_date": "2035-02-07"},
            headers=auth(auth_token),
        )
        assert p.status_code == 201
        pid = p.json()["payroll_period_id"]
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id,
                  "line_type": "M14_CUSTOM_BONUS", "amount": "75.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, (
            f"Expected 201 (item active at period.start_date) but got {add.status_code}: {add.text}"
        )
        assert add.json()["line_scope"] == "Period"

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_period_pay_active_today_but_not_at_period_start_is_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Strongest proof that period.start_date is used, not CURRENT_DATE.

        Setup:
          - effectivefrom = 2026-01-01  (in the past — active today, 2026-05-30)
          - period.start_date = 2025-01-01  (before effectivefrom — item NOT yet active)

        With CURRENT_DATE the check sees:  effectivefrom (2026-01-01) <= today (2026-05-30) ✓
            → would ACCEPT the item (wrong)
        With period.start_date it sees:    effectivefrom (2026-01-01) <= 2025-01-01 ✗
            → correctly REJECTS the item (expected 422)
        """
        from sqlalchemy import text as _text

        item_resp = await session_client.post(
            "/settings/pay-items",
            json={"pay_item_code": "M14_TODAY_ACTIVE",
                  "pay_item_name": "M14 Active Today Not At Period Start",
                  "item_scope": "Period", "rate_behavior": "EnteredAmount",
                  "category": "Bonus"},
            headers=auth(auth_token),
        )
        assert item_resp.status_code == 201, item_resp.text
        item_id = item_resp.json()["pay_item_id"]

        # Activate now — creates BranchPayItemConfig with effectivefrom = today
        await session_client.patch(
            f"/settings/branches/{paytest_branch_id}/pay-items/{item_id}",
            json={"is_active": True}, headers=auth(auth_token),
        )
        # Backdate effectivefrom to 2026-01-01: the item IS active today (2026-05-30 >= 2026-01-01)
        # but NOT active for a period starting 2025-01-01 (2026-01-01 > 2025-01-01).
        # A CURRENT_DATE check would PASS; a period.start_date check must FAIL.
        await direct_db.execute(
            _text(
                "UPDATE payroll.branchpayitemconfig "
                "SET effectivefrom = '2026-01-01' "
                "WHERE payitemid = :piid AND branchid = :bid"
            ),
            {"piid": item_id, "bid": paytest_branch_id},
        )

        # Backdated period: start_date = 2025-01-01 (before effectivefrom 2026-01-01)
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2025-01-01", "end_date": "2025-01-07"},
            headers=auth(auth_token),
        )
        assert p.status_code == 201
        pid = p.json()["payroll_period_id"]
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Must be rejected: item active today but NOT active at period.start_date 2025-01-01.
        # If CURRENT_DATE were used this would wrongly return 201.
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id,
                  "line_type": "M14_TODAY_ACTIVE", "amount": "50.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 422, (
            f"Expected 422 (item active today but NOT at period.start_date 2025-01-01). "
            f"If this returned 201 the code is using CURRENT_DATE instead of period.start_date. "
            f"Got {add.status_code}: {add.text}"
        )
        assert "active" in add.text.lower()

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )

    async def test_daily_lines_summary_excludes_period_pay(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
        m14_adjustment_activated,
    ):
        """
        GET /payroll/periods/{id}/lines/summary must NOT include Period Pay lines.
        The summary endpoint filters LineScope = 'Daily' and is intended for
        the daily payroll entry grid — period-level bonuses/adjustments must
        not pollute the daily quantity/amount totals.
        """
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        p = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": "2035-03-01", "end_date": "2035-03-07"},
            headers=auth(auth_token),
        )
        assert p.status_code == 201
        pid = p.json()["payroll_period_id"]
        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Open"}, headers=auth(auth_token)
        )

        # Add a daily line (PTO_STATUS) — should appear in the summary.
        # Phase 4C: rate_amount removed; PTO_STATUS has None behavior (no rate needed).
        await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "PTO_STATUS",
                  "quantity": "1", "work_date": "2035-03-01"},
            headers=auth(auth_token),
        )

        # Add two Period Pay lines — must NOT appear in the summary
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "500.00"},
            headers=auth(auth_token),
        )
        await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Adjustment", "amount": "-50.00"},
            headers=auth(auth_token),
        )

        summary_resp = await session_client.get(
            f"/payroll/periods/{pid}/lines/summary", headers=auth(auth_token)
        )
        assert summary_resp.status_code == 200
        summary = summary_resp.json()

        # Only the daily PTO_STATUS line should appear (not Period Pay).
        line_types_in_summary = {row["line_type"] for row in summary}
        assert "PTO_STATUS" in line_types_in_summary, (
            f"Expected PTO_STATUS in summary, got: {line_types_in_summary}"
        )
        assert "BONUS" not in line_types_in_summary, (
            f"BONUS (Period Pay) must NOT appear in /lines/summary, got: {line_types_in_summary}"
        )
        assert "ADJUSTMENT" not in line_types_in_summary, (
            f"ADJUSTMENT (Period Pay) must NOT appear in /lines/summary, "
            f"got: {line_types_in_summary}"
        )

        # Total quantity in summary should reflect only the daily PTO_STATUS line
        pto_rows = [r for r in summary if r["line_type"] == "PTO_STATUS"]
        assert len(pto_rows) == 1
        assert Decimal(pto_rows[0]["total_quantity"]) == Decimal("1")

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )
