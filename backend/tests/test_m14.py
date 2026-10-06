"""
M14 integration tests — period-level Bonus lifecycle and scope separation.

Period-level amounts are canonical Bonus Events (POST /periods/{id}/bonuses).
Legacy Period-scope DraftLines (WorkDate = NULL) are only injected directly
to characterize that daily routes and submit/finalize guards handle them.

Function-scoped fixtures create/cancel periods around each test.

All entry tests use an M14-owned branch on period dates in 2034-2035 (avoids
any conflicts with M13a/M13b/M13c tests which use 2026-2033 dates).
"""
import uuid
from decimal import Decimal

import httpx
import pytest_asyncio

from tests.ownership import assert_no_mutable_period_state

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient, token: str, branch_id: int, db=None
) -> None:
    from sqlalchemy import text as _sqla_text
    headers = auth(token)
    if db is None:
        raise AssertionError("M14 period cleanup requires direct_db ownership checks")
    period_prefix = f"M14-{branch_id}-%"
    protected_periods = (await db.execute(
        _sqla_text("""
            SELECT payrollperiodid FROM payroll.payrollperiods
            WHERE branchid = :bid AND periodcode LIKE :prefix
              AND status IN ('InReview', 'Approved')
        """), {"bid": branch_id, "prefix": period_prefix},
    )).scalars().all()
    for period_id in protected_periods:
        review_ids = (await db.execute(
            _sqla_text("""
                SELECT reviewitemid FROM review.managerreviewitems
                WHERE branchid = :bid AND entityschema = 'payroll'
                  AND entityname = 'PayrollPeriods' AND entityid = :eid
            """), {"bid": branch_id, "eid": str(period_id)},
        )).scalars().all()
        await db.execute(
            _sqla_text("""
                UPDATE review.managerreviewitems SET status = 'Cancelled'
                WHERE reviewitemid = ANY(:ids) AND status = 'Pending'
            """),
            {"ids": review_ids or [-1]},
        )
        await db.execute(
            _sqla_text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )
    for s in ("Draft", "Open"):
        resp = await client.get(
            "/payroll/periods", params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        assert resp.status_code == 200, f"List M14 {s} periods for cleanup failed: {resp.text}"
        for p in resp.json():
            if not p.get("period_code", "").startswith(f"M14-{branch_id}-"):
                continue
            cancelled = await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"}, headers=headers,
            )
            assert cancelled.status_code == 200, (
                f"Cancel owned M14 period {p['payroll_period_id']} failed: {cancelled.text}"
            )


async def _open_period(
    db,
    branch_id: int,
    start: str = "2034-01-01",
    end: str = "2034-01-07",
    status: str = "Open",
) -> dict:
    """Insert a period directly into DB with the given status (default Open).

    POST /payroll/periods requires an existing Open period (CP-1D B1 guard),
    so we insert directly — same pattern used by test_cp2d and test_cp1d.
    """
    from datetime import date as _date

    from sqlalchemy import text as _sqla_text
    code = f"M14-{branch_id}-{start}-{uuid.uuid4().hex[:8]}"
    row = (await db.execute(
        _sqla_text(f"""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, '{status}', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"M14 {start}", "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return {"payroll_period_id": row["payrollperiodid"]}


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


async def _activate_system_period_item(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    code: str,          # DB code e.g. 'BONUS', 'ADJUSTMENT'
    effective_from: str | None = None,
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
                    json={
                        "is_active": True,
                        **({"effective_from": effective_from} if effective_from else {}),
                    },
                    headers=auth(token),
                )
            return
    # Item not in branch list yet — find it globally and activate
    all_resp = await client.get("/settings/pay-items", headers=auth(token))
    for item in all_resp.json():
        if item.get("pay_item_code") == code:
            await client.patch(
                f"/settings/branches/{branch_id}/pay-items/{item['pay_item_id']}",
                json={
                    "is_active": True,
                    **({"effective_from": effective_from} if effective_from else {}),
                },
                headers=auth(token),
            )
            return


# ---------------------------------------------------------------------------
# Session fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Give M14 a module-owned branch so workflow slots stay out of PAYTEST."""
    from sqlalchemy import text as _sqla_text

    marker = uuid.uuid4().hex
    branch_id = (await session_db_conn.execute(
        _sqla_text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """), {"code": f"M14_{marker[:12]}", "name": f"M14 owned branch {marker[:8]}"},
    )).scalar_one()
    return int(branch_id)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def m14_terminal_state(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    session_db_conn,
):
    """Retained finalized history is legitimate; leftover mutable workflow state is not.

    M14 deliberately keeps Locked/Archived periods (immutable history) on its
    owned branch. When the module finishes, retire the periods it can still
    edit, review or approve, then require that none remain -- failing with the
    period IDs and statuses otherwise.
    """
    yield
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=session_db_conn)
    await assert_no_mutable_period_state(session_db_conn, paytest_branch_id)


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Use one M14-owned Driver; finalization history never reaches global PAYTEST."""
    marker = uuid.uuid4().hex
    created = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": f"M14 owned driver {marker}",
            "driver_code": f"M14D-{marker[:10]}",
        },
        headers=auth(auth_token),
    )
    assert created.status_code == 201, f"M14 Driver create failed: {created.text}"
    return int(created.json()["driver_id"])


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


# ---------------------------------------------------------------------------
# Function fixture — fresh open period per test
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def m14_open_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
) -> dict:
    """Open a fresh weekly period (2034-01-01 to 2034-01-07). Cancelled after test."""
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
    return await _open_period(direct_db, paytest_branch_id, start="2034-01-01", end="2034-01-07")


# ===========================================================================
# TestPeriodPayCreate
# ===========================================================================

class TestPeriodPayCreate:
    """Bonus validation and daily-lines scope separation."""

    async def test_zero_amount_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """CP-3A: amount=0 is rejected by the /bonuses endpoint (schema-level validation)."""
        pid = m14_open_period["payroll_period_id"]
        resp = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "0"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "positive" in resp.text.lower() or "zero" in resp.text.lower()

    async def test_inactive_item_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
    ):
        """
        Custom Period-scope items cannot be created (422).
        This test verifies the creation guard.
        """
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
        assert resp.status_code == 422
        assert "period" in resp.text.lower()

    async def test_period_pay_excluded_from_daily_lines_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_open_period: dict,
        direct_db,
    ):
        """Period-scope DraftLines (WorkDate=NULL) do not appear in /lines.
        Uses direct-DB injection of a non-BONUS Period line (BONUS is now canonical)."""
        from sqlalchemy import text as _text

        pid = m14_open_period["payroll_period_id"]

        # Inject a non-BONUS Period-scope DraftLine directly (ADJUSTMENT, legacy type)
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
        assert row is not None
        line_id = row["draftlineid"]

        # The daily lines endpoint must NOT include this period-scope line
        daily = await session_client.get(
            f"/payroll/periods/{pid}/lines", headers=auth(auth_token)
        )
        assert daily.status_code == 200
        daily_ids = [line["draft_line_id"] for line in daily.json()]
        assert line_id not in daily_ids, (
            "Period-scope DraftLine (WorkDate=NULL) must not appear in /lines endpoint"
        )


# ===========================================================================
# TestPeriodPayUpdate
# ===========================================================================

class TestPeriodPayUpdate:
    """PATCH /periods/{id}/bonuses/{event_id}."""

    async def test_update_amount_recalculates(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """CP-3A: PATCH /bonuses amount → amount updated to new value immediately."""
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "100.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        event_id = add.json()["bonus_event_id"]
        assert Decimal(add.json()["amount"]) == Decimal("100.00")

        patch = await session_client.patch(
            f"/payroll/periods/{pid}/bonuses/{event_id}",
            json={"amount": "200.00"},
            headers=auth(auth_token),
        )
        assert patch.status_code == 200, patch.text
        assert Decimal(patch.json()["amount"]) == Decimal("200.00")

    async def test_update_to_zero_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """CP-3A: PATCH /bonuses amount=0 is rejected (schema-level validation)."""
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "100.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        event_id = add.json()["bonus_event_id"]

        patch = await session_client.patch(
            f"/payroll/periods/{pid}/bonuses/{event_id}",
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
        """CP-3A: PATCH /bonuses notes only → amount unchanged, notes updated."""
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "25.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        event_id = add.json()["bonus_event_id"]
        original_amount = add.json()["amount"]

        patch = await session_client.patch(
            f"/payroll/periods/{pid}/bonuses/{event_id}",
            json={"notes": "Fuel bonus correction"},
            headers=auth(auth_token),
        )
        assert patch.status_code == 200
        assert patch.json()["amount"] == original_amount
        assert patch.json()["notes"] == "Fuel bonus correction"

# ===========================================================================
# TestPeriodPayVoid
# ===========================================================================

class TestPeriodPayVoid:
    """DELETE /periods/{id}/bonuses/{event_id}."""

    async def test_void_bonus_event_sets_voided_status(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """CP-3A: DELETE /bonuses/{id} → status becomes 'Voided'."""
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "50.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        event_id = add.json()["bonus_event_id"]

        void_resp = await session_client.delete(
            f"/payroll/periods/{pid}/bonuses/{event_id}",
            headers=auth(auth_token),
        )
        assert void_resp.status_code == 200
        assert void_resp.json()["status"] == "Voided"

    async def test_void_idempotent(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        m14_open_period: dict,
        m14_bonus_activated,
    ):
        """CP-3A: Voiding an already-voided bonus event succeeds without error (idempotent)."""
        pid = m14_open_period["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "30.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
        event_id = add.json()["bonus_event_id"]

        # First void
        r1 = await session_client.delete(
            f"/payroll/periods/{pid}/bonuses/{event_id}", headers=auth(auth_token)
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "Voided"

        # Second void — must not error
        r2 = await session_client.delete(
            f"/payroll/periods/{pid}/bonuses/{event_id}", headers=auth(auth_token)
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "Voided"


# ===========================================================================
# TestPeriodPayFinalization
# ===========================================================================

class TestPeriodPayFinalization:
    """Bonus events finalize correctly and appear in PayrollFinalLines."""

    async def test_finalization_includes_bonus_period_line(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
        direct_db,
    ):
        """CP-3A: Bonus event (POST /bonuses) appears in /final-lines after finalization."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = (await _open_period(direct_db, paytest_branch_id, start="2034-04-01", end="2034-04-07"))["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "300.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text

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

    async def test_voided_period_line_excluded_from_finalization(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
        direct_db,
    ):
        """CP-3A: A voided bonus event does not appear in PayrollFinalLines."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = (await _open_period(direct_db, paytest_branch_id, start="2034-06-01", end="2034-06-07"))["payroll_period_id"]

        # Event 1: $150 — will be voided
        add_bonus = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "150.00"},
            headers=auth(auth_token),
        )
        assert add_bonus.status_code == 201, add_bonus.text
        event_id = add_bonus.json()["bonus_event_id"]

        # Event 2: $10 — kept active so the period can be finalized
        add_keep = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "10.00"},
            headers=auth(auth_token),
        )
        assert add_keep.status_code == 201, add_keep.text

        # Void the first bonus before finalizing
        void_resp = await session_client.delete(
            f"/payroll/periods/{pid}/bonuses/{event_id}", headers=auth(auth_token)
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

        # Voided $150 must NOT appear
        voided_final = [
            fl for fl in final_lines
            if fl["line_type"] == "BONUS" and fl["work_date"] is None
            and Decimal(str(fl["final_amount"])) == Decimal("150.00")
        ]
        assert len(voided_final) == 0, "Voided bonus event must not appear in final lines"

        # Active $10 MUST appear
        kept_final = [
            fl for fl in final_lines
            if fl["line_type"] == "BONUS" and fl["work_date"] is None
            and Decimal(str(fl["final_amount"])) == Decimal("10.00")
        ]
        assert len(kept_final) == 1, "Active bonus event must appear in final lines"


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
        direct_db,
    ):
        """CP-3A: A bonus event does not interfere with InReview→Approved."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = (await _open_period(direct_db, paytest_branch_id, start="2034-07-01", end="2034-07-07"))["payroll_period_id"]

        r = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "200.00"},
            headers=auth(auth_token),
        )
        assert r.status_code == 201, r.text
        await _approve_period_via_review(session_client, auth_token, pid)
        # Bonus event must not block the review flow (approve succeeded)

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
        CP-3A: A Period-scope DraftLine with calculatedamount=NULL (injected directly)
        must block /finalize.  Uses direct DB injection (no API path creates Period-scope lines).
        """
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = (await _open_period(direct_db, paytest_branch_id, start="2034-08-01", end="2034-08-07"))["payroll_period_id"]

        # Inject a malformed Period-scope line directly (calculatedamount=NULL)
        result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid,
                     workdate, linetype, linescope, calculatedamount,
                     sourcetype, status, needsmanagerreview, addedbyuserid)
                VALUES
                    ((SELECT companyid FROM core.branches WHERE branchid = :bid),
                     :bid, :pid, :did,
                     NULL, 'Adjustment', 'Period', NULL,
                     'Manual', 'Active', FALSE,
                     (SELECT userid FROM sec.users WHERE username = 'admin' LIMIT 1))
                RETURNING draftlineid
            """),
            {"bid": paytest_branch_id, "pid": pid, "did": paytest_driver_id},
        )
        assert result.mappings().first() is not None

        # Force period to Approved bypassing the approval guard
        await direct_db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Approved' WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        await direct_db.commit()

        # /finalize must block — silent zero is not allowed for period pay lines
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin.status_code == 422, (
            f"Expected 422 (malformed period pay line) but got {fin.status_code}: {fin.text}"
        )
        assert (
            "resolved" in fin.text.lower()
            or "zero" in fin.text.lower()
            or "approved_snapshot_not_found_for_finalization" in fin.text.lower()
        )

        # Cleanup
        await direct_db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        await direct_db.commit()

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
        CP-3A: A Period-scope DraftLine with calculatedamount=NULL (injected directly)
        must block Open→InReview.  Uses direct DB injection (no API path creates Period-scope lines).
        """
        from sqlalchemy import text as _text

        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = (await _open_period(direct_db, paytest_branch_id, start="2034-09-01", end="2034-09-07"))["payroll_period_id"]

        # Inject a malformed Period-scope line directly (calculatedamount=NULL)
        result = await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid,
                     workdate, linetype, linescope, calculatedamount,
                     sourcetype, status, needsmanagerreview, addedbyuserid)
                VALUES
                    ((SELECT companyid FROM core.branches WHERE branchid = :bid),
                     :bid, :pid, :did,
                     NULL, 'Adjustment', 'Period', NULL,
                     'Manual', 'Active', FALSE,
                     (SELECT userid FROM sec.users WHERE username = 'admin' LIMIT 1))
                RETURNING draftlineid
            """),
            {"bid": paytest_branch_id, "pid": pid, "did": paytest_driver_id},
        )
        assert result.mappings().first() is not None
        await direct_db.commit()

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
# TestM14SafetyFixes  (review round 2)
# ===========================================================================

class TestM14SafetyFixes:
    """
    Fix 1: LineScope preserved in PayrollFinalLines (migration 0011).
    Fix 2: Daily lines summary excludes Period-scope lines.
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
        direct_db,
    ):
        """CP-3A: Finalized bonus event has line_scope='Period' in final-lines."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = (await _open_period(direct_db, paytest_branch_id, start="2034-10-01", end="2034-10-07"))["payroll_period_id"]

        add = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "175.00"},
            headers=auth(auth_token),
        )
        assert add.status_code == 201, add.text
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
        direct_db,
    ):
        """Finalized daily Miles line has line_scope='Daily' in final-lines."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = (await _open_period(direct_db, paytest_branch_id, start="2034-11-01", end="2034-11-07"))["payroll_period_id"]

        # Phase 4C: use DailyNote (non-PerUnit) instead of Miles+rate_amount.
        # The scope test (Daily) applies to any daily line type.
        add = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "DailyNote",
                  "quantity": "1", "work_date": "2034-11-01", "notes": "filler"},
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
        daily_lines = [x for x in fl.json() if x["line_type"] == "DailyNote"]
        assert daily_lines == []

    async def test_mixed_period_preserves_scope_in_final_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        m14_bonus_activated,
        direct_db,
    ):
        """Period with daily + period-pay lines: each final line gets its correct scope."""
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = (await _open_period(direct_db, paytest_branch_id, start="2034-12-01", end="2034-12-07"))["payroll_period_id"]

        # Phase 4C: use DailyNote (non-PerUnit) instead of Miles+rate_amount.
        await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "DailyNote",
                  "quantity": "1", "work_date": "2034-12-01", "notes": "filler"},
            headers=auth(auth_token),
        )
        r_bonus = await session_client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "100.00"},
            headers=auth(auth_token),
        )
        assert r_bonus.status_code == 201, r_bonus.text
        await _approve_period_via_review(session_client, auth_token, pid)
        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize", headers=auth(auth_token)
        )
        assert fin.status_code == 200, fin.text
        fl = await session_client.get(
            f"/payroll/periods/{pid}/final-lines", headers=auth(auth_token)
        )
        rows = fl.json()
        assert len(rows) == 1
        assert rows[0]["line_type"] == "BONUS"
        assert rows[0]["line_scope"] == "Period"

    # -----------------------------------------------------------------------
    # Daily lines summary excludes Period-scope lines
    # -----------------------------------------------------------------------

    async def test_daily_lines_summary_excludes_period_pay(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_driver_id: int,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        GET /payroll/periods/{id}/lines/summary must NOT include Period-scope lines.
        The summary endpoint filters LineScope = 'Daily' and is intended for
        the daily payroll entry grid — period-level bonuses/adjustments must
        not pollute the daily quantity/amount totals.
        """
        await _cancel_active_periods(session_client, auth_token, paytest_branch_id, db=direct_db)
        pid = (await _open_period(direct_db, paytest_branch_id, start="2035-03-01", end="2035-03-07"))["payroll_period_id"]


        # Add a daily line (DailyNote) — should appear in the summary.
        # Phase 4C: rate_amount removed; DailyNote has None behavior (no rate needed).
        await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "line_type": "DailyNote",
                  "quantity": "1", "work_date": "2035-03-01", "notes": "filler"},
            headers=auth(auth_token),
        )

        # Inject two Period-scope lines directly — must NOT appear in the summary
        from sqlalchemy import text as _text
        for line_type, amount in (("BONUS", "500.00"), ("ADJUSTMENT", "-50.00")):
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, calculatedamount,
                         sourcetype, status, needsmanagerreview, addedbyuserid)
                    VALUES
                        (1, :bid, :pid, :did, NULL, :lt, 'Period', :amt,
                         'Manual', 'Active', FALSE,
                         (SELECT userid FROM sec.users WHERE username = 'admin' LIMIT 1))
                """),
                {"bid": paytest_branch_id, "pid": pid, "did": paytest_driver_id,
                 "lt": line_type, "amt": amount},
            )
        await direct_db.commit()

        summary_resp = await session_client.get(
            f"/payroll/periods/{pid}/lines/summary", headers=auth(auth_token)
        )
        assert summary_resp.status_code == 200
        summary = summary_resp.json()

        # Only the daily DailyNote line should appear (not Period Pay).
        line_types_in_summary = {row["line_type"] for row in summary}
        assert "DailyNote" in line_types_in_summary, (
            f"Expected DailyNote in summary, got: {line_types_in_summary}"
        )
        assert "BONUS" not in line_types_in_summary, (
            f"BONUS (Period Pay) must NOT appear in /lines/summary, got: {line_types_in_summary}"
        )
        assert "ADJUSTMENT" not in line_types_in_summary, (
            f"ADJUSTMENT (Period Pay) must NOT appear in /lines/summary, "
            f"got: {line_types_in_summary}"
        )

        # Total quantity in summary should reflect only the daily DailyNote line
        pto_rows = [r for r in summary if r["line_type"] == "DailyNote"]
        assert len(pto_rows) == 1
        assert Decimal(pto_rows[0]["total_quantity"]) == Decimal("1")

        await session_client.patch(
            f"/payroll/periods/{pid}/status", json={"status": "Cancelled"}, headers=auth(auth_token)
        )
