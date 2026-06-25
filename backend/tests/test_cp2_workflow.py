"""
CP-2 integration tests — Period Workflow Controls + Bonus Panel.

Tests:
  - Status transitions: Draft→Open, Open→InReview (with guards), permission checks
  - Period pay lines: GET, POST (BONUS), void, locked-period guard, ODA guard

Uses year 2082 dates to avoid conflicts with all other test modules.
Follows conftest.py and test_m14.py patterns.
"""
import pytest
import pytest_asyncio
import httpx
from datetime import date as _date
from decimal import Decimal
from sqlalchemy import text as _sqla_text


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


async def _make_draft_period(
    db,
    branch_id: int,
    start: str,
    end: str,
) -> dict:
    """Insert a Draft period directly into DB and return a period dict."""
    code = f"CP2-{branch_id}-{start}"
    row = (await db.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Draft', :code, :name, 'Week', :start, :end)
            ON CONFLICT (branchid, periodcode) DO UPDATE SET status = 'Draft'
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"CP2 {start}", "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return {"payroll_period_id": row["payrollperiodid"], "status": "Draft"}


async def _make_open_period(
    db,
    branch_id: int,
    start: str,
    end: str,
) -> dict:
    """Insert an Open period directly into DB and return a period dict."""
    code = f"CP2-{branch_id}-{start}"
    row = (await db.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"CP2 {start}", "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return {"payroll_period_id": row["payrollperiodid"], "status": "Open"}


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
    # Try global items list
    all_items = await client.get("/settings/pay-items", headers=auth(token))
    for item in all_items.json():
        if item.get("pay_item_code") == "BONUS":
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
async def cp2_bonus_activated(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> None:
    """Activate BONUS on PAYTEST branch once for the session."""
    await _activate_bonus(session_client, auth_token, paytest_branch_id)


# ===========================================================================
# TestWorkflowTransitions
# ===========================================================================

class TestWorkflowTransitions:

    async def test_draft_to_open_transition_works(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """PATCH Draft→Open succeeds and returns the updated period."""
        period = await _make_draft_period(
            direct_db, paytest_branch_id,
            "2082-01-01", "2082-01-07",
        )
        pid = period["payroll_period_id"]
        assert period["status"] == "Draft"

        resp = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "Open"

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    async def test_open_to_inreview_blocked_if_needs_review(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """Open→InReview is blocked when NeedsManagerReview lines exist."""
        from sqlalchemy import text as _text

        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-01-08", "2082-01-14",
        )
        pid = period["payroll_period_id"]

        # Add a daily line and flag it NeedsManagerReview via direct DB
        add = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "DailyNote",
                  "notes": "filler", "source_type": "Manual",
                  "work_date": "2082-01-08"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        # Force NeedsManagerReview = TRUE via direct DB
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrolldraftlines
                SET    needsmanagerreview = TRUE,
                       calculatedamount   = NULL
                WHERE  draftlineid = :lid
            """),
            {"lid": line_id},
        )

        resp = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, f"Expected 422 but got {resp.status_code}: {resp.text}"
        assert "review" in resp.text.lower() or "unresolved" in resp.text.lower()

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    async def test_open_to_inreview_succeeds_when_clean(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        cp2_bonus_activated,
        direct_db,
    ):
        """Open→InReview succeeds when no NeedsManagerReview lines exist."""
        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-01-15", "2082-01-21",
        )
        pid = period["payroll_period_id"]

        # Add a clean bonus line (no rate engine, so NeedsManagerReview=FALSE)
        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus",
                  "amount": "100.00", "notes": "Test bonus"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text

        resp = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, f"Expected 200 but got {resp.status_code}: {resp.text}"
        assert resp.json()["status"] == "InReview"

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    async def test_status_transition_requires_payroll_entry(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """No payroll.entry permission → 403 on Draft→Open transition."""
        period = await _make_draft_period(
            direct_db, paytest_branch_id,
            "2082-01-22", "2082-01-28",
        )
        pid = period["payroll_period_id"]

        # branch_user has no payroll.entry permission
        resp = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )


# ===========================================================================
# TestBonusPanel
# ===========================================================================

class TestBonusPanel:

    async def test_get_period_pay_lines_returns_bonus_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        cp2_bonus_activated,
        direct_db,
    ):
        """GET /period-pay returns BONUS lines added to an Open period."""
        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-02-01", "2082-02-07",
        )
        pid = period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus",
                  "amount": "150.00", "notes": "Monthly bonus"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text

        resp = await session_client.get(
            f"/payroll/periods/{pid}/period-pay",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200, resp.text
        lines = resp.json()
        bonus_lines = [l for l in lines if l["line_type"] == "BONUS"]
        assert len(bonus_lines) >= 1
        assert bonus_lines[0]["work_date"] is None
        assert Decimal(bonus_lines[0]["calculated_amount"]) == Decimal("150.00")

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    async def test_add_bonus_line_stores_period_scope(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        cp2_bonus_activated,
        direct_db,
    ):
        """POST bonus → line_type=BONUS, line_scope=Period, work_date=NULL."""
        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-02-08", "2082-02-14",
        )
        pid = period["payroll_period_id"]

        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus",
                  "amount": "200.00", "notes": "Performance bonus"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        line = resp.json()
        assert line["line_type"] == "BONUS"
        assert line["line_scope"] == "Period"
        assert line["work_date"] is None
        assert Decimal(line["calculated_amount"]) == Decimal("200.00")
        assert line["needs_manager_review"] is False

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    async def test_add_bonus_requires_nonzero_amount(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        cp2_bonus_activated,
        direct_db,
    ):
        """amount=0 is rejected with 422."""
        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-02-15", "2082-02-21",
        )
        pid = period["payroll_period_id"]

        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus",
                  "amount": "0.00", "notes": "Should fail"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "non-zero" in resp.text.lower() or "zero" in resp.text.lower()

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    async def test_bonus_requires_payroll_entry(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        cp2_bonus_activated,
        direct_db,
    ):
        """No payroll.entry permission → 403 on POST bonus."""
        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-02-22", "2082-02-28",
        )
        pid = period["payroll_period_id"]

        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus",
                  "amount": "50.00", "notes": "Test"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    async def test_void_bonus_soft_deletes(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        cp2_bonus_activated,
        direct_db,
    ):
        """DELETE bonus → status='Void' (soft delete, line still retrievable)."""
        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-03-01", "2082-03-07",
        )
        pid = period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus",
                  "amount": "75.00", "notes": "To be voided"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        void_resp = await session_client.delete(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            headers=auth(auth_token),
        )
        assert void_resp.status_code == 200, void_resp.text
        assert void_resp.json()["status"] == "Void"

        # Line is still in the list (with Void status)
        list_resp = await session_client.get(
            f"/payroll/periods/{pid}/period-pay",
            headers=auth(auth_token),
        )
        assert list_resp.status_code == 200
        all_ids = [l["draft_line_id"] for l in list_resp.json()]
        assert line_id in all_ids, "Voided line must still appear (soft delete)"
        voided = next(l for l in list_resp.json() if l["draft_line_id"] == line_id)
        assert voided["status"] == "Void"

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    async def test_void_bonus_on_locked_period_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        cp2_bonus_activated,
        direct_db,
    ):
        """Cannot void bonus when period is in Draft status (not editable)."""
        from sqlalchemy import text as _text

        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-03-08", "2082-03-14",
        )
        pid = period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus",
                  "amount": "100.00", "notes": "Frozen bonus"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        line_id = add.json()["draft_line_id"]

        # Force period to Locked via direct DB
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollperiods
                SET    status = 'Locked'
                WHERE  payrollperiodid = :pid
            """),
            {"pid": pid},
        )

        void_resp = await session_client.delete(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            headers=auth(auth_token),
        )
        assert void_resp.status_code == 422, (
            f"Expected 422 (Locked period) but got {void_resp.status_code}: {void_resp.text}"
        )

        # Cleanup via direct DB (trigger bypass: migration 0035 blocks Locked→Cancelled)
        await direct_db.execute(_text(
            "ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert"
        ))
        await direct_db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                  "WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        await direct_db.execute(_text(
            "ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert"
        ))

    async def test_oda_user_cannot_get_period_pay_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        paytest_branch_id: int,
        hq_branch_id: int,
        direct_db,
    ):
        """
        branch_user (PAYROLL_VIEWER, SpecificBranch=HQ) cannot access
        period-pay lines on PAYTEST branch (cross-branch denial).
        """
        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-03-15", "2082-03-21",
        )
        pid = period["payroll_period_id"]

        resp = await session_client.get(
            f"/payroll/periods/{pid}/period-pay",
            headers=auth(branch_user_token),
        )
        # branch_user cannot see PAYTEST branch → 403
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    async def test_oda_user_cannot_add_bonus(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        branch_user_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        cp2_bonus_activated,
        direct_db,
    ):
        """branch_user without payroll.entry cannot POST bonus."""
        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-03-22", "2082-03-28",
        )
        pid = period["payroll_period_id"]

        resp = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus",
                  "amount": "50.00", "notes": "Unauthorized"},
            headers=auth(branch_user_token),
        )
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )

    async def test_adjustment_is_different_line_type(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        BONUS is accepted (201, line_type='BONUS').
        ADJUSTMENT is blocked (422) — manual Adjustment is deferred to a future release.
        The bonus panel (frontend) filters by line_type=='BONUS'; ADJUSTMENT entries
        cannot be created so cannot contaminate the bonus panel.
        """
        period = await _make_open_period(
            direct_db, paytest_branch_id,
            "2082-04-01", "2082-04-07",
        )
        pid = period["payroll_period_id"]

        # BONUS succeeds
        add_bonus = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Bonus",
                  "amount": "100.00", "notes": "Bonus line"},
            headers=auth(auth_token),
        )
        assert add_bonus.status_code == 201, add_bonus.text
        assert add_bonus.json()["line_type"] == "BONUS"

        # ADJUSTMENT is blocked
        add_adj = await session_client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={"driver_id": paytest_driver_id, "line_type": "Adjustment",
                  "amount": "-25.00", "notes": "Adjustment line"},
            headers=auth(auth_token),
        )
        assert add_adj.status_code == 422, add_adj.text
        assert "adjustment" in add_adj.json()["detail"].lower()

        # Period-pay list shows only BONUS (no ADJUSTMENT since it was blocked)
        list_resp = await session_client.get(
            f"/payroll/periods/{pid}/period-pay",
            headers=auth(auth_token),
        )
        assert list_resp.status_code == 200
        line_types = {l["line_type"] for l in list_resp.json() if l["status"] != "Void"}
        assert "BONUS" in line_types
        assert "ADJUSTMENT" not in line_types

        # Confirm types are distinct strings
        assert "BONUS" != "ADJUSTMENT"

        # Cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=auth(auth_token),
        )
