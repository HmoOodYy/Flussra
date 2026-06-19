"""
CP-0C integration tests — Transition permissions, review resolution, and deadlock safety.

Three fixes verified here:

1. Permission gap: ("Draft", "Cancelled") was absent from _TRANSITION_PERMISSIONS,
   meaning the permission check was silently skipped (None is falsy).  Any
   authenticated user — regardless of role — could cancel a Draft period.
   Fix: entry added with "payroll.finalize" requirement.

2. Review item orphan: Manual transitions that move a period out of InReview via
   PATCH (InReview→Open, InReview→Cancelled) did not resolve the Pending
   PeriodApproval review item that Open→InReview auto-created.  The item would
   remain Pending indefinitely, preventing re-submission and confusing the
   review queue.
   Fix: CP-0C resolves (Cancels) any Pending PeriodApproval item in the same
   transaction as the period UPDATE.

3. Deadlock prevention (CP-0C corrective follow-up):
   The original CP-0C acquired locks in Period→ReviewItem order while
   decide_review_item() uses ReviewItem→Period order.  Concurrent execution
   of the two paths could deadlock.
   Fix: InReview exits now do ReviewItem FOR UPDATE before the period UPDATE,
   establishing a consistent ReviewItem→Period lock order on both paths.

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
    await _cancel_active_periods(direct_db, branch_id)
    start, end = _next_dates()
    r = await client.post(
        "/payroll/periods",
        json={
            "branch_id":   branch_id,
            "period_type": "Week",
            "start_date":  start,
            "end_date":    end,
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"create Draft period failed: {r.text}"
    return r.json()["payroll_period_id"]


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

    async def test_inreview_to_open_cancels_pending_review_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """InReview→Open via PATCH resolves the Pending review item (status→Cancelled)."""
        company_id = _TEST_COMPANY_ID
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "InReview")

        ri_id = await _insert_pending_review_item(
            direct_db, company_id, paytest_branch_id, pid, _TEST_ADMIN_USER_ID,
        )

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 200, f"InReview→Open failed: {r.text}"
        assert r.json()["status"] == "Open"

        item_status = await _get_review_item_status(direct_db, company_id, pid)
        assert item_status == "Cancelled", (
            f"Expected review item {ri_id} to be Cancelled after InReview→Open, "
            f"got '{item_status}'"
        )

        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )

    async def test_inreview_to_cancelled_cancels_pending_review_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """InReview→Cancelled via PATCH resolves the Pending review item (status→Cancelled)."""
        company_id = _TEST_COMPANY_ID
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "InReview")

        ri_id = await _insert_pending_review_item(
            direct_db, company_id, paytest_branch_id, pid, _TEST_ADMIN_USER_ID,
        )

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 200, f"InReview→Cancelled failed: {r.text}"
        assert r.json()["status"] == "Cancelled"

        item_status = await _get_review_item_status(direct_db, company_id, pid)
        assert item_status == "Cancelled", (
            f"Expected review item {ri_id} to be Cancelled after InReview→Cancelled, "
            f"got '{item_status}'"
        )

    async def test_inreview_to_open_no_review_item_still_succeeds(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """InReview→Open works fine when no Pending review item exists (UPDATE 0 rows is OK)."""
        company_id = _TEST_COMPANY_ID
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "InReview")

        # No review item inserted — the UPDATE WHERE status='Pending' should affect 0 rows silently
        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Open"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 200, f"InReview→Open (no item) failed: {r.text}"
        assert r.json()["status"] == "Open"

        item_status = await _get_review_item_status(direct_db, company_id, pid)
        assert item_status is None, "Expected no review item for this period"

        await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )

    async def test_approved_to_cancelled_leaves_decided_item_intact(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Approved→Cancelled does not accidentally modify an already-decided review item.
        The review item should have been resolved (non-Pending) before the period
        reached Approved, so the CP-0C UPDATE WHERE status='Pending' should touch 0 rows.
        """
        company_id = _TEST_COMPANY_ID
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "InReview")

        # Insert a review item in 'Approved' state (simulating post-decide_review_item state)
        await direct_db.execute(
            text("""
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requestedbyuserid,
                     requesttype, entityschema, entityname, entityid,
                     title, description, priority, status,
                     finaldecisionbyuserid, finaldecisionatutc)
                VALUES
                    (:cid, :bid, :uid,
                     'PeriodApproval', 'payroll', 'PayrollPeriods', :eid,
                     'Test PeriodApproval', 'Already decided', 'Normal', 'Approved',
                     :uid, NOW())
            """),
            {
                "cid": company_id,
                "bid": paytest_branch_id,
                "uid": _TEST_ADMIN_USER_ID,
                "eid": str(pid),
            },
        )

        await _force_status(direct_db, pid, "Approved")

        r = await session_client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 200, f"Approved→Cancelled failed: {r.text}"
        assert r.json()["status"] == "Cancelled"

        # Decided item should remain 'Approved' (not touched by the CP-0C UPDATE)
        item_status = await _get_review_item_status(direct_db, company_id, pid)
        assert item_status == "Approved", (
            f"Expected already-decided review item to remain 'Approved', got '{item_status}'"
        )


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
    Deterministically prove that the payroll PATCH path acquires the review item
    lock BEFORE the period lock, matching decide_review_item()'s order.

    How determinism is achieved
    ---------------------------
    1. Thread A holds the review item row lock (SELECT ri FOR UPDATE).
    2. We monkeypatch AsyncConnection.execute to fire `patch_reached_ri_lock_attempt`
       the moment the PATCH path issues the specific ri-lock SQL
       (SELECT … managerreviewitems … FOR UPDATE … PeriodApproval … Pending).
       This is a hard event boundary — the PATCH has definitively attempted the ri
       lock and will block because Thread A holds it.
    3. After `patch_reached_ri_lock_attempt` fires we do NOWAIT on the period row.
       THE DECISIVE PROOF:
       - New code (ReviewItem → Period): PATCH is blocked on the ri lock and has
         NOT yet reached the period UPDATE → NOWAIT acquires the period → succeeds.
       - Old code (Period → ReviewItem): PATCH would have locked the period first,
         THEN blocked on the ri lock → period IS already locked → NOWAIT fails
         with LockNotAvailable.  The assertion below then FAILS, detecting the
         regression.
    4. We release Thread A.  PATCH wakes up, finds period no longer InReview → 409.
    5. Final state is verified coherent.

    What this test CANNOT prove vs what it DOES prove
    --------------------------------------------------
    • It DOES prove: at the exact moment the PATCH executes the ri-lock statement,
      the period row is not yet locked under new code.
    • It DOES prove: no deadlock occurs — both transactions complete within the
      bounded timeout.
    • It DOES NOT prove the PATCH's internal lock is specifically on the ri row vs
      any other lock; the scenario design (only ri is externally held) makes this
      implicit.
    """

    async def test_patch_locks_review_item_before_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
        pg_instance,
    ):
        """
        Regression-sensitive lock-order test.
        The NOWAIT assertion in step 4 FAILS if change_period_status acquires
        the Period lock before the ReviewItem lock.
        """
        from sqlalchemy.ext.asyncio import AsyncConnection

        company_id = _TEST_COMPANY_ID
        pid = await _create_draft_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _force_status(direct_db, pid, "InReview")
        ri_id = await _insert_pending_review_item(
            direct_db, company_id, paytest_branch_id, pid, _TEST_ADMIN_USER_ID,
        )

        pg_dsn = pg_instance.dsn()
        period_status: str | None = None  # set inside try; used in cleanup

        # Shared state
        acquired_ri               = threading.Event()   # Thread A: "ri lock is held"
        can_commit                = threading.Event()   # Main: "proceed and commit"
        thread_done               = threading.Event()   # Thread A: "transaction done"
        thread_errors: list[Exception] = []
        patch_reached_ri_lock_attempt = asyncio.Event()  # set when PATCH issues ri FOR UPDATE

        # ── Monkeypatch boundary instrumentation ────────────────────────────
        # Intercept AsyncConnection.execute at the class level.
        # When the PATCH path issues the specific ri-lock SQL
        # (SELECT … managerreviewitems … FOR UPDATE … PeriodApproval … Pending),
        # we set patch_reached_ri_lock_attempt BEFORE letting the real query
        # proceed.  The query will then block because Thread A holds the ri row
        # lock.  This gives us a hard boundary: the PATCH has definitively
        # attempted the ri lock — the period must NOT be locked yet under new code.
        _real_ac_execute = AsyncConnection.execute

        async def _patched_ac_execute(self, statement, *args, **kwargs):
            sql_text = str(statement)
            if (
                "managerreviewitems" in sql_text
                and "FOR UPDATE" in sql_text
                and "PeriodApproval" in sql_text
            ):
                patch_reached_ri_lock_attempt.set()
            return await _real_ac_execute(self, statement, *args, **kwargs)

        AsyncConnection.execute = _patched_ac_execute  # type: ignore[method-assign]

        def simulate_review_decision() -> None:
            """
            Models decide_review_item() lock order accurately:
              1. SELECT ri FOR UPDATE  (acquires ri lock — first)
              2. UPDATE period WHERE status='InReview' RETURNING  (second lock)
              3. UPDATE ri (resolve)  — only if period update succeeded
              4. COMMIT
            """
            try:
                conn = psycopg2.connect(client_encoding="utf-8", **pg_dsn)
                conn.autocommit = False
                cur = conn.cursor()
                cur.execute(
                    "SELECT reviewitemid FROM review.managerreviewitems "
                    "WHERE reviewitemid = %s FOR UPDATE",
                    [ri_id],
                )
                acquired_ri.set()
                can_commit.wait(timeout=15)

                cur.execute(
                    "UPDATE payroll.payrollperiods "
                    "SET    status = 'Approved' "
                    "WHERE  payrollperiodid = %s AND companyid = %s AND status = 'InReview' "
                    "RETURNING payrollperiodid",
                    [pid, company_id],
                )
                if cur.fetchone() is not None:
                    cur.execute(
                        "UPDATE review.managerreviewitems "
                        "SET    status = 'Approved', finaldecisionbyuserid = %s, "
                        "       finaldecisionatutc = NOW() "
                        "WHERE  reviewitemid = %s",
                        [_TEST_ADMIN_USER_ID, ri_id],
                    )
                conn.commit()
                conn.close()
            except Exception as exc:  # noqa: BLE001
                thread_errors.append(exc)
                try:
                    conn.rollback(); conn.close()
                except Exception:
                    pass
            finally:
                thread_done.set()

        loop = asyncio.get_running_loop()

        try:
            # ── Step 1: Thread A locks the review item ───────────────────────
            t = threading.Thread(target=simulate_review_decision, daemon=True)
            t.start()

            ri_acquired = await loop.run_in_executor(
                None, lambda: acquired_ri.wait(timeout=10)
            )
            assert ri_acquired, "Thread A did not acquire the ri lock within 10 s"

            # ── Step 2: Fire the PATCH; wait for ri-lock boundary event ─────
            patch_task = asyncio.ensure_future(
                session_client.patch(
                    f"/payroll/periods/{pid}/status",
                    json={"status": "Open"},
                    headers=_auth(auth_token),
                )
            )

            await asyncio.wait_for(patch_reached_ri_lock_attempt.wait(), timeout=10)
            # patch_reached_ri_lock_attempt is set inside _patched_ac_execute
            # immediately before the real SELECT ri FOR UPDATE query is sent to
            # the DB.  Thread A holds the ri row lock, so the PATCH is now
            # blocked there.  The period row is NOT locked yet under new code.
            assert patch_reached_ri_lock_attempt.is_set(), (
                "PATCH never issued the SELECT managerreviewitems FOR UPDATE "
                "within the timeout — ri-lock boundary was not reached."
            )

            # ── Step 3: THE DECISIVE PROOF ───────────────────────────────────
            # Attempt SELECT period FOR UPDATE NOWAIT from a third connection.
            # • New code  → PATCH is blocked at ri lock, period NOT locked → NOWAIT succeeds.
            # • Old code  → PATCH locked period first, then blocked on ri    → NOWAIT fails.
            # This assertion is regression-sensitive and does not depend on timing.
            period_not_locked = await loop.run_in_executor(
                None,
                lambda: _period_unlocked_nowait(pid, pg_dsn),
            )
            assert period_not_locked, (
                "Period row was already locked while Thread A held the ri lock "
                "and the PATCH had started.  This indicates the payroll path "
                "acquired the Period lock BEFORE the ReviewItem lock — the "
                "deadlock-safe ReviewItem→Period ordering has regressed."
            )

            # ── Step 4: Release Thread A ──────────────────────────────────────
            can_commit.set()

            done = await loop.run_in_executor(
                None, lambda: thread_done.wait(timeout=10)
            )
            assert done, "Thread A did not finish within 10 s"
            t.join(timeout=5)
            if thread_errors:
                raise thread_errors[0]

            # ── Step 5: PATCH must complete without deadlock ──────────────────
            patch_response = await asyncio.wait_for(patch_task, timeout=15)
            assert patch_response.status_code in (200, 409, 422), (
                f"Unexpected PATCH status {patch_response.status_code}: {patch_response.text}"
            )

            # ── Step 6: Final state is coherent ───────────────────────────────
            period_row = await direct_db.execute(
                text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )
            period_status = period_row.scalar_one()
            item_status   = await _get_review_item_status(direct_db, company_id, pid)

            coherent_outcomes = {
                ("Approved", "Approved"),  # Thread A won
                ("Open",     "Cancelled"), # PATCH won (unlikely in this setup)
            }
            assert (period_status, item_status) in coherent_outcomes, (
                f"Incoherent final state: period='{period_status}', "
                f"review_item='{item_status}'.  Expected one of {coherent_outcomes}."
            )

        finally:
            AsyncConnection.execute = _real_ac_execute  # type: ignore[method-assign]
            # Ensure Thread A is released if test failed mid-way
            can_commit.set()

        # ── Cleanup ──────────────────────────────────────────────────────────
        if period_status not in ("Cancelled", "Archived", None):
            await _force_status(direct_db, pid, "Cancelled")
