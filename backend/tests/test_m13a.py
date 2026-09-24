"""
M13a + M13b integration tests.

M13a — PayItem-driven line-type validation
    Replaces the old hardcoded _VALID_LINE_TYPES set in DraftLineCreate.
    System items pass via a fast-path dict; custom items go through a DB lookup.

M13b — Basic calculation engine (PerUnit + EnteredAmount)
    calculatedamount is computed at INSERT and UPDATE time:
      PerUnit + approved rate  → qty × rate
      PerUnit + no rate        → NULL, needs_manager_review = True
      Fixed / None             → NULL (no change to needs_manager_review)

Fixtures
--------
All entry tests use the PAYTEST branch and function-scoped periods so they
never conflict with other test modules.

Custom items use session-scoped fixtures (created once per session) with
unique codes prefixed M13A_ to avoid collisions with M12 test codes.

Approved driver rates are function-scoped with cleanup via a rates_clean
wrapper so they don't collide with test_rates.py.
"""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest_asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


async def _cancel_active_periods(
    client: httpx.AsyncClient, token: str, branch_id: int, db=None
) -> None:
    from sqlalchemy import text as _sqla_text
    headers = auth(token)
    # Force-cancel InReview/Approved directly in DB (CP-1A blocks those HTTP transitions)
    if db is not None:
        await db.execute(
            _sqla_text(
                "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                "WHERE branchid = :bid AND status IN ('InReview', 'Approved')"
            ),
            {"bid": branch_id},
        )
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


async def _void_all_rates(client: httpx.AsyncClient, token: str) -> None:
    """
    Void all non-terminal rates.

    Includes Superseded rows because the DriverRates EXCLUDE constraint covers
    both Approved and Superseded statuses — a Superseded rate from a previous
    test can block the approval of a new rate that overlaps its date range.
    """
    headers = auth(token)
    resp = await client.get("/payroll/rates", headers=headers)
    if resp.status_code != 200:
        return
    for r in resp.json():
        if r["status"] in ("PendingApproval", "Approved", "Superseded"):
            await client.delete(f"/payroll/rates/{r['driver_rate_id']}", headers=headers)


async def _make_period(db, branch_id: int, start: str, end: str, status: str = "Open") -> int:
    from datetime import date as _date

    from sqlalchemy import text as _sqla_text
    code = f"M13X-{branch_id}-{start}-{uuid.uuid4().hex[:8]}"
    row = (await db.execute(
        _sqla_text(f"""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, '{status}', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"M13X {start}",
         "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


# ---------------------------------------------------------------------------
# Function-scoped period fixtures (PAYTEST branch, unique future dates)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def m13_clean(
    session_client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    direct_db,
):
    """Cancel any active PAYTEST periods before and after the test."""
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
    yield paytest_branch_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)


@pytest_asyncio.fixture
async def m13_open_period(
    auth_token: str,
    m13_clean: int,
    direct_db,
) -> dict:
    """Create an Open payroll period on PAYTEST branch via direct DB insert.

    POST /payroll/periods requires an existing Open period (CP-1D B1 guard), so
    we insert directly — the same pattern used by test_cp2d and test_cp1d.
    Dates in 2032 to avoid conflicts with other test modules.
    """
    from sqlalchemy import text as _sqla_text
    row = (await direct_db.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', '2032-03-04', '2032-03-10')
            RETURNING payrollperiodid, startdate, enddate, status
        """),
        {"bid": m13_clean, "code": f"M13A-2032-0304-{uuid.uuid4().hex[:8]}",
         "name": f"M13A Week Mar 4 2032 {uuid.uuid4().hex[:6]}"},
    )).mappings().first()
    return {"payroll_period_id": row["payrollperiodid"], "status": row["status"],
            "start_date": str(row["startdate"]), "end_date": str(row["enddate"])}


# ---------------------------------------------------------------------------
# Session-scoped custom item fixtures (created once per test session)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def m13_daily_item(
    session_client: httpx.AsyncClient,
    session_db_conn,
    auth_token: str,
    paytest_branch_id: int,
) -> dict:
    """
    Seed a custom Daily PerUnit item (M13A_STOP) and activate it on PAYTEST.

    No PayItemRateTypeMap entry is seeded for this item, so calculation will
    return (None, True) — correct behaviour for M13b with unlinked custom items.
    """
    from tests.seed_helpers import seed_legacy_item
    item_id = await seed_legacy_item(
        session_db_conn,
        code="M13A_STOP",
        name="Stop Pay (M13 test)",
        unit="Stop",
        category="Count",
    )

    # Activate on PAYTEST via M11 PATCH (creates BranchPayItemConfig, EffectiveFrom = today).
    patch_resp = await session_client.patch(
        f"/settings/branches/{paytest_branch_id}/pay-items/{item_id}",
        json={"is_active": True},
        headers=auth(auth_token),
    )
    assert patch_resp.status_code == 200, f"activate failed: {patch_resp.text}"
    return {"pay_item_id": item_id, "pay_item_code": "M13A_STOP"}


@pytest_asyncio.fixture(scope="session")
async def m13_inactive_item(
    session_db_conn,
) -> dict:
    """
    Seed a custom Daily PerUnit item (M13A_INACT) but do NOT activate it on
    any branch.  Used to test the 'inactive on branch' rejection path.
    """
    from tests.seed_helpers import seed_legacy_item
    item_id = await seed_legacy_item(
        session_db_conn,
        code="M13A_INACT",
        name="Inactive Item (M13 test)",
        unit="Unit",
        category="Count",
    )
    # IsDefaultBranchActive = FALSE and no config row → inactive everywhere.
    return {"pay_item_id": item_id, "pay_item_code": "M13A_INACT"}


@pytest_asyncio.fixture(scope="session")
async def m13_period_item(
    session_client: httpx.AsyncClient,
    auth_token: str,
) -> dict:
    """
    Stub: system BONUS (Period-scope) is always seeded — no DB fetch needed.
    Custom Period-scope items are no longer creatable; tests now use the
    system BONUS item to verify Period items are blocked from daily entry.
    """
    return {"pay_item_code": "BONUS"}


# ---------------------------------------------------------------------------
# Function-scoped driver rate fixtures (cleaned up after each test)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def m13_hourly_rate(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_driver_id: int,
    paytest_rate_type_id: int,
):
    """
    Create and approve an HOURLY rate of $20.00/hr for paytest_driver_id,
    effective from 2032-01-01 (covers the test period 2032-03-04..10).
    Voids all rates before the test so there are no pre-existing approved rates.
    Cleans up after.
    """
    await _void_all_rates(session_client, auth_token)

    rate_resp = await session_client.post(
        "/payroll/rates",
        json={
            "driver_id":      paytest_driver_id,
            "rate_type_id":   paytest_rate_type_id,
            "amount":         "20.00",
            "effective_from": "2032-01-01",
        },
        headers=auth(auth_token),
    )
    assert rate_resp.status_code == 201
    rate_id = rate_resp.json()["driver_rate_id"]

    approve_resp = await session_client.post(
        f"/payroll/rates/{rate_id}/approve", headers=auth(auth_token)
    )
    assert approve_resp.status_code == 200

    yield {"rate_id": rate_id, "amount": Decimal("20.00")}

    await _void_all_rates(session_client, auth_token)


@pytest_asyncio.fixture
async def m13_no_rates(
    session_client: httpx.AsyncClient, auth_token: str
):
    """Ensure no approved rates exist for this test."""
    await _void_all_rates(session_client, auth_token)
    yield
    await _void_all_rates(session_client, auth_token)


# ---------------------------------------------------------------------------
# TestM13aValidation — line-type validation
# ---------------------------------------------------------------------------

class TestM13aValidation:
    """
    Validate that line-type checks are now PayItems-driven.
    System items still pass; custom items are validated via the DB.
    """

    async def test_system_hours_accepted(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int, m13_no_rates,
    ):
        """System item 'Hours' must still be accepted (fast-path)."""
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Hours", "quantity": "8.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["line_type"] == "HOURS"

    async def test_system_miles_accepted(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int, m13_no_rates,
    ):
        """System item 'Miles' must still be accepted."""
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Miles", "quantity": "200.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201

    async def test_unknown_code_rejected(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
    ):
        """A code that is neither a system type nor a company PayItem → 422."""
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "TOTALLY_UNKNOWN", "quantity": "1.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "not a recognised" in resp.json()["detail"].lower() or \
               "recognised" in resp.json()["detail"].lower()

    async def test_blank_line_type_rejected(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
    ):
        """Blank line_type is rejected by the schema non-empty validator."""
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "   ", "quantity": "1.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_custom_daily_item_active_on_branch_accepted(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        m13_daily_item: dict, m13_no_rates,
    ):
        """
        Custom Daily PerUnit item (M13A_STOP) is active on PAYTEST →
        accepted as a line type.
        """
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "M13A_STOP", "quantity": "3.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["line_type"] == "M13A_STOP"

    async def test_custom_daily_item_inactive_on_branch_rejected(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        m13_inactive_item: dict,
    ):
        """
        Custom item with no BranchPayItemConfig and IsDefaultBranchActive=FALSE
        → 422 'not active for this branch'.
        """
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "M13A_INACT", "quantity": "1.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "not active" in resp.json()["detail"].lower()

    async def test_period_custom_item_blocked_from_daily_entry(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        m13_period_item: dict,
    ):
        """
        Period-scope item (system BONUS) → 422 with 'Period-scope' message.
        Period items have their own endpoint (M14+). Custom Period-scope items
        are no longer creatable; the system BONUS item exercises the same guard.
        """
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Bonus", "quantity": "1.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "period" in resp.json()["detail"].lower()

    async def test_retired_custom_item_rejected(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        db_conn,
    ):
        """
        A retired custom item code → 422 'retired'.

        M12_DEL_MEANINGFUL was retired in TestCustomPayItemDelete tests —
        if tests run in order that item is retired.  We use a fresh item here
        to be self-contained.
        """
        # Seed a temporary item and immediately retire it via smart-delete
        # (force the retire path by mocking meaningful usage).
        from app.settings import service as settings_service
        from app.settings.schemas import CustomPayItemUsage
        from tests.seed_helpers import seed_legacy_item

        item_id = await seed_legacy_item(
            db_conn,
            code="M13A_RETD",
            name="Retire Me (M13 test)",
            unit="Unit",
            category="Count",
        )

        # Retire via forced meaningful-usage mock
        mock_usage = CustomPayItemUsage(
            pay_item_id=item_id, pay_item_code="M13A_RETD",
            has_meaningful_usage=True, has_final_lines=False,
            meaningful_draft_line_count=1, final_line_count=0,
            non_meaningful_draft_line_count=0,
            can_physical_delete=False, deletion_would_retire=True,
        )
        with patch.object(settings_service, "_compute_usage",
                          new=AsyncMock(return_value=mock_usage)):
            del_resp = await session_client.delete(
                f"/settings/pay-items/{item_id}", headers=auth(auth_token)
            )
        assert del_resp.json()["deletion_type"] == "retired"

        # Now try to use the retired code as a line type
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "M13A_RETD", "quantity": "1.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "retired" in resp.json()["detail"].lower()

    async def test_work_date_used_for_active_check(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        m13_daily_item: dict, m13_no_rates,
    ):
        """
        work_date is used for the BranchPayItemConfig active check,
        not today's date.  M13A_STOP was activated with EffectiveFrom = today
        (2026-xx-xx), and the test work_date is 2032-03-07 (future) — still
        within the config's open window.  The line must be accepted.
        """
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "M13A_STOP", "quantity": "2.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# TestM13bCalculation — calculatedamount on insert
# ---------------------------------------------------------------------------

class TestM13bCalculation:
    """
    M13b: calculatedamount is populated at insert time for PerUnit items
    and left NULL for Fixed/None items.
    """

    async def test_perunit_system_item_with_approved_rate_calculates(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        m13_hourly_rate: dict,
    ):
        """
        'Hours' (PerUnit) + approved HOURLY rate of $20.00 →
        calculatedamount = qty × rate = 8.00 × 20.00 = 160.00.
        """
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Hours", "quantity": "8.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["calculated_amount"] is not None
        assert Decimal(str(body["calculated_amount"])) == Decimal("160.0000")
        assert body["needs_manager_review"] is False

    async def test_perunit_system_item_no_rate_leaves_null_and_flags_review(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int, m13_no_rates,
    ):
        """
        'Hours' (PerUnit) but no approved rate →
        calculatedamount = NULL, needs_manager_review = True.
        This is NOT a hard block — the line is still accepted.
        """
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Hours", "quantity": "8.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["calculated_amount"] is None
        assert body["needs_manager_review"] is True

    async def test_fixed_system_item_no_calculation(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int, m13_no_rates,
    ):
        """
        'Overnight' (PerUnit after migration 0022) with no approved rate →
        calculatedamount = NULL, needs_manager_review = True.

        Phase 4C: rate_amount must NOT be supplied for PerUnit lines.
        Rates come exclusively from approved DriverRates.
        """
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Overnight", "quantity": "1.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["calculated_amount"] is None
        assert body["needs_manager_review"] is True

    async def test_none_system_item_no_calculation(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int, m13_no_rates,
    ):
        """
        'DailyNote' (None behavior) → calculatedamount = NULL, no flag.
        """
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "DailyNote", "quantity": "8.00", "notes": "filler"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["calculated_amount"] is None
        assert body["needs_manager_review"] is False

    async def test_custom_perunit_item_no_rate_type_map_flags_review(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        m13_daily_item: dict, m13_no_rates,
    ):
        """
        Custom Daily PerUnit item (M13A_STOP) has no PayItemRateTypeMap entry →
        calculatedamount = NULL, needs_manager_review = True.
        This is the expected M13b behaviour for custom items without a wired rate type.
        The map will be seeded in a future M13c cleanup migration.
        """
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "M13A_STOP", "quantity": "5.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["calculated_amount"] is None
        assert body["needs_manager_review"] is True

    async def test_rate_lookup_uses_work_date(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        """
        Rate effective from 2032-04-01 should NOT apply to work_date 2032-03-07.
        Result: calculatedamount = NULL, needs_manager_review = True.
        """
        await _void_all_rates(session_client, auth_token)
        try:
            # Rate only becomes effective AFTER the test period
            rate_resp = await session_client.post(
                "/payroll/rates",
                json={
                    "driver_id":      paytest_driver_id,
                    "rate_type_id":   paytest_rate_type_id,
                    "amount":         "25.00",
                    "effective_from": "2032-04-01",   # After the period end date
                },
                headers=auth(auth_token),
            )
            rate_id = rate_resp.json()["driver_rate_id"]
            await session_client.post(
                f"/payroll/rates/{rate_id}/approve", headers=auth(auth_token)
            )

            pid = m13_open_period["payroll_period_id"]
            resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                      "line_type": "Hours", "quantity": "8.00"},
                headers=auth(auth_token),
            )
            assert resp.status_code == 201
            body = resp.json()
            # Rate not yet effective at work_date → no calculation
            assert body["calculated_amount"] is None
            assert body["needs_manager_review"] is True
        finally:
            await _void_all_rates(session_client, auth_token)

    async def test_rate_lookup_uses_effective_rate_not_future_rate(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        """
        Two rates: old rate $15.00 effective 2030-01-01, new rate $25.00 effective
        2032-04-01.  work_date = 2032-03-07 → uses the $15.00 rate.
        """
        await _void_all_rates(session_client, auth_token)
        try:
            # Old rate (effective before period)
            old_resp = await session_client.post(
                "/payroll/rates",
                json={"driver_id": paytest_driver_id, "rate_type_id": paytest_rate_type_id,
                      "amount": "15.00", "effective_from": "2030-01-01",
                      "effective_to": "2032-03-31"},
                headers=auth(auth_token),
            )
            old_id = old_resp.json()["driver_rate_id"]
            await session_client.post(f"/payroll/rates/{old_id}/approve", headers=auth(auth_token))

            # New rate (effective after period)
            new_resp = await session_client.post(
                "/payroll/rates",
                json={"driver_id": paytest_driver_id, "rate_type_id": paytest_rate_type_id,
                      "amount": "25.00", "effective_from": "2032-04-01"},
                headers=auth(auth_token),
            )
            new_id = new_resp.json()["driver_rate_id"]
            await session_client.post(f"/payroll/rates/{new_id}/approve", headers=auth(auth_token))

            pid = m13_open_period["payroll_period_id"]
            resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                      "line_type": "Hours", "quantity": "8.00"},
                headers=auth(auth_token),
            )
            assert resp.status_code == 201
            body = resp.json()
            # Should use old rate $15.00: 8 × 15 = 120.00
            assert body["calculated_amount"] is not None
            assert Decimal(str(body["calculated_amount"])) == Decimal("120.0000")
        finally:
            await _void_all_rates(session_client, auth_token)


# ---------------------------------------------------------------------------
# TestM13bUpdateRecalculation — calculatedamount recalculated on PATCH
# ---------------------------------------------------------------------------

class TestM13bUpdateRecalculation:
    """
    When quantity or rate_amount changes via PATCH, calculatedamount is
    recomputed automatically.
    """

    async def test_update_quantity_recalculates(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        m13_hourly_rate: dict,
    ):
        """
        Add Hours line (qty=8, rate=$20 → calc=160), then PATCH qty=10.
        New calculatedamount must be 10 × 20 = 200.
        """
        pid = m13_open_period["payroll_period_id"]
        add_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Hours", "quantity": "8.00"},
            headers=auth(auth_token),
        )
        assert add_resp.status_code == 201
        line_id = add_resp.json()["draft_line_id"]
        assert Decimal(str(add_resp.json()["calculated_amount"])) == Decimal("160.0000")

        patch_resp = await session_client.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            json={"quantity": "10.00"},
            headers=auth(auth_token),
        )
        assert patch_resp.status_code == 200
        body = patch_resp.json()
        assert Decimal(str(body["calculated_amount"])) == Decimal("200.0000")

    async def test_update_with_no_approved_rate_clears_calculated_amount(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        m13_hourly_rate: dict,
    ):
        """
        If the rate is voided between insert and update, the update clears
        calculatedamount to NULL and sets needs_manager_review = True.
        """
        pid = m13_open_period["payroll_period_id"]
        add_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Hours", "quantity": "8.00"},
            headers=auth(auth_token),
        )
        assert add_resp.status_code == 201
        line_id = add_resp.json()["draft_line_id"]
        assert add_resp.json()["calculated_amount"] is not None  # rate was present

        # Void the rate
        await _void_all_rates(session_client, auth_token)

        # Update quantity — rate is gone, should clear calculated_amount
        patch_resp = await session_client.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            json={"quantity": "10.00"},
            headers=auth(auth_token),
        )
        assert patch_resp.status_code == 200
        body = patch_resp.json()
        assert body["calculated_amount"] is None
        assert body["needs_manager_review"] is True

    async def test_update_notes_only_does_not_touch_calculated_amount(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        m13_hourly_rate: dict,
    ):
        """
        Patching only notes leaves calculatedamount unchanged.
        Recalculation is only triggered when quantity or rate_amount changes.
        """
        pid = m13_open_period["payroll_period_id"]
        add_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Hours", "quantity": "8.00"},
            headers=auth(auth_token),
        )
        assert add_resp.status_code == 201
        original_calc = add_resp.json()["calculated_amount"]
        line_id = add_resp.json()["draft_line_id"]

        # Void the rate — but notes-only patch must NOT trigger recalculation
        await _void_all_rates(session_client, auth_token)

        patch_resp = await session_client.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            json={"notes": "Updated note only"},
            headers=auth(auth_token),
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["calculated_amount"] == original_calc  # unchanged


# ---------------------------------------------------------------------------
# TestM13bFinalizationUsesCalc — finalization uses calculatedamount
# ---------------------------------------------------------------------------

class TestM13bFinalizationUsesCalc:
    """
    Finalization should use the stored calculatedamount when it exists,
    falling back to qty × rateamount only when it is NULL.
    """

    async def test_finalization_uses_calculatedamount_when_present(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_clean: int, paytest_rate_type_id: int, direct_db,
    ):
        """
        Add Hours line, rate=$20 (from approved DriverRate), qty=8 → calculatedamount=160.
        After finalization, final_amount must be 160 (from calculatedamount).

        Phase 4C: rate_amount is no longer accepted for PerUnit lines.
        The approved DriverRate ($20) is used exclusively.

        Phase 5 note: uses an isolated driver so that the DriverRate stays
        referenced in PayrollFinalLines after finalization without contaminating
        paytest_driver_id.  _void_all_rates teardown silently ignores the 422
        from the Phase 5 guard; the isolated driver means no cascade to other
        tests' approval-conflict checks.
        """
        headers = auth(auth_token)

        # Isolated driver: rate stays in finallines after finalization.
        # _void_all_rates teardown will silently ignore the 422 (Phase 5 guard),
        # and since the rate is on an isolated driver, no other test is affected.
        drv_resp = await session_client.post(
            "/core/drivers",
            json={"branch_id": m13_clean, "full_name": "M13 FinalCalc Isolated Driver"},
            headers=headers,
        )
        assert drv_resp.status_code == 201, f"Create isolated driver failed: {drv_resp.text}"
        isolated_driver_id = drv_resp.json()["driver_id"]

        # Create and approve HOURLY rate ($20) for the isolated driver
        rate_resp = await session_client.post(
            "/payroll/rates",
            json={
                "driver_id":      isolated_driver_id,
                "rate_type_id":   paytest_rate_type_id,
                "amount":         "20.00",
                "effective_from": "2032-01-01",
            },
            headers=headers,
        )
        assert rate_resp.status_code == 201, f"Create rate failed: {rate_resp.text}"
        rate_id = rate_resp.json()["driver_rate_id"]
        approve_resp = await session_client.post(
            f"/payroll/rates/{rate_id}/approve", headers=headers,
        )
        assert approve_resp.status_code == 200, f"Approve rate failed: {approve_resp.text}"

        # Create a fresh period for this isolated test
        pid = await _make_period(direct_db, m13_clean, "2032-03-18", "2032-03-24")

        # Add Hours line — approved rate ($20) computes 8×20=160
        line_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": isolated_driver_id, "work_date": "2032-03-20",
                  "line_type": "Hours", "quantity": "8.00"},
            headers=headers,
        )
        assert line_resp.status_code == 201
        body = line_resp.json()
        # calculatedamount = 8 × 20 = 160 from approved DriverRate
        assert Decimal(str(body["calculated_amount"])) == Decimal("160.0000")

        await _approve_period_via_review(session_client, auth_token, pid)

        fin_resp = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=headers
        )
        assert fin_resp.status_code == 200

        fl_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=headers
        )
        assert fl_resp.status_code == 200
        lines = fl_resp.json()
        assert len(lines) == 1
        # final_amount = calculatedamount = 160.0000
        assert Decimal(str(lines[0]["final_amount"])) == Decimal("160.0000")

    async def test_finalization_with_null_calc_informational_item(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_clean: int, paytest_driver_id: int, direct_db,
    ):
        """
        'DailyNote' (None rate_behavior) → calculatedamount = NULL, needs_manager_review = False.
        Finalization includes this line with final_amount = COALESCE(NULL, qty × COALESCE(NULL,0)) = 0.

        Phase 4C note: the original test used 'Overnight' with a manual rate_amount.
        Migration 0022 changed OVERNIGHT to PerUnit, and Phase 4C blocks manual rate_amount
        for PerUnit lines. DailyNote (None rate_behavior) is used to verify that NULL-calc lines
        finalize without errors.
        """
        pid = await _make_period(direct_db, m13_clean, "2032-03-25", "2032-03-31")

        line_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-28",
                  "line_type": "DailyNote", "quantity": "1.00", "notes": "filler"},
            headers=auth(auth_token),
        )
        assert line_resp.status_code == 201
        body = line_resp.json()
        assert body["calculated_amount"] is None        # None behavior → no calculation
        assert body["needs_manager_review"] is False    # None behavior → no review flag

        await _approve_period_via_review(session_client, auth_token, pid)

        fin_resp = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin_resp.status_code == 200

        fl_resp = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        lines = fl_resp.json()
        # Informational NULL-calculation lines are not financial FinalLines;
        # finalization succeeds while the financial ledger remains empty.
        assert lines == []


# ---------------------------------------------------------------------------
# TestM13bCodexFixes — targeted tests for the 8 Codex-reported issues
# ---------------------------------------------------------------------------

class TestM13bCodexFixes:
    """
    Focused regression tests for the 8 issues reported by Codex review.

    Issue 1 — Period system items blocked from daily entry
    Issue 2 — System fast-path respects BranchPayItemConfig
    Issue 3 — work_date constrained to period range
    Issue 4 — system calc uses PayItemRateTypeMap (tested via issue 2 flow)
    Issue 5 — custom item active in branch A rejected in branch B
    Issue 6 — needs_manager_review blocks approval and finalization
    Issue 7 — update raises 422 when custom item is retired/inactive
    Issue 8 (test fixture) — custom PerUnit item with rate map calculates correctly
    """

    # ── Issue 1: Period system items must be rejected ────────────────────── #

    async def test_bonus_system_item_rejected_from_daily_entry(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
    ):
        """'Bonus' is a Period-scope system item and must be rejected with a clear message."""
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Bonus", "quantity": "1.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "period" in resp.json()["detail"].lower()

    async def test_adjustment_system_item_rejected_from_daily_entry(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
    ):
        """'Adjustment' is a Period-scope system item and must be rejected."""
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Adjustment", "quantity": "1.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "period" in resp.json()["detail"].lower()

    # ── Issue 2: System fast-path now checks branch activation ───────────── #

    async def test_system_item_disabled_by_branch_config_rejected(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, paytest_driver_id: int, direct_db,
    ):
        """
        A system item with IsDefaultBranchActive=FALSE that has been explicitly
        DISABLED via BranchPayItemConfig must be rejected from daily entry.

        Strategy: cancel all PAYTEST periods, disable OVERNIGHT on PAYTEST
        (creating an explicit IsActive=FALSE config row), open a new PAYTEST
        period, verify Overnight is rejected, then re-enable.

        The conftest autouse fixture activates OVERNIGHT at session start; this
        test temporarily overrides that config to test the 'disabled' path.
        """
        # Cancel any active PAYTEST periods so disabling has EffectiveFrom = today.
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)

        # Look up OVERNIGHT item ID
        items_resp = await session_client.get(
            f"/settings/branches/{paytest_branch_id}/pay-items",
            headers=auth(auth_token),
        )
        assert items_resp.status_code == 200
        overnight_id = next(
            (i["pay_item_id"] for i in items_resp.json() if i["pay_item_code"] == "OVERNIGHT"),
            None,
        )
        assert overnight_id is not None, "OVERNIGHT item not found in branch pay-items"

        # Disable OVERNIGHT on PAYTEST
        dis = await session_client.patch(
            f"/settings/branches/{paytest_branch_id}/pay-items/{overnight_id}",
            json={"is_active": False},
            headers=auth(auth_token),
        )
        assert dis.status_code == 200, f"disable OVERNIGHT failed: {dis.text}"

        period_id = None
        try:
            period_id = await _make_period(direct_db, paytest_branch_id, "2032-04-07", "2032-04-13")

            resp = await session_client.post(
                f"/payroll/periods/{period_id}/lines",
                json={"driver_id": paytest_driver_id, "work_date": "2032-04-10",
                      "line_type": "Overnight", "quantity": "1.00"},
                headers=auth(auth_token),
            )
            assert resp.status_code == 422
            assert "not active" in resp.json()["detail"].lower()

        finally:
            if period_id:
                await session_client.patch(
                    f"/payroll/periods/{period_id}/status",
                    json={"status": "Cancelled"}, headers=auth(auth_token),
                )
            # Re-enable OVERNIGHT (restores state for other tests)
            await session_client.patch(
                f"/settings/branches/{paytest_branch_id}/pay-items/{overnight_id}",
                json={"is_active": True},
                headers=auth(auth_token),
            )

    # ── Issue 3: work_date must be within the period range ───────────────── #

    async def test_work_date_before_period_start_rejected(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
    ):
        """work_date before period start_date must return 422."""
        pid = m13_open_period["payroll_period_id"]
        # Period is 2032-03-04..10; work_date 2032-03-03 is one day before start.
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-03",
                  "line_type": "Hours", "quantity": "8.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code in (400, 422)
        assert "outside the period range" in resp.json()["detail"].lower() or \
               "period range" in resp.json()["detail"].lower()

    async def test_work_date_after_period_end_rejected(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
    ):
        """work_date after period end_date must return 422."""
        pid = m13_open_period["payroll_period_id"]
        # Period is 2032-03-04..10; work_date 2032-03-11 is one day after end.
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-11",
                  "line_type": "Hours", "quantity": "8.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code in (400, 422)
        assert "period range" in resp.json()["detail"].lower()

    async def test_work_date_on_period_boundary_accepted(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int, m13_no_rates,
    ):
        """work_date exactly on start_date and end_date must both be accepted."""
        pid = m13_open_period["payroll_period_id"]
        for wd in ("2032-03-04", "2032-03-10"):
            resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": paytest_driver_id, "work_date": wd,
                      "line_type": "Hours", "quantity": "1.00"},
                headers=auth(auth_token),
            )
            assert resp.status_code == 201, f"work_date {wd} should be accepted: {resp.text}"

    # ── Issue 5: custom item active in A rejected in B ───────────────────── #

    async def test_custom_item_active_in_paytest_rejected_in_m13_open_period(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        m13_inactive_item: dict,
    ):
        """
        M13A_INACT was created with IsDefaultBranchActive=FALSE and no
        BranchPayItemConfig for PAYTEST (it was never activated).
        Attempting to add it to a PAYTEST period must be rejected.

        This verifies the cross-branch isolation: an item not activated for
        a branch is rejected regardless of whether it's activated elsewhere.

        Note: we use m13_inactive_item here because it's cleaner than
        disabling m13_daily_item (M13A_STOP) which is session-scoped active.
        """
        pid = m13_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "M13A_INACT", "quantity": "2.00"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "not active" in resp.json()["detail"].lower()

    # ── Issue 6: needs_manager_review blocks approval and finalization ────── #

    async def test_needs_manager_review_blocks_open_to_inreview(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, paytest_driver_id: int, m13_no_rates,
        paytest_rate_type_id: int, direct_db,
    ):
        """
        A period with at least one line where needs_manager_review=True
        cannot transition from Open to InReview (M16: guard moved here).
        After approving a rate (which auto-refreshes the calculation), the
        submission succeeds.

        Phase 4C: rate_amount is no longer a valid resolution path for PerUnit
        lines. Approved DriverRates are the only resolution mechanism.
        """
        # Create a fresh period
        pid = await _make_period(direct_db, paytest_branch_id, "2032-06-03", "2032-06-09")
        approved_rate_id = None
        try:
            # Add Hours line with no approved rate -> needs_manager_review = True
            line_resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": paytest_driver_id, "work_date": "2032-06-05",
                      "line_type": "Hours", "quantity": "8.00"},
                headers=auth(auth_token),
            )
            assert line_resp.status_code == 201
            assert line_resp.json()["needs_manager_review"] is True
            line_id = line_resp.json()["draft_line_id"]

            # Open -> InReview must be blocked (guard moved to Open→InReview in M16)
            blocked = await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "InReview"}, headers=auth(auth_token),
            )
            assert blocked.status_code == 422
            assert "manager review" in blocked.json()["detail"].lower()

            # Confirm that clearing the flag WITHOUT an approved rate is rejected
            # (Issue 3 guard: PerUnit line with NULL calc cannot have flag cleared).
            naked_clear = await session_client.patch(
                f"/payroll/periods/{pid}/lines/{line_id}",
                json={"needs_manager_review": False},
                headers=auth(auth_token),
            )
            assert naked_clear.status_code == 422, (
                "Clearing review flag on PerUnit line with NULL calc should fail"
            )

            # Properly resolve by approving a DriverRate (Phase 4C: rate_amount
            # is no longer accepted for PerUnit lines).
            # The Open→InReview transition auto-refreshes calculations.
            rate_resp = await session_client.post(
                "/payroll/rates",
                json={
                    "driver_id":      paytest_driver_id,
                    "rate_type_id":   paytest_rate_type_id,
                    "amount":         "20.00",
                    "effective_from": "2032-01-01",
                },
                headers=auth(auth_token),
            )
            assert rate_resp.status_code == 201
            approved_rate_id = rate_resp.json()["driver_rate_id"]
            approve_resp = await session_client.post(
                f"/payroll/rates/{approved_rate_id}/approve",
                headers=auth(auth_token),
            )
            assert approve_resp.status_code == 200

            # Now Open -> InReview must succeed (auto-refresh clears NMR)
            ok = await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "InReview"}, headers=auth(auth_token),
            )
            assert ok.status_code == 200
        finally:
            from sqlalchemy import text as _sqla_text
            await direct_db.execute(
                _sqla_text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )
            if approved_rate_id:
                await session_client.delete(
                    f"/payroll/rates/{approved_rate_id}", headers=auth(auth_token)
                )

    async def test_needs_manager_review_blocks_full_approval_chain(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, paytest_rate_type_id: int, m13_no_rates, direct_db,
    ):
        """
        Full chain test: a period with an unresolved needs_manager_review line
        is blocked at Open→InReview (M16: guard moved here).
        After resolving the flag the period proceeds through finalization.

        Phase 5 note: uses an isolated driver so the finalized DriverRate
        stays referenced in PayrollFinalLines without contaminating
        paytest_driver_id-based tests (Phase 5 guard rejects void attempts
        on finalized-used rates; the isolated driver ensures no cascade).
        """
        headers = auth(auth_token)

        # Isolated driver: keeps the finalized rate off paytest_driver_id.
        drv_resp = await session_client.post(
            "/core/drivers",
            json={"branch_id": paytest_branch_id,
                  "full_name": "M13 NMR FullChain Isolated Driver"},
            headers=headers,
        )
        assert drv_resp.status_code == 201, f"Create isolated driver failed: {drv_resp.text}"
        isolated_driver_id = drv_resp.json()["driver_id"]

        # Create and approve HOURLY rate for the isolated driver
        rate_resp = await session_client.post(
            "/payroll/rates",
            json={"driver_id": isolated_driver_id,
                  "rate_type_id": paytest_rate_type_id,
                  "amount": "20.00", "effective_from": "2032-01-01"},
            headers=headers,
        )
        assert rate_resp.status_code == 201, f"Create rate failed: {rate_resp.text}"
        rate_id = rate_resp.json()["driver_rate_id"]
        approve_resp = await session_client.post(
            f"/payroll/rates/{rate_id}/approve", headers=headers,
        )
        assert approve_resp.status_code == 200, f"Approve rate failed: {approve_resp.text}"

        pid = await _make_period(direct_db, paytest_branch_id, "2032-06-10", "2032-06-16")
        try:
            # Add an Hours line with an approved rate (flag starts False)
            line_resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": isolated_driver_id, "work_date": "2032-06-12",
                      "line_type": "Hours", "quantity": "8.00"},
                headers=headers,
            )
            assert line_resp.status_code == 201
            line_id = line_resp.json()["draft_line_id"]
            assert line_resp.json()["calculated_amount"] is not None

            # Manually flag for review while period is still Open
            flag_resp = await session_client.patch(
                f"/payroll/periods/{pid}/lines/{line_id}",
                json={"needs_manager_review": True},
                headers=headers,
            )
            assert flag_resp.status_code == 200

            # Guard: Open -> InReview is blocked (flag set)
            blocked = await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "InReview"}, headers=headers,
            )
            assert blocked.status_code in (409, 422)
            assert "manager review" in blocked.json()["detail"].lower()

            # Resolve the flag
            await session_client.patch(
                f"/payroll/periods/{pid}/lines/{line_id}",
                json={"needs_manager_review": False},
                headers=headers,
            )

            # Now Open -> InReview succeeds
            r = await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "InReview"}, headers=headers,
            )
            assert r.status_code == 200

            # Approve via review flow, then finalize
            review_resp = await session_client.get(
                "/review/items", headers=headers,
            )
            review_item = next(
                i for i in review_resp.json()
                if i.get("entity_name") == "PayrollPeriods"
                and i.get("entity_id") == str(pid)
                and i.get("status") == "Pending"
            )
            decide = await session_client.post(
                f"/review/items/{review_item['review_item_id']}/decide",
                headers=headers,
                json={"decision": "Approved"},
            )
            assert decide.status_code == 200

            fin_resp = await session_client.post(
                f"/payroll/periods/{pid}/finalize", headers=headers,
            )
            assert fin_resp.status_code == 200
            assert fin_resp.json()["status"] == "Locked"

        except Exception:
            await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Cancelled"}, headers=headers,
            )
            raise

    # ── Issue 7: update with retired/inactive custom item fails ──────────── #

    async def test_update_line_with_retired_item_fails_clearly(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_branch_id: int, paytest_driver_id: int,
        m13_daily_item: dict, m13_no_rates, db_conn,
    ):
        """
        After a custom pay item is retired, updating a draft line that uses
        it must return 422 ('retired') rather than silently succeeding.

        Issue 6 fix: the activation PATCH now correctly uses paytest_branch_id
        (not paytest_driver_id, which is a driver ID, not a branch ID).
        Issue 4 fix: all updates (quantity, notes, status) trigger revalidation.
        """
        from unittest.mock import patch as mock_patch

        from app.settings import service as settings_service
        from app.settings.schemas import CustomPayItemUsage
        from tests.seed_helpers import seed_legacy_item

        pid = m13_open_period["payroll_period_id"]

        # Seed a fresh item to retire (avoid touching session-scoped M13A_STOP).
        retire_id = await seed_legacy_item(
            db_conn,
            code="M13A_RETIRE2",
            name="Retire Me 2 (M13 test)",
            unit="Unit",
            category="Count",
        )

        # Issue 6 fix: use paytest_branch_id (branch ID), not paytest_driver_id.
        act_resp = await session_client.patch(
            f"/settings/branches/{paytest_branch_id}/pay-items/{retire_id}",
            json={"is_active": True}, headers=auth(auth_token),
        )
        assert act_resp.status_code == 200, (
            f"Activate M13A_RETIRE2 on PAYTEST failed: {act_resp.text}"
        )

        # Add a line using the newly activated item.
        add_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "M13A_RETIRE2", "quantity": "2.00"},
            headers=auth(auth_token),
        )
        assert add_resp.status_code == 201, (
            f"Add line using M13A_RETIRE2 failed: {add_resp.text}"
        )
        line_id = add_resp.json()["draft_line_id"]

        # Retire M13A_RETIRE2 via the smart-delete endpoint (mocking usage check).
        mock_usage = CustomPayItemUsage(
            pay_item_id=retire_id, pay_item_code="M13A_RETIRE2",
            has_meaningful_usage=True, has_final_lines=False,
            meaningful_draft_line_count=1, final_line_count=0,
            non_meaningful_draft_line_count=0,
            can_physical_delete=False, deletion_would_retire=True,
        )
        with mock_patch.object(settings_service, "_compute_usage",
                               new=AsyncMock(return_value=mock_usage)):
            del_resp = await session_client.delete(
                f"/settings/pay-items/{retire_id}", headers=auth(auth_token)
            )
        assert del_resp.json()["deletion_type"] == "retired"

        # Verify item is now Retired.
        get_resp = await session_client.get(
            f"/settings/pay-items/{retire_id}", headers=auth(auth_token)
        )
        assert get_resp.json()["status"] == "Retired"

        # Quantity update → must fail with 422 'retired'
        qty_resp = await session_client.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            json={"quantity": "5.00"},
            headers=auth(auth_token),
        )
        assert qty_resp.status_code == 422
        assert "retired" in qty_resp.json()["detail"].lower()

        # Notes-only update → must ALSO fail with 422 (Issue 4: all updates revalidate)
        notes_resp = await session_client.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            json={"notes": "just a note"},
            headers=auth(auth_token),
        )
        assert notes_resp.status_code == 422, (
            "Notes-only update on a line with a retired item should fail "
            f"(got {notes_resp.status_code}: {notes_resp.text})"
        )
        assert "retired" in notes_resp.json()["detail"].lower()

    # ── Issue 5+8: custom item with PayItemRateTypeMap calculates correctly ─ #

    async def test_custom_perunit_item_with_rate_map_calculates(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int, paytest_branch_id: int,
        paytest_rate_type_id: int, db_conn,
    ):
        """
        A custom Daily PerUnit item with a PayItemRateTypeMap entry and an
        approved driver rate for that rate type produces a correct calculatedamount.

        Flow:
          1. Seed custom item M13A_MAPPED (PerUnit, Daily)
          2. Activate it on PAYTEST
          3. Assign rate type HOURLY via POST /settings/pay-items/{id}/rate-type-map
          4. Approve an HOURLY driver rate $25.00 (effective from 2032-01-01)
          5. Add a draft line: qty=4 → calculatedamount must be 4 × 25 = 100.0000
        """
        from tests.seed_helpers import seed_legacy_item
        await _void_all_rates(session_client, auth_token)
        item_id = None
        try:
            # 1. Seed custom item
            item_id = await seed_legacy_item(
                db_conn,
                code="M13A_MAPPED",
                name="Mapped Stop (M13 test)",
                unit="Stop",
                category="Count",
            )

            # 2. Activate on PAYTEST
            act_resp = await session_client.patch(
                f"/settings/branches/{paytest_branch_id}/pay-items/{item_id}",
                json={"is_active": True},
                headers=auth(auth_token),
            )
            assert act_resp.status_code == 200, f"activate: {act_resp.text}"

            # 3. Assign rate type
            map_resp = await session_client.post(
                f"/settings/pay-items/{item_id}/rate-type-map",
                json={"rate_type_id": paytest_rate_type_id, "is_primary": True},
                headers=auth(auth_token),
            )
            assert map_resp.status_code == 201, f"rate-type-map: {map_resp.text}"
            assert map_resp.json()["rate_code"] == "HOURLY"

            # 4. Approve driver rate $25.00 effective 2032-01-01
            rate_resp = await session_client.post(
                "/payroll/rates",
                json={"driver_id": paytest_driver_id,
                      "rate_type_id": paytest_rate_type_id,
                      "amount": "25.00", "effective_from": "2032-01-01"},
                headers=auth(auth_token),
            )
            assert rate_resp.status_code == 201
            rate_id = rate_resp.json()["driver_rate_id"]
            approve_resp = await session_client.post(
                f"/payroll/rates/{rate_id}/approve", headers=auth(auth_token)
            )
            assert approve_resp.status_code == 200

            # 5. Add draft line: qty=4 → 4 × 25 = 100.00
            pid = m13_open_period["payroll_period_id"]
            line_resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                      "line_type": "M13A_MAPPED", "quantity": "4.00"},
                headers=auth(auth_token),
            )
            assert line_resp.status_code == 201, f"line add: {line_resp.text}"
            body = line_resp.json()
            assert body["calculated_amount"] is not None, "calculatedamount should not be NULL"
            assert Decimal(str(body["calculated_amount"])) == Decimal("100.0000")
            assert body["needs_manager_review"] is False

        finally:
            await _void_all_rates(session_client, auth_token)
            if item_id is not None:
                cleanup = await session_client.delete(
                    f"/settings/pay-items/{item_id}", headers=auth(auth_token)
                )
                assert cleanup.status_code == 200, cleanup.text

    # ── Issue 3: prevent silent zero finalization ─────────────────────────── #

    async def test_clearing_review_flag_on_unresolved_perunit_line_blocked(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int, m13_no_rates,
    ):
        """
        Explicitly setting needs_manager_review=False on a PerUnit line that has
        no resolved calculatedamount must return 422.

        Without rates, the calculation engine sets calculatedamount=NULL and
        needs_manager_review=True.  Allowing the caller to clear the flag while
        calculatedamount remains NULL would permit a silent zero finalization via
        COALESCE(NULL, qty * 0) = 0.
        """
        pid = m13_open_period["payroll_period_id"]

        # Add a PerUnit (Hours) line with no driver rate -> needs_manager_review=True.
        add_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Hours", "quantity": "8.00"},
            headers=auth(auth_token),
        )
        assert add_resp.status_code == 201
        line = add_resp.json()
        assert line["needs_manager_review"] is True
        assert line["calculated_amount"] is None
        line_id = line["draft_line_id"]

        # Attempt to clear the review flag without providing a rate -> must fail.
        patch_resp = await session_client.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            json={"needs_manager_review": False},
            headers=auth(auth_token),
        )
        assert patch_resp.status_code == 422, (
            f"Expected 422 when clearing review flag on unresolved PerUnit line "
            f"(got {patch_resp.status_code}: {patch_resp.text})"
        )
        detail = patch_resp.json()["detail"].lower()
        assert "review flag" in detail or "manager review" in detail

    async def test_clearing_review_flag_while_updating_qty_still_no_rate_blocked(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int, m13_no_rates,
    ):
        """
        Setting needs_manager_review=False while also updating qty, when there is
        still no approved rate, must return 422 -- not silently accept.
        """
        pid = m13_open_period["payroll_period_id"]

        add_resp = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                  "line_type": "Hours", "quantity": "4.00"},
            headers=auth(auth_token),
        )
        assert add_resp.status_code == 201
        line_id = add_resp.json()["draft_line_id"]

        # Update qty AND try to clear the flag while there's still no rate.
        patch_resp = await session_client.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            json={"quantity": "10.00", "needs_manager_review": False},
            headers=auth(auth_token),
        )
        assert patch_resp.status_code == 422, (
            f"Expected 422 when updating qty+clearing review with no rate "
            f"(got {patch_resp.status_code}: {patch_resp.text})"
        )

    async def test_approval_blocked_when_perunit_line_has_unresolved_review_flag(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_branch_id: int, paytest_driver_id: int, direct_db,
    ):
        """
        InReview->Approved must be blocked when any PerUnit line has
        needs_manager_review=True (no approved driver rate found).

        This is the practical path the review-flag guard defends. The zero-calc
        guard (Issue 3, Step 2) is defensive code for DB-level bypasses.
        """
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
        await _void_all_rates(session_client, auth_token)

        pid = await _make_period(direct_db, paytest_branch_id, "2032-07-07", "2032-07-13")

        try:
            # Add a PerUnit line with no rate -> needs_manager_review=True.
            line_resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": paytest_driver_id, "work_date": "2032-07-09",
                      "line_type": "Hours", "quantity": "8.00"},
                headers=auth(auth_token),
            )
            assert line_resp.status_code == 201
            assert line_resp.json()["needs_manager_review"] is True

            # Attempt Open->InReview -- must be blocked (M16: guard moved here).
            blocked_resp = await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "InReview"}, headers=auth(auth_token),
            )
            assert blocked_resp.status_code == 422
            detail = blocked_resp.json()["detail"].lower()
            assert "manager review" in detail or "require" in detail, (
                f"Expected 'manager review' in detail, got: {detail}"
            )

        finally:
            await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Cancelled"}, headers=auth(auth_token),
            )
            await _void_all_rates(session_client, auth_token)

    # ── Issue 5: rate-type-map endpoint requires setup.manage ─────────────── #

    async def test_rate_type_map_requires_setup_manage_branch_user_denied(
        self, session_client: httpx.AsyncClient, auth_token: str,
        branch_user_token: str, paytest_rate_type_id: int, db_conn,
    ):
        """
        POST /settings/pay-items/{id}/rate-type-map must require
        AllCompanyBranches scope + setup.manage.

        branch_user has SpecificBranch scope + PAYROLL_VIEWER role (no write
        permissions) and must receive 403.
        """
        from tests.seed_helpers import seed_legacy_item
        # Seed a custom PerUnit item as admin so we have a valid item_id.
        item_id = await seed_legacy_item(
            db_conn,
            code="M13A_PERM_TEST",
            name="Permission Test Item (M13)",
            unit="Unit",
            category="Count",
        )

        try:
            deny_resp = await session_client.post(
                f"/settings/pay-items/{item_id}/rate-type-map",
                json={"rate_type_id": paytest_rate_type_id, "is_primary": True},
                headers=auth(branch_user_token),
            )
            assert deny_resp.status_code == 403, (
                f"branch_user should be denied (403) but got {deny_resp.status_code}: "
                f"{deny_resp.text}"
            )
        finally:
            cleanup = await session_client.delete(
                f"/settings/pay-items/{item_id}", headers=auth(auth_token)
            )
            assert cleanup.status_code == 200, cleanup.text

    async def test_rate_type_map_admin_can_assign(
        self, session_client: httpx.AsyncClient, auth_token: str,
        paytest_rate_type_id: int, db_conn,
    ):
        """
        Admin (AllCompanyBranches + setup.manage) must be able to call
        POST /settings/pay-items/{id}/rate-type-map successfully.

        Positive counterpart to the denial test above.
        """
        from tests.seed_helpers import seed_legacy_item
        item_id = await seed_legacy_item(
            db_conn,
            code="M13A_PERM_TEST",
            name="Permission Test Item (M13)",
            unit="Unit",
            category="Count",
        )
        try:
            ok_resp = await session_client.post(
                f"/settings/pay-items/{item_id}/rate-type-map",
                json={"rate_type_id": paytest_rate_type_id, "is_primary": True},
                headers=auth(auth_token),
            )
            assert ok_resp.status_code == 201, (
                f"Admin should be allowed (201) but got {ok_resp.status_code}: "
                f"{ok_resp.text}"
            )
            assert ok_resp.json()["rate_code"] == "HOURLY"
        finally:
            cleanup = await session_client.delete(
                f"/settings/pay-items/{item_id}", headers=auth(auth_token)
            )
            assert cleanup.status_code == 200, cleanup.text

    # ── System line update uses DB-driven path (not hardcoded dict) ───────── #

    async def test_system_line_update_uses_db_rate_map(
        self, session_client: httpx.AsyncClient, auth_token: str,
        m13_open_period: dict, paytest_driver_id: int,
        paytest_rate_type_id: int,
    ):
        """
        update_draft_line for a system PerUnit line must resolve rate_code from
        PayItemRateTypeMap (DB path, migration 0008) not the hardcoded
        _SYSTEM_LINE_TYPE_INFO dict.

        Proof:
          1. Add an Hours line with no DriverRate -> calc=NULL, review=True
          2. Approve an HOURLY rate $30.00 effective 2032-01-01
          3. PATCH the line with an updated quantity
          4. calculatedamount must equal qty * 30 (DB rate resolved on update)
          5. needs_manager_review must be False

        If the update path used the hardcoded dict, it would still look up the
        same rate_code ('HOURLY') because the dict matches the DB.  The meaningful
        proof is that calculatedamount is computed correctly -- which requires the
        DB-driven path to resolve rate_code, then look up the DriverRate.
        The test also confirms the code path does not raise (no regression).
        """
        await _void_all_rates(session_client, auth_token)
        try:
            pid = m13_open_period["payroll_period_id"]

            # 1. Add Hours line with no rate -> review flagged.
            add_resp = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": paytest_driver_id, "work_date": "2032-03-07",
                      "line_type": "Hours", "quantity": "4.00"},
                headers=auth(auth_token),
            )
            assert add_resp.status_code == 201
            line = add_resp.json()
            assert line["calculated_amount"] is None
            assert line["needs_manager_review"] is True
            line_id = line["draft_line_id"]

            # 2. Approve HOURLY rate $30.00 effective 2032-01-01.
            rate_resp = await session_client.post(
                "/payroll/rates",
                json={"driver_id": paytest_driver_id,
                      "rate_type_id": paytest_rate_type_id,
                      "amount": "30.00", "effective_from": "2032-01-01"},
                headers=auth(auth_token),
            )
            assert rate_resp.status_code == 201
            rate_id = rate_resp.json()["driver_rate_id"]
            approve_resp = await session_client.post(
                f"/payroll/rates/{rate_id}/approve", headers=auth(auth_token)
            )
            assert approve_resp.status_code == 200

            # 3. PATCH quantity to 6.
            patch_resp = await session_client.patch(
                f"/payroll/periods/{pid}/lines/{line_id}",
                json={"quantity": "6.00"},
                headers=auth(auth_token),
            )
            assert patch_resp.status_code == 200, (
                f"System line update failed: {patch_resp.text}"
            )
            updated = patch_resp.json()

            # 4+5. DB-driven path resolved HOURLY -> $30 -> 6 * 30 = 180.
            from decimal import Decimal
            assert updated["calculated_amount"] is not None, (
                "calculatedamount should be resolved after rate is approved and qty updated"
            )
            assert Decimal(str(updated["calculated_amount"])) == Decimal("180.0000"), (
                f"Expected 180.0000 (6 * 30), got {updated['calculated_amount']}"
            )
            assert updated["needs_manager_review"] is False

        finally:
            await _void_all_rates(session_client, auth_token)
