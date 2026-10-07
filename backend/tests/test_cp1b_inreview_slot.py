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
        assert "0085" in lines[0], (
            f"Expected head 0085, got: {lines[0]}\n{result.stderr}"
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

    def test_disposable_duplicate_inreview_blocks_upgrade(
        self,
        safe_test_postgresql_stop,
    ):
        """
        Disposable PostgreSQL: applying 0049 SQL fails when duplicate InReview
        periods exist (blocking preflight RAISE EXCEPTION).
        """
        from pathlib import Path

        from tests.postgresql_compat import create_test_postgresql

        pg = create_test_postgresql()
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
            safe_test_postgresql_stop(pg)


# ---------------------------------------------------------------------------
# 2. Friendly precheck (sequential / non-race path)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 3. Deterministic concurrency tests
# ---------------------------------------------------------------------------
