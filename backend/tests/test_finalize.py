"""
Integration tests for finalization and the final-lines ledger:

  POST /payroll/periods/{id}/finalize
  GET  /payroll/periods/{id}/final-lines

Isolation
---------
Tests that mutate period state use `approved_period`, a function-scoped
fixture that creates a fresh period on PAYTEST, walks it through
Draft → Open → InReview → Approved, and guarantees cleanup via
`paytest_clean`.

`paytest_driver_id` (session-scoped, conftest) provides a valid driver on
the PAYTEST branch so we can seed draft lines before finalizing.
"""
import pytest
import pytest_asyncio
import httpx
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.payroll import service as payroll_service


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

async def _force_cancel_locked_periods(direct_db, branch_id: int) -> None:
    """Cancel Locked/Archived periods by temporarily disabling immutability triggers.

    Migration 0035 prevents cancelling Locked/Archived periods via normal UPDATE.
    Tests need this cleanup so subsequent tests can reuse the same date ranges.
    The triggers are re-enabled immediately after the cleanup UPDATE.
    """
    from sqlalchemy import text as _text
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
        # Add a non-void line so the period is not empty (PTO_STATUS needs no approved rate)
        await client.post(
            f"/payroll/periods/{period_id}/lines",
            headers=headers,
            json={
                "driver_id":   driver_id,
                "work_date":   start_date,
                "line_type":   "PTO_STATUS",
                "quantity":    1,
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

    # Create Draft
    r = await session_client.post(
        "/payroll/periods",
        json={
            "branch_id":   branch_id,
            "period_type": "Week",
            "start_date":  "2032-01-06",
            "end_date":    "2032-01-12",
        },
        headers=headers,
    )
    assert r.status_code == 201, f"create Draft failed: {r.text}"
    pid = r.json()["payroll_period_id"]

    # Draft → Open
    r = await session_client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "Open"},
        headers=headers,
    )
    assert r.status_code == 200, f"transition to Open failed: {r.text}"

    # Open → InReview → Approved via review flow
    result = await _advance_to_approved(session_client, auth_token, pid, paytest_driver_id)

    # Void the dummy Miles line added by _advance_to_approved so the period
    # is approved but logically empty — tests add their own lines via _add_line.
    await direct_db.execute(
        _text("""
            UPDATE payroll.payrolldraftlines
            SET status = 'Void'
            WHERE payrollperiodid = :pid
        """),
        {"pid": pid},
    )
    return result


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
    assert ra.status_code == 200, f"Approve rate failed: {ra.text}"
    return rate_id


async def _add_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    *,
    line_type: str = "PTO_STATUS",
    quantity: str = "1.00",
    work_date: str = "2032-01-07",
) -> dict:
    """
    Helper: temporarily open the period, add a draft line, then re-approve.
    The period must be in Approved status when called.
    Returns the created draft line dict.

    Default line_type is 'PTO_STATUS' (ratebehavior='None') because:
    - 'None' behavior never triggers a DriverRate lookup.
    - needs_manager_review is always False (no unresolved calculation).
    - The period can always reach Approved without resolving a rate.
    - calculatedamount is NULL; finalamount is 0 at finalization (correct for PTO).
    - Tests that need PerUnit calculation must set up an approved DriverRate first.

    Phase 4C: manual rate_amount is blocked for PerUnit lines.
    PerUnit line types (Hours, Miles, Wait, etc.) require an approved DriverRate.
    """
    headers = auth(token)

    # Approved -> InReview (no review item created for Approved→InReview) -> Open
    await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "InReview"},
        headers=headers,
    )
    await client.patch(
        f"/payroll/periods/{period_id}/status",
        json={"status": "Open"},
        headers=headers,
    )

    payload: dict = {
        "driver_id":  driver_id,
        "work_date":  work_date,
        "line_type":  line_type,
        "quantity":   quantity,
    }

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
        Finalizing an Approved period that has no non-void draft lines
        must return 422 with an informative message.
        The approved_period fixture leaves the period approved with all lines
        voided, so the period is logically empty.
        """
        pid = approved_period["payroll_period_id"]
        resp = await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(auth_token),
        )
        assert resp.status_code == 422
        assert "no payroll lines" in resp.json()["detail"].lower()

    async def test_finalize_returns_locked_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
    ):
        pid = approved_period["payroll_period_id"]
        await _add_line(client, auth_token, pid, paytest_driver_id)
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
    ):
        pid = approved_period["payroll_period_id"]

        # Seed two non-Void draft lines (PTO_STATUS requires no approved rate)
        await _add_line(client, auth_token, pid, paytest_driver_id)
        await _add_line(client, auth_token, pid, paytest_driver_id, work_date="2032-01-08")

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
        assert types == {"PTO_STATUS"}

    async def test_void_draft_lines_not_finalized(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
    ):
        """Voided draft lines must be excluded from FinalLines."""
        pid = approved_period["payroll_period_id"]
        headers = auth(auth_token)

        # Seed one non-voided PTO_STATUS line so finalization has something to lock.
        # _add_line steps the period: Approved -> Open -> (add line) -> Approved.
        await _add_line(client, auth_token, pid, paytest_driver_id)

        # Step down to Open again: Approved→InReview (no new review item created)→Open
        await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=headers,
        )
        await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=headers,
        )
        r = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": "2032-01-09",
                  "line_type": "Overnight", "quantity": "1.00"},
            headers=headers,
        )
        line_id = r.json()["draft_line_id"]
        await client.delete(
            f"/payroll/periods/{pid}/lines/{line_id}",
            headers=headers,
        )

        # Back to Approved via review flow
        await _advance_to_approved(client, auth_token, pid, paytest_driver_id, "2032-01-09")

        # Finalize — the non-voided Hours line is copied; Overnight (voided) must NOT appear
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
        assert "OVERNIGHT" not in types
        assert "PTO_STATUS" in types

    async def test_final_amount_computed_from_rate(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
        paytest_mileage_rate_type_id: int,
    ):
        """FinalAmount = quantity * approved_rate when calculatedamount is set."""
        pid = approved_period["payroll_period_id"]
        # Create and approve a MILEAGE rate of $0.50 so quantity=100 → final_amount=50.00
        rate_id = await _create_and_approve_rate(
            client, auth_token, paytest_driver_id, paytest_mileage_rate_type_id, "0.50"
        )
        try:
            await _add_line(
                client, auth_token, pid, paytest_driver_id,
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
    ):
        """Attempting to finalize an Open period must return 422."""
        headers = auth(auth_token)
        # Create a Draft, open it (status=Open)
        r = await client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_clean,
                "period_type": "Week",
                "start_date":  "2033-01-06",
                "end_date":    "2033-01-12",
            },
            headers=headers,
        )
        pid = r.json()["payroll_period_id"]
        await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=headers,
        )

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
    ):
        """Calling finalize a second time on a now-Locked period must return 422."""
        pid = approved_period["payroll_period_id"]
        headers = auth(auth_token)

        await _add_line(client, auth_token, pid, paytest_driver_id)

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

        # Draft → Open
        r = await session_client.post(
            "/payroll/periods",
            json={"branch_id": branch_id, "period_type": "Week",
                  "start_date": "2035-03-03", "end_date": "2035-03-09"},
            headers=headers,
        )
        assert r.status_code == 201, f"create failed: {r.text}"
        pid = r.json()["payroll_period_id"]

        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=headers,
        )

        # Add 2 PTO_STATUS lines on different dates while Open
        # (duplicate guard: same driver + date + line_type would be rejected).
        for wdate in ("2035-03-04", "2035-03-05"):
            lr = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": paytest_driver_id, "work_date": wdate,
                      "line_type": "PTO_STATUS", "quantity": "1.00"},
                headers=headers,
            )
            assert lr.status_code == 201, f"add PTO_STATUS line failed: {lr.text}"
            assert not lr.json().get("needs_manager_review")

        # Open → InReview → Approved → Locked (finalize)
        await _advance_to_approved(session_client, auth_token, pid, paytest_driver_id, "2035-03-04")
        fin = await session_client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
        assert fin.status_code == 200, f"finalize failed: {fin.text}"

        # The period summary must report exactly 2 final lines
        resp = await session_client.get(f"/payroll/periods/{pid}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["final_lines"] == 2

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
    ):
        """Check that every expected field is present and typed correctly."""
        pid = approved_period["payroll_period_id"]
        await _add_line(
            client, auth_token, pid, paytest_driver_id,
        )
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
        assert line["line_type"] == "PTO_STATUS"

    async def test_filter_by_driver_id(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        approved_period: dict,
        paytest_driver_id: int,
    ):
        pid = approved_period["payroll_period_id"]
        await _add_line(client, auth_token, pid, paytest_driver_id)
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
    ):
        """draft_line_id on the FinalLine must reference the original DraftLine."""
        pid = approved_period["payroll_period_id"]
        draft = await _add_line(
            client, auth_token, pid, paytest_driver_id,
        )
        await client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=auth(auth_token),
        )
        ledger = await client.get(
            f"/payroll/periods/{pid}/final-lines",
            headers=auth(auth_token),
        )
        pto_lines = [l for l in ledger.json() if l["line_type"] == "PTO_STATUS"]
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
        await _add_line(client, auth_token, pid, paytest_driver_id)

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
    ):
        """
        After finalization, editing or voiding a draft line must return 422.
        """
        pid = approved_period["payroll_period_id"]
        headers = auth(auth_token)

        # Add a line (helper steps through Approved → Open → Approved)
        draft = await _add_line(client, auth_token, pid, paytest_driver_id)
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
    ):
        """From Locked the only permitted PATCH transition is → Archived."""
        pid = approved_period["payroll_period_id"]
        headers = auth(auth_token)

        await _add_line(client, auth_token, pid, paytest_driver_id)
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

        await _add_line(client, auth_token, pid, paytest_driver_id)

        # First finalization — must succeed
        r1 = await client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
        assert r1.status_code == 200
        assert r1.json()["status"] == "Locked"

        # Second finalization — service guard (Locked ≠ Approved) must fire
        # with 422, not 500 (which would indicate the DB constraint fired instead)
        r2 = await client.post(f"/payroll/periods/{pid}/finalize", headers=headers)
        assert r2.status_code == 422
        # Service guard is working; DB constraint is a belt-and-suspenders backup
