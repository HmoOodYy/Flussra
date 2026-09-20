"""
CP-0D: Phase 0 concurrency regression tests.

Closes the remaining gaps in Phase 0 concurrency coverage that were not already
addressed by CP-0A/B/C focused suites:

Gap 1 — Area B / transition-wins direction (TestSourceMutationVsCancellation):
    CP-0A TestTrueRace proves _lock_period_for_mutation serialises source writes
    against Open→InReview transitions.  This class proves the same lock also
    protects against Open→Cancelled, i.e. _lock_period_for_mutation guards ALL
    terminal transitions from Open, not only the InReview path.

Gap 2 — Area C (TestDoubleSubmitPrevention):
    CP-0B TestStaleTransitionRejection tests the static case where the period is
    already InReview before the request arrives.  This class proves the *concurrent*
    case: two requests that both read Open and race to submit — only one wins.
    The decisive mechanism is the atomic UPDATE WHERE status='Open' RETURNING in
    change_period_status: zero RETURNING rows → 422.

Gap 3 — Area B / source-write-wins direction (TestSourceMutationFirstSerialization):
    Proves the reverse direction not covered by Gap 1: the source mutation acquires
    the real period row FOR UPDATE lock first; while it holds that lock, an
    Open→InReview submit transition cannot complete; the source mutation then
    commits, the submit transition succeeds, and the committed source row is
    coherently visible in the resulting InReview period.
    DECISIVE PROOF: SELECT period FOR UPDATE NOWAIT from a third connection fails
    with LockNotAvailable while the source transaction is paused, proving the real
    exclusive row lock is held.  If _lock_period_for_mutation were weakened to a
    plain SELECT (no FOR UPDATE), NOWAIT would succeed and the test would FAIL.
    SUBMIT-BLOCKING PROOF: the submit task cannot complete within a bounded timeout
    while the source mutation holds the period lock, proving the Open→InReview
    UPDATE cannot acquire the row until the source transaction commits.

Confirmed-adequate coverage (not duplicated here):
    Area 1  — Source mutations reject non-Open: TestDraftLineMutationStatusGuard,
              TestPeriodPayMutationStatusGuard, TestDayGridMutationStatusGuard (CP-0A).
    Area 3  — PayItem deletion/retirement races: TestSettingsDeletionGuard,
              TestFirstReferenceRace, TestStaleRetirementRace,
              TestZeroToMeaningfulRace (CP-0A).
    Area 4  — Expected-state lifecycle transitions: TestStaleTransitionRejection,
              TestOpenToInReviewAlreadySafe (CP-0B).
    Area 6  — Terminal-state revival blocked: TestTerminalStateProtection (CP-0B).
    Area 7  — Review/InReview-exit deadlock prevention: TestDeadlockPrevention (CP-0C).
    Area 8  — InReview mutation guard + source-first serialisation (static +
              Gap 3 above): all source mutation paths reject InReview at the upfront
              check; Gap 3 proves the period row lock prevents submit from publishing
              a review item until the source transaction commits.
    Area 9  — Cancellation permission matrix: TestDraftCancelPermission (CP-0C).
    Area 10 — Branch/company isolation: TestReviewBranchScope,
              TestReviewODASecurity (test_cp6_review.py).

Dates: 2094-* — isolated year, avoids conflicts with CP-0A (2092), CP-0B (2091),
CP-0C (2092-*).

Run from backend/:
    python -m pytest tests/test_cp0d_concurrency_regression.py -v
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
    """Return a unique (start, end) date pair — 2094 year to avoid conflicts."""
    n = next(_WEEK_COUNTER)
    base = datetime.date(2094, 1, 7)   # first Monday of 2094
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


async def _create_open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    direct_db,
) -> tuple[int, str]:
    """
    Insert an Open period directly and return (period_id, start_date).
    CP-1D: POST /payroll/periods requires an existing Open (B1 guard); use direct insert.
    """
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
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": f"CP0D-{start}", "name": f"CP0D {start}",
         "start": datetime.date.fromisoformat(start), "end": datetime.date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"], start


async def _add_draft_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str,
) -> int:
    """Add a DailyNote draft line while period is Open. Returns draft_line_id."""
    r = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json={
            "driver_id":   driver_id,
            "work_date":   work_date,
            "line_type":   "DailyNote",
            "quantity":    1,
            "source_type": "Manual",
            "notes":       "filler",
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"add line failed: {r.text}"
    return r.json()["draft_line_id"]


async def _force_cancel(direct_db, pid: int) -> None:
    """Force period to Cancelled — disables triggers for Locked/Archived rows."""
    for trig, tbl in (
        ("trg_final_line_immutable",    "payroll.payrollfinallines"),
        ("trg_period_status_revert",    "payroll.payrollperiods"),
    ):
        await direct_db.execute(
            text(f"ALTER TABLE {tbl} DISABLE TRIGGER {trig}")
        )
    await direct_db.execute(
        text(
            "UPDATE payroll.payrollperiods "
            "SET status = 'Cancelled' WHERE payrollperiodid = :pid"
        ),
        {"pid": pid},
    )
    for trig, tbl in (
        ("trg_final_line_immutable",    "payroll.payrollfinallines"),
        ("trg_period_status_revert",    "payroll.payrollperiods"),
    ):
        await direct_db.execute(
            text(f"ALTER TABLE {tbl} ENABLE TRIGGER {trig}")
        )


_TEST_COMPANY_ID = 1


def _period_is_locked_nowait(pid: int, pg_dsn: dict) -> bool:
    """
    Synchronous helper (run in executor).
    Attempts SELECT payrollperiodid FOR UPDATE NOWAIT on the period row.

    Returns True  if the row is currently locked by another transaction
                  (LockNotAvailable raised — NOWAIT cannot acquire the lock).
    Returns False if the row is NOT locked (NOWAIT acquires and rolls back).

    This is the decisive proof that _lock_period_for_mutation holds a real
    exclusive row lock: a plain SELECT (no FOR UPDATE) would never block NOWAIT,
    so a True return proves FOR UPDATE is in effect.
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
        return False  # not locked — NOWAIT acquired successfully
    except psycopg2.errors.LockNotAvailable:
        conn.rollback()
        return True   # locked — NOWAIT failed as expected
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# TestSourceMutationVsCancellation — Gap 1 (Area B complement)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSourceMutationVsCancellation:
    """
    Proves _lock_period_for_mutation serialises source writes against
    Open→Cancelled races, not only Open→InReview.

    CP-0A TestTrueRace covers the InReview direction.  This class covers
    Cancelled, closing the gap.

    Design:
    1. Thread A holds an uncommitted UPDATE lock on the period row and will
       commit it as Cancelled.
    2. _lock_period_for_mutation is monkeypatched: the wrapper sets
       boundary_reached and waits for release_boundary before calling the
       real helper.
    3. Orchestration:
         Step 1: Start Thread A; assert lock_acquired.
         Step 2: Fire the source mutation; assert boundary_reached — proves
                 the mutation reached the lock boundary (fails without it).
         Step 3: Let Thread A commit Cancelled; assert thread_done.
         Step 4: Release boundary; real helper reads Cancelled → 409.
         Step 5: Assert no source row exists.

    REGRESSION PROOF: removing _lock_period_for_mutation causes boundary_reached
    to never fire → the test FAILS at Step 2 *before* the response check,
    proving the lock boundary is required.
    """

    def _setup_cancellation_thread(
        self, pg_instance, pid: int
    ) -> tuple[threading.Thread, dict]:
        """Thread A: UPDATE period to Cancelled (uncommitted), then commit."""
        lock_acquired = threading.Event()
        release_lock  = threading.Event()
        thread_done   = threading.Event()
        thread_exc: list[BaseException] = []

        def run() -> None:
            conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
            conn.autocommit = False
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE payroll.payrollperiods "
                    "SET status = 'Cancelled' "
                    "WHERE payrollperiodid = %s",
                    [pid],
                )
                lock_acquired.set()
                assert release_lock.wait(timeout=15), "release_lock never fired"
                conn.commit()
            except Exception as exc:
                thread_exc.append(exc)
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                conn.close()
                thread_done.set()

        t = threading.Thread(target=run, daemon=True)
        return t, {
            "lock_acquired": lock_acquired,
            "release_lock":  release_lock,
            "thread_done":   thread_done,
            "thread_exc":    thread_exc,
        }

    def _patch_lock_boundary(
        self,
        boundary_reached: threading.Event,
        release_boundary: threading.Event,
    ):
        """
        Monkeypatch _lock_period_for_mutation: set boundary_reached, wait for
        release_boundary, then call the real helper.  Returns a restore fn.

        The exercised mutation is a draft-line source add (POST .../lines),
        whose implementation is app.payroll.draft_line_mutation.add_draft_line
        (Stage B4-14) — it resolves _lock_period_for_mutation from that
        module's own globals, not app.payroll.service's, so the patch targets
        app.payroll.draft_line_mutation directly.
        """
        import app.payroll.draft_line_mutation as dlm
        real_lock = dlm._lock_period_for_mutation

        async def _mock(period_id: int, company_id: int, db) -> None:
            boundary_reached.set()
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, lambda: release_boundary.wait(15.0))
            assert ok, "release_boundary never fired inside monkeypatched lock"
            await real_lock(period_id, company_id, db)

        dlm._lock_period_for_mutation = _mock
        return lambda: setattr(dlm, "_lock_period_for_mutation", real_lock)

    async def test_open_to_cancelled_wins_source_write_fails(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        pg_instance,
        direct_db,
    ):
        """
        Thread A commits Open→Cancelled while a source write waits at the
        _lock_period_for_mutation boundary.  The real helper then reads Cancelled
        → 409 Conflict; no source row is written.

        REGRESSION PROOF: without _lock_period_for_mutation, boundary_reached
        never fires → test FAILS at the boundary assertion, proving the lock is
        required.
        """
        pid, period_start = await _create_open_period(session_client, auth_token, paytest_branch_id, direct_db)
        # Use the day after period start as work_date — guaranteed within range
        work_date = (
            datetime.date.fromisoformat(period_start) + datetime.timedelta(days=1)
        ).isoformat()
        loop = asyncio.get_running_loop()

        t, ev = self._setup_cancellation_thread(pg_instance, pid)

        boundary_reached = threading.Event()
        release_boundary = threading.Event()
        restore = self._patch_lock_boundary(boundary_reached, release_boundary)

        try:
            # Step 1: Thread A holds the period row lock (uncommitted Cancelled)
            t.start()
            ok = await loop.run_in_executor(None, lambda: ev["lock_acquired"].wait(10.0))
            assert ok, "Thread A did not acquire the period row lock within 10 s"

            # Step 2: Fire the source mutation — it will reach the lock boundary
            mutation_task = asyncio.create_task(
                session_client.post(
                    f"/payroll/periods/{pid}/lines",
                    json={
                        "driver_id":   paytest_driver_id,
                        "work_date":   work_date,
                        "line_type":   "DailyNote",
                        "quantity":    1,
                        "source_type": "Manual",
                        "notes":       "filler",
                    },
                    headers=_auth(auth_token),
                )
            )

            ok = await loop.run_in_executor(None, lambda: boundary_reached.wait(10.0))
            assert ok, (
                "boundary_reached never fired — _lock_period_for_mutation was not called. "
                "Removing _lock_period_for_mutation causes this assertion to FAIL, proving "
                "the write boundary is required and cannot be bypassed by removing the lock."
            )

            # Step 3: Let Thread A commit Cancelled (releases row lock)
            ev["release_lock"].set()
            ok = await loop.run_in_executor(None, lambda: ev["thread_done"].wait(10.0))
            assert ok, "Thread A did not finish within 10 s"
            t.join(timeout=5)
            if ev["thread_exc"]:
                raise ev["thread_exc"][0]

            # Step 4: Release real helper — reads Cancelled → 409
            release_boundary.set()
            r = await asyncio.wait_for(mutation_task, timeout=15)
            assert r.status_code == 409, (
                f"Expected 409 (period is Cancelled) from _lock_period_for_mutation "
                f"after cancellation race, got {r.status_code}: {r.text}"
            )

            # Step 5: No source row must have been committed
            count = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM payroll.payrolldraftlines "
                    "WHERE payrollperiodid = :pid AND status != 'Void'"
                ),
                {"pid": pid},
            )).scalar_one()
            assert count == 0, (
                f"Expected 0 active source rows after rejected race mutation, found {count}"
            )

        finally:
            restore()
            ev["release_lock"].set()   # release Thread A if test failed early
            t.join(timeout=5)
            await _force_cancel(direct_db, pid)


# ---------------------------------------------------------------------------
# TestDoubleSubmitPrevention — Gap 2 (Area C concurrent double-submit)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDoubleSubmitPrevention:
    """
    Proves that two concurrent Open→InReview submit requests cannot both succeed.

    CP-0B TestStaleTransitionRejection covers the static case where the period
    is already InReview before the second request arrives.  This class covers
    the concurrent case: both requests read Open and race for the UPDATE.

    The decisive mechanism: change_period_status for Open→InReview uses an
    atomic predicate:
        UPDATE payrollperiods SET status='InReview'
        WHERE  payrollperiodid=:pid AND status='Open'
        RETURNING payrollperiodid
    Zero RETURNING rows → 422 "Period is no longer Open".

    Design:
    1. Create an Open period with a draft line (submit guards require non-empty).
    2. Thread A: SELECT period FOR UPDATE (holds row lock without committing).
    3. Patch svc_payroll.get_period_by_id so the API submit sets submit_read_event
       and pauses at allow_continue after reading Open.
    4. Fire the API submit; it reads Open and pauses at step 3.
    5. Assert submit_read_event (submit has already passed the pre-flight read).
    6. Signal Thread A: UPDATE status='InReview' WHERE status='Open'; COMMIT.
       Thread A's UPDATE is not blocked by the API submit (which hasn't reached
       its UPDATE yet — it is paused in _patched_gp).
    7. Assert thread_done; then set allow_continue.
    8. _patched_gp returns the stale Open period to change_period_status.
    9. change_period_status runs UPDATE WHERE status='Open' → 0 rows → 422.

    REGRESSION PROOF: removing the WHERE status='Open' predicate from the UPDATE
    would allow the second request to overwrite InReview back to InReview,
    silently "succeeding" — the assertion at step 9 would fail, detecting the
    regression.
    """

    async def test_concurrent_submit_second_rejected(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        pg_instance,
        direct_db,
    ):
        """
        A submit request that has already read Open is rejected with 422 when
        a concurrent transaction commits InReview before the atomic
        UPDATE WHERE status='Open' runs.

        REGRESSION PROOF: removing the WHERE status='Open' predicate from the
        period UPDATE in change_period_status would allow this second submit to
        succeed silently, breaking the "one Open→InReview at a time" invariant.
        """
        # Stage B4-18: get_period_by_id is called by change_period_status,
        # which now lives in app.payroll.period_lifecycle and resolves it as
        # a bare name through that module's own globals — patching
        # app.payroll.service's compatibility re-export no longer
        # intercepts it.
        import app.payroll.period_lifecycle as svc_payroll

        pid, period_start = await _create_open_period(session_client, auth_token, paytest_branch_id, direct_db)
        work_date = (
            datetime.date.fromisoformat(period_start) + datetime.timedelta(days=1)
        ).isoformat()
        await _add_draft_line(session_client, auth_token, pid, paytest_driver_id, work_date)

        pg_dsn = pg_instance.dsn()

        # Shared synchronisation state
        lock_held         = threading.Event()
        can_commit        = threading.Event()
        thread_done       = threading.Event()
        thread_errors: list[Exception] = []
        submit_read_event = asyncio.Event()    # set when API submit reads Open
        allow_continue    = asyncio.Event()    # set to release _patched_gp
        _first_open_read  = False              # guard: only pause the first Open read

        # ── Thread A: hold period lock, then commit InReview when signalled ─────
        def _run_thread_a() -> None:
            try:
                conn = psycopg2.connect(client_encoding="utf-8", **pg_dsn)
                conn.autocommit = False
                cur = conn.cursor()
                cur.execute(
                    "SELECT payrollperiodid FROM payroll.payrollperiods "
                    "WHERE payrollperiodid = %s FOR UPDATE",
                    [pid],
                )
                lock_held.set()
                assert can_commit.wait(timeout=15), "can_commit never fired"
                cur.execute(
                    "UPDATE payroll.payrollperiods "
                    "SET    status = 'InReview' "
                    "WHERE  payrollperiodid = %s AND status = 'Open'",
                    [pid],
                )
                conn.commit()
                conn.close()
            except Exception as exc:
                thread_errors.append(exc)
                try:
                    conn.rollback(); conn.close()
                except Exception:
                    pass
            finally:
                thread_done.set()

        # ── Patch: pause the API submit after get_period_by_id reads Open ───────
        # This ensures the submit's UPDATE WHERE status='Open' runs AFTER
        # Thread A has committed InReview, making the test deterministic.
        real_gp = svc_payroll.get_period_by_id

        async def _patched_gp(co_id, u_id, p_id, db):
            nonlocal _first_open_read
            result = await real_gp(co_id, u_id, p_id, db)
            if (
                p_id == pid
                and getattr(result, "status", None) == "Open"
                and not _first_open_read
            ):
                _first_open_read = True
                submit_read_event.set()
                # Yield until Thread A has committed InReview
                await asyncio.wait_for(allow_continue.wait(), timeout=15)
            return result

        svc_payroll.get_period_by_id = _patched_gp
        loop = asyncio.get_running_loop()

        try:
            # ── Step 1: Thread A acquires the row lock ────────────────────────
            t = threading.Thread(target=_run_thread_a, daemon=True)
            t.start()

            ok = await loop.run_in_executor(None, lambda: lock_held.wait(10.0))
            assert ok, "Thread A did not acquire period row lock within 10 s"

            # ── Step 2: Fire the API submit ───────────────────────────────────
            # The submit calls get_period_by_id (plain SELECT — not blocked by
            # Thread A's FOR UPDATE).  _patched_gp sets submit_read_event and
            # waits for allow_continue.
            submit_task = asyncio.ensure_future(
                session_client.patch(
                    f"/payroll/periods/{pid}/status",
                    json={"status": "InReview"},
                    headers=_auth(auth_token),
                )
            )

            # ── Step 3: Wait for submit to have read the Open period ──────────
            await asyncio.wait_for(submit_read_event.wait(), timeout=15)
            # submit has read Open and is paused; its UPDATE has NOT yet run.

            # ── Step 4: Signal Thread A to commit InReview ───────────────────
            can_commit.set()
            ok = await loop.run_in_executor(None, lambda: thread_done.wait(10.0))
            assert ok, "Thread A did not finish within 10 s"
            t.join(timeout=5)
            if thread_errors:
                raise thread_errors[0]

            # ── Step 5: Verify Thread A committed InReview ───────────────────
            committed = (await direct_db.execute(
                text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )).scalar_one()
            assert committed == "InReview", (
                f"Expected Thread A to have committed InReview, found '{committed}'"
            )

            # ── Step 6: Release the patched get_period_by_id ─────────────────
            # submit now proceeds with stale existing.status='Open' and runs
            # UPDATE WHERE status='Open' — which finds 0 rows (status is InReview).
            allow_continue.set()

            r = await asyncio.wait_for(submit_task, timeout=15)

            # ── Step 7: THE DECISIVE ASSERTION ───────────────────────────────
            # CP-1D: The loser returns 409 (period no longer Open — concurrent transition
            # moved it). 422 was the pre-CP-1D behavior; 409 is correct with advisory locking.
            assert r.status_code in (409, 422), (
                f"Expected 409/422 (period no longer Open) from the concurrent second submit, "
                f"got {r.status_code}: {r.text}. "
                "This means the atomic UPDATE WHERE status='Open' predicate was bypassed "
                "or is absent — a regression in the double-submit prevention logic."
            )

            # ── Step 8: Period is still InReview (Thread A's commit is stable) ─
            final = (await direct_db.execute(
                text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )).scalar_one()
            assert final == "InReview", (
                f"Expected period to remain InReview after rejected second submit, "
                f"got '{final}'"
            )

        finally:
            svc_payroll.get_period_by_id = real_gp
            can_commit.set()     # release Thread A if test failed early
            allow_continue.set() # unblock _patched_gp if test failed early
            t.join(timeout=5)
            # Cleanup: period is InReview — direct UPDATE to Cancelled is safe
            await direct_db.execute(
                text(
                    "UPDATE payroll.payrollperiods "
                    "SET status = 'Cancelled' "
                    "WHERE payrollperiodid = :pid"
                ),
                {"pid": pid},
            )


# ---------------------------------------------------------------------------
# TestSourceMutationFirstSerialization — Gap 3 (Area B source-write-wins)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSourceMutationFirstSerialization:
    """
    Proves that when a source mutation acquires the period row FOR UPDATE lock
    first, an Open→InReview submit transition cannot complete until the source
    transaction releases that lock, and the committed source row is coherently
    visible in the resulting InReview period.

    This is the source-write-wins direction.  Existing CP-0A TestTrueRace covers
    the transition-wins direction; this class closes the gap.

    How determinism is achieved
    ---------------------------
    1. _lock_period_for_mutation is wrapped to:
         a. call the real function first (acquires the real FOR UPDATE lock);
         b. after the real lock is held, set source_acquired_period_lock;
         c. wait for allow_source_to_commit before returning.
    2. DECISIVE PROOF — NOWAIT from a third psycopg2 connection:
         SELECT period FOR UPDATE NOWAIT → must raise LockNotAvailable.
         Returns True if locked (expected), False if not (regression).
         If _lock_period_for_mutation were weakened to a plain SELECT (no FOR
         UPDATE), NOWAIT would succeed → _period_is_locked_nowait returns False
         → the assertion FAILS, proving the real exclusive lock is required.
    3. SUBMIT-BLOCKING PROOF — the submit task must not complete while the
         source holds the lock:
         asyncio.wait_for(submit_task, timeout=1.0) must raise TimeoutError.
         The submit's UPDATE WHERE status='Open' tries to acquire the period
         row lock and blocks because the source transaction holds FOR UPDATE.
         If the submit could complete despite the lock (regression), the
         TimeoutError would NOT be raised → assertion FAILS.
    4. After allow_source_to_commit is set, the source mutation continues,
         INSERTs the draft line, and commits — releasing the period lock.
    5. The submit's blocked UPDATE unblocks, finds status='Open', transitions
         to InReview, and auto-creates a PeriodApproval review item.

    Final coherent state verified:
    • period status is InReview;
    • the draft line committed by the source mutation exists in the period;
    • a Pending PeriodApproval review item exists for the period;
    • no source mutation row was committed while period was non-Open.

    Why the test proves safety beyond what existing tests prove
    ----------------------------------------------------------
    The existing transition-wins tests (Gap 1, CP-0A) monkeypatch
    _lock_period_for_mutation BEFORE the real lock is acquired.  A weakened
    implementation (plain SELECT instead of FOR UPDATE) would still cause
    boundary_reached to fire in those tests — they cannot detect the absence
    of the actual lock.  This test calls the REAL function first and uses
    NOWAIT to verify the lock is actually held.
    """

    async def test_source_mutation_holds_lock_submit_blocks_then_both_succeed(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        pg_instance,
        direct_db,
    ):
        """
        Source mutation acquires the real period row FOR UPDATE lock; submit
        blocks until the source releases it; both complete; period is InReview
        with the source row coherently included.

        The exercised source mutation is a draft-line add (POST .../lines),
        whose implementation is app.payroll.draft_line_mutation.add_draft_line
        (Stage B4-14) — it resolves _lock_period_for_mutation from that
        module's own globals, not app.payroll.service's, so the patch targets
        app.payroll.draft_line_mutation directly.
        """
        import app.payroll.draft_line_mutation as svc_payroll

        pid, period_start = await _create_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        base = datetime.date.fromisoformat(period_start)
        # Pre-existing line: gives submit a non-empty period so it can pass the
        # empty-period guard and reach the UPDATE WHERE status='Open' step, where
        # it will block on the source mutation's period row lock.
        work_date_pre  = (base + datetime.timedelta(days=1)).isoformat()
        # Line added by the racing source mutation (different date — no dupe).
        work_date_race = (base + datetime.timedelta(days=2)).isoformat()

        await _add_draft_line(
            session_client, auth_token, pid, paytest_driver_id, work_date_pre
        )

        pg_dsn = pg_instance.dsn()

        # Synchronisation state
        source_acquired_period_lock = asyncio.Event()  # set after real FOR UPDATE
        allow_source_to_commit      = threading.Event()  # set to release source

        # ── Wrap _lock_period_for_mutation ────────────────────────────────────
        # Call the real function FIRST so the actual FOR UPDATE is acquired.
        # After the real function returns (lock held), signal and pause.
        real_lock = svc_payroll._lock_period_for_mutation

        async def _instrumented_lock(period_id: int, company_id: int, db) -> None:
            # Real SELECT … FOR UPDATE executes here; period row is now locked.
            await real_lock(period_id, company_id, db)
            # Lock is held for the duration of this DB transaction.
            source_acquired_period_lock.set()
            # Pause — the source mutation transaction stays open with the lock.
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(
                None, lambda: allow_source_to_commit.wait(20.0)
            )
            assert ok, "allow_source_to_commit never fired — test hung"

        svc_payroll._lock_period_for_mutation = _instrumented_lock
        loop = asyncio.get_running_loop()

        try:
            # ── Step 1: Fire source mutation (draft line POST) ────────────────
            # This will reach _instrumented_lock, acquire the real FOR UPDATE,
            # set source_acquired_period_lock, and wait for allow_source_to_commit.
            source_task = asyncio.ensure_future(
                session_client.post(
                    f"/payroll/periods/{pid}/lines",
                    json={
                        "driver_id":   paytest_driver_id,
                        "work_date":   work_date_race,
                        "line_type":   "DailyNote",
                        "quantity":    1,
                        "source_type": "Manual",
                        "notes":       "filler",
                    },
                    headers=_auth(auth_token),
                )
            )

            # ── Step 2: Assert source holds the real period row lock ──────────
            await asyncio.wait_for(source_acquired_period_lock.wait(), timeout=15)

            # DECISIVE PROOF: NOWAIT from third connection must fail.
            # If _lock_period_for_mutation used a plain SELECT (no FOR UPDATE),
            # NOWAIT would succeed here → locked=False → assertion FAILS.
            locked = await loop.run_in_executor(
                None, lambda: _period_is_locked_nowait(pid, pg_dsn)
            )
            assert locked, (
                "Period row is NOT locked — _lock_period_for_mutation does not "
                "hold a real FOR UPDATE lock.  Weakening it to a plain SELECT "
                "removes the serialisation guarantee and causes this test to FAIL."
            )

            # ── Step 3: Start the submit transition ───────────────────────────
            # The submit's UPDATE WHERE status='Open' will try to acquire the
            # period row lock and BLOCK because the source transaction holds it.
            submit_task = asyncio.ensure_future(
                session_client.patch(
                    f"/payroll/periods/{pid}/status",
                    json={"status": "InReview"},
                    headers=_auth(auth_token),
                )
            )

            # SUBMIT-BLOCKING PROOF: submit must not complete within 1 s while
            # the source mutation holds the period lock.
            # If the submit could bypass the lock and complete early (regression),
            # wait_for would NOT raise TimeoutError → assertion FAILS.
            submit_completed_early = False
            try:
                await asyncio.wait_for(asyncio.shield(submit_task), timeout=1.0)
                submit_completed_early = True
            except asyncio.TimeoutError:
                pass  # expected — submit is blocked on the period row lock
            assert not submit_completed_early, (
                "Submit transition completed before the source mutation released "
                "the period row lock.  This means the transition's UPDATE WHERE "
                "status='Open' did not wait for the FOR UPDATE lock to be released "
                "— a regression in lock-order enforcement."
            )

            # ── Step 4: Release the source mutation ───────────────────────────
            # The source mutation can now continue, INSERT the draft line, COMMIT,
            # and release the period row lock.
            allow_source_to_commit.set()

            # ── Step 5: Await source mutation — must succeed (201 Created) ────
            source_r = await asyncio.wait_for(source_task, timeout=20)
            assert source_r.status_code == 201, (
                f"Expected source mutation to succeed (201), got "
                f"{source_r.status_code}: {source_r.text}"
            )
            committed_line_id = source_r.json()["draft_line_id"]

            # ── Step 6: Await submit — must succeed (200 OK) ──────────────────
            submit_r = await asyncio.wait_for(submit_task, timeout=20)
            assert submit_r.status_code == 200, (
                f"Expected submit transition to succeed (200) after source "
                f"mutation released the lock, got {submit_r.status_code}: "
                f"{submit_r.text}"
            )

            # ── Step 7: Verify coherent final state ───────────────────────────
            period_row = await direct_db.execute(
                text(
                    "SELECT status FROM payroll.payrollperiods "
                    "WHERE payrollperiodid = :pid"
                ),
                {"pid": pid},
            )
            assert period_row.scalar_one() == "InReview", (
                "Period must be InReview after successful submit"
            )

            # The committed source row must exist in the period
            line_row = await direct_db.execute(
                text(
                    "SELECT draftlineid FROM payroll.payrolldraftlines "
                    "WHERE draftlineid = :lid AND payrollperiodid = :pid "
                    "  AND status = 'Active'"
                ),
                {"lid": committed_line_id, "pid": pid},
            )
            assert line_row.first() is not None, (
                "Draft line committed by the source mutation must exist in the "
                "InReview period — source row is coherently included in the review"
            )

            # A Pending PeriodApproval review item must have been created by submit
            ri_row = await direct_db.execute(
                text("""
                    SELECT reviewitemid FROM review.managerreviewitems
                    WHERE  companyid    = :cid
                      AND  entityschema = 'payroll'
                      AND  entityname   = 'PayrollPeriods'
                      AND  entityid     = :eid
                      AND  requesttype  = 'PeriodApproval'
                      AND  status       = 'Pending'
                """),
                {"cid": _TEST_COMPANY_ID, "eid": str(pid)},
            )
            assert ri_row.first() is not None, (
                "Submit must have auto-created a Pending PeriodApproval review "
                "item — period is in InReview awaiting manager decision"
            )

            # No further source mutations may commit after the period is InReview.
            # Use a third distinct date (no dupe constraint) so the only rejection
            # reason is the InReview status guard.
            work_date_post = (base + datetime.timedelta(days=3)).isoformat()
            extra_r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id":   paytest_driver_id,
                    "work_date":   work_date_post,
                    "line_type":   "DailyNote",
                    "quantity":    2,
                    "source_type": "Manual",
                    "notes":       "filler",
                },
                headers=_auth(auth_token),
            )
            assert extra_r.status_code in (409, 422), (
                f"Source mutation after period became InReview must be rejected, "
                f"got {extra_r.status_code}: {extra_r.text}"
            )

        finally:
            svc_payroll._lock_period_for_mutation = real_lock
            allow_source_to_commit.set()  # unblock if test failed early
            # Cancel tasks that might still be running
            for task in (source_task, submit_task):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            # Cleanup: cancel the period (may be Open, InReview, or Cancelled)
            await direct_db.execute(
                text(
                    "UPDATE payroll.payrollperiods "
                    "SET status = 'Cancelled' "
                    "WHERE payrollperiodid = :pid "
                    "  AND status NOT IN ('Locked', 'Archived', 'Cancelled')"
                ),
                {"pid": pid},
            )
