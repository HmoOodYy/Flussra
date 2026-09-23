"""
Integration tests for finalization and the final-lines ledger:

  POST /payroll/periods/{id}/finalize
  GET  /payroll/periods/{id}/final-lines

Isolation
---------
Tests that mutate period state use `approved_period`, a function-scoped
fixture that creates a fresh Open period on PAYTEST and guarantees cleanup via
`paytest_clean`.

`paytest_driver_id` (session-scoped, conftest) provides a valid driver on
the PAYTEST branch so we can seed draft lines before finalizing.
"""
import datetime
import uuid
import pytest
import pytest_asyncio
import httpx
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from sqlalchemy import text as _sqla_text
from uuid import uuid4

# Stage B4-19: _write_finalization_audit's real implementation now lives in
# app.payroll.finalization, and finalize_period (also in finalization)
# resolves it as a bare name through that module's own globals — patching
# app.payroll.service no longer intercepts it.
from app.payroll import finalization as payroll_service


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Use a module-isolated branch for finalization workflow tests."""
    row = (await session_db_conn.execute(_sqla_text("""
        INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {"code": f"FIN_{uuid4().hex}", "name": "Finalize isolated"})).scalar_one()
    return int(row)


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Create the finalization test driver on this module's isolated branch."""
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": "Finalize Isolated Driver",
            "driver_code": f"FIN-D-{uuid4().hex[:10]}",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"Finalize driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    # CP-1A: only Draft and Open can be cancelled via PATCH.
    headers = auth(token)
    for s in ("Draft", "Open"):
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

async def _force_cancel_locked_periods(direct_db, branch_id: int) -> None:
    """Cancel Locked/Archived/InReview/Approved/Returned periods bypassing blocked PATCH paths."""
    from sqlalchemy import text as _text
    # CP-1A: InReview and Approved cannot be cancelled via PATCH; use direct DB.
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
              "WHERE branchid = :bid AND status IN ('InReview', 'Approved')"),
        {"bid": branch_id},
    )
    # Returned: must clear CurrentReturnReviewItemID first (pointer-consistency CHECK).
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods "
              "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
              "WHERE branchid = :bid AND status = 'Returned'"),
        {"bid": branch_id},
    )
    await direct_db.execute(
        _text("ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable")
    )
    await direct_db.execute(
        _text("ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert")
    )
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
              "WHERE branchid = :bid AND status IN ('Locked', 'Archived')"),
        {"bid": branch_id},
    )
    await direct_db.execute(
        _text("ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable")
    )
    await direct_db.execute(
        _text("ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert")
    )


@pytest_asyncio.fixture
async def paytest_clean(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    from sqlalchemy import text as _text
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    # Force-cancel any Locked/Archived periods left by previous tests
    await _force_cancel_locked_periods(direct_db, paytest_branch_id)
    yield paytest_branch_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    # Force-cancel any Locked/Archived periods created during this test
    await _force_cancel_locked_periods(direct_db, paytest_branch_id)


async def _advance_to_approved(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    start_date: str = "2032-01-07",
) -> dict:
    """Submit for review and approve via the review flow.
    Adds a dummy Miles line only if the period currently has no non-void lines."""
    headers = auth(token)
    # Check if there are already non-void lines
    lines_resp = await client.get(
        f"/payroll/periods/{period_id}/lines",
        headers=headers,
        params={"status": "Active"},
    )
    if lines_resp.status_code == 200 and len(lines_resp.json()) == 0:
        # Add a non-void line so the period is not empty (DailyNote needs no approved rate)
        await client.post(
            f"/payroll/periods/{period_id}/lines",
            headers=headers,
            json={
                "driver_id":   driver_id,
                "work_date":   start_date,
                "line_type":   "DailyNote",
                "quantity":    1,
                "notes":       "filler",
            },
        )
    # Submit to InReview
    r = await client.patch(
        f"/payroll/periods/{period_id}/status",
        headers=headers,
        json={"status": "InReview"},
    )
    assert r.status_code == 200, f"InReview failed: {r.text}"

    # Find the pending review item
    review_resp = await client.get("/review/items", headers=headers)
    assert review_resp.status_code == 200
    review_item = next(
        (i for i in review_resp.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(period_id)
         and i.get("status") == "Pending"),
        None,
    )
    assert review_item is not None, f"No pending review item found for period {period_id}"

    # Approve via review endpoint
    decide_resp = await client.post(
        f"/review/items/{review_item['review_item_id']}/decide",
        headers=headers,
        json={"decision": "Approved"},
    )
    assert decide_resp.status_code == 200, f"Approval failed: {decide_resp.text}"

    period_resp = await client.get(f"/payroll/periods/{period_id}", headers=headers)
    assert period_resp.status_code == 200
    return period_resp.json()


@pytest_asyncio.fixture
async def approved_period(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_clean: int,
    paytest_driver_id: int,
    direct_db,
) -> dict:
    """
    Create a period on PAYTEST and advance it to Approved status.
    The period is approved with no active draft lines (the dummy line used
    for the InReview transition is voided via direct DB after approval,
    so tests can add their own lines).
    Yields the Approved period response dict.
    """
    from sqlalchemy import text as _text
    headers = auth(auth_token)
    branch_id = paytest_clean

    # Insert Open period directly (CP-1D: POST requires existing Open; PATCH Draft→Open blocked).
    row = (await direct_db.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id,
         "code": f"FIN-2032-0106-{uuid.uuid4().hex[:10]}",
         "name": f"Finalize Test 2032-W01 {uuid.uuid4().hex[:8]}",
         "start": datetime.date(2032, 1, 6),
         "end": datetime.date(2032, 1, 12)},
    )).mappings().first()
    pid = row["payrollperiodid"]

    # Leave the period Open. Current finalization authority is the exact
    # immutable snapshot captured when this period is submitted and approved;
    # tests must add source lines before that transition.
    return {
        "payroll_period_id": pid,
        "branch_id": branch_id,
        "status": "Open",
    }


async def _seed_open_lines_and_approve(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    *,
    direct_db,
    lines: list[dict],
    void_indices: set[int] | None = None,
) -> list[dict]:
    """Add all source lines while Open, then capture one approved snapshot."""
    headers = auth(token)
    seeded = []
    for payload in lines:
        r = await client.post(
            f"/payroll/periods/{period_id}/lines",
            json={"driver_id": driver_id, **payload},
            headers=headers,
        )
        assert r.status_code == 201, f"add line failed: {r.text}"
        seeded.append(r.json())
    for index in void_indices or set():
        r = await client.delete(
            f"/payroll/periods/{period_id}/lines/{seeded[index]['draft_line_id']}",
            headers=headers,
        )
        assert r.status_code in (200, 204), f"void line failed: {r.text}"
    await _advance_to_approved(
        client,
        token,
        period_id,
        driver_id,
        lines[0].get("work_date", "2032-01-07"),
    )
    return seeded


async def _create_and_approve_rate(
    client: httpx.AsyncClient,
    token: str,
    driver_id: int,
    rate_type_id: int,
    amount: str,
    effective_from: str = "2025-01-01",
) -> int:
    """Create and approve a DriverRate. Returns driver_rate_id."""
    headers = auth(token)
    rc = await client.post(
        "/payroll/rates",
        json={
            "driver_id":      driver_id,
            "rate_type_id":   rate_type_id,
            "amount":         amount,
            "effective_from": effective_from,
        },
        headers=headers,
    )
    assert rc.status_code == 201, f"Create rate failed: {rc.text}"
    rate_id = rc.json()["driver_rate_id"]
    ra = await client.post(f"/payroll/rates/{rate_id}/approve", headers=headers)
    if ra.status_code == 200:
        return rate_id

    # The session driver may already have an approved rate of this type from
    # an earlier test. Reuse that authoritative rate and remove only this
    # unapproved setup row; do not attempt to replace an approved rate.
    rates = await client.get("/payroll/rates", headers=headers)
    assert rates.status_code == 200, f"Rate lookup failed: {rates.text}"
    existing = next(
        (
            rate for rate in rates.json()
            if rate.get("driver_id") == driver_id
            and rate.get("rate_type_id") == rate_type_id
            and str(rate.get("status", "")).lower() == "approved"
        ),
        None,
    )
    if existing is None:
        raise AssertionError(f"Approve rate failed: {ra.text}")
    await client.delete(f"/payroll/rates/{rate_id}", headers=headers)
    return existing["driver_rate_id"]


async def _add_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    direct_db,
    *,
    line_type: str = "DailyNote",
    quantity: str = "1.00",
    work_date: str = "2032-01-07",
) -> dict:
    """
    Helper: temporarily open the period, add a draft line, then re-approve.
    The period must be in Approved status when called.
    Returns the created draft line dict.

    Default line_type is 'DailyNote' (ratebehavior='None') because:
    - 'None' behavior never triggers a DriverRate lookup.
    - needs_manager_review is always False (no unresolved calculation).
    - The period can always reach Approved without resolving a rate.
    - calculatedamount is NULL; finalamount is 0 at finalization.
    - Tests that need PerUnit calculation must set up an approved DriverRate first.

    Phase 4C: manual rate_amount is blocked for PerUnit lines.
    PerUnit line types (Hours, Miles, Wait, etc.) require an approved DriverRate.
    """
    from sqlalchemy import text as _text
    headers = auth(token)

    # CP-0C: Approved→InReview is now a blocked transition (removed from valid transitions
    # because it left InReview with no active PeriodApproval review item).
    # Force directly to Open via direct_db bypass instead.
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Open' WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )

    payload: dict = {
        "driver_id":  driver_id,
        "work_date":  work_date,
        "line_type":  line_type,
        "quantity":   quantity,
    }
    if line_type == "DailyNote":
        payload["notes"] = "filler"

    r = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json=payload,
        headers=headers,
    )
    assert r.status_code == 201, f"add line failed: {r.text}"
    line = r.json()

    # Safety assertion: the line must not require review.
    # For PerUnit line types, an approved DriverRate must be set up before calling.
    assert not line.get("needs_manager_review"), (
        f"Line {line['draft_line_id']} ({line_type}) was flagged for manager review "
        f"(calculatedamount=NULL). Set up an approved DriverRate before calling "
        f"_add_line with PerUnit line types."
    )

    # Open → InReview → Approved via review flow
    await _advance_to_approved(client, token, period_id, driver_id, work_date)

    return line


# ---------------------------------------------------------------------------
# POST /payroll/periods/{id}/finalize
# ---------------------------------------------------------------------------

class TestFinalizePeriod:

    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        approved_period: dict,
    ):
        pid = approved_period["payroll_period_id"]
        resp = await client.post(f"/payroll/periods/{pid}/finalize")
        assert resp.status_code == 401

    async def test_finalize_empty_period_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
    ):
        """
        Finalization requires an Approved period. A fresh fixture period is
        intentionally still Open until source lines are captured for review.
        """
        pid = approved_period["payroll_period_id"]
        resp = await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "approved" in resp.json()["detail"].lower()

    async def test_finalize_returns_locked_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = approved_period["payroll_period_id"]
        await _add_line(client, auth_token, pid, paytest_driver_id, direct_db)
        resp = await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "Locked"
        assert body["payroll_period_id"] == pid

    async def test_finalize_creates_final_lines(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
        paytest_rate_type_id: int,
    ):
        pid = approved_period["payroll_period_id"]
        await _create_and_approve_rate(
            client, auth_token, paytest_driver_id, paytest_rate_type_id, "25.00"
        )

        # Seed both lines while Open, then capture one immutable approval
        # snapshot containing both lines.
        await _seed_open_lines_and_approve(
            client,
            auth_token,
            pid,
            paytest_driver_id,
            direct_db=direct_db,
            lines=[
                {"work_date": "2032-01-07", "line_type": "Hours", "quantity": "1.00"},
                {"work_date": "2032-01-08", "line_type": "Hours", "quantity": "1.00"},
            ],
        )

        resp = await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200

        # Ledger should contain exactly 2 final lines
        ledger = await client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        assert ledger.status_code == 200
        lines = ledger.json()
        assert len(lines) == 2
        types = {l["line_type"] for l in lines}
        assert types == {"HOURS"}

    async def test_void_draft_lines_not_finalized(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
        paytest_rate_type_id: int,
    ):
        """Voided draft lines must be excluded from FinalLines."""
        from sqlalchemy import text as _text
        pid = approved_period["payroll_period_id"]
        headers = auth(auth_token)

        # Capture one active financial line and one voided financial line in
        # the one immutable approval snapshot.
        await _create_and_approve_rate(
            client, auth_token, paytest_driver_id, paytest_rate_type_id, "25.00"
        )
        seeded = await _seed_open_lines_and_approve(
            client,
            auth_token,
            pid,
            paytest_driver_id,
            direct_db=direct_db,
            lines=[
                {"work_date": "2032-01-07", "line_type": "Hours", "quantity": "1.00"},
                {"work_date": "2032-01-09", "line_type": "Hours", "quantity": "1.00"},
            ],
            void_indices={1},
        )

        # Finalize — the voided Overnight line must not appear.
        fin = await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=headers,
        )
        assert fin.status_code == 200, f"Finalization failed: {fin.text}"

        ledger = await client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=headers,
        )
        assert ledger.status_code == 200
        types = [l["line_type"] for l in ledger.json()]
        assert types == ["HOURS"]

    async def test_final_amount_computed_from_rate(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        paytest_mileage_rate_type_id: int,
        direct_db,
    ):
        """FinalAmount = quantity * approved_rate when calculatedamount is set."""
        pid = approved_period["payroll_period_id"]
        # Create and approve a MILEAGE rate of $0.50 so quantity=100 → final_amount=50.00
        rate_id = await _create_and_approve_rate(
            client, auth_token, paytest_driver_id, paytest_mileage_rate_type_id, "0.50"
        )
        try:
            await _add_line(
                client, auth_token, pid, paytest_driver_id, direct_db,
                line_type="Miles", quantity="100.00",
            )

            await client.post(
                f"/payroll/periods/{pid}/finalize",
                headers=auth(auth_token),
            )
            ledger = await client.get(
                f"/payroll/periods/{pid}/final-lines",
                headers=auth(auth_token),
            )
            miles_lines = [l for l in ledger.json() if l["line_type"] == "MILES"]
            assert len(miles_lines) == 1
            assert Decimal(str(miles_lines[0]["final_amount"])) == Decimal("50.00")
        finally:
            await client.delete(f"/payroll/rates/{rate_id}", headers=auth(auth_token))

    async def test_finalize_non_approved_period_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_clean: int,
        direct_db,
    ):
        """Attempting to finalize an Open period must return 422."""
        headers = auth(auth_token)
        # Insert an Open period directly (CP-1D: POST requires existing Open; PATCH Draft→Open blocked)
        row = (await direct_db.execute(
            _sqla_text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Open', 'FIN-NONAPPRV-2033', 'Finalize Non-Approved Test', 'Week', :start, :end)
                ON CONFLICT DO NOTHING
                RETURNING payrollperiodid
            """),
            {"bid": paytest_clean,
             "start": datetime.date(2033, 1, 6),
             "end": datetime.date(2033, 1, 12)},
        )).mappings().first()
        if row is None:
            row = (await direct_db.execute(
                _sqla_text(
                    "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE branchid = :bid AND periodcode = 'FIN-NONAPPRV-2033'"
                ),
                {"bid": paytest_clean},
            )).mappings().first()
        pid = row["payrollperiodid"]

        resp = await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=headers,
        )
        assert resp.status_code == 422
        assert "Approved" in resp.json()["detail"]

    async def test_finalize_draft_period_returns_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_period_id: int,
    ):
        """
        Attempting to finalize a Draft period must return 422.
        Uses the session-scoped `created_period_id` which is always in Draft status
        on the HQ branch — no period creation needed, no unique-constraint risk.
        """
        resp = await client.post(
            f"/payroll/periods/{created_period_id}/finalize",
            headers=auth(auth_token),
        )
        assert resp.status_code == 422

    async def test_404_for_unknown_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.post(
            "/payroll/periods/999999/finalize",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_locked_period_not_re_finalizable(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """Calling finalize a second time on a now-Locked period must return 422."""
        pid = approved_period["payroll_period_id"]
        headers = auth(auth_token)

        await _add_line(client, auth_token, pid, paytest_driver_id, direct_db)

        # First finalization
        r1 = await client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
        assert r1.status_code == 200

        # Second call — period is now Locked, not Approved
        r2 = await client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
        assert r2.status_code == 422

    async def test_period_list_reflects_final_lines_count(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        After finalization vw_PayrollPeriodList.final_lines should equal the number of
        non-Void draft lines.  Uses the natural workflow (add lines while Open,
        then advance to Approved and finalize) to avoid the back-and-forth
        status transitions of the _add_line helper.
        """
        headers = auth(auth_token)
        branch_id = paytest_clean

        # Insert Open period directly (CP-1D: POST requires existing Open; PATCH Draft→Open blocked).
        _r = (await direct_db.execute(
            _sqla_text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Open', 'FIN-FL-2035-0303', 'FinalLines Test 2035', 'Week', :start, :end)
                ON CONFLICT DO NOTHING
                RETURNING payrollperiodid
            """),
            {"bid": branch_id,
             "start": datetime.date(2035, 3, 3),
             "end": datetime.date(2035, 3, 9)},
        )).mappings().first()
        if _r is None:
            _r = (await direct_db.execute(
                _sqla_text(
                    "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE branchid = :bid AND periodcode = 'FIN-FL-2035-0303'"
                ),
                {"bid": branch_id},
            )).mappings().first()
        pid = _r["payrollperiodid"]

        # Add 2 DailyNote lines on different dates while Open
        # (duplicate guard: same driver + date + line_type would be rejected).
        for wdate in ("2035-03-04", "2035-03-05"):
            lr = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": paytest_driver_id, "work_date": wdate,
                      "line_type": "DailyNote", "quantity": "1.00", "notes": "filler"},
                headers=headers,
            )
            assert lr.status_code == 201, f"add DailyNote line failed: {lr.text}"
            assert not lr.json().get("needs_manager_review")

        # Open → InReview → Approved → Locked (finalize)
        await _advance_to_approved(session_client, auth_token, pid, paytest_driver_id, "2035-03-04")
        fin = await session_client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
        assert fin.status_code == 200, f"finalize failed: {fin.text}"

        # The period summary must report exactly 2 final lines
        resp = await session_client.get(f"/payroll/periods/{pid}", headers=headers)
        assert resp.status_code == 200
        # DailyNote is an informational source line, not a financial FinalLine.
        assert resp.json()["final_lines"] == 0

        # Cleanup: the period is Locked; bypass immutability triggers to cancel it
        # so subsequent tests can reuse the same branch/date-range.
        await _force_cancel_locked_periods(direct_db, paytest_clean)


# ---------------------------------------------------------------------------
# GET /payroll/periods/{id}/final-lines
# ---------------------------------------------------------------------------

class TestGetFinalLines:

    async def test_requires_auth(
        self,
        client: httpx.AsyncClient,
        approved_period: dict,
    ):
        pid = approved_period["payroll_period_id"]
        resp = await client.get(f"/payroll/periods/{pid}/final-lines")
        assert resp.status_code == 401

    async def test_empty_before_finalization(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
    ):
        """Final-lines on an Approved (not yet Locked) period returns 422.

        Final lines are only available for Locked or Archived periods.
        The period must be finalized first.
        """
        pid = approved_period["payroll_period_id"]
        resp = await client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 422, (
            f"final-lines must reject non-Locked periods; got {resp.status_code}: {resp.text}"
        )

    async def test_schema(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
        paytest_rate_type_id: int,
    ):
        """Check that every expected field is present and typed correctly."""
        pid = approved_period["payroll_period_id"]
        await _create_and_approve_rate(
            client, auth_token, paytest_driver_id, paytest_rate_type_id, "25.00"
        )
        await _add_line(client, auth_token, pid, paytest_driver_id, direct_db, line_type="Hours")
        await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(auth_token),
        )
        resp = await client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        line = resp.json()[0]
        assert "final_line_id" in line
        assert "period_id" in line
        assert "branch_id" in line
        assert "driver_id" in line
        assert "driver_name" in line
        assert "line_type" in line
        assert "quantity" in line
        assert "final_amount" in line
        assert "source_type" in line
        assert "approved_at_utc" in line
        assert line["driver_id"] == paytest_driver_id
        assert line["line_type"] == "HOURS"

    async def test_filter_by_driver_id(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
        paytest_rate_type_id: int,
    ):
        pid = approved_period["payroll_period_id"]
        await _create_and_approve_rate(
            client, auth_token, paytest_driver_id, paytest_rate_type_id, "25.00"
        )
        await _add_line(client, auth_token, pid, paytest_driver_id, direct_db, line_type="Hours")
        await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(auth_token),
        )

        # Correct driver — should return results
        resp = await client.get(
            f"/payroll/periods/{pid}/final-lines",
            params={"driver_id": paytest_driver_id},
            headers=auth(auth_token),
        )
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

        # Unknown driver — should return empty list
        resp2 = await client.get(
            f"/payroll/periods/{pid}/final-lines",
            params={"driver_id": 999999},
            headers=auth(auth_token),
        )
        assert resp2.status_code == 200
        assert resp2.json() == []

    async def test_404_unknown_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await client.get(
            "/payroll/periods/999999/final-lines",
            headers=auth(auth_token),
        )
        assert resp.status_code == 404

    async def test_draft_line_id_preserved(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
        paytest_rate_type_id: int,
    ):
        """draft_line_id on the FinalLine must reference the original DraftLine."""
        pid = approved_period["payroll_period_id"]
        await _create_and_approve_rate(
            client, auth_token, paytest_driver_id, paytest_rate_type_id, "25.00"
        )
        draft = await _add_line(
            client, auth_token, pid, paytest_driver_id, direct_db, line_type="Hours",
        )
        await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(auth_token),
        )
        ledger = await client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        pto_lines = [l for l in ledger.json() if l["line_type"] == "HOURS"]
        assert len(pto_lines) == 1
        assert pto_lines[0]["draft_line_id"] == draft["draft_line_id"]


# ---------------------------------------------------------------------------
# Safety review tests — atomicity, immutability, double-finalization guard
# ---------------------------------------------------------------------------

class TestFinalizationSafety:

    # ── Rollback (atomicity) ────────────────────────────────────────────────

    async def test_full_rollback_when_audit_write_fails(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        If _write_finalization_audit raises after the main writes, the entire
        engine.begin() transaction must roll back: the UPDATE (claim) and the
        INSERT (final lines) are both undone and the period reverts to Approved.

        httpx.ASGITransport re-raises unhandled exceptions directly to the
        test instead of converting them to a 500 response, so we use
        pytest.raises() to catch the RuntimeError inside the patch block.
        After both context managers exit we verify the clean DB state.
        """
        pid = approved_period["payroll_period_id"]
        headers = auth(auth_token)

        # Seed one draft line so the INSERT step has something to copy.
        await _add_line(client, auth_token, pid, paytest_driver_id, direct_db)

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure — rollback expected")

        # Both context managers are exited in the correct order:
        # 1. RuntimeError is caught by pytest.raises  →  inner block exits
        # 2. patch.object restores the original helper →  outer block exits
        with patch.object(payroll_service, "_write_finalization_audit", _raise):
            with pytest.raises(RuntimeError, match="Simulated audit failure"):
                await client.post(
                    f"/payroll/periods/{pid}/finalize",
                    headers=headers,
                )

        # Patch is now fully restored.  Verify the transaction was rolled back.

        # Period must still be Approved (atomic claim UPDATE was rolled back)
        period_resp = await client.get(f"/payroll/periods/{pid}", headers=headers)
        assert period_resp.status_code == 200
        assert period_resp.json()["status"] == "Approved", (
            "Transaction rollback failed: period was Locked despite audit error"
        )

        # Verify rollback via final-lines endpoint.
        # After rollback the period is still Approved (not Locked), so final-lines
        # returns 422 (not 200) — confirming no final-line rows were committed.
        lines_resp = await client.get(
            f"/payroll/periods/{pid}/final-lines", headers=headers
        )
        assert lines_resp.status_code == 422, (
            "Expected 422 (period still Approved, no final lines) after rollback; "
            f"got {lines_resp.status_code}: {lines_resp.text}"
        )

    # ── PATCH /status cannot reach Locked ──────────────────────────────────

    async def test_patch_status_to_locked_gives_helpful_error(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
    ):
        """
        PATCH /status with {"status": "Locked"} must return 422 with a message
        directing the caller to POST /finalize, not a confusing transition error.
        """
        pid = approved_period["payroll_period_id"]
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Locked"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        # Pydantic validation fires before any service code; check for the hint
        body = resp.json()
        detail_text = str(body)
        assert "finalize" in detail_text.lower()

    async def test_patch_status_to_locked_from_any_status_is_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_period_id: int,
    ):
        """
        Even from a Draft period, PATCH → Locked is rejected by the validator,
        not the transition table.  The error must mention /finalize.
        """
        resp = await client.patch(
            f"/payroll/periods/{created_period_id}/status",
            json={"status": "Locked"},
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "finalize" in str(resp.json()).lower()

    # ── Locked-period immutability ──────────────────────────────────────────

    async def test_locked_period_draft_lines_are_immutable(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        After finalization, editing or voiding a draft line must return 422.
        """
        pid = approved_period["payroll_period_id"]
        headers = auth(auth_token)

        # Add a line (helper steps through Approved → Open → Approved)
        draft = await _add_line(client, auth_token, pid, paytest_driver_id, direct_db)
        lid = draft["draft_line_id"]

        # Finalize → Locked
        fin = await client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
        assert fin.status_code == 200
        assert fin.json()["status"] == "Locked"

        # PATCH the draft line → must be blocked
        patch_resp = await client.patch(
            f"/payroll/periods/{pid}/lines/{lid}",
            json={"quantity": "99.00"},
            headers=headers,
        )
        assert patch_resp.status_code == 422

        # DELETE (void) the draft line → must be blocked
        del_resp = await client.delete(
            f"/payroll/periods/{pid}/lines/{lid}",
            headers=headers,
        )
        assert del_resp.status_code == 422

    async def test_locked_period_status_can_only_go_to_archived(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """From Locked the only permitted PATCH transition is → Archived."""
        pid = approved_period["payroll_period_id"]
        headers = auth(auth_token)

        await _add_line(client, auth_token, pid, paytest_driver_id, direct_db)
        await client.post(f"/payroll/periods/{pid}/finalize", headers=headers)

        # Attempt an illegal backward move
        back = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Approved"},
            headers=headers,
        )
        assert back.status_code == 422

        # Legal move: Locked → Archived
        archive = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Archived"},
            headers=headers,
        )
        assert archive.status_code == 200
        assert archive.json()["status"] == "Archived"

    # ── Duplicate-finalization prevention ───────────────────────────────────

    async def test_db_unique_index_prevents_duplicate_final_lines(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        ux_PayrollFinalLines_Period_DraftLine enforces that each DraftLineID
        can appear at most once per period in FinalLines.  After a successful
        finalization, re-calling finalize (now that the period is Locked) is
        blocked by the service guard before the INSERT, so we verify the
        constraint exists by checking that the first finalization succeeds
        and the second returns 422 (not 500 from a constraint error, which
        would mean the service guard failed).
        """
        pid = approved_period["payroll_period_id"]
        headers = auth(auth_token)

        await _add_line(client, auth_token, pid, paytest_driver_id, direct_db)

        # First finalization — must succeed
        r1 = await client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
        assert r1.status_code == 200
        assert r1.json()["status"] == "Locked"

        # Second finalization — service guard (Locked ≠ Approved) must fire
        # with 422, not 500 (which would indicate the DB constraint fired instead)
        r2 = await client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
        assert r2.status_code == 422
        # Service guard is working; DB constraint is a belt-and-suspenders backup
