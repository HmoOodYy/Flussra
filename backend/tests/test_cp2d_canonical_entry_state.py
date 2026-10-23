"""
CP-2D1: Canonical daily driver/day entry-state tests.

Product contracts verified:
  - Migration 0054: PayrollPeriodDriverDayEntryState table exists with required
    columns, unique constraint, FKs, and indexes.
  - Live dropdown: newly-created active Status Key appears in get_day_grid
    without period reopen (not snapshotted at creation time).
  - save_day_grid creates/upserts a canonical entry-state row for each driver row.
  - Canonical row has correct StatusKeyID, NoteText, IsVoided, CompanyID, BranchID.
  - get_day_grid reads Status and notes only from canonical EntryState.
  - Deactivated key bypass: re-submitting the same status code when the key has
    since been deactivated → 200 (no change to status).
  - Voided entry state: IsVoided=TRUE when both StatusKeyID and NoteText are cleared.
  - Finalization snapshot: finalize_period fills StatusCodeSnapshot,
    StatusLabelSnapshot, StatusIsOffReasonSnapshot, FinalizedAtUtc on canonical rows.
  - Locked period display: get_day_grid uses snapshot values (not live lookup) for
    Locked periods.
  - Historical pseudo-lines are ignored and are never created by Day Grid.
  - Tenant isolation: canonical rows are scoped by CompanyID; cross-company
    rows are not visible.
  - Ordinary DraftLines remain keyed to PayrollPeriodDefinitionID.


Dates: 2096-* — isolated year (2094=CP-2C, 2095=CP-2B).
Run from backend/:
    python -m pytest tests/test_cp2d_canonical_entry_state.py -v
"""
import datetime
import itertools
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text as _text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.builders.final_evidence import seed_finalized_evidence
from tests.db_state import (
    PERIOD_EVIDENCE_IMMUTABLE_TRIGGERS,
    suspended_test_triggers,
)

# ---------------------------------------------------------------------------
# Shared helpers / constants
# ---------------------------------------------------------------------------

_COMPANY_ID = 1
_BASE_MONDAY_2096 = datetime.date(2096, 1, 7)

_CTR = itertools.count(0)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _week_2096(offset: int = 0) -> tuple[datetime.date, datetime.date]:
    n = next(_CTR) + offset
    start = _BASE_MONDAY_2096 + datetime.timedelta(weeks=n)
    return start, start + datetime.timedelta(days=6)


def _sk_code(prefix: str, date: datetime.date) -> str:
    """Build a status-key code safe for the normalizedstatuscode constraint (no hyphens)."""
    return f"{prefix}_{date.strftime('%Y%m%d')}"


async def _clean_branch(db: AsyncConnection, branch_id: int) -> None:
    """
    Physically remove every PayrollPeriods row this module created for the
    branch, and everything that references it -- zero footprint. The PAYTEST
    branch is shared by ~65 other test files, so leaving rows behind (even as
    a terminal Cancelled status) would accumulate unbounded cross-suite data.

    CP-4C/CP-4D/CP-5C/Phase6/P6D (migrations 0061-0065) added several
    ON DELETE RESTRICT children of PayrollPeriods and PayrollCalculationSnapshots
    that this fixture predates: PayrollCalculationSnapshots and its own
    children (StatusEntries, BonusEvents, Lines, DriverTotals,
    UsedRateDefinitions), PayrollPeriodWorkflowActionEvidence, and the three
    P6D PayrollPeriodAuditEvidence* tables -- each protected by its own
    immutable BEFORE UPDATE OR DELETE trigger, plus a BEFORE DELETE guard
    directly on PayrollPeriods (trg_PayrollPeriods_AuditEvidenceDelete).
    Deleting a snapshot-backed or audit-evidenced period now requires
    clearing that whole chain first, in FK-verified leaf-to-root order.
    review.ManagerReviewItems.PayrollCalculationSnapshotID (CP-4D, migration
    0062) also RESTRICTs deletion of PayrollCalculationSnapshots, and
    review.ManagerReviewDecisions / payroll.PayrollPeriods.CurrentReturnReviewItemID
    (0001/0048) both RESTRICT deletion of the review item itself -- so the
    review-domain rows this module's own PeriodApproval Submit/Decide/Resubmit
    calls create are deleted outright (leaf-to-root: Decisions, then Items),
    not left behind. Neither review table has an immutable trigger of its own.

    Each table's own specifically-named immutable trigger is disabled only
    for the duration of this cleanup -- never DISABLE TRIGGER ALL, which was
    tried and rejected: it also silently suspends
    PayrollPeriodDriverDayEntryState's normal ON DELETE CASCADE from
    PayrollPeriods, leaving those rows behind unexpectedly.
    """
    # (table, trigger) pairs, in the exact order each table's DELETE below
    # needs its own guard disabled. All names verified against migrations
    # 0035 (final_line_immutable) and 0061/0063/0064/0065 (everything else).
    async with suspended_test_triggers(db, PERIOD_EVIDENCE_IMMUTABLE_TRIGGERS):
        period_subq = "(SELECT payrollperiodid FROM payroll.payrollperiods WHERE branchid = :bid)"
        snapshot_subq = (
            "(SELECT payrollcalculationsnapshotid FROM payroll.payrollcalculationsnapshots "
            f"WHERE payrollperiodid IN {period_subq})"
        )
        drivertotal_subq = (
            "(SELECT payrollcalculationdrivertotalid FROM payroll.payrollcalculationdrivertotals "
            f"WHERE payrollcalculationsnapshotid IN {snapshot_subq})"
        )
        # ManagerReviewItems has no real FK to PayrollPeriods -- the link is
        # the polymorphic (EntitySchema, EntityName, EntityID) convention this
        # module's own Submit/Resubmit calls create (entityschema='payroll',
        # entityname='PayrollPeriods', entityid=str(period_id)), confirmed
        # against app/payroll/period_lifecycle.py's review-item creation sites.
        review_items_subq = (
            "(SELECT reviewitemid FROM review.managerreviewitems "
            "WHERE entityschema = 'payroll' AND entityname = 'PayrollPeriods' "
            "AND entityid IN (SELECT payrollperiodid::text FROM payroll.payrollperiods WHERE branchid = :bid))"
        )

        # Leaf-to-root, verified against each table's actual FK targets:
        # SnapshotLines -> DriverTotals/UsedRateDefinitions -> Snapshots;
        # AuditEvidenceSnapshotEvents -> AuditEvidenceEvents AND Snapshots;
        # StatusEntries/BonusEvents/UsedRateDefinitions/WorkflowActionEvidence
        # -> Snapshots (and/or Periods directly, all have PayrollPeriodID);
        # Snapshots/FinalLines/DraftLines/EntryState/AuditEvidenceCoverage
        # -> Periods; Periods last.
        for stmt in (
            f"DELETE FROM payroll.payrollfinallines WHERE payrollperiodid IN {period_subq}",
            f"DELETE FROM payroll.payrollcalculationsnapshotlines "
            f"WHERE payrollcalculationdrivertotalid IN {drivertotal_subq}",
            f"DELETE FROM payroll.payrollcalculationsnapshotcomponents WHERE payrollperiodid IN {period_subq}",
            f"DELETE FROM payroll.payrollcalculationsnapshotminmaxrules WHERE payrollperiodid IN {period_subq}",
            f"DELETE FROM payroll.payrollcalculationsnapshotminmaxevaluations WHERE payrollperiodid IN {period_subq}",

            f"DELETE FROM payroll.payrollperiodauditevidencesnapshotevents "
            f"WHERE payrollperiodid IN {period_subq}",

            f"DELETE FROM payroll.payrollperiodauditevidenceevents "
            f"WHERE payrollperiodid IN {period_subq}",

            f"DELETE FROM payroll.payrollperiodauditevidencecoverage "
            f"WHERE payrollperiodid IN {period_subq}",

            f"DELETE FROM payroll.payrollcalculationsnapshotusedratedefinitions "
            f"WHERE payrollperiodid IN {period_subq}",

            f"DELETE FROM payroll.payrollcalculationdrivertotals "
            f"WHERE payrollcalculationsnapshotid IN {snapshot_subq}",

            f"DELETE FROM payroll.payrollcalculationsnapshotstatusentries "
            f"WHERE payrollperiodid IN {period_subq}",

            f"DELETE FROM payroll.payrollcalculationsnapshotbonusevents "
            f"WHERE payrollperiodid IN {period_subq}",

            f"DELETE FROM payroll.payrollperiodworkflowactionevidence "
            f"WHERE payrollperiodid IN {period_subq}",

            # PayrollPeriods.CurrentReturnReviewItemID (0048) RESTRICTs
            # deleting the review item it points to -- clear it first (the
            # period row is being deleted below anyway, so this is a pure
            # unblock, not a behavior change).
            "UPDATE payroll.payrollperiods SET currentreturnreviewitemid = NULL "
            "WHERE branchid = :bid",

            # ManagerReviewDecisions (0001) RESTRICTs deleting the
            # ManagerReviewItems row it belongs to -- delete children first.
            f"DELETE FROM review.managerreviewdecisions "
            f"WHERE reviewitemid IN {review_items_subq}",

            # Deleting the review item outright (rather than only nulling its
            # PayrollCalculationSnapshotID) also removes the leftover
            # Pending/Approved/EditRequested/Rejected rows this module's own
            # PeriodApproval Submit/Decide/Resubmit calls create -- otherwise
            # they orphan-reference the PayrollPeriods rows deleted below and
            # accumulate indefinitely on the shared PAYTEST branch.
            f"DELETE FROM review.managerreviewitems "
            f"WHERE reviewitemid IN {review_items_subq}",

            f"DELETE FROM payroll.payrollcalculationsnapshots "
            f"WHERE payrollperiodid IN {period_subq}",

            "DELETE FROM payroll.payrollfinallines "
            f"WHERE payrollperiodid IN {period_subq}",

            "DELETE FROM payroll.payrolldraftlines "
            f"WHERE payrollperiodid IN {period_subq}",

            # PayrollPeriodDriverDayEntryState is normally ON DELETE CASCADE
            # from PayrollPeriods; deleted explicitly here since its own
            # trigger isn't in the disabled set (it has no guard of its own)
            # and callers need its StatusKeyID references gone before they
            # can delete the PayrollStatusKeys rows they created.
            f"DELETE FROM payroll.payrollperioddriverdayentrystate "
            f"WHERE payrollperiodid IN {period_subq}",

            "DELETE FROM payroll.payrollperiods WHERE branchid = :bid",
        ):
            await db.execute(_text(stmt), {"bid": branch_id})
    await db.commit()


async def _open_period(
    db: AsyncConnection,
    branch_id: int,
    start: datetime.date,
    end: datetime.date,
    code_suffix: str = "",
) -> int:
    code = f"CP2D-{branch_id}-{start.isoformat()}{code_suffix}"
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollperiods
                (companyid, branchid, status, periodcode, periodname, periodtype, startdate, enddate)
            VALUES (1, :bid, 'Open', :code, :name, 'Week', :start, :end)
            ON CONFLICT DO NOTHING
            RETURNING payrollperiodid
        """),
        {"bid": branch_id, "code": code, "name": f"CP2D {start}", "start": start, "end": end},
    )).mappings().first()
    await db.commit()
    if r is None:
        r = (await db.execute(
            _text("SELECT payrollperiodid FROM payroll.payrollperiods "
                  "WHERE branchid = :bid AND periodcode = :code"),
            {"bid": branch_id, "code": code},
        )).mappings().first()
    assert r is not None, f"Failed to create or find period with code {code!r}"
    period_id = r["payrollperiodid"]
    from app.payroll.eligibility import _create_period_driver_eligibility_rows

    actor_id = (await db.execute(_text(
        "SELECT UserID FROM sec.Users WHERE CompanyID = 1 AND Username = 'admin' LIMIT 1"
    ))).scalar_one()
    await _create_period_driver_eligibility_rows(
        period_id, 1, branch_id, db, snapshot_source="Generated", freeze=True,
        frozen_by_user_id=actor_id,
    )
    await db.commit()
    return period_id


async def _insert_status_key(
    db: AsyncConnection,
    company_id: int,
    branch_id: int,
    code: str,
    *,
    is_off_reason: bool = False,
    is_active: bool = True,
) -> int:
    r = (await db.execute(
        _text("""
            INSERT INTO payroll.payrollstatuskeys
                (companyid, statuscode, normalizedstatuscode, keyname, normalizedkeyname,
                 allowancecategory, isoffreason, hoursvalue, isactive, displayorder)
            VALUES (:cid, :code, :norm, :name, :nname, 'Other', :off, 8.0, :active, 99)
            RETURNING statuskeyid
        """),
        {
            "cid": company_id,
            "code": code, "norm": code,
            "name": f"Test Key {code}", "nname": f"Test Key {code}".lower(),
            "off": is_off_reason, "active": is_active,
        },
    )).mappings().first()
    await db.commit()
    return r["statuskeyid"]


async def _insert_approved_review_item(
    db: AsyncConnection, company_id: int, branch_id: int, period_id: int, snapshot_id: int,
) -> int:
    """Bind a snapshot to a period the way an Approved PeriodApproval review
    item does -- the exact selector app.payroll.finalization._load_approved_snapshot_packet
    (and therefore finalize_period) trusts, and the one
    status_evidence.resolve_finalized_snapshot mirrors for Locked/Archived
    Day Grid reads (Stage B3 Unit 8C-3)."""
    r = (await db.execute(
        _text("""
            INSERT INTO review.managerreviewitems
                (companyid, branchid, requesttype, entityschema, entityname, entityid,
                 title, status, payrollcalculationsnapshotid)
            VALUES (:cid, :bid, 'PeriodApproval', 'payroll', 'PayrollPeriods', :pid_text,
                    'Test review item', 'Approved', :sid)
            RETURNING reviewitemid
        """),
        {
            "cid": company_id, "bid": branch_id, "pid_text": str(period_id), "sid": snapshot_id,
        },
    )).mappings().first()
    await db.commit()
    return r["reviewitemid"]


async def _canonical_rows(db: AsyncConnection, period_id: int) -> list[dict]:
    rows = (await db.execute(
        _text("""
            SELECT payrollperioddriverdayentrystateid, payrollperiodid,
                   companyid, branchid, driverassignmentid, workdate,
                   statuskeyid, notetext, isvoided,
                   statuscodesnapshot, statuslabelsnapshot,
                   statusisoffreasonsnapshot, finalizedatutc
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  payrollperiodid = :pid
            ORDER BY driverassignmentid, workdate
        """),
        {"pid": period_id},
    )).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(scope="class")
async def ces_branch_id(session_db_conn) -> int:
    row = (await session_db_conn.execute(
        _text("""
            INSERT INTO core.branches
                (companyid, branchcode, branchname, status, isdefault, timezoneid)
            VALUES (1, :code, :name, 'Active', FALSE, 'UTC')
            RETURNING branchid
        """),
        {"code": (code := f"CP2D_{uuid.uuid4().hex[:10]}"), "name": code},
    )).mappings().first()
    await session_db_conn.commit()
    assert row is not None
    return row["branchid"]


@pytest_asyncio.fixture(scope="class")
async def ces_driver_assignment_id(session_client, auth_token: str, ces_branch_id: int) -> int:
    from tests.builders.workforce import create_workforce_driver
    driver = await create_workforce_driver(
        session_client, auth_token, ces_branch_id, "CES Driver Alpha", "CESA-2096",
        preferred_name="CESA", cdl_number="CDL-CESA-2096", email="cesa2096@example.com",
    )
    return driver["driver_assignment_id"]


@pytest_asyncio.fixture(scope="class")
async def ces_driver_assignment_id_b(session_client, auth_token: str, ces_branch_id: int) -> int:
    """Second driver for per-driver precedence tests."""
    from tests.builders.workforce import create_workforce_driver
    driver = await create_workforce_driver(
        session_client, auth_token, ces_branch_id, "CES Driver Beta", "CESB-2096",
        preferred_name="CESB", cdl_number="CDL-CESB-2096", email="cesb2096@example.com",
    )
    return driver["driver_assignment_id"]


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestCp2dCanonicalEntryState:

    # ------------------------------------------------------------------ #
    # E01 — Migration schema
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e01_migration_schema(self, direct_db):
        """E01: PayrollPeriodDriverDayEntryState table exists with required columns."""
        r = await direct_db.execute(
            _text("""
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'payroll'
                  AND table_name   = 'payrollperioddriverdayentrystate'
            """)
        )
        assert r.first() is not None, "payroll.PayrollPeriodDriverDayEntryState table missing"

        cols_r = await direct_db.execute(
            _text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'payroll'
                  AND table_name   = 'payrollperioddriverdayentrystate'
            """)
        )
        cols = {row["column_name"] for row in cols_r.mappings().all()}
        required = {
            "payrollperioddriverdayentrystateid", "companyid", "branchid",
            "payrollperiodid", "payrollperioddayid", "workdate", "driverassignmentid",
            "statuskeyid", "notetext",
            "statuscodesnapshot", "statuslabelsnapshot",
            "statusisoffreasonsnapshot", "statushoursvaluesnapshot",
            "finalizedatutc", "isvoided",
            "createdbyuserid", "updatedbyuserid", "createdatutc", "updatedatutc",
        }
        missing = required - cols
        assert not missing, f"Missing columns: {sorted(missing)}"

    @pytest.mark.asyncio
    async def test_e01b_unique_constraint(self, direct_db, ces_branch_id: int):
        """E01b: Unique constraint rejects duplicate (PayrollPeriodID, DriverAssignmentID, WorkDate)."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e01b")
        try:
            shared_params = {
                "cid": _COMPANY_ID, "bid": ces_branch_id,
                "pid": pid, "dt": start, "did": 999991, "uid": 1,
            }
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrollperioddriverdayentrystate
                        (companyid, branchid, payrollperiodid, workdate, driverassignmentid,
                         isvoided, createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
                    VALUES (:cid, :bid, :pid, :dt, :did, FALSE, :uid, :uid, NOW(), NOW())
                """),
                shared_params,
            )
            with pytest.raises(Exception):
                await direct_db.execute(
                    _text("""
                        INSERT INTO payroll.payrollperioddriverdayentrystate
                            (companyid, branchid, payrollperiodid, workdate, driverassignmentid,
                             isvoided, createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
                        VALUES (:cid, :bid, :pid, :dt, :did, FALSE, :uid, :uid, NOW(), NOW())
                    """),
                    shared_params,
                )
        finally:
            await direct_db.rollback()
            await _clean_branch(direct_db, ces_branch_id)

    @pytest.mark.asyncio
    async def test_e01c_indexes_exist(self, direct_db):
        """E01c: Required indexes exist on the canonical table."""
        idx_r = await direct_db.execute(
            _text("""
                SELECT indexname FROM pg_indexes
                WHERE schemaname = 'payroll'
                  AND tablename  = 'payrollperioddriverdayentrystate'
            """)
        )
        indexes = {row["indexname"] for row in idx_r.mappings().all()}
        required_indexes = {
            "ix_ppdes_period_date", "ix_ppdes_period_assignment",
            "ix_ppdes_company_branch", "ix_ppdes_statuskey", "ix_ppdes_period_finalized",
        }
        missing = required_indexes - {i.lower() for i in indexes}
        assert not missing, f"Missing indexes: {sorted(missing)}"

    # ------------------------------------------------------------------ #
    # E02 — Live dropdown (not snapshotted at period creation)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e02_live_status_key_dropdown(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E02: A Status Key added AFTER period creation appears in get_day_grid."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e02")
        new_code = _sk_code("LIVE2096", start)
        try:
            headers = _auth(auth_token)
            wdate = str(start)

            # Confirm new_code not in dropdown before insert
            r1 = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r1.status_code == 200
            before_codes = {sk["key_code"] for sk in r1.json().get("status_keys", [])}
            assert new_code not in before_codes, "Key should not exist yet"

            # Insert the new key
            await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, new_code)

            # Dropdown now includes new key — no period reopen needed
            r2 = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r2.status_code == 200
            after_codes = {sk["key_code"] for sk in r2.json().get("status_keys", [])}
            assert new_code in after_codes, "Newly-added Status Key must appear in dropdown"
        finally:
            await direct_db.execute(
                _text("DELETE FROM payroll.payrollstatuskeys WHERE statuscode = :code AND companyid = "
                      "(SELECT companyid FROM core.branches WHERE branchid = :bid)"),
                {"code": new_code, "bid": ces_branch_id},
            )
            await direct_db.commit()
            await _clean_branch(direct_db, ces_branch_id)

    # ------------------------------------------------------------------ #
    # E03 — Canonical storage via save_day_grid
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e03_save_day_grid_creates_canonical_row(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E03: save_day_grid writes a canonical entry-state row with correct fields."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e03")
        code = _sk_code("E03KEY", start)
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
            wdate = str(start)
            headers = _auth(auth_token)

            r = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={
                    "work_date": wdate,
                    "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {},
                               "status_key": code, "notes": "Test note"}],
                },
            )
            assert r.status_code == 200, f"save_day_grid failed: {r.text}"

            # Verify canonical row in DB
            rows = await _canonical_rows(direct_db, pid)
            ces = [row for row in rows if row["driverassignmentid"] == ces_driver_assignment_id]
            assert len(ces) == 1, f"Expected 1 canonical row, got {len(ces)}"
            assert ces[0]["statuskeyid"] == sk_id
            assert ces[0]["notetext"] == "Test note"
            assert ces[0]["isvoided"] is False
            assert ces[0]["companyid"] == _COMPANY_ID
            assert ces[0]["branchid"] == ces_branch_id
            assert ces[0]["finalizedatutc"] is None, "Should not be finalized yet"

            # Status and note are canonical EntryState only; measured quantity lines remain separate.
            pseudo_count = (await direct_db.execute(
                _text("""
                    SELECT count(*) FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverassignmentid = :did
                      AND linetype IN ('DailyStatus', 'DailyNote') AND status != 'Void'
                """),
                {"pid": pid, "did": ces_driver_assignment_id},
            )).scalar_one()
            assert pseudo_count == 0
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E04 — Clearing sets IsVoided=TRUE when both fields empty
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e04_clear_both_sets_isvoided(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E04: Clearing both status and note sets IsVoided=TRUE on canonical row."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e04")
        code = _sk_code("E04KEY", start)
        sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            # Set status + note
            await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {},
                                 "status_key": code, "notes": "A note"}]},
            )
            # Clear both
            r = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {},
                                 "status_key": None, "notes": None}]},
            )
            assert r.status_code == 200

            rows = await _canonical_rows(direct_db, pid)
            ces = [row for row in rows if row["driverassignmentid"] == ces_driver_assignment_id]
            assert len(ces) == 1
            assert ces[0]["isvoided"] is True
            assert ces[0]["statuskeyid"] is None
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E05 — get_day_grid canonical-first read (editable period)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e05_get_day_grid_canonical_first_read(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E05: get_day_grid returns canonical row values when canonical row exists."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e05")
        code = _sk_code("E05KEY", start)
        sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {},
                                 "status_key": code, "notes": "E05 note"}]},
            )
            r = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r.status_code == 200
            data = r.json()
            drv_row = next(
                (d for d in data["rows"] if d["driver_assignment_id"] == ces_driver_assignment_id), None
            )
            assert drv_row is not None
            assert drv_row["status_key"] == code
            assert drv_row["notes"] == "E05 note"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E06 — Historical Status pseudo-lines are ignored by current reads
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e06_historical_status_pseudo_line_is_ignored_by_current_reads(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E06: A historical pseudo-line cannot supply Status or note to current reads."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e06")
        code = _sk_code("E06KEY", start)
        sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            # Insert DraftLine directly — bypasses canonical write
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverassignmentid,
                         workdate, linetype, quantity,
                         sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                    VALUES (1, :bid, :pid, :did, :dt,
                            'DailyStatus', 0,
                            'Manual', 'Active', FALSE, :code, 1)
                """),
                {"bid": ces_branch_id, "pid": pid, "did": ces_driver_assignment_id, "dt": start, "code": code},
            )
            await direct_db.commit()

            # No canonical rows should exist
            rows = await _canonical_rows(direct_db, pid)
            assert not rows, "Test setup error: canonical rows should not exist"

            # Current reads use canonical entry state only.
            r = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r.status_code == 200
            drv_row = next(
                (d for d in r.json()["rows"] if d["driver_assignment_id"] == ces_driver_assignment_id), None
            )
            assert drv_row is not None
            assert drv_row["status_key"] is None
            assert drv_row["notes"] is None
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E07 — Per-driver canonical-first: only drivers WITH canonical row use it
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e07_per_driver_canonical_precedence(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
        ces_driver_assignment_id_b: int,
    ):
        """EntryState supplies Driver A; a historical pseudo-line supplies no Status for B."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e07")
        code_a = _sk_code("E07AKEY", start)
        code_b = _sk_code("E07BKEY", start)
        sk_a = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code_a)
        sk_b = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code_b)
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            # Driver A: write via save_day_grid (creates canonical row)
            await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {}, "status_key": code_a}]},
            )

            # Driver B: inject unsupported historical data; current reads ignore it.
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrolldraftlines
                        (companyid, branchid, payrollperiodid, driverassignmentid,
                         workdate, linetype, quantity,
                         sourcetype, status, needsmanagerreview, notes, addedbyuserid)
                    VALUES (1, :bid, :pid, :did, :dt,
                            'DailyStatus', 0, 'Manual', 'Active', FALSE, :code, 1)
                """),
                {"bid": ces_branch_id, "pid": pid, "did": ces_driver_assignment_id_b, "dt": start, "code": code_b},
            )
            await direct_db.commit()

            # get_day_grid: Driver A from EntryState, Driver B has no Status.
            r = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r.status_code == 200
            drv_rows = {d["driver_assignment_id"]: d for d in r.json()["rows"]}

            assert drv_rows[ces_driver_assignment_id]["status_key"] == code_a, "A: canonical row"
            assert drv_rows[ces_driver_assignment_id_b]["status_key"] is None, "Pseudo-lines are not Status authority"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            for sid in (sk_a, sk_b):
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sid},
                )
            await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E08 — Deactivated key: bypass active-only check on unchanged re-submit
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e08_deactivated_key_bypass_unchanged(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E08: Re-submitting the same code after the key is deactivated → 200."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e08")
        code = _sk_code("E08DEACT", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)

            # Set the status while key is active
            r1 = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {}, "status_key": code}]},
            )
            assert r1.status_code == 200, f"initial save failed: {r1.text}"

            # Deactivate the key
            await direct_db.execute(
                _text("UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = :sid"),
                {"sid": sk_id},
            )
            await direct_db.commit()

            # Re-submit the same code (unchanged) → must succeed (bypass active-only check)
            r2 = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {}, "status_key": code}]},
            )
            assert r2.status_code == 200, f"Re-submit of deactivated unchanged key failed: {r2.text}"

            # Submitting a DIFFERENT code while key is inactive → 422
            r3 = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {},
                                 "status_key": code + "_NEW"}]},
            )
            assert r3.status_code == 422, "A new deactivated code must be rejected"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E09 — Solution B: Submit captures immutable Status evidence; the old
    # finalize-time EntryState freeze is NOT the historical authority.
    # ------------------------------------------------------------------ #
    #
    # Superseded contract (this test used to encode): finalize_period filled
    # EntryState's own FinalizedAtUtc/StatusCodeSnapshot/StatusLabelSnapshot/
    # StatusIsOffReasonSnapshot columns via _finalize_canonicalize_entry_state.
    # That is no longer true of the live finalize path: finalize_period (the
    # function POST /periods/{id}/finalize actually calls) projects
    # PayrollFinalLines from _load_approved_snapshot_packet and never calls
    # _finalize_canonicalize_entry_state at all. That helper is called only
    # from _legacy_finalize_period, which has zero callers anywhere in
    # app/ or tests/ -- it is dead code, not a second live path. So under
    # Solution B, EntryState's freeze columns are never written by any
    # reachable flow; immutable PayrollCalculationSnapshotStatusEntries
    # (captured at Submit/Resubmit) is the historical Status authority, and
    # Locked/Archived get_day_grid (Stage B3 Unit 8C-3) reads it directly.

    # ------------------------------------------------------------------ #
    # E10 — Solution B: Locked period Day Grid reads immutable Status
    # evidence, not the EntryState freeze columns and not mutable
    # PayrollStatusKeys.
    # ------------------------------------------------------------------ #
    #
    # Superseded contract (pre-Stage-B3): get_day_grid used to read Locked
    # periods' Status straight off PayrollPeriodDriverDayEntryState's own
    # freeze columns (StatusCodeSnapshot/StatusLabelSnapshot/
    # StatusIsOffReasonSnapshot, filled by _finalize_canonicalize_entry_state
    # at finalize time). Stage B3 Unit 8C-3 moves Locked/Archived Day Grid
    # reads onto the immutable calculation-snapshot Status evidence captured
    # at Submit instead -- the same historical authority CP-5C/P6A already
    # use for reports and the finalized library. This test proves that by
    # drifting the CURRENT StatusKey's label/off-reason AFTER Locking: the
    # Locked Day Grid must keep showing the ORIGINAL captured values.

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_e28_locked_period_missing_snapshot_provenance_is_unavailable(
        self,
        direct_db,
        session_client,
        auth_token: str,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E28: a Locked period whose FinalLines name more than one snapshot
        must report status_evidence as UNAVAILABLE/PROVENANCE_UNAVAILABLE
        -- never silently falling back to another snapshot or to mutable
        current state."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e28")
        headers = _auth(auth_token)
        try:
            await direct_db.execute(
                _text("UPDATE payroll.payrollperiods SET status = 'Locked' WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )
            # Two snapshots projected into one period: the FinalLines do not name exactly one
            # snapshot, so the evidence is unavailable rather than taken from either of them.
            for revision in (1, 2):
                await seed_finalized_evidence(
                    direct_db, period_id=pid, driver_assignment_id=ces_driver_assignment_id,
                    revision=revision)
            await direct_db.commit()

            r = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": str(start)},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("status_evidence") == {
                "state": "UNAVAILABLE", "reason_code": "PROVENANCE_UNAVAILABLE",
            }
            drv_row = next(
                (d for d in body["rows"] if d["driver_assignment_id"] == ces_driver_assignment_id), None
            )
            assert drv_row is not None
            assert drv_row["status_key"] is None
            assert drv_row["status_label"] is None
            assert drv_row["is_off"] is False
            # Unknowable historical Status must not be tallied as "worked".
            assert body["summary"]["worked"] == 0
            assert body["summary"]["off"] == 0
        finally:
            await _clean_branch(direct_db, ces_branch_id)

    @pytest.mark.asyncio
    async def test_e29_open_period_day_grid_unaffected_by_status_evidence(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E29: Open periods are untouched by Stage B3 Unit 8C-3 -- Day Grid
        keeps reading the live StatusKey name (a rename DOES show immediately,
        unlike Locked/Archived), and status_evidence stays absent (None). The
        off-day semantic of a used Status can no longer drift, so it stays."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e29")
        code = _sk_code("E29OPEN", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(
                direct_db, _COMPANY_ID, ces_branch_id, code, is_off_reason=True
            )

            save = await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {}, "status_key": code}]},
            )
            assert save.status_code == 200, save.text

            await direct_db.execute(
                _text("""
                    UPDATE payroll.payrollstatuskeys
                    SET keyname = 'DRIFTED LIVE LABEL', normalizedkeyname = 'drifted live label'
                    WHERE statuskeyid = :sid
                """),
                {"sid": sk_id},
            )
            await direct_db.commit()

            r = await session_client.get(
                f"/payroll/periods/{pid}/day-grid",
                params={"work_date": wdate},
                headers=headers,
            )
            assert r.status_code == 200
            body = r.json()
            drv_row = next(
                (d for d in body["rows"] if d["driver_assignment_id"] == ces_driver_assignment_id), None
            )
            assert drv_row is not None
            assert drv_row["status_key"] == code
            assert drv_row["status_label"] == "DRIFTED LIVE LABEL", (
                "Open period must reflect the CURRENT live StatusKey, not a frozen value"
            )
            assert drv_row["is_off"] is True, "the off-day semantic of a used Status never drifts"
            assert body.get("status_evidence") is None
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # Historical pseudo-lines cannot be introduced through current source APIs.
    # Submit/Resubmit and immutable evidence consume canonical EntryState directly.

    # ------------------------------------------------------------------ #
    # E12 — Direct DraftLine API: add_draft_line writes canonical row
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # E13 — P6D immutable-history contract: physical delete of a period
    # that has acquired audit evidence is rejected, not cascaded.
    #
    # Originally (pre-P6D) this contract asserted the opposite: a raw
    # DELETE of PayrollPeriods cascaded away its canonical entry-state
    # rows via the FK's ON DELETE CASCADE. Migration 0065 (P6D) added
    # trg_PayrollPeriods_AuditEvidenceDelete, a BEFORE DELETE guard that
    # raises whenever the period has rows in
    # PayrollPeriodAuditEvidenceCoverage/Events or PayrollCalculationSnapshots
    # -- and the Day Grid save below (the supported workflow for writing a
    # canonical Status row) always leaves a STATUS_NOTE coverage row behind
    # via capture_period_audit_evidence's _ensure_partial_coverage. So the
    # old cascade is no longer reachable for a period with real data; this
    # test now proves the guard rejects the delete and that both the
    # canonical row and the audit-evidence coverage it produced survive the
    # rejected attempt untouched.
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e13_delete_rejected_for_evidence_bearing_period(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E13: A period that has acquired P6D audit evidence through the
        supported Day Grid workflow rejects physical DELETE, and the
        canonical entry-state row plus the audit-evidence coverage row it
        produced remain intact after the rejected attempt."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e13")
        code = _sk_code("E13KEY", start)
        sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            await session_client.post(
                f"/payroll/periods/{pid}/day-grid",
                headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {}, "status_key": code}]},
            )
            assert await _canonical_rows(direct_db, pid), "Canonical row must exist before delete"

            coverage_before = (await direct_db.execute(
                _text("""
                    SELECT evidencedomain, coveragestate
                    FROM   payroll.payrollperiodauditevidencecoverage
                    WHERE  payrollperiodid = :pid
                """),
                {"pid": pid},
            )).mappings().all()
            assert coverage_before, (
                "Day Grid save must produce P6D audit-evidence coverage for "
                "the period -- that is the evidence this test proves is protected."
            )

            # payrolldraftlines has no delete guard of its own; only the
            # PayrollPeriods row itself is protected once evidence exists.
            await direct_db.execute(
                _text("DELETE FROM payroll.payrolldraftlines WHERE payrollperiodid = :pid"),
                {"pid": pid},
            )
            with pytest.raises(IntegrityError, match="payroll_period_delete"):
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
                    {"pid": pid},
                )
        finally:
            await direct_db.rollback()

        period_row = (await direct_db.execute(
            _text("SELECT payrollperiodid FROM payroll.payrollperiods WHERE payrollperiodid = :pid"),
            {"pid": pid},
        )).mappings().first()
        assert period_row is not None, "Rejected DELETE must leave the period row in place"

        rows_after = await _canonical_rows(direct_db, pid)
        assert rows_after, "Canonical row must survive the rejected DELETE"

        coverage_after = (await direct_db.execute(
            _text("""
                SELECT evidencedomain, coveragestate
                FROM   payroll.payrollperiodauditevidencecoverage
                WHERE  payrollperiodid = :pid
            """),
            {"pid": pid},
        )).mappings().all()
        def as_pairs(rows):
            return sorted((r["evidencedomain"], r["coveragestate"]) for r in rows)

        assert as_pairs(coverage_after) == as_pairs(coverage_before), (
            "Protected audit-evidence coverage must be unchanged by the rejected DELETE"
        )

        await _clean_branch(direct_db, ces_branch_id)
        await direct_db.execute(
            _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
            {"sid": sk_id},
        )
        await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E14 — Tenant isolation: CompanyID scoping
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e14_canonical_row_company_id_scoped(
        self,
        direct_db,
        ces_branch_id: int,
    ):
        """E14: Canonical rows carry the period's CompanyID — cross-company rows absent."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e14")
        try:
            await direct_db.execute(
                _text("""
                    INSERT INTO payroll.payrollperioddriverdayentrystate
                        (companyid, branchid, payrollperiodid, workdate, driverassignmentid,
                         isvoided, createdbyuserid, updatedbyuserid, createdatutc, updatedatutc)
                    VALUES (1, :bid, :pid, :dt, 999992, FALSE, 1, 1, NOW(), NOW())
                """),
                {"bid": ces_branch_id, "pid": pid, "dt": start},
            )
            await direct_db.commit()

            # Row is scoped to company 1
            rows = (await direct_db.execute(
                _text("SELECT companyid FROM payroll.payrollperioddriverdayentrystate "
                      "WHERE payrollperiodid = :pid AND driverassignmentid = 999992"),
                {"pid": pid},
            )).mappings().all()
            assert all(r["companyid"] == 1 for r in rows)

            # Company 2 sees zero rows for this period
            rows_c2 = (await direct_db.execute(
                _text("SELECT 1 FROM payroll.payrollperioddriverdayentrystate "
                      "WHERE payrollperiodid = :pid AND companyid = 2"),
                {"pid": pid},
            )).all()
            assert not rows_c2, "Cross-company canonical rows must not exist"
        finally:
            await _clean_branch(direct_db, ces_branch_id)

    # ------------------------------------------------------------------ #
    # E16 — Direct add_draft_line: invalid status code → 422, no mutations
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e16_direct_add_dailystatus_invalid_code_rejected(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E16: add_draft_line with DailyStatus and a non-existent code → 422.
        No DraftLine and no canonical row must be created."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e16")
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
                json={
                    "driver_assignment_id": ces_driver_assignment_id,
                    "work_date": wdate,
                    "line_type": "DailyStatus",
                    "quantity": "0",
                    "notes": "NONEXISTENT_CODE_E16",
                },
            )
            assert r.status_code == 422, f"Expected 422 for invalid status code; got {r.status_code}"

            # No DraftLine should have been inserted
            dl_count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverassignmentid = :did AND linetype = 'DailyStatus'
                """),
                {"pid": pid, "did": ces_driver_assignment_id},
            )).scalar()
            assert dl_count == 0, "No DraftLine must be inserted on validation failure"

            # No canonical row should exist
            assert not await _canonical_rows(direct_db, pid), \
                "No canonical row must be created on validation failure"
        finally:
            await _clean_branch(direct_db, ces_branch_id)

    # ------------------------------------------------------------------ #
    # E17 — Direct add_draft_line: inactive status code → 422, no mutations
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e17_direct_add_dailystatus_inactive_code_rejected(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E17: add_draft_line with DailyStatus and an inactive code → 422.
        No DraftLine and no canonical row must be created."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e17")
        code = _sk_code("E17INACT", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)
            # Deactivate immediately
            await direct_db.execute(
                _text("UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = :sid"),
                {"sid": sk_id},
            )
            await direct_db.commit()

            r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
                json={
                    "driver_assignment_id": ces_driver_assignment_id,
                    "work_date": wdate,
                    "line_type": "DailyStatus",
                    "quantity": "0",
                    "notes": code,
                },
            )
            assert r.status_code == 422, f"Expected 422 for inactive status code; got {r.status_code}"

            dl_count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverassignmentid = :did AND linetype = 'DailyStatus'
                """),
                {"pid": pid, "did": ces_driver_assignment_id},
            )).scalar()
            assert dl_count == 0, "No DraftLine must be inserted for inactive code"
            assert not await _canonical_rows(direct_db, pid), \
                "No canonical row must be created for inactive code"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E18 — Direct add_draft_line: blank notes for DailyStatus → 422
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e18_direct_add_dailystatus_blank_notes_rejected(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E18: add_draft_line with DailyStatus and missing/blank notes → 422."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e18")
        wdate = str(start)
        headers = _auth(auth_token)
        try:
            r = await session_client.post(
                f"/payroll/periods/{pid}/lines",
                headers=headers,
                json={
                    "driver_assignment_id": ces_driver_assignment_id,
                    "work_date": wdate,
                    "line_type": "DailyStatus",
                    "quantity": "0",
                },
            )
            assert r.status_code == 422, f"Expected 422 for blank notes; got {r.status_code}"

            dl_count = (await direct_db.execute(
                _text("""
                    SELECT COUNT(*) FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverassignmentid = :did AND linetype = 'DailyStatus'
                """),
                {"pid": pid, "did": ces_driver_assignment_id},
            )).scalar()
            assert dl_count == 0, "No DraftLine must be inserted for blank notes"
        finally:
            await _clean_branch(direct_db, ces_branch_id)

    # ------------------------------------------------------------------ #
    # E19 — Direct update_draft_line: invalid code → 422, state unchanged
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e19_generic_update_dailystatus_is_retired(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E19 (G0.3): the generic DraftLine PATCH no longer mutates DailyStatus.
        Existing DraftLine and canonical row must remain unchanged."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e19")
        code = _sk_code("E19VALID", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)

            # Add a valid DailyStatus line first
            add_r = await session_client.post(
                f"/payroll/periods/{pid}/day-grid", headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {},
                                 "status_key": code}]},
            )
            assert add_r.status_code == 200, f"day-grid seed failed: {add_r.text}"
            await direct_db.execute(_text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverassignmentid, workdate,
                     linetype, quantity, sourcetype, status, needsmanagerreview,
                     notes, addedbyuserid)
                VALUES (1, :bid, :pid, :did, :dt, 'DailyStatus', 0,
                        'Manual', 'Active', FALSE, :code, 1)
            """), {"bid": ces_branch_id, "pid": pid, "did": ces_driver_assignment_id,
                   "dt": start, "code": code})
            await direct_db.commit()
            line_id = (await direct_db.execute(
                _text("""
                    SELECT draftlineid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverassignmentid = :did
                      AND linetype = 'DailyStatus' AND status != 'Void'
                """),
                {"pid": pid, "did": ces_driver_assignment_id},
            )).scalar_one()

            # Canonical row must exist with the correct statuskeyid
            rows_before = await _canonical_rows(direct_db, pid)
            assert rows_before, "Canonical row must exist after valid add"
            canonical_sk_id_before = rows_before[0]["statuskeyid"]
            assert canonical_sk_id_before == sk_id

            # Attempt update to invalid code
            upd_r = await session_client.patch(
                f"/payroll/periods/{pid}/lines/{line_id}",
                headers=headers,
                json={"notes": "NONEXISTENT_E19"},
            )
            assert upd_r.status_code == 404, f"Pseudo-line was exposed to current mutation: {upd_r.text}"

            # DraftLine notes must still be the original code
            dl = (await direct_db.execute(
                _text("SELECT notes FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
                {"lid": line_id},
            )).mappings().first()
            assert dl["notes"] == code, "DraftLine notes must not change on failed update"

            # Canonical row must still hold the original statuskeyid
            rows_after = await _canonical_rows(direct_db, pid)
            assert rows_after[0]["statuskeyid"] == sk_id, \
                "Canonical statuskeyid must not change on failed update"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # E20 — Direct update_draft_line: inactive code → 422 (no bypass on direct path)
    # E20b — Same code after deactivation (unchanged resave) → also 422
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_e20_generic_update_dailystatus_retired_even_when_unchanged(
        self,
        session_client,
        auth_token: str,
        direct_db,
        ces_branch_id: int,
        ces_driver_assignment_id: int,
    ):
        """E20 (G0.3): the generic DraftLine PATCH rejects a DailyStatus line
        outright, including an unchanged resave of a deactivated key. The
        deactivated-key bypass lives only in save_day_grid."""
        start, end = _week_2096()
        pid = await _open_period(direct_db, ces_branch_id, start, end, "-e20")
        code = _sk_code("E20DEACT", start)
        wdate = str(start)
        headers = _auth(auth_token)
        sk_id = None
        try:
            sk_id = await _insert_status_key(direct_db, _COMPANY_ID, ces_branch_id, code)

            # Add a valid DailyStatus line while key is active
            add_r = await session_client.post(
                f"/payroll/periods/{pid}/day-grid", headers=headers,
                json={"work_date": wdate,
                      "rows": [{"driver_assignment_id": ces_driver_assignment_id, "values": {},
                                 "status_key": code}]},
            )
            assert add_r.status_code == 200, f"day-grid seed failed: {add_r.text}"
            await direct_db.execute(_text("""
                INSERT INTO payroll.payrolldraftlines
                    (companyid, branchid, payrollperiodid, driverassignmentid, workdate,
                     linetype, quantity, sourcetype, status, needsmanagerreview,
                     notes, addedbyuserid)
                VALUES (1, :bid, :pid, :did, :dt, 'DailyStatus', 0,
                        'Manual', 'Active', FALSE, :code, 1)
            """), {"bid": ces_branch_id, "pid": pid, "did": ces_driver_assignment_id,
                   "dt": start, "code": code})
            await direct_db.commit()
            line_id = (await direct_db.execute(
                _text("""
                    SELECT draftlineid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :pid AND driverassignmentid = :did
                      AND linetype = 'DailyStatus' AND status != 'Void'
                """),
                {"pid": pid, "did": ces_driver_assignment_id},
            )).scalar_one()

            # Deactivate the key
            await direct_db.execute(
                _text("UPDATE payroll.payrollstatuskeys SET isactive = FALSE WHERE statuskeyid = :sid"),
                {"sid": sk_id},
            )
            await direct_db.commit()

            # Update to a DIFFERENT inactive code → 422
            upd_r1 = await session_client.patch(
                f"/payroll/periods/{pid}/lines/{line_id}",
                headers=headers,
                json={"notes": code},  # same code but now inactive
            )
            assert upd_r1.status_code == 404, \
                "Historical pseudo-line must not be exposed through current source mutation"

            # DraftLine unchanged
            dl = (await direct_db.execute(
                _text("SELECT notes FROM payroll.payrolldraftlines WHERE draftlineid = :lid"),
                {"lid": line_id},
            )).mappings().first()
            assert dl["notes"] == code, "DraftLine must not change on failed update"
        finally:
            await _clean_branch(direct_db, ces_branch_id)
            if sk_id is not None:
                await direct_db.execute(
                    _text("DELETE FROM payroll.payrollstatuskeys WHERE statuskeyid = :sid"),
                    {"sid": sk_id},
                )
                await direct_db.commit()

    # ------------------------------------------------------------------ #
    # DailyNote pseudo-line compatibility coverage was retired with the generic
    # source API seam. Day Grid note behavior is covered through EntryState.
