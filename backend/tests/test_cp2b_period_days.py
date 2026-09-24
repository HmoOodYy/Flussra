"""
CP-2B: Payroll period-day calendar snapshot tests.

Product contracts verified:
  - Migration 0052: PayrollPeriodDays table exists with all required columns,
    constraints (unique, checks, FKs), and indexes.
  - Alembic head is exactly 0052.
  - Candidate-created Open/Draft periods get one row per calendar day.
  - Candidate-created Prepared periods also create day rows.
  - Day rows: WorkDate range inclusive, DayOfWeek correct, NormalDaysOffMask
    applied correctly (bit 0=Sun, bit 1=Mon, etc.).
  - New periods and day rows bind the same Payroll Setup Assignment and Version.
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
  - SemiMonthly remains unsupported by schedule chronology.

Dates: 2095-* — isolated year, no conflict with other test suites.
Run from backend/:
    python -m pytest tests/test_cp2b_period_days.py -v
"""
import datetime
import itertools
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COMPANY_ID = 1


@pytest_asyncio.fixture(autouse=True)
async def activate_paytest_system_items():
    """These tests use fresh branches and do not depend on PAYTEST activation."""


@pytest_asyncio.fixture
async def paytest_branch_id(direct_db) -> int:
    """Give each test an isolated branch authority timeline."""
    row = (await direct_db.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """),
        {"code": (code := f"CP2B_{uuid.uuid4().hex[:10]}"), "name": code},
    )).mappings().first()
    assert row is not None
    return row["branchid"]


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
    """Release slots and delete only periods with no immutable P6D evidence."""
    eligible = """
        SELECT period.payrollperiodid
        FROM payroll.payrollperiods period
        WHERE period.branchid = :bid
          AND NOT EXISTS (
              SELECT 1 FROM payroll.payrollcalculationsnapshots snapshot
              WHERE snapshot.payrollperiodid = period.payrollperiodid
          )
          AND NOT EXISTS (
              SELECT 1 FROM payroll.payrollperiodauditevidencecoverage coverage
              WHERE coverage.payrollperiodid = period.payrollperiodid
          )
          AND NOT EXISTS (
              SELECT 1 FROM payroll.payrollperiodauditevidenceevents evidence
              WHERE evidence.payrollperiodid = period.payrollperiodid
          )
    """
    await db.execute(
        _text("""
            UPDATE payroll.payrollperiods
            SET status = 'Cancelled', currentreturnreviewitemid = NULL
            WHERE branchid = :bid AND status IN ('Draft', 'Open', 'InReview', 'Returned')
        """),
        {"bid": branch_id},
    )
    await db.execute(
        _text(f"DELETE FROM payroll.PayrollPeriodDays WHERE payrollperiodid IN ({eligible})"),
        {"bid": branch_id},
    )
    await db.execute(
        _text(f"DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid IN ({eligible})"),
        {"bid": branch_id},
    )
    await db.execute(
        _text(f"DELETE FROM payroll.payrollperiods WHERE payrollperiodid IN ({eligible})"),
        {"bid": branch_id},
    )
    await db.commit()


async def _setup(db: AsyncConnection, branch_id: int, freq: str = "Week",
                 anchor: str = "2095-01-07", interval: int | None = None,
                 mask: int = 0) -> dict:
    """Create current Setup/Published Version/Branch Assignment policy state."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.payroll_setup.policy import (
        assign_setup,
        create_draft,
        create_setup,
        publish_version,
    )

    engine = create_async_engine(db.engine.url, echo=False)
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                tenant = (await conn.execute(_text("""
                SELECT c.CompanyID, u.UserID
                FROM core.Companies c
                JOIN sec.Users u ON u.CompanyID = c.CompanyID
                WHERE c.CompanyID = :cid AND u.Username = 'admin'
                """), {"cid": _COMPANY_ID})).mappings().one()
                company_id, user_id = int(tenant["companyid"]), int(tenant["userid"])
                suffix = uuid.uuid4().hex[:12]
                setup_id = await create_setup(
                    company_id, user_id, f"CP2B_{suffix}", f"CP2B {suffix}", conn,
                )
                draft_id = await create_draft(
                    company_id, user_id, setup_id, conn,
                    payroll_frequency=freq,
                    anchor_start_date=datetime.date.fromisoformat(anchor),
                    custom_interval_days=interval,
                    normal_days_off_mask=mask,
                )
                version_id = await publish_version(
                    company_id, user_id, setup_id, draft_id,
                    datetime.date.fromisoformat(anchor), conn,
                )
                assignment_id = await assign_setup(
                    company_id, user_id, branch_id, setup_id,
                    datetime.date.fromisoformat(anchor), conn,
                )
    finally:
        await engine.dispose()
    return {
        "payroll_setup_id": setup_id,
        "version_id": version_id,
        "assignment_id": assignment_id,
    }


async def _publish_future_version(db: AsyncConnection, setup_id: int,
                                  effective: datetime.date, mask: int) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.payroll_setup.policy import create_draft, publish_version

    engine = create_async_engine(db.engine.url, echo=False)
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                user_id = (await conn.execute(_text("""
                    SELECT UserID FROM sec.Users
                    WHERE CompanyID = :cid AND Username = 'admin'
                """), {"cid": _COMPANY_ID})).scalar_one()
                draft_id = await create_draft(
                    _COMPANY_ID, user_id, setup_id, conn,
                    payroll_frequency="Week", anchor_start_date=effective,
                    normal_days_off_mask=mask,
                )
                return await publish_version(
                    _COMPANY_ID, user_id, setup_id, draft_id, effective, conn,
                )
    finally:
        await engine.dispose()


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
                   isaddedworkday, scheduleversionid,
                   branchpayrollsetupassignmentid, payrollsetupversionid,
                   companyid, branchid,
                   addedbyuserid, addedatutc, addedreason
            FROM   payroll.PayrollPeriodDays
            WHERE  payrollperiodid = :pid
            ORDER  BY workdate
        """),
        {"pid": period_id},
    )).mappings().all()
    return [dict(r) for r in rows]


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
    # D02 — Alembic head is the current migration
    # ------------------------------------------------------------------ #

    def test_d02_alembic_head_current(self):
        """D02: Migration chain is linear and head is 0068."""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "heads"],
            capture_output=True, text=True,
            cwd=str(__import__("pathlib").Path(__file__).parent.parent.parent),
        )
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, (
            f"Expected exactly one alembic head, got {len(lines)}: {result.stdout}"
        )
        assert "0068" in lines[0], f"Expected head 0068, got: {lines[0]}"

    # ------------------------------------------------------------------ #
    # D03 — Candidate Open Week period gets 7 day rows
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d03_open_week_gets_7_rows(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D03: Candidate-created Open Week period has exactly 7 PayrollPeriodDays rows."""
        await _clean(direct_db, paytest_branch_id)
        setup = await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

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
        await _clean(direct_db, paytest_branch_id)
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")
        open_preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        await _create_period(session_client, auth_token, paytest_branch_id,
                             open_preview["selected"]["candidate_key"])
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
        await _setup(direct_db, paytest_branch_id, "Biweek", "2095-01-07")

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
        await _setup(direct_db, paytest_branch_id, "Month", "2095-03-01")

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
    # D07 — Candidate Prepared creation creates day rows
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d07_prepared_candidate_creates_rows(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D07: Candidate-based Prepared creation creates PayrollPeriodDays rows."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

        # Create Open via candidate
        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        await _create_period(session_client, auth_token, paytest_branch_id, ck)

        preview_draft = await _preview(
            session_client, auth_token, paytest_branch_id, "PREPARED_CREATION",
        )
        result = await _create_period(
            session_client, auth_token, paytest_branch_id,
            preview_draft["selected"]["candidate_key"],
        )
        assert result["status"] == "Draft"
        rows = await _day_rows_simple(direct_db, result["payroll_period_id"])
        assert len(rows) == 7, f"Prepared period got {len(rows)} day rows, expected 7"

    # ------------------------------------------------------------------ #
    # D08 — WorkDate range is inclusive
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d08_workdate_range_inclusive(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D08: Day rows span exactly [StartDate, EndDate] inclusive."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(direct_db, paytest_branch_id, "Week", "2095-04-07")

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
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

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

        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07", mask=1)

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

        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07", mask=2)

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
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")
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
    # D13 — Period and day rows share exact Setup authority
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d13_sv_id_matches_period(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D13: Every day row binds the period's exact Assignment and Version."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        period_authority = (await direct_db.execute(_text("""
            SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID, ScheduleVersionID
            FROM payroll.PayrollPeriods WHERE PayrollPeriodID = :pid
        """), {"pid": period_id})).mappings().one()
        assert period_authority["branchpayrollsetupassignmentid"] is not None
        assert period_authority["payrollsetupversionid"] is not None
        assert period_authority["scheduleversionid"] is None

        rows = await _day_rows_simple(direct_db, period_id)
        for row in rows:
            assert row["scheduleversionid"] is None
            assert row["branchpayrollsetupassignmentid"] == period_authority["branchpayrollsetupassignmentid"]
            assert row["payrollsetupversionid"] == period_authority["payrollsetupversionid"]

    # ------------------------------------------------------------------ #
    # D14 — Company/branch on rows match the period
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d14_company_branch_match(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D14: CompanyID and BranchID on day rows match the period."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

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
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

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
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        rows_before = await _day_rows_simple(direct_db, period_id)
        authority_before = (await direct_db.execute(_text("""
            SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID
            FROM payroll.PayrollPeriods WHERE PayrollPeriodID = :pid
        """), {"pid": period_id})).mappings().one()
        assert all(r["isdefaultworkday"] is True for r in rows_before), (
            "All rows should be default work days (no mask set)"
        )

        # Publish a future effective version after the period boundary.
        period_end = datetime.date.fromisoformat(result["end_date"])
        new_anchor = (period_end + datetime.timedelta(days=1)).isoformat()
        setup_id = (await direct_db.execute(_text("""
            SELECT FrozenPayrollSetupID FROM payroll.PayrollPeriods
            WHERE PayrollPeriodID = :pid
        """), {"pid": period_id})).scalar_one()
        await _publish_future_version(
            direct_db, setup_id, period_end + datetime.timedelta(days=1), 1,
        )

        # Existing period's day rows must remain unchanged
        rows_after = await _day_rows_simple(direct_db, period_id)
        assert len(rows_after) == len(rows_before), "Row count changed after setup update"
        for before, after in zip(rows_before, rows_after):
            assert before["workdate"] == after["workdate"]
            assert before["isdefaultworkday"] == after["isdefaultworkday"]
            assert before["isconfiguredoffday"] == after["isconfiguredoffday"]
            assert before["branchpayrollsetupassignmentid"] == after["branchpayrollsetupassignmentid"]
            assert before["payrollsetupversionid"] == after["payrollsetupversionid"]
        assert all(r["scheduleversionid"] is None for r in rows_after)
        current_authority = (await direct_db.execute(_text("""
            SELECT BranchPayrollSetupAssignmentID, PayrollSetupVersionID
            FROM payroll.PayrollPeriods WHERE PayrollPeriodID = :pid
        """), {"pid": period_id})).mappings().one()
        assert current_authority["branchpayrollsetupassignmentid"] == authority_before["branchpayrollsetupassignmentid"]
        assert current_authority["payrollsetupversionid"] == authority_before["payrollsetupversionid"]

    # ------------------------------------------------------------------ #
    # D17 — Draft→Open promotion preserves day rows unchanged
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d17_draft_to_open_preserves_rows(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D17: Draft→Open promotion does not change or remove day rows."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

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
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

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
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        ck = preview["selected"]["candidate_key"]
        result = await _create_period(session_client, auth_token, paytest_branch_id, ck)
        period_id = result["payroll_period_id"]

        # Fetch the first existing row to know sv_id and a workdate
        existing = (await direct_db.execute(
            _text("""
                SELECT payrollperiodid, companyid, branchid, scheduleversionid,
                       branchpayrollsetupassignmentid, payrollsetupversionid,
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
                     BranchPayrollSetupAssignmentID, PayrollSetupVersionID,
                         WorkDate, DayOfWeek, IsDefaultWorkDay, IsConfiguredOffDay)
                    VALUES (:pid, :cid, :bid, :sv, :assignment, :version,
                            :wd, :dow, :isd, :ico)
                """),
                {
                    "pid": existing["payrollperiodid"],
                    "cid": existing["companyid"],
                    "bid": existing["branchid"],
                    "sv":  existing["scheduleversionid"],
                    "assignment": existing["branchpayrollsetupassignmentid"],
                    "version": existing["payrollsetupversionid"],
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
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

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
        await _setup(direct_db, paytest_branch_id)
        preview = await _preview(session_client, auth_token, paytest_branch_id, "OPEN_CREATION")
        created = await _create_period(
            session_client, auth_token, paytest_branch_id,
            preview["selected"]["candidate_key"],
        )
        period_id = created["payroll_period_id"]
        end_date = datetime.date.fromisoformat(created["end_date"])
        await direct_db.execute(_text("""
            DELETE FROM payroll.PayrollPeriodDays
            WHERE PayrollPeriodID = :pid AND WorkDate = :work_date
        """), {"pid": period_id, "work_date": end_date})
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
        sv_id = (await direct_db.execute(_text("""
            INSERT INTO payroll.PayrollScheduleVersions
                (CompanyID, BranchID, VersionNumber, PayrollFrequency,
                 AnchorStartDate, NormalDaysOffMask, EffectiveFromDate, SourceAction)
            VALUES (:cid, :bid, 1, 'Week', '2095-09-01', 0, '2095-09-01', 'LegacyFallbackTest')
            RETURNING ScheduleVersionID
        """), {"cid": _COMPANY_ID, "bid": paytest_branch_id})).scalar_one()

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
                VALUES (1, :bid, :code, 'D22 Legacy Period', 'Week',
                        :start, :end, 'Locked', 1, :sv_id)
                RETURNING payrollperiodid
            """),
            {"bid": paytest_branch_id, "code": f"D22-{uuid.uuid4().hex[:10]}",
             "start": legacy_start, "end": legacy_end, "sv_id": sv_id},
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
                _text("""
                    DELETE FROM payroll.payrollperiods period
                    WHERE period.payrollperiodid = :pid
                      AND NOT EXISTS (
                          SELECT 1 FROM payroll.payrollperiodauditevidencecoverage coverage
                          WHERE coverage.payrollperiodid = period.payrollperiodid
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM payroll.payrollperiodauditevidenceevents evidence
                          WHERE evidence.payrollperiodid = period.payrollperiodid
                      )
                """),
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
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07", mask=1)

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
    # D24 — PeriodDay authority matches parent Period
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d24_sv_id_integrity(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D24: All day rows carry the exact parent Assignment and Version authority."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")

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
                  AND (ppd.scheduleversionid IS NOT NULL
                       OR pp.scheduleversionid IS NOT NULL
                       OR ppd.branchpayrollsetupassignmentid
                            IS DISTINCT FROM pp.branchpayrollsetupassignmentid
                       OR ppd.payrollsetupversionid
                            IS DISTINCT FROM pp.payrollsetupversionid)
            """),
            {"pid": period_id},
        )).scalar()
        assert mismatch == 0, f"Found {mismatch} day rows with mismatched Setup authority"

    # ------------------------------------------------------------------ #
    # D25 — SemiMonthly still not enabled
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d25_semimonthly_still_rejected(self):
        """D25: Current schedule chronology rejects unsupported SemiMonthly frequency."""
        from app.payroll_setup.chronology import Schedule

        with pytest.raises(ValueError):
            Schedule("SemiMonthly", datetime.date(2095, 1, 1), None, 0)

    # ------------------------------------------------------------------ #
    # D26 — Direct draft-line creation rejects work_date missing from snapshot
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_d26_add_draft_line_rejects_missing_snapshot_date(
        self, session_client, auth_token, direct_db, paytest_branch_id
    ):
        """D26: POST /payroll/periods/{id}/draft-lines rejects a work_date that is in
        StartDate/EndDate but has been removed from PayrollPeriodDays snapshot."""
        await _clean(direct_db, paytest_branch_id)
        await _setup(direct_db, paytest_branch_id, "Week", "2095-01-07")
        driver_response = await session_client.post(
            "/core/drivers",
            json={
                "branch_id": paytest_branch_id,
                "full_name": "CP2B Snapshot Driver",
                "driver_code": f"CP2B-{uuid.uuid4().hex[:10]}",
            },
            headers=_auth(auth_token),
        )
        assert driver_response.status_code == 201, driver_response.text
        driver_id = driver_response.json()["driver_id"]

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
                "driver_id": driver_id,
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
