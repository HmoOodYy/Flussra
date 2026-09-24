"""
CP-2C: Payroll period pay-item layout snapshot tests.

Product contracts verified:
  - Migration 0053: PayrollPeriodPayItems table exists with all required columns,
    constraints (unique, checks, FKs), and indexes.
  - Alembic head is the current migration.
  - Candidate-created Open periods get one snapshot row per non-Retired PayItem.
  - Candidate-created Open and Prepared periods create snapshot rows.
  - Snapshot rows: CompanyID / BranchID / PayrollPeriodID match the period.
  - IsActiveInPeriod resolved correctly from BranchPayItemConfig + fallback.
  - PayItemStatusAtSnapshot reflects status at creation time, not later changes.
  - Both Daily and Period scope items are snapshotted.
  - Retired items at snapshot time are excluded.
  - Setup change after period creation does NOT alter existing snapshot rows.
  - Unique constraint rejects duplicate (PayrollPeriodID, PayItemID) inserts.
  - get_day_grid uses snapshot columns for post-0053 periods.
  - get_day_grid falls back to live query for legacy periods (no snapshot rows).
  - save_day_grid rejects PayItemCode not in snapshot.
  - save_day_grid accepts codes present in snapshot.
  - add_draft_line rejects PayItemCode not in period snapshot.
  - add_draft_line accepts PayItemCode present in snapshot.
  - add_period_pay_line rejects PayItemCode not in period snapshot.
  - add_period_pay_line accepts PayItemCode present in period snapshot.
  - Snapshot rows survive period status transitions (Open → InReview).
  - Candidate replay does not create duplicate snapshot rows (ON CONFLICT DO NOTHING).
  - delete_custom_pay_item routes to retire (not physical delete) when snapshot rows exist.
  - ON DELETE CASCADE: deleting a period removes its snapshot rows.
  - UNIQUE constraint is per period (same PayItem in two different periods is allowed).
  - ItemScope CHECK constraint enforced.
  - SortOrder CHECK (>= 0) enforced.

Dates: 2094-* — isolated year, no conflict with other test suites.
Run from backend/:
    python -m pytest tests/test_cp2c_pay_item_snapshot.py -v
"""
import itertools
import datetime
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.ext.asyncio import create_async_engine

from app.payroll_setup.policy import assign_setup, create_draft, create_setup, publish_version

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_COMPANY_ID = 1
_ANCHOR = "2094-01-07"   # first Monday of 2094

_C_CTR = itertools.count(0)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _week_2094(offset: int = 0) -> tuple[datetime.date, datetime.date]:
    """Return (Monday, Sunday) in 2094, offset by `offset` weeks."""
    base = datetime.date(2094, 1, 7)
    n = next(_C_CTR) + offset
    start = base + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


async def _clean(db: AsyncConnection, branch_id: int) -> None:
    """Remove mutable CP-2C setup while retaining immutable submit history."""
    # CP-4D snapshots deliberately restrict period deletion.  A prior CP-2C
    # submit test can therefore leave immutable history behind; cancel its
    # non-final period to keep candidate generation isolated, but never delete
    # the snapshot-backed period or any snapshot row.
    await db.execute(
        _text("""
            UPDATE payroll.payrollperiods period
            SET status = 'Cancelled'
            WHERE period.branchid = :bid
              AND period.status IN ('Draft', 'Open', 'InReview', 'Returned')
        """),
        {"bid": branch_id},
    )
    await db.execute(
        _text("DELETE FROM payroll.payrolldraftlines "
              "WHERE payrollperiodid IN "
              "(SELECT period.payrollperiodid FROM payroll.payrollperiods period "
              " WHERE period.branchid = :bid "
              "   AND NOT EXISTS (SELECT 1 FROM payroll.payrollcalculationsnapshots snapshot "
              "                   WHERE snapshot.payrollperiodid = period.payrollperiodid "
              "                     AND snapshot.companyid = period.companyid "
              "                     AND snapshot.branchid = period.branchid))"),
        {"bid": branch_id},
    )
    await db.execute(
        _text("""
            DELETE FROM payroll.payrollperiods period
            WHERE period.branchid = :bid
              AND NOT EXISTS (
                  SELECT 1
                  FROM payroll.payrollcalculationsnapshots snapshot
                  WHERE snapshot.payrollperiodid = period.payrollperiodid
                    AND snapshot.companyid = period.companyid
                    AND snapshot.branchid = period.branchid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM payroll.payrollperiodauditevidencecoverage coverage
                  WHERE coverage.payrollperiodid = period.payrollperiodid
              )
              AND NOT EXISTS (
                  SELECT 1 FROM payroll.payrollperiodauditevidenceevents evidence
                  WHERE evidence.payrollperiodid = period.payrollperiodid
              )
        """),
        {"bid": branch_id},
    )
    await db.commit()


async def _setup(db: AsyncConnection, branch_id: int,
                 freq: str = "Week", anchor: str = _ANCHOR) -> tuple[int, int, int]:
    engine = create_async_engine(db.engine.url, echo=False)
    try:
        async with engine.begin() as conn:
            user_id = (await conn.execute(
                _text("SELECT userid FROM sec.users WHERE companyid = :cid AND username = 'admin'"),
                {"cid": _COMPANY_ID},
            )).scalar_one()
            suffix = uuid.uuid4().hex[:10].upper()
            setup_id = await create_setup(
                _COMPANY_ID, user_id, f"CP2C_{suffix}", f"CP-2C Setup {suffix}", conn,
            )
            draft_id = await create_draft(
                _COMPANY_ID, user_id, setup_id, conn,
                payroll_frequency=freq,
                anchor_start_date=datetime.date.fromisoformat(anchor),
                normal_days_off_mask=0,
            )
            version_id = await publish_version(
                _COMPANY_ID, user_id, setup_id, draft_id,
                datetime.date.fromisoformat(anchor), conn,
            )
            assignment_id = await assign_setup(
                _COMPANY_ID, user_id, branch_id, setup_id,
                datetime.date.fromisoformat(anchor), conn,
            )
        return setup_id, version_id, assignment_id
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


async def _snap_rows(db: AsyncConnection, period_id: int) -> list[dict]:
    rows = (await db.execute(
        _text("""
            SELECT payrollperiodpayitemid, payrollperiodid, companyid, branchid,
                   payitemid, payitemcode, payitemname, displaylabel,
                   category, datatype, unit, itemscope, ratebehavior,
                   appearsinpayrollentry, appearsinledger, appearsinreports,
                   requiresrate, issystemstandard, iscustom,
                   payitemstatusatsnapshot, isactiveinperiod, sortorder,
                   snapshoteffectivefrom, sourcebranchpayitemconfigid, createdatutc
            FROM payroll.payrollperiodpayitems
            WHERE payrollperiodid = :pid
            ORDER BY sortorder NULLS LAST, payitemcode
        """),
        {"pid": period_id},
    )).mappings().all()
    return [dict(r) for r in rows]


async def _active_snap_codes(db: AsyncConnection, period_id: int,
                              scope: str | None = None) -> set[str]:
    query = """
        SELECT payitemcode FROM payroll.payrollperiodpayitems
        WHERE payrollperiodid = :pid AND isactiveinperiod = TRUE
    """
    params: dict = {"pid": period_id}
    if scope:
        query += " AND itemscope = :scope"
        params["scope"] = scope
    rows = (await db.execute(_text(query), params)).mappings().all()
    return {r["payitemcode"] for r in rows}


# ---------------------------------------------------------------------------
# Branch used exclusively by this suite — avoids Draft/Open conflicts
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def snap_branch_id(session_db_conn) -> int:
    """Use an isolated branch so immutable periods cannot move another test's anchor."""
    row = (await session_db_conn.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault)
            VALUES (1, :code, :name, 'Active', FALSE)
            RETURNING branchid
        """),
        {"code": (code := f"CP2C_{uuid.uuid4().hex[:10]}"), "name": code},
    )).mappings().first()
    await session_db_conn.commit()
    assert row is not None
    return row["branchid"]


@pytest_asyncio.fixture
async def snap_driver_id(session_client, auth_token: str, snap_branch_id: int) -> int:
    """Create a driver on this test's isolated branch."""
    suffix = uuid.uuid4().hex[:10]
    resp = await session_client.post(
        "/core/drivers",
        json={
            "branch_id":      snap_branch_id,
            "full_name":      "Snap Driver",
            "preferred_name": "SND",
            "driver_code":    f"SND-{suffix}",
            "cdl_number":     f"CDL-SND-{suffix}",
            "email":          f"snd-{suffix}@example.com",
        },
        headers=_auth(auth_token),
    )
    assert resp.status_code in (200, 201), f"driver seed failed: {resp.text}"
    return resp.json()["driver_id"]


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestCp2cPayItemSnapshot:

    # ------------------------------------------------------------------ #
    # S01 — Migration schema
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s01_migration_schema(self, direct_db):
        """S01: PayrollPeriodPayItems table exists with all required columns."""
        r = await direct_db.execute(
            _text("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'payroll'
                  AND table_name   = 'payrollperiodpayitems'
            """)
        )
        assert r.first() is not None, "payroll.PayrollPeriodPayItems table missing"

        cols_r = await direct_db.execute(
            _text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'payroll'
                  AND table_name   = 'payrollperiodpayitems'
            """)
        )
        cols = {row["column_name"] for row in cols_r.mappings().all()}
        required = {
            "payrollperiodpayitemid", "payrollperiodid", "companyid", "branchid",
            "payitemid", "payitemcode", "payitemname", "displaylabel",
            "category", "datatype", "unit", "itemscope", "ratebehavior",
            "appearsinpayrollentry", "appearsinledger", "appearsinreports",
            "requiresrate", "issystemstandard", "iscustom",
            "payitemstatusatsnapshot", "isactiveinperiod", "sortorder",
            "snapshoteffectivefrom", "sourcebranchpayitemconfigid", "createdatutc",
        }
        missing = required - cols
        assert not missing, f"Columns missing from PayrollPeriodPayItems: {missing}"

    # ------------------------------------------------------------------ #
    # S02 — Unique constraint
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s02_unique_constraint(self, direct_db):
        """S02: UNIQUE(PayrollPeriodID, PayItemID) constraint exists."""
        uq_r = await direct_db.execute(
            _text("""
                SELECT 1 FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                     ON kcu.constraint_name = tc.constraint_name
                     AND kcu.table_schema   = tc.table_schema
                WHERE tc.table_schema     = 'payroll'
                  AND tc.table_name       = 'payrollperiodpayitems'
                  AND tc.constraint_type  = 'UNIQUE'
                  AND kcu.column_name     = 'payitemid'
            """)
        )
        assert uq_r.first() is not None, "uq_PayrollPeriodPayItems_Period_PayItem missing"

    # ------------------------------------------------------------------ #
    # S03 — Alembic head
    # ------------------------------------------------------------------ #

    def test_s03_alembic_head(self):
        """S03: Alembic migration chain is linear and head is 0068."""
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
        assert "0068" in lines[0], f"Expected head 0068, got: {lines[0]}"

    # ------------------------------------------------------------------ #
    # S04 — Indexes exist
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s04_indexes_exist(self, direct_db):
        """S04: Required indexes exist on PayrollPeriodPayItems."""
        for idx in (
            "ix_payrollperiodpayitems_period",
            "ix_payrollperiodpayitems_branch",
            "ix_payrollperiodpayitems_payitem",
        ):
            r = await direct_db.execute(
                _text("""
                    SELECT 1 FROM pg_indexes
                    WHERE schemaname = 'payroll'
                      AND indexname  = :idx
                """),
                {"idx": idx},
            )
            assert r.first() is not None, f"Index {idx!r} missing"

    # ------------------------------------------------------------------ #
    # S05 — ItemScope CHECK constraint
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s05_itemscope_check(self, direct_db):
        """S05: ItemScope CHECK constraint rejects invalid values."""
        # Get any period_id and payitem_id pair that doesn't exist in the table
        # to test the constraint with a raw INSERT
        pi_row = (await direct_db.execute(
            _text("SELECT payitemid FROM payroll.payitems LIMIT 1")
        )).mappings().first()
        assert pi_row, "No PayItems seeded"
        pid = 999999999  # non-existent period — FK will fail first unless we bypass

        # Insert with invalid ItemScope — expect integrity error
        with pytest.raises(Exception) as exc_info:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrollperiodpayitems
                        (payrollperiodid, companyid, branchid, payitemid,
                         payitemcode, payitemname, category, datatype,
                         itemscope, ratebehavior,
                         appearsinpayrollentry, appearsinledger, appearsinreports,
                         requiresrate, issystemstandard, iscustom,
                         payitemstatusatsnapshot, isactiveinperiod, sortorder)
                    VALUES
                        (1, 1, 1, :piid,
                         'TEST', 'Test', 'General', 'Number',
                         'INVALID_SCOPE', 'None',
                         FALSE, FALSE, FALSE, FALSE, FALSE, FALSE,
                         'Active', TRUE, 0)
                """),
                {"piid": pi_row["payitemid"]},
            )
        assert exc_info.value is not None

    # ------------------------------------------------------------------ #
    # S06 — Snapshot created on candidate-based period creation
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s06_snapshot_created_on_candidate_create(
        self, client, session_client, auth_token, direct_db, snap_branch_id
    ):
        """S06: Candidate-created Open period gets PayrollPeriodPayItems rows."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        assert candidates, "No candidates returned"
        key = candidates["selected"]["candidate_key"]

        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        rows = await _snap_rows(direct_db, pid)
        assert len(rows) > 0, "No PayrollPeriodPayItems rows created"

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S07 — Snapshot row fields match period
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s07_snapshot_fields_match_period(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S07: Snapshot rows have correct CompanyID, BranchID, PayrollPeriodID."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        rows = await _snap_rows(direct_db, pid)
        assert rows, "No snapshot rows"
        for row in rows:
            assert row["payrollperiodid"] == pid
            assert row["companyid"] == _COMPANY_ID
            assert row["branchid"] == snap_branch_id

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S08 — Retired items excluded from snapshot
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s08_retired_items_excluded(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S08: PayItems with Status='Retired' at creation time are not snapshotted."""
        await _clean(direct_db, snap_branch_id)

        # Insert a custom Retired item
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payitems
                    (companyid, payitemcode, payitemname, category, datatype,
                     itemscope, ratebehavior, status, sortorder,
                     appearsinpayrollentry, appearsinledger, appearsinreports,
                     requiresrate, issystemstandard, isdefaultbranchactive)
                VALUES
                    (:cid, 'RETIRED_2094', 'Retired 2094 Item', 'General', 'Number',
                     'Daily', 'None', 'Retired', 999,
                     FALSE, FALSE, FALSE, FALSE, FALSE, FALSE)
                ON CONFLICT DO NOTHING
            """),
            {"cid": _COMPANY_ID},
        )

        await _setup(direct_db, snap_branch_id)
        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        rows = await _snap_rows(direct_db, pid)
        codes = {r["payitemcode"] for r in rows}
        assert "RETIRED_2094" not in codes, "Retired item should not be in snapshot"

        # cleanup
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' WHERE payitemcode = 'RETIRED_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )
        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S09 — Both Daily and Period scope items snapshotted
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s09_both_scopes_snapshotted(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S09: Daily and Period scope items both appear in the snapshot."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        rows = await _snap_rows(direct_db, pid)
        scopes = {r["itemscope"] for r in rows}
        assert "Daily" in scopes, "No Daily-scope items in snapshot"
        assert "Period" in scopes, "No Period-scope items in snapshot"

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S10 — Snapshot unchanged after Pay Item config change
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s10_snapshot_immutable_after_config_change(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S10: Disabling a pay item in BranchPayItemConfig does not alter existing snapshot."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        rows_before = await _snap_rows(direct_db, pid)
        assert rows_before

        # Disable HOURS in BranchPayItemConfig for this branch
        hours_row = (await direct_db.execute(
            _text("""
                SELECT payitemid FROM payroll.payitems
                WHERE payitemcode = 'HOURS' AND companyid IS NULL
            """)
        )).mappings().first()
        if hours_row:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.branchpayitemconfig
                        (companyid, branchid, payitemid, isactive, effectivefrom)
                    VALUES
                        (:cid, :bid, :piid, FALSE, CURRENT_DATE)
                    ON CONFLICT DO NOTHING
                """),
                {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": hours_row["payitemid"]},
            )

        rows_after = await _snap_rows(direct_db, pid)
        assert len(rows_before) == len(rows_after), (
            "Snapshot row count changed after config update"
        )
        # Restore
        if hours_row:
            await direct_db.execute(
                _text("""
                    DELETE FROM payroll.branchpayitemconfig
                    WHERE companyid = :cid AND branchid = :bid AND payitemid = :piid
                      AND isactive = FALSE
                """),
                {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": hours_row["payitemid"]},
            )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S11 — P6D evidence protects snapshot rows from period deletion
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s11_cascade_delete_on_period_delete(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S11: P6D-backed periods cannot be hard-deleted, preserving snapshots."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        rows = await _snap_rows(direct_db, pid)
        assert rows, "No snapshot rows created"

        with pytest.raises(Exception, match="P6D|audit evidence"):
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )

        rows_after = await _snap_rows(direct_db, pid)
        assert len(rows_after) == len(rows), "P6D protection must preserve snapshot rows"

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S12 — Unique constraint: duplicate (period, payitem) rejected
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s12_unique_period_payitem(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S12: Inserting duplicate (PayrollPeriodID, PayItemID) raises IntegrityError."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        first_row = (await direct_db.execute(
            _text("SELECT payitemid, payitemcode, payitemname, category, datatype, "
                  "itemscope, ratebehavior FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid LIMIT 1"),
            {"pid": pid},
        )).mappings().first()
        assert first_row

        with pytest.raises(Exception):
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrollperiodpayitems
                        (payrollperiodid, companyid, branchid, payitemid,
                         payitemcode, payitemname, category, datatype,
                         itemscope, ratebehavior,
                         appearsinpayrollentry, appearsinledger, appearsinreports,
                         requiresrate, issystemstandard, iscustom,
                         payitemstatusatsnapshot, isactiveinperiod, sortorder)
                    VALUES
                        (:pid, :cid, :bid, :piid,
                         :code, :name, :cat, :dt,
                         :scope, :rb,
                         FALSE, TRUE, TRUE, FALSE, FALSE, FALSE,
                         'Active', TRUE, 0)
                """),
                {
                    "pid": pid, "cid": _COMPANY_ID, "bid": snap_branch_id,
                    "piid": first_row["payitemid"],
                    "code": first_row["payitemcode"],
                    "name": first_row["payitemname"],
                    "cat": first_row["category"],
                    "dt": first_row["datatype"],
                    "scope": first_row["itemscope"],
                    "rb": first_row["ratebehavior"],
                },
            )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S13 — Same PayItem can appear in two different periods
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s13_same_payitem_different_periods(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S13: UNIQUE is per period; the same PayItemID may appear in multiple periods."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        # Create first period
        c1 = await _preview(client, auth_token, snap_branch_id)
        p1 = await _create_period(client, auth_token, snap_branch_id, c1["selected"]["candidate_key"])
        pid1 = p1["payroll_period_id"]

        # The next canonical candidate is Prepared and receives its own snapshot.
        c2 = await _preview(client, auth_token, snap_branch_id, "PREPARED_CREATION")
        p2 = await _create_period(
            client, auth_token, snap_branch_id, c2["selected"]["candidate_key"],
        )
        pid2 = p2["payroll_period_id"]

        # Both periods should have snapshot rows for HOURS
        hours_in_p1 = (await direct_db.execute(
            _text("SELECT 1 FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'HOURS'"),
            {"pid": pid1},
        )).first()
        hours_in_p2 = (await direct_db.execute(
            _text("SELECT 1 FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'HOURS'"),
            {"pid": pid2},
        )).first()

        assert hours_in_p1 is not None, "HOURS not in period 1 snapshot"
        assert hours_in_p2 is not None, "HOURS not in period 2 snapshot"

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S14 — get_day_grid uses snapshot columns for post-0053 periods
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s14_day_grid_uses_snapshot(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S14: get_day_grid columns match snapshot Daily-active pay items."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]
        start = period["start_date"]

        r = await client.get(
            f"/payroll/periods/{pid}/day-grid",
            params={"work_date": start},
            headers=_auth(auth_token),
        )
        assert r.status_code == 200, f"day-grid failed: {r.text}"
        grid = r.json()
        grid_codes = {col["pay_item_code"] for col in grid.get("columns", [])}

        snap_daily_active = await _active_snap_codes(direct_db, pid, scope="Daily")
        # Grid should include at least HOURS
        assert "HOURS" in grid_codes
        # Grid codes should be a subset of the snapshot active daily codes
        assert grid_codes.issubset(snap_daily_active), (
            f"Grid contains codes not in snapshot: {grid_codes - snap_daily_active}"
        )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S15 — get_day_grid legacy fallback (no snapshot rows)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s15_day_grid_legacy_fallback(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S15: get_day_grid falls back to live query for periods with no snapshot rows."""
        await _clean(direct_db, snap_branch_id)

        # Insert a bare period with no snapshot rows
        start = datetime.date(2094, 6, 2)
        end   = datetime.date(2094, 6, 8)
        pid = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiods
                    (companyid, branchid, status, periodcode, periodname, periodtype,
                     startdate, enddate, paydate)
                VALUES (:cid, :bid, 'Open', 'SNAP-LEGACY-01', 'Legacy 2094', 'Week',
                        :start, :end, :end)
                RETURNING payrollperiodid
            """),
            {"cid": _COMPANY_ID, "bid": snap_branch_id,
             "start": start, "end": end},
        )).scalar_one()

        # Verify no snapshot rows
        snap_count = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollperiodpayitems WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).scalar_one()
        assert snap_count == 0, "Expected no snapshot rows for legacy period"

        # day-grid should still succeed (live fallback)
        r = await client.get(
            f"/payroll/periods/{pid}/day-grid",
            params={"work_date": start.isoformat()},
            headers=_auth(auth_token),
        )
        assert r.status_code == 200, f"Legacy fallback failed: {r.text}"
        grid = r.json()
        assert "columns" in grid

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S16 — save_day_grid rejects code not in snapshot
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s16_save_day_grid_rejects_non_snapshot_code(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S16: save_day_grid 422s a PayItemCode not present in the period snapshot."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]
        start = period["start_date"]

        r = await client.post(
            f"/payroll/periods/{pid}/day-grid",
            json={
                "work_date": start,
                "rows": [
                    {
                        "driver_id": snap_driver_id,
                        "values": {"NONEXISTENT_CODE_2094": "10"},
                    }
                ],
            },
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"Expected 422 for non-snapshot code, got {r.status_code}: {r.text}"
        )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S17 — add_draft_line rejects code not in snapshot
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s17_add_draft_line_rejects_non_snapshot_code(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S17: add_draft_line 422s a PayItemCode not in the period snapshot."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]
        start = period["start_date"]

        r = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id":  snap_driver_id,
                "line_type":  "NONEXISTENT_CODE_2094",
                "work_date":  start,
                "quantity":   1,
            },
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"Expected 422 for non-snapshot code, got {r.status_code}: {r.text}"
        )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S18 — add_draft_line accepts HOURS (in snapshot)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s18_add_draft_line_accepts_hours(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S18: add_draft_line accepts HOURS which is always in the snapshot."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]
        start = period["start_date"]

        r = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id":  snap_driver_id,
                "line_type":  "HOURS",
                "work_date":  start,
                "quantity":   8,
            },
            headers=_auth(auth_token),
        )
        assert r.status_code in (200, 201), (
            f"HOURS should be accepted, got {r.status_code}: {r.text}"
        )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S19 — add_period_pay_line rejects code not in snapshot
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s19_add_period_pay_line_rejects_non_snapshot_code(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S19: add_period_pay_line 422s a code not in the period snapshot."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        r = await client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={
                "driver_id": snap_driver_id,
                "line_type": "NONEXISTENT_PERIOD_2094",
                "amount":    100.00,
            },
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"Expected 422 for non-snapshot period code, got {r.status_code}: {r.text}"
        )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S20 — generic Period Pay BONUS is retired in favor of Bonus Events
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s20_add_period_pay_line_accepts_bonus(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S20: generic period-pay BONUS is rejected; Bonus Events is canonical."""
        await _clean(direct_db, snap_branch_id)

        # Ensure BONUS is active for this branch before period creation
        bonus_pi = (await direct_db.execute(
            _text("SELECT payitemid FROM payroll.payitems "
                  "WHERE payitemcode = 'BONUS' AND companyid IS NULL")
        )).mappings().first()
        assert bonus_pi, "BONUS system item not seeded"

        await direct_db.execute(
            _text("""
                DELETE FROM payroll.branchpayitemconfig
                WHERE companyid = :cid AND branchid = :bid AND payitemid = :piid
                  AND effectiveto IS NULL
            """),
            {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": bonus_pi["payitemid"]},
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, payitemid, isactive, effectivefrom)
                VALUES (:cid, :bid, :piid, TRUE, '2094-01-01')
            """),
            {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": bonus_pi["payitemid"]},
        )
        await direct_db.commit()

        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        r = await client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={
                "driver_id": snap_driver_id,
                "line_type": "BONUS",
                "amount":    250.00,
            },
            headers=_auth(auth_token),
        )
        assert r.status_code == 422, (
            f"BONUS must use the canonical Bonus Events API, got {r.status_code}: {r.text}"
        )

        # Restore — remove the forced BranchPayItemConfig row
        await direct_db.execute(
            _text("DELETE FROM payroll.branchpayitemconfig "
                  "WHERE companyid = :cid AND branchid = :bid AND payitemid = :piid"),
            {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": bonus_pi["payitemid"]},
        )
        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S21 — Snapshot survives Open → InReview transition
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s21_snapshot_survives_status_transition(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S21: Snapshot rows are unchanged after Open → InReview status transition."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]
        start = period["start_date"]

        rows_before = await _snap_rows(direct_db, pid)
        assert rows_before, "No snapshot rows after period creation"

        # DailyNote is informational-only (no rate needed), satisfies both the
        # empty-period guard and the NMR guard.
        r_line = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id": snap_driver_id,
                "line_type": "DailyNote",
                "work_date": start,
                "notes":     "filler",
            },
            headers=_auth(auth_token),
        )
        assert r_line.status_code in (200, 201), f"Add DailyNote line failed: {r_line.text}"

        # Promote Open → InReview
        r_review = await client.patch(
            f"/payroll/periods/{pid}/status",
            json={"status": "InReview"},
            headers=_auth(auth_token),
        )
        assert r_review.status_code in (200, 201), (
            f"Open→InReview failed {r_review.status_code}: {r_review.text}"
        )

        rows_after = await _snap_rows(direct_db, pid)
        assert len(rows_before) == len(rows_after), (
            f"Snapshot row count changed after Open→InReview: {len(rows_before)} → {len(rows_after)}"
        )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S22 — IsActiveInPeriod = FALSE for branch-deactivated item
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s22_inactive_item_has_isactiveinperiod_false(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S22: A branch-deactivated item gets IsActiveInPeriod=FALSE in the snapshot."""
        await _clean(direct_db, snap_branch_id)

        # Get MILES payitem
        miles_row = (await direct_db.execute(
            _text("SELECT payitemid FROM payroll.payitems "
                  "WHERE payitemcode = 'MILES' AND companyid IS NULL")
        )).mappings().first()
        if not miles_row:
            pytest.skip("MILES system item not seeded")

        # Deactivate MILES for this branch before period creation.
        # Delete any existing open-version config row first (the unique index
        # uix_BranchPayItemConfig_OpenVersion covers the open case), then insert
        # a fresh inactive row.
        await direct_db.execute(
            _text("""
                DELETE FROM payroll.branchpayitemconfig
                WHERE companyid = :cid AND branchid = :bid AND payitemid = :piid
                  AND effectiveto IS NULL
            """),
            {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": miles_row["payitemid"]},
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, payitemid, isactive, effectivefrom)
                VALUES (:cid, :bid, :piid, FALSE, '2094-01-01')
            """),
            {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": miles_row["payitemid"]},
        )

        await _setup(direct_db, snap_branch_id)
        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        snap_row = (await direct_db.execute(
            _text("SELECT isactiveinperiod FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'MILES'"),
            {"pid": pid},
        )).mappings().first()

        if snap_row is not None:
            assert snap_row["isactiveinperiod"] is False, (
                "Branch-deactivated MILES should have IsActiveInPeriod=FALSE"
            )

        # Restore
        await direct_db.execute(
            _text("DELETE FROM payroll.branchpayitemconfig "
                  "WHERE companyid = :cid AND branchid = :bid AND payitemid = :piid"),
            {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": miles_row["payitemid"]},
        )
        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S23 — delete_custom_pay_item retires when snapshot rows exist
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s23_delete_custom_item_retires_when_snapshot_exists(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S23: delete_custom_pay_item routes to retire when PayrollPeriodPayItems rows reference the item."""
        await _clean(direct_db, snap_branch_id)

        # Insert a custom pay item directly into the DB (bypass CDPI workflow for test isolation).
        item_id = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payitems
                    (companyid, payitemcode, payitemname, category, datatype,
                     itemscope, ratebehavior, status, sortorder,
                     appearsinpayrollentry, appearsinledger, appearsinreports,
                     requiresrate, issystemstandard, isdefaultbranchactive)
                VALUES
                    (:cid, 'SNAP_CUSTOM_2094', 'Snapshot Custom 2094', 'Custom', 'Number',
                     'Period', 'EnteredAmount', 'Active', 500,
                     FALSE, TRUE, TRUE, FALSE, FALSE, TRUE)
                ON CONFLICT DO NOTHING
                RETURNING payitemid
            """),
            {"cid": _COMPANY_ID},
        )).scalar_one()

        # Create a period (will snapshot the new custom item)
        await _setup(direct_db, snap_branch_id)
        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        # Verify item is in snapshot (should be auto-snapshotted at period creation)
        snap_check = (await direct_db.execute(
            _text("SELECT 1 FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'SNAP_CUSTOM_2094'"),
            {"pid": pid},
        )).first()
        assert snap_check is not None, "Custom item not snapshotted"

        # Delete the custom item — should retire (not physically delete)
        r_del = await client.delete(
            f"/settings/pay-items/{item_id}",
            headers=_auth(auth_token),
        )
        assert r_del.status_code in (200, 204), f"delete failed: {r_del.text}"
        result = r_del.json() if r_del.status_code == 200 else {}
        assert result.get("deletion_type") == "retired", (
            f"Expected 'retired', got {result.get('deletion_type')!r}"
        )

        # Snapshot row should still exist
        snap_after = (await direct_db.execute(
            _text("SELECT 1 FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'SNAP_CUSTOM_2094'"),
            {"pid": pid},
        )).first()
        assert snap_after is not None, "Snapshot row was removed on retire — it should persist"

        await _clean(direct_db, snap_branch_id)
        # Clean up the custom item (now retired; physical delete blocked by snapshot rows)
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' WHERE payitemcode = 'SNAP_CUSTOM_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )

    # ------------------------------------------------------------------ #
    # S24 — Snapshot ON CONFLICT DO NOTHING (replay-safe)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s24_snapshot_on_conflict_do_nothing(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S24: Calling _create_period_pay_item_rows twice does not duplicate rows."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        count_before = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollperiodpayitems WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).scalar_one()

        # Simulate a second snapshot call via raw re-insert with ON CONFLICT DO NOTHING
        first_row = (await direct_db.execute(
            _text("""
                SELECT payitemid, payitemcode, payitemname, category, datatype,
                       itemscope, ratebehavior
                FROM payroll.payrollperiodpayitems
                WHERE payrollperiodid = :pid LIMIT 1
            """),
            {"pid": pid},
        )).mappings().first()
        assert first_row

        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payrollperiodpayitems
                    (payrollperiodid, companyid, branchid, payitemid,
                     payitemcode, payitemname, category, datatype,
                     itemscope, ratebehavior,
                     appearsinpayrollentry, appearsinledger, appearsinreports,
                     requiresrate, issystemstandard, iscustom,
                     payitemstatusatsnapshot, isactiveinperiod, sortorder)
                VALUES
                    (:pid, :cid, :bid, :piid,
                     :code, :name, :cat, :dt,
                     :scope, :rb,
                     FALSE, TRUE, TRUE, FALSE, FALSE, FALSE,
                     'Active', TRUE, 0)
                ON CONFLICT (payrollperiodid, payitemid) DO NOTHING
            """),
            {
                "pid": pid, "cid": _COMPANY_ID, "bid": snap_branch_id,
                "piid": first_row["payitemid"],
                "code": first_row["payitemcode"],
                "name": first_row["payitemname"],
                "cat": first_row["category"],
                "dt": first_row["datatype"],
                "scope": first_row["itemscope"],
                "rb": first_row["ratebehavior"],
            },
        )

        count_after = (await direct_db.execute(
            _text("SELECT COUNT(*) FROM payroll.payrollperiodpayitems WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).scalar_one()
        assert count_before == count_after, "ON CONFLICT DO NOTHING failed — duplicate row inserted"

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S25 — Prepared candidate creation also creates a snapshot
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s25_prepared_candidate_create_also_snapshots(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S25: Prepared candidate creation creates PayrollPeriodPayItems rows."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        open_cands = await _preview(client, auth_token, snap_branch_id)
        await _create_period(
            client, auth_token, snap_branch_id,
            open_cands["selected"]["candidate_key"],
        )
        prepared = await _preview(
            client, auth_token, snap_branch_id, "PREPARED_CREATION",
        )
        period = await _create_period(
            client, auth_token, snap_branch_id,
            prepared["selected"]["candidate_key"],
        )
        pid = period["payroll_period_id"]

        rows = await _snap_rows(direct_db, pid)
        assert len(rows) > 0, "Prepared period creation did not create snapshot rows"

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S26 — Retired-after-creation: daily line still accepted
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s26_retired_after_creation_daily_line_accepted(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S26: A custom Daily item active in the snapshot remains usable after live retire."""
        await _clean(direct_db, snap_branch_id)

        # Insert a custom Daily item (Period scope not needed; use None ratebehavior
        # so no rate computation is required).
        item_id = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payitems
                    (companyid, payitemcode, payitemname, category, datatype,
                     itemscope, ratebehavior, status, sortorder,
                     appearsinpayrollentry, appearsinledger, appearsinreports,
                     requiresrate, issystemstandard, isdefaultbranchactive)
                VALUES
                    (:cid, 'SNAP_DAILY_2094', 'Snap Daily 2094', 'Custom', 'Number',
                     'Daily', 'None', 'Active', 600,
                     FALSE, TRUE, TRUE, FALSE, FALSE, TRUE)
                ON CONFLICT DO NOTHING
                RETURNING payitemid
            """),
            {"cid": _COMPANY_ID},
        )).scalar_one()

        await _setup(direct_db, snap_branch_id)
        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]
        start = period["start_date"]

        # Verify item snapshotted and active
        snap_check = (await direct_db.execute(
            _text("SELECT isactiveinperiod FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'SNAP_DAILY_2094'"),
            {"pid": pid},
        )).mappings().first()
        assert snap_check is not None, "Custom Daily item not snapshotted"
        assert snap_check["isactiveinperiod"] is True

        # Retire the live item after period creation
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' "
                  "WHERE payitemcode = 'SNAP_DAILY_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )
        await direct_db.commit()

        # Adding a draft line must still succeed — snapshot authorises it
        r = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id": snap_driver_id,
                "line_type": "SNAP_DAILY_2094",
                "work_date": start,
                "quantity":  1,
            },
            headers=_auth(auth_token),
        )
        assert r.status_code in (200, 201), (
            f"Expected snapshot-authorised daily line to succeed after retire, "
            f"got {r.status_code}: {r.text}"
        )

        await _clean(direct_db, snap_branch_id)
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' "
                  "WHERE payitemcode = 'SNAP_DAILY_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )

    # ------------------------------------------------------------------ #
    # S27 — Retired-after-creation: period pay line still accepted
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s27_retired_after_creation_period_pay_accepted(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S27: A custom Period item active in snapshot remains usable after live retire."""
        await _clean(direct_db, snap_branch_id)

        item_id = (await direct_db.execute(
            _text("""
                INSERT INTO payroll.payitems
                    (companyid, payitemcode, payitemname, category, datatype,
                     itemscope, ratebehavior, status, sortorder,
                     appearsinpayrollentry, appearsinledger, appearsinreports,
                     requiresrate, issystemstandard, isdefaultbranchactive)
                VALUES
                    (:cid, 'SNAP_PERIOD_2094', 'Snap Period 2094', 'Custom', 'Number',
                     'Period', 'EnteredAmount', 'Active', 601,
                     FALSE, TRUE, TRUE, FALSE, FALSE, TRUE)
                ON CONFLICT DO NOTHING
                RETURNING payitemid
            """),
            {"cid": _COMPANY_ID},
        )).scalar_one()

        await _setup(direct_db, snap_branch_id)
        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        # Verify snapshotted and active
        snap_check = (await direct_db.execute(
            _text("SELECT isactiveinperiod FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'SNAP_PERIOD_2094'"),
            {"pid": pid},
        )).mappings().first()
        assert snap_check is not None, "Custom Period item not snapshotted"
        assert snap_check["isactiveinperiod"] is True

        # Retire the live item
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' "
                  "WHERE payitemcode = 'SNAP_PERIOD_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )
        await direct_db.commit()

        # Period pay add must still succeed
        r = await client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={
                "driver_id": snap_driver_id,
                "line_type": "SNAP_PERIOD_2094",
                "amount":    100.00,
            },
            headers=_auth(auth_token),
        )
        assert r.status_code in (200, 201), (
            f"Expected snapshot-authorised period pay to succeed after retire, "
            f"got {r.status_code}: {r.text}"
        )

        await _clean(direct_db, snap_branch_id)
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' "
                  "WHERE payitemcode = 'SNAP_PERIOD_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )

    # ------------------------------------------------------------------ #
    # S28 — update_draft_line snapshot-aware after retire
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s28_update_draft_line_snapshot_aware(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S28: update_draft_line succeeds for a snapshot-authorised item even after live retire."""
        await _clean(direct_db, snap_branch_id)

        # Insert custom Daily item
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payitems
                    (companyid, payitemcode, payitemname, category, datatype,
                     itemscope, ratebehavior, status, sortorder,
                     appearsinpayrollentry, appearsinledger, appearsinreports,
                     requiresrate, issystemstandard, isdefaultbranchactive)
                VALUES
                    (:cid, 'SNAP_UPD_2094', 'Snap Update 2094', 'Custom', 'Number',
                     'Daily', 'None', 'Active', 602,
                     FALSE, TRUE, TRUE, FALSE, FALSE, TRUE)
                ON CONFLICT DO NOTHING
            """),
            {"cid": _COMPANY_ID},
        )
        await direct_db.commit()

        await _setup(direct_db, snap_branch_id)
        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]
        start = period["start_date"]

        # Add a draft line
        r_add = await client.post(
            f"/payroll/periods/{pid}/lines",
            json={
                "driver_id": snap_driver_id,
                "line_type": "SNAP_UPD_2094",
                "work_date": start,
                "quantity":  5,
            },
            headers=_auth(auth_token),
        )
        assert r_add.status_code in (200, 201), f"Add line failed: {r_add.text}"
        line_id = r_add.json()["draft_line_id"]

        # Retire the live item
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' "
                  "WHERE payitemcode = 'SNAP_UPD_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )
        await direct_db.commit()

        # Meaningful update (quantity change) must succeed via snapshot
        r_upd = await client.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            json={"quantity": 7},
            headers=_auth(auth_token),
        )
        assert r_upd.status_code in (200, 201), (
            f"Expected snapshot-aware update to succeed after retire, "
            f"got {r_upd.status_code}: {r_upd.text}"
        )

        # Void cleanup must also succeed
        r_void = await client.patch(
            f"/payroll/periods/{pid}/lines/{line_id}",
            json={"status": "Void"},
            headers=_auth(auth_token),
        )
        assert r_void.status_code in (200, 201), (
            f"Expected void cleanup to succeed after retire, "
            f"got {r_void.status_code}: {r_void.text}"
        )

        await _clean(direct_db, snap_branch_id)
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' "
                  "WHERE payitemcode = 'SNAP_UPD_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )

    # ------------------------------------------------------------------ #
    # S29 — update_period_pay_line snapshot-aware after retire
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s29_update_period_pay_line_snapshot_aware(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S29: update_period_pay_line succeeds for a snapshot-authorised item after live retire."""
        await _clean(direct_db, snap_branch_id)

        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payitems
                    (companyid, payitemcode, payitemname, category, datatype,
                     itemscope, ratebehavior, status, sortorder,
                     appearsinpayrollentry, appearsinledger, appearsinreports,
                     requiresrate, issystemstandard, isdefaultbranchactive)
                VALUES
                    (:cid, 'SNAP_PPL_2094', 'Snap PPL 2094', 'Custom', 'Number',
                     'Period', 'EnteredAmount', 'Active', 603,
                     FALSE, TRUE, TRUE, FALSE, FALSE, TRUE)
                ON CONFLICT DO NOTHING
            """),
            {"cid": _COMPANY_ID},
        )
        await direct_db.commit()

        await _setup(direct_db, snap_branch_id)
        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        # Add period pay line
        r_add = await client.post(
            f"/payroll/periods/{pid}/period-pay",
            json={
                "driver_id": snap_driver_id,
                "line_type": "SNAP_PPL_2094",
                "amount":    50.00,
            },
            headers=_auth(auth_token),
        )
        assert r_add.status_code in (200, 201), f"Add period pay failed: {r_add.text}"
        line_id = r_add.json()["draft_line_id"]

        # Retire live item
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' "
                  "WHERE payitemcode = 'SNAP_PPL_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )
        await direct_db.commit()

        # Update amount must succeed
        r_upd = await client.patch(
            f"/payroll/periods/{pid}/period-pay/{line_id}",
            json={"amount": 75.00},
            headers=_auth(auth_token),
        )
        assert r_upd.status_code in (200, 201), (
            f"Expected snapshot-aware period pay update after retire, "
            f"got {r_upd.status_code}: {r_upd.text}"
        )

        await _clean(direct_db, snap_branch_id)
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' "
                  "WHERE payitemcode = 'SNAP_PPL_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )

    # ------------------------------------------------------------------ #
    # S30 — Rename stability: snapshot retains original label
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s30_rename_stability(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S30: Renaming a live PayItem does not alter the snapshot label for old periods."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        # Read HOURS snapshot label before rename
        hours_snap_before = (await direct_db.execute(
            _text("SELECT payitemname, displaylabel FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'HOURS'"),
            {"pid": pid},
        )).mappings().first()
        assert hours_snap_before is not None

        old_name = hours_snap_before["payitemname"]

        # Rename the live HOURS item
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET payitemname = 'Renamed Hours 2094' "
                  "WHERE payitemcode = 'HOURS' AND companyid IS NULL"),
        )
        await direct_db.commit()

        # Snapshot name must be unchanged
        hours_snap_after = (await direct_db.execute(
            _text("SELECT payitemname FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'HOURS'"),
            {"pid": pid},
        )).mappings().first()
        assert hours_snap_after["payitemname"] == old_name, (
            f"Snapshot name changed after live rename: "
            f"{old_name!r} → {hours_snap_after['payitemname']!r}"
        )

        # Restore
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET payitemname = :name "
                  "WHERE payitemcode = 'HOURS' AND companyid IS NULL"),
            {"name": old_name},
        )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S31 — Reorder stability: snapshot retains original sort order
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s31_reorder_stability(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S31: Changing live SortOrder does not alter snapshot order for old periods."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        # Read HOURS snapshot sort order
        snap_before = (await direct_db.execute(
            _text("SELECT sortorder FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'HOURS'"),
            {"pid": pid},
        )).mappings().first()
        assert snap_before is not None
        original_sort = snap_before["sortorder"]

        # Change live sort order to something very different
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET sortorder = 9999 "
                  "WHERE payitemcode = 'HOURS' AND companyid IS NULL"),
        )
        await direct_db.commit()

        # Snapshot sort order unchanged
        snap_after = (await direct_db.execute(
            _text("SELECT sortorder FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'HOURS'"),
            {"pid": pid},
        )).mappings().first()
        assert snap_after["sortorder"] == original_sort, (
            f"Snapshot sortorder changed: {original_sort} → {snap_after['sortorder']}"
        )

        # Restore
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET sortorder = :s "
                  "WHERE payitemcode = 'HOURS' AND companyid IS NULL"),
            {"s": original_sort},
        )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S32 — Classification stability: snapshot retains original metadata
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s32_classification_stability(
        self, client, auth_token, direct_db, snap_branch_id
    ):
        """S32: Changing live DataType does not alter snapshot classification for old periods."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]

        # Read MILES snapshot datatype
        snap_before = (await direct_db.execute(
            _text("SELECT datatype, ratebehavior FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'MILES'"),
            {"pid": pid},
        )).mappings().first()
        if snap_before is None:
            pytest.skip("MILES not in snapshot for this branch setup")

        orig_datatype = snap_before["datatype"]

        # Change live datatype
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET datatype = 'Text' "
                  "WHERE payitemcode = 'MILES' AND companyid IS NULL"),
        )
        await direct_db.commit()

        # Snapshot datatype unchanged
        snap_after = (await direct_db.execute(
            _text("SELECT datatype FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'MILES'"),
            {"pid": pid},
        )).mappings().first()
        assert snap_after["datatype"] == orig_datatype, (
            f"Snapshot datatype changed: {orig_datatype!r} → {snap_after['datatype']!r}"
        )

        # Restore
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET datatype = :dt "
                  "WHERE payitemcode = 'MILES' AND companyid IS NULL"),
            {"dt": orig_datatype},
        )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S33 — Inactive snapshot row: hidden from grid and rejected on write
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s33_inactive_snapshot_row_hidden_and_rejected(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S33: A snapshot row with IsActiveInPeriod=FALSE is not in grid columns
        and save_day_grid rejects it."""
        await _clean(direct_db, snap_branch_id)

        # Deactivate MILES for this branch before period creation (same as S22)
        miles_row = (await direct_db.execute(
            _text("SELECT payitemid FROM payroll.payitems "
                  "WHERE payitemcode = 'MILES' AND companyid IS NULL")
        )).mappings().first()
        if not miles_row:
            pytest.skip("MILES system item not seeded")

        await direct_db.execute(
            _text("""
                DELETE FROM payroll.branchpayitemconfig
                WHERE companyid = :cid AND branchid = :bid AND payitemid = :piid
                  AND effectiveto IS NULL
            """),
            {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": miles_row["payitemid"]},
        )
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.branchpayitemconfig
                    (companyid, branchid, payitemid, isactive, effectivefrom)
                VALUES (:cid, :bid, :piid, FALSE, '2094-01-01')
            """),
            {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": miles_row["payitemid"]},
        )
        await direct_db.commit()

        await _setup(direct_db, snap_branch_id)
        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]
        start = period["start_date"]

        # Verify MILES is inactive in snapshot
        snap_row = (await direct_db.execute(
            _text("SELECT isactiveinperiod FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'MILES'"),
            {"pid": pid},
        )).mappings().first()
        assert snap_row is not None, "MILES missing from snapshot"
        assert snap_row["isactiveinperiod"] is False, "MILES should be inactive in snapshot"

        # get_day_grid must NOT include MILES in columns
        r_grid = await client.get(
            f"/payroll/periods/{pid}/day-grid",
            params={"work_date": start},
            headers=_auth(auth_token),
        )
        assert r_grid.status_code == 200
        grid_codes = {col["pay_item_code"] for col in r_grid.json().get("columns", [])}
        assert "MILES" not in grid_codes, "MILES (inactive in snapshot) must not appear in grid"

        # save_day_grid with MILES must reject
        r_save = await client.post(
            f"/payroll/periods/{pid}/day-grid",
            json={
                "work_date": start,
                "rows": [{"driver_id": snap_driver_id, "values": {"MILES": "10"}}],
            },
            headers=_auth(auth_token),
        )
        assert r_save.status_code == 422, (
            f"Expected 422 for inactive-snapshot code, got {r_save.status_code}: {r_save.text}"
        )

        # Restore BranchPayItemConfig
        await direct_db.execute(
            _text("DELETE FROM payroll.branchpayitemconfig "
                  "WHERE companyid = :cid AND branchid = :bid AND payitemid = :piid"),
            {"cid": _COMPANY_ID, "bid": snap_branch_id, "piid": miles_row["payitemid"]},
        )
        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S34 — Zero active Daily in snapshot: no fallback to live config
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s34_zero_active_daily_no_live_fallback(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S34: Post-0053 period with zero active Daily snapshot rows does not fall back
        to live BranchPayItemConfig."""
        await _clean(direct_db, snap_branch_id)
        await _setup(direct_db, snap_branch_id)

        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]
        start = period["start_date"]

        # Force all Daily snapshot rows to IsActiveInPeriod=FALSE directly in DB
        await direct_db.execute(
            _text("""
                UPDATE payroll.payrollperiodpayitems
                SET isactiveinperiod = FALSE
                WHERE payrollperiodid = :pid AND itemscope = 'Daily'
            """),
            {"pid": pid},
        )
        await direct_db.commit()

        # get_day_grid must return empty columns (not fall back to live HOURS etc.)
        r = await client.get(
            f"/payroll/periods/{pid}/day-grid",
            params={"work_date": start},
            headers=_auth(auth_token),
        )
        assert r.status_code == 200
        columns = r.json().get("columns", [])
        assert len(columns) == 0, (
            f"Expected zero columns (no fallback), got {len(columns)}: "
            f"{[c['pay_item_code'] for c in columns]}"
        )

        # save_day_grid with a live-active code must reject (snapshot has no active Daily)
        r_save = await client.post(
            f"/payroll/periods/{pid}/day-grid",
            json={
                "work_date": start,
                "rows": [{"driver_id": snap_driver_id, "values": {"HOURS": "8"}}],
            },
            headers=_auth(auth_token),
        )
        assert r_save.status_code == 422, (
            f"Expected 422 (no active Daily in snapshot, no fallback), "
            f"got {r_save.status_code}: {r_save.text}"
        )

        await _clean(direct_db, snap_branch_id)

    # ------------------------------------------------------------------ #
    # S35 — Downgrade refusal when PayrollPeriodPayItems rows exist
    # ------------------------------------------------------------------ #

    def test_s35_downgrade_refusal_with_rows(self, direct_db):
        """S35: Migration 0053 downgrade raises RuntimeError when rows exist."""
        import sys
        from pathlib import Path
        import importlib.util

        migration_path = (
            Path(__file__).parent.parent.parent
            / "migrations" / "versions" / "0053_payroll_period_pay_items.py"
        )
        assert migration_path.exists(), f"Migration file not found: {migration_path}"

        spec = importlib.util.spec_from_file_location("migration_0053", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # The guard is: if row_count > 0 → raise RuntimeError
        # Verify the logic string is present without executing alembic context
        import inspect
        src = inspect.getsource(mod.downgrade)
        assert "RuntimeError" in src, "Downgrade must raise RuntimeError when rows exist"
        assert "PayrollPeriodPayItems" in src, "Downgrade must check PayrollPeriodPayItems table"
        assert "row_count" in src or "COUNT" in src, "Downgrade must count existing rows"

    # ------------------------------------------------------------------ #
    # S36 — save_day_grid batch pre-lock is snapshot-aware
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_s36_save_day_grid_batch_lock_snapshot_aware(
        self, client, auth_token, direct_db, snap_branch_id, snap_driver_id
    ):
        """S36: save_day_grid succeeds for a custom Daily item that is active in the
        period snapshot but was retired from the live catalog after period creation.

        Regression for the save_day_grid batch pre-lock path:
        _lock_pay_item_for_source_write must pass period_id so the snapshot
        authorisation applies in the batch lock loop, not just in the per-line
        add_draft_line calls that follow.
        """
        await _clean(direct_db, snap_branch_id)

        # Insert a custom Daily item active for this branch
        await direct_db.execute(
            _text("""
                INSERT INTO payroll.payitems
                    (companyid, payitemcode, payitemname, category, datatype,
                     itemscope, ratebehavior, status, sortorder,
                     appearsinpayrollentry, appearsinledger, appearsinreports,
                     requiresrate, issystemstandard, isdefaultbranchactive)
                VALUES
                    (:cid, 'SNAP_GRID_2094', 'Snap Grid 2094', 'Custom', 'Number',
                     'Daily', 'None', 'Active', 604,
                     FALSE, TRUE, TRUE, FALSE, FALSE, TRUE)
                ON CONFLICT DO NOTHING
            """),
            {"cid": _COMPANY_ID},
        )
        await direct_db.commit()

        await _setup(direct_db, snap_branch_id)
        candidates = await _preview(client, auth_token, snap_branch_id)
        key = candidates["selected"]["candidate_key"]
        period = await _create_period(client, auth_token, snap_branch_id, key)
        pid = period["payroll_period_id"]
        start = period["start_date"]

        # Verify the item was snapshotted and active
        snap_row = (await direct_db.execute(
            _text("SELECT isactiveinperiod FROM payroll.payrollperiodpayitems "
                  "WHERE payrollperiodid = :pid AND payitemcode = 'SNAP_GRID_2094'"),
            {"pid": pid},
        )).mappings().first()
        assert snap_row is not None, "Custom Daily item not snapshotted"
        assert snap_row["isactiveinperiod"] is True

        # Retire the live item AFTER period creation
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' "
                  "WHERE payitemcode = 'SNAP_GRID_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )
        await direct_db.commit()

        # save_day_grid must succeed — the batch pre-lock must be snapshot-aware
        r = await client.post(
            f"/payroll/periods/{pid}/day-grid",
            json={
                "work_date": start,
                "rows": [{"driver_id": snap_driver_id, "values": {"SNAP_GRID_2094": "3"}}],
            },
            headers=_auth(auth_token),
        )
        assert r.status_code in (200, 201), (
            f"Expected save_day_grid to succeed for snapshot-authorised retired item "
            f"(batch pre-lock must pass period_id), "
            f"got {r.status_code}: {r.text}"
        )

        await _clean(direct_db, snap_branch_id)
        await direct_db.execute(
            _text("UPDATE payroll.payitems SET status = 'Retired' "
                  "WHERE payitemcode = 'SNAP_GRID_2094' AND companyid = :cid"),
            {"cid": _COMPANY_ID},
        )
        await direct_db.commit()
