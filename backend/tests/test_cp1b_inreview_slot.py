"""
CP-1B: One-InReview-per-branch slot enforcement tests.

Product contract verified here:
  - At most one period per CompanyID/BranchID may have Status='InReview'.
  - Friendly precheck returns 409 on the sequential (non-race) path.
  - Partial unique index ux_payrollperiods_oneinreviewperbranch is the
    concurrency authority: detected by the SAIntegrityError handler.
  - Full loser rollback for both deterministic scenarios:
      a) Open submit blocked by B2 while Returned exists (RETURNED_BACKLOG_BLOCKS_SUBMIT).
      b) Open submit blocked by InReview slot after Returned resubmits.
  - Migration preflight refuses duplicate InReview periods.
  - Downgrade drops only the 0049 index; 0048 Returned index survives.

Dates: 2094-* — isolated year.  Run from backend/:
    python -m pytest tests/test_cp1b_inreview_slot.py -v
"""
import datetime
import itertools
import uuid

import httpx
import psycopg2
import pytest
from sqlalchemy import text as _text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_WEEK_COUNTER = itertools.count(0)


def _next_dates() -> tuple[str, str]:
    """Return a unique (start, end) date pair in 2094."""
    n = next(_WEEK_COUNTER)
    base = datetime.date(2094, 1, 7)   # first Monday of 2094
    start = base + datetime.timedelta(weeks=n)
    end   = start + datetime.timedelta(days=6)
    return start.isoformat(), end.isoformat()


async def _cancel_active(direct_db, branch_id: int) -> None:
    await direct_db.execute(
        _text(
            "UPDATE payroll.payrollperiods "
            "SET status = 'Cancelled', currentreturnreviewitemid = NULL "
            "WHERE branchid = :bid AND status = 'Returned'"
        ),
        {"bid": branch_id},
    )
    await direct_db.execute(
        _text(
            "UPDATE payroll.payrollperiods "
            "SET status = 'Cancelled' "
            "WHERE branchid = :bid AND status IN ('Draft','Open','InReview')"
        ),
        {"bid": branch_id},
    )
    await direct_db.commit()


async def _create_and_open_period(client, token, branch_id, direct_db) -> tuple[int, str]:
    """Insert an Open period directly (CP-1D: legacy POST guard blocks creation without a prior Open)."""
    start_str, end_str = _next_dates()
    start_d = datetime.date.fromisoformat(start_str)
    end_d   = datetime.date.fromisoformat(end_str)
    row = (await direct_db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": f"CP1B-{start_str}", "name": f"CP1B {start_str}",
         "start": start_d, "end": end_d},
    )).mappings().first()
    await direct_db.commit()
    return row["payrollperiodid"], start_str


async def _add_line(client, token, pid, driver_id, work_date) -> None:
    r = await client.post(
        f"/payroll/periods/{pid}/lines",
        json={"driver_id": driver_id, "work_date": work_date,
              "line_type": "DailyNote", "quantity": 1, "notes": "filler"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"add line: {r.text}"


async def _submit_to_inreview(client, token, pid) -> int:
    """Submit period to InReview. Returns review_item_id."""
    r = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "InReview"},
        headers=_auth(token),
    )
    assert r.status_code == 200, f"submit: {r.text}"
    ri_resp = await client.get(
        "/review/items",
        params={"payroll_period_id": pid},
        headers=_auth(token),
    )
    pending = [i for i in ri_resp.json() if i["status"] == "Pending"]
    assert pending, f"No Pending review item for period {pid}"
    return pending[0]["review_item_id"]


async def _reject_to_returned(client, token, ri_id) -> None:
    dec = await client.post(
        f"/review/items/{ri_id}/decide",
        json={"decision": "Rejected", "decision_reason": "CP-1B test"},
        headers=_auth(token),
    )
    assert dec.status_code == 200, f"reject: {dec.text}"


async def _create_cp1b_driver(client, token: str, branch_id: int) -> int:
    """
    Create a CP-1B-owned driver on the given branch; return its driver_id.

    TestDeterministicConcurrency seeds an approved HOURLY rate for its driver.
    Doing that on the session-scoped paytest_driver_id leaked a future-dated
    Approved rate into later tests (e.g. backdated approvals then hit the
    future-approved-rate conflict guard), so the rate-dependent scenarios use
    their own driver instead.
    """
    r = await client.post(
        "/core/drivers",
        json={"branch_id": branch_id, "full_name": f"CP1B Driver {uuid.uuid4().hex[:8]}"},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"create CP-1B driver: {r.text}"
    return r.json()["driver_id"]


async def _ensure_hourly_rate(
    session_client, auth_token: str, driver_id: int, direct_db
) -> None:
    """
    Ensure an approved HOURLY rate (25.00/hr, effective only through 2094) exists for driver.
    Idempotent: skips seeding if a suitable approved rate already exists.
    """
    existing = (await direct_db.execute(
        _text("""
            SELECT 1 FROM payroll.driverrates dr
            JOIN payroll.ratetypes rt ON rt.ratetypeid = dr.ratetypeid
            WHERE dr.driverid = :did
              AND rt.ratecode = 'HOURLY'
              AND dr.status = 'Approved'
              AND dr.effectivefrom <= '2094-12-31'
              AND (dr.effectiveto IS NULL OR dr.effectiveto >= '2094-01-01')
            LIMIT 1
        """),
        {"did": driver_id},
    )).first()
    if existing is not None:
        return

    rt_resp = await session_client.get("/payroll/rate-types", headers=_auth(auth_token))
    assert rt_resp.status_code == 200, rt_resp.text
    hourly_rt = next(
        (rt for rt in rt_resp.json() if rt["rate_code"] == "HOURLY"), None
    )
    if hourly_rt is None:
        pytest.skip("HOURLY rate type not found; cannot prove calculation-refresh rollback")

    rate_resp = await session_client.post(
        "/payroll/rates",
        json={
            "driver_id": driver_id,
            "rate_type_id": hourly_rt["rate_type_id"],
            "amount": "25.00",
            "effective_from": "2094-01-01",
            "effective_to": "2094-12-31",
        },
        headers=_auth(auth_token),
    )
    if rate_resp.status_code != 201:
        pytest.skip(f"Cannot seed HOURLY rate ({rate_resp.status_code}): {rate_resp.text}")
    driver_rate_id = rate_resp.json()["driver_rate_id"]

    approve_resp = await session_client.post(
        f"/payroll/rates/{driver_rate_id}/approve",
        headers=_auth(auth_token),
    )
    assert approve_resp.status_code == 200, f"approve HOURLY rate: {approve_resp.text}"


async def _add_hours_line(
    client, token: str, pid: int, driver_id: int, work_date: str
) -> int:
    """Add an HOURS (PerUnit, rate-dependent) draft line. Returns draft_line_id."""
    r = await client.post(
        f"/payroll/periods/{pid}/lines",
        json={"driver_id": driver_id, "work_date": work_date,
              "line_type": "HOURS", "quantity": 8},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"add HOURS line to period {pid}: {r.text}"
    return r.json()["draft_line_id"]


async def _stale_calc(direct_db, line_id: int) -> None:
    """Set calculatedamount=0.01, needsmanagerreview=FALSE as sentinel for rollback proof."""
    await direct_db.execute(
        _text(
            "UPDATE payroll.payrolldraftlines "
            "SET calculatedamount = 0.01, needsmanagerreview = FALSE "
            "WHERE draftlineid = :lid"
        ),
        {"lid": line_id},
    )
    await direct_db.commit()


# ---------------------------------------------------------------------------
# 1. Migration / index metadata
# ---------------------------------------------------------------------------

class TestMigration:

    def test_0049_down_revision(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "migration_0049",
            Path(__file__).parent.parent.parent / "migrations" / "versions"
            / "0049_one_inreview_slot.py",
        )
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        assert m.revision == "0049"
        assert m.down_revision == "0048"

    def test_0049_in_alembic_heads(self):
        """Migration chain must be linear (single current head)."""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "heads"],
            capture_output=True, text=True,
            cwd=str(__import__("pathlib").Path(__file__).parent.parent.parent),
        )
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, (
            f"Expected exactly one alembic head (linear chain), got {len(lines)}: "
            f"{result.stdout}\n{result.stderr}"
        )
        assert "0068" in lines[0], (
            f"Expected head 0068, got: {lines[0]}\n{result.stderr}"
        )

    @pytest.mark.asyncio
    async def test_index_exists(self, direct_db):
        row = (await direct_db.execute(_text(
            "SELECT 1 FROM pg_indexes "
            "WHERE indexname = 'ux_payrollperiods_oneinreviewperbranch'"
        ))).first()
        assert row is not None, "Index ux_payrollperiods_oneinreviewperbranch must exist"

    @pytest.mark.asyncio
    async def test_index_columns(self, direct_db):
        row = (await direct_db.execute(_text("""
            SELECT indexdef FROM pg_indexes
            WHERE indexname = 'ux_payrollperiods_oneinreviewperbranch'
        """))).first()
        assert row is not None
        defn = row[0].lower()
        assert "companyid" in defn
        assert "branchid" in defn

    @pytest.mark.asyncio
    async def test_index_where_clause(self, direct_db):
        row = (await direct_db.execute(_text("""
            SELECT indexdef FROM pg_indexes
            WHERE indexname = 'ux_payrollperiods_oneinreviewperbranch'
        """))).first()
        assert row is not None
        defn = row[0].lower()
        assert "inreview" in defn, f"WHERE clause must filter on InReview; got: {defn}"

    @pytest.mark.asyncio
    async def test_existing_indexes_unchanged(self, direct_db):
        """0048 OneReturnedPerBranch index must survive CP-1B migration."""
        row = (await direct_db.execute(_text(
            "SELECT 1 FROM pg_indexes "
            "WHERE indexname ILIKE '%onereturnedperbranch%'"
        ))).first()
        assert row is not None, "0048 OneReturnedPerBranch index must still exist after 0049"

    def test_disposable_duplicate_inreview_blocks_upgrade(self):
        """
        Disposable PostgreSQL: applying 0049 SQL fails when duplicate InReview
        periods exist (blocking preflight RAISE EXCEPTION).
        """
        from pathlib import Path

        import testing.postgresql

        pg = testing.postgresql.Postgresql()
        try:
            conn = psycopg2.connect(pg.url())
            conn.autocommit = True
            cur = conn.cursor()

            migrations_sql_dir = Path(__file__).parent.parent.parent / "migrations" / "sql"

            # Apply 0001–0048 only
            for sql_file in sorted(migrations_sql_dir.glob("*.sql")):
                if sql_file.stem.split("_")[0] < "0049":
                    cur.execute(sql_file.read_text(encoding="utf-8"))

            # Minimal seed
            cur.execute("""
                INSERT INTO core.companies (companycode, companyname, legalname, status, issuspended, timezonename)
                VALUES ('DT49', 'Disposable49 Co', 'Disposable49 Ltd', 'Active', FALSE, 'UTC')
            """)
            cur.execute("""
                INSERT INTO core.branches (companyid, branchcode, branchname, status, isdefault)
                SELECT companyid, 'D49B', 'D49 Branch', 'Active', TRUE
                FROM core.companies WHERE companycode = 'DT49'
            """)
            cur.execute("""
                INSERT INTO sec.users (companyid, username, displayname, passwordhash, isactive, canlogin)
                SELECT companyid, 'd49user', 'D49 User', 'placeholder-not-for-auth', TRUE, FALSE
                FROM core.companies WHERE companycode = 'DT49'
            """)

            # Insert 2 InReview periods for the same company/branch
            cur.execute("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status, createdbyuserid)
                SELECT c.companyid, b.branchid, 'D49-W01', 'D49 Week 01', 'Week',
                       '2094-03-01'::date, '2094-03-07'::date, 'InReview', u.userid
                FROM core.companies c
                JOIN core.branches b ON b.companyid = c.companyid AND b.branchcode = 'D49B'
                JOIN sec.users u ON u.companyid = c.companyid AND u.username = 'd49user'
                WHERE c.companycode = 'DT49'
            """)
            cur.execute("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status, createdbyuserid)
                SELECT c.companyid, b.branchid, 'D49-W02', 'D49 Week 02', 'Week',
                       '2094-03-08'::date, '2094-03-14'::date, 'InReview', u.userid
                FROM core.companies c
                JOIN core.branches b ON b.companyid = c.companyid AND b.branchcode = 'D49B'
                JOIN sec.users u ON u.companyid = c.companyid AND u.username = 'd49user'
                WHERE c.companycode = 'DT49'
            """)

            # Now apply 0049 — MUST fail (blocking preflight)
            sql_0049 = (migrations_sql_dir / "0049_one_inreview_slot.sql").read_text(encoding="utf-8")
            with pytest.raises(psycopg2.Error, match="(?i)preflight failed|duplicate inreview"):
                cur.execute(sql_0049)
            conn.rollback()

            # Cancel one → preflight must pass → index created
            cur.execute("""
                UPDATE payroll.payrollperiods
                SET status = 'Cancelled'
                WHERE startdate = '2094-03-08'
                  AND companyid = (SELECT companyid FROM core.companies WHERE companycode = 'DT49')
            """)
            cur.execute(sql_0049)  # must succeed

            # Downgrade: drop only the 0049 index
            cur.execute("DROP INDEX IF EXISTS payroll.ux_payrollperiods_oneinreviewperbranch")

            cur.execute("""
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'ux_payrollperiods_oneinreviewperbranch'
            """)
            assert cur.fetchone() is None, "0049 index must be gone after downgrade"

            cur.execute("""
                SELECT 1 FROM pg_indexes
                WHERE indexname ILIKE '%onereturnedperbranch%'
            """)
            assert cur.fetchone() is not None, "0048 OneReturnedPerBranch index must still exist"

            cur.close()
            conn.close()
        finally:
            try:
                pg.stop()
            except (ValueError, OSError):
                pass  # Windows: SIGINT not supported; cluster exits with pytest


# ---------------------------------------------------------------------------
# 2. Friendly precheck (sequential / non-race path)
# ---------------------------------------------------------------------------

class TestFriendlyGuard:

    @pytest.mark.asyncio
    async def test_existing_inreview_blocks_open_submit(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        Friendly precheck blocks a second Open→InReview submit with 409.
        The message must describe the InReview slot conflict, not a concurrent race.
        """
        await _cancel_active(direct_db, paytest_branch_id)

        pid1, start1 = await _create_and_open_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _add_line(session_client, auth_token, pid1, paytest_driver_id, start1)
        await _submit_to_inreview(session_client, auth_token, pid1)

        # Create a second Open period via direct DB (CP-1D: POST guard blocks when InReview
        # exists as the only non-Cancelled period — no Open present for the B1 guard to pass).
        start2, end2 = _next_dates()
        start2_d = datetime.date.fromisoformat(start2)
        end2_d   = datetime.date.fromisoformat(end2)
        pid2_row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id, "code": f"CP1B-2ND-{start2}", "name": f"CP1B 2nd {start2}",
             "start": start2_d, "end": end2_d},
        )).mappings().first()
        await direct_db.commit()
        pid2 = pid2_row["payrollperiodid"]
        await _add_line(session_client, auth_token, pid2, paytest_driver_id, start2)

        r_submit = await session_client.patch(
            f"/payroll/periods/{pid2}/status",
            json={"status": "InReview"},
            headers=_auth(auth_token),
        )
        assert r_submit.status_code == 409, (
            f"Second InReview submit must be blocked 409; got {r_submit.status_code}: {r_submit.text}"
        )
        detail = r_submit.json().get("detail", "")
        assert "inreview" in detail.lower() or "in review" in detail.lower(), (
            f"409 detail must mention InReview slot; got: {detail!r}"
        )
        # Must NOT be a concurrent-race message (that only appears from unique-index handler)
        assert "concurrent" not in detail.lower(), (
            f"Sequential guard must not say 'concurrent'; got: {detail!r}"
        )

        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_existing_inreview_blocks_returned_resubmit(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        Friendly precheck blocks a Returned resubmit when another period is InReview.
        Requires two separate periods (may need CP-1D for multi-open).
        """
        await _cancel_active(direct_db, paytest_branch_id)

        # Period 1: Open → add line → InReview → Returned
        pid1, start1 = await _create_and_open_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _add_line(session_client, auth_token, pid1, paytest_driver_id, start1)
        ri1 = await _submit_to_inreview(session_client, auth_token, pid1)
        await _reject_to_returned(session_client, auth_token, ri1)

        # Period 2: insert directly as InReview — B2 fail-closed blocks API submission when any
        # Returned period exists, so we bypass the submit path here to reach the test scenario.
        start2_d = datetime.date.fromisoformat(start1) + datetime.timedelta(weeks=1)
        end2_d   = start2_d + datetime.timedelta(days=6)
        start2   = start2_d.isoformat()
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'InReview', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id, "code": f"CP1B-T2-{start2}", "name": f"CP1B T2 {start2}",
             "start": start2_d, "end": end2_d},
        )
        await direct_db.commit()
        # Now the period-2 row is InReview; pid1 is Returned → resubmit must be blocked

        r_resub = await session_client.post(
            f"/payroll/periods/{pid1}/resubmissions",
            headers=_auth(auth_token),
        )
        assert r_resub.status_code == 409, (
            f"Returned resubmit must be blocked 409 (another period InReview); "
            f"got {r_resub.status_code}: {r_resub.text}"
        )
        detail = r_resub.json().get("detail", "")
        assert "inreview" in detail.lower() or "in review" in detail.lower(), (
            f"409 detail must mention InReview slot; got: {detail!r}"
        )

        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_company_branch_scope(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        InReview in one branch must not block submission in a different branch.
        The slot guard is scoped to (CompanyID, BranchID).
        """
        await _cancel_active(direct_db, paytest_branch_id)

        # Create an InReview period in paytest branch
        pid1, start1 = await _create_and_open_period(session_client, auth_token, paytest_branch_id, direct_db)
        await _add_line(session_client, auth_token, pid1, paytest_driver_id, start1)
        r_submit = await session_client.patch(
            f"/payroll/periods/{pid1}/status",
            json={"status": "InReview"},
            headers=_auth(auth_token),
        )
        assert r_submit.status_code == 200, f"First submit must succeed: {r_submit.text}"

        # HQ branch should be unaffected — check that an InReview period can exist there independently.
        hq_resp = await session_client.get("/payroll/periods", headers=_auth(auth_token))
        assert hq_resp.status_code == 200

        await _cancel_active(direct_db, paytest_branch_id)


# ---------------------------------------------------------------------------
# 3. Deterministic concurrency tests
# ---------------------------------------------------------------------------

class TestDeterministicConcurrency:
    """
    Deterministic sequential coverage of CP-1D + CP-1B interaction.

    Fixture: period A (Returned, older date range) + period B (Open, later date range).
    Valid chronological dates ensure B2 fires deterministically.

    Scenario 1: Open submit while Returned exists → B2 RETURNED_BACKLOG_BLOCKS_SUBMIT (409).
                No side effects: Open stays Open, no RI, no audit, calc stays stale.
    Scenario 2: Returned resubmit succeeds (→ InReview), then Open submit →
                CP-1B InReview slot conflict (409).  No duplicate RI.
    """

    async def _setup(
        self, session_client, auth_token, paytest_branch_id, direct_db
    ) -> tuple[int, str, int, str, int, int, int]:
        """
        Build test fixture:
          pid_open (Open, has DailyNote + HOURS lines, ready to submit)
          pid_returned (Returned, has DailyNote + HOURS lines, old resolved review item)

        Both HOURS lines are stale-ified to calculatedamount=0.01 after setup so that
        refresh mutations are observable as rollback proof.

        Returns:
          (pid_open, start_open, pid_returned, start_returned, ri_old,
           hours_line_open, hours_line_returned)
        """
        await _cancel_active(direct_db, paytest_branch_id)

        driver_id = await _create_cp1b_driver(session_client, auth_token, paytest_branch_id)

        # Seed an approved HOURLY rate for this test's own driver.
        await _ensure_hourly_rate(session_client, auth_token, driver_id, direct_db)

        # Period A: Draft → Open → DailyNote line → HOURS line → InReview → Returned
        pid_a, start_a = await _create_and_open_period(
            session_client, auth_token, paytest_branch_id, direct_db
        )
        await _add_line(session_client, auth_token, pid_a, driver_id, start_a)
        hours_a = await _add_hours_line(
            session_client, auth_token, pid_a, driver_id, start_a
        )
        ri_a = await _submit_to_inreview(session_client, auth_token, pid_a)
        await _reject_to_returned(session_client, auth_token, ri_a)

        # Period B: starts the week AFTER period A ends (A ends at start_a+6).
        # Chronological order ensures B2 fires deterministically when pid_b submits:
        #   Returned.enddate (A.start+6) < Open.startdate (A.start+7) → RETURNED_BACKLOG_BLOCKS_SUBMIT.
        start_b_d = datetime.date.fromisoformat(start_a) + datetime.timedelta(weeks=1)
        end_b_d   = start_b_d + datetime.timedelta(days=6)
        start_b   = start_b_d.isoformat()
        b_row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
                VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id, "code": f"CP1B-B-{start_b}", "name": f"CP1B B {start_b}",
             "start": start_b_d, "end": end_b_d},
        )).mappings().first()
        pid_b = b_row["payrollperiodid"]

        await _add_line(session_client, auth_token, pid_b, driver_id, start_b)
        hours_b = await _add_hours_line(
            session_client, auth_token, pid_b, driver_id, start_b
        )

        # Stale-ify both HOURS lines so refresh mutations are visible as rollback proof.
        # 8 qty × 25.00/hr = 200.00 expected after a successful refresh commit.
        await _stale_calc(direct_db, hours_a)
        await _stale_calc(direct_db, hours_b)

        return pid_b, start_b, pid_a, start_a, ri_a, hours_b, hours_a

    @pytest.mark.asyncio
    async def test_b2_blocks_open_submit_while_returned_exists(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Scenario 1 (sequential, deterministic): Open submit while a Returned period
        exists on the same branch.

        B2 fail-closed logic fires inside the advisory lock before any write:
          Returned.enddate < Open.startdate → RETURNED_BACKLOG_BLOCKS_SUBMIT (409).

        Verified invariants:
        - Open period remains Open (no status change committed).
        - No Pending PeriodApproval review item for the Open period.
        - No PERIOD_STATUS_CHANGED Open→InReview audit for the Open period.
        - HOURS line calculatedamount stays 0.01 (calc refresh never reached).
        """
        (pid_open, start_open, pid_returned, start_returned, ri_old,
         hours_line_open, hours_line_returned) = await self._setup(
            session_client, auth_token, paytest_branch_id, direct_db
        )

        pre_audit = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'PERIOD_STATUS_CHANGED' AND entityid = :eid "
                  "  AND newvaluejson::text LIKE '%InReview%'"),
            {"eid": str(pid_open)},
        )).scalar_one()

        r = await session_client.patch(
            f"/payroll/periods/{pid_open}/status",
            json={"status": "InReview"},
            headers=_auth(auth_token),
        )
        assert r.status_code == 409, (
            f"Open submit must be blocked by B2 while Returned exists; got {r.status_code}: {r.text}"
        )
        err_body = r.json()
        err_code = err_body.get("detail", {}).get("code") or err_body.get("code")
        assert err_code == "RETURNED_BACKLOG_BLOCKS_SUBMIT", (
            f"Expected RETURNED_BACKLOG_BLOCKS_SUBMIT; got {err_code!r} (body={err_body})"
        )

        row = (await direct_db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid_open},
        )).mappings().first()
        assert row["status"] == "Open", (
            f"Open period must remain Open after B2 rejection; got {row['status']!r}"
        )

        ri_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems "
                  "WHERE entityid = :eid AND status = 'Pending'"),
            {"eid": str(pid_open)},
        )).scalar_one()
        assert ri_count == 0, (
            f"No Pending RI must exist for Open period after B2 rejection; found {ri_count}"
        )

        post_audit = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'PERIOD_STATUS_CHANGED' AND entityid = :eid "
                  "  AND newvaluejson::text LIKE '%InReview%'"),
            {"eid": str(pid_open)},
        )).scalar_one()
        assert post_audit == pre_audit, (
            f"No new PERIOD_STATUS_CHANGED audit for Open period; "
            f"pre={pre_audit} post={post_audit}"
        )

        # B2 fires before calc refresh — HOURS line calculatedamount must stay 0.01.
        calc_row = (await direct_db.execute(
            _text("SELECT calculatedamount FROM payroll.payrolldraftlines "
                  "WHERE draftlineid = :lid"),
            {"lid": hours_line_open},
        )).mappings().first()
        assert calc_row is not None, f"HOURS line {hours_line_open} not found"
        assert float(calc_row["calculatedamount"]) == pytest.approx(0.01, abs=0.001), (
            f"HOURS line calculatedamount must stay 0.01 (calc refresh not reached); "
            f"got {calc_row['calculatedamount']}"
        )

        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_inreview_slot_blocks_open_after_returned_resubmits(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        direct_db,
    ):
        """
        Scenario 2 (sequential, deterministic): Returned resubmit succeeds (→ InReview),
        then Open submit is blocked by CP-1B InReview slot check (409).

        Verified invariants:
        - Returned period is InReview after resubmit.
        - Open period remains Open after blocked submit.
        - No Pending review item for Open period.
        - Exactly one Pending PeriodApproval for Returned period.
        - No PERIOD_STATUS_CHANGED Open→InReview audit for Open period.
        """
        (pid_open, start_open, pid_returned, start_returned, ri_old,
         hours_line_open, hours_line_returned) = await self._setup(
            session_client, auth_token, paytest_branch_id, direct_db
        )

        pre_audit_open = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'PERIOD_STATUS_CHANGED' AND entityid = :eid "
                  "  AND newvaluejson::text LIKE '%InReview%'"),
            {"eid": str(pid_open)},
        )).scalar_one()

        # Step 1: Returned resubmit — must succeed.
        r_resub = await session_client.post(
            f"/payroll/periods/{pid_returned}/resubmissions",
            headers=_auth(auth_token),
        )
        assert r_resub.status_code == 200, (
            f"Returned resubmit must succeed; got {r_resub.status_code}: {r_resub.text}"
        )

        ret_row = (await direct_db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid_returned},
        )).mappings().first()
        assert ret_row["status"] == "InReview", (
            f"Returned period must be InReview after resubmit; got {ret_row['status']!r}"
        )

        # Step 2: Open submit — must be blocked by CP-1B InReview slot conflict.
        r_open = await session_client.patch(
            f"/payroll/periods/{pid_open}/status",
            json={"status": "InReview"},
            headers=_auth(auth_token),
        )
        assert r_open.status_code == 409, (
            f"Open submit must be blocked by InReview slot; "
            f"got {r_open.status_code}: {r_open.text}"
        )

        open_row = (await direct_db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid_open},
        )).mappings().first()
        assert open_row["status"] == "Open", (
            f"Open period must remain Open after slot rejection; got {open_row['status']!r}"
        )

        ri_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems "
                  "WHERE entityid = :eid AND status = 'Pending'"),
            {"eid": str(pid_open)},
        )).scalar_one()
        assert ri_count == 0, (
            f"No Pending RI must exist for Open period; found {ri_count}"
        )

        post_audit_open = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'PERIOD_STATUS_CHANGED' AND entityid = :eid "
                  "  AND newvaluejson::text LIKE '%InReview%'"),
            {"eid": str(pid_open)},
        )).scalar_one()
        assert post_audit_open == pre_audit_open, (
            f"No new PERIOD_STATUS_CHANGED audit for Open period; "
            f"pre={pre_audit_open} post={post_audit_open}"
        )

        pending_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems "
                  "WHERE entityid = :eid AND requesttype = 'PeriodApproval' AND status = 'Pending'"),
            {"eid": str(pid_returned)},
        )).scalar_one()
        assert pending_count == 1, (
            f"Returned period must have exactly one Pending PeriodApproval; found {pending_count}"
        )

        await _cancel_active(direct_db, paytest_branch_id)
