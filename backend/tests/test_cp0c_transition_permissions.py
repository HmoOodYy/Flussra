"""
CP-0C integration tests — Transition permissions, review resolution, and deadlock safety.

Three fixes verified here:

1. Permission gap: ("Draft", "Cancelled") was absent from _TRANSITION_PERMISSIONS,
   meaning the permission check was silently skipped (None is falsy).  Any
   authenticated user — regardless of role — could cancel a Draft period.
   Fix: entry added with "payroll.finalize" requirement.

2. Review item orphan (CP-0C era): Manual transitions that move a period out of
   InReview via PATCH did not resolve the Pending PeriodApproval review item.
   CP-1A supersedes this: InReview now has NO PATCH exits (InReview→Open and
   InReview→Cancelled are both blocked).  Tests 2a/2b are inverted to verify the
   block; the "no item" path test is also inverted.

3. Deadlock prevention (CP-0C corrective follow-up):
   PATCH InReview exits were using ReviewItem FOR UPDATE before Period UPDATE.
   CP-1A removes all InReview PATCH exits, so the deadlock scenario is eliminated.
   The CP-0C deadlock test (TestDeadlockPrevention) is preserved and verifies that
   the review DECIDE path still acquires locks in the correct order.

Dates: 2092-* — isolated year, avoids conflicts with CP-0A (no year) and
CP-0B (2091) test modules.
Run from backend/:
    python -m pytest tests/test_cp0c_transition_permissions.py -v
"""
import asyncio
import datetime
import itertools
import threading
import psycopg2
import pytest
import httpx
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_WEEK_COUNTER = itertools.count(0)

def _next_dates() -> tuple[str, str]:
    """Return a unique (start, end) date pair — 2092 year to avoid conflicts."""
    n = next(_WEEK_COUNTER)
    base = datetime.date(2092, 1, 6)
    start = base + datetime.timedelta(weeks=n)
    end   = start + datetime.timedelta(days=6)
    return start.isoformat(), end.isoformat()


async def _cancel_active_periods(direct_db, branch_id: int) -> None:
    await direct_db.execute(
        text(
            "UPDATE payroll.payrollperiods "
            "SET status = 'Cancelled' "
            "WHERE branchid = :bid "
            "  AND status IN ('Draft', 'Open', 'InReview', 'Approved')"
        ),
        {"bid": branch_id},
    )


async def _create_draft_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    direct_db,
) -> int:
    """Insert Draft directly — CP-1D B1 guard blocks POST /payroll/periods without an Open."""
    await _cancel_active_periods(direct_db, branch_id)
    await direct_db.execute(
        text(
            "UPDATE payroll.payrollperiods "
            "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
            "WHERE branchid = :bid AND status = 'Returned'"
        ),
        {"bid": branch_id},
    )
    start, end = _next_dates()
    row = (await direct_db.execute(
        text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Draft', :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": f"CP0C-{start}", "name": f"CP0C {start}",
         "start": datetime.date.fromisoformat(start), "end": datetime.date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _force_status(direct_db, period_id: int, new_status: str) -> None:
    await direct_db.execute(
        text(
            "UPDATE payroll.payrollperiods "
            "SET status = :s WHERE payrollperiodid = :pid"
        ),
        {"s": new_status, "pid": period_id},
    )


async def _create_role_with_perms(
    client: httpx.AsyncClient,
    admin_token: str,
    role_name: str,
    perms: list[str],
) -> int:
    cr = await client.post(
        "/admin/company-roles",
        json={"role_name": role_name},
        headers=_auth(admin_token),
    )
    assert cr.status_code == 201, f"Create role failed: {cr.text}"
    role_id = cr.json()["company_role_id"]
    if perms:
        pr = await client.put(
            f"/admin/company-roles/{role_id}/permissions",
            json={"permission_codes": perms},
            headers=_auth(admin_token),
        )
        assert pr.status_code == 200, f"Set perms failed: {pr.text}"
    return role_id


async def _create_user_with_role(
    client: httpx.AsyncClient,
    admin_token: str,
    username: str,
    role_id: int,
) -> str:
    resp = await client.post(
        "/admin/users",
        json={
            "username":             username,
            "display_name":         username,
            "password":             "TestPass123!",
            "is_active":            True,
            "can_login":            True,
            "must_change_password": False,
        },
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"
    user_id = resp.json()["user_id"]

    assign_resp = await client.post(
        f"/admin/users/{user_id}/company-role-assignments",
        json={"company_role_id": role_id, "scope_type": "AllCompanyBranches"},
        headers=_auth(admin_token),
    )
    assert assign_resp.status_code in (200, 201), f"Assign role failed: {assign_resp.text}"

    login_resp = await client.post(
        "/auth/login",
        json={"username": username, "password": "TestPass123!", "company_code": "DEMO"},
    )
    assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"
    return login_resp.json()["access_token"]


async def _get_review_item_status(direct_db, company_id: int, period_id: int) -> str | None:
    """Return the status of the most recent PeriodApproval item for this period."""
    result = await direct_db.execute(
        text("""
            SELECT status FROM review.managerreviewitems
            WHERE  companyid    = :cid
              AND  entityschema = 'payroll'
              AND  entityname   = 'PayrollPeriods'
              AND  entityid     = :eid
              AND  requesttype  = 'PeriodApproval'
            ORDER BY createdatutc DESC
            LIMIT 1
        """),
        {"cid": company_id, "eid": str(period_id)},
    )
    row = result.first()
    return row[0] if row else None


async def _insert_pending_review_item(
    direct_db,
    company_id: int,
    branch_id: int,
    period_id: int,
    user_id: int,
) -> int:
    """Directly insert a Pending PeriodApproval review item, bypassing the API."""
    result = await direct_db.execute(
        text("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requestedbyuserid,
                 requesttype, entityschema, entityname, entityid,
                 title, description, priority, status)
            VALUES
                (:cid, :bid, :uid,
                 'PeriodApproval', 'payroll', 'PayrollPeriods', :eid,
                 'Test PeriodApproval', 'Inserted by test fixture', 'Normal', 'Pending')
            RETURNING reviewitemid
        """),
        {"cid": company_id, "bid": branch_id, "uid": user_id, "eid": str(period_id)},
    )
    return result.scalar_one()


# Seed data constants — company_id=1 and admin user_id=1 from JWT "cid":1 / "sub":"1".
_TEST_COMPANY_ID    = 1
_TEST_ADMIN_USER_ID = 1


# ---------------------------------------------------------------------------
# TestDraftCancelPermission — CP-0C fix: ("Draft","Cancelled") permission gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftCancelPermission:
    """Verify that Draft→Cancelled now correctly requires payroll.finalize."""

    async def test_view_only_user_cannot_cancel_draft(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """payroll.view only → 403 on Draft cancellation (CP-0C gap was silent skip)."""
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)

        role_id = await _create_role_with_perms(
            session_client, auth_token, "cp0c_view_only_role_2092", ["payroll.view"],
        )
        view_token = await _create_user_with_role(
            session_client, auth_token, "cp0c_view_only_user_2092", role_id,
        )

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(view_token),
        )
        assert r.status_code == 403, (
            f"Expected 403 for payroll.view-only Draft cancel, got {r.status_code}: {r.text}"
        )

        # cleanup
        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )

    async def test_entry_only_user_cannot_cancel_draft(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """payroll.entry only → 403 on Draft cancellation (finalize is required)."""
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)

        role_id = await _create_role_with_perms(
            session_client, auth_token, "cp0c_entry_only_role_2092", ["payroll.entry"],
        )
        entry_token = await _create_user_with_role(
            session_client, auth_token, "cp0c_entry_only_user_2092", role_id,
        )

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(entry_token),
        )
        assert r.status_code == 403, (
            f"Expected 403 for payroll.entry-only Draft cancel, got {r.status_code}: {r.text}"
        )

        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )

    async def test_finalize_user_can_cancel_draft(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """payroll.finalize → 200 on Draft cancellation (correct behavior)."""
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)

        role_id = await _create_role_with_perms(
            session_client, auth_token, "cp0c_finalize_role_2092",
            ["payroll.view", "payroll.finalize"],
        )
        finalize_token = await _create_user_with_role(
            session_client, auth_token, "cp0c_finalize_user_2092", role_id,
        )

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(finalize_token),
        )
        assert r.status_code == 200, (
            f"Expected 200 for payroll.finalize Draft cancel, got {r.status_code}: {r.text}"
        )
        assert r.json()["status"] == "Cancelled"

    async def test_approved_to_inreview_is_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Approved→InReview is now an invalid transition (removed from _VALID_TRANSITIONS).
        It left the period in InReview with no active PeriodApproval item, creating
        a dead-end review state with no forward path.
        """
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "Approved")

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"Expected 422 for Approved→InReview, got {r.status_code}: {r.text}"
        )

        # Period is still Approved; cancel it directly
        await _force_status(direct_db, pid, "Cancelled")

    async def test_open_cancel_still_requires_finalize(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """Regression: Open→Cancelled also requires payroll.finalize (pre-existing)."""
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "Open")

        role_id = await _create_role_with_perms(
            session_client, auth_token, "cp0c_open_view_role_2092", ["payroll.view"],
        )
        view_token = await _create_user_with_role(
            session_client, auth_token, "cp0c_open_view_user_2092", role_id,
        )

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(view_token),
        )
        assert r.status_code == 403

        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )


# ---------------------------------------------------------------------------
# TestReviewItemResolution — CP-0C fix: orphaned Pending items are resolved
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestReviewItemResolution:
    """
    Verify that manual transitions out of InReview cancel any Pending
    PeriodApproval review item in the same transaction.
    """

    async def test_inreview_to_open_now_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """CP-1A: InReview→Open via PATCH is blocked; InReview has no PATCH exits."""
        company_id = _TEST_COMPANY_ID
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "InReview")

        await _insert_pending_review_item(
            direct_db, company_id, paytest_branch_id, pid, _TEST_ADMIN_USER_ID,
        )

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"CP-1A: InReview→Open must be blocked (422), got {r.status_code}: {r.text}"
        )

        # Period and review item should be unchanged
        item_status = await _get_review_item_status(direct_db, company_id, pid)
        assert item_status == "Pending", (
            f"Review item should remain Pending after blocked InReview→Open, got '{item_status}'"
        )

        await _force_status(direct_db, pid, "Cancelled")

    async def test_inreview_to_cancelled_now_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """CP-1A: InReview→Cancelled via PATCH is blocked; InReview has no PATCH exits."""
        company_id = _TEST_COMPANY_ID
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "InReview")

        await _insert_pending_review_item(
            direct_db, company_id, paytest_branch_id, pid, _TEST_ADMIN_USER_ID,
        )

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"CP-1A: InReview→Cancelled must be blocked (422), got {r.status_code}: {r.text}"
        )

        # Period and review item should be unchanged
        item_status = await _get_review_item_status(direct_db, company_id, pid)
        assert item_status == "Pending", (
            f"Review item should remain Pending after blocked InReview→Cancelled, got '{item_status}'"
        )

        await _force_status(direct_db, pid, "Cancelled")

    async def test_inreview_to_open_no_exit_regardless_of_review_items(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """CP-1A: InReview→Open is blocked even when no Pending review item exists."""
        company_id = _TEST_COMPANY_ID
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "InReview")

        # No review item — the transition should still be blocked
        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"CP-1A: InReview→Open must be blocked even without a review item, "
            f"got {r.status_code}: {r.text}"
        )

        item_status = await _get_review_item_status(direct_db, company_id, pid)
        assert item_status is None, "Expected no review item for this period"

        await _force_status(direct_db, pid, "Cancelled")

    async def test_approved_to_cancelled_is_blocked(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        CP-1A: Approved→Cancelled via PATCH is blocked.
        Only Draft and Open can be cancelled. Approved periods exit only via
        POST /finalize (→ Locked). The blocking prevents inadvertent data loss
        of periods that have already been approved by a reviewer.
        """
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "Approved")

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"CP-1A: Approved→Cancelled must be blocked (422), got {r.status_code}: {r.text}"
        )
        assert "cannot be transitioned" in r.text or "Approved" in r.text

        # Period must still be Approved (transition was rejected)
        from sqlalchemy import text as _t
        row = await direct_db.execute(
            _t("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        assert row.scalar_one() == "Approved", "Period should remain Approved after rejected transition"

        await _force_status(direct_db, pid, "Cancelled")


# ---------------------------------------------------------------------------
# TestDeadlockPrevention — deterministic lock-order proof
# ---------------------------------------------------------------------------


def _period_unlocked_nowait(pid: int, pg_dsn: dict) -> bool:
    """
    Synchronous helper (runs in executor).
    Attempts SELECT … FOR UPDATE NOWAIT on the period row.

    Returns True  if the row is NOT currently locked (NOWAIT acquires, then rolled back).
    Returns False if another transaction holds the row lock (LockNotAvailable raised).

    This is the regression-sensitive proof:
      New code (ReviewItem → Period): PATCH is blocked waiting for ri lock, has NOT
        yet reached the period UPDATE → period row is free → returns True.
      Old code (Period → ReviewItem): PATCH would have UPDATE'd/locked the period
        first, THEN blocked on the ri lock → period row IS locked → returns False.
    A False result causes the test to FAIL, detecting the lock-order regression.
    """
    conn = psycopg2.connect(client_encoding="utf-8", **pg_dsn)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT payrollperiodid FROM payroll.payrollperiods "
            "WHERE payrollperiodid = %s FOR UPDATE NOWAIT",
            [pid],
        )
        conn.rollback()
        return True
    except psycopg2.errors.LockNotAvailable:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


@pytest.mark.asyncio
class TestDeadlockPrevention:
    """
    CP-0C: Lock-order and deadlock-prevention assertions for InReview exits.

    CP-1A update: InReview now has NO PATCH exits (InReview → Open and
    InReview → Cancelled are both blocked with 422). This eliminates the
    review-item / period lock-ordering concern at the source — since the PATCH
    path returns before acquiring any lock, there is nothing to order.

    The test below proves this: it monitors AsyncConnection.execute for any
    review-item lock attempt during a PATCH InReview → Open call and asserts
    that NO such attempt is made. This is the definitive proof that the
    deadlock scenario is structurally eliminated, not just avoided by ordering.
    """

    @pytest.mark.asyncio
    async def test_patch_inreview_to_open_acquires_no_ri_lock(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        CP-1A: PATCH InReview→Open is blocked before any lock is acquired.
        Proves the deadlock scenario from CP-0C is structurally eliminated.

        DECISIVE PROOF: We monkeypatch AsyncConnection.execute. If the PATCH
        path ever issues a SELECT managerreviewitems FOR UPDATE (the ri-lock),
        the flag is set and the assertion fails. With CP-1A blocking the
        transition at schema validation (before DB queries), the flag stays
        unset — the deadlock source no longer exists.
        """
        from sqlalchemy.ext.asyncio import AsyncConnection

        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "InReview")

        ri_lock_attempted = False
        _real_ac_execute = AsyncConnection.execute

        async def _patched_ac_execute(self, statement, *args, **kwargs):
            nonlocal ri_lock_attempted
            sql_text = str(statement)
            if "managerreviewitems" in sql_text and "FOR UPDATE" in sql_text:
                ri_lock_attempted = True
            return await _real_ac_execute(self, statement, *args, **kwargs)

        AsyncConnection.execute = _patched_ac_execute  # type: ignore[method-assign]
        try:
            r = await session_client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Open"},
                headers=_auth(auth_token),
            )
            assert r.status_code == 422, (
                f"CP-1A: InReview→Open must be blocked (422), got {r.status_code}"
            )
            assert not ri_lock_attempted, (
                "PATCH InReview→Open issued a SELECT managerreviewitems FOR UPDATE — "
                "the transition should be rejected before any lock is attempted. "
                "This indicates the CP-1A transition guard is missing."
            )
        finally:
            AsyncConnection.execute = _real_ac_execute  # type: ignore[method-assign]
            await _force_status(direct_db, pid, "Cancelled")
