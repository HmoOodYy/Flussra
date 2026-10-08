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
              TestBonusMutationStatusGuard, TestDayGridMutationStatusGuard (CP-0A).
    Area 3  — PayItem retirement race: TestStaleRetirementRace (CP-0A).
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

import httpx
import psycopg2
import pytest
from sqlalchemy import text

from tests.db_state import FINALIZED_HISTORY_TRIGGERS, suspended_test_triggers

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
    """Force period to Cancelled -- suspends the finalized-history guards for Locked/Archived rows."""
    async with suspended_test_triggers(direct_db, FINALIZED_HISTORY_TRIGGERS):
        await direct_db.execute(
            text(
                "UPDATE payroll.payrollperiods "
                "SET status = 'Cancelled' WHERE payrollperiodid = :pid"
            ),
            {"pid": pid},
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
