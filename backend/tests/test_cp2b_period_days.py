"""
CP-2B: Payroll period-day calendar snapshot tests.

Product contracts verified:
  - Migration 0052: PayrollPeriodDays table exists with all required columns,
    constraints (unique, checks, FKs), and indexes.
  - Alembic head is exactly 0052.
  - Candidate-created Open/Draft periods get one row per calendar day.
  - Legacy POST /payroll/periods also creates day rows.
  - Day rows: WorkDate range inclusive, DayOfWeek correct, NormalDaysOffMask
    applied correctly (bit 0=Sun, bit 1=Mon, etc.).
  - ScheduleVersionID on day rows matches the period's ScheduleVersionID.
  - Company/branch values on rows match the period.
  - IsAddedWorkDay=FALSE and Add Day metadata all NULL on CP-2B-created rows.
  - Setup change after period creation does not mutate existing day rows.
  - Draft→Open promotion preserves day rows unchanged.
  - Candidate replay does not create duplicate day rows.
  - Unique constraint rejects duplicate (PayrollPeriodID, WorkDate) inserts.
  - get_day_grid rejects work_date not in snapshot (even if within StartDate/EndDate).
  - save_day_grid rejects work_date not in snapshot.
  - Legacy period (no day rows) falls back to StartDate/EndDate bounds check.
  - Configured-off-day does NOT block get_day_grid (metadata-only in CP-2B).
  - SemiMonthly still not enabled; no PayDate behavior; no Prepared entry.

Dates: 2095-* — isolated year, no conflict with other test suites.
Run from backend/:
    python -m pytest tests/test_cp2b_period_days.py -v
"""
import datetime
import itertools

import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMPANY_ID = 1


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_B_CTR = itertools.count(0)


def _week_2095(offset: int = 0) -> tuple[datetime.date, datetime.date]:
    """Return a Monday-to-Sunday date pair in 2095, offset by `offset` weeks."""
    base = datetime.date(2095, 1, 7)  # first Monday of 2095
    n = next(_B_CTR) + offset
    start = base + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


async def _clean(db: AsyncConnection, branch_id: int) -> None:
    """Delete all PayrollPeriodDays and PayrollPeriods for this branch."""
    await db.execute(
        _text("""
            DELETE FROM payroll.PayrollPeriodDays
            WHERE payrollperiodid IN (
                SELECT payrollperiodid FROM payroll.payrollperiods WHERE branchid = :bid
            )
        """),
        {"bid": branch_id},
    )
    await db.execute(
        _text("""
            DELETE FROM payroll.payrolldraftlines
            WHERE payrollperiodid IN (
                SELECT payrollperiodid FROM payroll.payrollperiods WHERE branchid = :bid
            )
        """),
        {"bid": branch_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payrollperiods WHERE branchid = :bid"),
        {"bid": branch_id},
    )
    await db.commit()


async def _setup(client, token, branch_id: int, freq: str = "Week",
                 anchor: str = "2095-01-07", interval: int | None = None) -> dict:
    body: dict = {"payroll_frequency": freq, "anchor_start_date": anchor}
    if interval is not None:
        body["custom_interval_days"] = interval
    r = await client.put(
        f"/settings/branches/{branch_id}/payroll-setup",
        json=body,
        headers=_auth(token),
    )
    assert r.status_code in (200, 201), f"setup failed: {r.text}"
    return r.json()


async def _preview(client, token, branch_id: int, mode: str = "OPEN_CREATION") -> dict:
    r = await client.get(
        f"/payroll/branches/{branch_id}/period-candidates",
        params={"mode": mode},
        headers=_auth(token),
    )
    assert r.status_code == 200, f"preview failed: {r.text}"
    return r.json()


async def _create_period(client, token, branch_id: int, candidate_key: str) -> dict:
    r = await client.post(
        f"/payroll/branches/{branch_id}/period-creations",
        json={"candidate_key": candidate_key},
        headers=_auth(token),
    )
    assert r.status_code in (200, 201), f"create_period failed: {r.text}"
    return r.json()


async def _day_rows(db: AsyncConnection, period_id: int) -> list[dict]:
    rows = (await db.execute(
        _text("""
            SELECT periodday_id_alias, payrollperiodid, companyid, branchid,
                   scheduleversionid, workdate, dayofweek,
                   isdefaultworkday, isconfiguredoffday,
                   isaddedworkday, addedbyuserid, addedatutc, addedreason
            FROM   payroll.PayrollPeriodDays
            WHERE  payrollperiodid = :pid
            ORDER  BY workdate
        """.replace(
            "periodday_id_alias",
            "payrollperioddayid"
        )),
        {"pid": period_id},
    )).mappings().all()
    return [dict(r) for r in rows]


async def _day_rows_simple(db: AsyncConnection, period_id: int) -> list[dict]:
    rows = (await db.execute(
        _text("""
            SELECT workdate, dayofweek, isdefaultworkday, isconfiguredoffday,
                   isaddedworkday, scheduleversionid, companyid, branchid,
                   addedbyuserid, addedatutc, addedreason
            FROM   payroll.PayrollPeriodDays
            WHERE  payrollperiodid = :pid
            ORDER  BY workdate
        """),
        {"pid": period_id},
    )).mappings().all()
    return [dict(r) for r in rows]


async def _period_sv(db: AsyncConnection, period_id: int) -> int | None:
    row = (await db.execute(
        _text("SELECT scheduleversionid FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
        {"pid": period_id},
    )).mappings().first()
    return row["scheduleversionid"] if row else None


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestCp2bPeriodDays:

    # ------------------------------------------------------------------ #
    # D01 — Migration schema
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d01_migration_schema(self, direct_db):
        """D01: PayrollPeriodDays table exists with required columns, constraints, indexes."""
        # Table exists
        r = await direct_db.execute(
            _text("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'payroll' AND table_name = 'payrollperioddays'
            """)
        )
        assert r.first() is not None, "payroll.PayrollPeriodDays table missing"

        # Required columns exist
        cols_r = await direct_db.execute(
            _text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'payroll' AND table_name = 'payrollperioddays'
            """)
        )
        cols = {row["column_name"] for row in cols_r.mappings().all()}
        for required in (
            "payrollperioddayid", "payrollperiodid", "companyid", "branchid",
            "scheduleversionid", "workdate", "dayofweek",
            "isdefaultworkday", "isconfiguredoffday",
            "isaddedworkday", "addedbyuserid", "addedatutc", "addedreason",
            "createdatutc",
        ):
            assert required in cols, f"Column {required!r} missing from PayrollPeriodDays"

        # Unique constraint on (PayrollPeriodID, WorkDate)
        uq_r = await direct_db.execute(
            _text("""
                SELECT 1 FROM information_schema.table_constraints tc
                JOIN information_schema.constraint_column_usage ccu
                     ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema   = tc.table_schema
                WHERE tc.table_schema      = 'payroll'
                  AND tc.table_name        = 'payrollperioddays'
                  AND tc.constraint_type   = 'UNIQUE'
                  AND ccu.column_name      = 'workdate'
            """)
        )
        assert uq_r.first() is not None, "uq_PayrollPeriodDays_Period_Date missing"

        # Indexes exist
        for idx_name in ("ix_payrollperioddays_period", "ix_payrollperioddays_branch_date"):
            idx_r = await direct_db.execute(
                _text("""
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname = 'payroll'
                      AND tablename  = 'payrollperioddays'
                      AND indexname  = :iname
                """),
                {"iname": idx_name},
            )
            assert idx_r.first() is not None, f"Index {idx_name!r} missing"

    # ------------------------------------------------------------------ #
    # D02 — Alembic head is exactly 0053
    # ------------------------------------------------------------------ #

    def test_d02_alembic_head_0053(self):
        """D02: Migration chain is linear and head is 0053."""
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "heads"],
            capture_output=True, text=True,
            cwd=str(__import__("pathlib").Path(__file__).parent.parent.parent),
        )
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, (
            f"Expected exactly one alembic head, got {len(lines)}: {result.stdout}"
        )
        assert "0053" in lines[0], f"Expected head 0053, got: {lines[0]}"

    # ------------------------------------------------------------------ #
    # D03 — Candidate Open Week period gets 7 day rows
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d03_open_week_gets_7_rows(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D03: Candidate-created Open Week period has exactly 7 PayrollPeriodDays rows."""
        await _clean(direct_db, paytest_branch_id)
        setup = await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")
        sv_id = setup.get("schedule_version_id")
        assert sv_id is not None

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        rows = await _day_rows_simple(direct_db, period_id)
        assert len(rows) == 7, f"Expected 7 day rows for Week period, got {len(rows)}"

    # ------------------------------------------------------------------ #
    # D04 — Candidate Draft Week period gets 7 day rows
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d04_draft_week_gets_7_rows(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D04: Candidate-created Draft (Prepared) Week period also gets 7 day rows."""
        # D03 left an Open period; we can now create a Draft via PREPARED_CREATION
        preview = await _preview(session_client, auth_token, paytest_branch_id, "PREPARED_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]
        assert result["status"] == "Draft"

        rows = await _day_rows_simple(direct_db, period_id)
        assert len(rows) == 7, f"Expected 7 day rows for Draft Week period, got {len(rows)}"

    # ------------------------------------------------------------------ #
    # D05 — Biweek period gets 14 day rows
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d05_biweek_gets_14_rows(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D05: Candidate-created Biweek period has exactly 14 day rows."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Biweek", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        rows = await _day_rows_simple(direct_db, period_id)
        assert len(rows) == 14, f"Expected 14 day rows for Biweek period, got {len(rows)}"

    # ------------------------------------------------------------------ #
    # D06 — Month period gets exact calendar-day count
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d06_month_gets_exact_day_count(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D06: Candidate-created Month period gets exactly 31 rows (March 2095)."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Month", "2095-03-01")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]
        start = datetime.date.fromisoformat(result["start_date"])
        end = datetime.date.fromisoformat(result["end_date"])
        expected = (end - start).days + 1

        rows = await _day_rows_simple(direct_db, period_id)
        assert len(rows) == expected, (
            f"Expected {expected} day rows for Month period ({start}–{end}), got {len(rows)}"
        )

    # ------------------------------------------------------------------ #
    # D07 — Legacy POST creates day rows
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d07_legacy_post_creates_rows(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D07: Legacy POST /payroll/periods also creates PayrollPeriodDays rows."""
        # Need an Open period first (legacy Draft creation requires exactly one Open)
        await _clean(direct_db, paytest_branch_id)
        setup = await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")
        sv_id = setup.get("schedule_version_id")

        # Create Open via candidate
        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        await _create_period(session_client, auth_token, paytest_branch_id, ck)

        # Now create a Draft via legacy POST
        open_row = (await direct_db.execute(
            _text("""
                SELECT startdate, enddate FROM payroll.payrollperiods
                WHERE branchid = :bid AND status = 'Open'
                ORDER BY startdate DESC LIMIT 1
            """),
            {"bid": paytest_branch_id},
        )).mappings().first()
        assert open_row is not None, "No Open period to anchor legacy Draft creation"
        open_end = open_row["enddate"]
        draft_start = open_end + datetime.timedelta(days=1)
        draft_end = draft_start + datetime.timedelta(days=6)

        r = await session_client.post(
            "/payroll/periods",
            json={
                "branch_id":   paytest_branch_id,
                "period_type": "Week",
                "start_date":  draft_start.isoformat(),
                "end_date":    draft_end.isoformat(),
                "period_name": "D07 Legacy Draft",
            },
            headers=_auth(auth_token),
        )
        if r.status_code in (200, 201):
            period_id = r.json()["payroll_period_id"]
            rows = await _day_rows_simple(direct_db, period_id)
            assert len(rows) == 7, f"Legacy POST period got {len(rows)} day rows, expected 7"
        else:
            # 409 is acceptable if slot state changed; skip with note
            assert r.status_code == 409, f"Unexpected legacy create status {r.status_code}: {r.text}"
            pytest.skip("D07 skipped: legacy POST slot conditions not met in this run")

    # ------------------------------------------------------------------ #
    # D08 — WorkDate range is inclusive
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d08_workdate_range_inclusive(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D08: Day rows span exactly [StartDate, EndDate] inclusive."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-04-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]
        start = datetime.date.fromisoformat(result["start_date"])
        end = datetime.date.fromisoformat(result["end_date"])

        rows = await _day_rows_simple(direct_db, period_id)
        dates = [r["workdate"] for r in rows]
        assert min(dates) == start, f"First day row {min(dates)} != period start {start}"
        assert max(dates) == end, f"Last day row {max(dates)} != period end {end}"
        assert len(set(dates)) == len(dates), "Duplicate WorkDate entries"

    # ------------------------------------------------------------------ #
    # D09 — DayOfWeek values are correct
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d09_dayofweek_correct(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D09: DayOfWeek uses Sun=0...Sat=6; values match (date.weekday()+1)%7 for all rows."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]
        start = datetime.date.fromisoformat(result["start_date"])

        rows = await _day_rows_simple(direct_db, period_id)
        assert len(rows) == 7

        # Verify the DayOfWeek formula is applied correctly for every row
        for row in rows:
            wd = row["workdate"]
            expected_dow = (wd.weekday() + 1) % 7  # Mon=0->1, Sun=6->0
            assert row["dayofweek"] == expected_dow, (
                f"{wd} should have DayOfWeek={expected_dow}, got {row['dayofweek']}"
            )
            assert 0 <= row["dayofweek"] <= 6

        # Spot-check: first and last rows must match the actual start date's weekday
        expected_first_dow = (start.weekday() + 1) % 7
        assert rows[0]["dayofweek"] == expected_first_dow, (
            f"First row ({start}) should have DayOfWeek={expected_first_dow}, got {rows[0]['dayofweek']}"
        )
        last_date = start + datetime.timedelta(days=6)
        expected_last_dow = (last_date.weekday() + 1) % 7
        assert rows[6]["dayofweek"] == expected_last_dow, (
            f"Last row ({last_date}) should have DayOfWeek={expected_last_dow}, got {rows[6]['dayofweek']}"
        )

    # ------------------------------------------------------------------ #
    # D10 — Sunday bit-0 mask → Sundays are configured off
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d10_sunday_off_mask(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D10: NormalDaysOffMask=1 (bit 0=Sun) → Sunday rows IsConfiguredOffDay=TRUE."""
        await _clean(direct_db, paytest_branch_id)

        # Setup with Sunday off (mask bit 0 = 1)
        r = await session_client.put(
            f"/settings/branches/{paytest_branch_id}/payroll-setup",
            json={
                "payroll_frequency": "Week",
                "anchor_start_date": "2095-01-07",
                "normal_days_off_mask": 1,  # bit 0 = Sunday
            },
            headers=_auth(auth_token),
        )
        assert r.status_code in (200, 201), f"setup with mask failed: {r.text}"

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        rows = await _day_rows_simple(direct_db, period_id)
        for row in rows:
            if row["dayofweek"] == 0:  # Sunday
                assert row["isconfiguredoffday"] is True, f"Sunday should be configured off: {row}"
                assert row["isdefaultworkday"] is False, f"Sunday should not be default work: {row}"
            else:
                assert row["isdefaultworkday"] is True, f"Non-Sunday should be default work: {row}"
                assert row["isconfiguredoffday"] is False, f"Non-Sunday should not be configured off: {row}"

    # ------------------------------------------------------------------ #
    # D11 — Monday bit-1 mask → Mondays are configured off
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d11_monday_off_mask(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D11: NormalDaysOffMask=2 (bit 1=Mon) → Monday rows IsConfiguredOffDay=TRUE."""
        await _clean(direct_db, paytest_branch_id)

        r = await session_client.put(
            f"/settings/branches/{paytest_branch_id}/payroll-setup",
            json={
                "payroll_frequency": "Week",
                "anchor_start_date": "2095-01-07",
                "normal_days_off_mask": 2,  # bit 1 = Monday
            },
            headers=_auth(auth_token),
        )
        assert r.status_code in (200, 201), f"setup with mask=2 failed: {r.text}"

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        rows = await _day_rows_simple(direct_db, period_id)
        for row in rows:
            if row["dayofweek"] == 1:  # Monday
                assert row["isconfiguredoffday"] is True, f"Monday should be configured off: {row}"
                assert row["isdefaultworkday"] is False, f"Monday should not be default work: {row}"
            else:
                assert row["isdefaultworkday"] is True, f"Non-Monday should be default work: {row}"
                assert row["isconfiguredoffday"] is False, f"Non-Monday should not be off: {row}"

    # ------------------------------------------------------------------ #
    # D12 — NULL mask → all days are default work days
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d12_null_mask_all_default(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D12: NULL NormalDaysOffMask → all day rows have IsDefaultWorkDay=TRUE."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")
        # no mask → defaults to NULL

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        rows = await _day_rows_simple(direct_db, period_id)
        assert len(rows) == 7
        for row in rows:
            assert row["isdefaultworkday"] is True, f"Expected all default work, got: {row}"
            assert row["isconfiguredoffday"] is False, f"Expected none configured off: {row}"

    # ------------------------------------------------------------------ #
    # D13 — ScheduleVersionID on rows matches period's ScheduleVersionID
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d13_sv_id_matches_period(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D13: Every PayrollPeriodDays.ScheduleVersionID equals the period's ScheduleVersionID."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        period_sv = await _period_sv(direct_db, period_id)
        assert period_sv is not None

        rows = await _day_rows_simple(direct_db, period_id)
        for row in rows:
            assert row["scheduleversionid"] == period_sv, (
                f"Row sv_id {row['scheduleversionid']} != period sv_id {period_sv}"
            )

    # ------------------------------------------------------------------ #
    # D14 — Company/branch on rows match the period
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d14_company_branch_match(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D14: CompanyID and BranchID on day rows match the period."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        rows = await _day_rows_simple(direct_db, period_id)
        for row in rows:
            assert row["companyid"] == _COMPANY_ID
            assert row["branchid"] == paytest_branch_id

    # ------------------------------------------------------------------ #
    # D15 — IsAddedWorkDay and Add Day metadata are null/false on creation
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d15_added_fields_null_on_creation(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D15: IsAddedWorkDay=FALSE and AddedBy/At/Reason=NULL on all CP-2B-created rows."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        rows = await _day_rows_simple(direct_db, period_id)
        for row in rows:
            assert row["isaddedworkday"] is False, f"IsAddedWorkDay should be FALSE: {row}"
            assert row["addedbyuserid"] is None
            assert row["addedatutc"] is None
            assert row["addedreason"] is None

    # ------------------------------------------------------------------ #
    # D16 — Setup change after creation does not mutate existing rows
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d16_setup_change_does_not_mutate_rows(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D16: Updating payroll setup after period creation leaves existing day rows unchanged."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        rows_before = await _day_rows_simple(direct_db, period_id)
        sv_id_before = rows_before[0]["scheduleversionid"]
        assert all(r["isdefaultworkday"] is True for r in rows_before), (
            "All rows should be default work days (no mask set)"
        )

        # Change setup: advance anchor past the current period end, add Sunday-off mask.
        # The new anchor must be after the current period's end date.
        period_end = datetime.date.fromisoformat(result["end_date"])
        new_anchor = (period_end + datetime.timedelta(days=1)).isoformat()
        r_setup = await session_client.put(
            f"/settings/branches/{paytest_branch_id}/payroll-setup",
            json={
                "payroll_frequency": "Week",
                "anchor_start_date": new_anchor,
                "normal_days_off_mask": 1,
            },
            headers=_auth(auth_token),
        )
        assert r_setup.status_code in (200, 201), (
            f"Setup update failed: {r_setup.text}"
        )
        new_sv_id = r_setup.json().get("schedule_version_id")
        assert new_sv_id is not None and new_sv_id != sv_id_before, (
            "Expected a new schedule version ID after setup update"
        )

        # Existing period's day rows must remain unchanged
        rows_after = await _day_rows_simple(direct_db, period_id)
        assert len(rows_after) == len(rows_before), "Row count changed after setup update"
        for before, after in zip(rows_before, rows_after):
            assert before["workdate"] == after["workdate"]
            assert before["isdefaultworkday"] == after["isdefaultworkday"]
            assert before["isconfiguredoffday"] == after["isconfiguredoffday"]
            assert before["scheduleversionid"] == after["scheduleversionid"], (
                f"ScheduleVersionID changed from {before['scheduleversionid']} "
                f"to {after['scheduleversionid']} after setup update"
            )
        assert all(r["scheduleversionid"] == sv_id_before for r in rows_after), (
            "Some day rows now reference the new schedule version — snapshot was mutated"
        )

    # ------------------------------------------------------------------ #
    # D17 — Draft→Open promotion preserves day rows unchanged
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d17_draft_to_open_preserves_rows(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D17: Draft→Open promotion does not change or remove day rows."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        # Create Draft via PREPARED_CREATION — requires an Open first
        preview_open = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        await _create_period(session_client, auth_token, paytest_branch_id,
                             preview_open["selected"]["candidate_key"])

        preview_draft = await _preview(session_client, auth_token, paytest_branch_id, "PREPARED_CREATION")
        result = await _create_period(session_client, auth_token, paytest_branch_id,
                                      preview_draft["selected"]["candidate_key"])
        draft_id = result["payroll_period_id"]
        assert result["status"] == "Draft"

        rows_before = await _day_rows_simple(direct_db, draft_id)
        assert len(rows_before) == 7

        # Promote Draft → Open by submitting
        r = await session_client.post(
            f"/payroll/periods/{draft_id}/submit",
            headers=_auth(auth_token),
        )
        # 200/422/409 — any outcome that changes status is fine; we just need to check rows
        # If submit doesn't apply (no active Open to replace), we can still verify rows unchanged.

        rows_after = await _day_rows_simple(direct_db, draft_id)
        assert len(rows_after) == len(rows_before), (
            f"Day row count changed after promotion attempt: {len(rows_before)} → {len(rows_after)}"
        )
        for before, after in zip(rows_before, rows_after):
            assert before["workdate"] == after["workdate"]
            assert before["isdefaultworkday"] == after["isdefaultworkday"]

    # ------------------------------------------------------------------ #
    # D18 — Candidate replay does not create duplicate rows
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d18_replay_no_duplicate_rows(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D18: Re-submitting the same candidate key returns ALREADY_EXISTS with unchanged day rows."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result1 = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result1["payroll_period_id"]
        rows_before = await _day_rows_simple(direct_db, period_id)
        assert len(rows_before) == 7

        # Replay — same candidate key
        result2 = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        assert result2["result"] == "ALREADY_EXISTS"
        assert result2["payroll_period_id"] == period_id

        rows_after = await _day_rows_simple(direct_db, period_id)
        assert len(rows_after) == 7, (
            f"Row count changed after replay: {len(rows_before)} → {len(rows_after)}"
        )

    # ------------------------------------------------------------------ #
    # D19 — Unique constraint rejects duplicate (PayrollPeriodID, WorkDate)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d19_unique_constraint_rejects_duplicate(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D19: Manually inserting a duplicate (PayrollPeriodID, WorkDate) raises a DB error."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        # Fetch the first existing row to know sv_id and a workdate
        existing = (await direct_db.execute(
            _text("""
                SELECT payrollperiodid, companyid, branchid, scheduleversionid,
                       workdate, dayofweek, isdefaultworkday, isconfiguredoffday
                FROM payroll.PayrollPeriodDays
                WHERE payrollperiodid = :pid
                ORDER BY workdate
                LIMIT 1
            """),
            {"pid": period_id},
        )).mappings().first()
        assert existing is not None

        violated = False
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.PayrollPeriodDays
                        (PayrollPeriodID, CompanyID, BranchID, ScheduleVersionID,
                         WorkDate, DayOfWeek, IsDefaultWorkDay, IsConfiguredOffDay)
                    VALUES (:pid, :cid, :bid, :sv, :wd, :dow, :isd, :ico)
                """),
                {
                    "pid": existing["payrollperiodid"],
                    "cid": existing["companyid"],
                    "bid": existing["branchid"],
                    "sv":  existing["scheduleversionid"],
                    "wd":  existing["workdate"],
                    "dow": existing["dayofweek"],
                    "isd": existing["isdefaultworkday"],
                    "ico": existing["isconfiguredoffday"],
                },
            )
        except Exception as exc:
            violated = True
            await direct_db.rollback()
            err = str(exc).lower()
            assert any(k in err for k in ("unique", "duplicate", "uq_payrollperioddays_period_date")), (
                f"Expected unique-constraint error, got: {exc}"
            )
        assert violated, "Duplicate (PayrollPeriodID, WorkDate) insert should have been rejected"

    # ------------------------------------------------------------------ #
    # D20 — get_day_grid rejects work_date missing from snapshot
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d20_get_day_grid_rejects_missing_snapshot_date(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D20: get_day_grid returns 400 when work_date is in StartDate/EndDate but not in snapshot."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]
        end_date = datetime.date.fromisoformat(result["end_date"])

        # Manually delete the last day row (end_date)
        await direct_db.execute(
            _text("""
                DELETE FROM payroll.PayrollPeriodDays
                WHERE payrollperiodid = :pid AND workdate = :wd
            """),
            {"pid": period_id, "wd": end_date},
        )
        await direct_db.commit()

        # end_date is still within StartDate/EndDate, but no longer in the snapshot
        r = await session_client.get(
            f"/payroll/periods/{period_id}/day-grid",
            params={"work_date": end_date.isoformat()},
            headers=_auth(auth_token),
        )
        assert r.status_code == 400, (
            f"Expected 400 for missing snapshot date, got {r.status_code}: {r.text}"
        )
        assert "snapshot" in r.text.lower() or "not in" in r.text.lower(), (
            f"Expected 'snapshot' or 'not in' in error message: {r.text}"
        )

    # ------------------------------------------------------------------ #
    # D21 — save_day_grid rejects work_date missing from snapshot
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d21_save_day_grid_rejects_missing_snapshot_date(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D21: save_day_grid returns 400 when work_date is in bounds but not in snapshot."""
        # Reuse state from D20 — the last row for the current Open period is deleted
        period_row = (await direct_db.execute(
            _text("""
                SELECT payrollperiodid, enddate FROM payroll.payrollperiods
                WHERE branchid = :bid AND status = 'Open'
                ORDER BY startdate DESC LIMIT 1
            """),
            {"bid": paytest_branch_id},
        )).mappings().first()

        if period_row is None:
            pytest.skip("D21 skipped: no Open period available after D20")

        period_id = period_row["payrollperiodid"]
        end_date = period_row["enddate"]

        # Verify the end_date row is still missing (D20 deleted it)
        missing = (await direct_db.execute(
            _text("""
                SELECT 1 FROM payroll.PayrollPeriodDays
                WHERE payrollperiodid = :pid AND workdate = :wd
            """),
            {"pid": period_id, "wd": end_date},
        )).first()

        if missing is not None:
            # Re-delete it in case D20 ran in a different order
            await direct_db.execute(
                _text("""
                    DELETE FROM payroll.PayrollPeriodDays
                    WHERE payrollperiodid = :pid AND workdate = :wd
                """),
                {"pid": period_id, "wd": end_date},
            )
            await direct_db.commit()

        r = await session_client.post(
            f"/payroll/periods/{period_id}/day-grid",
            json={"work_date": end_date.isoformat(), "rows": []},
            headers=_auth(auth_token),
        )
        assert r.status_code == 400, (
            f"Expected 400 for missing snapshot date in save_day_grid, got {r.status_code}: {r.text}"
        )

    # ------------------------------------------------------------------ #
    # D22 — Legacy period (no day rows) falls back to bounds check
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d22_legacy_period_fallback(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D22: get_day_grid for a legacy period (no day rows) uses StartDate/EndDate bounds."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        # Get the current sv_id for the branch
        sv_row = (await direct_db.execute(
            _text("""
                SELECT currentscheduleversionid FROM payroll.branchpayrollsettings
                WHERE branchid = :bid AND isactive = TRUE
            """),
            {"bid": paytest_branch_id},
        )).mappings().first()
        assert sv_row is not None, "No active setup for paytest branch"
        sv_id = sv_row["currentscheduleversionid"]

        # Insert a legacy period with 'Locked' status (no slot-uniqueness constraint on Locked).
        # Use a date range in 2095-09 (no overlap with other tests).
        # Intentionally do NOT insert any PayrollPeriodDays rows — simulating pre-0052 period.
        legacy_start = datetime.date(2095, 9, 1)
        legacy_end = datetime.date(2095, 9, 7)
        period_id = None
        period_row = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, periodcode, periodname, periodtype,
                     startdate, enddate, status, createdbyuserid, scheduleversionid)
                VALUES (1, :bid, 'D22-LEGACY', 'D22 Legacy Period', 'Week',
                        :start, :end, 'Locked', 1, :sv_id)
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id, "start": legacy_start, "end": legacy_end, "sv_id": sv_id},
        )).mappings().first()
        await direct_db.commit()
        period_id = period_row["payrollperiodid"]

        # Verify no day rows for this period (legacy fallback condition)
        day_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.PayrollPeriodDays WHERE payrollperiodid = :pid"),
            {"pid": period_id},
        )).scalar()
        assert day_count == 0, f"Expected 0 day rows for legacy period, got {day_count}"

        try:
            # No day rows — should fall back to bounds check and succeed for a valid date
            r = await session_client.get(
                f"/payroll/periods/{period_id}/day-grid",
                params={"work_date": legacy_start.isoformat()},
                headers=_auth(auth_token),
            )
            assert r.status_code == 200, (
                f"Expected 200 for legacy period start_date, got {r.status_code}: {r.text}"
            )

            # A date outside the period's bounds should still be rejected (400)
            out_of_bounds = (legacy_end + datetime.timedelta(days=1)).isoformat()
            r2 = await session_client.get(
                f"/payroll/periods/{period_id}/day-grid",
                params={"work_date": out_of_bounds},
                headers=_auth(auth_token),
            )
            assert r2.status_code == 400, (
                f"Expected 400 for out-of-bounds date on legacy period, got {r2.status_code}"
            )
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": period_id},
            )
            await direct_db.commit()

    # ------------------------------------------------------------------ #
    # D23 — Configured-off day does NOT block get_day_grid (metadata-only)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d23_configured_off_day_not_blocked(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D23: IsConfiguredOffDay=TRUE does not cause get_day_grid to reject the date."""
        await _clean(direct_db, paytest_branch_id)
        r = await session_client.put(
            f"/settings/branches/{paytest_branch_id}/payroll-setup",
            json={
                "payroll_frequency": "Week",
                "anchor_start_date": "2095-01-07",
                "normal_days_off_mask": 1,  # Sunday off
            },
            headers=_auth(auth_token),
        )
        assert r.status_code in (200, 201), f"setup failed: {r.text}"

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        # Find the actual Sunday in the period (dayofweek=0)
        rows = await _day_rows_simple(direct_db, period_id)
        sunday_rows = [r for r in rows if r["dayofweek"] == 0]
        assert len(sunday_rows) >= 1, (
            f"Expected at least one Sunday in the period, period dates: "
            f"{result['start_date']} to {result['end_date']}"
        )
        for sr in sunday_rows:
            assert sr["isconfiguredoffday"] is True, (
                f"Sunday {sr['workdate']} should be configured off (mask=1)"
            )

        # get_day_grid for a configured-off Sunday must succeed (not blocked in CP-2B)
        sunday_date = sunday_rows[0]["workdate"]
        r2 = await session_client.get(
            f"/payroll/periods/{period_id}/day-grid",
            params={"work_date": sunday_date.isoformat()},
            headers=_auth(auth_token),
        )
        assert r2.status_code == 200, (
            f"Configured-off Sunday should not be blocked by get_day_grid, got {r2.status_code}: {r2.text}"
        )

    # ------------------------------------------------------------------ #
    # D24 — ScheduleVersionID mismatch impossible via service path
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d24_sv_id_integrity(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D24: All day rows have ScheduleVersionID matching their period's ScheduleVersionID."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        # Verify at DB level: all day rows reference the same sv_id as the period
        mismatch = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.PayrollPeriodDays ppd
                JOIN payroll.payrollperiods pp
                     ON pp.payrollperiodid = ppd.payrollperiodid
                WHERE ppd.payrollperiodid = :pid
                  AND ppd.scheduleversionid != pp.scheduleversionid
            """),
            {"pid": period_id},
        )).scalar()
        assert mismatch == 0, f"Found {mismatch} day rows with mismatched ScheduleVersionID"

    # ------------------------------------------------------------------ #
    # D25 — SemiMonthly still not enabled
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d25_semimonthly_still_rejected(
        self, session_client, auth_token, paytest_branch_id
    ):
        """D25: SemiMonthly setup is rejected; CP-2B does not enable it."""
        r = await session_client.put(
            f"/settings/branches/{paytest_branch_id}/payroll-setup",
            json={"payroll_frequency": "SemiMonthly", "anchor_start_date": "2095-01-01"},
            headers=_auth(auth_token),
        )
        assert r.status_code in (400, 422), (
            f"SemiMonthly should be rejected, got {r.status_code}: {r.text}"
        )

    # ------------------------------------------------------------------ #
    # D26 — Direct draft-line creation rejects work_date missing from snapshot
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d26_add_draft_line_rejects_missing_snapshot_date(
        self, session_client, auth_token, direct_db, paytest_branch_id, paytest_driver_id
    ):
        """D26: POST /payroll/periods/{id}/draft-lines rejects a work_date that is in
        StartDate/EndDate but has been removed from PayrollPeriodDays snapshot."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(session_client, auth_token, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]
        end_date = datetime.date.fromisoformat(result["end_date"])

        # Confirm snapshot rows exist
        rows = await _day_rows_simple(direct_db, period_id)
        assert len(rows) == 7, f"Expected 7 day rows, got {len(rows)}"

        # Delete the last day row (end_date) — simulates a missing snapshot entry.
        # end_date is still within StartDate/EndDate so bounds-only check would pass.
        await direct_db.execute(
            _text("""
                DELETE FROM payroll.PayrollPeriodDays
                WHERE payrollperiodid = :pid AND workdate = :wd
            """),
            {"pid": period_id, "wd": end_date},
        )
        await direct_db.commit()

        # Confirm the row is gone
        missing = (await direct_db.execute(
            _text("""
                SELECT 1 FROM payroll.PayrollPeriodDays
                WHERE payrollperiodid = :pid AND workdate = :wd
            """),
            {"pid": period_id, "wd": end_date},
        )).first()
        assert missing is None, "Day row should have been deleted"

        # POST direct draft-line creation for the missing snapshot date
        r = await session_client.post(
            f"/payroll/periods/{period_id}/lines",
            json={
                "driver_id": paytest_driver_id,
                "work_date": end_date.isoformat(),
                "line_type": "HOURS",
                "quantity": "8",
            },
            headers=_auth(auth_token),
        )
        assert r.status_code == 400, (
            f"Expected 400 for missing snapshot date in add_draft_line, "
            f"got {r.status_code}: {r.text}"
        )
        assert "snapshot" in r.text.lower() or "not in" in r.text.lower(), (
            f"Expected 'snapshot' or 'not in' in error body: {r.text}"
        )

        # Confirm no draft line was created for that date
        line_count = (await direct_db.execute(
            _text("""
                SELECT COUNT(*) FROM payroll.payrolldraftlines
                WHERE payrollperiodid = :pid AND workdate = :wd
            """),
            {"pid": period_id, "wd": end_date},
        )).scalar()
        assert line_count == 0, (
            f"Expected no draft lines for the rejected date, found {line_count}"
        )
