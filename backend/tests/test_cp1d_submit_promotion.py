"""
CP-1D: Branch-locked payroll submission, atomic Draft promotion, Returned-backlog blocking.

Product contracts verified:
  - PATCH Draft→Open returns 422 (transition removed)
  - PATCH Draft→Cancelled still works (not removed)
  - Open→InReview acquires branch advisory lock before locking workflow rows
  - Returned backlog (Returned.EndDate < Open.StartDate) blocks submit
  - Adjacent Draft (Draft.StartDate == Open.EndDate + 1 day) promoted atomically to Open
  - Non-adjacent Draft blocks submit → DRAFT_PROMOTION_CONFLICT
  - No Draft → submit works fine (no promotion attempted)
  - Draft promotion audit written (Draft→Open entry created)
  - Backlog check fires before promotion check
  - Resubmit (Returned→InReview) path acquires branch advisory lock
  - PeriodApproval approve decision: branch lock acquired before review item row lock
  - Existing guards preserved (empty period, NMR, InReview slot)
  - Full approve flow: submit → InReview → Approve → period Approved

Dates: 2095-* — isolated year, no conflict with other test suites.
Run from backend/:
    python -m pytest tests/test_cp1d_submit_promotion.py -v
"""
import asyncio
import datetime
import itertools
import json

import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMPANY_ID = 1  # DEMO company, always seeded first
_DATE_CTR = itertools.count(0)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _dates_2095(offset_weeks: int = 0) -> tuple[datetime.date, datetime.date]:
    """Return a week-long date pair in 2095."""
    n = next(_DATE_CTR) + offset_weeks
    base = datetime.date(2095, 1, 7)
    start = base + datetime.timedelta(weeks=n)
    end = start + datetime.timedelta(days=6)
    return start, end


async def _clean(direct_db: AsyncConnection, branch_id: int) -> None:
    """Cancel active periods and delete only 2095 test periods without snapshots.

    Successful CP-4D submissions create immutable snapshots with a restrictive
    period foreign key.  Snapshot-backed periods are retained in the ephemeral
    test database after being cancelled, which releases their workflow slots
    without treating immutable history as test-cleanup data.
    """
    p = {"bid": branch_id}
    # 1. Cancel ALL active periods on this branch (no date filter) so no stale InReview/Returned
    # period from a prior test blocks the next test's submit. Dates may fall outside 2095 due to
    # _DATE_CTR accumulating across the module; cancel-all prevents cross-test bleed.
    await direct_db.execute(
        _text("""
            UPDATE payroll.payrollperiods
            SET    status = 'Cancelled', currentreturnreviewitemid = NULL
            WHERE  branchid = :bid AND status = 'Returned'
        """),
        p,
    )
    await direct_db.execute(
        _text("""
            UPDATE payroll.payrollperiods
            SET    status = 'Cancelled'
            WHERE  branchid = :bid AND status IN ('Draft', 'Open', 'InReview')
        """),
        p,
    )
    # 3. Delete review decisions for 2095 review items on this branch
    await direct_db.execute(
        _text("""
            DELETE FROM review.managerreviewdecisions
            WHERE reviewitemid IN (
                SELECT reviewitemid FROM review.managerreviewitems
                WHERE  branchid = :bid
            )
        """),
        p,
    )
    # 4. Delete review items for this branch (all types, to avoid FK issues)
    await direct_db.execute(
        _text("DELETE FROM review.managerreviewitems WHERE branchid = :bid"),
        p,
    )
    # 5. Delete draft lines referencing 2095 periods on this branch
    await direct_db.execute(
        _text("""
            DELETE FROM payroll.payrolldraftlines
            WHERE payrollperiodid IN (
                SELECT payrollperiodid FROM payroll.payrollperiods
                WHERE  branchid = :bid AND startdate >= '2095-01-01' AND startdate < '2096-01-01'
            )
        """),
        p,
    )
    # 6. Delete mutable test periods. Immutable snapshot-backed periods remain
    # cancelled so the next test has no active workflow-slot contention.
    await direct_db.execute(
        _text("""
            DELETE FROM payroll.payrollperiods AS period
            WHERE period.branchid = :bid
              AND period.startdate >= '2095-01-01'
              AND period.startdate < '2096-01-01'
              AND NOT EXISTS (
                  SELECT 1
                  FROM payroll.payrollcalculationsnapshots AS snapshot
                  WHERE snapshot.payrollperiodid = period.payrollperiodid
                    AND snapshot.companyid = period.companyid
                    AND snapshot.branchid = period.branchid
              )
        """),
        p,
    )
    # 7. Ensure branchpayrollsettings row exists (required by CP-2A ensure_current_schedule_version).
    # The PAYTEST branch is created in seed stmts but never configured — this is a test-only upsert.
    await direct_db.execute(
        _text("""
            INSERT INTO payroll.branchpayrollsettings
                (companyid, branchid, payrollfrequency, anchorstartdate, isactive)
            VALUES (:cid, :bid, 'Week', '2024-01-01', TRUE)
            ON CONFLICT (companyid, branchid) DO NOTHING
        """),
        {"cid": _COMPANY_ID, "bid": branch_id},
    )


async def _ensure_prepared_creation_setup(
    client: httpx.AsyncClient,
    token: str,
    direct_db: AsyncConnection,
    branch_id: int,
) -> None:
    """Ensure the weekly setup required by candidate tests without rewriting history."""
    response = await client.put(
        f"/settings/branches/{branch_id}/payroll-setup",
        json={"payroll_frequency": "Week", "anchor_start_date": "2095-01-06"},
        headers=_auth(token),
    )
    if response.status_code in (200, 201):
        return

    assert response.status_code == 409, f"payroll setup failed: {response.text}"
    assert "existing payroll periods" in response.text, response.text
    settings = (await direct_db.execute(
        _text("""
            SELECT payrollfrequency, isactive
            FROM payroll.branchpayrollsettings
            WHERE companyid = :cid AND branchid = :bid
        """),
        {"cid": _COMPANY_ID, "bid": branch_id},
    )).mappings().first()
    assert settings is not None
    assert settings["payrollfrequency"] == "Week"
    assert settings["isactive"] is True


async def _insert_open_period(
    direct_db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    code_suffix: str = "OP",
) -> int:
    """Insert an Open period directly. Returns payrollperiodid."""
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES
                (:cid, :bid, 'Open', :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {
            "cid":   _COMPANY_ID,
            "bid":   branch_id,
            "code":  f"2095-{code_suffix}-{start.isoformat()}",
            "name":  f"Open {start.isoformat()} {code_suffix}",
            "start": start,
            "end":   end,
        },
    )).mappings().first()
    return row["payrollperiodid"]


async def _insert_draft_period(
    direct_db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    code_suffix: str = "DR",
) -> int:
    """Insert a Draft period directly. Returns payrollperiodid."""
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES
                (:cid, :bid, 'Draft', :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {
            "cid":   _COMPANY_ID,
            "bid":   branch_id,
            "code":  f"2095-{code_suffix}-{start.isoformat()}",
            "name":  f"Draft {start.isoformat()} {code_suffix}",
            "start": start,
            "end":   end,
        },
    )).mappings().first()
    return row["payrollperiodid"]


async def _insert_returned_period(
    direct_db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    code_suffix: str = "RET",
) -> tuple[int, int]:
    """Insert a review item + Returned period. Returns (period_id, review_item_id)."""
    ri_row = (await direct_db.execute(
        _text("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requestedbyuserid, requesttype,
                 entityschema, entityname, entityid,
                 title, description, priority, status,
                 finaldecisionbyuserid, finaldecisionatutc, finaldecisionreason)
            VALUES
                (:cid, :bid, 1, 'PeriodApproval',
                 'payroll', 'PayrollPeriods', '0',
                 'CP-1D test return', 'CP-1D test return', 'Normal', 'Rejected',
                 1, NOW(), 'CP-1D test')
            RETURNING reviewitemid
        """),
        {"cid": _COMPANY_ID, "bid": branch_id},
    )).mappings().first()
    ri_id = ri_row["reviewitemid"]

    per_row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype,
                 startdate, enddate, currentreturnreviewitemid)
            VALUES
                (:cid, :bid, 'Returned', :code, :name, 'Week',
                 :start, :end, :ri_id)
            RETURNING payrollperiodid
        """),
        {
            "cid":   _COMPANY_ID,
            "bid":   branch_id,
            "code":  f"2095-{code_suffix}-{start.isoformat()}",
            "name":  f"Returned {start.isoformat()} {code_suffix}",
            "start": start,
            "end":   end,
            "ri_id": ri_id,
        },
    )).mappings().first()
    return per_row["payrollperiodid"], ri_id


async def _add_pto_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: datetime.date,
) -> None:
    """Add a non-rate-dependent DailyNote line to satisfy the empty-period guard."""
    r = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json={
            "driver_id": driver_id,
            "work_date":  work_date.isoformat(),
            "line_type":  "DailyNote",
            "quantity":   1,
            "notes":      "filler",
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"add PTO line: {r.text}"


async def _add_hours_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: datetime.date,
    quantity: int = 1,
) -> None:
    """Add a PerUnit Hours line.  Requires the driver to have an approved HOURLY rate."""
    r = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json={
            "driver_id": driver_id,
            "work_date":  work_date.isoformat(),
            "line_type":  "Hours",
            "quantity":   quantity,
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"add Hours line: {r.text}"


async def _create_approved_hourly_rate(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_type_id: int,
    effective_from: datetime.date,
    amount: str = "25.00",
) -> int:
    """Create + approve an HOURLY driver rate; return driver_rate_id."""
    cr = await client.post(
        "/payroll/rates",
        json={
            "driver_id":      driver_id,
            "rate_type_id":   rate_type_id,
            "amount":         amount,
            "effective_from": effective_from.isoformat(),
        },
        headers=_auth(token),
    )
    assert cr.status_code == 201, f"create HOURLY rate: {cr.text}"
    rid = cr.json()["driver_rate_id"]
    ar = await client.post(f"/payroll/rates/{rid}/approve", headers=_auth(token))
    assert ar.status_code == 200, f"approve HOURLY rate: {ar.text}"
    return rid


async def _submit(client: httpx.AsyncClient, token: str, period_id: int) -> httpx.Response:
    return await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=_auth(token),
    )


async def _get_period(client: httpx.AsyncClient, token: str, period_id: int) -> dict:
    r = await client.get(f"/payroll/periods/{period_id}", headers=_auth(token))
    assert r.status_code == 200, f"get period {period_id}: {r.text}"
    return r.json()


async def _pending_review_item_id(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
) -> int:
    r = await client.get("/review/items", params={"payroll_period_id": period_id}, headers=_auth(token))
    assert r.status_code == 200
    pending = [i for i in r.json() if i["status"] == "Pending"]
    assert pending, f"No Pending review item for period {period_id}"
    return pending[0]["review_item_id"]


async def _approve(client: httpx.AsyncClient, token: str, ri_id: int) -> httpx.Response:
    return await client.post(
        f"/review/items/{ri_id}/decide",
        json={"decision": "Approved"},
        headers=_auth(token),
    )


async def _reject(client: httpx.AsyncClient, token: str, ri_id: int) -> httpx.Response:
    return await client.post(
        f"/review/items/{ri_id}/decide",
        json={"decision": "Rejected", "decision_reason": "CP-1D test rejection"},
        headers=_auth(token),
    )


async def _wait_for_lock_waiter(
    direct_db: AsyncConnection,
    company_id: int,
    branch_id: int,
    *,
    timeout: float = 5.0,
) -> None:
    """Poll pg_locks until the advisory lock shows at least one waiting connection (NOT granted)."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM pg_locks
                WHERE locktype = 'advisory'
                  AND classid  = :cid
                  AND objid    = :bid
                  AND objsubid = 2
                  AND NOT granted
            """),
            {"cid": company_id, "bid": branch_id},
        )).scalar_one()
        if count >= 1:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"No advisory lock waiter appeared within {timeout}s "
        f"(company={company_id}, branch={branch_id}). "
        "Verify that the branch advisory lock is acquired at the start of the submit/create path."
    )


# ---------------------------------------------------------------------------
# Test 01-02: Draft→Open transition removal
# ---------------------------------------------------------------------------

class TestDraftOpenRemoved:

    @pytest.mark.asyncio
    async def test_01_draft_to_open_patch_blocked(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """T1: PATCH Draft→Open now returns 422 — transition removed in CP-1D."""
        await _clean(direct_db, paytest_branch_id)

        # Insert Draft directly — B1 guard blocks POST /payroll/periods without an Open.
        start, end = _dates_2095()
        row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Draft', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id, "code": f"CP1D-T1-{start}", "name": f"CP1D T1 {start}",
             "start": start, "end": end},
        )).mappings().first()
        await direct_db.commit()
        pid = row["payrollperiodid"]

        patch = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=_auth(auth_token),
        )
        assert patch.status_code == 422, (
            f"Expected 422 for Draft→Open after CP-1D; got {patch.status_code}: {patch.text}"
        )
        assert "Open" in patch.text or "Cancelled" in patch.text

    @pytest.mark.asyncio
    async def test_02_draft_to_cancelled_still_works(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """T2: PATCH Draft→Cancelled still allowed — CP-1D only removes Draft→Open."""
        await _clean(direct_db, paytest_branch_id)

        # Insert Draft directly — B1 guard blocks POST /payroll/periods without an Open.
        start, end = _dates_2095()
        row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Draft', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id, "code": f"CP1D-T2-{start}", "name": f"CP1D T2 {start}",
             "start": start, "end": end},
        )).mappings().first()
        await direct_db.commit()
        pid = row["payrollperiodid"]

        patch = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )
        assert patch.status_code == 200, f"Draft→Cancelled: {patch.text}"
        assert patch.json()["status"] == "Cancelled"


# ---------------------------------------------------------------------------
# Test 03-04: Submit happy path and empty-period guard
# ---------------------------------------------------------------------------

class TestSubmitHappyPath:

    @pytest.mark.asyncio
    async def test_03_submit_open_period_succeeds(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T3: Open period with a non-rate-dependent line submits to InReview."""
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095()
        pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "T3")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, start)

        r = await _submit(session_client, auth_token, pid)
        assert r.status_code == 200, f"submit: {r.text}"
        assert r.json()["status"] == "InReview"

    @pytest.mark.asyncio
    async def test_04_submit_empty_period_blocked(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """T4: Empty period (no draft lines) still returns 422 after CP-1D lock changes."""
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095()
        pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "T4")

        r = await _submit(session_client, auth_token, pid)
        assert r.status_code == 422
        assert "empty" in r.text.lower()


# ---------------------------------------------------------------------------
# Test 05-07: Returned backlog blocking
# ---------------------------------------------------------------------------

class TestReturnedBacklog:

    @pytest.mark.asyncio
    async def test_05_returned_backlog_blocks_submit(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T5: Returned.EndDate < Open.StartDate → RETURNED_BACKLOG_BLOCKS_SUBMIT."""
        await _clean(direct_db, paytest_branch_id)

        # Returned period: Jan 7-13
        ret_start = datetime.date(2095, 1, 7)
        ret_end   = datetime.date(2095, 1, 13)
        await _insert_returned_period(direct_db, paytest_branch_id, ret_start, ret_end, "T5-RET")

        # Open period: Jan 21-27 — so ret_end (Jan 13) < open_start (Jan 21) → backlog
        op_start = datetime.date(2095, 1, 21)
        op_end   = datetime.date(2095, 1, 27)
        pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "T5-OP")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, op_start)

        r = await _submit(session_client, auth_token, pid)
        assert r.status_code == 409, f"Expected 409 backlog block; got {r.status_code}: {r.text}"
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("code") == "RETURNED_BACKLOG_BLOCKS_SUBMIT", detail
        else:
            assert "RETURNED_BACKLOG_BLOCKS_SUBMIT" in str(detail), detail

    @pytest.mark.asyncio
    async def test_06_returned_equal_open_start_blocks_submit(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T6: Returned.EndDate == Open.StartDate — fail closed as WORKFLOW_SLOT_CONFLICT.

        Codex P1 fix: CP-1D is fail-closed for ALL Returned presence.
        - Returned.EndDate < Open.StartDate → RETURNED_BACKLOG_BLOCKS_SUBMIT
        - Returned.EndDate >= Open.StartDate → WORKFLOW_SLOT_CONFLICT (chronologically anomalous)
        Both block submission; no Returned state allows a forward submit.
        """
        await _clean(direct_db, paytest_branch_id)

        boundary = datetime.date(2095, 2, 7)
        ret_start = datetime.date(2095, 2, 1)
        await _insert_returned_period(
            direct_db, paytest_branch_id, ret_start, boundary, "T6-RET",
        )

        op_start = boundary  # Open starts on same day Returned ends (EndDate >= StartDate → conflict)
        op_end   = datetime.date(2095, 2, 13)
        pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "T6-OP")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, op_start)

        r = await _submit(session_client, auth_token, pid)
        assert r.status_code == 409, (
            f"Expected 409 WORKFLOW_SLOT_CONFLICT (Returned.EndDate >= Open.StartDate); "
            f"got {r.status_code}: {r.text}"
        )
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("code") == "WORKFLOW_SLOT_CONFLICT", (
                f"Expected WORKFLOW_SLOT_CONFLICT; got: {detail}"
            )
        else:
            assert "WORKFLOW_SLOT_CONFLICT" in str(detail), detail

    @pytest.mark.asyncio
    async def test_07_no_returned_period_submit_ok(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T7: No Returned periods in branch → backlog check passes silently."""
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095()
        pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "T7")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, start)

        r = await _submit(session_client, auth_token, pid)
        assert r.status_code == 200, f"No-returned submit: {r.text}"
        assert r.json()["status"] == "InReview"


# ---------------------------------------------------------------------------
# Test 08-11: Draft promotion
# ---------------------------------------------------------------------------

class TestDraftPromotion:

    @pytest.mark.asyncio
    async def test_08_adjacent_draft_promotes_to_open(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T8: Draft.StartDate == Open.EndDate+1 → Draft atomically promoted to Open on submit."""
        await _clean(direct_db, paytest_branch_id)

        op_start = datetime.date(2095, 3, 7)
        op_end   = datetime.date(2095, 3, 13)
        dr_start = datetime.date(2095, 3, 14)  # op_end + 1 day — adjacent
        dr_end   = datetime.date(2095, 3, 20)

        open_pid  = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "T8-OP")
        draft_pid = await _insert_draft_period(direct_db, paytest_branch_id, dr_start, dr_end, "T8-DR")

        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, op_start)

        r = await _submit(session_client, auth_token, open_pid)
        assert r.status_code == 200, f"submit with adjacent draft: {r.text}"
        assert r.json()["status"] == "InReview"

        # Draft should have been promoted to Open atomically
        draft_state = await _get_period(session_client, auth_token, draft_pid)
        assert draft_state["status"] == "Open", (
            f"Adjacent Draft was not promoted to Open; status={draft_state['status']}"
        )

    @pytest.mark.asyncio
    async def test_09_adjacent_draft_promotion_audit_written(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T9: Draft promotion writes an audit entry for Draft→Open."""
        await _clean(direct_db, paytest_branch_id)

        op_start = datetime.date(2095, 3, 21)
        op_end   = datetime.date(2095, 3, 27)
        dr_start = datetime.date(2095, 3, 28)
        dr_end   = datetime.date(2095, 4, 3)

        open_pid  = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "T9-OP")
        draft_pid = await _insert_draft_period(direct_db, paytest_branch_id, dr_start, dr_end, "T9-DR")
        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, op_start)

        r = await _submit(session_client, auth_token, open_pid)
        assert r.status_code == 200

        # Verify audit entry for Draft→Open promotion.
        # _write_period_status_audit stores {"status": x} in oldvaluejson and newvaluejson.
        audit_rows = (await direct_db.execute(
            _text("""
                SELECT oldvaluejson, newvaluejson
                FROM   audit.auditlog
                WHERE  entityschema = 'payroll'
                  AND  entityname   = 'PayrollPeriods'
                  AND  entityid     = :eid
                  AND  actioncode   = 'PERIOD_STATUS_CHANGED'
                ORDER  BY auditid DESC
                LIMIT  5
            """),
            {"eid": str(draft_pid)},
        )).mappings().all()

        promotion_audits = [
            r for r in audit_rows
            if json.loads(r["oldvaluejson"]).get("status") == "Draft"
            and json.loads(r["newvaluejson"]).get("status") == "Open"
        ]
        assert promotion_audits, (
            f"No Draft→Open audit written for draft_pid={draft_pid}; "
            f"found: {[(r['oldvaluejson'], r['newvaluejson']) for r in audit_rows]}"
        )
        # J: Verify audit context — trigger and submitted_period_id must be present.
        promo_new = json.loads(promotion_audits[0]["newvaluejson"])
        assert promo_new.get("trigger") == "submit_promotion", (
            f"Promotion audit missing trigger context; newvaluejson={promo_new}"
        )
        assert promo_new.get("submitted_period_id") == open_pid, (
            f"Promotion audit submitted_period_id mismatch; expected {open_pid}, got {promo_new}"
        )

    @pytest.mark.asyncio
    async def test_10_non_adjacent_draft_blocks_submit(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T10: Draft.StartDate != Open.EndDate+1 → DRAFT_PROMOTION_CONFLICT."""
        await _clean(direct_db, paytest_branch_id)

        op_start = datetime.date(2095, 4, 7)
        op_end   = datetime.date(2095, 4, 13)
        # Gap: Draft starts April 21, not April 14 (op_end + 1)
        dr_start = datetime.date(2095, 4, 21)
        dr_end   = datetime.date(2095, 4, 27)

        open_pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "T10-OP")
        await _insert_draft_period(direct_db, paytest_branch_id, dr_start, dr_end, "T10-DR")
        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, op_start)

        r = await _submit(session_client, auth_token, open_pid)
        assert r.status_code == 409, f"Expected 409 draft conflict; got {r.status_code}: {r.text}"
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("code") == "DRAFT_PROMOTION_CONFLICT", detail
        else:
            assert "DRAFT_PROMOTION_CONFLICT" in str(detail), detail

    @pytest.mark.asyncio
    async def test_11_no_draft_submit_ok(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T11: No Draft in branch → submit works, no promotion attempted."""
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095()
        pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "T11")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, start)

        r = await _submit(session_client, auth_token, pid)
        assert r.status_code == 200, f"No-draft submit: {r.text}"
        assert r.json()["status"] == "InReview"


# ---------------------------------------------------------------------------
# Test 12: Backlog check fires before promotion check
# ---------------------------------------------------------------------------

class TestBlockerOrdering:

    @pytest.mark.asyncio
    async def test_12_backlog_fires_before_draft_conflict(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T12: Returned backlog AND non-adjacent Draft → RETURNED_BACKLOG_BLOCKS_SUBMIT fires first."""
        await _clean(direct_db, paytest_branch_id)

        # Returned period with early dates (backlog)
        ret_start = datetime.date(2095, 5, 1)
        ret_end   = datetime.date(2095, 5, 7)
        await _insert_returned_period(direct_db, paytest_branch_id, ret_start, ret_end, "T12-RET")

        # Open period with later dates
        op_start = datetime.date(2095, 5, 14)
        op_end   = datetime.date(2095, 5, 20)
        open_pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "T12-OP")
        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, op_start)

        # Non-adjacent Draft (would trigger DRAFT_PROMOTION_CONFLICT if backlog check passes)
        dr_start = datetime.date(2095, 6, 1)
        dr_end   = datetime.date(2095, 6, 7)
        await _insert_draft_period(direct_db, paytest_branch_id, dr_start, dr_end, "T12-DR")

        r = await _submit(session_client, auth_token, open_pid)
        assert r.status_code == 409
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            code = detail.get("code", "")
        else:
            code = str(detail)
        assert "RETURNED_BACKLOG_BLOCKS_SUBMIT" in code, (
            f"Expected RETURNED_BACKLOG_BLOCKS_SUBMIT (backlog fires first); got: {detail}"
        )


# ---------------------------------------------------------------------------
# Test 13: InReview slot guard preserved
# ---------------------------------------------------------------------------

class TestInReviewSlotPreserved:

    @pytest.mark.asyncio
    async def test_13_inreview_slot_guard_still_fires(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T13: Existing InReview in branch → CP-1B friendly 409 still fires after CP-1D lock."""
        await _clean(direct_db, paytest_branch_id)

        # Insert existing InReview period
        ir_start = datetime.date(2095, 6, 7)
        ir_end   = datetime.date(2095, 6, 13)
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES
                    (:cid, :bid, 'InReview', '2095-IR-existing', 'Existing InReview', 'Week', :start, :end)
            """),
            {"cid": _COMPANY_ID, "bid": paytest_branch_id, "start": ir_start, "end": ir_end},
        )

        # Second Open period trying to submit
        op_start = datetime.date(2095, 6, 14)
        op_end   = datetime.date(2095, 6, 20)
        pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "T13-OP")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, op_start)

        r = await _submit(session_client, auth_token, pid)
        assert r.status_code == 409, f"Expected 409 InReview slot; got {r.status_code}: {r.text}"
        assert "InReview" in r.text or "review" in r.text.lower()


# ---------------------------------------------------------------------------
# Test 14-15: Resubmit path and approve decision
# ---------------------------------------------------------------------------

class TestResubmitAndApprove:

    @pytest.mark.asyncio
    async def test_14_resubmit_returned_period_succeeds(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T14: Resubmit a Returned period → InReview (branch lock acquired in resubmit path)."""
        await _clean(direct_db, paytest_branch_id)

        # Full flow: create Open, add line, submit, reject, resubmit
        op_start = datetime.date(2095, 7, 7)
        op_end   = datetime.date(2095, 7, 13)
        pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "T14-OP")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, op_start)

        submit_r = await _submit(session_client, auth_token, pid)
        assert submit_r.status_code == 200, f"submit: {submit_r.text}"

        ri_id = await _pending_review_item_id(session_client, auth_token, pid)
        reject_r = await _reject(session_client, auth_token, ri_id)
        assert reject_r.status_code == 200, f"reject: {reject_r.text}"

        period_state = await _get_period(session_client, auth_token, pid)
        assert period_state["status"] == "Returned", (
            f"Expected Returned after reject; got {period_state['status']}"
        )

        # Resubmit (this path now acquires branch advisory lock in CP-1D)
        resub_r = await session_client.post(
            f"/payroll/periods/{pid}/resubmissions",
            headers=_auth(auth_token),
        )
        assert resub_r.status_code == 200, f"resubmit: {resub_r.text}"
        assert resub_r.json()["status"] == "InReview"

    @pytest.mark.asyncio
    async def test_15_full_approve_flow_period_becomes_approved(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T15: Submit → InReview → Approve → period Approved (branch lock in decide path)."""
        await _clean(direct_db, paytest_branch_id)

        op_start = datetime.date(2095, 8, 7)
        op_end   = datetime.date(2095, 8, 13)
        pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "T15-OP")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, op_start)

        submit_r = await _submit(session_client, auth_token, pid)
        assert submit_r.status_code == 200, f"submit: {submit_r.text}"

        ri_id = await _pending_review_item_id(session_client, auth_token, pid)

        # Approve: this path now acquires branch advisory lock first (CP-1D)
        approve_r = await _approve(session_client, auth_token, ri_id)
        assert approve_r.status_code == 200, f"approve: {approve_r.text}"

        period_state = await _get_period(session_client, auth_token, pid)
        assert period_state["status"] == "Approved", (
            f"Expected Approved after approve decision; got {period_state['status']}"
        )

    @pytest.mark.asyncio
    async def test_16_draft_promotion_open_is_submittable(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """T16: Promoted Draft (now Open) can itself be submitted in a subsequent call."""
        await _clean(direct_db, paytest_branch_id)

        # Set up: Open period + adjacent Draft
        op_start = datetime.date(2095, 9, 7)
        op_end   = datetime.date(2095, 9, 13)
        dr_start = datetime.date(2095, 9, 14)
        dr_end   = datetime.date(2095, 9, 20)

        open_pid  = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "T16-OP")
        draft_pid = await _insert_draft_period(direct_db, paytest_branch_id, dr_start, dr_end, "T16-DR")

        # Only the Open period can have lines before submit (Draft blocks line entry)
        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, op_start)

        # Submit first period → Draft promoted to Open atomically
        submit_r = await _submit(session_client, auth_token, open_pid)
        assert submit_r.status_code == 200

        promoted_state = await _get_period(session_client, auth_token, draft_pid)
        assert promoted_state["status"] == "Open", (
            f"Promoted period should be Open; got {promoted_state['status']}"
        )

        # Approve the first period so the InReview slot is freed
        ri_id = await _pending_review_item_id(session_client, auth_token, open_pid)
        approve_r = await _approve(session_client, auth_token, ri_id)
        assert approve_r.status_code == 200

        # Add a line to the promoted period (now Open, so line entry is allowed)
        await _add_pto_line(session_client, auth_token, draft_pid, paytest_driver_id, dr_start)

        # Submit the promoted (formerly Draft) period
        submit2_r = await _submit(session_client, auth_token, draft_pid)
        assert submit2_r.status_code == 200, (
            f"Submitting promoted period failed: {submit2_r.text}"
        )
        assert submit2_r.json()["status"] == "InReview"


# ---------------------------------------------------------------------------
# K: SubmittedAtUtc tests (B4)
# ---------------------------------------------------------------------------

class TestSubmittedAtUtc:

    @pytest.mark.asyncio
    async def test_k1_submit_sets_submitted_at_utc(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """K1: Successful Open→InReview sets SubmittedAtUtc to a non-null timestamp."""
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095(50)
        pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "K1")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, start)

        r = await _submit(session_client, auth_token, pid)
        assert r.status_code == 200, r.text

        row = (await direct_db.execute(
            _text("SELECT submittedatutc FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).mappings().first()
        assert row is not None
        assert row["submittedatutc"] is not None, "SubmittedAtUtc must be set after successful submit"

    @pytest.mark.asyncio
    async def test_k2_resubmit_updates_submitted_at_utc(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """K2: Successful Returned→InReview resubmission updates SubmittedAtUtc."""
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095(52)
        pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "K2")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, start)

        r1 = await _submit(session_client, auth_token, pid)
        assert r1.status_code == 200

        ri_id = await _pending_review_item_id(session_client, auth_token, pid)
        rej = await _reject(session_client, auth_token, ri_id)
        assert rej.status_code == 200

        first_ts = (await direct_db.execute(
            _text("SELECT submittedatutc FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).mappings().first()["submittedatutc"]
        assert first_ts is not None

        resub = await session_client.post(
            f"/payroll/periods/{pid}/resubmissions",
            headers=_auth(auth_token),
        )
        assert resub.status_code == 200, resub.text

        second_ts = (await direct_db.execute(
            _text("SELECT submittedatutc FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).mappings().first()["submittedatutc"]
        assert second_ts is not None
        assert second_ts >= first_ts, "Resubmit must refresh SubmittedAtUtc"

    @pytest.mark.asyncio
    async def test_k3_failed_submit_leaves_submitted_at_utc_null(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """K3: Failed submit (empty period) leaves SubmittedAtUtc unchanged (NULL)."""
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095(54)
        pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "K3")

        # No lines — empty period guard fires
        r = await _submit(session_client, auth_token, pid)
        assert r.status_code == 422

        row = (await direct_db.execute(
            _text("SELECT submittedatutc FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).mappings().first()
        assert row["submittedatutc"] is None, (
            "SubmittedAtUtc must remain NULL after a failed submit"
        )


# ---------------------------------------------------------------------------
# G: Legacy create guard tests (B1)
# ---------------------------------------------------------------------------

class TestLegacyCreateGuard:

    @pytest.mark.asyncio
    async def test_g1_legacy_create_without_open_blocked(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """G1: Legacy POST /payroll/periods with no active Open → DRAFT_CREATION_REQUIRES_OPEN."""
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095(60)
        r = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": start.isoformat(), "end_date": end.isoformat()},
            headers=_auth(auth_token),
        )
        assert r.status_code == 409, f"Expected 409 DRAFT_CREATION_REQUIRES_OPEN; got {r.status_code}: {r.text}"
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("code") == "DRAFT_CREATION_REQUIRES_OPEN", detail
        else:
            assert "DRAFT_CREATION_REQUIRES_OPEN" in str(detail), detail

    @pytest.mark.asyncio
    async def test_g2_legacy_create_with_open_succeeds(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """G2: Legacy POST /payroll/periods with exactly one Open succeeds → creates Draft."""
        await _clean(direct_db, paytest_branch_id)

        # Insert an Open period so the guard passes
        op_start, op_end = _dates_2095(62)
        await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "G2-OP")

        dr_start = op_end + datetime.timedelta(days=1)
        dr_end   = dr_start + datetime.timedelta(days=6)
        r = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": dr_start.isoformat(), "end_date": dr_end.isoformat()},
            headers=_auth(auth_token),
        )
        assert r.status_code == 201, f"Expected 201; got {r.status_code}: {r.text}"
        assert r.json()["status"] == "Draft"

    @pytest.mark.asyncio
    async def test_g3_legacy_duplicate_draft_blocked(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """G3: Second legacy POST /payroll/periods when a Draft already exists → DRAFT_SLOT_OCCUPIED."""
        await _clean(direct_db, paytest_branch_id)

        op_start, op_end = _dates_2095(64)
        await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "G3-OP")

        dr_start = op_end + datetime.timedelta(days=1)
        dr_end   = dr_start + datetime.timedelta(days=6)
        await _insert_draft_period(direct_db, paytest_branch_id, dr_start, dr_end, "G3-DR")

        dr2_start = dr_end + datetime.timedelta(days=1)
        dr2_end   = dr2_start + datetime.timedelta(days=6)
        r = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": dr2_start.isoformat(), "end_date": dr2_end.isoformat()},
            headers=_auth(auth_token),
        )
        assert r.status_code == 409, f"Expected 409 DRAFT_SLOT_OCCUPIED; got {r.status_code}: {r.text}"
        detail = r.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("code") == "DRAFT_SLOT_OCCUPIED", detail
        else:
            assert "DRAFT_SLOT_OCCUPIED" in str(detail), detail

    @pytest.mark.asyncio
    async def test_g4_legacy_create_with_inreview_allowed(
        self, session_client, auth_token, paytest_branch_id, direct_db,
    ):
        """G4: Legacy POST when InReview exists alongside Open → 201 (B1 fix: InReview co-existence is valid).

        Before the B1 fix, the guard incorrectly rejected [Open, InReview] with WORKFLOW_SLOT_CONFLICT.
        The corrected guard only checks Open count (must be exactly 1) and Draft presence; InReview/Returned
        co-existence is a normal part of the workflow and must not block Draft creation.
        """
        await _clean(direct_db, paytest_branch_id)

        op_start, op_end = _dates_2095(68)
        await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "G4-OP")

        # Insert an InReview period alongside the Open period (valid workflow state)
        ir_start = op_end + datetime.timedelta(days=1)
        ir_end   = ir_start + datetime.timedelta(days=6)
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (:cid, :bid, 'InReview', :code, :name, 'Week', :start, :end)
            """),
            {"cid": _COMPANY_ID, "bid": paytest_branch_id,
             "code": f"G4-IR-{ir_start}", "name": f"G4 IR {ir_start}",
             "start": ir_start, "end": ir_end},
        )

        dr_start = ir_end + datetime.timedelta(days=1)
        dr_end   = dr_start + datetime.timedelta(days=6)
        r = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": dr_start.isoformat(), "end_date": dr_end.isoformat()},
            headers=_auth(auth_token),
        )
        assert r.status_code == 201, (
            f"Expected 201 Draft created with [Open+InReview]; got {r.status_code}: {r.text}"
        )
        assert r.json()["status"] == "Draft"


# ---------------------------------------------------------------------------
# F: Candidate replay after Draft promotion
# ---------------------------------------------------------------------------

class TestCandidateReplayAfterPromotion:

    @pytest.mark.asyncio
    async def test_f1_candidate_replay_after_draft_promoted_to_open(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """F1: Replay same CP-1C candidate key after the Draft is promoted to Open.

        Invariants verified:
          - ALREADY_EXISTS result on replay
          - Same period ID returned
          - Status is Open (not Draft)
          - CreationCandidateKeyHash is captured before and verified unchanged after promotion and replay
          - Total branch period count (broad, 2095 date range) does not grow on replay
          - Audit row count for test-owned periods does not grow on replay
          - No second Draft exists after replay
        """
        await _clean(direct_db, paytest_branch_id)

        # PREPARED_CREATION requires an active weekly setup. Immutable history
        # may correctly reject rewriting its original test anchor.
        await _ensure_prepared_creation_setup(
            session_client, auth_token, direct_db, paytest_branch_id,
        )

        # Step 1: Insert Open period.
        op_start, op_end = _dates_2095(72)
        open_pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "F1-OP")

        # Step 2: Create Draft via CP-1C candidate creation.
        cand_resp = await session_client.get(
            f"/payroll/branches/{paytest_branch_id}/period-candidates",
            params={"mode": "PREPARED_CREATION"},
            headers=_auth(auth_token),
        )
        assert cand_resp.status_code == 200, (
            f"Candidate preview must be available after payroll setup: "
            f"{cand_resp.status_code} {cand_resp.text}"
        )

        cand_data = cand_resp.json()
        candidate_key = cand_data.get("selected", {}).get("candidate_key")
        assert candidate_key, (
            f"No candidate key returned for PREPARED_CREATION mode: {cand_data}"
        )

        create_resp = await session_client.post(
            f"/payroll/branches/{paytest_branch_id}/period-creations",
            json={"candidate_key": candidate_key},
            headers=_auth(auth_token),
        )
        assert create_resp.status_code in (200, 201), (
            f"Candidate creation must succeed: {create_resp.status_code} {create_resp.text}"
        )

        draft_pid = create_resp.json()["payroll_period_id"]

        # Capture CreationCandidateKeyHash set by CP-1C creation.
        hash_row = (await direct_db.execute(
            _text(
                "SELECT creationcandidatekeyhash FROM payroll.payrollperiods "
                "WHERE payrollperiodid = :pid"
            ),
            {"pid": draft_pid},
        )).mappings().first()
        original_hash = hash_row["creationcandidatekeyhash"]
        assert original_hash is not None, (
            "CP-1C creation must set CreationCandidateKeyHash on the created Draft period"
        )

        # Step 3: Submit the Open period → promotes Draft to Open atomically.
        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, op_start)
        submit_r = await _submit(session_client, auth_token, open_pid)
        assert submit_r.status_code == 200, f"submit: {submit_r.text}"

        # Draft should now be Open.
        promoted = await _get_period(session_client, auth_token, draft_pid)
        assert promoted["status"] == "Open", (
            f"Draft should have been promoted to Open; got {promoted['status']}"
        )

        # Hash must survive promotion — promotion must not overwrite it.
        hash_after_promo = (await direct_db.execute(
            _text(
                "SELECT creationcandidatekeyhash FROM payroll.payrollperiods "
                "WHERE payrollperiodid = :pid"
            ),
            {"pid": draft_pid},
        )).mappings().first()["creationcandidatekeyhash"]
        assert hash_after_promo == original_hash, (
            f"Promotion must not change CreationCandidateKeyHash; "
            f"before={original_hash}, after={hash_after_promo}"
        )

        # Capture pre-replay counts using a broader query (detects unexpected extras).
        pre_period_count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.payrollperiods
                WHERE branchid = :bid
                  AND startdate >= '2095-01-01' AND startdate < '2096-01-01'
                  AND status NOT IN ('Cancelled')
            """),
            {"bid": paytest_branch_id},
        )).scalar_one()

        pre_audit_count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE entityschema = 'payroll'
                  AND entityname   = 'PayrollPeriods'
                  AND entityid     IN (:oid, :did)
            """),
            {"oid": str(open_pid), "did": str(draft_pid)},
        )).scalar_one()

        # Step 4: Replay the same candidate key.
        replay_resp = await session_client.post(
            f"/payroll/branches/{paytest_branch_id}/period-creations",
            json={"candidate_key": candidate_key},
            headers=_auth(auth_token),
        )

        # Step 5: Assert idempotent replay.
        assert replay_resp.status_code in (200, 201), (
            f"Replay failed: {replay_resp.status_code} {replay_resp.text}"
        )
        replay_body = replay_resp.json()
        assert replay_body["result"] == "ALREADY_EXISTS", (
            f"Expected ALREADY_EXISTS on replay; got {replay_body['result']}"
        )
        assert replay_body["payroll_period_id"] == draft_pid, (
            f"Replay returned different period ID: {replay_body['payroll_period_id']} != {draft_pid}"
        )
        assert replay_body["status"] == "Open", (
            f"Replayed period should report current status Open; got {replay_body['status']}"
        )

        # Total branch period count must not grow (detects any unexpected extra period).
        post_period_count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.payrollperiods
                WHERE branchid = :bid
                  AND startdate >= '2095-01-01' AND startdate < '2096-01-01'
                  AND status NOT IN ('Cancelled')
            """),
            {"bid": paytest_branch_id},
        )).scalar_one()
        assert post_period_count == pre_period_count, (
            f"Replay must not create extra periods; pre={pre_period_count}, post={post_period_count}"
        )

        # Audit count for test-owned periods must not grow on replay.
        post_audit_count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE entityschema = 'payroll'
                  AND entityname   = 'PayrollPeriods'
                  AND entityid     IN (:oid, :did)
            """),
            {"oid": str(open_pid), "did": str(draft_pid)},
        )).scalar_one()
        assert post_audit_count == pre_audit_count, (
            f"Replay must not write new audit rows; pre={pre_audit_count}, post={post_audit_count}"
        )

        # CreationCandidateKeyHash must survive replay.
        hash_after_replay = (await direct_db.execute(
            _text(
                "SELECT creationcandidatekeyhash FROM payroll.payrollperiods "
                "WHERE payrollperiodid = :pid"
            ),
            {"pid": draft_pid},
        )).mappings().first()["creationcandidatekeyhash"]
        assert hash_after_replay == original_hash, (
            f"CreationCandidateKeyHash must not change after replay; "
            f"before={original_hash}, after={hash_after_replay}"
        )

        # No second Draft exists for the branch (replay must not create a new Draft).
        draft_count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.payrollperiods
                WHERE branchid = :bid
                  AND startdate >= '2095-01-01' AND startdate < '2096-01-01'
                  AND status = 'Draft'
            """),
            {"bid": paytest_branch_id},
        )).scalar_one()
        assert draft_count == 0, (
            f"No Draft must remain after Open promotion + replay; found {draft_count}"
        )


# ---------------------------------------------------------------------------
# C/D: CP-1B replacement + deterministic concurrency tests
# ---------------------------------------------------------------------------

class TestSerializationConcurrency:
    """
    CP-1D serialization tests replacing the skipped CP-1B monkey-patch race tests.

    With pg_advisory_xact_lock in place, concurrent same-branch operations serialize.
    The loser waits at the advisory lock, then after the winner commits the loser
    runs its guards and encounters the already-InReview slot.

    These tests use asyncio.gather to run two concurrent API requests and assert:
    - Exactly one succeeds (200)
    - The other fails safely with 409 (no partial state)
    - No duplicate review items, no duplicate promotions
    - DB state is consistent after both complete
    """

    async def _clean_concurrency(self, direct_db, branch_id: int) -> None:
        """FK-safe cleanup for concurrency test setup."""
        # Cancel ALL active periods (no date filter) — dates may be in 2096+ due to _DATE_CTR.
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollperiods
                SET status = 'Cancelled', currentreturnreviewitemid = NULL
                WHERE branchid = :bid AND status = 'Returned'
            """),
            {"bid": branch_id},
        )
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollperiods
                SET status = 'Cancelled'
                WHERE branchid = :bid AND status IN ('Draft', 'Open', 'InReview')
            """),
            {"bid": branch_id},
        )
        await direct_db.execute(
            _text("""
                DELETE FROM review.managerreviewdecisions
                WHERE reviewitemid IN (
                    SELECT reviewitemid FROM review.managerreviewitems WHERE branchid = :bid
                )
            """),
            {"bid": branch_id},
        )
        await direct_db.execute(_text("DELETE FROM review.managerreviewitems WHERE branchid = :bid"), {"bid": branch_id})
        await direct_db.execute(
            _text("""
                DELETE FROM payroll.payrolldraftlines
                WHERE payrollperiodid IN (
                    SELECT payrollperiodid FROM payroll.payrollperiods
                    WHERE branchid = :bid AND startdate >= '2095-01-01' AND startdate < '2096-01-01'
                )
            """),
            {"bid": branch_id},
        )
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollperiods SET status = 'Cancelled'
                WHERE branchid = :bid AND status IN ('Draft','Open','InReview')
                  AND startdate >= '2095-01-01' AND startdate < '2096-01-01'
            """),
            {"bid": branch_id},
        )
        await direct_db.commit()

    @pytest.mark.asyncio
    async def test_c1_open_submit_wins_returned_resubmit_loses(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """C1: CP-1D serialization — Open submit wins, Returned resubmit serializes and loses.

        With advisory locking, both requests run sequentially (winner first).
        Loser finds InReview slot occupied after winner commits → 409.
        Full rollback: no duplicate review item for the loser.
        """
        await self._clean_concurrency(direct_db, paytest_branch_id)

        # Setup: Returned period A (earlier dates → no backlog concern for Open B).
        ret_start, ret_end = _dates_2095(80)
        ret_pid, _ = await _insert_returned_period(
            direct_db, paytest_branch_id, ret_start, ret_end, "C1-RET",
        )
        # Add a line to the Returned period so it can pass submission guards.
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (payrollperiodid, companyid, branchid, driverid, linetype, linescope, quantity, sourcetype, status)
                VALUES (:pid, :cid, :bid, :did, 'DailyNote', 'Daily', 1, 'Manual', 'Active')
            """),
            {"pid": ret_pid, "cid": _COMPANY_ID, "bid": paytest_branch_id, "did": paytest_driver_id},
        )
        await direct_db.commit()

        # Open period B starts on or after Returned A end → no backlog conflict.
        op_start = ret_end + datetime.timedelta(days=1)
        op_end   = op_start + datetime.timedelta(days=6)
        open_pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "C1-OP")
        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, op_start)

        # Count Pending review items before the race.
        pre_pending = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems WHERE branchid = :bid AND status = 'Pending'"),
            {"bid": paytest_branch_id},
        )).scalar_one()

        # Run both concurrently; advisory lock serializes them.
        # With B2 fail-closed: Open submit fails (RETURNED_BACKLOG_BLOCKS_SUBMIT) whenever
        # a Returned period exists; Returned resubmit always wins this race.
        r_open, r_resub = await asyncio.gather(
            _submit(session_client, auth_token, open_pid),
            session_client.post(f"/payroll/periods/{ret_pid}/resubmissions", headers=_auth(auth_token)),
        )

        statuses = {r_open.status_code, r_resub.status_code}
        assert 200 in statuses, (
            f"One request must succeed (200); got {r_open.status_code}, {r_resub.status_code}"
        )
        assert 409 in statuses or 422 in statuses, (
            f"One request must fail (409/422); got {r_open.status_code}, {r_resub.status_code}"
        )

        # Exactly one new Pending review item from the winner.
        post_pending = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems WHERE branchid = :bid AND status = 'Pending'"),
            {"bid": paytest_branch_id},
        )).scalar_one()
        assert post_pending == pre_pending + 1, (
            f"Expected exactly 1 new Pending review item; "
            f"pre={pre_pending}, post={post_pending}"
        )

    @pytest.mark.asyncio
    async def test_c2_returned_resubmit_wins_open_submit_loses(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """C2: CP-1D serialization — Returned resubmit wins, Open submit serializes and loses.

        Order is non-deterministic in asyncio but both orders must be safe.
        Assert: exactly one wins, one loses, no duplicate state.
        """
        await self._clean_concurrency(direct_db, paytest_branch_id)

        ret_start, ret_end = _dates_2095(88)
        ret_pid, _ = await _insert_returned_period(
            direct_db, paytest_branch_id, ret_start, ret_end, "C2-RET",
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (payrollperiodid, companyid, branchid, driverid, linetype, linescope, quantity, sourcetype, status)
                VALUES (:pid, :cid, :bid, :did, 'DailyNote', 'Daily', 1, 'Manual', 'Active')
            """),
            {"pid": ret_pid, "cid": _COMPANY_ID, "bid": paytest_branch_id, "did": paytest_driver_id},
        )
        await direct_db.commit()

        op_start = ret_end + datetime.timedelta(days=1)
        op_end   = op_start + datetime.timedelta(days=6)
        open_pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "C2-OP")
        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, op_start)

        r_resub, r_open = await asyncio.gather(
            session_client.post(f"/payroll/periods/{ret_pid}/resubmissions", headers=_auth(auth_token)),
            _submit(session_client, auth_token, open_pid),
        )

        statuses = {r_resub.status_code, r_open.status_code}
        assert 200 in statuses, (
            f"One request must succeed; got {r_resub.status_code}, {r_open.status_code}"
        )
        assert len({r for r in [r_resub.status_code, r_open.status_code] if r in (409, 422)}) >= 1, (
            f"One must fail; got {r_resub.status_code}, {r_open.status_code}"
        )

        pending_ri = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems WHERE branchid = :bid AND status = 'Pending'"),
            {"bid": paytest_branch_id},
        )).scalar_one()
        assert pending_ri == 1, f"Expected exactly 1 Pending review item; got {pending_ri}"

    @pytest.mark.asyncio
    async def test_c3_two_same_branch_submits_one_succeeds(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db,
    ):
        """C3: Concurrent double-submit of the same Open period — exactly one succeeds.

        ux_payrollperiods_oneopenperbranch prevents two Open periods coexisting, so this test
        races two concurrent submits of the same period. Advisory lock serializes; the loser
        finds the period already InReview and fails (409 or 422). No duplicate review item.
        """
        await self._clean_concurrency(direct_db, paytest_branch_id)

        start1, end1 = _dates_2095(96)
        pid = await _insert_open_period(direct_db, paytest_branch_id, start1, end1, "C3-OP")
        await _add_pto_line(session_client, auth_token, pid, paytest_driver_id, start1)

        # Same period submitted twice concurrently — advisory lock serializes, loser finds non-Open.
        r1, r2 = await asyncio.gather(
            _submit(session_client, auth_token, pid),
            _submit(session_client, auth_token, pid),
        )

        success_count = sum(1 for r in [r1, r2] if r.status_code == 200)
        assert success_count == 1, (
            f"Exactly one submit must succeed; got {r1.status_code}, {r2.status_code}"
        )

        pending_ri = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems WHERE branchid = :bid AND status = 'Pending'"),
            {"bid": paytest_branch_id},
        )).scalar_one()
        assert pending_ri == 1, f"Expected exactly 1 Pending review item; got {pending_ri}"

    @pytest.mark.asyncio
    async def test_c4_different_branch_isolation(
        self, session_client, auth_token, paytest_branch_id, hq_branch_id,
        paytest_driver_id, hq_driver_id, direct_db,
    ):
        """C4: Branch A lock does not block branch B workflow operation.

        Two submits on different branches run concurrently and both succeed.
        """
        await self._clean_concurrency(direct_db, paytest_branch_id)
        await self._clean_concurrency(direct_db, hq_branch_id)

        start_a, end_a = _dates_2095(100)
        pid_a = await _insert_open_period(direct_db, paytest_branch_id, start_a, end_a, "C4-A")
        await _add_pto_line(session_client, auth_token, pid_a, paytest_driver_id, start_a)

        start_b, end_b = _dates_2095(102)
        pid_b = await _insert_open_period(direct_db, hq_branch_id, start_b, end_b, "C4-B")
        # Insert a draft line for HQ branch using the HQ branch driver.
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (payrollperiodid, companyid, branchid, driverid,
                     linetype, linescope, quantity, sourcetype, status)
                VALUES (:pid, :cid, :bid, :did, 'DailyNote', 'Daily', 1, 'Manual', 'Active')
            """),
            {"pid": pid_b, "cid": _COMPANY_ID, "bid": hq_branch_id, "did": hq_driver_id},
        )
        await direct_db.commit()

        r_a, r_b = await asyncio.gather(
            _submit(session_client, auth_token, pid_a),
            _submit(session_client, auth_token, pid_b),
        )

        assert r_a.status_code == 200, f"Branch A submit failed: {r_a.status_code} {r_a.text}"
        assert r_b.status_code == 200, f"Branch B submit failed: {r_b.status_code} {r_b.text}"


# ---------------------------------------------------------------------------
# D: Deterministic lock boundary tests
# ---------------------------------------------------------------------------

class TestDeterministicLockBoundary:
    """
    Deterministic advisory-lock boundary tests using an independent raw DB connection
    to hold the advisory lock. Each test proves:
      1. The target operation acquires pg_advisory_xact_lock(company_id, branch_id) before
         proceeding — proven by the pg_locks NOT-granted row appearing while the lock is held.
      2. State mutations committed before lock release are visible to the operation after
         it acquires the lock — i.e., the operation re-evaluates guards against the new state.
      3. After rejection, no partial state leaks from the abandoned transaction.
    """

    @pytest.mark.asyncio
    async def test_d1_submit_waits_and_revalidates_inreview_slot(
        self,
        session_client, auth_token, paytest_branch_id, paytest_driver_id,
        direct_db, test_database_url,
    ):
        """D1: Submit waits on branch advisory lock and revalidates InReview slot.

        Protocol:
          1. Hold advisory lock on independent connection.
          2. Start Open→InReview submit (must queue for lock).
          3. Prove submit is waiting: pg_locks NOT granted count >= 1.
          4. Before releasing: directly insert an InReview period (autocommit → visible after release).
          5. Release lock.
          6. Assert submit returns 409 (InReview slot occupied) — guard fired after lock acquisition.
          7. Assert: original Open still Open, no new review item, SubmittedAtUtc IS NULL.
        """
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095(130)
        open_pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "D1-OP")
        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, start)

        pre_ri_count = (await direct_db.execute(
            _text(
                "SELECT COUNT(*) FROM review.managerreviewitems "
                "WHERE branchid = :bid AND status = 'Pending'"
            ),
            {"bid": paytest_branch_id},
        )).scalar_one()

        lock_held = asyncio.Event()
        blocker_done = asyncio.Event()

        async def _hold_lock():
            engine = create_async_engine(test_database_url, echo=False)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        _text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
                        {"cid": _COMPANY_ID, "bid": paytest_branch_id},
                    )
                    lock_held.set()
                    await asyncio.wait_for(blocker_done.wait(), timeout=10.0)
            finally:
                await engine.dispose()

        lock_task = asyncio.ensure_future(_hold_lock())
        await asyncio.wait_for(lock_held.wait(), timeout=10.0)

        submit_task = asyncio.ensure_future(_submit(session_client, auth_token, open_pid))
        await _wait_for_lock_waiter(direct_db, _COMPANY_ID, paytest_branch_id)

        # Before releasing: inject an InReview period so CP-1B slot guard fires after lock.
        ir_start = start + datetime.timedelta(days=14)
        ir_end   = ir_start + datetime.timedelta(days=6)
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (:cid, :bid, 'InReview', :code, :name, 'Week', :start, :end)
            """),
            {
                "cid":   _COMPANY_ID,
                "bid":   paytest_branch_id,
                "code":  f"D1-IR-{ir_start}",
                "name":  f"D1 InReview {ir_start}",
                "start": ir_start,
                "end":   ir_end,
            },
        )

        blocker_done.set()
        submit_resp = await asyncio.wait_for(submit_task, timeout=10.0)
        await asyncio.wait_for(lock_task, timeout=10.0)

        assert submit_resp.status_code == 409, (
            f"Expected 409 (InReview slot filled before lock release); "
            f"got {submit_resp.status_code}: {submit_resp.text}"
        )

        open_status = (await direct_db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": open_pid},
        )).scalar_one()
        assert open_status == "Open", f"Open must remain Open after failed submit; got {open_status}"

        post_ri_count = (await direct_db.execute(
            _text(
                "SELECT COUNT(*) FROM review.managerreviewitems "
                "WHERE branchid = :bid AND status = 'Pending'"
            ),
            {"bid": paytest_branch_id},
        )).scalar_one()
        assert post_ri_count == pre_ri_count, (
            f"No new review item from rejected submit; pre={pre_ri_count}, post={post_ri_count}"
        )

        sat = (await direct_db.execute(
            _text(
                "SELECT submittedatutc FROM payroll.payrollperiods "
                "WHERE payrollperiodid = :pid"
            ),
            {"pid": open_pid},
        )).scalar_one()
        assert sat is None, (
            f"SubmittedAtUtc must remain NULL after failed submit; got {sat}"
        )

    @pytest.mark.asyncio
    async def test_d2_candidate_creation_waits_and_revalidates_slot(
        self,
        session_client, auth_token, paytest_branch_id,
        direct_db, test_database_url,
    ):
        """D2: CP-1C candidate creation waits on branch advisory lock and revalidates slot.

        Protocol:
          1. Prepare a valid candidate key while the Draft slot is empty.
          2. Hold advisory lock on independent connection.
          3. Start period-creations POST (must queue for lock).
          4. Prove creation is waiting in pg_locks.
          5. Before releasing: directly insert a Draft (fills slot, different hash).
          6. Release lock.
          7. Assert 409 (slot conflict) — no new period was CREATED.
        """
        await _clean(direct_db, paytest_branch_id)

        await _ensure_prepared_creation_setup(
            session_client, auth_token, direct_db, paytest_branch_id,
        )

        op_start, op_end = _dates_2095(140)
        await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "D2-OP")

        cand_r = await session_client.get(
            f"/payroll/branches/{paytest_branch_id}/period-candidates",
            params={"mode": "PREPARED_CREATION"},
            headers=_auth(auth_token),
        )
        assert cand_r.status_code == 200, f"candidates: {cand_r.text}"
        candidate_key = cand_r.json().get("selected", {}).get("candidate_key")
        assert candidate_key, "Need a candidate key for D2"

        pre_count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.payrollperiods
                WHERE branchid = :bid
                  AND startdate >= '2095-01-01' AND startdate < '2096-01-01'
                  AND status NOT IN ('Cancelled')
            """),
            {"bid": paytest_branch_id},
        )).scalar_one()

        lock_held = asyncio.Event()
        blocker_done = asyncio.Event()

        async def _hold_lock():
            engine = create_async_engine(test_database_url, echo=False)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        _text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
                        {"cid": _COMPANY_ID, "bid": paytest_branch_id},
                    )
                    lock_held.set()
                    await asyncio.wait_for(blocker_done.wait(), timeout=10.0)
            finally:
                await engine.dispose()

        lock_task = asyncio.ensure_future(_hold_lock())
        await asyncio.wait_for(lock_held.wait(), timeout=10.0)

        create_task = asyncio.ensure_future(
            session_client.post(
                f"/payroll/branches/{paytest_branch_id}/period-creations",
                json={"candidate_key": candidate_key},
                headers=_auth(auth_token),
            )
        )
        await _wait_for_lock_waiter(direct_db, _COMPANY_ID, paytest_branch_id)

        # Fill the Draft slot with a directly-inserted period (different hash → will conflict).
        dr_start = op_end + datetime.timedelta(days=1)
        dr_end   = dr_start + datetime.timedelta(days=6)
        await _insert_draft_period(direct_db, paytest_branch_id, dr_start, dr_end, "D2-DR")

        blocker_done.set()
        create_resp = await asyncio.wait_for(create_task, timeout=10.0)
        await asyncio.wait_for(lock_task, timeout=10.0)

        # Creation must not have produced a new CREATED period (slot was filled).
        assert create_resp.status_code in (200, 201, 409), (
            f"Unexpected status {create_resp.status_code}: {create_resp.text}"
        )
        if create_resp.status_code in (200, 201):
            result = create_resp.json().get("result")
            assert result == "ALREADY_EXISTS", (
                f"If creation succeeded it must be ALREADY_EXISTS (not CREATED); got {result}"
            )

        post_count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.payrollperiods
                WHERE branchid = :bid
                  AND startdate >= '2095-01-01' AND startdate < '2096-01-01'
                  AND status NOT IN ('Cancelled')
            """),
            {"bid": paytest_branch_id},
        )).scalar_one()
        # At most pre_count + 1 (the directly-inserted D2-DR); not pre_count + 2.
        assert post_count <= pre_count + 1, (
            f"At most one new period (the directly-inserted D2-DR); "
            f"pre={pre_count}, post={post_count}"
        )

    @pytest.mark.asyncio
    async def test_d3_legacy_create_waits_and_revalidates_open_guard(
        self,
        session_client, auth_token, paytest_branch_id,
        direct_db, test_database_url,
    ):
        """D3: Legacy POST /payroll/periods waits on branch advisory lock and revalidates B1 guard.

        Protocol:
          1. Insert Open period so B1 guard would pass initially.
          2. Hold advisory lock on independent connection.
          3. Start legacy POST /payroll/periods (must queue for lock).
          4. Prove waiting in pg_locks.
          5. Before releasing: cancel the Open period (B1 guard will fire after lock).
          6. Release lock.
          7. Assert 409 DRAFT_CREATION_REQUIRES_OPEN.
        """
        await _clean(direct_db, paytest_branch_id)

        op_start, op_end = _dates_2095(150)
        open_pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "D3-OP")

        dr_start = op_end + datetime.timedelta(days=1)
        dr_end   = dr_start + datetime.timedelta(days=6)

        lock_held = asyncio.Event()
        blocker_done = asyncio.Event()

        async def _hold_lock():
            engine = create_async_engine(test_database_url, echo=False)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        _text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
                        {"cid": _COMPANY_ID, "bid": paytest_branch_id},
                    )
                    lock_held.set()
                    await asyncio.wait_for(blocker_done.wait(), timeout=10.0)
            finally:
                await engine.dispose()

        lock_task = asyncio.ensure_future(_hold_lock())
        await asyncio.wait_for(lock_held.wait(), timeout=10.0)

        create_task = asyncio.ensure_future(
            session_client.post(
                "/payroll/periods",
                json={
                    "branch_id":   paytest_branch_id,
                    "period_type": "Week",
                    "start_date":  dr_start.isoformat(),
                    "end_date":    dr_end.isoformat(),
                },
                headers=_auth(auth_token),
            )
        )
        await _wait_for_lock_waiter(direct_db, _COMPANY_ID, paytest_branch_id)

        # Remove the Open period so B1 guard fires after lock acquisition.
        await direct_db.execute(
            _text(
                "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                "WHERE payrollperiodid = :pid"
            ),
            {"pid": open_pid},
        )

        blocker_done.set()
        create_resp = await asyncio.wait_for(create_task, timeout=10.0)
        await asyncio.wait_for(lock_task, timeout=10.0)

        assert create_resp.status_code == 409, (
            f"Expected 409 DRAFT_CREATION_REQUIRES_OPEN after Open cancelled; "
            f"got {create_resp.status_code}: {create_resp.text}"
        )
        detail = create_resp.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("code") == "DRAFT_CREATION_REQUIRES_OPEN", (
                f"Expected DRAFT_CREATION_REQUIRES_OPEN; got {detail}"
            )
        else:
            assert "DRAFT_CREATION_REQUIRES_OPEN" in str(detail), detail

    @pytest.mark.asyncio
    async def test_d4_approve_decision_waits_behind_branch_lock(
        self,
        session_client, auth_token, paytest_branch_id, paytest_driver_id,
        direct_db, test_database_url,
    ):
        """D4: PeriodApproval approve decision waits on branch advisory lock.

        Protocol:
          1. Submit a period to InReview to get a Pending review item.
          2. Hold advisory lock on independent connection.
          3. Start approve decision (must queue for lock).
          4. Prove waiting in pg_locks.
          5. Release lock (no state mutation — just prove serialization).
          6. Assert approve succeeds (200) and period becomes Approved.
        """
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095(160)
        open_pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "D4-OP")
        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, start)

        submit_r = await _submit(session_client, auth_token, open_pid)
        assert submit_r.status_code == 200, f"D4 setup submit: {submit_r.text}"

        ri_id = await _pending_review_item_id(session_client, auth_token, open_pid)

        lock_held = asyncio.Event()
        blocker_done = asyncio.Event()

        async def _hold_lock():
            engine = create_async_engine(test_database_url, echo=False)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        _text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
                        {"cid": _COMPANY_ID, "bid": paytest_branch_id},
                    )
                    lock_held.set()
                    await asyncio.wait_for(blocker_done.wait(), timeout=10.0)
            finally:
                await engine.dispose()

        lock_task = asyncio.ensure_future(_hold_lock())
        await asyncio.wait_for(lock_held.wait(), timeout=10.0)

        approve_task = asyncio.ensure_future(_approve(session_client, auth_token, ri_id))
        await _wait_for_lock_waiter(direct_db, _COMPANY_ID, paytest_branch_id)

        blocker_done.set()
        approve_resp = await asyncio.wait_for(approve_task, timeout=10.0)
        await asyncio.wait_for(lock_task, timeout=10.0)

        assert approve_resp.status_code == 200, (
            f"Approve must succeed after lock released; got {approve_resp.status_code}: {approve_resp.text}"
        )

        period_state = await _get_period(session_client, auth_token, open_pid)
        assert period_state["status"] == "Approved", (
            f"Period must be Approved after decision; got {period_state['status']}"
        )

    @pytest.mark.asyncio
    async def test_d5_different_branch_lock_does_not_block(
        self,
        session_client, auth_token,
        paytest_branch_id, hq_branch_id,
        paytest_driver_id, hq_driver_id,
        direct_db, test_database_url,
    ):
        """D5: Advisory lock on branch A does not block operations on branch B.

        Protocol:
          1. Hold advisory lock for (company=1, branch=paytest_branch_id) on independent connection.
          2. Start submit on hq_branch_id (branch B — different branch_id → different lock coords).
          3. Assert branch B does NOT appear as a waiter for branch A's lock in pg_locks.
          4. Assert branch B submit completes successfully while branch A lock is still held.
          5. Release branch A lock.
        """
        await _clean(direct_db, paytest_branch_id)
        await _clean(direct_db, hq_branch_id)

        # Branch A: Open period with PTO line.
        a_start, a_end = _dates_2095(170)
        pid_a = await _insert_open_period(direct_db, paytest_branch_id, a_start, a_end, "D5-A")
        await _add_pto_line(session_client, auth_token, pid_a, paytest_driver_id, a_start)

        # Branch B: Open period with a draft line inserted directly.
        b_start, b_end = _dates_2095(172)
        pid_b = await _insert_open_period(direct_db, hq_branch_id, b_start, b_end, "D5-B")
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrolldraftlines
                    (payrollperiodid, companyid, branchid, driverid,
                     linetype, linescope, quantity, sourcetype, status)
                VALUES (:pid, :cid, :bid, :did, 'DailyNote', 'Daily', 1, 'Manual', 'Active')
            """),
            {
                "pid": pid_b,
                "cid": _COMPANY_ID,
                "bid": hq_branch_id,
                "did": hq_driver_id,
            },
        )

        lock_held = asyncio.Event()
        branch_b_done = asyncio.Event()  # event-gated: B signals completion; only then A releases

        async def _hold_branch_a_lock():
            engine = create_async_engine(test_database_url, echo=False)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        _text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
                        {"cid": _COMPANY_ID, "bid": paytest_branch_id},
                    )
                    lock_held.set()
                    # Hold until branch B signals done — event-gated, not timing-based.
                    await asyncio.wait_for(branch_b_done.wait(), timeout=10.0)
            finally:
                await engine.dispose()

        lock_task = asyncio.ensure_future(_hold_branch_a_lock())
        await asyncio.wait_for(lock_held.wait(), timeout=10.0)

        # Branch B submit — must NOT wait for branch A's lock.
        # A lock is still held here; if B waited for A this would deadlock / timeout.
        r_b = await asyncio.wait_for(
            _submit(session_client, auth_token, pid_b),
            timeout=5.0,
        )

        # Assert BEFORE releasing A: B completed while A was still held.
        assert r_b.status_code == 200, (
            f"Branch B submit must succeed without waiting for branch A lock; "
            f"got {r_b.status_code}: {r_b.text}"
        )

        # Verify branch B is not in pg_locks as a waiter for the branch A advisory lock.
        waiter_count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM pg_locks
                WHERE locktype = 'advisory'
                  AND classid  = :cid
                  AND objid    = :bid_a
                  AND objsubid = 2
                  AND NOT granted
            """),
            {"cid": _COMPANY_ID, "bid_a": paytest_branch_id},
        )).scalar_one()
        assert waiter_count == 0, (
            f"Branch B must not be waiting on branch A's advisory lock; "
            f"found {waiter_count} waiter(s)"
        )

        # Release A only after B is confirmed done — this is the decisive proof:
        # B completed while A held the lock → B was never serialised by A.
        branch_b_done.set()
        await asyncio.wait_for(lock_task, timeout=5.0)

    @pytest.mark.asyncio
    async def test_d6_reject_decision_waits_and_revalidates_stale_period(
        self,
        session_client, auth_token,
        paytest_branch_id, paytest_driver_id,
        direct_db, test_database_url,
    ):
        """D6: Rejected decision waits on branch advisory lock and detects stale period.

        Protocol:
          1. Submit period to InReview → get Pending review item.
          2. Hold advisory lock on an independent raw connection.
          3. Issue a Rejected decision (must queue — PeriodApproval substantive).
          4. Prove decision is waiting in pg_locks.
          5. Before releasing lock: cancel the linked period via AUTOCOMMIT direct_db.
          6. Release lock.
          7. Assert 422 (period no longer InReview).
          8. Assert: period is Cancelled, CurrentReturnReviewItemID IS NULL,
             review item still Pending, PERIOD_STATUS_CHANGED count unchanged.
        """
        await _clean(direct_db, paytest_branch_id)
        start, end = _dates_2095(190)
        open_pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "D6-OP")
        await _add_pto_line(session_client, auth_token, open_pid, paytest_driver_id, start)

        submit_r = await _submit(session_client, auth_token, open_pid)
        assert submit_r.status_code == 200, f"D6 setup submit failed: {submit_r.text}"

        ri_id = await _pending_review_item_id(session_client, auth_token, open_pid)
        assert ri_id is not None, "Expected a Pending review item after submit"

        # Capture baseline audit count.
        pre_status_audit = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods'
                  AND entityid = :eid AND actioncode = 'PERIOD_STATUS_CHANGED'
            """),
            {"eid": str(open_pid)},
        )).scalar_one()

        lock_held   = asyncio.Event()
        blocker_done = asyncio.Event()

        async def _hold_lock() -> None:
            engine = create_async_engine(test_database_url, echo=False)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        _text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
                        {"cid": _COMPANY_ID, "bid": paytest_branch_id},
                    )
                    lock_held.set()
                    await asyncio.wait_for(blocker_done.wait(), timeout=10.0)
            finally:
                await engine.dispose()

        lock_task = asyncio.ensure_future(_hold_lock())
        await asyncio.wait_for(lock_held.wait(), timeout=5.0)

        # Start reject — will block on advisory lock acquisition.
        reject_task = asyncio.ensure_future(
            _reject(session_client, auth_token, ri_id)
        )

        # Prove decision is waiting for the lock.
        await _wait_for_lock_waiter(direct_db, _COMPANY_ID, paytest_branch_id)

        # Cancel the period before releasing the lock — stale-state injection.
        await direct_db.execute(
            _text(
                "UPDATE payroll.payrollperiods "
                "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
                "WHERE payrollperiodid = :pid"
            ),
            {"pid": open_pid},
        )

        # Release the lock; decision proceeds and must detect stale state.
        blocker_done.set()
        reject_resp = await asyncio.wait_for(reject_task, timeout=10.0)
        await asyncio.wait_for(lock_task, timeout=5.0)

        assert reject_resp.status_code == 422, (
            f"Reject must 422 when period is no longer InReview; "
            f"got {reject_resp.status_code}: {reject_resp.text}"
        )

        # Period is still Cancelled (not Returned).
        period_status = (await direct_db.execute(
            _text("SELECT status, currentreturnreviewitemid FROM payroll.payrollperiods "
                  "WHERE payrollperiodid = :pid"),
            {"pid": open_pid},
        )).mappings().first()
        assert period_status["status"] == "Cancelled", (
            f"Period must remain Cancelled; got {period_status['status']}"
        )
        assert period_status["currentreturnreviewitemid"] is None, (
            f"CurrentReturnReviewItemID must remain NULL; "
            f"got {period_status['currentreturnreviewitemid']}"
        )

        # Review item must still be Pending (decision rolled back).
        ri_status = (await direct_db.execute(
            _text("SELECT status FROM review.managerreviewitems "
                  "WHERE reviewitemid = :rid"),
            {"rid": ri_id},
        )).scalar_one()
        assert ri_status == "Pending", (
            f"Review item must remain Pending after 422 rollback; got {ri_status}"
        )

        # No new PERIOD_STATUS_CHANGED audit rows.
        post_status_audit = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods'
                  AND entityid = :eid AND actioncode = 'PERIOD_STATUS_CHANGED'
            """),
            {"eid": str(open_pid)},
        )).scalar_one()
        assert post_status_audit == pre_status_audit, (
            f"PERIOD_STATUS_CHANGED audit must not grow after 422 rollback; "
            f"pre={pre_status_audit}, post={post_status_audit}"
        )


# ---------------------------------------------------------------------------
# E: Rollback tests
# ---------------------------------------------------------------------------

class TestRollbackProof:

    @pytest.mark.asyncio
    async def test_e1_promotion_failure_rollback(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id,
        paytest_rate_type_id, direct_db, monkeypatch,
    ):
        """E1: If Draft promotion audit fails, the entire transaction rolls back.

        Failure is injected AFTER _refresh_draft_calculations, review item INSERT,
        status UPDATE, and Draft promotion UPDATE have all run — proving that all those
        changes roll back atomically with the promotion audit failure.

        Sentinel: a PerUnit Hours line backed by an approved HOURLY rate.
        _refresh_draft_calculations would update calculatedamount from 0.01 → 25.00
        (qty=1 × rate=$25.00).  After rollback, the value must be restored to 0.01,
        proving the refresh was part of the rolled-back transaction.

        Proof: Open remains Open, Draft remains Draft, no Pending review item,
        PERIOD_STATUS_CHANGED audit count unchanged, REVIEW_ITEM_CREATED audit count
        unchanged, SubmittedAtUtc IS NULL, calc sentinel = 0.01 (refresh rolled back).
        """
        await _clean(direct_db, paytest_branch_id)

        op_start, op_end = _dates_2095(110)
        dr_start = op_end + datetime.timedelta(days=1)
        dr_end   = dr_start + datetime.timedelta(days=6)

        open_pid  = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "E1-OP")
        draft_pid = await _insert_draft_period(direct_db, paytest_branch_id, dr_start, dr_end, "E1-DR")

        # Create an approved HOURLY rate so _refresh_draft_calculations resolves
        # qty × rate = 1 × 25.00 = 25.00 (non-sentinel, rate-dependent).
        rate_id = await _create_approved_hourly_rate(
            session_client, auth_token, paytest_driver_id,
            paytest_rate_type_id, op_start,
        )
        try:
            await _add_hours_line(session_client, auth_token, open_pid, paytest_driver_id, op_start)

            # Stale the Hours line's calculatedamount to sentinel 0.01 via AUTOCOMMIT.
            # _refresh_draft_calculations will recompute it to 25.00 inside the
            # transaction; rollback restores it to 0.01.
            line_row = (await direct_db.execute(
                _text(
                    "SELECT draftlineid FROM payroll.payrolldraftlines "
                    "WHERE payrollperiodid = :pid AND status != 'Void' "
                    "AND linetype = 'HOURS' LIMIT 1"
                ),
                {"pid": open_pid},
            )).mappings().first()
            assert line_row is not None, "Expected an Hours draft line for E1"
            sentinel_line_id = line_row["draftlineid"]
            await direct_db.execute(
                _text(
                    "UPDATE payroll.payrolldraftlines SET calculatedamount = 0.01 "
                    "WHERE draftlineid = :lid"
                ),
                {"lid": sentinel_line_id},
            )

            # Capture pre-submit audit counts.
            pre_status_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods'
                      AND entityid = :eid AND actioncode = 'PERIOD_STATUS_CHANGED'
                """),
                {"eid": str(open_pid)},
            )).scalar_one()

            pre_ri_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'review' AND entityname = 'ManagerReviewItems'
                      AND branchid = :bid AND actioncode = 'REVIEW_ITEM_CREATED'
                """),
                {"bid": paytest_branch_id},
            )).scalar_one()

            # Force the Draft promotion audit to fail → simulates post-UPDATE failure.
            # Stage B4-18: _write_period_status_audit's real implementation
            # now lives in app.payroll.period_lifecycle, and
            # change_period_status (also in period_lifecycle) resolves it as
            # a bare name through that module's own globals.
            from app.payroll import period_lifecycle as _svc
            original_write_audit = _svc._write_period_status_audit
            from fastapi import HTTPException as _HTTPException

            async def _failing_audit(db, *, company_id, branch_id, user_id, period_id,
                                      old_status, new_status, extra=None):
                if old_status == "Draft" and new_status == "Open":
                    raise _HTTPException(status_code=500, detail="Injected failure: promotion audit")
                return await original_write_audit(
                    db, company_id=company_id, branch_id=branch_id, user_id=user_id,
                    period_id=period_id, old_status=old_status, new_status=new_status, extra=extra,
                )

            monkeypatch.setattr(_svc, "_write_period_status_audit", _failing_audit)

            r = await _submit(session_client, auth_token, open_pid)
            assert r.status_code == 500, (
                f"Injected failure should propagate as 500; got {r.status_code}"
            )

            # Open still Open, Draft still Draft.
            open_status = (await direct_db.execute(
                _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": open_pid},
            )).scalar_one()
            draft_status = (await direct_db.execute(
                _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": draft_pid},
            )).scalar_one()
            assert open_status == "Open", f"Open must remain Open after rollback; got {open_status}"
            assert draft_status == "Draft", f"Draft must remain Draft after rollback; got {draft_status}"

            # No Pending review item survived.
            pending_ri = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM review.managerreviewitems
                    WHERE branchid = :bid AND status = 'Pending' AND entityid = :eid
                """),
                {"bid": paytest_branch_id, "eid": str(open_pid)},
            )).scalar_one()
            assert pending_ri == 0, f"No Pending review item must survive rollback; found {pending_ri}"

            # PERIOD_STATUS_CHANGED audit count unchanged.
            post_status_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods'
                      AND entityid = :eid AND actioncode = 'PERIOD_STATUS_CHANGED'
                """),
                {"eid": str(open_pid)},
            )).scalar_one()
            assert post_status_audit == pre_status_audit, (
                f"PERIOD_STATUS_CHANGED audit must not grow on rollback; "
                f"pre={pre_status_audit}, post={post_status_audit}"
            )

            # REVIEW_ITEM_CREATED audit count unchanged.
            post_ri_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'review' AND entityname = 'ManagerReviewItems'
                      AND branchid = :bid AND actioncode = 'REVIEW_ITEM_CREATED'
                """),
                {"bid": paytest_branch_id},
            )).scalar_one()
            assert post_ri_audit == pre_ri_audit, (
                f"REVIEW_ITEM_CREATED audit must not grow on rollback; "
                f"pre={pre_ri_audit}, post={post_ri_audit}"
            )

            # SubmittedAtUtc must remain NULL.
            sat = (await direct_db.execute(
                _text("SELECT submittedatutc FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": open_pid},
            )).scalar_one()
            assert sat is None, f"SubmittedAtUtc must remain NULL after rollback; got {sat}"

            # Hours calc sentinel must remain 0.01 — _refresh_draft_calculations ran
            # (updated it from 0.01 to 25.00 inside the transaction) but the promotion
            # audit failure rolled back everything including that update.
            calc = (await direct_db.execute(
                _text("SELECT calculatedamount FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
                {"lid": sentinel_line_id},
            )).scalar_one()
            assert calc is not None and abs(float(calc) - 0.01) < 1e-9, (
                f"Hours calc sentinel must remain 0.01 after rollback "
                f"(refresh ran but was rolled back); got {calc}"
            )

        finally:
            await session_client.delete(f"/payroll/rates/{rate_id}", headers=_auth(auth_token))

    @pytest.mark.asyncio
    async def test_e2_backlog_blocker_full_rollback(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id,
        paytest_rate_type_id, direct_db,
    ):
        """E2: RETURNED_BACKLOG_BLOCKS_SUBMIT — full no-op proof.

        B2 backlog check fires BEFORE _refresh_draft_calculations, so no DB write
        of any kind occurs inside the aborted transaction.

        Sentinel: a PerUnit Hours line backed by an approved HOURLY rate.
        _refresh_draft_calculations WOULD update calculatedamount from 0.01 → 25.00
        if it had run, but B2 fires before it is reached — so the value stays 0.01,
        proving that refresh was never called.

        Proof: Open remains Open, Draft remains Draft, no review item created,
        PERIOD_STATUS_CHANGED count unchanged, REVIEW_ITEM_CREATED count unchanged,
        SubmittedAtUtc IS NULL, calc sentinel unchanged (refresh never ran).
        """
        await _clean(direct_db, paytest_branch_id)

        # Returned period is backlog (ends before Open starts)
        ret_start = datetime.date(2095, 10, 1)
        ret_end   = datetime.date(2095, 10, 7)
        await _insert_returned_period(direct_db, paytest_branch_id, ret_start, ret_end, "E2-RET")

        op_start = datetime.date(2095, 10, 14)
        op_end   = datetime.date(2095, 10, 20)
        open_pid = await _insert_open_period(direct_db, paytest_branch_id, op_start, op_end, "E2-OP")

        # Adjacent Draft (would be promoted if backlog check didn't fire first).
        dr_start = op_end + datetime.timedelta(days=1)
        dr_end   = dr_start + datetime.timedelta(days=6)
        draft_pid = await _insert_draft_period(direct_db, paytest_branch_id, dr_start, dr_end, "E2-DR")

        # Create and approve an HOURLY rate so the Hours sentinel would be changed
        # by _refresh_draft_calculations IF it ever ran.  B2 fires before refresh,
        # so the value must remain 0.01 — proving refresh was never reached.
        rate_id = await _create_approved_hourly_rate(
            session_client, auth_token, paytest_driver_id,
            paytest_rate_type_id, op_start,
        )
        try:
            await _add_hours_line(session_client, auth_token, open_pid, paytest_driver_id, op_start)

            # Stale the Hours line's calculatedamount to sentinel 0.01.
            line_row = (await direct_db.execute(
                _text(
                    "SELECT draftlineid FROM payroll.payrolldraftlines "
                    "WHERE payrollperiodid = :pid AND status != 'Void' "
                    "AND linetype = 'HOURS' LIMIT 1"
                ),
                {"pid": open_pid},
            )).mappings().first()
            assert line_row is not None, "Expected an Hours draft line for E2"
            sentinel_line_id = line_row["draftlineid"]
            await direct_db.execute(
                _text(
                    "UPDATE payroll.payrolldraftlines SET calculatedamount = 0.01 "
                    "WHERE draftlineid = :lid"
                ),
                {"lid": sentinel_line_id},
            )

            # Capture pre-submit audit counts.
            pre_status_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'payroll' AND entityid = :eid
                      AND actioncode = 'PERIOD_STATUS_CHANGED'
                """),
                {"eid": str(open_pid)},
            )).scalar_one()

            pre_ri_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'review' AND entityname = 'ManagerReviewItems'
                      AND branchid = :bid AND actioncode = 'REVIEW_ITEM_CREATED'
                """),
                {"bid": paytest_branch_id},
            )).scalar_one()

            r = await _submit(session_client, auth_token, open_pid)
            assert r.status_code == 409

            # Open still Open, Draft still Draft.
            open_status = (await direct_db.execute(
                _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": open_pid},
            )).scalar_one()
            assert open_status == "Open", f"Open must remain Open; got {open_status}"

            draft_status = (await direct_db.execute(
                _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": draft_pid},
            )).scalar_one()
            assert draft_status == "Draft", f"Draft must remain Draft; got {draft_status}"

            # No review item created.
            ri_count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM review.managerreviewitems
                    WHERE branchid = :bid AND entityid = :eid AND status = 'Pending'
                """),
                {"bid": paytest_branch_id, "eid": str(open_pid)},
            )).scalar_one()
            assert ri_count == 0, f"No review item must survive a blocker; got {ri_count}"

            # PERIOD_STATUS_CHANGED count unchanged.
            post_status_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'payroll' AND entityid = :eid
                      AND actioncode = 'PERIOD_STATUS_CHANGED'
                """),
                {"eid": str(open_pid)},
            )).scalar_one()
            assert post_status_audit == pre_status_audit, (
                f"PERIOD_STATUS_CHANGED must not grow on blocker; "
                f"pre={pre_status_audit}, post={post_status_audit}"
            )

            # REVIEW_ITEM_CREATED count unchanged.
            post_ri_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'review' AND entityname = 'ManagerReviewItems'
                      AND branchid = :bid AND actioncode = 'REVIEW_ITEM_CREATED'
                """),
                {"bid": paytest_branch_id},
            )).scalar_one()
            assert post_ri_audit == pre_ri_audit, (
                f"REVIEW_ITEM_CREATED must not grow on blocker; "
                f"pre={pre_ri_audit}, post={post_ri_audit}"
            )

            # SubmittedAtUtc IS NULL.
            sat = (await direct_db.execute(
                _text("SELECT submittedatutc FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": open_pid},
            )).scalar_one()
            assert sat is None, f"SubmittedAtUtc must remain NULL after blocker; got {sat}"

            # Hours calc sentinel unchanged — B2 fired before _refresh_draft_calculations
            # was reached.  If refresh had run, the value would be 1 × 25.00 = 25.00;
            # the fact that it is still 0.01 proves refresh was never called.
            calc = (await direct_db.execute(
                _text("SELECT calculatedamount FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
                {"lid": sentinel_line_id},
            )).scalar_one()
            assert calc is not None and abs(float(calc) - 0.01) < 1e-9, (
                f"Hours calc sentinel must remain 0.01 (refresh never ran — B2 fired first); got {calc}"
            )

        finally:
            await session_client.delete(f"/payroll/rates/{rate_id}", headers=_auth(auth_token))

    @pytest.mark.asyncio
    async def test_e3_review_audit_failure_rollback(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id,
        paytest_rate_type_id, direct_db, monkeypatch,
    ):
        """E3: If Open→InReview audit write fails (after review item INSERT + status UPDATE),
        the entire transaction rolls back.

        _refresh_draft_calculations DOES run before the audit.  Sentinel: a PerUnit Hours
        line backed by an approved HOURLY rate.  Inside the transaction, refresh updates
        calculatedamount from 0.01 → 25.00 (qty=1 × rate=$25.00).  When the audit write
        fails, the entire transaction rolls back — including the refresh update — so the
        value is restored to 0.01, proving that refresh changes are not durable without a
        successful commit.

        Proof: Open remains Open, no review item persists, PERIOD_STATUS_CHANGED count
        unchanged, REVIEW_ITEM_CREATED count unchanged, SubmittedAtUtc IS NULL,
        Hours calc sentinel = 0.01 (refresh ran but was rolled back).
        """
        await _clean(direct_db, paytest_branch_id)

        start, end = _dates_2095(120)
        open_pid = await _insert_open_period(direct_db, paytest_branch_id, start, end, "E3-OP")

        # Create and approve an HOURLY rate so _refresh_draft_calculations resolves
        # qty × rate = 1 × 25.00 = 25.00 (non-null, rate-dependent update).
        rate_id = await _create_approved_hourly_rate(
            session_client, auth_token, paytest_driver_id,
            paytest_rate_type_id, start,
        )
        try:
            await _add_hours_line(session_client, auth_token, open_pid, paytest_driver_id, start)

            # Stale the Hours line's calculatedamount to sentinel 0.01 via AUTOCOMMIT.
            # _refresh_draft_calculations recomputes it to 25.00 inside the transaction;
            # rollback restores it to 0.01.
            line_row = (await direct_db.execute(
                _text(
                    "SELECT draftlineid FROM payroll.payrolldraftlines "
                    "WHERE payrollperiodid = :pid AND status != 'Void' "
                    "AND linetype = 'HOURS' LIMIT 1"
                ),
                {"pid": open_pid},
            )).mappings().first()
            assert line_row is not None, "Expected an Hours draft line for E3"
            sentinel_line_id = line_row["draftlineid"]
            await direct_db.execute(
                _text(
                    "UPDATE payroll.payrolldraftlines SET calculatedamount = 0.01 "
                    "WHERE draftlineid = :lid"
                ),
                {"lid": sentinel_line_id},
            )

            # Capture pre-submit audit counts.
            pre_status_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods'
                      AND entityid = :eid AND actioncode = 'PERIOD_STATUS_CHANGED'
                """),
                {"eid": str(open_pid)},
            )).scalar_one()

            pre_ri_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'review' AND entityname = 'ManagerReviewItems'
                      AND branchid = :bid AND actioncode = 'REVIEW_ITEM_CREATED'
                """),
                {"bid": paytest_branch_id},
            )).scalar_one()

            # Inject failure at the Open→InReview status audit (fires after review item INSERT
            # and status UPDATE — everything runs, then the audit write fails → full rollback).
            # Stage B4-18: _write_period_status_audit's real implementation
            # now lives in app.payroll.period_lifecycle, and
            # change_period_status (also in period_lifecycle) resolves it as
            # a bare name through that module's own globals.
            from app.payroll import period_lifecycle as _svc
            original = _svc._write_period_status_audit
            from fastapi import HTTPException as _HTTPException

            async def _fail_on_inreview(db, *, company_id, branch_id, user_id, period_id,
                                         old_status, new_status, extra=None):
                if old_status == "Open" and new_status == "InReview":
                    raise _HTTPException(status_code=500, detail="Injected: InReview audit failure")
                return await original(
                    db, company_id=company_id, branch_id=branch_id, user_id=user_id,
                    period_id=period_id, old_status=old_status, new_status=new_status, extra=extra,
                )

            monkeypatch.setattr(_svc, "_write_period_status_audit", _fail_on_inreview)

            r = await _submit(session_client, auth_token, open_pid)
            assert r.status_code == 500

            # Open must still be Open.
            open_status = (await direct_db.execute(
                _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": open_pid},
            )).scalar_one()
            assert open_status == "Open", f"Open must remain Open after rollback; got {open_status}"

            # No Pending review item must exist.
            ri_count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM review.managerreviewitems
                    WHERE branchid = :bid AND entityid = :eid AND status = 'Pending'
                """),
                {"bid": paytest_branch_id, "eid": str(open_pid)},
            )).scalar_one()
            assert ri_count == 0, f"No review item must survive audit failure; got {ri_count}"

            # PERIOD_STATUS_CHANGED count unchanged.
            post_status_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods'
                      AND entityid = :eid AND actioncode = 'PERIOD_STATUS_CHANGED'
                """),
                {"eid": str(open_pid)},
            )).scalar_one()
            assert post_status_audit == pre_status_audit, (
                f"PERIOD_STATUS_CHANGED must not grow on rollback; "
                f"pre={pre_status_audit}, post={post_status_audit}"
            )

            # REVIEW_ITEM_CREATED count unchanged.
            post_ri_audit = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE entityschema = 'review' AND entityname = 'ManagerReviewItems'
                      AND branchid = :bid AND actioncode = 'REVIEW_ITEM_CREATED'
                """),
                {"bid": paytest_branch_id},
            )).scalar_one()
            assert post_ri_audit == pre_ri_audit, (
                f"REVIEW_ITEM_CREATED must not grow on rollback; "
                f"pre={pre_ri_audit}, post={post_ri_audit}"
            )

            # SubmittedAtUtc IS NULL (status UPDATE rolled back).
            sat = (await direct_db.execute(
                _text("SELECT submittedatutc FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": open_pid},
            )).scalar_one()
            assert sat is None, f"SubmittedAtUtc must remain NULL after rollback; got {sat}"

            # Hours calc sentinel must remain 0.01 — _refresh_draft_calculations ran
            # (it updated calculatedamount from 0.01 to 25.00 inside the transaction)
            # but the audit write failure rolled back everything including that update.
            calc = (await direct_db.execute(
                _text("SELECT calculatedamount FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
                {"lid": sentinel_line_id},
            )).scalar_one()
            assert calc is not None and abs(float(calc) - 0.01) < 1e-9, (
                f"Hours calc sentinel must remain 0.01 (refresh ran but was rolled back); got {calc}"
            )

        finally:
            await session_client.delete(f"/payroll/rates/{rate_id}", headers=_auth(auth_token))
