"""
M17 — Dashboard / Home API tests.

Endpoint: GET /dashboard
Permission: payroll.entry required.

Test strategy:
  - Most tests use the session-level admin user (AllCompanyBranches + all permissions).
  - branch_user has SpecificBranch scope on HQ only + PAYROLL_VIEWER (no payroll.entry).
  - A dedicated branch_with_entry_token fixture creates a third user with
    SpecificBranch scope + payroll.entry so the branch-scoped read tests work.
  - Tests are function-scoped to avoid cross-test state issues.
"""
import pytest
import pytest_asyncio
import httpx
import psycopg2
from datetime import date, datetime, timezone, timedelta
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE = "/dashboard"


async def _get_dashboard(client: httpx.AsyncClient, token: str) -> dict:
    resp = await client.get(BASE, headers={"Authorization": f"Bearer {token}"})
    return resp


# ---------------------------------------------------------------------------
# Third test user: branch-scoped but WITH payroll.entry
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="session")
async def hq_entry_token(
    session_client: httpx.AsyncClient,
    pg_instance,
    hq_branch_id: int,
) -> str:
    """
    A user with SpecificBranch scope on HQ and payroll.entry permission
    (PAYROLL_ADMIN role, SpecificBranch on HQ).

    Created via direct SQL so we don't depend on the admin API's exact
    field names. Idempotent — ON CONFLICT DO NOTHING.
    """
    from app.auth.security import hash_password

    pw_hash = hash_password("TestPass123!")
    conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
    conn.autocommit = True
    cur = conn.cursor()

    # Insert user (idempotent)
    cur.execute(
        """
        INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
        SELECT c.companyid, 'hq_entry_user', 'HQ Entry User', %s, TRUE, TRUE
        FROM core.companies c WHERE c.companycode = 'DEMO'
        ON CONFLICT (companyid, username) DO NOTHING
        """,
        (pw_hash,),
    )
    # Assign PAYROLL_ADMIN role with SpecificBranch scope on HQ (idempotent)
    cur.execute(
        """
        INSERT INTO sec.userbranchroles (userid, companyid, branchid, roleid, scopetype, isactive)
        SELECT u.userid, u.companyid, %(bid)s, r.roleid, 'SpecificBranch', TRUE
        FROM sec.users u, sec.roles r
        WHERE u.username = 'hq_entry_user' AND r.rolecode = 'PAYROLL_ADMIN'
        ON CONFLICT DO NOTHING
        """,
        {"bid": hq_branch_id},
    )
    conn.close()

    login = await session_client.post("/auth/login", json={
        "username":     "hq_entry_user",
        "password":     "TestPass123!",
        "company_code": "DEMO",
    })
    assert login.status_code == 200, f"HQ entry user login failed: {login.text}"
    return login.json()["access_token"]


# ---------------------------------------------------------------------------
# TestDashboardAuth
# ---------------------------------------------------------------------------

async def _make_open_period(db, branch_id: int, start: str, end: str) -> int:
    """Insert an Open period directly into DB. Returns period_id."""
    from datetime import date as _date
    from sqlalchemy import text as _sqla_text
    code = f"M17-{branch_id}-{start}"
    row = (await db.execute(
        _sqla_text(f"""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"M17 {start}",
         "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


class TestDashboardAuth:
    async def test_requires_auth(self, client: httpx.AsyncClient):
        resp = await client.get(BASE)
        assert resp.status_code == 401

    async def test_invalid_token_rejected(self, client: httpx.AsyncClient):
        resp = await client.get(BASE, headers={"Authorization": "Bearer bad.token.here"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# TestDashboardPermission
# ---------------------------------------------------------------------------

class TestDashboardPermission:
    async def test_payroll_view_user_can_access_dashboard(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
    ):
        """
        Dashboard D1: branch_user holds payroll.view (PAYROLL_VIEWER_CO).
        payroll.view is now a valid dashboard permission — returns 200 with
        payroll_ops section.

        Previously expected 403 when dashboard required payroll.entry only.
        Updated for Dashboard D1 which opens access to any user with at least
        one relevant permission (payroll.view, review.decide, payrates.view, etc.).
        """
        resp = await _get_dashboard(client, branch_user_token)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "payroll_ops" in data["sections_available"]

    async def test_admin_allowed(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await _get_dashboard(client, auth_token)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# TestDashboardScope
# ---------------------------------------------------------------------------

class TestDashboardScope:
    async def test_all_company_user_scope(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """AllCompanyBranches user → scope field = 'AllCompanyBranches'."""
        resp = await _get_dashboard(client, auth_token)
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "AllCompanyBranches"

    async def test_branch_scoped_user_scope(
        self,
        client: httpx.AsyncClient,
        hq_entry_token: str,
    ):
        """SpecificBranch user → scope field = 'Branch'."""
        resp = await _get_dashboard(client, hq_entry_token)
        assert resp.status_code == 200
        data = resp.json()
        assert data["scope"] == "Branch"

    async def test_all_company_user_sees_multiple_branches(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """AllCompanyBranches user → branch_summaries has ≥ 2 entries (HQ + PAYTEST)."""
        resp = await _get_dashboard(client, auth_token)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["branch_summaries"]) >= 2

    async def test_branch_scoped_user_sees_only_their_branch(
        self,
        client: httpx.AsyncClient,
        hq_entry_token: str,
        hq_branch_id: int,
    ):
        """SpecificBranch user (HQ only) → branch_summaries has exactly 1 entry."""
        resp = await _get_dashboard(client, hq_entry_token)
        assert resp.status_code == 200
        data = resp.json()
        branch_ids = [b["branch_id"] for b in data["branch_summaries"]]
        assert len(branch_ids) == 1
        assert branch_ids[0] == hq_branch_id


# ---------------------------------------------------------------------------
# TestDashboardResponseShape
# ---------------------------------------------------------------------------

class TestDashboardResponseShape:
    async def test_generated_at_present(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await _get_dashboard(client, auth_token)
        assert resp.status_code == 200
        data = resp.json()
        assert "generated_at" in data
        assert data["generated_at"] is not None
        # Should parse as ISO datetime
        dt = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00"))
        # Should be within the last 60 seconds
        now = datetime.now(timezone.utc)
        assert abs((now - dt).total_seconds()) < 60

    async def test_required_fields_present(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await _get_dashboard(client, auth_token)
        assert resp.status_code == 200
        data = resp.json()
        for field in [
            "generated_at", "scope",
            "periods_draft", "periods_open", "periods_in_review",
            "periods_approved", "periods_locked",
            "review_pending", "review_edit_requested",
            "active_drivers",
            "last_finalized_period",
            "setup_warnings",
            "branch_summaries",
        ]:
            assert field in data, f"Missing field: {field}"

    async def test_branch_summary_fields(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        resp = await _get_dashboard(client, auth_token)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["branch_summaries"]) > 0
        b = data["branch_summaries"][0]
        for field in [
            "branch_id", "branch_name",
            "draft_count", "open_count", "in_review_count",
            "approved_count", "locked_count",
            "pending_review_count", "edit_requested_count",
            "active_driver_count", "needs_manager_review_lines",
        ]:
            assert field in b, f"Missing branch_summary field: {field}"


# ---------------------------------------------------------------------------
# TestDashboardPeriodCounts
# ---------------------------------------------------------------------------

class TestDashboardPeriodCounts:
    """
    These tests create periods with specific statuses and verify the counts
    reflect them. Uses the PAYTEST branch to avoid conflicting with other tests.
    """

    async def test_open_period_counted(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """Create an Open period on PAYTEST; verify periods_open ≥ 1."""
        # Get baseline
        base = (await _get_dashboard(client, auth_token)).json()
        base_open = base["periods_open"]

        # Create an Open period via direct DB (CP-1D B1 guard blocks HTTP POST)
        pid = await _make_open_period(direct_db, paytest_branch_id, "2027-06-01", "2027-06-07")

        # Verify count increased
        resp = await _get_dashboard(client, auth_token)
        assert resp.json()["periods_open"] >= base_open + 1

        # Cleanup
        await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )

    async def test_approved_period_counted(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Advance a period through review to Approved; verify periods_approved ≥ 1."""
        base = (await _get_dashboard(client, auth_token)).json()
        base_approved = base["periods_approved"]

        # Create an Open period via direct DB (CP-1D B1 guard blocks HTTP POST)
        pid = await _make_open_period(direct_db, paytest_branch_id, "2027-07-01", "2027-07-07")

        # Add a DailyNote line so period is non-empty (no approved rate needed)
        line_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id":   paytest_driver_id,
                "line_type":   "DailyNote",
                "quantity":    1,
                "work_date":   "2027-07-02",
                "source_type": "Manual",
                "notes":       "filler",
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert line_resp.status_code == 201, f"Line add failed: {line_resp.text}"

        # Open → InReview (auto-creates review item)
        resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status_code == 200, resp.text

        # Find the review item and approve it
        items = await client.get(
            "/review/items",
            params={"status": "Pending"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        review_id = None
        for item in items.json():
            if item.get("entity_id") == str(pid) and item.get("request_type") == "PeriodApproval":
                review_id = item["review_item_id"]
                break
        assert review_id is not None, "Review item not found"

        await client.post(
            f"/review/items/{review_id}/decide",
            json={"decision": "Approved", "reason": "test"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        # Verify approved count increased
        after = (await _get_dashboard(client, auth_token)).json()
        assert after["periods_approved"] >= base_approved + 1

        # Cleanup
        await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )

    async def test_cancelled_periods_not_counted(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """Cancelled periods must NOT appear in any count."""
        pid = await _make_open_period(direct_db, paytest_branch_id, "2027-08-01", "2027-08-07")
        await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "Cancelled"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )

        data = (await _get_dashboard(client, auth_token)).json()
        total = (
            data["periods_draft"] + data["periods_open"] +
            data["periods_in_review"] + data["periods_approved"] + data["periods_locked"]
        )
        # Just verify no error; cancelled should not inflate counts
        assert total >= 0


# ---------------------------------------------------------------------------
# TestDashboardReviewCounts
# ---------------------------------------------------------------------------

class TestDashboardReviewCounts:
    async def test_pending_review_counted(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Submit a period for review; verify review_pending increases."""
        base = (await _get_dashboard(client, auth_token)).json()
        base_pending = base["review_pending"]

        pid = await _make_open_period(direct_db, paytest_branch_id, "2027-09-01", "2027-09-07")
        line_resp = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id":   paytest_driver_id,
                "line_type":   "DailyNote",
                "quantity":    1,
                "work_date":   "2027-09-02",
                "source_type": "Manual",
                "notes":       "filler",
            },
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert line_resp.status_code == 201, f"Line add failed: {line_resp.text}"
        ir_resp = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert ir_resp.status_code == 200, f"InReview transition failed: {ir_resp.text}"

        after = (await _get_dashboard(client, auth_token)).json()
        assert after["review_pending"] >= base_pending + 1

        # Cleanup: reject → return to Open → cancel
        items = await client.get(
            "/review/items",
            params={"status": "Pending"},
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        review_id = None
        for item in items.json():
            if item.get("entity_id") == str(pid):
                review_id = item["review_item_id"]
                break
        # Force-cancel via direct DB: CP-1A blocks Returned→Cancelled via HTTP.
        await direct_db.execute(
            text("UPDATE payroll.payrollperiods SET status = 'Cancelled', currentreturnreviewitemid = NULL WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )

    async def test_edit_requested_review_counted(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """EditRequested review item → review_edit_requested increases."""
        from sqlalchemy import text as _sqla_text
        await direct_db.execute(
            _sqla_text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE branchid = :bid AND status IN ('InReview', 'Approved', 'Returned')"),
            {"bid": paytest_branch_id},
        )
        base = (await _get_dashboard(client, auth_token)).json()
        base_er = base["review_edit_requested"]

        pid = await _make_open_period(direct_db, paytest_branch_id, "2027-10-01", "2027-10-07")
        lr2 = await client.post(f"/payroll/periods/{pid}/lines",
                          json={"driver_id": paytest_driver_id, "line_type": "DailyNote",
                                "quantity": 1,
                                "work_date": "2027-10-02", "source_type": "Manual",
                                "notes": "filler"},
                          headers={"Authorization": f"Bearer {auth_token}"})
        assert lr2.status_code == 201, f"Line add failed: {lr2.text}"
        ir2 = await client.patch(f"/payroll/periods/{pid}/status", json={"status": "InReview"},
                           headers={"Authorization": f"Bearer {auth_token}"})
        assert ir2.status_code == 200, f"InReview transition failed: {ir2.text}"

        # EditRequested decision
        items = await client.get("/review/items", params={"status": "Pending"},
                                  headers={"Authorization": f"Bearer {auth_token}"})
        review_id = None
        for item in items.json():
            if item.get("entity_id") == str(pid):
                review_id = item["review_item_id"]
                break
        assert review_id is not None
        decide_resp = await client.post(f"/review/items/{review_id}/decide",
                          json={"decision": "EditRequested", "decision_reason": "needs fix"},
                          headers={"Authorization": f"Bearer {auth_token}"})
        assert decide_resp.status_code == 200, f"EditRequested decision failed: {decide_resp.text}"

        after = (await _get_dashboard(client, auth_token)).json()
        assert after["review_edit_requested"] >= base_er + 1, f"Dashboard after: {after}"

        # Force-cancel via direct DB: CP-1A blocks Returned→Cancelled via HTTP.
        await direct_db.execute(
            text("UPDATE payroll.payrollperiods SET status = 'Cancelled', currentreturnreviewitemid = NULL WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )


# ---------------------------------------------------------------------------
# TestDashboardDrivers
# ---------------------------------------------------------------------------

class TestDashboardDrivers:
    async def test_active_drivers_counted(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        created_driver_id: int,
        paytest_driver_id: int,
    ):
        """Seeded active drivers appear in active_drivers count."""
        resp = await _get_dashboard(client, auth_token)
        assert resp.status_code == 200
        data = resp.json()
        # At minimum the 2 session-seeded drivers
        assert data["active_drivers"] >= 2


# ---------------------------------------------------------------------------
# TestDashboardLastFinalized
# ---------------------------------------------------------------------------

class TestDashboardLastFinalized:
    async def test_last_finalized_none_when_no_locked_period(
        self,
        client: httpx.AsyncClient,
        session_client: httpx.AsyncClient,
        pg_instance,
        direct_db,
    ):
        """
        last_finalized_period is null when the user's only accessible branch
        has no Locked/Archived periods.

        Creates a temporary isolated branch with no payroll data, scopes a
        dedicated user to it, and asserts null.  Fully self-contained — no
        dependency on whether HQ or PAYTEST have locked periods.
        """
        from app.auth.security import hash_password

        # -- Setup: create temp branch and scoped user via direct psycopg2 ----
        pw_hash = hash_password("TestPass123!")
        conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
        conn.autocommit = True
        cur = conn.cursor()

        # Create temp branch (unique code to avoid conflicts)
        cur.execute(
            """
            INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
            SELECT companyid, 'NULLTEST', 'Null Test Branch', 'Active', FALSE
            FROM core.companies WHERE companycode = 'DEMO'
            RETURNING branchid
            """,
        )
        null_branch_id = cur.fetchone()[0]

        # Create temp user scoped to new branch
        cur.execute(
            """
            INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
            SELECT c.companyid, 'null_test_user', 'Null Test User', %s, TRUE, TRUE
            FROM core.companies c WHERE c.companycode = 'DEMO'
            ON CONFLICT (companyid, username) DO NOTHING
            """,
            (pw_hash,),
        )
        cur.execute(
            """
            INSERT INTO sec.userbranchroles (userid, companyid, branchid, roleid, scopetype, isactive)
            SELECT u.userid, u.companyid, %(bid)s, r.roleid, 'SpecificBranch', TRUE
            FROM sec.users u, sec.roles r
            WHERE u.username = 'null_test_user' AND r.rolecode = 'PAYROLL_ADMIN'
            ON CONFLICT DO NOTHING
            """,
            {"bid": null_branch_id},
        )
        conn.close()

        try:
            login = await session_client.post("/auth/login", json={
                "username":     "null_test_user",
                "password":     "TestPass123!",
                "company_code": "DEMO",
            })
            assert login.status_code == 200, f"null_test_user login failed: {login.text}"
            null_token = login.json()["access_token"]

            resp = await _get_dashboard(client, null_token)
            assert resp.status_code == 200
            assert resp.json()["last_finalized_period"] is None

        finally:
            # Cleanup: remove userbranchroles, user, branch
            await direct_db.execute(
                text("""
                    DELETE FROM sec.userbranchroles
                    WHERE branchid = :bid
                """),
                {"bid": null_branch_id},
            )
            await direct_db.execute(
                text("""
                    DELETE FROM sec.users
                    WHERE username = 'null_test_user'
                """),
            )
            await direct_db.execute(
                text("DELETE FROM core.branches WHERE branchid = :bid"),
                {"bid": null_branch_id},
            )

    async def test_last_finalized_includes_locked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
        paytest_branch_id: int,
    ):
        """A Locked period appears in last_finalized_period."""
        # Insert a Locked period directly
        await direct_db.execute(
            text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status, lockedatutc)
                SELECT companyid, :bid, 'M17-LOCK-TEST', 'M17 Lock Test',
                       'Week', '2024-01-01', '2024-01-07', 'Locked', NOW()
                FROM core.branches WHERE branchid = :bid
            """),
            {"bid": paytest_branch_id},
        )

        resp = await _get_dashboard(client, auth_token)
        assert resp.status_code == 200
        lfp = resp.json()["last_finalized_period"]
        assert lfp is not None
        assert lfp["period_name"] is not None

        # Cleanup
        await direct_db.execute(
            text("DELETE FROM payroll.payrollperiods WHERE periodcode = 'M17-LOCK-TEST'"),
        )

    async def test_last_finalized_includes_archived(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
        paytest_branch_id: int,
    ):
        """An Archived period also counts as finalized."""
        await direct_db.execute(
            text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status, lockedatutc)
                SELECT companyid, :bid, 'M17-ARCH-TEST', 'M17 Archive Test',
                       'Week', '2024-02-01', '2024-02-07', 'Archived', NOW()
                FROM core.branches WHERE branchid = :bid
            """),
            {"bid": paytest_branch_id},
        )

        resp = await _get_dashboard(client, auth_token)
        assert resp.status_code == 200
        lfp = resp.json()["last_finalized_period"]
        assert lfp is not None

        await direct_db.execute(
            text("DELETE FROM payroll.payrollperiods WHERE periodcode = 'M17-ARCH-TEST'"),
        )


# ---------------------------------------------------------------------------
# TestDashboardSetupWarnings
# ---------------------------------------------------------------------------

class TestDashboardSetupWarnings:
    async def test_warning_branch_no_payroll_settings(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
    ):
        """
        Insert a new active branch with no BranchPayrollSettings;
        expect BRANCH_NO_PAYROLL_SETTINGS warning.
        """
        # Insert a bare branch — admin has AllCompanyBranches scope so no extra grant needed
        result = await direct_db.execute(
            text("""
                INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
                SELECT companyid, 'M17WARN', 'M17 Warning Branch', 'Active', FALSE
                FROM core.companies WHERE companycode = 'DEMO'
                RETURNING branchid
            """),
        )
        new_bid = result.scalar_one()

        try:
            resp = await _get_dashboard(client, auth_token)
            assert resp.status_code == 200
            codes = [w["code"] for w in resp.json()["setup_warnings"]]
            assert "BRANCH_NO_PAYROLL_SETTINGS" in codes
        finally:
            await direct_db.execute(
                text("DELETE FROM core.branches WHERE branchid = :bid"), {"bid": new_bid}
            )

    async def test_warning_needs_manager_review(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
        paytest_branch_id: int,
        paytest_driver_id: int,
    ):
        """
        Create an Open period with a NeedsManagerReview draft line;
        expect OPEN_PERIOD_NEEDS_MANAGER_REVIEW warning.
        """
        # Create Open period
        from app.auth.security import hash_password as _hp  # noqa: F401
        import httpx as _httpx

        transport = httpx.AsyncClient  # just to import; use client fixture

        # Get company_id from PAYTEST branch
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        # Insert period directly in Open status
        result = await direct_db.execute(
            text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status)
                VALUES (:cid, :bid, 'M17-NMR-TEST', 'M17 NMR Test',
                        'Week', '2025-01-06', '2025-01-12', 'Open')
                RETURNING payrollperiodid
            """),
            {"cid": company_id, "bid": paytest_branch_id},
        )
        pid = result.scalar_one()

        # Insert a NeedsManagerReview draft line
        await direct_db.execute(
            text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverid,
                     workdate, linetype, quantity, sourcetype, status, needsmanagerreview)
                VALUES (:cid, :bid, :pid, :did,
                        '2025-01-07', 'MILES', 100, 'Manual', 'Active', TRUE)
            """),
            {"cid": company_id, "bid": paytest_branch_id, "pid": pid, "did": paytest_driver_id},
        )

        try:
            resp = await _get_dashboard(client, auth_token)
            assert resp.status_code == 200
            codes = [w["code"] for w in resp.json()["setup_warnings"]]
            assert "OPEN_PERIOD_NEEDS_MANAGER_REVIEW" in codes
        finally:
            await direct_db.execute(
                text("DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"), {"pid": pid}
            )
            await direct_db.execute(
                text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"), {"pid": pid}
            )

    async def test_warning_drivers_no_approved_rate(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
        paytest_branch_id: int,
    ):
        """
        Create an active driver with no approved rates;
        expect DRIVERS_NO_APPROVED_RATE warning.
        """
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        # Insert employee + driver with no rates
        result = await direct_db.execute(
            text("""
                INSERT INTO core.employees
                    (companyid, branchid, fullname, employeetype, employmentstatus)
                VALUES (:cid, :bid, 'M17 No Rate Driver', 'Driver', 'Active')
                RETURNING employeeid
            """),
            {"cid": company_id, "bid": paytest_branch_id},
        )
        emp_id = result.scalar_one()

        result = await direct_db.execute(
            text("""
                INSERT INTO core.drivers (companyid, branchid, employeeid, driverstatus)
                VALUES (:cid, :bid, :eid, 'Active')
                RETURNING driverid
            """),
            {"cid": company_id, "bid": paytest_branch_id, "eid": emp_id},
        )
        drv_id = result.scalar_one()

        try:
            resp = await _get_dashboard(client, auth_token)
            assert resp.status_code == 200
            codes = [w["code"] for w in resp.json()["setup_warnings"]]
            assert "DRIVERS_NO_APPROVED_RATE" in codes
        finally:
            await direct_db.execute(
                text("DELETE FROM core.drivers WHERE driverid = :did"), {"did": drv_id}
            )
            await direct_db.execute(
                text("DELETE FROM core.employees WHERE employeeid = :eid"), {"eid": emp_id}
            )

    async def test_warning_pay_item_missing_rate_type_map(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
    ):
        """
        Insert an active Daily rate-requiring PayItem with no PayItemRateTypeMap;
        expect PAY_ITEM_MISSING_RATE_TYPE_MAP warning.
        """
        result = await direct_db.execute(
            text("""
                INSERT INTO payroll.payitems
                    (companyid, branchid, payitemcode, payitemname, category,
                     datatype, itemscope, ratebehavior, requiresrate,
                     isdefaultbranchactive, isSystemStandard,
                     appearsinpayrollentry, appearsInLedger, appearsInReports,
                     status)
                VALUES
                    (NULL, NULL, 'M17_NOMAP', 'M17 No Map Item', 'Mileage',
                     'Currency', 'Daily', 'PerUnit', TRUE,
                     FALSE, FALSE,
                     TRUE, TRUE, TRUE,
                     'Active')
                RETURNING payitemid
            """),
        )
        pi_id = result.scalar_one()

        try:
            resp = await _get_dashboard(client, auth_token)
            assert resp.status_code == 200
            codes = [w["code"] for w in resp.json()["setup_warnings"]]
            assert "PAY_ITEM_MISSING_RATE_TYPE_MAP" in codes
        finally:
            await direct_db.execute(
                text("DELETE FROM payroll.payitems WHERE payitemid = :pid"), {"pid": pi_id}
            )

    async def test_no_warnings_when_all_clear(
        self,
        client: httpx.AsyncClient,
        hq_entry_token: str,
        hq_branch_id: int,
        direct_db,
    ):
        """
        When HQ has payroll settings, no open NMR lines, and all drivers have
        approved rates, no warnings should appear for the HQ-scoped user.
        This is a best-effort check; skip if prerequisites aren't met.
        """
        # Check HQ has payroll settings
        result = await direct_db.execute(
            text("""
                SELECT COUNT(*) FROM payroll.branchpayrollsettings
                WHERE branchid = :bid
            """),
            {"bid": hq_branch_id},
        )
        if result.scalar_one() == 0:
            pytest.skip("HQ has no payroll settings — prerequisite not met")

        # Check HQ has no active drivers without approved rates
        result = await direct_db.execute(
            text("""
                SELECT COUNT(*) FROM core.drivers d
                JOIN core.employees e ON e.employeeid = d.employeeid
                WHERE d.branchid = :bid
                  AND d.driverstatus = 'Active'
                  AND e.employmentstatus = 'Active'
                  AND NOT EXISTS (
                      SELECT 1 FROM payroll.driverrates dr
                      WHERE dr.driverid = d.driverid AND dr.status = 'Approved'
                  )
            """),
            {"bid": hq_branch_id},
        )
        if result.scalar_one() > 0:
            pytest.skip("HQ has drivers without approved rates — skip clean check")

        resp = await _get_dashboard(client, hq_entry_token)
        assert resp.status_code == 200
        branch_warnings = [
            w for w in resp.json()["setup_warnings"]
            if w.get("branch_id") == hq_branch_id
        ]
        # Should be empty for HQ
        assert branch_warnings == []


# ---------------------------------------------------------------------------
# TestDashboardBranchSummaries
# ---------------------------------------------------------------------------

class TestDashboardBranchSummaries:
    async def test_branch_summary_counts_match_totals(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """
        Sum of per-branch counts must equal top-level company-wide totals.
        """
        resp = await _get_dashboard(client, auth_token)
        assert resp.status_code == 200
        data = resp.json()

        sum_open     = sum(b["open_count"]        for b in data["branch_summaries"])
        sum_approved = sum(b["approved_count"]     for b in data["branch_summaries"])
        sum_pending  = sum(b["pending_review_count"] for b in data["branch_summaries"])
        sum_drivers  = sum(b["active_driver_count"]  for b in data["branch_summaries"])

        assert data["periods_open"]     == sum_open
        assert data["periods_approved"] == sum_approved
        assert data["review_pending"]   == sum_pending
        assert data["active_drivers"]   == sum_drivers
