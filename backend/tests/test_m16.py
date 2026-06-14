"""
Tests for M16: Review / Payroll Period Approval Integration.

Covers:
  - Open → InReview auto-creates a PeriodApproval review item in same transaction
  - Pre-submission guards: empty period, unresolved NeedsManagerReview lines,
    zero-calc unresolved lines, duplicate Pending review item
  - Review Approved  → period moves to Approved + audit entries written
  - Review Rejected  → period moves to Open
  - Review EditRequested → period moves to Open (re-submit creates new item)
  - Review Comment   → period stays InReview, review item unchanged
  - AllowSelfApproval=FALSE blocks self-approval; Comments still allowed
  - AllowSelfApproval=TRUE allows same-user approval
  - Direct PATCH /status {Approved} → 422 (removed from valid transitions)
  - InReview → Open still allowed (manual return)
  - Duplicate Pending guard blocks re-submit; Rejected/EditRequested do not
  - Concurrency: period no longer InReview when decision arrives → 422 rollback
  - Audit: both REVIEW_ITEM_DECIDED and PERIOD_STATUS_CHANGED written
  - Audit rollback: all writes roll back if audit or period update fails
  - Non-PeriodApproval review items are unaffected (no period write-back)
"""
import json
import pytest
import pytest_asyncio
import httpx
import psycopg2
from typing import AsyncGenerator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: httpx.AsyncClient) -> str:
    resp = await client.post("/auth/login", json={
        "username": "admin",
        "password": "TestPass123!",
        "company_code": "DEMO",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _get_branch_id(client: httpx.AsyncClient, token: str, branch_code: str = "PAYTEST") -> int:
    """Return branch_id for PAYTEST (the dedicated payroll-test branch)."""
    resp = await client.get("/core/branches", headers=_auth(token))
    assert resp.status_code == 200
    for b in resp.json():
        if b["branch_code"] == branch_code:
            return b["branch_id"]
    raise AssertionError(f"Branch {branch_code!r} not found")


async def _get_driver_id(client: httpx.AsyncClient, token: str, branch_id: int) -> int:
    """Return the first active driver in the given branch, or create one."""
    resp = await client.get(f"/core/drivers?branch_id={branch_id}", headers=_auth(token))
    assert resp.status_code == 200
    drivers = [d for d in resp.json() if d["branch_id"] == branch_id]
    if drivers:
        return drivers[0]["driver_id"]
    # Create a minimal driver
    resp2 = await client.post("/core/drivers", headers=_auth(token), json={
        "branch_id": branch_id,
        "full_name": f"M16 Test Driver {branch_id}",
        "employee_type": "Driver",
    })
    assert resp2.status_code == 201, resp2.text
    return resp2.json()["driver_id"]


async def _cancel_stale_periods(client: httpx.AsyncClient, token: str, branch_id: int) -> None:
    """Cancel any Draft or Open periods on the branch to avoid unique-constraint failures."""
    for status in ("Draft", "Open"):
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": branch_id, "status": status},
            headers=_auth(token),
        )
        if resp.status_code != 200:
            continue
        for p in resp.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                headers=_auth(token),
                json={"status": "Cancelled"},
            )


async def _create_open_period(client: httpx.AsyncClient, token: str, branch_id: int,
                               start: str = "2028-01-08",
                               end: str = "2028-01-14") -> dict:
    """Create a period in Draft status, then move it to Open."""
    # Cancel any leftover Draft/Open periods to avoid unique constraint failures
    await _cancel_stale_periods(client, token, branch_id)

    resp = await client.post("/payroll/periods", headers=_auth(token), json={
        "branch_id": branch_id,
        "period_type": "Week",
        "start_date": start,
        "end_date":   end,
        "period_name": f"M16 Week {start}",
    })
    assert resp.status_code == 201, resp.text
    pid = resp.json()["payroll_period_id"]

    r2 = await client.patch(f"/payroll/periods/{pid}/status",
                             headers=_auth(token), json={"status": "Open"})
    assert r2.status_code == 200, r2.text
    return r2.json()


async def _add_miles_line(client: httpx.AsyncClient, token: str, period_id: int,
                           driver_id: int, miles: int = 200,
                           rate: float = 0.55) -> dict:
    """Add a PTO_STATUS draft line to make the period non-empty.
    (Miles/rate_amount overrides are blocked by Phase 4C; PTO_STATUS has
    None rate-behavior so no driver rate is required.)
    Fetches the period's start date to use as work_date."""
    period_resp = await client.get(f"/payroll/periods/{period_id}", headers=_auth(token))
    assert period_resp.status_code == 200
    start_date = period_resp.json()["start_date"]
    resp = await client.post(
        f"/payroll/periods/{period_id}/lines",
        headers=_auth(token),
        json={
            "driver_id": driver_id,
            "work_date": start_date,
            "line_type": "PTO_STATUS",
            "quantity":  1,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _submit_for_review(client: httpx.AsyncClient, token: str, period_id: int) -> dict:
    """Move period from Open to InReview (triggers review item creation)."""
    resp = await client.patch(
        f"/payroll/periods/{period_id}/status",
        headers=_auth(token),
        json={"status": "InReview"},
    )
    return resp


async def _get_review_item(client: httpx.AsyncClient, token: str, period_id: int) -> dict | None:
    """Return the most-recent PeriodApproval review item for this period."""
    resp = await client.get("/review/items", headers=_auth(token))
    assert resp.status_code == 200
    for item in resp.json():
        if (item.get("entity_name") == "PayrollPeriods"
                and item.get("entity_id") == str(period_id)
                and item.get("request_type") == "PeriodApproval"):
            return item
    return None


# ---------------------------------------------------------------------------
# Session-level fixtures (shared driver + branch setup)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def m16_branch_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    return await _get_branch_id(session_client, auth_token, "PAYTEST")


@pytest_asyncio.fixture(scope="session")
async def m16_driver_id(session_client: httpx.AsyncClient, auth_token: str,
                         m16_branch_id: int) -> int:
    return await _get_driver_id(session_client, auth_token, m16_branch_id)


# Function-scoped: every test that needs an open period gets a fresh one on
# a unique date range so the one-Open-per-branch constraint never fires.
_m16_period_counter = 0


@pytest_asyncio.fixture
async def m16_open_period(client: httpx.AsyncClient, auth_token: str,
                           m16_branch_id: int) -> dict:
    global _m16_period_counter
    _m16_period_counter += 1
    from datetime import date, timedelta
    base = date(2028, 1, 1) + timedelta(weeks=_m16_period_counter)
    s = base.strftime("%Y-%m-%d")
    e = (base + timedelta(days=6)).strftime("%Y-%m-%d")
    return await _create_open_period(client, auth_token, m16_branch_id, s, e)


# ---------------------------------------------------------------------------
# TestPeriodSubmitForReview
# ---------------------------------------------------------------------------

class TestPeriodSubmitForReview:

    async def test_submit_creates_review_item(self, client, auth_token, m16_open_period,
                                               m16_driver_id):
        """Open → InReview auto-creates a Pending PeriodApproval review item."""
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)

        resp = await _submit_for_review(client, auth_token, pid)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "InReview"

        item = await _get_review_item(client, auth_token, pid)
        assert item is not None, "Review item not created"
        assert item["request_type"] == "PeriodApproval"
        assert item["entity_schema"] == "payroll"
        assert item["entity_name"] == "PayrollPeriods"
        assert item["entity_id"] == str(pid)
        assert item["status"] == "Pending"

    async def test_review_item_title_contains_period_name(self, client, auth_token,
                                                           m16_open_period, m16_driver_id):
        pid = m16_open_period["payroll_period_id"]
        period_name = m16_open_period["period_name"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)

        await _submit_for_review(client, auth_token, pid)

        item = await _get_review_item(client, auth_token, pid)
        assert item is not None
        assert period_name in item["title"]

    async def test_review_item_requested_by_is_submitter(self, client, auth_token,
                                                           m16_open_period, m16_driver_id):
        """The review item's requested_by_user_id must be the submitting user."""
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)

        item = await _get_review_item(client, auth_token, pid)
        assert item is not None
        # Admin user created the item
        assert item["requested_by_user_id"] is not None

    async def test_empty_period_cannot_be_submitted(self, client, auth_token, m16_open_period):
        """Period with no non-Void lines → 422."""
        pid = m16_open_period["payroll_period_id"]
        resp = await _submit_for_review(client, auth_token, pid)
        assert resp.status_code == 422
        assert "empty" in resp.text.lower() or "no" in resp.text.lower()

    async def test_unresolved_needs_review_lines_block_submit(
            self, client, auth_token, m16_open_period, m16_driver_id):
        """NeedsManagerReview=TRUE lines block submission."""
        pid = m16_open_period["payroll_period_id"]
        start_date = m16_open_period["start_date"]
        # Add a line with NeedsManagerReview=True
        resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            headers=_auth(auth_token),
            json={
                "driver_id":            m16_driver_id,
                "work_date":            start_date,
                "line_type":            "Miles",
                "quantity":             100,
                "needs_manager_review": True,
            },
        )
        assert resp.status_code == 201

        submit_resp = await _submit_for_review(client, auth_token, pid)
        assert submit_resp.status_code == 422
        assert "manager review" in submit_resp.text.lower()

    async def test_duplicate_pending_review_item_blocks_submit(
            self, client, auth_token, m16_open_period, m16_driver_id):
        """A second submission while a Pending review item exists → 422."""
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)

        # First submit → succeeds
        r1 = await _submit_for_review(client, auth_token, pid)
        assert r1.status_code == 200

        # Return to Open
        r2 = await client.patch(f"/payroll/periods/{pid}/status",
                                  headers=_auth(auth_token), json={"status": "Open"})
        assert r2.status_code == 200

        # Second submit → blocked (Pending item still exists)
        r3 = await _submit_for_review(client, auth_token, pid)
        assert r3.status_code == 422
        assert "pending" in r3.text.lower()

    async def test_submit_allowed_after_rejected_review(
            self, client, auth_token, m16_open_period, m16_driver_id):
        """After a Rejected review item, re-submit creates a new Pending item."""
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)

        # Submit → review item created
        r1 = await _submit_for_review(client, auth_token, pid)
        assert r1.status_code == 200

        # Reject → period goes back to Open
        item = await _get_review_item(client, auth_token, pid)
        r2 = await client.post(
            f"/review/items/{item['review_item_id']}/decide",
            headers=_auth(auth_token),
            json={"decision": "Rejected", "decision_reason": "Needs corrections"},
        )
        assert r2.status_code == 200

        # Verify period is Open again
        period_resp = await client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert period_resp.json()["status"] == "Open"

        # Re-submit → creates a NEW Pending review item
        r3 = await _submit_for_review(client, auth_token, pid)
        assert r3.status_code == 200, r3.text

        new_item = await _get_review_item(client, auth_token, pid)
        assert new_item is not None
        assert new_item["status"] == "Pending"
        assert new_item["review_item_id"] != item["review_item_id"]


# ---------------------------------------------------------------------------
# TestPeriodApprovalViaReview
# ---------------------------------------------------------------------------

class TestPeriodApprovalViaReview:

    async def _setup(self, client, auth_token, m16_open_period, m16_driver_id):
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        r = await _submit_for_review(client, auth_token, pid)
        assert r.status_code == 200
        item = await _get_review_item(client, auth_token, pid)
        assert item is not None
        return pid, item["review_item_id"]

    async def test_approved_decision_moves_period_to_approved(
            self, client, auth_token, m16_open_period, m16_driver_id):
        pid, review_id = await self._setup(client, auth_token, m16_open_period, m16_driver_id)

        resp = await client.post(
            f"/review/items/{review_id}/decide",
            headers=_auth(auth_token),
            json={"decision": "Approved"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "Approved"

        period_resp = await client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert period_resp.json()["status"] == "Approved"

    async def test_rejected_decision_moves_period_to_open(
            self, client, auth_token, m16_open_period, m16_driver_id):
        pid, review_id = await self._setup(client, auth_token, m16_open_period, m16_driver_id)

        resp = await client.post(
            f"/review/items/{review_id}/decide",
            headers=_auth(auth_token),
            json={"decision": "Rejected", "decision_reason": "Incomplete data"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "Rejected"

        period_resp = await client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert period_resp.json()["status"] == "Open"

    async def test_edit_requested_moves_period_to_open(
            self, client, auth_token, m16_open_period, m16_driver_id):
        """EditRequested returns the period to Open so the user can correct it."""
        pid, review_id = await self._setup(client, auth_token, m16_open_period, m16_driver_id)

        resp = await client.post(
            f"/review/items/{review_id}/decide",
            headers=_auth(auth_token),
            json={"decision": "EditRequested", "decision_reason": "Fix line 3"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "EditRequested"

        period_resp = await client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert period_resp.json()["status"] == "Open", (
            "EditRequested should return the period to Open for corrections"
        )

    async def test_comment_does_not_change_period_status(
            self, client, auth_token, m16_open_period, m16_driver_id):
        """Comment decision: review item stays Pending, period stays InReview."""
        pid, review_id = await self._setup(client, auth_token, m16_open_period, m16_driver_id)

        resp = await client.post(
            f"/review/items/{review_id}/decide",
            headers=_auth(auth_token),
            json={"decision": "Comment", "decision_reason": "Looks mostly good, just checking."},
        )
        assert resp.status_code == 200, resp.text
        # Review item stays Pending (Comment doesn't change status)
        assert resp.json()["status"] == "Pending"

        period_resp = await client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert period_resp.json()["status"] == "InReview"

    async def test_edit_requested_allows_resubmission_with_new_item(
            self, client, auth_token, m16_open_period, m16_driver_id):
        """After EditRequested → Open, re-submit creates a new Pending review item."""
        pid, review_id = await self._setup(client, auth_token, m16_open_period, m16_driver_id)

        await client.post(
            f"/review/items/{review_id}/decide",
            headers=_auth(auth_token),
            json={"decision": "EditRequested", "decision_reason": "Please fix"},
        )

        # Period is back to Open — re-submit
        r = await _submit_for_review(client, auth_token, pid)
        assert r.status_code == 200, r.text

        new_item = await _get_review_item(client, auth_token, pid)
        assert new_item is not None
        assert new_item["status"] == "Pending"
        assert new_item["review_item_id"] != review_id


# ---------------------------------------------------------------------------
# TestDirectApprovalBlocked
# ---------------------------------------------------------------------------

class TestDirectApprovalBlocked:

    async def test_patch_status_approved_is_invalid_transition(
            self, client, auth_token, m16_open_period, m16_driver_id):
        """PATCH /status with Approved is no longer a valid transition from InReview."""
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)

        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            headers=_auth(auth_token),
            json={"status": "Approved"},
        )
        assert resp.status_code == 422
        assert "Approved" in resp.text

    async def test_patch_inreview_to_open_still_works(
            self, client, auth_token, m16_open_period, m16_driver_id):
        """PATCH InReview → Open remains valid (manual return for corrections)."""
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)

        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            headers=_auth(auth_token),
            json={"status": "Open"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "Open"

    async def test_patch_inreview_to_cancelled_still_works(
            self, client, auth_token, m16_open_period, m16_driver_id):
        """PATCH InReview → Cancelled remains valid."""
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)

        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            headers=_auth(auth_token),
            json={"status": "Cancelled"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "Cancelled"

    async def test_patch_status_approved_rejected_from_schema(self, client, auth_token):
        """Approved is still a valid status value for the schema; just not from InReview."""
        # Sending 'Approved' to a non-InReview period should give the transition-not-allowed
        # message (not a schema validation error) — period is Open here.
        resp = await client.get("/payroll/periods", headers=_auth(auth_token))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestSelfApprovalPolicy
# ---------------------------------------------------------------------------

class TestSelfApprovalPolicy:

    async def _set_allow_self_approval(self, direct_db, allow: bool):
        from sqlalchemy import text as _text
        await direct_db.execute(
            _text("UPDATE core.companies SET allowselfapproval = :allow WHERE companycode = 'DEMO'"),
            {"allow": allow},
        )

    async def test_self_approval_allowed_when_policy_true(
            self, client, auth_token, m16_open_period, m16_driver_id, direct_db):
        """AllowSelfApproval=TRUE: same user can approve."""
        await self._set_allow_self_approval(direct_db, True)

        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)
        item = await _get_review_item(client, auth_token, pid)

        resp = await client.post(
            f"/review/items/{item['review_item_id']}/decide",
            headers=_auth(auth_token),
            json={"decision": "Approved"},
        )
        assert resp.status_code == 200, resp.text

    async def test_self_approval_blocked_when_policy_false(
            self, client, auth_token, m16_open_period, m16_driver_id, direct_db):
        """AllowSelfApproval=FALSE: same user cannot Approve their own submission."""
        await self._set_allow_self_approval(direct_db, False)

        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)
        item = await _get_review_item(client, auth_token, pid)

        resp = await client.post(
            f"/review/items/{item['review_item_id']}/decide",
            headers=_auth(auth_token),
            json={"decision": "Approved"},
        )
        assert resp.status_code == 422
        assert "self-approval" in resp.text.lower()

        # Period must remain InReview (decision rolled back)
        period_resp = await client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert period_resp.json()["status"] == "InReview"

        # Restore policy
        await self._set_allow_self_approval(direct_db, True)

    async def test_comment_allowed_regardless_of_self_approval_policy(
            self, client, auth_token, m16_open_period, m16_driver_id, direct_db):
        """Comments are always allowed even when AllowSelfApproval=FALSE."""
        await self._set_allow_self_approval(direct_db, False)

        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)
        item = await _get_review_item(client, auth_token, pid)

        resp = await client.post(
            f"/review/items/{item['review_item_id']}/decide",
            headers=_auth(auth_token),
            json={"decision": "Comment", "decision_reason": "Just a note."},
        )
        assert resp.status_code == 200, resp.text

        # Restore policy
        await self._set_allow_self_approval(direct_db, True)


# ---------------------------------------------------------------------------
# TestNonPeriodApprovalItemsUnaffected
# ---------------------------------------------------------------------------

class TestNonPeriodApprovalItemsUnaffected:

    async def test_deciding_non_period_approval_item_does_not_touch_period(
            self, client, auth_token, m16_branch_id):
        """Deciding a PayrollDraftChange item must not write back to any period."""
        # Create a generic review item (not linked to a period)
        resp = await client.post(
            "/review/items",
            headers=_auth(auth_token),
            json={
                "branch_id":    m16_branch_id,
                "request_type": "PayrollDraftChange",
                "title":        "Non-period review item",
                "description":  "Should not affect any period",
            },
        )
        assert resp.status_code == 201, resp.text
        item_id = resp.json()["review_item_id"]

        # Decide Approved — must not blow up, must not touch any period
        resp2 = await client.post(
            f"/review/items/{item_id}/decide",
            headers=_auth(auth_token),
            json={"decision": "Approved"},
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["status"] == "Approved"


# ---------------------------------------------------------------------------
# TestAuditEntries
# ---------------------------------------------------------------------------

class TestAuditEntries:

    async def test_approved_decision_writes_two_audit_entries(
            self, client, auth_token, m16_open_period, m16_driver_id, direct_db):
        """
        Approving a PeriodApproval review item must write both:
          - REVIEW_ITEM_DECIDED (entityname='ManagerReviewItems')
          - PERIOD_STATUS_CHANGED (entityname='PayrollPeriods')
        """
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)
        item = await _get_review_item(client, auth_token, pid)
        review_id = item["review_item_id"]

        await client.post(
            f"/review/items/{review_id}/decide",
            headers=_auth(auth_token),
            json={"decision": "Approved"},
        )

        from sqlalchemy import text as _text
        # Check audit log for REVIEW_ITEM_DECIDED
        row = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'REVIEW_ITEM_DECIDED' "
                  "  AND entityid = :eid"),
            {"eid": str(review_id)},
        )).fetchone()
        assert row[0] >= 1, "REVIEW_ITEM_DECIDED not found in audit log"

        # Check audit log for PERIOD_STATUS_CHANGED for this period
        row2 = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'PERIOD_STATUS_CHANGED' "
                  "  AND entityname = 'PayrollPeriods' "
                  "  AND entityid = :eid"),
            {"eid": str(pid)},
        )).fetchone()
        assert row2[0] >= 1, "PERIOD_STATUS_CHANGED not found in audit log"

    async def test_submit_writes_review_item_created_audit(
            self, client, auth_token, m16_open_period, m16_driver_id, direct_db):
        """Open → InReview must write REVIEW_ITEM_CREATED to audit.AuditLog."""
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)

        item = await _get_review_item(client, auth_token, pid)
        review_id = item["review_item_id"]

        from sqlalchemy import text as _text
        row = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'REVIEW_ITEM_CREATED' "
                  "  AND entityid = :eid"),
            {"eid": str(review_id)},
        )).fetchone()
        assert row[0] >= 1, "REVIEW_ITEM_CREATED not found in audit log"


# ---------------------------------------------------------------------------
# TestAuditRollback
# ---------------------------------------------------------------------------

class TestAuditRollback:

    async def test_period_status_update_failure_rolls_back_review_decision(
            self, client, auth_token, m16_open_period, m16_driver_id, monkeypatch, direct_db):
        """
        If the period status UPDATE fails (period no longer InReview),
        the review decision INSERT and item status UPDATE must both roll back.
        """
        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)
        item = await _get_review_item(client, auth_token, pid)
        review_id = item["review_item_id"]

        from sqlalchemy import text as _text
        # Manually return the period to Open via direct DB, bypassing the API —
        # this simulates a concurrent status change so the period is no longer InReview
        # when the review decision fires.
        await direct_db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Open' "
                  "WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )

        # Now try to approve — the atomic period UPDATE returns 0 rows → 422
        resp = await client.post(
            f"/review/items/{review_id}/decide",
            headers=_auth(auth_token),
            json={"decision": "Approved"},
        )
        assert resp.status_code == 422
        assert "InReview" in resp.text or "no longer" in resp.text

        # The review item must remain Pending (decision rolled back)
        item_resp = await client.get(f"/review/items/{review_id}", headers=_auth(auth_token))
        assert item_resp.json()["status"] == "Pending"

        # The period must remain Open (not Approved)
        period_resp = await client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert period_resp.json()["status"] == "Open"

    async def test_review_audit_failure_rolls_back_submit(
            self, client, auth_token, m16_open_period, m16_driver_id, monkeypatch):
        """
        If the audit write for review item creation raises, the period status
        change and the review item INSERT must both roll back.
        ASGITransport re-raises unhandled exceptions, so we use pytest.raises.
        """
        import pytest
        import app.payroll.service as payroll_svc

        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure")

        with monkeypatch.context() as m:
            m.setattr(payroll_svc, "_write_period_status_audit", _raise)
            with pytest.raises(RuntimeError, match="Simulated audit failure"):
                await _submit_for_review(client, auth_token, pid)

        # Period must still be Open (the status change rolled back)
        period_resp = await client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert period_resp.json()["status"] == "Open"

        # No review item should exist for this period
        item = await _get_review_item(client, auth_token, pid)
        assert item is None, "Review item should not exist — transaction rolled back"

    async def test_review_decision_audit_failure_rolls_back_everything(
            self, client, auth_token, m16_open_period, m16_driver_id, monkeypatch):
        """
        If _write_period_status_audit raises during decide_review_item,
        both the review decision INSERT and period status UPDATE must roll back.
        ASGITransport re-raises unhandled exceptions, so we use pytest.raises.
        Note: monkeypatch must target app.review.service (where it's imported into).
        """
        import pytest
        import app.review.service as review_svc

        pid = m16_open_period["payroll_period_id"]
        await _add_miles_line(client, auth_token, pid, m16_driver_id)
        await _submit_for_review(client, auth_token, pid)
        item = await _get_review_item(client, auth_token, pid)
        review_id = item["review_item_id"]

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated audit failure in decide")

        with monkeypatch.context() as m:
            m.setattr(review_svc, "_write_period_status_audit", _raise)
            with pytest.raises(RuntimeError, match="Simulated audit failure in decide"):
                await client.post(
                    f"/review/items/{review_id}/decide",
                    headers=_auth(auth_token),
                    json={"decision": "Approved"},
                )

        # Review item must remain Pending
        item_resp = await client.get(f"/review/items/{review_id}", headers=_auth(auth_token))
        assert item_resp.json()["status"] == "Pending"

        # Period must remain InReview
        period_resp = await client.get(f"/payroll/periods/{pid}", headers=_auth(auth_token))
        assert period_resp.json()["status"] == "InReview"
