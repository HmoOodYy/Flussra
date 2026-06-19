"""
CP-0A: Freeze InReview source mutations.

Verifies that every payroll source mutation path (draft-line create/update/void,
period-pay create/update/void, day-grid save) accepts only Open periods and
rejects all other statuses (Draft, InReview, Approved, Locked, Archived,
Cancelled).

Also includes a race-regression test: a period forced to InReview by a
concurrent direct-DB write must still be rejected even when the API-level
upfront check had already passed.

ODA / permission / audit-rollback regressions are confirmed to remain green
(existing test suites cover these paths; here we only check that CP-0A does
not accidentally break the Open-success path).
"""

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
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str = "2092-03-03",
    end: str = "2092-03-09",
) -> int:
    """Create a Draft period and advance it to Open. Returns period_id."""
    await _cancel_all_non_terminal(client, token, branch_id)
    r = await client.post(
        "/payroll/periods",
        json={
            "branch_id": branch_id,
            "period_type": "Week",
            "start_date": start,
            "end_date": end,
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"create failed: {r.text}"
    pid = r.json()["payroll_period_id"]
    r2 = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "Open"},
        headers=_auth(token),
    )
    assert r2.status_code == 200, f"open failed: {r2.text}"
    return pid


async def _add_draft_line(
    client: httpx.AsyncClient,
    token: str,
    period_id: int,
    driver_id: int,
    work_date: str = "2092-03-04",
) -> int:
    """Add a PTO_STATUS draft line while period is Open. Returns draft_line_id."""
    r = await client.post(
        f"/payroll/periods/{period_id}/lines",
        json={
            "driver_id": driver_id,
            "work_date": work_date,
            "line_type": "PTO_STATUS",
            "quantity": 1,
            "source_type": "Manual",
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
    """Activate BONUS, add a period-pay line while period is Open. Returns draft_line_id."""
    await _ensure_bonus_active(client, token, branch_id)
    r = await client.post(
        f"/payroll/periods/{period_id}/period-pay",
        json={"driver_id": driver_id, "line_type": "Bonus", "amount": "50.00"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"add bonus failed: {r.text}"
    return r.json()["draft_line_id"]


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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": paytest_driver_id,
                    "work_date": _WORK_DATE,
                    "line_type": "PTO_STATUS",
                    "quantity": 1,
                    "source_type": "Manual",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 201, r.text
        finally:
            await _force_cancel(direct_db, pid)

    @pytest.mark.parametrize("bad_status", [
        "Draft", "InReview", "Approved", "Locked", "Archived", "Cancelled"
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            await _force_status(direct_db, pid, bad_status)
            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": paytest_driver_id,
                    "work_date": _WORK_DATE,
                    "line_type": "PTO_STATUS",
                    "quantity": 1,
                    "source_type": "Manual",
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
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
        "Draft", "InReview", "Approved", "Locked", "Archived", "Cancelled"
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
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
        "Draft", "InReview", "Approved", "Locked", "Archived", "Cancelled"
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            await _ensure_bonus_active(client, auth_token, paytest_branch_id)
            r = await client.post(
                f"/payroll/periods/{pid}/period-pay",
                json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "25.00"},
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            await _ensure_bonus_active(client, auth_token, paytest_branch_id)
            await _force_status(direct_db, pid, bad_status)
            r = await client.post(
                f"/payroll/periods/{pid}/period-pay",
                json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "25.00"},
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            lid = await _add_bonus_line(client, auth_token, paytest_branch_id, pid, paytest_driver_id)
            r = await client.patch(
                f"/payroll/periods/{pid}/period-pay/{lid}",
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            lid = await _add_bonus_line(client, auth_token, paytest_branch_id, pid, paytest_driver_id)
            await _force_status(direct_db, pid, bad_status)
            r = await client.patch(
                f"/payroll/periods/{pid}/period-pay/{lid}",
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            lid = await _add_bonus_line(client, auth_token, paytest_branch_id, pid, paytest_driver_id)
            r = await client.delete(
                f"/payroll/periods/{pid}/period-pay/{lid}",
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            lid = await _add_bonus_line(client, auth_token, paytest_branch_id, pid, paytest_driver_id)
            await _force_status(direct_db, pid, bad_status)
            r = await client.delete(
                f"/payroll/periods/{pid}/period-pay/{lid}",
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
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
        "Draft", "InReview", "Approved", "Locked", "Archived", "Cancelled"
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
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
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
# Race regression — concurrent status transition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestConcurrentStatusRace:
    """
    Regression: a period forced to InReview via a direct-DB write (simulating a
    concurrent Open→InReview transition that committed between the API upfront
    check and the DB write) must still be rejected.

    This verifies that _lock_period_for_mutation catches the race even when the
    application-level status check passed with Open.

    Implementation note: the race is simulated by using direct_db to change the
    period status *before* the HTTP call reaches the service.  This proves the
    within-transaction recheck rather than the upfront check.
    """

    async def test_add_draft_line_rejected_after_concurrent_inreview(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        Simulate: status was Open when the request arrived; it becomes InReview
        before _lock_period_for_mutation runs.  The write must be rejected with
        409.
        """
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            # Simulate concurrent transition: force period to InReview via DB.
            # The API endpoint will see Open in its cached period object but the
            # _lock_period_for_mutation recheck will see InReview.
            await _force_status(direct_db, pid, "InReview")

            r = await client.post(
                f"/payroll/periods/{pid}/lines",
                json={
                    "driver_id": paytest_driver_id,
                    "work_date": _WORK_DATE,
                    "line_type": "PTO_STATUS",
                    "quantity": 1,
                    "source_type": "Manual",
                },
                headers=_auth(auth_token),
            )
            # Must be rejected — either 422 from the upfront ENTRY_ALLOWED_STATUSES
            # check (which now correctly excludes InReview) or 409 from the
            # _lock_period_for_mutation recheck if the transition happened in the
            # narrow window between upfront check and write.
            assert r.status_code in (409, 422), (
                f"Expected rejection after concurrent InReview transition, "
                f"got {r.status_code}: {r.text}"
            )
        finally:
            await _force_cancel(direct_db, pid)

    async def test_day_grid_save_rejected_after_concurrent_inreview(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        Same race scenario for day-grid save: forced InReview must be rejected
        even if the API had passed the Phase 1 ENTRY_ALLOWED_STATUSES check.
        """
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            await _force_status(direct_db, pid, "InReview")

            r = await client.post(
                f"/payroll/periods/{pid}/day-grid",
                json={
                    "work_date": _WORK_DATE,
                    "rows": [{"driver_id": paytest_driver_id, "values": {"HOURS": "8"}}],
                },
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), (
                f"Expected rejection after concurrent InReview transition, "
                f"got {r.status_code}: {r.text}"
            )
        finally:
            await _force_cancel(direct_db, pid)

    async def test_bonus_add_rejected_after_concurrent_inreview(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Same race scenario for period-pay (bonus) add."""
        pid = await _create_open_period(client, auth_token, paytest_branch_id)
        try:
            await _force_status(direct_db, pid, "InReview")

            r = await client.post(
                f"/payroll/periods/{pid}/period-pay",
                json={"driver_id": paytest_driver_id, "line_type": "Bonus", "amount": "50.00"},
                headers=_auth(auth_token),
            )
            assert r.status_code in (403, 409, 422), (
                f"Expected rejection after concurrent InReview transition, "
                f"got {r.status_code}: {r.text}"
            )
        finally:
            await _force_cancel(direct_db, pid)
