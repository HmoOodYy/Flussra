"""
CP-6 Integration tests ->' Review / Approve UI

Tests cover:
  1.  Review list (GET /review/items?status=Pending) shows InReview period items
  2.  Open/Draft/Approved/Locked periods are NOT in the default Pending review list
  3.  Operational reviewer can approve a clean InReview period (->'Approved)
  4.  Period becomes Approved after review approval
  5.  Approved period can then be finalized (existing finalize flow)
  6.  Post-submit live NeedsManagerReview drift does not replace the submitted snapshot
  7.  Return (EditRequested) works ->' period goes to Open
  8.  DRIVER/Self user is blocked from GET /review/items (403)
  9.  Branch-scoped user cannot approve another branch's period (403)
  10. AllCompanyBranches user can review periods from any branch
  11. No NMR lines = no blocker (Approve proceeds)
  12. Existing CP-0 through CP-5 payroll/review tests still pass

Isolation: all periods use dates in 2091 to avoid conflicts with other test suites.
"""
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text as _text

from tests.builders.access import (
    create_company_role_with_permissions,
    create_user_with_role_token,
    get_company_role_id,
)
from tests.db_state import FINALIZED_HISTORY_TRIGGERS, suspended_test_triggers
from tests.ownership import delete_period_and_children, delete_user_access_state

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PERIOD_START = "2095-01-06"
PERIOD_END   = "2095-01-12"
WORK_DATE    = "2095-01-07"


@pytest_asyncio.fixture(scope="session")
async def paytest_branch_id(session_db_conn) -> int:
    """Use a branch isolated from shared PAYTEST workflow slots."""
    row = (await session_db_conn.execute(_text("""
        INSERT INTO core.branches
            (companyid, branchcode, branchname, status, isdefault)
        VALUES (1, :code, :name, 'Active', FALSE)
        RETURNING branchid
    """), {
        "code": f"CP6_{uuid4().hex}",
        "name": f"CP6 isolated {uuid4().hex[:8]}",
    })).scalar_one()
    return int(row)


@pytest_asyncio.fixture(scope="session")
async def paytest_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
) -> int:
    """Create the CP6 driver on the isolated CP6 branch."""
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id": paytest_branch_id,
            "full_name": "CP6 Isolated Driver",
            "driver_code": f"CP6-D-{uuid4().hex[:10]}",
        },
        headers=auth(auth_token),
    )
    assert resp.status_code == 201, f"CP6 driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_active_periods(
    client: httpx.AsyncClient, token: str, branch_id: int,
) -> None:
    headers = auth(token)
    # InReview and Approved are protected workflow states. Callers that own
    # those periods must use exact-ID test teardown, not a forbidden API move.
    for s in ("Draft", "Open"):
        resp = await client.get(
            "/payroll/periods",
            params={"branch_id": branch_id, "status": s},
            headers=headers,
        )
        assert resp.status_code == 200, f"List {s} periods for cleanup failed: {resp.text}"
        for p in resp.json():
            cancelled = await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"},
                headers=headers,
            )
            assert cancelled.status_code == 200, (
                f"Cancel period {p['payroll_period_id']} failed: {cancelled.text}"
            )


@pytest_asyncio.fixture
async def cp6_owned_branch_pair(direct_db):
    branch_ids = []
    try:
        for label in ("permitted", "unpermitted"):
            token = uuid4().hex
            branch_id = (await direct_db.execute(
                _text("""
                    INSERT INTO core.branches
                        (companyid, branchcode, branchname, status, isdefault)
                    VALUES (1, :code, :name, 'Active', FALSE)
                    RETURNING branchid
                """), {"code": f"CP6_{token[:12]}", "name": f"CP6 {label} {token[:8]}"},
            )).scalar_one()
            branch_ids.append(int(branch_id))
        yield tuple(branch_ids)
    finally:
        if branch_ids:
            residue = (await direct_db.execute(
                _text("""
                    SELECT
                        (SELECT COUNT(*) FROM payroll.payrollperiods WHERE branchid = ANY(:bids)) AS periods,
                        (SELECT COUNT(*) FROM core.drivers WHERE branchid = ANY(:bids)) AS drivers,
                        (SELECT COUNT(*) FROM payroll.branchpayitemconfig WHERE branchid = ANY(:bids)) AS item_config,
                        (SELECT COUNT(*) FROM sec.userbranchroles WHERE branchid = ANY(:bids)) AS user_roles,
                        (SELECT COUNT(*) FROM review.managerreviewitems WHERE branchid = ANY(:bids)) AS review_items
                """), {"bids": branch_ids},
            )).mappings().one()
            assert all(value == 0 for value in residue.values()), (
                f"CP6 owned branches retained business state: {dict(residue)}"
            )
            await direct_db.execute(
                _text("DELETE FROM core.branches WHERE branchid = ANY(:bids)"), {"bids": branch_ids},
            )
            remaining = (await direct_db.execute(
                _text("SELECT COUNT(*) FROM core.branches WHERE branchid = ANY(:bids)"),
                {"bids": branch_ids},
            )).scalar_one()
            assert remaining == 0, f"CP6 branch cleanup left {remaining} owned Branch row(s)"


async def _create_owned_cp6_review_scenario(direct_db, branch_id: int, label: str) -> tuple[int, int]:
    company_id = (await direct_db.execute(
        _text("SELECT companyid FROM core.branches WHERE branchid = :bid"), {"bid": branch_id},
    )).scalar_one()
    marker = uuid4().hex
    period_id = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (:cid, :bid, 'InReview', :code, :name, 'Week', '2095-07-07', '2095-07-13')
            RETURNING payrollperiodid
        """), {
            "cid": company_id, "bid": branch_id, "code": f"CP6-{marker[:16]}",
            "name": f"CP6 {label} review {marker[:8]}",
        },
    )).scalar_one()
    review_item_id = (await direct_db.execute(
        _text("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requesttype, entityschema, entityname,
                 entityid, title, status, priority)
            VALUES (:cid, :bid, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                    :eid, :title, 'Pending', 'Normal')
            RETURNING reviewitemid
        """), {
            "cid": company_id, "bid": branch_id, "eid": str(period_id),
            "title": f"CP6 {label} review scenario",
        },
    )).scalar_one()
    return int(period_id), int(review_item_id)


async def _delete_owned_cp6_period(direct_db, period_id: int, review_item_id: int) -> None:
    decisions = (await direct_db.execute(
        _text("SELECT COUNT(*) FROM review.managerreviewdecisions WHERE reviewitemid = :rid"),
        {"rid": review_item_id},
    )).scalar_one()
    assert decisions == 0, f"Owned CP6 review item {review_item_id} unexpectedly has decisions"
    await direct_db.execute(
        _text("DELETE FROM review.managerreviewitems WHERE reviewitemid = :rid"),
        {"rid": review_item_id},
    )
    await delete_period_and_children(direct_db, period_id)
    residue = (await direct_db.execute(
        _text("""
            SELECT
                (SELECT COUNT(*) FROM payroll.payrollperiods WHERE payrollperiodid = :pid) AS periods,
                (SELECT COUNT(*) FROM review.managerreviewitems WHERE reviewitemid = :rid) AS review_items,
                (SELECT COUNT(*) FROM payroll.payrollperiodworkflowactionevidence
                 WHERE payrollperiodid = :pid) AS workflow_evidence
        """), {"pid": period_id, "rid": review_item_id},
    )).mappings().one()
    assert all(value == 0 for value in residue.values()), (
        f"CP6 owned period cleanup left residue for {period_id}: {dict(residue)}"
    )


async def _delete_owned_cp6_user(direct_db, username: str) -> None:
    rows = (await direct_db.execute(
        _text("""
            SELECT userid AS user_id FROM sec.users WHERE username = :username
            UNION
            SELECT entityid::integer AS user_id FROM audit.auditlog
            WHERE entityname = 'Users'
              AND newvaluejson::jsonb ->> 'username' = :username
        """), {"username": username},
    )).all()
    user_ids = {int(row[0]) for row in rows}
    assert len(user_ids) <= 1, f"CP6 username {username!r} resolved to multiple owned User IDs"
    if not user_ids:
        return

    user_id = user_ids.pop()
    await delete_user_access_state(direct_db, user_id)
    remaining = (await direct_db.execute(
        _text("""
            SELECT COUNT(*) FROM sec.users WHERE userid = :uid OR username = :username
        """), {"uid": user_id, "username": username},
    )).scalar_one()
    assert remaining == 0, f"CP6 owned User {user_id} remains after cleanup"


async def _create_open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str = PERIOD_START,
    end: str = PERIOD_END,
    direct_db=None,
) -> int:
    """Insert an Open period directly. CP-1D: POST requires existing Open (B1 guard)."""
    import datetime

    from sqlalchemy import text as _sqla_text
    assert direct_db is not None, "_create_open_period requires direct_db after CP-1D"
    # Cancel any existing Open so ux_payrollperiods_oneopenperbranch doesn't fire.
    await direct_db.execute(
        _sqla_text(
            "UPDATE payroll.payrollperiods SET status = 'Cancelled' "
            "WHERE branchid = :bid AND status IN ('Draft', 'Open')"
        ),
        {"bid": branch_id},
    )
    row = (await direct_db.execute(
        _sqla_text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": f"CP6-{start}", "name": f"CP6 {start}",
         "start": datetime.date.fromisoformat(start), "end": datetime.date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _advance_to_inreview(
    client: httpx.AsyncClient,
    token: str,
    pid: int,
    driver_id: int,
    work_date: str = WORK_DATE,
) -> int:
    """Advance an Open period to InReview. Returns the review_item_id."""
    headers = auth(token)
    # Ensure a non-void line exists (DailyNote is informational, always passes)
    lines = await client.get(
        f"/payroll/periods/{pid}/lines",
        params={"status": "Active"}, headers=headers,
    )
    if lines.status_code == 200 and len(lines.json()) == 0:
        r = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": driver_id, "work_date": work_date,
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
            headers=headers,
        )
        assert r.status_code == 201, f"Add line: {r.text}"

    tr = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "InReview"}, headers=headers,
    )
    assert tr.status_code == 200, f"InReview: {tr.text}"

    # Retrieve the Pending review item created by the InReview transition
    rv = await client.get("/review/items", headers=headers,
                          params={"status": "Pending"})
    assert rv.status_code == 200
    item = next(
        (i for i in rv.json()
         if i.get("entity_name") == "PayrollPeriods"
         and i.get("entity_id") == str(pid)
         and i.get("status") == "Pending"),
        None,
    )
    assert item is not None, f"No Pending review item for period {pid}"
    return item["review_item_id"]


async def _advance_to_approved_via_review(
    client: httpx.AsyncClient,
    token: str,
    pid: int,
    driver_id: int,
    work_date: str = WORK_DATE,
) -> None:
    """Full Open ->' InReview ->' Approved flow via review decide."""
    review_id = await _advance_to_inreview(client, token, pid, driver_id, work_date)
    dec = await client.post(
        f"/review/items/{review_id}/decide",
        json={"decision": "Approved"}, headers=auth(token),
    )
    assert dec.status_code == 200, f"Approve failed: {dec.text}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def hq_branch_id(session_client: httpx.AsyncClient, auth_token: str) -> int:
    resp = await session_client.get("/core/branches", headers=auth(auth_token))
    assert resp.status_code == 200
    branch = next((b for b in resp.json() if b["branch_code"] == "HQ"), None)
    assert branch is not None, "HQ branch not found"
    return branch["branch_id"]


@pytest_asyncio.fixture
async def hq_driver_id(
    session_client: httpx.AsyncClient,
    auth_token: str,
    hq_branch_id: int,
) -> int:
    """Return an existing HQ driver or create one for per-branch filter tests."""
    resp = await session_client.get(
        "/core/drivers",
        params={"branch_id": hq_branch_id},
        headers=auth(auth_token),
    )
    assert resp.status_code == 200
    drivers = resp.json()
    if drivers:
        return drivers[0]["driver_id"]
    new_d = await session_client.post(
        "/core/drivers",
        json={"branch_id": hq_branch_id, "full_name": "CP6 HQ Driver", "driver_code": "CP6HQ01"},
        headers=auth(auth_token),
    )
    assert new_d.status_code == 201, f"Create HQ driver: {new_d.text}"
    return new_d.json()["driver_id"]


async def _force_cancel_locked_periods(direct_db, branch_id: int) -> None:
    """Cancel Locked/Archived/InReview/Returned/Approved periods bypassing blocked PATCH paths."""
    # Legacy branch-scoped cleanup for older CP6 period scenarios.
    # InReview and Approved: PATCH to Cancelled is blocked in CP-1A, use direct DB.
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
              "WHERE branchid = :bid AND status IN ('InReview', 'Approved')"),
        {"bid": branch_id},
    )
    # Returned: must also clear CurrentReturnReviewItemID (pointer-consistency CHECK).
    await direct_db.execute(
        _text("UPDATE payroll.payrollperiods "
              "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
              "WHERE branchid = :bid AND status = 'Returned'"),
        {"bid": branch_id},
    )
    async with suspended_test_triggers(direct_db, FINALIZED_HISTORY_TRIGGERS):
        await direct_db.execute(
            _text("UPDATE payroll.payrollperiods SET status = 'Cancelled' "
                  "WHERE branchid = :bid AND status IN ('Locked', 'Archived')"),
            {"bid": branch_id},
        )


@pytest_asyncio.fixture
async def cp6_clean(
    session_client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    direct_db,
):
    """Run the legacy period cleanup around this module's scenarios."""
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    await _force_cancel_locked_periods(direct_db, paytest_branch_id)
    yield paytest_branch_id
    await _cancel_active_periods(session_client, auth_token, paytest_branch_id)
    await _force_cancel_locked_periods(direct_db, paytest_branch_id)


# ---------------------------------------------------------------------------
# 1 & 2. Review list ->' what appears
# ---------------------------------------------------------------------------

class TestReviewList:

    @pytest.mark.asyncio
    async def test_inreview_period_appears_in_pending_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """A period submitted to InReview creates a Pending review item in the list."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        rv = await session_client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(auth_token),
        )
        assert rv.status_code == 200
        ids = [i["review_item_id"] for i in rv.json()]
        assert review_id in ids

    @pytest.mark.asyncio
    async def test_open_period_not_in_pending_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        direct_db,
    ):
        """An Open period has no Pending review item."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)

        rv = await session_client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(auth_token),
        )
        assert rv.status_code == 200
        period_ids = {
            int(i["entity_id"])
            for i in rv.json()
            if i.get("entity_name") == "PayrollPeriods" and i.get("entity_id")
        }
        assert pid not in period_ids, "Open period should have no Pending review item"

    @pytest.mark.asyncio
    async def test_payroll_periods_endpoint_shows_inreview_status(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """GET /payroll/periods?status=InReview returns the period for the review page."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        periods = await session_client.get(
            "/payroll/periods",
            params={"status": "InReview", "branch_id": cp6_clean},
            headers=auth(auth_token),
        )
        assert periods.status_code == 200
        pids = [p["payroll_period_id"] for p in periods.json()]
        assert pid in pids

    @pytest.mark.asyncio
    async def test_open_draft_locked_not_in_inreview_filter(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        direct_db,
    ):
        """GET /payroll/periods?status=InReview does not include Open/Draft periods."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)

        periods = await session_client.get(
            "/payroll/periods",
            params={"status": "InReview", "branch_id": cp6_clean},
            headers=auth(auth_token),
        )
        assert periods.status_code == 200
        pids = [p["payroll_period_id"] for p in periods.json()]
        assert pid not in pids, "Open period must not appear in InReview filter"


# ---------------------------------------------------------------------------
# 3 & 4. Approve workflow
# ---------------------------------------------------------------------------

class TestReviewApprove:

    @pytest.mark.asyncio
    async def test_reviewer_can_approve_clean_period(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Operational user approves InReview ->' period status becomes Approved."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200, f"Approve failed: {dec.text}"
        assert dec.json()["status"] == "Approved"

        # Period must be Approved
        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.status_code == 200
        assert period_resp.json()["status"] == "Approved"

    @pytest.mark.asyncio
    async def test_approved_period_disappears_from_pending_review(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """After approval the review item is no longer Pending."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )

        rv = await session_client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(auth_token),
        )
        pending_ids = [i["review_item_id"] for i in rv.json()]
        assert review_id not in pending_ids

    @pytest.mark.asyncio
    async def test_approved_period_can_be_finalized(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        direct_db,
    ):
        """After review approval the period can be finalized (existing finalize flow).

        Uses a fresh one-off driver so the resulting Locked period does NOT
        affect the paytest_driver_id used by other test suites' pay-rule tests.
        """
        headers = auth(auth_token)

        # Create a fresh one-off driver for this test only
        drv = await session_client.post(
            "/core/drivers",
            json={
                "branch_id": cp6_clean,
                "full_name": "CP6 Finalize Driver",
                "driver_code": "CP6-FIN-001",
            },
            headers=headers,
        )
        assert drv.status_code == 201, f"Create driver: {drv.text}"
        fresh_driver_id = drv.json()["driver_id"]

        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        await _advance_to_approved_via_review(
            session_client, auth_token, pid, fresh_driver_id
        )

        fin = await session_client.post(
            f"/payroll/periods/{pid}/finalize",
            headers=headers,
        )
        assert fin.status_code == 200, f"Finalize failed: {fin.text}"
        assert fin.json()["status"] == "Locked"

        # Cleanup: period is now Locked; bypass immutability triggers to cancel.
        await _force_cancel_locked_periods(direct_db, cp6_clean)


# ---------------------------------------------------------------------------
# 6. NMR blocker
# ---------------------------------------------------------------------------

class TestReviewSnapshotBinding:

    @pytest.mark.asyncio
    async def test_approve_binds_submitted_snapshot_despite_post_submit_nmr_drift(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
        monkeypatch,
    ):
        """A post-submit live NMR flag cannot redefine the submitted packet."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)

        # Add a line before InReview
        lr = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": WORK_DATE,
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
            headers=auth(auth_token),
        )
        assert lr.status_code == 201
        line_id = lr.json()["draft_line_id"]

        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )
        snapshot_id = (await direct_db.execute(
            _text("""
                SELECT payrollcalculationsnapshotid
                FROM review.managerreviewitems
                WHERE reviewitemid = :review_id
            """),
            {"review_id": review_id},
        )).scalar_one()
        assert snapshot_id is not None
        snapshot_count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots
                WHERE payrollperiodid = :period_id
            """),
            {"period_id": pid},
        )).scalar_one()
        assert snapshot_count == 1

        # Simulate a live change after CP-4D captured the submitted packet.
        await direct_db.execute(
            _text("UPDATE payroll.payrolldraftlines SET needsmanagerreview = TRUE "
                  "WHERE draftlineid = :lid"),
            {"lid": line_id},
        )

        import app.payroll.period_calculation as period_calculation
        import app.payroll.service as payroll_service

        async def _unexpected_live_calculation(*_args, **_kwargs):
            raise AssertionError("approval must not rebuild live payroll calculations")

        # Stage B4-17: _build_live_calculation_packet's real implementation
        # now lives in app.payroll.period_calculation. app.payroll.service
        # still carries a load-bearing compatibility binding to the same
        # function (Lifecycle resolves it there). decide_review_item's call
        # graph does not reference either namespace today, so this guard
        # patches BOTH to remain a genuine "never invoke by any route"
        # assertion rather than a single-namespace guard that a future
        # accidental call through the other namespace could silently escape.
        monkeypatch.setattr(payroll_service, "_build_live_calculation_packet", _unexpected_live_calculation)
        monkeypatch.setattr(period_calculation, "_build_live_calculation_packet", _unexpected_live_calculation)

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200, f"Approval failed: {dec.text}"
        assert dec.json()["status"] == "Approved"
        assert (await direct_db.execute(
            _text("""
                SELECT payrollcalculationsnapshotid FROM review.managerreviewitems
                WHERE reviewitemid = :review_id
            """),
            {"review_id": review_id},
        )).scalar_one() == snapshot_id
        assert (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.payrollcalculationsnapshots
                WHERE payrollperiodid = :period_id
            """),
            {"period_id": pid},
        )).scalar_one() == snapshot_count

        period_resp = await session_client.get(f"/payroll/periods/{pid}", headers=auth(auth_token))
        assert period_resp.json()["status"] == "Approved"

    @pytest.mark.asyncio
    async def test_approve_succeeds_when_no_nmr_lines(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Period with no NMR lines ->' approve succeeds."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )
        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200
        assert dec.json()["status"] == "Approved"


# ---------------------------------------------------------------------------
# 7. Return / reject
# ---------------------------------------------------------------------------

class TestReviewReturn:

    @pytest.mark.asyncio
    async def test_edit_requested_returns_period_to_returned(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """CP-1A: EditRequested decision ->' period transitions from InReview to Returned."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "EditRequested", "decision_reason": "Missing data"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200, f"EditRequested failed: {dec.text}"

        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.json()["status"] == "Returned"

    @pytest.mark.asyncio
    async def test_rejected_returns_period_to_returned(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """CP-1A: Rejected decision ->' period transitions from InReview to Returned."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Rejected", "decision_reason": "Period is incorrect"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200, f"Rejected failed: {dec.text}"

        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.json()["status"] == "Returned"

    @pytest.mark.asyncio
    async def test_return_blocked_by_nmr_still_allowed(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """EditRequested is NOT blocked by NMR lines ->' reviewer can always return."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        lr = await session_client.post(
            f"/payroll/periods/{pid}/lines",
            json={"driver_id": paytest_driver_id, "work_date": WORK_DATE,
                  "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
            headers=auth(auth_token),
        )
        line_id = lr.json()["draft_line_id"]
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )
        # Force NMR=True
        await direct_db.execute(
            _text("UPDATE payroll.payrolldraftlines SET needsmanagerreview = TRUE "
                  "WHERE draftlineid = :lid"),
            {"lid": line_id},
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "EditRequested", "decision_reason": "NMR line present"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200, f"Return should succeed even with NMR: {dec.text}"
        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.json()["status"] == "Returned"


# ---------------------------------------------------------------------------
# 8. DRIVER/Self security
# ---------------------------------------------------------------------------

class TestReviewODASecurity:

    @pytest.mark.asyncio
    async def test_oda_user_blocked_from_review_list(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """DRIVER/Self user receives 403 on GET /review/items."""
        oda_token = await create_user_with_role_token(
            session_client, auth_token,
            "cp6_oda_review_user",
            await get_company_role_id(session_client, auth_token, "DRIVER"),
            scope_type="Self",
            driver_branch_id=paytest_branch_id,
        )

        rv = await session_client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(oda_token),
        )
        assert rv.status_code == 403

    @pytest.mark.asyncio
    async def test_oda_user_blocked_from_review_decide(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """DRIVER/Self user cannot decide a review item; 403 comes before data access."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        oda_token = await create_user_with_role_token(
            session_client, auth_token,
            "cp6_oda_decide_user",
            await get_company_role_id(session_client, auth_token, "DRIVER"),
            scope_type="Self",
            driver_branch_id=paytest_branch_id,
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(oda_token),
        )
        assert dec.status_code == 403

        # Period must still be InReview
        period_resp = await session_client.get(
            f"/payroll/periods/{pid}", headers=auth(auth_token)
        )
        assert period_resp.json()["status"] == "InReview"


# ---------------------------------------------------------------------------
# 9 & 10. Branch scoping
# ---------------------------------------------------------------------------

class TestReviewBranchScope:

    @pytest.mark.asyncio
    async def test_specific_branch_user_cannot_decide_other_branch_item(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        hq_branch_id: int,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """SpecificBranch user scoped to HQ cannot approve a PAYTEST period."""
        # Create and submit period on PAYTEST
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        # Create a user scoped ONLY to HQ (not PAYTEST)
        role_id = await create_company_role_with_permissions(
            session_client, auth_token,
            "CP6_HQ_Only_Role",
            ["payroll.view", "payroll.entry", "review.decide"],
        )
        hq_only_token = await create_user_with_role_token(
            session_client, auth_token,
            "cp6_hq_only_user",
            role_id,
            scope_type="SpecificBranch",
            branch_id=hq_branch_id,
        )

        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(hq_only_token),
        )
        assert dec.status_code == 403, (
            f"Cross-branch approval must be 403, got {dec.status_code}"
        )

    @pytest.mark.asyncio
    async def test_all_company_branches_user_can_approve_any_branch(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """AllCompanyBranches reviewer can approve a period on any branch."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        # admin has AllCompanyBranches scope
        dec = await session_client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved"},
            headers=auth(auth_token),
        )
        assert dec.status_code == 200

    @pytest.mark.asyncio
    async def test_specific_branch_user_can_see_own_branch_items(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """SpecificBranch user scoped to PAYTEST CAN see PAYTEST review items."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        review_id = await _advance_to_inreview(
            session_client, auth_token, pid, paytest_driver_id
        )

        role_id = await create_company_role_with_permissions(
            session_client, auth_token,
            "CP6_PAYTEST_Reviewer_Role",
            ["payroll.view", "payroll.entry", "review.decide"],
        )
        paytest_token = await create_user_with_role_token(
            session_client, auth_token,
            "cp6_paytest_reviewer",
            role_id,
            scope_type="SpecificBranch",
            branch_id=paytest_branch_id,
        )

        rv = await session_client.get(
            "/review/items",
            params={"status": "Pending"},
            headers=auth(paytest_token),
        )
        assert rv.status_code == 200
        ids = [i["review_item_id"] for i in rv.json()]
        assert review_id in ids


# ---------------------------------------------------------------------------
# 11. No manual Min/Max/Adjustment controls implied
# ---------------------------------------------------------------------------

class TestReviewNoManualAdjustment:

    @pytest.mark.asyncio
    async def test_approve_does_not_create_sys_adjustments(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_clean: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Review approval does not insert any SYS_MIN_TOPUP/SYS_MAX_CAP lines.
        Those are only created by finalize_period.  This confirms review does not
        trigger or expose any manual adjustment UI/logic.

        Note: period is left in Approved (not finalized) to avoid creating a
        Locked period on paytest_driver_id that could block pay-rule tests."""
        pid = await _create_open_period(session_client, auth_token, cp6_clean, direct_db=direct_db)
        await _advance_to_approved_via_review(
            session_client, auth_token, pid, paytest_driver_id
        )

        # No final lines should exist (finalize not called)
        agg = await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        assert agg.scalar_one() == 0, "Review approval must not insert final lines"

        # No SYS lines in draft (review does not add adjustments)
        sys_agg = await direct_db.execute(
            _text("""
                SELECT COUNT(*)
                FROM payroll.payrolldraftlines
                WHERE payrollperiodid = :pid
                  AND linetype LIKE 'SYS_%'
            """),
            {"pid": pid},
        )
        assert sys_agg.scalar_one() == 0, "Review must not add SYS_* draft lines"


# ---------------------------------------------------------------------------
# P1 ->' per-branch permission filtering: mixed-branch user sees only permitted branches
# ---------------------------------------------------------------------------

class TestReviewPerBranchPermissionFilter:
    """
    A user with payroll.view permission on one owned Branch must not see
    pending review items for a different owned Branch. The permitted_branches
    filter must scope the query to the user's authorized branches.
    """

    @pytest.mark.asyncio
    async def test_single_branch_permission_does_not_leak_other_branch_items(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        cp6_owned_branch_pair: tuple[int, int],
        direct_db,
    ):
        """A user scoped to one owned Branch sees its item and not the other."""
        permitted_branch_id, unpermitted_branch_id = cp6_owned_branch_pair
        scenarios: list[tuple[int, int]] = []
        owned_username: str | None = None
        try:
            scenarios.append(await _create_owned_cp6_review_scenario(
                direct_db, unpermitted_branch_id, "unpermitted",
            ))
            scenarios.append(await _create_owned_cp6_review_scenario(
                direct_db, permitted_branch_id, "permitted",
            ))
            unpermitted_pid, _ = scenarios[0]

            view_role_id = await get_company_role_id(
                session_client, auth_token, "PAYROLL_VIEWER_CO",
            )
            owned_username = f"cp6_view_{unpermitted_pid}"
            token = await create_user_with_role_token(
                session_client, auth_token, owned_username,
                view_role_id,
                scope_type="SpecificBranch",
                branch_id=permitted_branch_id,
            )

            response = await session_client.get("/review/items", headers=auth(token))
            assert response.status_code == 200, (
                f"Permitted user must be able to list review items: {response.text}"
            )
            item_branch_ids = {item["branch_id"] for item in response.json()}
            assert unpermitted_branch_id not in item_branch_ids, (
                f"Unpermitted branch review items leaked: got branch_ids={item_branch_ids}"
            )
            assert permitted_branch_id in item_branch_ids, (
                f"Permitted branch review items were missing: got branch_ids={item_branch_ids}"
            )
        finally:
            if owned_username is not None:
                await _delete_owned_cp6_user(direct_db, owned_username)
            for period_id, review_item_id in reversed(scenarios):
                await _delete_owned_cp6_period(direct_db, period_id, review_item_id)
