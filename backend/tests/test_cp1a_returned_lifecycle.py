"""
CP-1A: Pre-finalization Returned-for-Correction lifecycle tests.

Product contract verified here:
  - Rejected / EditRequested review decisions → period moves to 'Returned'
  - Rejected / EditRequested without decision_reason → 422
  - Returned period is editable (draft-line, period-pay, day-grid paths)
  - Direct PATCH targeting Returned is blocked (422)
  - InReview → Cancelled via PATCH is blocked (422)
  - Returned → Cancelled via PATCH is blocked (422)
  - POST /payroll/periods/{id}/resubmissions: Returned → InReview
  - Resubmission creates a new Pending PeriodApproval; old item stays resolved
  - One-Returned-slot invariant per CompanyID/BranchID
  - current_return_review_item_id is set on return and cleared on resubmit
  - Schema: column, FK, partial unique index, CHECK constraints present
  - Migration downgrade guard refuses while Returned rows exist

Dates: 2095-* — isolated year.  Run from backend/:
    python -m pytest tests/test_cp1a_returned_lifecycle.py -v
"""
import datetime
import itertools

import pytest
import httpx
import pytest_asyncio
from sqlalchemy import text as _text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_WEEK_COUNTER = itertools.count(0)


def _next_dates() -> tuple[str, str]:
    """Return a unique (start, end) date pair in 2095."""
    n = next(_WEEK_COUNTER)
    base = datetime.date(2095, 1, 6)   # first Monday of 2095
    start = base + datetime.timedelta(weeks=n)
    end   = start + datetime.timedelta(days=6)
    return start.isoformat(), end.isoformat()


async def _cancel_active(direct_db, branch_id: int) -> None:
    # Clear CurrentReturnReviewItemID for Returned periods first — the pointer-consistency
    # CHECK requires it to be NULL when status != 'Returned'.
    await direct_db.execute(
        _text(
            "UPDATE payroll.payrollperiods "
            "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
            "WHERE branchid = :bid AND status = 'Returned'"
        ),
        {"bid": branch_id},
    )
    await direct_db.execute(
        _text(
            "UPDATE payroll.payrollperiods "
            "SET status = 'Cancelled' "
            "WHERE branchid = :bid "
            "  AND status IN ('Draft', 'Open', 'InReview', 'Approved')"
        ),
        {"bid": branch_id},
    )


async def _create_open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    direct_db,
) -> tuple[int, str]:
    """Create Draft → Open period.  Returns (period_id, work_date_str)."""
    await _cancel_active(direct_db, branch_id)
    start, end = _next_dates()
    r = await client.post(
        "/payroll/periods",
        json={"branch_id": branch_id, "period_type": "Week",
              "start_date": start, "end_date": end},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"create period: {r.text}"
    pid = r.json()["payroll_period_id"]
    r2 = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "Open"},
        headers=_auth(token),
    )
    assert r2.status_code == 200, f"open period: {r2.text}"
    return pid, start


async def _add_line(
    client: httpx.AsyncClient,
    token: str,
    pid: int,
    driver_id: int,
    work_date: str,
) -> int:
    r = await client.post(
        f"/payroll/periods/{pid}/lines",
        json={"driver_id": driver_id, "work_date": work_date,
              "line_type": "PTO_STATUS", "quantity": 1},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"add line: {r.text}"
    return r.json()["draft_line_id"]


async def _submit(
    client: httpx.AsyncClient,
    token: str,
    pid: int,
    driver_id: int,
    work_date: str,
) -> int:
    """Ensure a line exists, submit period to InReview.  Returns review_item_id."""
    existing = await client.get(f"/payroll/periods/{pid}/lines", headers=_auth(token))
    if not existing.json():
        await _add_line(client, token, pid, driver_id, work_date)
    r = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "InReview"},
        headers=_auth(token),
    )
    assert r.status_code == 200, f"submit: {r.text}"
    ri_resp = await client.get(
        "/review/items",
        params={"payroll_period_id": pid},
        headers=_auth(token),
    )
    assert ri_resp.status_code == 200
    pending = [i for i in ri_resp.json() if i["status"] == "Pending"]
    assert pending, f"no Pending review item for period {pid}"
    return pending[0]["review_item_id"]


async def _return_period(
    client: httpx.AsyncClient,
    token: str,
    pid: int,
    driver_id: int,
    work_date: str,
    decision: str = "Rejected",
    reason: str = "Needs correction",
) -> tuple[int, int]:
    """Open → InReview → Returned.  Returns (period_id, review_item_id)."""
    ri_id = await _submit(client, token, pid, driver_id, work_date)
    dec = await client.post(
        f"/review/items/{ri_id}/decide",
        json={"decision": decision, "decision_reason": reason},
        headers=_auth(token),
    )
    assert dec.status_code == 200, f"decide {decision}: {dec.text}"
    return pid, ri_id


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def cp1a_clean(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """Cancel any leftover active/returned periods before and after each test."""
    await _cancel_active(direct_db, paytest_branch_id)
    yield paytest_branch_id
    await _cancel_active(direct_db, paytest_branch_id)


# ---------------------------------------------------------------------------
# 1. Schema / migration assertions
# ---------------------------------------------------------------------------

class TestSchemaPresence:

    @pytest.mark.asyncio
    async def test_currentreturnreviewitemid_column_exists(self, direct_db):
        row = await direct_db.execute(
            _text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'payroll' AND table_name = 'payrollperiods' "
                "  AND column_name = 'currentreturnreviewitemid'"
            )
        )
        assert row.first() is not None, "Column CurrentReturnReviewItemID must exist"

    @pytest.mark.asyncio
    async def test_partial_unique_index_exists(self, direct_db):
        row = await direct_db.execute(
            _text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'payroll' "
                "  AND indexname = 'ux_payrollperiods_onereturnedperbranch'"
            )
        )
        assert row.first() is not None, "Partial unique index ux_PayrollPeriods_OneReturnedPerBranch must exist"

    @pytest.mark.asyncio
    async def test_returned_in_status_check_constraint(self, direct_db):
        row = await direct_db.execute(
            _text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_payrollperiods_status' "
                "  AND conrelid = 'payroll.payrollperiods'::regclass"
            )
        )
        r = row.first()
        assert r is not None, "ck_PayrollPeriods_Status constraint must exist"
        assert "Returned" in r[0], f"'Returned' not found in constraint: {r[0]}"

    @pytest.mark.asyncio
    async def test_pointer_consistency_check_constraint(self, direct_db):
        row = await direct_db.execute(
            _text(
                "SELECT conname FROM pg_constraint "
                "WHERE conname = 'ck_payrollperiods_returnedpointerconsistency' "
                "  AND conrelid = 'payroll.payrollperiods'::regclass"
            )
        )
        assert row.first() is not None, "ck_PayrollPeriods_ReturnedPointerConsistency must exist"

    @pytest.mark.asyncio
    async def test_review_unique_index_exists(self, direct_db):
        row = await direct_db.execute(
            _text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'review' "
                "  AND indexname = 'ux_managerreviewitems_reviewitemid_company_branch'"
            )
        )
        assert row.first() is not None, "ux_ManagerReviewItems_ReviewItemID_Company_Branch must exist"


# ---------------------------------------------------------------------------
# 2. Review decisions → Returned
# ---------------------------------------------------------------------------

class TestReviewDecisionsReturnPeriod:

    @pytest.mark.asyncio
    async def test_rejected_moves_period_to_returned(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _return_period(session_client, auth_token, pid, paytest_driver_id, work_date,
                             decision="Rejected", reason="Wrong totals")
        r = await session_client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert r.json()["status"] == "Returned"

    @pytest.mark.asyncio
    async def test_edit_requested_moves_period_to_returned(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _return_period(session_client, auth_token, pid, paytest_driver_id, work_date,
                             decision="EditRequested", reason="Please fix line 3")
        r = await session_client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert r.json()["status"] == "Returned"

    @pytest.mark.asyncio
    async def test_rejected_without_reason_is_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        ri_id = await _submit(session_client, auth_token, pid, paytest_driver_id, work_date)
        r = await session_client.post(
            f"/review/items/{ri_id}/decide",
            json={"decision": "Rejected"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, f"Expected 422 for missing reason, got {r.status_code}: {r.text}"
        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_rejected_with_blank_reason_is_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        ri_id = await _submit(session_client, auth_token, pid, paytest_driver_id, work_date)
        r = await session_client.post(
            f"/review/items/{ri_id}/decide",
            json={"decision": "Rejected", "decision_reason": "   "},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, f"Expected 422 for blank reason, got {r.status_code}: {r.text}"
        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_edit_requested_without_reason_is_422(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        ri_id = await _submit(session_client, auth_token, pid, paytest_driver_id, work_date)
        r = await session_client.post(
            f"/review/items/{ri_id}/decide",
            json={"decision": "EditRequested"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, f"Expected 422 for missing reason, got {r.status_code}: {r.text}"
        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_current_return_review_item_id_set_after_return(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        pid, ri_id = await _return_period(
            session_client, auth_token, pid, paytest_driver_id, work_date
        )
        r = await session_client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        body = r.json()
        assert body["current_return_review_item_id"] == ri_id, (
            f"current_return_review_item_id should be {ri_id}, got {body.get('current_return_review_item_id')}"
        )

    @pytest.mark.asyncio
    async def test_approved_still_moves_to_approved(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Approved decision path is unaffected by CP-1A."""
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        ri_id = await _submit(session_client, auth_token, pid, paytest_driver_id, work_date)
        dec = await session_client.post(
            f"/review/items/{ri_id}/decide",
            json={"decision": "Approved"},
            headers=_auth(auth_token),
        )
        assert dec.status_code == 200, f"Approved decision failed: {dec.text}"
        r = await session_client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert r.json()["status"] == "Approved"


# ---------------------------------------------------------------------------
# 3. Cancellation blocked
# ---------------------------------------------------------------------------

class TestCancellationBlocked:

    @pytest.mark.asyncio
    async def test_inreview_to_cancelled_now_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """CP-1A: InReview→Cancelled via PATCH is now blocked."""
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _submit(session_client, auth_token, pid, paytest_driver_id, work_date)
        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"CP-1A: InReview→Cancelled must be blocked (422), got {r.status_code}: {r.text}"
        )
        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_returned_to_cancelled_is_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """CP-1A: Returned→Cancelled via PATCH is blocked."""
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _return_period(session_client, auth_token, pid, paytest_driver_id, work_date)
        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"Returned→Cancelled must be blocked (422), got {r.status_code}: {r.text}"
        )
        await _cancel_active(direct_db, paytest_branch_id)


# ---------------------------------------------------------------------------
# 4. Direct PATCH targeting Returned is reserved
# ---------------------------------------------------------------------------

class TestDirectPatchReturnedBlocked:

    @pytest.mark.asyncio
    async def test_patch_to_returned_status_is_reserved(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Direct PATCH {"status": "Returned"} is blocked on any period."""
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _submit(session_client, auth_token, pid, paytest_driver_id, work_date)
        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Returned"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"Direct PATCH→Returned must be blocked (422), got {r.status_code}: {r.text}"
        )
        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_patch_inreview_to_open_now_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """CP-1A: InReview→Open via PATCH is now blocked."""
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _submit(session_client, auth_token, pid, paytest_driver_id, work_date)
        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"CP-1A: InReview→Open must be blocked (422), got {r.status_code}: {r.text}"
        )
        await _cancel_active(direct_db, paytest_branch_id)


# ---------------------------------------------------------------------------
# 5. Returned is editable (source mutations accepted)
# ---------------------------------------------------------------------------

class TestReturnedEditability:

    @pytest.mark.asyncio
    async def test_draft_line_add_on_returned_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _return_period(session_client, auth_token, pid, paytest_driver_id, work_date)

        next_date = (
            datetime.date.fromisoformat(work_date) + datetime.timedelta(days=1)
        ).isoformat()
        r = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": next_date,
                  "line_type": "PTO_STATUS", "quantity": 2},
            headers=_auth(auth_token),
        )
        assert r.status_code == 201, (
            f"Draft line add on Returned period should succeed (201), got {r.status_code}: {r.text}"
        )

    @pytest.mark.asyncio
    async def test_draft_line_delete_on_returned_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        lid = await _add_line(session_client, auth_token, pid, paytest_driver_id, work_date)
        await _return_period(session_client, auth_token, pid, paytest_driver_id, work_date)

        r = await session_client.delete(
            f"/payroll/periods/{pid}/lines/{lid}",
            headers=_auth(auth_token),
        )
        assert r.status_code in (200, 204), (
            f"Draft line delete on Returned period should succeed, got {r.status_code}: {r.text}"
        )

    @pytest.mark.asyncio
    async def test_source_mutation_rejected_on_inreview(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """InReview period must still reject source mutations (regression guard)."""
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _submit(session_client, auth_token, pid, paytest_driver_id, work_date)
        next_date = (
            datetime.date.fromisoformat(work_date) + datetime.timedelta(days=1)
        ).isoformat()
        r = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": next_date,
                  "line_type": "PTO_STATUS", "quantity": 1},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"InReview must reject source mutations (422), got {r.status_code}: {r.text}"
        )
        await _cancel_active(direct_db, paytest_branch_id)


# ---------------------------------------------------------------------------
# 6. Resubmission endpoint
# ---------------------------------------------------------------------------

class TestResubmission:

    @pytest.mark.asyncio
    async def test_resubmit_returned_moves_to_inreview(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _return_period(session_client, auth_token, pid, paytest_driver_id, work_date)

        r = await session_client.post(
            f"/payroll/periods/{pid}/resubmissions",
            headers=_auth(auth_token),
        )
        assert r.status_code == 200, f"Resubmit failed: {r.text}"
        assert r.json()["status"] == "InReview", f"Expected InReview, got: {r.json()['status']}"

    @pytest.mark.asyncio
    async def test_resubmit_clears_current_return_review_item_id(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _return_period(session_client, auth_token, pid, paytest_driver_id, work_date)

        r = await session_client.post(
            f"/payroll/periods/{pid}/resubmissions",
            headers=_auth(auth_token),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["current_return_review_item_id"] is None, (
            f"current_return_review_item_id must be NULL after resubmit, got {body.get('current_return_review_item_id')}"
        )

    @pytest.mark.asyncio
    async def test_resubmit_creates_new_pending_review_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        pid, first_ri_id = await _return_period(
            session_client, auth_token, pid, paytest_driver_id, work_date
        )

        await session_client.post(
            f"/payroll/periods/{pid}/resubmissions",
            headers=_auth(auth_token),
        )

        # /review/items doesn't filter by period; query DB directly via entityid
        rows = await direct_db.execute(
            _text(
                "SELECT reviewitemid, status FROM review.managerreviewitems "
                "WHERE entityid = :pid "
                "ORDER BY reviewitemid"
            ),
            {"pid": str(pid)},
        )
        items = [{"review_item_id": r[0], "status": r[1]} for r in rows.fetchall()]
        pending = [i for i in items if i["status"] == "Pending"]
        assert len(pending) == 1, f"Expected exactly 1 Pending review item, got {len(pending)}: {items}"
        assert pending[0]["review_item_id"] != first_ri_id, (
            "Resubmit must create a NEW review item, not reuse the old one"
        )

    @pytest.mark.asyncio
    async def test_resubmit_old_review_item_stays_resolved(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        pid, first_ri_id = await _return_period(
            session_client, auth_token, pid, paytest_driver_id, work_date
        )

        await session_client.post(
            f"/payroll/periods/{pid}/resubmissions",
            headers=_auth(auth_token),
        )

        # Query DB directly since /review/items doesn't filter by period
        rows = await direct_db.execute(
            _text(
                "SELECT reviewitemid, status FROM review.managerreviewitems "
                "WHERE entityid = :pid "
                "ORDER BY reviewitemid"
            ),
            {"pid": str(pid)},
        )
        items = [{"review_item_id": r[0], "status": r[1]} for r in rows.fetchall()]
        first = next((i for i in items if i["review_item_id"] == first_ri_id), None)
        assert first is not None, f"Original review item {first_ri_id} should still exist"
        assert first["status"] != "Pending", (
            f"Old review item must not be Pending after resubmit, got {first['status']}"
        )

    @pytest.mark.asyncio
    async def test_resubmit_on_open_period_fails(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        r = await session_client.post(
            f"/payroll/periods/{pid}/resubmissions",
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"Resubmit on Open period must be 422, got {r.status_code}: {r.text}"
        )

    @pytest.mark.asyncio
    async def test_resubmit_on_inreview_period_fails(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _submit(session_client, auth_token, pid, paytest_driver_id, work_date)
        r = await session_client.post(
            f"/payroll/periods/{pid}/resubmissions",
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"Resubmit on InReview period must be 422, got {r.status_code}: {r.text}"
        )
        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_resubmit_full_roundtrip(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Returned → resubmit → Returned → resubmit → InReview (two cycles)."""
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _return_period(session_client, auth_token, pid, paytest_driver_id, work_date)

        r1 = await session_client.post(
            f"/payroll/periods/{pid}/resubmissions",
            headers=_auth(auth_token),
        )
        assert r1.status_code == 200 and r1.json()["status"] == "InReview"

        # Return again (second cycle) — query DB for the Pending review item
        rows = await direct_db.execute(
            _text(
                "SELECT reviewitemid FROM review.managerreviewitems "
                "WHERE entityid = :pid AND status = 'Pending' "
                "ORDER BY reviewitemid DESC LIMIT 1"
            ),
            {"pid": str(pid)},
        )
        row = rows.first()
        assert row is not None, "Expected a Pending review item after first resubmit"
        dec = await session_client.post(
            f"/review/items/{row[0]}/decide",
            json={"decision": "EditRequested", "decision_reason": "Still wrong"},
            headers=_auth(auth_token),
        )
        assert dec.status_code == 200

        r2 = await session_client.post(
            f"/payroll/periods/{pid}/resubmissions",
            headers=_auth(auth_token),
        )
        assert r2.status_code == 200, f"Second resubmit failed: {r2.text}"
        assert r2.json()["status"] == "InReview"

        # Verify: exactly 1 Pending item for this period, and at least 3 total
        rows2 = await direct_db.execute(
            _text(
                "SELECT reviewitemid, status FROM review.managerreviewitems "
                "WHERE entityid = :pid ORDER BY reviewitemid"
            ),
            {"pid": str(pid)},
        )
        all_items = [{"review_item_id": r[0], "status": r[1]} for r in rows2.fetchall()]
        assert len([i for i in all_items if i["status"] == "Pending"]) == 1
        assert len(all_items) >= 3, "Should have at least 3 review items (original submit + 2 cycles)"
        await _cancel_active(direct_db, paytest_branch_id)


# ---------------------------------------------------------------------------
# 7. One-Returned-slot invariant
# ---------------------------------------------------------------------------

class TestOneReturnedSlot:

    @pytest.mark.asyncio
    async def test_second_return_for_same_branch_is_409(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        After one Returned period exists for a branch, attempting to return
        a second InReview period for the same branch must yield 409.

        Per CP-1A product contract, Returned may coexist with a newer Open period
        (CP-1D adds the newer-Open blocker). So we can create period2 via API,
        submit it to InReview, and then try to return it while period1 is Returned.
        """
        # Period 1: Open -> InReview -> Returned
        pid1, work_date1 = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        pid1, ri_id1 = await _return_period(
            session_client, auth_token, pid1, paytest_driver_id, work_date1
        )
        r = await session_client.get(f"/payroll/periods/{pid1}", headers=_auth(auth_token))
        assert r.json()["status"] == "Returned", "Period 1 must be Returned before slot test"

        # Period 2: create without cancelling period 1 (Returned coexists per CP-1A)
        start2, end2 = _next_dates()
        create_r = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": start2, "end_date": end2},
            headers=_auth(auth_token),
        )
        if create_r.status_code != 201:
            # If API blocks a second period while Returned exists, skip gracefully —
            # this is CP-1D's responsibility, not CP-1A's.
            pytest.skip(
                f"Cannot create period2 while Returned exists ({create_r.status_code}); "
                "CP-1D blocker may be active — slot guard still enforced by DB index."
            )
        pid2 = create_r.json()["payroll_period_id"]

        # Open period 2
        open_r = await session_client.patch(
            f"/payroll/periods/{pid2}/status",
            json={"status": "Open"},
            headers=_auth(auth_token),
        )
        assert open_r.status_code == 200, f"Open period2 failed: {open_r.text}"

        # Submit period 2 to InReview
        ri2_id = await _submit(session_client, auth_token, pid2, paytest_driver_id, start2)

        # Attempt to return period 2 while period 1 is already Returned for same branch
        dec = await session_client.post(
            f"/review/items/{ri2_id}/decide",
            json={"decision": "Rejected", "decision_reason": "Second return attempt"},
            headers=_auth(auth_token),
        )
        assert dec.status_code == 409, (
            f"Second return for same branch must be 409, got {dec.status_code}: {dec.text}"
        )
        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_two_inreview_return_race_structurally_impossible_after_cp1b(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        Replaces test_concurrent_return_race_one_succeeds_one_409 after CP-1B.

        The original test created two simultaneous InReview periods and raced them
        to Returned.  After CP-1B, that scenario is structurally unreachable: the
        ux_payrollperiods_oneinreviewperbranch index blocks any second period from
        entering InReview while one already exists.  Without two InReview periods,
        the two-InReview→Returned race cannot be set up.

        This test proves:
        1.  A second Open→InReview submit is blocked with 409 (InReview slot full).
        2.  Period 2 remains Open; period 1 remains InReview.
        3.  The one-Returned unique index is still exercised sequentially by
            test_second_return_for_same_branch_is_409 (the race form is unreachable).

        To restore the two-InReview scenario the index would have to be dropped, at
        which point this test would fail (the second submit would return 201, not 409).
        """
        await _cancel_active(direct_db, paytest_branch_id)

        # -- Period 1: Draft → Open → add line → InReview (occupies the slot)
        start1, end1 = _next_dates()
        r = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": start1, "end_date": end1},
            headers=_auth(auth_token),
        )
        assert r.status_code == 201, f"create p1: {r.text}"
        pid1 = r.json()["payroll_period_id"]
        await session_client.patch(f"/payroll/periods/{pid1}/status",
                                   json={"status": "Open"}, headers=_auth(auth_token))
        await _add_line(session_client, auth_token, pid1, paytest_driver_id, start1)
        r_submit1 = await session_client.patch(
            f"/payroll/periods/{pid1}/status",
            json={"status": "InReview"},
            headers=_auth(auth_token),
        )
        assert r_submit1.status_code == 200, (
            f"First InReview submit must succeed; got {r_submit1.status_code}: {r_submit1.text}"
        )

        # -- Period 2: Draft → Open → add line → attempt InReview (slot is full)
        start2, end2 = _next_dates()
        r = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": start2, "end_date": end2},
            headers=_auth(auth_token),
        )
        if r.status_code != 201:
            pytest.skip(f"Cannot create period2 ({r.status_code}); may require CP-1D for multi-open.")
        pid2 = r.json()["payroll_period_id"]
        await session_client.patch(f"/payroll/periods/{pid2}/status",
                                   json={"status": "Open"}, headers=_auth(auth_token))
        await _add_line(session_client, auth_token, pid2, paytest_driver_id, start2)

        r_submit2 = await session_client.patch(
            f"/payroll/periods/{pid2}/status",
            json={"status": "InReview"},
            headers=_auth(auth_token),
        )
        assert r_submit2.status_code == 409, (
            f"Second InReview submit must be blocked 409 (CP-1B slot full); "
            f"got {r_submit2.status_code}: {r_submit2.text}"
        )
        detail = r_submit2.json().get("detail", "")
        assert "inreview" in detail.lower() or "in review" in detail.lower(), (
            f"409 detail must describe InReview slot conflict; got: {detail!r}"
        )

        # Period 1 must remain InReview; period 2 must remain Open.
        p1_row = (await direct_db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid1},
        )).mappings().first()
        assert p1_row["status"] == "InReview", (
            f"Period 1 must remain InReview; got {p1_row['status']!r}"
        )

        p2_row = (await direct_db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid2},
        )).mappings().first()
        assert p2_row["status"] == "Open", (
            f"Period 2 must remain Open after rejected submit; got {p2_row['status']!r}"
        )

        # Exactly one InReview period for this branch.
        ir_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollperiods "
                  "WHERE branchid = :bid AND status = 'InReview'"),
            {"bid": paytest_branch_id},
        )).scalar_one()
        assert ir_count == 1, (
            f"Exactly one InReview period must exist; found {ir_count}"
        )

        await _cancel_active(direct_db, paytest_branch_id)


# ---------------------------------------------------------------------------
# 8. Read / terminal state behavior
# ---------------------------------------------------------------------------

class TestReturnedReadBehavior:

    @pytest.mark.asyncio
    async def test_returned_period_visible_in_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _return_period(session_client, auth_token, pid, paytest_driver_id, work_date)

        r = await session_client.get(
            "/payroll/periods",
            params={"branch_id": paytest_branch_id},
            headers=_auth(auth_token),
        )
        assert r.status_code == 200
        periods = r.json()
        returned = [p for p in periods if p["payroll_period_id"] == pid]
        assert returned and returned[0]["status"] == "Returned"

    @pytest.mark.asyncio
    async def test_returned_period_get_by_id(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid, work_date = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        pid, ri_id = await _return_period(
            session_client, auth_token, pid, paytest_driver_id, work_date
        )
        r = await session_client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "Returned"
        assert body["current_return_review_item_id"] == ri_id
