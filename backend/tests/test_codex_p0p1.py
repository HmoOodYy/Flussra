"""
Tests for Codex P0/P1 backend blocker fixes.

Covers:
  P0  - Direct PeriodApproval review item creation blocked
  P0  - decide_review_item write-back validates period existence, company, branch
  P1  - Open->InReview atomic claim (double-submit guard)
  P1  - Admin _ensure_admin inactive-user / suspended-company checks
  P1  - Dashboard payroll.entry per-branch permission filtering
  P1  - DriverPayRules finalized-period protection includes Archived
  P1  - Draft-line mutations write audit entries (+ rollback on audit failure)
  P1  - Period-pay mutations write audit entries (+ rollback on audit failure)
"""

import httpx
import psycopg2
import pytest
from sqlalchemy import text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _cancel_existing_draft(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
) -> None:
    """
    Cancel any existing Draft period on this branch so tests can create a new
    one without hitting the one-Draft-per-branch unique constraint.

    test_branch_access.py has a session-scoped fixture that creates a permanent
    Draft period on PAYTEST for the whole test session.  Without this guard,
    our tests fail when running alongside the full suite.
    """
    r = await client.get(
        "/payroll/periods",
        params={"branch_id": branch_id},
        headers=_auth(token),
    )
    if r.status_code != 200:
        return
    for p in r.json():
        if p["status"] in ("Draft", "Open"):
            await client.patch(
                f"/payroll/periods/{p['payroll_period_id']}/status",
                json={"status": "Cancelled"},
                headers=_auth(token),
            )


async def _create_open_period(
    client: httpx.AsyncClient,
    token: str,
    branch_id: int,
    start: str,
    end: str,
    db=None,
) -> int:
    """Insert an Open period directly into DB; returns period_id."""
    from datetime import date as _date
    if db is None:
        raise RuntimeError("_create_open_period requires db= since CP-1D B1 guard blocks HTTP POST")
    code = f"CP0P1-{branch_id}-{start}"
    row = (await db.execute(
        text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"CP0P1 {start}",
         "start": _date.fromisoformat(start), "end": _date.fromisoformat(end)},
    )).mappings().first()
    return row["payrollperiodid"]


async def _add_line(client, token, pid, driver_id, work_date):
    r = await client.post(
        f"/payroll/periods/{pid}/lines",
        json={"driver_id": driver_id, "line_type": "DailyNote",
              "quantity": 1, "work_date": work_date, "source_type": "Manual", "notes": "filler"},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["draft_line_id"]


async def _submit_for_review(client, token, pid):
    r = await client.patch(f"/payroll/periods/{pid}/status",
                            json={"status": "InReview"}, headers=_auth(token))
    assert r.status_code == 200, r.text


async def _find_review_item(client, token, pid):
    items = await client.get("/review/items", params={"status": "Pending"},
                              headers=_auth(token))
    for item in items.json():
        if item.get("entity_id") == str(pid) and item.get("request_type") == "PeriodApproval":
            return item["review_item_id"]
    return None


# ---------------------------------------------------------------------------
# P0: Block manual PeriodApproval creation
# ---------------------------------------------------------------------------

class TestBlockManualPeriodApproval:
    async def test_manual_period_approval_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """POST /review/items with request_type=PeriodApproval must return 422."""
        resp = await client.post(
            "/review/items",
            json={
                "branch_id":    paytest_branch_id,
                "request_type": "PeriodApproval",
                "entity_schema": "payroll",
                "entity_name":   "PayrollPeriods",
                "entity_id":     "999",
                "title":         "Forged PeriodApproval",
            },
            headers=_auth(auth_token),
        )
        assert resp.status_code == 422
        assert "PeriodApproval" in resp.json()["detail"]

    async def test_other_request_types_still_allowed(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
    ):
        """Non-PeriodApproval types can still be created manually."""
        resp = await client.post(
            "/review/items",
            json={
                "branch_id":    paytest_branch_id,
                "request_type": "Correction",
                "title":        "Manual correction item",
            },
            headers=_auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        # Cleanup
        rid = resp.json()["review_item_id"]
        await client.post(f"/review/items/{rid}/decide",
                          json={"decision": "Rejected", "reason": "cleanup"},
                          headers=_auth(auth_token))


# ---------------------------------------------------------------------------
# P0: decide_review_item write-back validates period
# ---------------------------------------------------------------------------

class TestPeriodApprovalWritebackValidation:
    async def test_cross_branch_period_approval_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        hq_branch_id: int,
        direct_db,
    ):
        """
        A PeriodApproval item created for PAYTEST branch but linked to an HQ period
        must be rejected by decide_review_item (branch mismatch).

        Strategy: directly insert an HQ period in InReview status (no real
        review item created), then insert a single forged PeriodApproval item
        on PAYTEST branch pointing to it.  The unique index only fires on
        duplicate (companyid, entityid) pending items; this is the only one.
        """
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": hq_branch_id},
        )
        company_id = result.scalar_one()

        # Insert an HQ period directly in InReview status (bypasses review-item creation)
        p_result = await direct_db.execute(
            text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status)
                VALUES (:cid, :bid, 'XBRANCH-IR-TEST', 'Cross Branch IR Test',
                        'Week', '2029-01-07', '2029-01-13', 'InReview')
                RETURNING payrollperiodid
            """),
            {"cid": company_id, "bid": hq_branch_id},
        )
        hq_pid = p_result.scalar_one()

        # Insert a forged PeriodApproval item on PAYTEST branch pointing to HQ period
        result = await direct_db.execute(
            text("""
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requestedbyuserid, requesttype,
                     entityschema, entityname, entityid, title, status, priority)
                VALUES (:cid, :bid, 1, 'PeriodApproval',
                        'payroll', 'PayrollPeriods', :eid,
                        'Cross-branch forged item', 'Pending', 'Normal')
                RETURNING reviewitemid
            """),
            {"cid": company_id, "bid": paytest_branch_id, "eid": str(hq_pid)},
        )
        forged_id = result.scalar_one()

        try:
            # Approve via the forged item — must fail: PAYTEST branch != HQ branch
            resp = await client.post(
                f"/review/items/{forged_id}/decide",
                json={"decision": "Approved", "reason": "forged"},
                headers=_auth(auth_token),
            )
            assert resp.status_code == 422
            assert "branch" in resp.json()["detail"].lower()
        finally:
            await direct_db.execute(
                text("DELETE FROM review.managerreviewitems WHERE reviewitemid = :rid"),
                {"rid": forged_id},
            )
            await direct_db.execute(
                text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": hq_pid},
            )

    async def test_nonexistent_period_entity_id_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """PeriodApproval item with entity_id pointing to nonexistent period is blocked."""
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        # Use a period ID that definitely doesn't exist (large number)
        result = await direct_db.execute(
            text("""
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requestedbyuserid, requesttype,
                     entityschema, entityname, entityid, title, status, priority)
                VALUES (:cid, :bid, 1, 'PeriodApproval',
                        'payroll', 'PayrollPeriods', '999999999',
                        'Ghost period item', 'Pending', 'Normal')
                RETURNING reviewitemid
            """),
            {"cid": company_id, "bid": paytest_branch_id},
        )
        ghost_id = result.scalar_one()

        resp = await client.post(
            f"/review/items/{ghost_id}/decide",
            json={"decision": "Approved", "reason": "forged"},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 422
        assert "does not exist" in resp.json()["detail"]

        await direct_db.execute(
            text("DELETE FROM review.managerreviewitems WHERE reviewitemid = :rid"),
            {"rid": ghost_id},
        )

    async def test_bad_entity_metadata_blocked(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """PeriodApproval item with wrong entityschema/entityname is blocked."""
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        result = await direct_db.execute(
            text("""
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requestedbyuserid, requesttype,
                     entityschema, entityname, entityid, title, status, priority)
                VALUES (:cid, :bid, 1, 'PeriodApproval',
                        'wrong_schema', 'WrongTable', '1',
                        'Bad metadata item', 'Pending', 'Normal')
                RETURNING reviewitemid
            """),
            {"cid": company_id, "bid": paytest_branch_id},
        )
        bad_id = result.scalar_one()

        resp = await client.post(
            f"/review/items/{bad_id}/decide",
            json={"decision": "Approved", "reason": "forged"},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 422
        assert "entity metadata" in resp.json()["detail"]

        await direct_db.execute(
            text("DELETE FROM review.managerreviewitems WHERE reviewitemid = :rid"),
            {"rid": bad_id},
        )


# ---------------------------------------------------------------------------
# P1: Open->InReview atomic double-submit guard
# ---------------------------------------------------------------------------

class TestOpenToInReviewConcurrency:
    async def test_double_submit_second_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        If a period is already InReview, a second concurrent submit must return 422.
        We simulate by submitting, then attempting again after manually returning
        to Open and submitting again — but more directly by checking the status guard.
        """
        await direct_db.execute(
            text("UPDATE payroll.payrollperiods SET status = 'Cancelled' WHERE branchid = :bid AND status IN ('InReview', 'Approved')"),
            {"bid": paytest_branch_id},
        )
        pid = await _create_open_period(
            client, auth_token, paytest_branch_id, "2029-02-03", "2029-02-09", db=direct_db
        )
        await _add_line(client, auth_token, pid, paytest_driver_id, "2029-02-04")
        await _submit_for_review(client, auth_token, pid)

        # Period is now InReview. Attempting Open->InReview again should fail.
        r = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 422

        # Cleanup
        rid = await _find_review_item(client, auth_token, pid)
        if rid:
            await client.post(f"/review/items/{rid}/decide",
                              json={"decision": "Rejected", "reason": "cleanup"},
                              headers=_auth(auth_token))
        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Cancelled"}, headers=_auth(auth_token))

    async def test_db_unique_index_prevents_duplicate_pending(
        self,
        direct_db,
        paytest_branch_id: int,
    ):
        """
        DB-level: inserting a second Pending PeriodApproval for the same
        (companyid, entityid) must raise a unique violation.
        """
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        # Insert first item
        result = await direct_db.execute(
            text("""
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requesttype, entityschema, entityname,
                     entityid, title, status, priority)
                VALUES (:cid, :bid, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                        '88888', 'First pending', 'Pending', 'Normal')
                RETURNING reviewitemid
            """),
            {"cid": company_id, "bid": paytest_branch_id},
        )
        first_id = result.scalar_one()

        # Try inserting a second Pending item for the same entity
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            await direct_db.execute(
                text("""
                    INSERT INTO review.managerreviewitems
                        (companyid, branchid, requesttype, entityschema, entityname,
                         entityid, title, status, priority)
                    VALUES (:cid, :bid, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                            '88888', 'Second pending', 'Pending', 'Normal')
                """),
                {"cid": company_id, "bid": paytest_branch_id},
            )

        # Cleanup first item
        await direct_db.execute(
            text("DELETE FROM review.managerreviewitems WHERE reviewitemid = :rid"),
            {"rid": first_id},
        )

    async def test_concurrent_race_returns_clean_422(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        Simulate the true concurrent-race scenario:

        1. Create an Open period and add a line.
        2. Pre-insert a Pending PeriodApproval review item for the same period
           (simulates the 'winning' concurrent request having already inserted it
           but not yet committed — or having committed just ahead of us).
        3. Submit Open->InReview via the API.
        4. The INSERT inside the service hits the unique index and must surface
           as a clean HTTP 422, NOT a 500 or raw IntegrityError.

        This tests that the SAIntegrityError catch in the Open->InReview path
        converts the DB unique-index violation into a user-friendly 422.
        """
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        # CP-1B: cancel any leftover InReview/Returned periods so the InReview slot
        # check doesn't fire before we reach the duplicate-review-item INSERT.
        await direct_db.execute(
            text(
                "UPDATE payroll.payrollperiods "
                "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
                "WHERE branchid = :bid AND status = 'Returned'"
            ),
            {"bid": paytest_branch_id},
        )
        await direct_db.execute(
            text(
                "UPDATE payroll.payrollperiods "
                "SET status = 'Cancelled' "
                "WHERE branchid = :bid AND status = 'InReview'"
            ),
            {"bid": paytest_branch_id},
        )
        await direct_db.commit()

        pid = await _create_open_period(
            client, auth_token, paytest_branch_id, "2029-08-04", "2029-08-10", db=direct_db
        )
        await _add_line(client, auth_token, pid, paytest_driver_id, "2029-08-05")

        # Pre-insert a Pending PeriodApproval for this period (winning race leg)
        result = await direct_db.execute(
            text("""
                INSERT INTO review.managerreviewitems
                    (companyid, branchid, requesttype, entityschema, entityname,
                     entityid, title, status, priority)
                VALUES (:cid, :bid, 'PeriodApproval', 'payroll', 'PayrollPeriods',
                        :eid, 'Race winner review item', 'Pending', 'Normal')
                RETURNING reviewitemid
            """),
            {"cid": company_id, "bid": paytest_branch_id, "eid": str(pid)},
        )
        race_winner_rid = result.scalar_one()

        try:
            # Now submit — the service INSERT will collide with the unique index
            r = await client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "InReview"},
                headers=_auth(auth_token),
            )
            # Must be a clean 422, not 500 or raw DB error
            assert r.status_code == 422, (
                f"Expected 422 for duplicate pending review race, got {r.status_code}: {r.text}"
            )
            detail = r.json().get("detail", "")
            assert "pending review" in detail.lower(), (
                f"Expected user-friendly 422 message, got: {detail!r}"
            )
        finally:
            # Cleanup the pre-inserted review item and cancel the period
            await direct_db.execute(
                text("DELETE FROM review.managerreviewitems WHERE reviewitemid = :rid"),
                {"rid": race_winner_rid},
            )
            await client.patch(
                f"/payroll/periods/{pid}/status",
                json={"status": "Cancelled"},
                headers=_auth(auth_token),
            )


# ---------------------------------------------------------------------------
# P1: Dashboard per-branch permission filtering
# ---------------------------------------------------------------------------

class TestDashboardPerBranchPermission:
    async def test_user_with_payroll_entry_sees_dashboard(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
    ):
        """Admin (AllCompanyBranches + payroll.entry) can access dashboard."""
        resp = await client.get("/dashboard", headers=_auth(auth_token))
        assert resp.status_code == 200

    async def test_payroll_view_user_can_access_dashboard(
        self,
        client: httpx.AsyncClient,
        branch_user_token: str,
    ):
        """
        Dashboard D1: branch_user holds payroll.view (PAYROLL_VIEWER_CO company role).
        payroll.view is a valid dashboard permission — the endpoint returns 200
        and includes payroll_ops in sections_available.

        Previously this test expected 403 when the dashboard required payroll.entry only.
        Updated for Dashboard D1 which opens access to any user with at least one
        relevant permission (payroll.view, review.decide, payrates.view, etc.).
        """
        resp = await client.get("/dashboard", headers=_auth(branch_user_token))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "payroll_ops" in data["sections_available"]

    async def test_branch_user_with_entry_sees_own_branch_only(
        self,
        client: httpx.AsyncClient,
        pg_instance,
        session_client: httpx.AsyncClient,
        hq_branch_id: int,
        paytest_branch_id: int,
    ):
        """
        A user with SpecificBranch scope on HQ only and payroll.entry
        should see only HQ in branch_summaries, not PAYTEST.
        """
        from app.auth.security import hash_password
        pw_hash = hash_password("TestPass123!")
        conn = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
            SELECT c.companyid, 'hq_only_entry', 'HQ Only Entry', %s, TRUE, TRUE
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
            WHERE u.username = 'hq_only_entry' AND r.rolecode = 'PAYROLL_ADMIN'
            ON CONFLICT DO NOTHING
            """,
            {"bid": hq_branch_id},
        )
        conn.close()

        login = await session_client.post("/auth/login", json={
            "username": "hq_only_entry", "password": "TestPass123!",
            "company_code": "DEMO",
        })
        assert login.status_code == 200
        token = login.json()["access_token"]

        resp = await client.get("/dashboard", headers=_auth(token))
        assert resp.status_code == 200
        branch_ids_in_response = [b["branch_id"] for b in resp.json()["branch_summaries"]]
        assert hq_branch_id in branch_ids_in_response
        assert paytest_branch_id not in branch_ids_in_response


# ---------------------------------------------------------------------------
# P1: DriverPayRules Archived period protection
# ---------------------------------------------------------------------------

class TestDriverPayRulesArchivedProtection:
    async def test_void_blocked_by_archived_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
        paytest_branch_id: int,
        paytest_driver_id: int,
    ):
        """
        Voiding a pay rule must be blocked if an Archived period (not just Locked)
        was governed by it.
        """
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        # Create an Active MinimumPay rule
        rule_resp = await client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      paytest_driver_id,
                "rule_type":      "MinimumPay",
                "amount":         "100.00",
                "effective_from": "2022-01-01",
            },
            headers=_auth(auth_token),
        )
        assert rule_resp.status_code == 201, rule_resp.text
        rule_id = rule_resp.json()["driver_pay_rule_id"]

        # Insert an Archived period with a FinalLine for this driver
        p_result = await direct_db.execute(
            text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status, lockedatutc)
                VALUES (:cid, :bid, 'ARCH-TEST-VOID', 'Archived Void Test',
                        'Week', '2022-06-01', '2022-06-07', 'Archived', NOW())
                RETURNING payrollperiodid
            """),
            {"cid": company_id, "bid": paytest_branch_id},
        )
        arch_pid = p_result.scalar_one()

        # Phase 6: authorise this test-setup INSERT via the session-level GUC.
        # is_local=false (third arg) persists for this AUTOCOMMIT connection session.
        await direct_db.execute(
            text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)")
        )
        await direct_db.execute(
            text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid,
                     linetype, quantity, finalamount, sourcetype)
                VALUES (:cid, :bid, :pid, :did, 'Miles', 100, 55.00, 'Manual')
            """),
            {"cid": company_id, "bid": paytest_branch_id,
             "pid": arch_pid, "did": paytest_driver_id},
        )

        try:
            # Void must be blocked because Archived period was governed by the rule
            void_resp = await client.post(
                f"/payroll/driver-pay-rules/{rule_id}/void",
                headers=_auth(auth_token),
            )
            assert void_resp.status_code == 422
            assert "finalized" in void_resp.json()["detail"].lower() or \
                   "governed" in void_resp.json()["detail"].lower()
        finally:
            await direct_db.execute(text(
                "ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable"
            ))
            await direct_db.execute(
                text("DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
                {"pid": arch_pid},
            )
            await direct_db.execute(text(
                "ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable"
            ))
            await direct_db.execute(
                text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": arch_pid},
            )
            # Void the rule for cleanup (now safe, period deleted)
            await client.post(f"/payroll/driver-pay-rules/{rule_id}/void",
                              headers=_auth(auth_token))

    async def test_end_blocked_by_archived_period(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        direct_db,
        paytest_branch_id: int,
        paytest_driver_id: int,
    ):
        """Ending a rule before an Archived period's start_date must be blocked."""
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        rule_resp = await client.post(
            "/payroll/driver-pay-rules",
            json={
                "driver_id":      paytest_driver_id,
                "rule_type":      "MaximumPay",
                "amount":         "500.00",
                "effective_from": "2022-01-01",
            },
            headers=_auth(auth_token),
        )
        assert rule_resp.status_code == 201, rule_resp.text
        rule_id = rule_resp.json()["driver_pay_rule_id"]

        # Insert Archived period within rule range
        p_result = await direct_db.execute(
            text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status, lockedatutc)
                VALUES (:cid, :bid, 'ARCH-TEST-END', 'Archived End Test',
                        'Week', '2022-07-01', '2022-07-07', 'Archived', NOW())
                RETURNING payrollperiodid
            """),
            {"cid": company_id, "bid": paytest_branch_id},
        )
        arch_pid = p_result.scalar_one()

        # Phase 6: authorise this test-setup INSERT via the session-level GUC.
        await direct_db.execute(
            text("SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)")
        )
        await direct_db.execute(
            text("""
                INSERT INTO payroll.payrollfinallines
                    (companyid, branchid, payrollperiodid, driverid,
                     linetype, quantity, finalamount, sourcetype)
                VALUES (:cid, :bid, :pid, :did, 'Miles', 100, 55.00, 'Manual')
            """),
            {"cid": company_id, "bid": paytest_branch_id,
             "pid": arch_pid, "did": paytest_driver_id},
        )

        try:
            # Try to end rule before the archived period start — must be blocked
            end_resp = await client.post(
                f"/payroll/driver-pay-rules/{rule_id}/end",
                json={"effective_to": "2022-06-01"},  # before 2022-07-01
                headers=_auth(auth_token),
            )
            assert end_resp.status_code == 422
        finally:
            await direct_db.execute(text(
                "ALTER TABLE payroll.payrollfinallines DISABLE TRIGGER trg_final_line_immutable"
            ))
            await direct_db.execute(
                text("DELETE FROM payroll.payrollfinallines WHERE payrollperiodid = :pid"),
                {"pid": arch_pid},
            )
            await direct_db.execute(text(
                "ALTER TABLE payroll.payrollfinallines ENABLE TRIGGER trg_final_line_immutable"
            ))
            await direct_db.execute(
                text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": arch_pid},
            )
            await client.post(f"/payroll/driver-pay-rules/{rule_id}/void",
                              headers=_auth(auth_token))


# ---------------------------------------------------------------------------
# P1: Draft-line audit logging + rollback
# ---------------------------------------------------------------------------

class TestDraftLineAuditLogging:
    async def test_add_line_writes_audit(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Adding a draft line must write a DRAFT_LINE_ADDED audit entry."""
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        pid = await _create_open_period(
            client, auth_token, paytest_branch_id, "2029-03-03", "2029-03-09", db=direct_db
        )
        line_id = await _add_line(client, auth_token, pid, paytest_driver_id, "2029-03-04")

        result = await direct_db.execute(
            text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE actioncode = 'DRAFT_LINE_ADDED'
                  AND entityid   = :eid
                  AND companyid  = :cid
            """),
            {"eid": str(line_id), "cid": company_id},
        )
        assert result.scalar_one() == 1

        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Cancelled"}, headers=_auth(auth_token))

    async def test_void_line_writes_audit(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Voiding a draft line must write a DRAFT_LINE_VOIDED audit entry."""
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        pid = await _create_open_period(
            client, auth_token, paytest_branch_id, "2029-04-07", "2029-04-13", db=direct_db
        )
        line_id = await _add_line(client, auth_token, pid, paytest_driver_id, "2029-04-08")

        r = await client.delete(
            f"/payroll/periods/{pid}/lines/{line_id}",
            headers=_auth(auth_token),
        )
        assert r.status_code in (200, 204), r.text

        result = await direct_db.execute(
            text("""
                SELECT COUNT(*) FROM audit.auditlog
                WHERE actioncode = 'DRAFT_LINE_VOIDED'
                  AND entityid   = :eid
                  AND companyid  = :cid
            """),
            {"eid": str(line_id), "cid": company_id},
        )
        assert result.scalar_one() == 1

        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Cancelled"}, headers=_auth(auth_token))

    async def test_add_line_rolls_back_on_audit_failure(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
        monkeypatch,
    ):
        """If _write_line_audit raises, the draft line INSERT must roll back."""
        pid = await _create_open_period(
            client, auth_token, paytest_branch_id, "2029-05-05", "2029-05-11", db=direct_db
        )

        # Count lines before
        result = await direct_db.execute(
            text("SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        count_before = result.scalar_one()

        async def _fail(*args, **kwargs):
            raise RuntimeError("Simulated audit failure")

        monkeypatch.setattr("app.payroll.draft_line_mutation._write_line_audit", _fail)

        import pytest as _pytest
        with _pytest.raises(RuntimeError):
            await client.post(
                f"/payroll/periods/{pid}/lines",
                json={"driver_id": paytest_driver_id, "line_type": "DailyNote",
                      "quantity": 1, "work_date": "2029-05-06", "source_type": "Manual", "notes": "filler"},
                headers=_auth(auth_token),
            )

        # Line count must be unchanged — INSERT rolled back
        result = await direct_db.execute(
            text("SELECT COUNT(*) FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )
        assert result.scalar_one() == count_before

        await client.patch(f"/payroll/periods/{pid}/status",
                           json={"status": "Cancelled"}, headers=_auth(auth_token))


# ---------------------------------------------------------------------------
# P1: Period-pay audit logging + rollback
# ---------------------------------------------------------------------------

async def _create_period_scope_item(
    client: httpx.AsyncClient,
    auth_token: str,
    branch_id: int,
    code_suffix: str,
) -> tuple[int, str]:
    """
    Return the system BONUS item activated for the given branch.
    Custom Period-scope items are no longer supported; BONUS (Period-scope,
    Fixed behavior) is used instead.  Returns (item_id, "Bonus").
    """
    r = await client.get(f"/settings/branches/{branch_id}/pay-items", headers=_auth(auth_token))
    assert r.status_code == 200
    bonus = next(i for i in r.json() if i.get("pay_item_code") == "BONUS")
    item_id = bonus["pay_item_id"]

    r2 = await client.patch(
        f"/settings/branches/{branch_id}/pay-items/{item_id}",
        json={"is_active": True},
        headers=_auth(auth_token),
    )
    assert r2.status_code == 200, f"Failed to activate BONUS pay item: {r2.text}"
    return item_id, "Bonus"


async def _cleanup_period_scope_item(
    client: httpx.AsyncClient,
    auth_token: str,
    item_id: int,
) -> None:
    """No-op: system BONUS item cannot be deleted."""
    pass


class TestPeriodPayAuditLogging:
    async def test_bonus_period_pay_is_rejected(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """Retired generic Bonus writes are rejected in favor of Bonus Events."""
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        # Create and activate a Period-scope pay item for this test
        item_id, item_code = await _create_period_scope_item(
            client, auth_token, paytest_branch_id, "ADD1"
        )
        pid = None
        try:
            pid = await _create_open_period(
                client, auth_token, paytest_branch_id, "2029-06-02", "2029-06-08", db=direct_db
            )

            r = await client.post(
                f"/payroll/periods/{pid}/period-pay",
                json={
                    "driver_id": paytest_driver_id,
                    "line_type": item_code,
                    "amount":    "250.00",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 422, r.text
            assert "bonus events" in r.text.lower()
            return
            line_id = r.json()["draft_line_id"]

            result = await direct_db.execute(
                text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE actioncode = 'PERIOD_PAY_ADDED'
                      AND entityid   = :eid
                      AND companyid  = :cid
                """),
                {"eid": str(line_id), "cid": company_id},
            )
            assert result.scalar_one() == 1
        finally:
            if pid is not None:
                await client.patch(
                    f"/payroll/periods/{pid}/status",
                    json={"status": "Cancelled"},
                    headers=_auth(auth_token),
                )
            await _cleanup_period_scope_item(client, auth_token, item_id)

    async def test_bonus_period_pay_cannot_be_voided(
        self,
        client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """A rejected generic Bonus write has no period-pay line to void."""
        result = await direct_db.execute(
            text("SELECT companyid FROM core.branches WHERE branchid = :bid"),
            {"bid": paytest_branch_id},
        )
        company_id = result.scalar_one()

        item_id, item_code = await _create_period_scope_item(
            client, auth_token, paytest_branch_id, "VOID1"
        )
        pid = None
        try:
            pid = await _create_open_period(
                client, auth_token, paytest_branch_id, "2029-07-07", "2029-07-13", db=direct_db
            )

            r = await client.post(
                f"/payroll/periods/{pid}/period-pay",
                json={
                    "driver_id": paytest_driver_id,
                    "line_type": item_code,
                    "amount":    "300.00",
                },
                headers=_auth(auth_token),
            )
            assert r.status_code == 422, r.text
            assert "bonus events" in r.text.lower()
            return
            line_id = r.json()["draft_line_id"]

            v = await client.delete(
                f"/payroll/periods/{pid}/period-pay/{line_id}",
                headers=_auth(auth_token),
            )
            assert v.status_code in (200, 204), v.text

            result = await direct_db.execute(
                text("""
                    SELECT COUNT(*) FROM audit.auditlog
                    WHERE actioncode = 'PERIOD_PAY_VOIDED'
                      AND entityid   = :eid
                      AND companyid  = :cid
                """),
                {"eid": str(line_id), "cid": company_id},
            )
            assert result.scalar_one() == 1
        finally:
            if pid is not None:
                await client.patch(
                    f"/payroll/periods/{pid}/status",
                    json={"status": "Cancelled"},
                    headers=_auth(auth_token),
                )
            await _cleanup_period_scope_item(client, auth_token, item_id)
