"""
CP-0A: Freeze InReview source mutations.

Verifies that every payroll source mutation path (draft-line create/update/void,
bonus-event create/update/void, day-grid save) accepts only Open periods and
rejects all other statuses (Draft, InReview, Approved, Locked, Archived,
Cancelled).

True concurrency tests (TestTrueRace) use a psycopg2 thread to hold a
FOR UPDATE lock on the period row, change its status to InReview, and commit.
The concurrent HTTP mutation blocks at _lock_period_for_mutation until the
thread commits, then reads InReview and must return 409.  These tests FAIL if
_lock_period_for_mutation is removed — the upfront check alone cannot catch this
race because it runs before the lock and sees the original Open status.

Invariant tests (TestRejectedMutationInvariants) verify that a rejected
mutation leaves source rows unchanged and writes no audit event.

Retirement race (TestStaleRetirementRace) verifies that a source write waiting
on the PayItem lock rejects an item retired in the meantime.
"""

import asyncio
import threading

import httpx
import psycopg2
import pytest
from sqlalchemy import text

from tests.db_state import FINALIZED_HISTORY_TRIGGERS, suspended_test_triggers
from tests.seed_helpers import attach_cdpi_owner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_all_non_terminal(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    """Cancel any Draft/Open/InReview/Approved period on the branch."""
    headers = _auth(token)
    for status in ("Draft", "Open", "InReview", "Approved"):
        r = await client.get(
            "/payroll/periods",
            params={"branch_id": branch_id, "status": status},
            headers=headers,
        )
        if r.status_code != 200:
            continue
        for p in r.json():
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"},
                headers=headers,
            )


async def _force_status(direct_db, period_id: int, new_status: str) -> None:
    """Bypass application logic and set period status directly in DB."""
    await direct_db.execute(
        text(
            "UPDATE payroll.payrollperiods SET status = :s "
            "WHERE payrollperiodid = :pid"
        ),
        {"s": new_status, "pid": period_id},
    )


async def _force_cancel(direct_db, period_id: int) -> None:
    """Force a period to Cancelled (needed for cleanup when triggers fire)."""
    # Disable triggers briefly so Locked/Archived can be cleaned up
    async with suspended_test_triggers(direct_db, FINALIZED_HISTORY_TRIGGERS):
        await direct_db.execute(
            text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )


async def _create_open_period(
    direct_db,
    branch_id: int,
    start: str = "2092-03-03",
    end: str = "2092-03-09",
) -> int:
    """Insert an Open period directly. Returns period_id.

    CP-1D: POST /payroll/periods (legacy) now requires an existing Open period.
    Direct insertion bypasses the guard for test setup.
    """
    # Cancel any existing active period so ux_payrollperiods_oneopenperbranch doesn't fire.
    await direct_db.execute(
        text(
            "UPDATE payroll.payrollperiods "
            "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
            "WHERE branchid = :bid AND status = 'Returned'"
        ),
        {"bid": branch_id},
    )
    await direct_db.execute(
        text(
            "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
            "WHERE branchid = :bid AND status IN ('Draft','Open','InReview')"
        ),
        {"bid": branch_id},
    )
    import datetime as _dt
    row = (await direct_db.execute(
        text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": f"CP0A-{start}", "name": f"CP0A {start}",
         "start": _dt.date.fromisoformat(start), "end": _dt.date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _add_draft_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str = "2092-03-04",
) -> int:
    """Add a DailyNote draft line while period is Open. Returns draft_line_id."""
    r = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json={
            "driver_id": driver_id,
            "work_date": work_date,
            "line_type": "DailyNote",
            "quantity": 1,
            "source_type": "Manual",
            "notes": "filler",
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"add line failed: {r.text}"
    return r.json()["draft_line_id"]


async def _add_bonus_line(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    period_id: int,
    driver_id: int,
) -> int:
    """Add a canonical bonus event while period is Open. Returns bonus_event_id."""
    r = await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_id": driver_id, "amount": "50.00"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"add bonus failed: {r.text}"
    return r.json()["bonus_event_id"]


# ---------------------------------------------------------------------------
# Test data — week far in the future to avoid colliding with other test
# suites that create periods on the same branch.
# ---------------------------------------------------------------------------

_WEEK_START = "2092-03-03"
_WEEK_END   = "2092-03-09"
_WORK_DATE  = "2092-03-04"


# ---------------------------------------------------------------------------
# Draft-line family
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDraftLineMutationStatusGuard:
    """
    Each mutation (create / update / void) on a draft line must accept Open
    and reject every other status.
    """

    async def test_add_line_open_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": paytest_driver_id,
                    "work_date": _WORK_DATE,
                    "line_type": "DailyNote",
                    "quantity": 1,
                    "source_type": "Manual",
                    "notes": "filler",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 201, r.text
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", [
        # CP-2F: Draft removed — Draft now allows daily source line adds.
        "InReview", "Approved", "Locked", "Archived", "Cancelled"
    ])
    async def test_add_line_non_open_rejected(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            await _force_status(direct_db, pid, bad_status)
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": paytest_driver_id,
                    "work_date": _WORK_DATE,
                    "line_type": "DailyNote",
                    "quantity": 1,
                    "source_type": "Manual",
                    "notes": "filler",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), (
                f"Expected rejection for status={bad_status!r}, got {r.status_code}: {r.text}"
            )
        finally:
            await _force_cancel(direct_db, pid)

    async def test_update_line_open_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_draft_line(
                client, auth_token, pid, paytest_driver_id, _WORK_DATE
            )
            r = await client.patch(
                f"/payroll/periods/{pid}/lines/{lid}",
                json={"quantity": 2},
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", [
        # CP-2F: Draft removed — Draft now allows daily source line updates.
        "InReview", "Approved", "Locked", "Archived", "Cancelled"
    ])
    async def test_update_line_non_open_rejected(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_draft_line(
                client, auth_token, pid, paytest_driver_id, _WORK_DATE
            )
            await _force_status(direct_db, pid, bad_status)
            r = await client.patch(
                f"/payroll/periods/{pid}/lines/{lid}",
                json={"quantity": 3},
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), (
                f"Expected rejection for status={bad_status!r}, got {r.status_code}: {r.text}"
            )
        finally:
            await _force_cancel(direct_db, pid)

    async def test_void_line_open_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_draft_line(
                client, auth_token, pid, paytest_driver_id, _WORK_DATE
            )
            r = await client.delete(
                f"/payroll/periods/{pid}/lines/{lid}",
                headers=_auth(auth_token),
            )
            assert r.status_code == 204, r.text
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", [
        # CP-2F: Draft removed — Draft now allows daily source line voids.
        "InReview", "Approved", "Locked", "Archived", "Cancelled"
    ])
    async def test_void_line_non_open_rejected(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_draft_line(
                client, auth_token, pid, paytest_driver_id, _WORK_DATE
            )
            await _force_status(direct_db, pid, bad_status)
            r = await client.delete(
                f"/payroll/periods/{pid}/lines/{lid}",
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), (
                f"Expected rejection for status={bad_status!r}, got {r.status_code}: {r.text}"
            )
        finally:
            await _force_cancel(direct_db, pid)


# ---------------------------------------------------------------------------
# Period-pay / bonus-compatible family
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestBonusMutationStatusGuard:
    """
    Each Bonus event mutation (create / update / void) must accept Open
    and reject every other status.
    """

    async def test_add_bonus_open_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/bonuses",
                json={"driver_id": paytest_driver_id, "amount": "25.00"},
                headers=_auth(auth_token),
            )
            assert r.status_code == 201, r.text
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", [
        "Draft", "InReview", "Approved", "Locked", "Archived", "Cancelled"
    ])
    async def test_add_bonus_non_open_rejected(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            await _force_status(direct_db, pid, bad_status)
            r = await client.post(
                f"/payroll/periods/{pid}/bonuses",
                json={"driver_id": paytest_driver_id, "amount": "25.00"},
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), (
                f"Expected rejection for status={bad_status!r}, got {r.status_code}: {r.text}"
            )
        finally:
            await _force_cancel(direct_db, pid)

    async def test_update_bonus_open_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_bonus_line(client, auth_token, paytest_branch_id, pid, paytest_driver_id)
            r = await client.patch(
                f"/payroll/periods/{pid}/bonuses/{lid}",
                json={"amount": "75.00"},
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", [
        "Draft", "InReview", "Approved", "Locked", "Archived", "Cancelled"
    ])
    async def test_update_bonus_non_open_rejected(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_bonus_line(client, auth_token, paytest_branch_id, pid, paytest_driver_id)
            await _force_status(direct_db, pid, bad_status)
            r = await client.patch(
                f"/payroll/periods/{pid}/bonuses/{lid}",
                json={"amount": "75.00"},
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), (
                f"Expected rejection for status={bad_status!r}, got {r.status_code}: {r.text}"
            )
        finally:
            await _force_cancel(direct_db, pid)

    async def test_void_bonus_open_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_bonus_line(client, auth_token, paytest_branch_id, pid, paytest_driver_id)
            r = await client.delete(
                f"/payroll/periods/{pid}/bonuses/{lid}",
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", [
        "Draft", "InReview", "Approved", "Locked", "Archived", "Cancelled"
    ])
    async def test_void_bonus_non_open_rejected(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_bonus_line(client, auth_token, paytest_branch_id, pid, paytest_driver_id)
            await _force_status(direct_db, pid, bad_status)
            r = await client.delete(
                f"/payroll/periods/{pid}/bonuses/{lid}",
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), (
                f"Expected rejection for status={bad_status!r}, got {r.status_code}: {r.text}"
            )
        finally:
            await _force_cancel(direct_db, pid)


# ---------------------------------------------------------------------------
# Day-grid save family
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDayGridMutationStatusGuard:
    """
    Day-grid save must accept Open and reject every other status.
    """

    async def test_day_grid_save_open_succeeds(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": _WORK_DATE,
                    "rows": [
                        {
                            "driver_id": paytest_driver_id,
                            "values": {"HOURS": "8"},
                        }
                    ],
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, r.text
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", [
        # CP-2F: Draft removed — Draft now allows day-grid saves (operational entry).
        "InReview", "Approved", "Locked", "Archived", "Cancelled"
    ])
    async def test_day_grid_save_non_open_rejected(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            await _force_status(direct_db, pid, bad_status)
            r = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": _WORK_DATE,
                    "rows": [
                        {
                            "driver_id": paytest_driver_id,
                            "values": {"HOURS": "8"},
                        }
                    ],
                },
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), (
                f"Expected rejection for status={bad_status!r}, got {r.status_code}: {r.text}"
            )
        finally:
            await _force_cancel(direct_db, pid)


# ---------------------------------------------------------------------------
# Deterministic concurrency race tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTrueRace:
    """
    Deterministic concurrency tests using monkeypatching + psycopg2 threads.

    Design:
      A. A psycopg2 thread holds a FOR UPDATE lock on the period row
         (uncommitted UPDATE status → InReview), signalling lock_acquired, then
         waits for an explicit release_lock signal before committing.
      B. _lock_period_for_mutation is monkeypatched with a wrapper that sets
         boundary_reached when the HTTP request arrives at the protected write
         boundary, then waits for release_boundary before calling the real helper.
      C. The test orchestrates:
           1. Start thread (holds lock).
           2. Assert lock_acquired fired.
           3. Start HTTP mutation as a background asyncio task.
           4. Assert boundary_reached fired (proves the request reached the
              protected boundary, not just the upfront check).
           5. Let the transition thread commit first (release_lock).
           6. Assert thread_done fired.
           7. Let the real helper run (release_boundary).
           8. Await HTTP response — must be 409.

    These tests FAIL deterministically if _lock_period_for_mutation is removed:
    without the function, boundary_reached is never set → the wait times out →
    the assertion fails immediately, before even checking the response code.
    """

    def _setup_race(
        self,
        pg_instance,
        pid: int,
    ):
        """
        Return (thread, events_dict).
        events_dict keys: lock_acquired, release_lock, thread_done.
        Caller must set release_lock to commit the transition.
        """
        lock_acquired = threading.Event()
        release_lock  = threading.Event()
        thread_done   = threading.Event()
        thread_exc: list[BaseException] = []

        def run():
            conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
            conn.autocommit = False
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE payroll.payrollperiods SET status = 'InReview' "
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

    def _patch_lock(self, boundary_reached: threading.Event, release_boundary: threading.Event):
        """
        Return (monkeypatched_fn, restore_fn).
        The monkeypatched fn sets boundary_reached, waits for release_boundary,
        then calls the real _lock_period_for_mutation.

        This fixture is shared by draft-line, day-grid, and bonus race tests.
        Each of those domains now lives in its own module and imports its own
        _lock_period_for_mutation binding directly from app.payroll.mutation_lock
        — separate namespace entries from app.payroll.service's, and from each
        other's. Bonus (Stage B4-8) moved to app.payroll.bonus, Draft-line CRUD
        (add_draft_line/update_draft_line/void_draft_line) moved to
        app.payroll.draft_line_mutation in Stage B4-14, and Day Grid
        (get_day_grid/save_day_grid) moved to app.payroll.day_grid in Stage
        B4-16. app.payroll.service is still patched because other callers that
        resolve the name through its globals remain there. All four must be
        patched so the boundary fires regardless of which module's bare-name
        lookup resolves the call.

        The day_grid entry is load-bearing, not defensive: a day-grid save whose
        payload carries ordinary pay-item values reaches add_draft_line first and
        would fire the boundary through the draft_line_mutation binding anyway,
        but a status/notes-only payload skips Draft-line mutation entirely and
        reaches save_day_grid's own lock call as the first lock of the request.
        Without this entry that path is not intercepted at all.
        """
        import app.payroll.bonus as bonus_mod
        import app.payroll.day_grid as day_grid_mod
        import app.payroll.draft_line_mutation as dlm
        import app.payroll.service as svc
        real_lock_svc = svc._lock_period_for_mutation
        real_lock_bonus = bonus_mod._lock_period_for_mutation
        real_lock_dlm = dlm._lock_period_for_mutation
        real_lock_day_grid = day_grid_mod._lock_period_for_mutation

        async def mock_lock(period_id, company_id, db):
            boundary_reached.set()
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, release_boundary.wait, 15.0)
            assert ok, "release_boundary never fired inside mock_lock"
            await real_lock_svc(period_id, company_id, db)

        svc._lock_period_for_mutation = mock_lock
        bonus_mod._lock_period_for_mutation = mock_lock
        dlm._lock_period_for_mutation = mock_lock
        day_grid_mod._lock_period_for_mutation = mock_lock

        def restore():
            svc._lock_period_for_mutation = real_lock_svc
            bonus_mod._lock_period_for_mutation = real_lock_bonus
            dlm._lock_period_for_mutation = real_lock_dlm
            day_grid_mod._lock_period_for_mutation = real_lock_day_grid

        return restore

    async def _run_race(
        self,
        *,
        pg_instance,
        pid: int,
        direct_db,
        http_coro,
        loop,
    ):
        """
        Core race orchestration.  http_coro is a coroutine that fires the HTTP
        mutation.  Returns the HTTP response.
        """
        t, ev = self._setup_race(pg_instance, pid)

        boundary_reached  = threading.Event()
        release_boundary  = threading.Event()
        restore = self._patch_lock(boundary_reached, release_boundary)

        try:
            t.start()

            # Step 1: wait for transition to hold the lock
            ok = await loop.run_in_executor(None, ev["lock_acquired"].wait, 5.0)
            assert ok, "Transition thread failed to acquire lock within 5 s"

            # Step 2: fire the HTTP mutation in the background
            http_task = asyncio.create_task(http_coro)

            # Step 3: wait for the request to reach the protected write boundary
            ok = await loop.run_in_executor(None, boundary_reached.wait, 10.0)
            assert ok, (
                "boundary_reached never fired — _lock_period_for_mutation was "
                "not called.  If _lock_period_for_mutation was removed, this "
                "test FAILS here, proving the boundary is required."
            )

            # Step 4: let transition commit first (race condition scenario)
            ev["release_lock"].set()
            ok = await loop.run_in_executor(None, ev["thread_done"].wait, 10.0)
            assert ok, "Transition thread did not finish within 10 s"

            # Check thread did not raise
            if ev["thread_exc"]:
                raise ev["thread_exc"][0]

            # Step 5: release the real helper — it will now see committed InReview
            release_boundary.set()

            # Step 6: await response
            r = await http_task
            return r
        finally:
            restore()
            t.join(timeout=5)
            await _force_cancel(direct_db, pid)

    async def test_draft_line_add_boundary_sees_inreview(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        pg_instance,
        direct_db,
    ):
        """
        _lock_period_for_mutation is proven to be called (boundary_reached fires).
        After the transition commits InReview, the real helper reads it and → 409.
        Without the helper, boundary_reached never fires and the test fails at
        the assertion before the response check.
        """
        pid = await _create_open_period(direct_db, paytest_branch_id)
        loop = asyncio.get_running_loop()

        coro = client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id": paytest_driver_id,
                "work_date": _WORK_DATE,
                "line_type": "DailyNote",
                "quantity": 1,
                "source_type": "Manual",
                "notes": "filler",
            },
            headers=_auth(auth_token),
        )
        r = await self._run_race(
            pg_instance=pg_instance, pid=pid, direct_db=direct_db,
            http_coro=coro, loop=loop,
        )
        assert r.status_code == 409, (
            f"Expected 409 from _lock_period_for_mutation after InReview committed, "
            f"got {r.status_code}: {r.text}"
        )
        # Source row must not exist
        count = (await direct_db.execute(
            text(
                "SELECT COUNT(*) FROM payroll.payrolldraftlines "
                "WHERE payrollperiodid = :pid AND driverid = :did AND status != 'Void'"
            ),
            {"pid": pid, "did": paytest_driver_id},
        )).scalar_one()
        assert count == 0, "Source row must not exist after rejected race mutation"

    async def test_day_grid_save_boundary_sees_inreview(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        pg_instance,
        direct_db,
    ):
        """
        Day-grid save: boundary is reached, transition commits InReview → 409.
        Removing _lock_period_for_mutation makes boundary_reached never fire → test fails.
        """
        pid = await _create_open_period(direct_db, paytest_branch_id)
        loop = asyncio.get_running_loop()

        coro = client.post(
            f"/payroll/periods/{pid}/day-grid",
            json={
                "work_date": _WORK_DATE,
                "rows": [{"driver_id": paytest_driver_id, "values": {"HOURS": "8"}}],
            },
            headers=_auth(auth_token),
        )
        r = await self._run_race(
            pg_instance=pg_instance, pid=pid, direct_db=direct_db,
            http_coro=coro, loop=loop,
        )
        assert r.status_code == 409, (
            f"Expected 409 from day-grid race, got {r.status_code}: {r.text}"
        )

    async def test_bonus_add_boundary_sees_inreview(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        pg_instance,
        direct_db,
    ):
        """
        Bonus event add: boundary reached, transition commits InReview → 409.
        """
        pid = await _create_open_period(direct_db, paytest_branch_id)
        loop = asyncio.get_running_loop()

        coro = client.post(
            f"/payroll/periods/{pid}/bonuses",
            json={"driver_id": paytest_driver_id, "amount": "50.00"},
            headers=_auth(auth_token),
        )
        r = await self._run_race(
            pg_instance=pg_instance, pid=pid, direct_db=direct_db,
            http_coro=coro, loop=loop,
        )
        assert r.status_code == 409, (
            f"Expected 409 from period-pay race, got {r.status_code}: {r.text}"
        )


# ---------------------------------------------------------------------------
# Rejected mutation invariants
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRejectedMutationInvariants:
    """
    After a rejected mutation the source row must be unchanged and no audit
    event must have been written for the rejected attempt.
    """

    @pytest.mark.parametrize("bad_status", ["InReview", "Approved"])
    async def test_rejected_draft_add_no_row_and_no_audit(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            audit_before = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode = 'DRAFT_LINE_ADDED' AND entityschema = 'payroll'"
                ),
            )).scalar_one()

            await _force_status(direct_db, pid, bad_status)

            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": paytest_driver_id,
                    "work_date": _WORK_DATE,
                    "line_type": "DailyNote",
                    "quantity": 1,
                    "source_type": "Manual",
                    "notes": "filler",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), r.text

            # No new source row
            row_count = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM payroll.payrolldraftlines "
                    "WHERE payrollperiodid = :pid AND driverid = :did"
                ),
                {"pid": pid, "did": paytest_driver_id},
            )).scalar_one()
            assert row_count == 0, "Rejected add must not insert a source row"

            # No new audit row
            audit_after = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode = 'DRAFT_LINE_ADDED' AND entityschema = 'payroll'"
                ),
            )).scalar_one()
            assert audit_after == audit_before, "Rejected add must not write an audit event"
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", ["InReview", "Approved"])
    async def test_rejected_draft_void_preserves_row_and_no_audit(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_draft_line(
                client, auth_token, pid, paytest_driver_id, _WORK_DATE
            )
            await _force_status(direct_db, pid, bad_status)

            audit_before = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode = 'DRAFT_LINE_VOIDED' AND entityid = :eid",
                ),
                {"eid": str(lid)},
            )).scalar_one()

            r = await client.delete(
                f"/payroll/periods/{pid}/lines/{lid}",
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), r.text

            # Row must still be active (not voided)
            row = (await direct_db.execute(
                text(
                    "SELECT status FROM payroll.payrolldraftlines WHERE draftlineid = :lid"
                ),
                {"lid": lid},
            )).mappings().first()
            assert row is not None and row["status"] != "Void", (
                "Rejected void must leave the source row active"
            )

            audit_after = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode = 'DRAFT_LINE_VOIDED' AND entityid = :eid",
                ),
                {"eid": str(lid)},
            )).scalar_one()
            assert audit_after == audit_before, "Rejected void must not write a VOIDED audit event"
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", ["InReview", "Approved", "Locked"])
    async def test_rejected_bonus_update_row_unchanged_no_audit(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_bonus_line(
                client, auth_token, paytest_branch_id, pid, paytest_driver_id
            )

            orig = (await direct_db.execute(
                text("SELECT amount FROM payroll.payrollbonusevents WHERE payrollbonuseventid = :lid"),
                {"lid": lid},
            )).scalar_one()

            audit_before = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode = 'BONUS_EVENT_UPDATED' AND entityid = :eid"
                ),
                {"eid": str(lid)},
            )).scalar_one()

            await _force_status(direct_db, pid, bad_status)

            r = await client.patch(
                f"/payroll/periods/{pid}/bonuses/{lid}",
                json={"amount": "999.00"},
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), r.text

            after_amount = (await direct_db.execute(
                text("SELECT amount FROM payroll.payrollbonusevents WHERE payrollbonuseventid = :lid"),
                {"lid": lid},
            )).scalar_one()
            assert float(after_amount) == float(orig), (
                f"Amount must not change after rejected update (was {orig}, now {after_amount})"
            )

            audit_after = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode = 'BONUS_EVENT_UPDATED' AND entityid = :eid"
                ),
                {"eid": str(lid)},
            )).scalar_one()
            assert audit_after == audit_before, "Rejected bonus update must not write audit"
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", ["InReview", "Approved", "Locked"])
    async def test_rejected_bonus_void_row_unchanged_no_audit(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            lid = await _add_bonus_line(
                client, auth_token, paytest_branch_id, pid, paytest_driver_id
            )
            await _force_status(direct_db, pid, bad_status)

            audit_before = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode = 'BONUS_EVENT_VOIDED' AND entityid = :eid"
                ),
                {"eid": str(lid)},
            )).scalar_one()

            r = await client.delete(
                f"/payroll/periods/{pid}/bonuses/{lid}",
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), r.text

            row = (await direct_db.execute(
                text("SELECT status FROM payroll.payrollbonusevents WHERE payrollbonuseventid = :lid"),
                {"lid": lid},
            )).mappings().first()
            assert row is not None and row["status"] != "Voided", (
                "Rejected bonus void must leave the event active"
            )

            audit_after = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode = 'BONUS_EVENT_VOIDED' AND entityid = :eid"
                ),
                {"eid": str(lid)},
            )).scalar_one()
            assert audit_after == audit_before, "Rejected bonus void must not write audit"
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", ["InReview", "Approved"])
    async def test_rejected_day_grid_status_row_unchanged_no_audit(
        self,
        bad_status: str,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Day-grid save is rejected and no DailyStatus row is inserted/changed."""
        pid = await _create_open_period(direct_db, paytest_branch_id)
        try:
            audit_before = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode IN ('DRAFT_LINE_ADDED','DRAFT_LINE_UPDATED') "
                    "  AND entityschema = 'payroll'"
                ),
            )).scalar_one()

            await _force_status(direct_db, pid, bad_status)

            r = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": _WORK_DATE,
                    "rows": [
                        {
                            "driver_id": paytest_driver_id,
                            "values": {"HOURS": "8"},
                            "status": "PTO",
                            "notes": "test note",
                        }
                    ],
                },
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), r.text

            # No DailyStatus or DailyNote rows inserted
            ds_count = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM payroll.payrolldraftlines "
                    "WHERE payrollperiodid = :pid AND driverid = :did "
                    "  AND linetype IN ('DailyStatus','DailyNote') AND status != 'Void'"
                ),
                {"pid": pid, "did": paytest_driver_id},
            )).scalar_one()
            assert ds_count == 0, "No DailyStatus/DailyNote rows must be written after rejection"

            audit_after = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode IN ('DRAFT_LINE_ADDED','DRAFT_LINE_UPDATED') "
                    "  AND entityschema = 'payroll'"
                ),
            )).scalar_one()
            assert audit_after == audit_before, "Rejected day-grid save must not write audit"
        finally:
            await _force_cancel(direct_db, pid)


# ---------------------------------------------------------------------------
# Custom PayItem fixtures and retirement race
# ---------------------------------------------------------------------------

async def _insert_minimal_custom_item(direct_db, *, company_id: int, user_id: int) -> tuple[int, str]:
    """
    Insert a minimal custom pay item directly into the DB.
    Returns (pay_item_id, pay_item_code).
    """
    import uuid as _uuid
    code = f"CP0A_{_uuid.uuid4().hex[:6].upper()}"
    iid = (await direct_db.execute(
        text("""
            INSERT INTO payroll.payitems
                (companyid, payitemcode, payitemname, category, datatype, status,
                 itemscope, ratebehavior, isdefaultbranchactive, issystemstandard,
                 sortorder, appearsinpayrollentry, appearsinledger, appearsinreports,
                 requiresrate, createdbyuserid, createdatutc)
            VALUES
                (:cid, :code, 'CP0A Test Item', 'Custom', 'Decimal',
                 'Active', 'Daily', 'Fixed', FALSE, FALSE,
                 999, TRUE, TRUE, TRUE,
                 FALSE, :uid, NOW())
            RETURNING payitemid
        """),
        {"cid": company_id, "uid": user_id, "code": code},
    )).scalar_one()
    await attach_cdpi_owner(direct_db, item_id=iid, user_id=user_id)
    return iid, code


async def _cleanup_custom_item(direct_db, item_id: int) -> None:
    """Remove a test custom pay item and any remaining DraftLines referencing it."""
    for tbl in (
        "payroll.payitemlinetypemap",
        "payroll.payitemratetypemap",
        "payroll.branchpayitemconfig",
    ):
        await direct_db.execute(
            text(f"DELETE FROM {tbl} WHERE payitemid = :iid"),
            {"iid": item_id},
        )
    await direct_db.execute(
        text("DELETE FROM payroll.payrolldraftlines WHERE linetype IN "
             "(SELECT payitemcode FROM payroll.payitems WHERE payitemid = :iid)"),
        {"iid": item_id},
    )
    await direct_db.execute(
        text("DELETE FROM payroll.cdpidefinitions WHERE payitemid = :iid"),
        {"iid": item_id},
    )
    await direct_db.execute(
        text("DELETE FROM payroll.payitems WHERE payitemid = :iid"),
        {"iid": item_id},
    )


# ---------------------------------------------------------------------------
# Stale-retirement race test (CP-0A corrective #5)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStaleRetirementRace:
    """
    Proves that _lock_pay_item_for_source_write revalidates Active status after
    acquiring the lock, so a concurrent retirement that commits while the source
    request is waiting cannot result in a DraftLine for a Retired PayItem.

    Race scenario:
      1. Source creation validates an Active custom PayItem (plain read, OK).
      2. _lock_pay_item_for_source_write reaches the FOR UPDATE and BLOCKS because
         a psycopg2 thread already holds the lock.
      3. The thread retires the PayItem (status = 'Retired') and commits.
      4. Source creation unblocks, reads status = 'Retired', rejects with 422.
      5. No DraftLine is created. PayItem remains Retired.

    This test FAILS if the helper only checks row existence and not Active status.
    """

    async def test_stale_retirement_blocks_source_write(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
        pg_instance,
    ):
        import app.payroll.draft_line_mutation as dlm_mod

        loop = asyncio.get_running_loop()

        cid = (await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )).scalar_one()
        uid = (await direct_db.execute(
            text("SELECT userid FROM sec.users WHERE username = 'testadmin'"),
        )).scalar_one_or_none() or 1

        iid, code = await _insert_minimal_custom_item(direct_db, company_id=cid, user_id=uid)
        pid = await _create_open_period(direct_db, paytest_branch_id)

        try:
            # Activate the custom item on the branch so API validation passes.
            await direct_db.execute(
                text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (payitemid, companyid, branchid, isactive, effectivefrom, createdbyuserid)
                    VALUES (:iid, :cid, :bid, TRUE, '2000-01-01', :uid)
                    ON CONFLICT DO NOTHING
                """),
                {"iid": iid, "cid": cid, "bid": paytest_branch_id, "uid": uid},
            )

            lock_held    = threading.Event()  # psycopg2 thread holds PayItem FOR UPDATE
            release_lock = threading.Event()  # tell thread to retire + commit
            thread_done  = threading.Event()
            thread_exc: list[BaseException] = []

            def retire_thread():
                """Hold PayItem FOR UPDATE, wait, then retire + commit."""
                conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
                conn.autocommit = False
                cur = conn.cursor()
                try:
                    cur.execute(
                        "SELECT payitemid FROM payroll.payitems "
                        "WHERE payitemid = %s AND companyid = %s FOR UPDATE",
                        [iid, cid],
                    )
                    lock_held.set()
                    assert release_lock.wait(timeout=15), "release_lock never fired"
                    cur.execute(
                        "UPDATE payroll.payitems SET status = 'Retired' "
                        "WHERE payitemid = %s",
                        [iid],
                    )
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

            # Monkeypatch: pause _lock_pay_item_for_source_write BEFORE the real
            # FOR UPDATE so the retire thread can commit between Phase 1 validation
            # and the actual lock attempt.  This simulates the race window.
            real_lock_fn = dlm_mod._lock_pay_item_for_source_write
            boundary_reached = threading.Event()
            release_boundary = threading.Event()

            async def mock_lock_fn(line_type, company_id_arg, db, *, period_id=None):
                if line_type == code:
                    boundary_reached.set()
                    ok = await loop.run_in_executor(None, release_boundary.wait, 15.0)
                    assert ok, "release_boundary never fired"
                await real_lock_fn(line_type, company_id_arg, db, period_id=period_id)

            dlm_mod._lock_pay_item_for_source_write = mock_lock_fn

            t = threading.Thread(target=retire_thread, daemon=True)
            try:
                t.start()
                ok = await loop.run_in_executor(None, lock_held.wait, 5.0)
                assert ok, "retire thread failed to acquire PayItem lock within 5 s"

                source_task = asyncio.create_task(
                    client.post(
                        f"/payroll/periods/{pid}/lines",
                        json={
                            "driver_id": paytest_driver_id,
                            "work_date": _WORK_DATE,
                            "line_type": code,
                            "quantity": 1,
                            "source_type": "Manual",
                        },
                        headers=_auth(auth_token),
                    )
                )

                ok = await loop.run_in_executor(None, boundary_reached.wait, 10.0)
                assert ok, (
                    "boundary_reached never fired — _lock_pay_item_for_source_write "
                    "was not called.  Removing the helper makes this FAIL here."
                )

                # Retire and commit while source is waiting at the boundary.
                release_lock.set()
                ok = await loop.run_in_executor(None, thread_done.wait, 10.0)
                assert ok, "retire thread did not finish within 10 s"
                if thread_exc:
                    raise thread_exc[0]

                # Release source — real helper attempts FOR UPDATE, reads Retired → 422.
                release_boundary.set()
                r = await source_task

                assert r.status_code == 422, (
                    f"Expected 422 (PayItem retired concurrently), "
                    f"got {r.status_code}: {r.text}\n"
                    "If _lock_pay_item_for_source_write does not check Active status "
                    "after acquiring the lock, this test FAILS here."
                )

                dl_count = (await direct_db.execute(
                    text("SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE linetype = :code"),
                    {"code": code},
                )).scalar_one()
                assert dl_count == 0, (
                    f"No DraftLine should exist for Retired pay item '{code}', "
                    f"found {dl_count} row(s)"
                )

                item_status = (await direct_db.execute(
                    text("SELECT status FROM payroll.payitems WHERE payitemid = :iid"),
                    {"iid": iid},
                )).scalar_one_or_none()
                assert item_status == "Retired", (
                    f"PayItem must remain Retired, got: {item_status}"
                )

            finally:
                dlm_mod._lock_pay_item_for_source_write = real_lock_fn
                t.join(timeout=5)

        finally:
            await _force_cancel(direct_db, pid)
            await _cleanup_custom_item(direct_db, iid)
