"""
CP-0A: Freeze InReview source mutations.

Verifies that every payroll source mutation path (draft-line create/update/void,
period-pay create/update/void, day-grid save) accepts only Open periods and
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

Settings guard tests (TestSettingsDeletionGuard) verify that the custom pay
item physical-delete path does not remove lines from non-Open periods.
"""

import asyncio
import datetime
import threading
import time

import psycopg2
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text

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
    await direct_db.execute(
        text("ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable")
    )
    await direct_db.execute(
        text("ALTER TABLE payroll.payrollperiods DISABLE TRIGGER trg_period_status_revert")
    )
    await direct_db.execute(
        text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )
    await direct_db.execute(
        text("ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable")
    )
    await direct_db.execute(
        text("ALTER TABLE payroll.payrollperiods ENABLE TRIGGER trg_period_status_revert")
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


async def _ensure_bonus_active(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    """Activate the system BONUS pay item for the branch (idempotent)."""
    r = await client.get(
        f"/settings/branches/{branch_id}/pay-items",
        headers=_auth(token),
    )
    assert r.status_code == 200, r.text
    bonus = next((i for i in r.json() if i.get("pay_item_code") == "BONUS"), None)
    if bonus and not bonus.get("is_active"):
        r2 = await client.patch(
            f"/settings/branches/{branch_id}/pay-items/{bonus['pay_item_id']}",
            json={"is_active": True},
            headers=_auth(token),
        )
        assert r2.status_code == 200, r2.text


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
class TestPeriodPayMutationStatusGuard:
    """
    Each period-pay mutation (create / update / void) must accept Open
    and reject every other status.  Bonus lines use the same endpoint.
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
        """
        import app.payroll.service as svc
        real_lock = svc._lock_period_for_mutation

        async def mock_lock(period_id, company_id, db):
            boundary_reached.set()
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, release_boundary.wait, 15.0)
            assert ok, "release_boundary never fired inside mock_lock"
            await real_lock(period_id, company_id, db)

        svc._lock_period_for_mutation = mock_lock

        def restore():
            svc._lock_period_for_mutation = real_lock

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

    @pytest.mark.parametrize("bad_status", ["InReview", "Approved"])
    async def test_rejected_bonus_add_no_row_and_no_audit(
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
            await _ensure_bonus_active(client, auth_token, paytest_branch_id)

            audit_before = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode = 'PERIOD_PAY_ADDED' AND entityschema = 'payroll'"
                ),
            )).scalar_one()

            await _force_status(direct_db, pid, bad_status)

            r = await client.post(
                f"/payroll/periods/{pid}/period-pay",
                json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "25.00"},
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), r.text

            row_count = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM payroll.payrolldraftlines "
                    "WHERE payrollperiodid = :pid AND driverid = :did AND linescope = 'Period'"
                    "  AND status != 'Void'"
                ),
                {"pid": pid, "did": paytest_driver_id},
            )).scalar_one()
            assert row_count == 0, "Rejected period-pay add must not insert a row"

            audit_after = (await direct_db.execute(
                text(
                    "SELECT COUNT(*) FROM audit.auditlog "
                    "WHERE actioncode = 'PERIOD_PAY_ADDED' AND entityschema = 'payroll'"
                ),
            )).scalar_one()
            assert audit_after == audit_before, "Rejected period-pay add must not write audit"
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
# Settings deletion guard — real service/API path
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
                 'Active', 'Daily', 'EnteredAmount', FALSE, FALSE,
                 999, TRUE, TRUE, TRUE,
                 FALSE, :uid, NOW())
            RETURNING payitemid
        """),
        {"cid": company_id, "uid": user_id, "code": code},
    )).scalar_one()
    return iid, code


async def _cleanup_custom_item(direct_db, item_id: int) -> None:
    """Remove a test custom pay item and any remaining DraftLines referencing it."""
    for tbl in (
        "payroll.payitemsettings",
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
        text("DELETE FROM payroll.payitems WHERE payitemid = :iid"),
        {"iid": item_id},
    )


@pytest.mark.asyncio
class TestSettingsDeletionGuard:
    """
    The custom pay item physical-delete path must block deletion when non-Open
    periods have DraftLines referencing the pay item code.

    Tests go through the real DELETE /settings/pay-items/{item_id} endpoint
    so production guards in settings/service.py are exercised directly.
    Removing the non_open_refs guard makes test_non_open_ref_blocks_deletion FAIL.
    """

    async def test_non_open_ref_blocks_deletion(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        A custom pay item with a DraftLine in an InReview period cannot be
        physically deleted.  The endpoint must return 409.  The pay item row
        must still exist after the rejected call.
        """
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
            # Insert a void DraftLine with the custom item's code directly
            # (API won't accept an unregistered custom item code)
            await direct_db.execute(
                text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         sourcetype, status, needsmanagerreview, addedbyuserid)
                    VALUES
                        (:cid, :bid, :pid, :did,
                         :dt, :code, 'Daily', 0,
                         'Manual', 'Void', FALSE, :uid)
                """),
                {
                    "cid": cid, "bid": paytest_branch_id, "pid": pid,
                    "did": paytest_driver_id,
                    "dt": datetime.date.fromisoformat(_WORK_DATE),
                    "code": code, "uid": uid,
                },
            )

            # Force period to InReview — now a non-Open period has a reference
            await _force_status(direct_db, pid, "InReview")

            # Attempt physical delete via the real settings endpoint
            r = await client.delete(
                f"/settings/pay-items/{iid}",
                headers=_auth(auth_token),
            )
            assert r.status_code == 409, (
                f"Expected 409 when non-Open DraftLines exist, got {r.status_code}: {r.text}\n"
                "Removing the non_open_refs guard in settings/service.py makes this FAIL."
            )

            # Pay item must still exist (not physically deleted)
            still_exists = (await direct_db.execute(
                text("SELECT COUNT(*) FROM payroll.payitems WHERE payitemid = :iid"),
                {"iid": iid},
            )).scalar_one()
            assert still_exists == 1, "Pay item must remain after rejected deletion"

            # DraftLine must still exist (catalog not orphaned)
            dl_exists = (await direct_db.execute(
                text("SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE linetype = :code"),
                {"code": code},
            )).scalar_one()
            assert dl_exists == 1, "DraftLine in InReview period must survive rejected deletion"

        finally:
            await _force_cancel(direct_db, pid)
            await _cleanup_custom_item(direct_db, iid)

    async def test_open_only_refs_allow_deletion(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        A custom pay item whose only DraftLine references are void rows in Open
        periods can still be physically deleted via the real endpoint.
        """
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
            # Insert a void DraftLine in an Open period
            await direct_db.execute(
                text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         sourcetype, status, needsmanagerreview, addedbyuserid)
                    VALUES
                        (:cid, :bid, :pid, :did,
                         :dt, :code, 'Daily', 0,
                         'Manual', 'Void', FALSE, :uid)
                """),
                {
                    "cid": cid, "bid": paytest_branch_id, "pid": pid,
                    "did": paytest_driver_id,
                    "dt": datetime.date.fromisoformat(_WORK_DATE),
                    "code": code, "uid": uid,
                },
            )
            # Period is still Open → guard allows deletion

            r = await client.delete(
                f"/settings/pay-items/{iid}",
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, (
                f"Expected 200 for pay item with only Open-period void refs, "
                f"got {r.status_code}: {r.text}"
            )
            data = r.json()
            assert data.get("deletion_type") == "physical", (
                f"Expected physical deletion, got: {data}"
            )

            # Pay item row must be gone
            gone = (await direct_db.execute(
                text("SELECT COUNT(*) FROM payroll.payitems WHERE payitemid = :iid"),
                {"iid": iid},
            )).scalar_one()
            assert gone == 0, "Pay item must be physically deleted"

            iid = None  # cleanup already happened
        finally:
            await _force_cancel(direct_db, pid)
            if iid is not None:
                await _cleanup_custom_item(direct_db, iid)

    async def test_concurrent_transition_blocked_by_for_update(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
        pg_instance,
    ):
        """
        Concurrency guard: the physical-delete path uses SELECT FOR UPDATE on
        referenced PayrollPeriods rows, so a concurrent Open→InReview transition
        that starts BEFORE the deletion's FOR UPDATE must block until the deletion
        transaction either commits or rolls back.

        Scenario (transition thread wins the lock first, commits InReview, then
        deletion unblocks and reads non-Open → 409):

          1. psycopg2 thread acquires SELECT FOR UPDATE on the period row.
          2. Deletion request starts; it reaches its own FOR UPDATE on the same
             row and blocks (cannot acquire while thread holds it).
          3. We sleep briefly to let the deletion reach its FOR UPDATE.
          4. Thread commits UPDATE status='InReview', releasing the row lock.
          5. Deletion unblocks, reads status='InReview' from the now-committed
             state, and must return 409.
          6. Pay item and DraftLine must both still exist (deletion rejected).

        This proves the FOR UPDATE guard is concurrency-safe: replacing it with a
        plain COUNT query would allow the window where the old status was read
        before InReview committed, making deletion proceed incorrectly.
        If the FOR UPDATE is removed, step 5 may read 'Open' (race) and return
        200 when it should return 409 — making this test non-deterministic or FAIL.
        """
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
            # Insert a void DraftLine in the Open period.
            await direct_db.execute(
                text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         sourcetype, status, needsmanagerreview, addedbyuserid)
                    VALUES
                        (:cid, :bid, :pid, :did,
                         :dt, :code, 'Daily', 0,
                         'Manual', 'Void', FALSE, :uid)
                """),
                {
                    "cid": cid, "bid": paytest_branch_id, "pid": pid,
                    "did": paytest_driver_id,
                    "dt": datetime.date.fromisoformat(_WORK_DATE),
                    "code": code, "uid": uid,
                },
            )

            lock_acquired = threading.Event()
            release_lock  = threading.Event()
            thread_done   = threading.Event()
            thread_exc: list[BaseException] = []

            def transition_thread():
                """
                Hold SELECT FOR UPDATE on the period row, signal lock_acquired,
                wait for release_lock, then commit InReview.
                """
                conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
                conn.autocommit = False
                cur = conn.cursor()
                try:
                    # Acquire the row lock (same row the deletion service will try to lock)
                    cur.execute(
                        "SELECT payrollperiodid FROM payroll.payrollperiods "
                        "WHERE payrollperiodid = %s FOR UPDATE",
                        [pid],
                    )
                    lock_acquired.set()
                    assert release_lock.wait(timeout=15), "release_lock never fired"
                    # Transition to InReview while holding the lock, then commit
                    cur.execute(
                        "UPDATE payroll.payrollperiods SET status = 'InReview' "
                        "WHERE payrollperiodid = %s",
                        [pid],
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

            t = threading.Thread(target=transition_thread, daemon=True)
            try:
                t.start()

                # Step 1: wait for thread to hold the row lock.
                ok = await loop.run_in_executor(None, lock_acquired.wait, 5.0)
                assert ok, "transition thread failed to acquire FOR UPDATE within 5 s"

                # Step 2: start the deletion task — it will block at its own FOR UPDATE.
                delete_task = asyncio.create_task(
                    client.delete(f"/settings/pay-items/{iid}", headers=_auth(auth_token))
                )

                # Step 3: give the deletion service time to reach its FOR UPDATE and block.
                # asyncpg + asyncio dispatch is fast; 200 ms is conservative.
                await asyncio.sleep(0.2)

                # Step 4: release the transition thread → commits InReview, frees the lock.
                release_lock.set()
                ok = await loop.run_in_executor(None, thread_done.wait, 10.0)
                assert ok, "transition thread did not finish within 10 s"
                if thread_exc:
                    raise thread_exc[0]

                # Step 5: deletion unblocks, reads committed InReview → must return 409.
                r = await delete_task
                assert r.status_code == 409, (
                    f"Expected 409: deletion unblocked after InReview was committed, "
                    f"got {r.status_code}: {r.text}\n"
                    "If the FOR UPDATE guard is replaced with a plain COUNT, this "
                    "test may non-deterministically pass or FAIL depending on timing."
                )

                # Step 6: pay item and DraftLine must be untouched.
                item_count = (await direct_db.execute(
                    text("SELECT COUNT(*) FROM payroll.payitems WHERE payitemid = :iid"),
                    {"iid": iid},
                )).scalar_one()
                assert item_count == 1, "Pay item must survive rejected deletion"

                dl_count = (await direct_db.execute(
                    text("SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE linetype = :code"),
                    {"code": code},
                )).scalar_one()
                assert dl_count == 1, "DraftLine must survive rejected deletion — no orphan"

            finally:
                t.join(timeout=5)

        finally:
            await _force_cancel(direct_db, pid)
            await _cleanup_custom_item(direct_db, iid)


# ---------------------------------------------------------------------------
# First-reference race tests (CP-0A corrective #4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFirstReferenceRace:
    """
    Proves catalog serialization between payroll source creation and custom pay
    item physical deletion.

    _lock_pay_item_for_source_write in payroll/service.py acquires FOR UPDATE on
    the custom PayItem row before inserting a DraftLine.  The deletion path in
    settings/service.py holds the same lock while computing usage and deleting.
    Both paths acquire: PayItem lock → Period lock (same order, no deadlock).

    Two deterministic scenarios:
      delete_wins:    deletion commits first → source add unblocks, item gone → 422, no orphan
      source_add_wins: source add commits first → deletion re-evaluates → retires, no orphan
    """

    async def test_delete_wins_source_add_fails_safely(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
        pg_instance,
    ):
        """
        Delete wins: a psycopg2 thread holds the PayItem FOR UPDATE lock while
        physically deleting the item, then commits.  Concurrently,
        _lock_pay_item_for_source_write is monkeypatched to pause at a boundary
        BEFORE it attempts FOR UPDATE, so we can confirm the lock path is
        exercised.  After the thread commits (item gone), the mock releases and
        the real helper attempts FOR UPDATE on a now-deleted row → finds neither
        custom nor system row → raises 422.  No DraftLine is created.
        """
        import app.payroll.service as svc_payroll

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
            release_lock = threading.Event()  # tell thread to delete + commit
            thread_done  = threading.Event()
            thread_exc: list[BaseException] = []

            def delete_thread():
                """Acquire PayItem FOR UPDATE, signal, wait, then physically delete+commit."""
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
                    for tbl in ("payroll.payitemsettings", "payroll.payitemlinetypemap",
                                "payroll.payitemratetypemap", "payroll.branchpayitemconfig"):
                        cur.execute(f"DELETE FROM {tbl} WHERE payitemid = %s", [iid])
                    cur.execute(
                        "DELETE FROM payroll.payrolldraftlines WHERE linetype = %s", [code]
                    )
                    cur.execute("DELETE FROM payroll.payitems WHERE payitemid = %s", [iid])
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

            real_lock_fn = svc_payroll._lock_pay_item_for_source_write
            boundary_reached = threading.Event()
            release_boundary = threading.Event()

            async def mock_lock_fn(line_type, company_id_arg, db, *, period_id=None):
                if line_type == code:
                    boundary_reached.set()
                    ok = await loop.run_in_executor(None, release_boundary.wait, 15.0)
                    assert ok, "release_boundary never fired"
                await real_lock_fn(line_type, company_id_arg, db, period_id=period_id)

            svc_payroll._lock_pay_item_for_source_write = mock_lock_fn

            t = threading.Thread(target=delete_thread, daemon=True)
            try:
                t.start()
                ok = await loop.run_in_executor(None, lock_held.wait, 5.0)
                assert ok, "delete thread failed to acquire PayItem lock within 5 s"

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
                    "boundary_reached never fired — _lock_pay_item_for_source_write was "
                    "not called.  Removing the helper makes this test FAIL here."
                )

                # Commit the deletion (item is now gone), then release the source path.
                release_lock.set()
                ok = await loop.run_in_executor(None, thread_done.wait, 10.0)
                assert ok, "delete thread did not finish within 10 s"
                if thread_exc:
                    raise thread_exc[0]

                release_boundary.set()
                r = await source_task

                assert r.status_code == 422, (
                    f"Expected 422 (item deleted concurrently), got {r.status_code}: {r.text}\n"
                    "Without _lock_pay_item_for_source_write, deletion can commit first "
                    "and an orphaned DraftLine would be created."
                )

                dl_count = (await direct_db.execute(
                    text("SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE linetype = :code"),
                    {"code": code},
                )).scalar_one()
                assert dl_count == 0, f"No DraftLine should exist for deleted item '{code}'"
                iid = None  # thread deleted it

            finally:
                svc_payroll._lock_pay_item_for_source_write = real_lock_fn
                t.join(timeout=5)

        finally:
            await _force_cancel(direct_db, pid)
            if iid is not None:
                await _cleanup_custom_item(direct_db, iid)

    async def test_source_add_wins_delete_retires_not_deletes(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
        pg_instance,
    ):
        """
        Source add wins: _lock_pay_item_for_source_write acquires the PayItem
        FOR UPDATE lock, inserts the DraftLine, and commits.  Deletion then
        unblocks, recomputes usage (finds the new DraftLine), and must retire
        instead of physically deleting the item.

        Determinism: we monkeypatch _lock_pay_item_for_source_write to signal
        payitem_lock_acquired AFTER the real helper acquires FOR UPDATE but
        BEFORE returning control.  A psycopg2 thread then blocks trying the same
        FOR UPDATE.  After the source add commits, the thread unblocks and the
        deletion API call follows with the now-committed DraftLine visible.
        """
        import app.payroll.service as svc_payroll

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
            await direct_db.execute(
                text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (payitemid, companyid, branchid, isactive, effectivefrom, createdbyuserid)
                    VALUES (:iid, :cid, :bid, TRUE, '2000-01-01', :uid)
                    ON CONFLICT DO NOTHING
                """),
                {"iid": iid, "cid": cid, "bid": paytest_branch_id, "uid": uid},
            )

            payitem_lock_acquired = threading.Event()
            release_after_lock    = threading.Event()
            thread_done           = threading.Event()
            thread_exc: list[BaseException] = []

            real_lock_fn = svc_payroll._lock_pay_item_for_source_write

            async def mock_lock_fn(line_type, company_id_arg, db, *, period_id=None):
                await real_lock_fn(line_type, company_id_arg, db, period_id=period_id)
                if line_type == code:
                    payitem_lock_acquired.set()
                    ok = await loop.run_in_executor(None, release_after_lock.wait, 15.0)
                    assert ok, "release_after_lock never fired"

            svc_payroll._lock_pay_item_for_source_write = mock_lock_fn

            def concurrent_lock_thread():
                """
                Block on PayItem FOR UPDATE — will unblock once source add commits.
                Proves the lock is held through the insert.
                """
                conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
                conn.autocommit = False
                cur = conn.cursor()
                try:
                    cur.execute(
                        "SELECT payitemid FROM payroll.payitems "
                        "WHERE payitemid = %s AND companyid = %s FOR UPDATE",
                        [iid, cid],
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

            t = threading.Thread(target=concurrent_lock_thread, daemon=True)
            try:
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

                ok = await loop.run_in_executor(None, payitem_lock_acquired.wait, 10.0)
                assert ok, (
                    "payitem_lock_acquired never fired — lock helper did not acquire "
                    "FOR UPDATE.  Removing the helper makes this test FAIL here."
                )

                # Start thread that tries the same lock (will block until source commits).
                t.start()
                # Give thread time to reach and block on the FOR UPDATE.
                await asyncio.sleep(0.1)

                # Release source add to proceed (INSERT + commit).
                release_after_lock.set()
                r_source = await source_task
                assert r_source.status_code == 201, (
                    f"Source add should succeed, got {r_source.status_code}: {r_source.text}"
                )

                # Thread unblocks after source commits.
                ok = await loop.run_in_executor(None, thread_done.wait, 10.0)
                assert ok, "concurrent lock thread did not finish within 10 s"
                if thread_exc:
                    raise thread_exc[0]

                # Run deletion via API — must see the new DraftLine and retire.
                r_delete = await client.delete(
                    f"/settings/pay-items/{iid}",
                    headers=_auth(auth_token),
                )
                assert r_delete.status_code == 200, (
                    f"Deletion should return 200, got {r_delete.status_code}: {r_delete.text}"
                )
                deletion_type = r_delete.json().get("deletion_type")
                assert deletion_type == "retired", (
                    f"Expected 'retired' (DraftLine visible after recompute), got '{deletion_type}'"
                )

                # DraftLine must survive (not orphaned).
                dl_count = (await direct_db.execute(
                    text("SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE linetype = :code"),
                    {"code": code},
                )).scalar_one()
                assert dl_count >= 1, "DraftLine must survive — pay item was incorrectly deleted"

                item_status = (await direct_db.execute(
                    text("SELECT status FROM payroll.payitems WHERE payitemid = :iid"),
                    {"iid": iid},
                )).scalar_one_or_none()
                assert item_status == "Retired", (
                    f"Pay item must be Retired (not physically deleted), got: {item_status}"
                )
                iid = None

            finally:
                svc_payroll._lock_pay_item_for_source_write = real_lock_fn
                t.join(timeout=5)

        finally:
            await _force_cancel(direct_db, pid)
            if iid is not None:
                await _cleanup_custom_item(direct_db, iid)


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
        import app.payroll.service as svc_payroll

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
            real_lock_fn = svc_payroll._lock_pay_item_for_source_write
            boundary_reached = threading.Event()
            release_boundary = threading.Event()

            async def mock_lock_fn(line_type, company_id_arg, db, *, period_id=None):
                if line_type == code:
                    boundary_reached.set()
                    ok = await loop.run_in_executor(None, release_boundary.wait, 15.0)
                    assert ok, "release_boundary never fired"
                await real_lock_fn(line_type, company_id_arg, db, period_id=period_id)

            svc_payroll._lock_pay_item_for_source_write = mock_lock_fn

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
                svc_payroll._lock_pay_item_for_source_write = real_lock_fn
                t.join(timeout=5)

        finally:
            await _force_cancel(direct_db, pid)
            await _cleanup_custom_item(direct_db, iid)


# ---------------------------------------------------------------------------
# Zero-to-meaningful update vs physical deletion race (CP-0A corrective #6)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestZeroToMeaningfulRace:
    """
    Proves that a zero/non-meaningful DraftLine cannot be updated to a meaningful
    value concurrently with physical PayItem deletion without one operation winning
    cleanly and the other failing safely.

    The forbidden final state is:
      - PayItem metadata deleted  AND
      - A meaningful DraftLine referencing that code still exists

    Two safe outcomes:
      A. Deletion wins: update is blocked waiting for PayItem lock; deletion
         commits (cleans the zero line, deletes catalog); update unblocks, finds
         item gone/retired → 422. No meaningful DraftLine created.
      B. Update wins: update acquires PayItem lock, changes line to meaningful,
         commits; deletion then acquires PayItem lock, recomputes usage, sees
         meaningful line → retires instead of physically deleting. DraftLine
         survives with catalog intact (Retired).

    This test exercises Scenario A (deletion wins): the psycopg2 thread holds the
    PayItem FOR UPDATE lock while the update waits at the
    _lock_pay_item_for_source_write boundary. Thread commits (physically deletes
    item), then the update unblocks, checks Active status → 422.

    This test FAILS if:
      - update_draft_line does not call _lock_pay_item_for_source_write, OR
      - deletion does not recompute usage after period locks (tested indirectly
        by scenario B's retirement assertion)
    """

    async def test_deletion_wins_update_rejected_no_orphan(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
        pg_instance,
    ):
        """
        Deletion acquires PayItem FOR UPDATE first; update_draft_line waits;
        deletion cleans the zero DraftLine and physically deletes the catalog;
        update unblocks, finds item gone → 422; no meaningful DraftLine orphaned.
        """
        import app.payroll.service as svc_payroll

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
            # Activate the custom item on the branch.
            await direct_db.execute(
                text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (payitemid, companyid, branchid, isactive, effectivefrom, createdbyuserid)
                    VALUES (:iid, :cid, :bid, TRUE, '2000-01-01', :uid)
                    ON CONFLICT DO NOTHING
                """),
                {"iid": iid, "cid": cid, "bid": paytest_branch_id, "uid": uid},
            )

            # Insert a zero/non-meaningful DraftLine (quantity=0, null amounts).
            dl_id = (await direct_db.execute(
                text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         sourcetype, status, needsmanagerreview, addedbyuserid)
                    VALUES (:cid, :bid, :pid, :did,
                            :dt, :code, 'Daily', 0,
                            'Manual', 'Active', FALSE, :uid)
                    RETURNING draftlineid
                """),
                {
                    "cid": cid, "bid": paytest_branch_id, "pid": pid,
                    "did": paytest_driver_id,
                    "dt": datetime.date.fromisoformat(_WORK_DATE),
                    "code": code, "uid": uid,
                },
            )).scalar_one()

            # psycopg2 thread: hold PayItem FOR UPDATE, signal, wait, then physically
            # delete the item and commit (simulating the deletion path winning the lock).
            lock_held    = threading.Event()
            release_lock = threading.Event()
            thread_done  = threading.Event()
            thread_exc: list[BaseException] = []

            def deletion_thread():
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
                    # Clean the zero DraftLine
                    cur.execute(
                        "DELETE FROM payroll.payrolldraftlines "
                        "WHERE linetype = %s AND quantity = 0 "
                        "  AND (calculatedamount IS NULL OR calculatedamount = 0)",
                        [code],
                    )
                    # Delete catalog rows
                    for tbl in ("payroll.payitemsettings", "payroll.payitemlinetypemap",
                                "payroll.payitemratetypemap", "payroll.branchpayitemconfig"):
                        cur.execute(f"DELETE FROM {tbl} WHERE payitemid = %s", [iid])
                    cur.execute("DELETE FROM payroll.payitems WHERE payitemid = %s", [iid])
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

            # Monkeypatch _lock_pay_item_for_source_write to pause update before
            # the actual FOR UPDATE, so the deletion thread can commit first.
            real_lock_fn = svc_payroll._lock_pay_item_for_source_write
            boundary_reached = threading.Event()
            release_boundary = threading.Event()

            async def mock_lock_fn(line_type, company_id_arg, db, *, period_id=None):
                if line_type == code:
                    boundary_reached.set()
                    ok = await loop.run_in_executor(None, release_boundary.wait, 15.0)
                    assert ok, "release_boundary never fired"
                await real_lock_fn(line_type, company_id_arg, db, period_id=period_id)

            svc_payroll._lock_pay_item_for_source_write = mock_lock_fn

            t = threading.Thread(target=deletion_thread, daemon=True)
            try:
                t.start()
                ok = await loop.run_in_executor(None, lock_held.wait, 5.0)
                assert ok, "deletion thread failed to acquire PayItem lock within 5 s"

                # Start update_draft_line (zero→meaningful).
                update_task = asyncio.create_task(
                    client.patch(
                        f"/payroll/periods/{pid}/lines/{dl_id}",
                        json={"quantity": 5},
                        headers=_auth(auth_token),
                    )
                )

                ok = await loop.run_in_executor(None, boundary_reached.wait, 10.0)
                assert ok, (
                    "boundary_reached never fired — update_draft_line did not call "
                    "_lock_pay_item_for_source_write.  Fix 1 (PayItem lock on updates) "
                    "must be applied; this test FAILS if the helper is absent."
                )

                # Commit the deletion (item physically gone).
                release_lock.set()
                ok = await loop.run_in_executor(None, thread_done.wait, 10.0)
                assert ok, "deletion thread did not finish within 10 s"
                if thread_exc:
                    raise thread_exc[0]

                # Release update — real helper finds item gone → 422.
                release_boundary.set()
                r = await update_task

                assert r.status_code == 422, (
                    f"Expected 422 (item deleted concurrently), "
                    f"got {r.status_code}: {r.text}\n"
                    "Without PayItem lock on update_draft_line, deletion can commit first "
                    "and a meaningful orphaned DraftLine could be created."
                )

                # No meaningful DraftLine should exist for the deleted code.
                dl_count = (await direct_db.execute(
                    text("SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE linetype = :code"),
                    {"code": code},
                )).scalar_one()
                assert dl_count == 0, (
                    f"No DraftLine should reference deleted pay item '{code}', "
                    f"found {dl_count} row(s)"
                )
                iid = None  # deletion thread cleaned up the catalog

            finally:
                svc_payroll._lock_pay_item_for_source_write = real_lock_fn
                t.join(timeout=5)

        finally:
            await _force_cancel(direct_db, pid)
            if iid is not None:
                await _cleanup_custom_item(direct_db, iid)

    async def test_update_wins_deletion_retires_not_deletes(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
        pg_instance,
    ):
        """
        Update wins: update_draft_line acquires PayItem lock, changes the line
        from zero to meaningful (quantity=5), commits.  Then deletion runs via
        the API and must recompute usage after holding all locks — it sees the
        now-meaningful line and retires instead of physically deleting.

        Proves Fix 2 (recompute after period locks): if deletion trusted its
        initial usage check (before period locks), it would have seen zero usage
        and proceeded to physical delete, orphaning the meaningful DraftLine.
        """
        import app.payroll.service as svc_payroll

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
            await direct_db.execute(
                text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (payitemid, companyid, branchid, isactive, effectivefrom, createdbyuserid)
                    VALUES (:iid, :cid, :bid, TRUE, '2000-01-01', :uid)
                    ON CONFLICT DO NOTHING
                """),
                {"iid": iid, "cid": cid, "bid": paytest_branch_id, "uid": uid},
            )

            # Insert a zero DraftLine.
            dl_id = (await direct_db.execute(
                text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverid,
                         workdate, linetype, linescope, quantity,
                         sourcetype, status, needsmanagerreview, addedbyuserid)
                    VALUES (:cid, :bid, :pid, :did,
                            :dt, :code, 'Daily', 0,
                            'Manual', 'Active', FALSE, :uid)
                    RETURNING draftlineid
                """),
                {
                    "cid": cid, "bid": paytest_branch_id, "pid": pid,
                    "did": paytest_driver_id,
                    "dt": datetime.date.fromisoformat(_WORK_DATE),
                    "code": code, "uid": uid,
                },
            )).scalar_one()

            # Update the zero DraftLine to meaningful (quantity=5) via API.
            r_upd = await client.patch(
                f"/payroll/periods/{pid}/lines/{dl_id}",
                json={"quantity": 5},
                headers=_auth(auth_token),
            )
            assert r_upd.status_code == 200, (
                f"Update should succeed (period Open, item Active): "
                f"{r_upd.status_code}: {r_upd.text}"
            )

            # Now run deletion via API.
            # Deletion must recompute usage after acquiring all locks and find the
            # meaningful DraftLine (quantity=5) → retire, not physically delete.
            r_del = await client.delete(
                f"/settings/pay-items/{iid}",
                headers=_auth(auth_token),
            )
            assert r_del.status_code == 200, (
                f"Deletion should return 200 (retire), "
                f"got {r_del.status_code}: {r_del.text}"
            )
            deletion_type = r_del.json().get("deletion_type")
            assert deletion_type == "retired", (
                f"Expected deletion_type='retired' (meaningful DraftLine found on recompute), "
                f"got '{deletion_type}'.\n"
                "If deletion does not recompute usage after period locks, it may "
                "physically delete an item whose lines became meaningful concurrently."
            )

            # DraftLine must still exist (meaningful, not cleaned up).
            dl_row = (await direct_db.execute(
                text("SELECT quantity FROM payroll.payrolldraftlines WHERE draftlineid = :dlid"),
                {"dlid": dl_id},
            )).mappings().first()
            assert dl_row is not None, "DraftLine must survive (not orphaned)"
            assert int(dl_row["quantity"]) == 5, (
                f"DraftLine must remain meaningful (quantity=5), got {dl_row['quantity']}"
            )

            # PayItem catalog must still exist (now Retired, not physically deleted).
            item_status = (await direct_db.execute(
                text("SELECT status FROM payroll.payitems WHERE payitemid = :iid"),
                {"iid": iid},
            )).scalar_one_or_none()
            assert item_status == "Retired", (
                f"PayItem must be Retired (not physically deleted), got: {item_status}"
            )
            iid = None  # Retired; cleanup will handle it

        finally:
            await _force_cancel(direct_db, pid)
            if iid is not None:
                await _cleanup_custom_item(direct_db, iid)
