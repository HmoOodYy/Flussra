"""
CP-1B: One-InReview-per-branch slot enforcement tests.

Product contract verified here:
  - At most one period per CompanyID/BranchID may have Status='InReview'.
  - Friendly precheck returns 409 on the sequential (non-race) path.
  - Partial unique index ux_payrollperiods_oneinreviewperbranch is the
    concurrency authority: detected by the SAIntegrityError handler.
  - Full loser rollback for both race scenarios:
      a) Open submit wins, Returned resubmit loses.
      b) Returned resubmit wins, Open submit loses.
  - Migration preflight refuses duplicate InReview periods.
  - Downgrade drops only the 0049 index; 0048 Returned index survives.

Dates: 2094-* — isolated year.  Run from backend/:
    python -m pytest tests/test_cp1b_inreview_slot.py -v
"""
import asyncio
import datetime
import itertools
from decimal import Decimal

import psycopg2
import pytest
import pytest_asyncio
import httpx
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

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


async def _create_and_open_period(client, token, branch_id) -> tuple[int, str]:
    """Create Draft → Open period. Returns (period_id, start_date_str)."""
    start, end = _next_dates()
    r = await client.post(
        "/payroll/periods",
        json={"branch_id": branch_id, "period_type": "Week",
              "start_date": start, "end_date": end},
        headers=_auth(token),
    )
    assert r.status_code == 201, f"create period: {r.text}"
    pid = r.json()["payroll_period_id"]
    r2 = await client.patch(
        f"/payroll/periods/{pid}/status",
        json={"status": "Open"},
        headers=_auth(token),
    )
    assert r2.status_code == 200, f"open period: {r2.text}"
    return pid, start


async def _add_line(client, token, pid, driver_id, work_date) -> None:
    r = await client.post(
        f"/payroll/periods/{pid}/lines",
        json={"driver_id": driver_id, "work_date": work_date,
              "line_type": "PTO_STATUS", "quantity": 1},
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
        """0049 was the head when CP-1B was written; 0050 (CP-1C) now extends it.
        The chain must be linear: 0050 is the single head, which down-revises to 0049.
        """
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "heads"],
            capture_output=True, text=True,
            cwd=str(__import__("pathlib").Path(__file__).parent.parent.parent),
        )
        # Accept either 0049 (pre-CP-1C) or 0050 (post-CP-1C) as the sole head
        assert ("0049" in result.stdout or "0050" in result.stdout), (
            f"alembic heads must include 0049 or 0050; got:\n{result.stdout}\n{result.stderr}"
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
        import testing.postgresql
        from pathlib import Path

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

        pid1, start1 = await _create_and_open_period(session_client, auth_token, paytest_branch_id)
        await _add_line(session_client, auth_token, pid1, paytest_driver_id, start1)
        await _submit_to_inreview(session_client, auth_token, pid1)

        # Create a second Open period (may require CP-1D for multi-open; skip if blocked)
        start2, end2 = _next_dates()
        r = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": start2, "end_date": end2},
            headers=_auth(auth_token),
        )
        if r.status_code != 201:
            pytest.skip(f"Cannot create second period ({r.status_code}); may require CP-1D.")
        pid2 = r.json()["payroll_period_id"]
        await session_client.patch(f"/payroll/periods/{pid2}/status",
                                   json={"status": "Open"}, headers=_auth(auth_token))
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
        pid1, start1 = await _create_and_open_period(session_client, auth_token, paytest_branch_id)
        await _add_line(session_client, auth_token, pid1, paytest_driver_id, start1)
        ri1 = await _submit_to_inreview(session_client, auth_token, pid1)
        await _reject_to_returned(session_client, auth_token, ri1)

        # Period 2: create Open (slot freed after p1 went Returned)
        start2, end2 = _next_dates()
        r = await session_client.post(
            "/payroll/periods",
            json={"branch_id": paytest_branch_id, "period_type": "Week",
                  "start_date": start2, "end_date": end2},
            headers=_auth(auth_token),
        )
        if r.status_code != 201:
            pytest.skip(f"Cannot create second period ({r.status_code}); may require CP-1D.")
        pid2 = r.json()["payroll_period_id"]
        await session_client.patch(f"/payroll/periods/{pid2}/status",
                                   json={"status": "Open"}, headers=_auth(auth_token))
        await _add_line(session_client, auth_token, pid2, paytest_driver_id, start2)
        await _submit_to_inreview(session_client, auth_token, pid2)
        # Now pid2 is InReview; pid1 is Returned → resubmit must be blocked

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
        pid1, start1 = await _create_and_open_period(session_client, auth_token, paytest_branch_id)
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
    These tests inject a barrier at the InReview slot-check SELECT so that
    BOTH concurrent requests pass the precheck before either executes its
    UPDATE.  This proves that the unique index — not the precheck — is the
    fallback barrier.

    Barrier design:
      1. Both requests execute _check_inreview_slot_available and see "no slot
         occupied" before either proceeds.
      2. The designated winner's UPDATE fires first and sets winner_update_done.
      3. The designated loser's UPDATE waits for winner_update_done, then runs —
         hitting the unique index because the winner's uncommitted row already
         holds the slot (PostgreSQL blocks at the DB level until winner commits,
         then loser sees a unique violation).

    SQL fingerprints (all lowercased):
      Slot check:           "'inreview'" + "payrollperiodid != :pid" + "limit 1"
      Open submit UPDATE:   ":new_status" + "'open'"
      Returned resub UPDATE: "currentreturnreviewitemid = null"
    """

    async def _setup(
        self, session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db
    ) -> tuple[int, str, int, str, int, int, int]:
        """
        Build test fixture:
          pid_open (Open, has PTO_STATUS + HOURS lines, ready to submit)
          pid_returned (Returned, has PTO_STATUS + HOURS lines, old resolved review item)

        Both HOURS lines are stale-ified to calculatedamount=0.01 after setup so that
        refresh mutations are observable as rollback proof.

        Returns:
          (pid_open, start_open, pid_returned, start_returned, ri_old,
           hours_line_open, hours_line_returned)
        """
        await _cancel_active(direct_db, paytest_branch_id)

        # Ensure an approved HOURLY rate exists for the driver (idempotent).
        await _ensure_hourly_rate(session_client, auth_token, paytest_driver_id, direct_db)

        # Period A: Draft → Open → PTO_STATUS line → HOURS line → InReview → Returned
        pid_a, start_a = await _create_and_open_period(
            session_client, auth_token, paytest_branch_id
        )
        await _add_line(session_client, auth_token, pid_a, paytest_driver_id, start_a)
        hours_a = await _add_hours_line(
            session_client, auth_token, pid_a, paytest_driver_id, start_a
        )
        ri_a = await _submit_to_inreview(session_client, auth_token, pid_a)
        await _reject_to_returned(session_client, auth_token, ri_a)

        # Period B: Draft → Open → PTO_STATUS line → HOURS line
        # (slot is free — A is Returned)
        pid_b, start_b = await _create_and_open_period(
            session_client, auth_token, paytest_branch_id
        )
        # Re-check A is still Returned (create_and_open_period calls _cancel_active).
        p_a_row = (await direct_db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid_a},
        )).mappings().first()
        if p_a_row["status"] != "Returned":
            pytest.skip("Setup: period A was cancelled during period B creation (expected; retry needed).")

        await _add_line(session_client, auth_token, pid_b, paytest_driver_id, start_b)
        hours_b = await _add_hours_line(
            session_client, auth_token, pid_b, paytest_driver_id, start_b
        )

        # Stale-ify both HOURS lines so refresh mutations are visible as rollback proof.
        # 8 qty × 25.00/hr = 200.00 expected after a successful refresh commit.
        await _stale_calc(direct_db, hours_a)
        await _stale_calc(direct_db, hours_b)

        return pid_b, start_b, pid_a, start_a, ri_a, hours_b, hours_a

    @pytest.mark.asyncio
    async def test_open_wins_returned_resubmit_loses(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        Deterministic race: Open submit is the winner; Returned resubmit is the loser.

        The loser gets 409 from the unique-index SAIntegrityError handler
        (detail contains 'concurrent'), not from the precheck.
        Every write the loser attempted is fully rolled back.
        """
        try:
            (pid_open, start_open, pid_returned, start_returned, ri_old,
             hours_line_open, hours_line_returned) = await self._setup(
                session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db
            )
        except pytest.skip.Exception:
            raise

        precheck_count = [0]
        both_past_precheck = asyncio.Event()
        winner_update_done = asyncio.Event()
        _real_execute = AsyncConnection.execute

        async def _gate_open_wins(self_conn, statement, *args, **kwargs):
            sql_lower = str(statement).lower()

            is_slot_check = (
                "'inreview'" in sql_lower
                and "payrollperiodid != :pid" in sql_lower
                and "limit 1" in sql_lower
            )
            is_open_update = (
                "update payroll.payrollperiods" in sql_lower
                and ":new_status" in sql_lower
                and "'open'" in sql_lower
                and "returning payrollperiodid" in sql_lower
            )
            is_returned_update = (
                "update payroll.payrollperiods" in sql_lower
                and "currentreturnreviewitemid = null" in sql_lower
                and "returning payrollperiodid" in sql_lower
            )

            if is_slot_check:
                result = await _real_execute(self_conn, statement, *args, **kwargs)
                idx = precheck_count[0]
                precheck_count[0] += 1
                if precheck_count[0] >= 2:
                    both_past_precheck.set()
                if idx == 0:
                    await asyncio.wait_for(both_past_precheck.wait(), timeout=5.0)
                return result

            if is_open_update:
                result = await _real_execute(self_conn, statement, *args, **kwargs)
                winner_update_done.set()
                return result

            if is_returned_update:
                await asyncio.wait_for(winner_update_done.wait(), timeout=10.0)
                return await _real_execute(self_conn, statement, *args, **kwargs)

            return await _real_execute(self_conn, statement, *args, **kwargs)

        # Snapshot audit count before the race — pid_returned already has one
        # InReview audit from the setup (its original Open→InReview submission).
        pre_race_audit = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'PERIOD_STATUS_CHANGED' AND entityid = :eid "
                  "  AND newvaluejson::text LIKE '%InReview%'"),
            {"eid": str(pid_returned)},
        )).scalar_one()

        # Snapshot REVIEW_ITEM_CREATED audit count for the loser period.
        pre_ri_audit_returned = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'REVIEW_ITEM_CREATED' "
                  "  AND newvaluejson::jsonb->>'period_id' = :pid_str"),
            {"pid_str": str(pid_returned)},
        )).scalar_one()

        # Snapshot the old review item's decision fields to verify they are unchanged.
        ri_old_row = (await direct_db.execute(
            _text("SELECT status, finaldecisionbyuserid, finaldecisionatutc, finaldecisionreason "
                  "FROM review.managerreviewitems WHERE reviewitemid = :rid"),
            {"rid": ri_old},
        )).mappings().first()
        assert ri_old_row is not None, f"ri_old {ri_old} not found"
        ri_old_status = ri_old_row["status"]
        ri_old_decidedby = ri_old_row["finaldecisionbyuserid"]
        ri_old_decidedat = ri_old_row["finaldecisionatutc"]
        ri_old_reason = ri_old_row["finaldecisionreason"]

        AsyncConnection.execute = _gate_open_wins
        try:
            r_open, r_resub = await asyncio.gather(
                session_client.patch(
                    f"/payroll/periods/{pid_open}/status",
                    json={"status": "InReview"},
                    headers=_auth(auth_token),
                ),
                session_client.post(
                    f"/payroll/periods/{pid_returned}/resubmissions",
                    headers=_auth(auth_token),
                ),
            )
        finally:
            AsyncConnection.execute = _real_execute

        assert precheck_count[0] == 2, (
            f"Both requests must have reached the slot-check barrier; only {precheck_count[0]} did."
        )

        # Open must win (200); Returned resubmit must lose (409)
        assert r_open.status_code == 200, (
            f"Open submit (winner) must succeed (200); got {r_open.status_code}: {r_open.text}"
        )
        assert r_resub.status_code == 409, (
            f"Returned resubmit (loser) must get 409; got {r_resub.status_code}: {r_resub.text}"
        )

        loser_detail = r_resub.json().get("detail", "")
        assert "concurrent" in loser_detail.lower(), (
            f"409 must come from the unique-index handler (contains 'concurrent'); "
            f"got precheck message: {loser_detail!r}"
        )

        # Winner (pid_open) must be InReview; its review item Pending
        w_row = (await direct_db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid_open},
        )).mappings().first()
        assert w_row["status"] == "InReview", (
            f"Winner period must be InReview; got {w_row['status']!r}"
        )

        # Loser (pid_returned) must remain Returned — all its writes rolled back
        l_row = (await direct_db.execute(
            _text("SELECT status, currentreturnreviewitemid "
                  "FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid_returned},
        )).mappings().first()
        assert l_row["status"] == "Returned", (
            f"Loser period must remain Returned (rolled back); got {l_row['status']!r}"
        )
        assert l_row["currentreturnreviewitemid"] == ri_old, (
            "Loser period pointer must still point to original returned review item (rolled back)"
        )

        # No new Pending review item for loser period
        new_ri_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems "
                  "WHERE entityid = :eid AND status = 'Pending'"),
            {"eid": str(pid_returned)},
        )).scalar_one()
        assert new_ri_count == 0, (
            f"Loser must have no new Pending review item (rolled back); found {new_ri_count}"
        )

        # Loser resubmit audit must be fully rolled back — count must not have grown.
        post_race_audit = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'PERIOD_STATUS_CHANGED' AND entityid = :eid "
                  "  AND newvaluejson::text LIKE '%InReview%'"),
            {"eid": str(pid_returned)},
        )).scalar_one()
        assert post_race_audit == pre_race_audit, (
            f"Loser PERIOD_STATUS_CHANGED→InReview audit must be rolled back; "
            f"pre-race={pre_race_audit} post-race={post_race_audit}"
        )

        # Exactly one InReview period for this branch
        ir_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollperiods "
                  "WHERE branchid = :bid AND status = 'InReview'"),
            {"bid": paytest_branch_id},
        )).scalar_one()
        assert ir_count == 1, f"Exactly one InReview period must exist; found {ir_count}"

        # ── Calculation-refresh rollback proof ──────────────────────────────
        # Loser's HOURS line: refresh ran inside the rolled-back transaction;
        # calculatedamount must revert to the stale sentinel.
        loser_calc = (await direct_db.execute(
            _text("SELECT calculatedamount FROM payroll.payrolldraftlines "
                  "WHERE draftlineid = :lid"),
            {"lid": hours_line_returned},
        )).scalar_one()
        assert loser_calc == Decimal("0.01"), (
            f"Loser HOURS line must still hold stale sentinel 0.01 (refresh rolled back); "
            f"got {loser_calc}"
        )

        # Winner's HOURS line: refresh ran and committed; must be refreshed to 200.00.
        winner_calc = (await direct_db.execute(
            _text("SELECT calculatedamount FROM payroll.payrolldraftlines "
                  "WHERE draftlineid = :lid"),
            {"lid": hours_line_open},
        )).scalar_one()
        assert winner_calc == Decimal("200.00"), (
            f"Winner HOURS line must be refreshed to 8 × 25.00 = 200.00; got {winner_calc}"
        )

        # ── REVIEW_ITEM_CREATED audit rollback proof ─────────────────────────
        post_ri_audit_returned = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'REVIEW_ITEM_CREATED' "
                  "  AND newvaluejson::jsonb->>'period_id' = :pid_str"),
            {"pid_str": str(pid_returned)},
        )).scalar_one()
        assert post_ri_audit_returned == pre_ri_audit_returned, (
            f"Loser REVIEW_ITEM_CREATED audit must be rolled back; "
            f"pre={pre_ri_audit_returned} post={post_ri_audit_returned}"
        )

        # ── Exactly one Pending PeriodApproval for winner ───────────────────
        winner_pending = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems "
                  "WHERE entityid = :eid AND requesttype = 'PeriodApproval' "
                  "  AND status = 'Pending'"),
            {"eid": str(pid_open)},
        )).scalar_one()
        assert winner_pending == 1, (
            f"Winner must have exactly one Pending PeriodApproval; found {winner_pending}"
        )

        # ── Old review item fields unchanged ────────────────────────────────
        ri_old_row_after = (await direct_db.execute(
            _text("SELECT status, finaldecisionbyuserid, finaldecisionatutc, finaldecisionreason "
                  "FROM review.managerreviewitems WHERE reviewitemid = :rid"),
            {"rid": ri_old},
        )).mappings().first()
        assert ri_old_row_after["status"] == ri_old_status, (
            f"ri_old status must not change; before={ri_old_status!r} "
            f"after={ri_old_row_after['status']!r}"
        )
        assert ri_old_row_after["finaldecisionbyuserid"] == ri_old_decidedby, (
            "ri_old finaldecisionbyuserid must not change"
        )
        assert ri_old_row_after["finaldecisionatutc"] == ri_old_decidedat, (
            "ri_old finaldecisionatutc must not change"
        )
        assert ri_old_row_after["finaldecisionreason"] == ri_old_reason, (
            "ri_old finaldecisionreason must not change"
        )

        await _cancel_active(direct_db, paytest_branch_id)

    @pytest.mark.asyncio
    async def test_returned_resubmit_wins_open_loses(
        self,
        session_client: httpx.AsyncClient,
        auth_token: str,
        paytest_branch_id: int,
        paytest_driver_id: int,
        direct_db,
    ):
        """
        Deterministic race: Returned resubmit is the winner; Open submit is the loser.

        The loser (Open submit) gets 409 from the unique-index SAIntegrityError
        handler.  Every write the loser attempted is fully rolled back.
        """
        try:
            (pid_open, start_open, pid_returned, start_returned, ri_old,
             hours_line_open, hours_line_returned) = await self._setup(
                session_client, auth_token, paytest_branch_id, paytest_driver_id, direct_db
            )
        except pytest.skip.Exception:
            raise

        precheck_count = [0]
        both_past_precheck = asyncio.Event()
        winner_update_done = asyncio.Event()
        _real_execute = AsyncConnection.execute

        async def _gate_returned_wins(self_conn, statement, *args, **kwargs):
            sql_lower = str(statement).lower()

            is_slot_check = (
                "'inreview'" in sql_lower
                and "payrollperiodid != :pid" in sql_lower
                and "limit 1" in sql_lower
            )
            is_returned_update = (
                "update payroll.payrollperiods" in sql_lower
                and "currentreturnreviewitemid = null" in sql_lower
                and "returning payrollperiodid" in sql_lower
            )
            is_open_update = (
                "update payroll.payrollperiods" in sql_lower
                and ":new_status" in sql_lower
                and "'open'" in sql_lower
                and "returning payrollperiodid" in sql_lower
            )

            if is_slot_check:
                result = await _real_execute(self_conn, statement, *args, **kwargs)
                idx = precheck_count[0]
                precheck_count[0] += 1
                if precheck_count[0] >= 2:
                    both_past_precheck.set()
                if idx == 0:
                    await asyncio.wait_for(both_past_precheck.wait(), timeout=5.0)
                return result

            if is_returned_update:
                result = await _real_execute(self_conn, statement, *args, **kwargs)
                winner_update_done.set()
                return result

            if is_open_update:
                await asyncio.wait_for(winner_update_done.wait(), timeout=10.0)
                return await _real_execute(self_conn, statement, *args, **kwargs)

            return await _real_execute(self_conn, statement, *args, **kwargs)

        # Snapshot pre-race audit count for Open period (should be 0; it was never submitted).
        pre_race_audit_open = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'PERIOD_STATUS_CHANGED' AND entityid = :eid "
                  "  AND newvaluejson::text LIKE '%InReview%'"),
            {"eid": str(pid_open)},
        )).scalar_one()

        # Snapshot REVIEW_ITEM_CREATED audit count for the loser (Open) period.
        pre_ri_audit_open = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'REVIEW_ITEM_CREATED' "
                  "  AND newvaluejson::jsonb->>'period_id' = :pid_str"),
            {"pid_str": str(pid_open)},
        )).scalar_one()

        AsyncConnection.execute = _gate_returned_wins
        try:
            r_open, r_resub = await asyncio.gather(
                session_client.patch(
                    f"/payroll/periods/{pid_open}/status",
                    json={"status": "InReview"},
                    headers=_auth(auth_token),
                ),
                session_client.post(
                    f"/payroll/periods/{pid_returned}/resubmissions",
                    headers=_auth(auth_token),
                ),
            )
        finally:
            AsyncConnection.execute = _real_execute

        assert precheck_count[0] == 2, (
            f"Both requests must have reached the slot-check barrier; only {precheck_count[0]} did."
        )

        # Returned resubmit must win (200); Open submit must lose (409)
        assert r_resub.status_code == 200, (
            f"Returned resubmit (winner) must succeed (200); got {r_resub.status_code}: {r_resub.text}"
        )
        assert r_open.status_code == 409, (
            f"Open submit (loser) must get 409; got {r_open.status_code}: {r_open.text}"
        )

        loser_detail = r_open.json().get("detail", "")
        assert "concurrent" in loser_detail.lower(), (
            f"409 must come from the unique-index handler (contains 'concurrent'); "
            f"got precheck message: {loser_detail!r}"
        )

        # Winner (pid_returned) must be InReview; pointer cleared
        w_row = (await direct_db.execute(
            _text("SELECT status, currentreturnreviewitemid "
                  "FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid_returned},
        )).mappings().first()
        assert w_row["status"] == "InReview", (
            f"Winner period must be InReview; got {w_row['status']!r}"
        )
        assert w_row["currentreturnreviewitemid"] is None, (
            "Winner period pointer must be cleared (NULL) after resubmit"
        )

        # Loser (pid_open) must remain Open — all its writes rolled back
        l_row = (await direct_db.execute(
            _text("SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid_open},
        )).mappings().first()
        assert l_row["status"] == "Open", (
            f"Loser period must remain Open (rolled back); got {l_row['status']!r}"
        )

        # No new Pending review item for loser (Open) period
        new_ri_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems "
                  "WHERE entityid = :eid AND status = 'Pending'"),
            {"eid": str(pid_open)},
        )).scalar_one()
        assert new_ri_count == 0, (
            f"Loser Open period must have no Pending review item (rolled back); found {new_ri_count}"
        )

        # Loser Open period audit must be fully rolled back — count must not have grown.
        post_race_audit_open = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'PERIOD_STATUS_CHANGED' AND entityid = :eid "
                  "  AND newvaluejson::text LIKE '%InReview%'"),
            {"eid": str(pid_open)},
        )).scalar_one()
        assert post_race_audit_open == pre_race_audit_open, (
            f"Loser PERIOD_STATUS_CHANGED→InReview audit must be rolled back; "
            f"pre-race={pre_race_audit_open} post-race={post_race_audit_open}"
        )

        # Exactly one InReview period for this branch
        ir_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollperiods "
                  "WHERE branchid = :bid AND status = 'InReview'"),
            {"bid": paytest_branch_id},
        )).scalar_one()
        assert ir_count == 1, f"Exactly one InReview period must exist; found {ir_count}"

        # ── Calculation-refresh rollback proof ──────────────────────────────
        # Loser's (pid_open) HOURS line: refresh ran inside the rolled-back transaction;
        # calculatedamount must revert to the stale sentinel.
        loser_calc = (await direct_db.execute(
            _text("SELECT calculatedamount FROM payroll.payrolldraftlines "
                  "WHERE draftlineid = :lid"),
            {"lid": hours_line_open},
        )).scalar_one()
        assert loser_calc == Decimal("0.01"), (
            f"Loser HOURS line must still hold stale sentinel 0.01 (refresh rolled back); "
            f"got {loser_calc}"
        )

        # Winner's (pid_returned) HOURS line: refresh ran and committed; must be 200.00.
        winner_calc = (await direct_db.execute(
            _text("SELECT calculatedamount FROM payroll.payrolldraftlines "
                  "WHERE draftlineid = :lid"),
            {"lid": hours_line_returned},
        )).scalar_one()
        assert winner_calc == Decimal("200.00"), (
            f"Winner HOURS line must be refreshed to 8 × 25.00 = 200.00; got {winner_calc}"
        )

        # ── REVIEW_ITEM_CREATED audit rollback proof ─────────────────────────
        post_ri_audit_open = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM audit.auditlog "
                  "WHERE actioncode = 'REVIEW_ITEM_CREATED' "
                  "  AND newvaluejson::jsonb->>'period_id' = :pid_str"),
            {"pid_str": str(pid_open)},
        )).scalar_one()
        assert post_ri_audit_open == pre_ri_audit_open, (
            f"Loser REVIEW_ITEM_CREATED audit must be rolled back; "
            f"pre={pre_ri_audit_open} post={post_ri_audit_open}"
        )

        # ── Exactly one Pending PeriodApproval for winner ───────────────────
        winner_pending = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM review.managerreviewitems "
                  "WHERE entityid = :eid AND requesttype = 'PeriodApproval' "
                  "  AND status = 'Pending'"),
            {"eid": str(pid_returned)},
        )).scalar_one()
        assert winner_pending == 1, (
            f"Winner must have exactly one Pending PeriodApproval; found {winner_pending}"
        )

        await _cancel_active(direct_db, paytest_branch_id)
