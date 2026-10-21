"""
Payroll Driver eligibility validation, frozen period snapshots, and eligible-driver reads.

Every operational period requires its tenant-scoped eligibility marker. A missing marker
is a structural error and never falls back to a live roster.
"""
from dataclasses import dataclass
from datetime import UTC, date, datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_any_permission
from app.access.policy import require_non_driver_subject
from app.db.sql import build_in_clause
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import SOURCE_ENTRY_STATUSES
from app.payroll.workflow_lock import acquire_branch_workflow_lock

# ---------------------------------------------------------------------------
# Driver eligibility helpers (shared by all payroll write/finalization paths)
# ---------------------------------------------------------------------------

async def _lifecycle_eligible_assignment_ids(
    period_id: int,
    company_id: int,
    branch_id: int,
    work_date: date | None,
    driver_assignment_ids: list[int],
    db: AsyncConnection,
) -> set[int]:
    """Return DriverAssignments whose live window and Driver state still allow new source.

    An assignment is lifecycle-eligible when it has ended (it then carries an
    EndReason and its window decides) or when its permanent Driver is Active.
    """
    if not driver_assignment_ids:
        return set()
    in_clause, in_params = build_in_clause(driver_assignment_ids, "lifecycle_assignment")
    result = await db.execute(
        text(f"""
            WITH eligible_days AS MATERIALIZED (
                SELECT pde.DriverAssignmentID, pde.BranchTimeZoneIdSnapshot,
                       unnest(CASE WHEN CAST(:work_date AS DATE) IS NULL
                                   THEN pde.EligibleWorkDates
                                   ELSE ARRAY[CAST(:work_date AS DATE)] END) AS WorkDate
                FROM payroll.PayrollPeriodDriverEligibility pde
                WHERE pde.PayrollPeriodID = :period_id
                  AND pde.CompanyID = :company_id AND pde.BranchID = :branch_id
                  AND (CAST(:work_date AS DATE) IS NULL
                       OR CAST(:work_date AS DATE) = ANY(pde.EligibleWorkDates))
            ), distinct_zone_days AS MATERIALIZED (
                SELECT DISTINCT ed.BranchTimeZoneIdSnapshot, ed.WorkDate
                FROM eligible_days ed
            ), work_windows AS MATERIALIZED (
                SELECT z.BranchTimeZoneIdSnapshot, z.WorkDate,
                       core.fn_WorkDateRange(
                           z.BranchTimeZoneIdSnapshot, z.WorkDate, z.WorkDate
                       ) AS WorkWindow
                FROM distinct_zone_days z
            )
            SELECT DISTINCT a.DriverAssignmentID
            FROM core.DriverAssignments a
            JOIN core.Drivers dr ON dr.DriverID = a.DriverID
            JOIN core.Employees e ON e.EmployeeID = a.EmployeeID
            JOIN payroll.PayrollPeriodDriverEligibility pde
              ON pde.PayrollPeriodID = :period_id
             AND pde.CompanyID = a.CompanyID AND pde.BranchID = a.BranchID
             AND pde.DriverAssignmentID = a.DriverAssignmentID
            JOIN eligible_days ed ON ed.DriverAssignmentID = pde.DriverAssignmentID
            JOIN work_windows w
              ON w.BranchTimeZoneIdSnapshot = ed.BranchTimeZoneIdSnapshot
             AND w.WorkDate = ed.WorkDate
            WHERE a.CompanyID = :company_id
              AND a.BranchID = :branch_id
              AND a.DriverAssignmentID IN ({in_clause})
              AND core.fn_DriverEffectiveRange(a.EffectiveFromAt, a.EffectiveToAt) && w.WorkWindow
              AND (a.EndReason IS NOT NULL OR dr.DriverStatus = 'Active')
              AND (e.EmploymentStatus = 'Active'
                   OR (e.EmploymentStatus = 'Terminated'
                       AND e.TerminationDate IS NOT NULL
                       AND e.TerminationDate >= ed.WorkDate))
              AND (e.HireDate IS NULL OR e.HireDate <= ed.WorkDate)
              AND (e.TerminationDate IS NULL OR e.TerminationDate >= ed.WorkDate)
        """),
        {
            "period_id": period_id,
            "company_id": company_id,
            "branch_id": branch_id,
            "work_date": work_date,
            **in_params,
        },
    )
    return {row["driverassignmentid"] for row in result.mappings().all()}


# ── CP-2E: Canonical Eligibility Snapshot helpers ────────────────────────────

async def require_period_eligibility_snapshot(
    period_id: int, company_id: int, branch_id: int, db: AsyncConnection,
) -> None:
    """Fail closed when a persisted period has no tenant-scoped roster marker."""
    row = (await db.execute(text("""
        SELECT 1 FROM payroll.PayrollPeriodEligibilitySnapshots
        WHERE PayrollPeriodID = :period_id AND CompanyID = :company_id AND BranchID = :branch_id
    """), {
        "period_id": period_id, "company_id": company_id, "branch_id": branch_id,
    })).first()
    if row is None:
        raise HTTPException(status_code=409, detail={
            "code": "ELIGIBILITY_SNAPSHOT_MISSING",
            "message": "Payroll period eligibility snapshot is missing; rebuild the invalid period.",
        })


async def _get_driver_eligibility_row(
    period_id: int, driver_assignment_id: int, company_id: int, branch_id: int,
    db: AsyncConnection,
):
    return (await db.execute(
        text("""
            SELECT payrollperioddrivereligibilityid,
                   eligibilityreasoncode,
                   hiredatesnapshot,
                   terminationdatesnapshot,
                   drivereffectivefromatsnapshot,
                   drivereffectivetoatsnapshot,
                   branchtimezoneidsnapshot,
                   eligibleworkdates,
                   frozenatutc,
                   drivernamesnapshot,
                   drivernumbersnapshot,
                   employeekeysnapshot,
                   iseligibleforperiod
            FROM   payroll.payrollperioddrivereligibility
            WHERE  payrollperiodid    = :pid
              AND  driverassignmentid = :aid
              AND  companyid          = :cid
              AND  branchid           = :bid
        """),
        {"pid": period_id, "aid": driver_assignment_id, "cid": company_id, "bid": branch_id},
    )).first()


def _is_snapshot_row_eligible_for_workdate(row, work_date: date) -> bool:
    """Derive date-level eligibility from a snapshot row.

    Returns True only if work_date is in the captured Branch-local date list.
    IncludedByExistingData always returns False here — such assignments require an
    existing-source check (DB query) and are never eligible purely by window.
    Generated rows retain their exact eligible WorkDates as frozen evidence.
    """
    if row is None or not row.iseligibleforperiod:
        return False
    reason = row.eligibilityreasoncode

    # IBED is never window-eligible; always requires existing-source rescue
    if reason == "IncludedByExistingData":
        return False

    return work_date in (row.eligibleworkdates or [])


async def _driver_has_existing_daily_source_on_date(
    period_id: int, driver_assignment_id: int, work_date: date, db: AsyncConnection
) -> bool:
    r = (await db.execute(
        text("""
            SELECT 1 FROM payroll.payrolldraftlines
            WHERE payrollperiodid = :pid AND driverassignmentid = :aid
              AND workdate = :dt AND status != 'Void'
            LIMIT 1
        """),
        {"pid": period_id, "aid": driver_assignment_id, "dt": work_date},
    )).first()
    if r:
        return True
    r2 = (await db.execute(
        text("""
            SELECT 1 FROM payroll.payrollperioddriverdayentrystate
            WHERE payrollperiodid = :pid AND driverassignmentid = :aid
              AND workdate = :dt AND isvoided = FALSE
            LIMIT 1
        """),
        {"pid": period_id, "aid": driver_assignment_id, "dt": work_date},
    )).first()
    return r2 is not None


async def _assert_driver_eligible_for_workdate_via_snapshot(
    company_id: int, branch_id: int, period_id: int,
    driver_assignment_id: int, work_date: date, db: AsyncConnection,
    allow_existing_source_rescue: bool = True,
) -> None:
    """Check a WorkDate against the mandatory captured roster and live lifecycle.

    Primary gate: exact captured WorkDate membership plus current assignment overlap.
    Secondary gate (rescue): if allow_existing_source_rescue=True, an assignment
    whose date window does not cover work_date is still allowed if it has
    existing daily source (DraftLine or EntryState) on that exact date.
    This rescue applies to ALL reason codes, not just IncludedByExistingData —
    a generated-row assignment (Active/TerminatedHistorical/Transferred) may have
    data outside its eligibility window from before the snapshot was frozen.

    Callers:
    - save_day_grid:         allow_existing_source_rescue=True  (default)
    - update_draft_line:     allow_existing_source_rescue=True  (default)
    - add_draft_line:        allow_existing_source_rescue=False (new source — no rescue)
    """
    await require_period_eligibility_snapshot(period_id, company_id, branch_id, db)
    row = await _get_driver_eligibility_row(
        period_id, driver_assignment_id, company_id, branch_id, db,
    )
    if row is None:
        raise HTTPException(
            status_code=422,
            detail="Driver has no eligibility snapshot for this period.",
        )
    lifecycle_eligible = driver_assignment_id in await _lifecycle_eligible_assignment_ids(
        period_id, company_id, branch_id, work_date, [driver_assignment_id], db,
    )
    # Primary: date-window check
    if _is_snapshot_row_eligible_for_workdate(row, work_date) and lifecycle_eligible:
        return
    # Secondary rescue: existing source on exact date (any reason code)
    if allow_existing_source_rescue:
        has_existing = await _driver_has_existing_daily_source_on_date(
            period_id, driver_assignment_id, work_date, db
        )
        if has_existing:
            return
    # Build a useful error message based on reason code
    if row.eligibilityreasoncode == "IncludedByExistingData":
        raise HTTPException(
            status_code=422,
            detail=(
                "Driver is only eligible for existing saved dates. "
                "New entries on new work dates are not permitted."
            ),
        )
    raise HTTPException(
        status_code=422,
        detail="Driver is not eligible for this work date.",
    )


async def _assert_driver_eligible_for_period_via_snapshot(
    company_id: int, branch_id: int, period_id: int,
    driver_assignment_id: int, db: AsyncConnection,
) -> None:
    """Check period-level Bonus eligibility against capture and live assignment.

    IncludedByExistingData assignments are in the roster only because they have
    existing saved daily source; that history never grants authority to create
    new period-level Bonus money, so they are rejected here.
    """
    await require_period_eligibility_snapshot(period_id, company_id, branch_id, db)
    row = await _get_driver_eligibility_row(
        period_id, driver_assignment_id, company_id, branch_id, db,
    )
    if row is None:
        raise HTTPException(
            status_code=422,
            detail="Driver has no eligibility snapshot for this period.",
        )
    if not row.iseligibleforperiod:
        raise HTTPException(status_code=422, detail="Driver is not eligible for this period.")
    if row.eligibilityreasoncode == "IncludedByExistingData":
        raise HTTPException(
            status_code=422,
            detail="Driver is not eligible for new Bonus entries in this period.",
        )
    # A frozen roster row does not grant authority after its live assignment window
    # has stopped overlapping every captured Branch-local WorkDate.
    eligible = await _lifecycle_eligible_assignment_ids(
        period_id, company_id, branch_id, None, [driver_assignment_id], db
    )
    if driver_assignment_id not in eligible:
        raise HTTPException(status_code=422, detail="Driver is not eligible for new Bonus entries in this period.")


async def _create_period_driver_eligibility_rows(
    period_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
    snapshot_source: str = "Generated",
    freeze: bool = False,
    created_by_user_id: int | None = None,
    frozen_by_user_id: int | None = None,
    only_driver_assignment_id: int | None = None,
    time_zone_id: str | None = None,
) -> None:
    """Capture eligibility rows for a period.

    ``time_zone_id`` is used only by re-capture of an existing assignment row: the row keeps
    the zone it was captured under (captured timezone authority is immutable), and its dates
    are recomputed under that zone. Brand-new rows (period creation, append) pass None and
    use the Branch's current zone.
    """
    now = datetime.now(UTC)
    frozen_at = now if freeze else None

    # Paths 1–3: Active / TerminatedHistorical / Transferred. The generated
    # DATE array is frozen at capture and uses Branch-local range overlap. The
    # reason derives from the assignment's EndReason; the Driver status is snapshotted.
    await db.execute(text("""
        INSERT INTO payroll.payrollperioddrivereligibility
            (companyid, branchid, payrollperiodid, driverassignmentid, sourceemployeeid,
             drivernumbersnapshot, drivernamesnapshot, employeekeysnapshot,
             driverstatussnapshot, endreasonsnapshot,
             employmentstatussnapshot,
             transferredfromassignmentidsnapshot, transferredtoassignmentidsnapshot,
             hiredatesnapshot, terminationdatesnapshot,
             drivereffectivefromatsnapshot, drivereffectivetoatsnapshot,
             branchtimezoneidsnapshot, eligibleworkdates,
             iseligibleforperiod, eligibilityreasoncode, snapshotsource,
             createdatutc, createdbyuserid, updatedatutc, frozenatutc, frozenbyuserid)
        WITH period_context AS MATERIALIZED (
            SELECT pp.*, COALESCE(CAST(:zone_override AS VARCHAR), b.TimeZoneId) AS TimeZoneId
            FROM payroll.PayrollPeriods pp
            JOIN core.Branches b ON b.CompanyID = pp.CompanyID AND b.BranchID = pp.BranchID
            WHERE pp.PayrollPeriodID = :pid
              AND pp.CompanyID = :cid
              AND pp.BranchID = :bid
        ),
        work_days AS MATERIALIZED (
            SELECT p.PayrollPeriodID,
                   p.StartDate + n.day_index AS WorkDate,
                   core.fn_WorkDateRange(
                       p.TimeZoneId, p.StartDate + n.day_index, p.StartDate + n.day_index
                   ) AS WorkWindow
            FROM period_context p
            CROSS JOIN LATERAL generate_series(0, p.EndDate - p.StartDate) n(day_index)
        ),
        candidates AS (
        SELECT
            pp.companyid, pp.branchid, pp.payrollperiodid,
            a.driverassignmentid, e.employeeid,
            dr.drivernumber, e.fullname, e.employeekey, dr.driverstatus, a.endreason,
            e.employmentstatus,
            a.transferredfromassignmentid, a.transferredtoassignmentid,
            e.hiredate, e.terminationdate, a.effectivefromat, a.effectivetoat,
            pp.timezoneid,
            ARRAY(
                SELECT w.WorkDate
                FROM work_days w
                WHERE w.PayrollPeriodID = pp.PayrollPeriodID
                  AND (e.HireDate IS NULL OR e.HireDate <= w.WorkDate)
                  AND (e.TerminationDate IS NULL OR e.TerminationDate >= w.WorkDate)
                  AND core.fn_DriverEffectiveRange(a.EffectiveFromAt, a.EffectiveToAt) && w.WorkWindow
                ORDER BY w.WorkDate
            ) AS eligible_dates,
            pp.startdate, pp.enddate
        FROM period_context pp
        JOIN core.driverassignments a ON a.companyid = pp.companyid AND a.branchid = pp.branchid
        JOIN core.drivers dr ON dr.driverid = a.driverid
        JOIN core.employees e ON e.employeeid = a.employeeid
        WHERE pp.payrollperiodid = :pid
          AND (CAST(:assignment_id AS INTEGER) IS NULL
               OR a.driverassignmentid = CAST(:assignment_id AS INTEGER))
          AND (a.endreason IS NOT NULL OR dr.driverstatus = 'Active')
          AND (e.employmentstatus = 'Active'
               OR (e.employmentstatus = 'Terminated' AND e.terminationdate IS NOT NULL))
        )
        SELECT companyid, branchid, payrollperiodid,
               driverassignmentid, employeeid,
               drivernumber, fullname, employeekey, driverstatus, endreason, employmentstatus,
               transferredfromassignmentid, transferredtoassignmentid,
               hiredate, terminationdate, effectivefromat, effectivetoat,
               timezoneid, eligible_dates,
            TRUE,
            CASE endreason
                WHEN 'Terminated' THEN 'TerminatedHistorical'
                WHEN 'Transferred' THEN 'Transferred'
                ELSE 'Active'
            END,
            :src, CAST(:now AS TIMESTAMPTZ), CAST(:uid AS INTEGER), CAST(:now AS TIMESTAMPTZ), CAST(:frozen_at AS TIMESTAMPTZ), CAST(:fuid AS INTEGER)
        FROM candidates
        WHERE cardinality(eligible_dates) > 0
        ON CONFLICT (payrollperiodid, driverassignmentid) DO NOTHING
    """), {
        "pid": period_id, "cid": company_id, "bid": branch_id, "src": snapshot_source, "now": now,
        "uid": created_by_user_id, "frozen_at": frozen_at, "fuid": frozen_by_user_id,
        "assignment_id": only_driver_assignment_id, "zone_override": time_zone_id,
    })


    # Path 4: IncludedByExistingData
    await db.execute(text("""
        INSERT INTO payroll.payrollperioddrivereligibility
            (companyid, branchid, payrollperiodid, driverassignmentid, sourceemployeeid,
             drivernumbersnapshot, drivernamesnapshot, employeekeysnapshot,
             driverstatussnapshot, endreasonsnapshot,
             employmentstatussnapshot,
             transferredfromassignmentidsnapshot, transferredtoassignmentidsnapshot,
             hiredatesnapshot, terminationdatesnapshot,
             drivereffectivefromatsnapshot, drivereffectivetoatsnapshot,
             branchtimezoneidsnapshot, eligibleworkdates,
             iseligibleforperiod, eligibilityreasoncode, snapshotsource,
             createdatutc, createdbyuserid, updatedatutc, frozenatutc, frozenbyuserid)
        SELECT DISTINCT
            pp.companyid, pp.branchid, pp.payrollperiodid,
            a.driverassignmentid, e.employeeid,
            dr.drivernumber, e.fullname, e.employeekey, dr.driverstatus, a.endreason,
            e.employmentstatus,
            a.transferredfromassignmentid, a.transferredtoassignmentid,
            e.hiredate, e.terminationdate, a.effectivefromat, a.effectivetoat,
            COALESCE(CAST(:zone_override AS VARCHAR), b.timezoneid), ARRAY[]::DATE[],
            TRUE, 'IncludedByExistingData', :src,
            CAST(:now AS TIMESTAMPTZ), CAST(:uid AS INTEGER), CAST(:now AS TIMESTAMPTZ), CAST(:frozen_at AS TIMESTAMPTZ), CAST(:fuid AS INTEGER)
        FROM payroll.payrollperiods pp
        JOIN (
            SELECT payrollperiodid, driverassignmentid, companyid
            FROM   payroll.payrolldraftlines
            WHERE  status != 'Void'
            UNION
            SELECT payrollperiodid, driverassignmentid, companyid
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  isvoided = FALSE
        ) src2 ON src2.payrollperiodid = pp.payrollperiodid
               AND src2.companyid = pp.companyid
        JOIN core.driverassignments a ON a.driverassignmentid = src2.driverassignmentid
                                     AND a.companyid = pp.companyid
                                     AND a.branchid  = pp.branchid
        JOIN core.drivers dr ON dr.driverid = a.driverid
        JOIN core.employees e ON e.employeeid = a.employeeid
        JOIN core.branches b ON b.companyid = pp.companyid AND b.branchid = pp.branchid
        WHERE pp.payrollperiodid = :pid
          AND pp.companyid = :cid
          AND pp.branchid = :bid
          AND (CAST(:assignment_id AS INTEGER) IS NULL
               OR a.driverassignmentid = CAST(:assignment_id AS INTEGER))
        ON CONFLICT (payrollperiodid, driverassignmentid) DO NOTHING
    """), {
        "pid": period_id, "cid": company_id, "bid": branch_id, "src": snapshot_source, "now": now,
        "uid": created_by_user_id, "frozen_at": frozen_at, "fuid": frozen_by_user_id,
        "assignment_id": only_driver_assignment_id, "zone_override": time_zone_id,
    })

    # Upsert marker row — ensure period is tracked as snapshotted even if zero drivers
    await db.execute(text("""
        INSERT INTO payroll.payrollperiodeligibilitysnapshots
            (payrollperiodid, companyid, branchid, snapshotsource,
             createdatutc, createdbyuserid, updatedatutc, frozenatutc, frozenbyuserid)
        SELECT
            pp.payrollperiodid, pp.companyid, pp.branchid, :src,
            CAST(:now AS TIMESTAMPTZ), CAST(:uid AS INTEGER),
            CAST(:now AS TIMESTAMPTZ),
            CAST(:frozen_at AS TIMESTAMPTZ), CAST(:fuid AS INTEGER)
        FROM payroll.payrollperiods pp
        WHERE pp.payrollperiodid = :pid
          AND pp.CompanyID = :cid
          AND pp.BranchID = :bid
        ON CONFLICT (payrollperiodid) DO NOTHING
    """), {
        "pid": period_id, "cid": company_id, "bid": branch_id, "src": snapshot_source, "now": now,
        "uid": created_by_user_id, "frozen_at": frozen_at, "fuid": frozen_by_user_id,
    })


async def append_driver_assignment_to_editable_periods(
    company_id: int,
    branch_id: int,
    driver_assignment_id: int,
    actor_user_id: int,
    db: AsyncConnection,
) -> None:
    """Append a newly created assignment to existing editable frozen rosters."""
    await acquire_branch_workflow_lock(company_id, branch_id, db)
    rows = await db.execute(text("""
        SELECT pp.PayrollPeriodID, pp.Status
        FROM payroll.PayrollPeriods pp
        JOIN payroll.PayrollPeriodEligibilitySnapshots marker
          ON marker.PayrollPeriodID = pp.PayrollPeriodID
        WHERE pp.CompanyID = :company_id AND pp.BranchID = :branch_id
          AND pp.Status = ANY(:statuses)
        ORDER BY pp.PayrollPeriodID
        FOR UPDATE OF pp
    """), {"company_id": company_id, "branch_id": branch_id,
           "statuses": list(SOURCE_ENTRY_STATUSES)})
    periods = rows.mappings().all()
    for period in periods:
        await _create_period_driver_eligibility_rows(
            period["payrollperiodid"], company_id, branch_id, db,
            snapshot_source="Generated", created_by_user_id=actor_user_id,
            freeze=period["status"] in {"Open", "Returned"},
            frozen_by_user_id=actor_user_id if period["status"] in {"Open", "Returned"} else None,
            only_driver_assignment_id=driver_assignment_id,
        )


# ---------------------------------------------------------------------------
# Boundary-change re-capture (transfer cancel and reschedule)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BoundaryWindow:
    """An instant interval of one Branch whose payroll rosters a boundary change touches."""

    branch_id: int
    start: datetime
    end: datetime | None = None


async def lock_boundary_periods(
    company_id: int,
    windows: list[BoundaryWindow],
    db: AsyncConnection,
    *,
    lock_only: bool = False,
) -> list[dict]:
    """Lock Branch workflow and PayrollPeriod rows touched by a boundary change.

    Lock order: Branch workflow advisory locks in ascending BranchID, then every
    overlapping PayrollPeriod row in ascending PayrollPeriodID. This is what makes a
    coverage-shrinking operation wait for an in-flight source writer (which holds the
    period row) and what makes a later writer re-check its eligibility against the
    changed window.

    Strict mode (cancel, reschedule): a period in review, approved, locked or archived
    state that overlaps a window makes the whole operation refuse with 409; nothing in
    such a period is ever rewritten, and only the Draft, Open and Returned periods are
    returned for re-capture. A Cancelled period never carries submissions or evidence
    (it is reachable only from Draft or Open), so it neither blocks nor is re-captured.

    Lock-only mode (transfer complete, termination): every overlapping period is locked
    and returned, and no state blocks the operation.

    Overlap is judged with the authority payroll itself uses. A captured roster row keeps
    the zone it was captured under (``BranchTimeZoneIdSnapshot``) even if the Branch zone
    is edited later, so a period overlaps a window when its WorkDate range does under ANY
    governing zone: each distinct snapshot zone of the period's eligibility rows, plus the
    Branch's current zone (rows appended or captured from now on use it). Judging by the
    current zone alone would miss a period a writer is still validly writing under its
    frozen zone.
    """
    for branch_id in sorted({window.branch_id for window in windows}):
        await acquire_branch_workflow_lock(company_id, branch_id, db)
    clauses: list[str] = []
    params: dict = {"company_id": company_id}
    for index, window in enumerate(windows):
        clauses.append(
            f"(pp.BranchID = :branch_{index} AND EXISTS ("
            f"SELECT 1 FROM ("
            f"SELECT br.TimeZoneId AS ZoneId "
            f"UNION "
            f"SELECT pde.BranchTimeZoneIdSnapshot FROM payroll.PayrollPeriodDriverEligibility pde "
            f"WHERE pde.PayrollPeriodID = pp.PayrollPeriodID "
            f"AND pde.BranchTimeZoneIdSnapshot IS NOT NULL"
            f") governing WHERE core.fn_WorkDateRange(governing.ZoneId, pp.StartDate, pp.EndDate) "
            f"&& tstzrange(:start_{index}, :end_{index}, '[)')))"
        )
        params[f"branch_{index}"] = window.branch_id
        params[f"start_{index}"] = window.start
        params[f"end_{index}"] = window.end
    rows = await db.execute(text(f"""
        SELECT pp.PayrollPeriodID, pp.BranchID, pp.Status
        FROM payroll.PayrollPeriods pp
        JOIN core.Branches br ON br.CompanyID = pp.CompanyID AND br.BranchID = pp.BranchID
        JOIN payroll.PayrollPeriodEligibilitySnapshots marker
          ON marker.PayrollPeriodID = pp.PayrollPeriodID
        WHERE pp.CompanyID = :company_id AND ({' OR '.join(clauses)})
        ORDER BY pp.PayrollPeriodID
        FOR UPDATE OF pp
    """), params)
    periods = [dict(row) for row in rows.mappings().all()]
    if lock_only:
        return periods
    periods = [p for p in periods if p["status"] != "Cancelled"]
    blocking = [p for p in periods if p["status"] not in SOURCE_ENTRY_STATUSES]
    if blocking:
        raise HTTPException(status_code=409, detail={
            "code": "DRIVER_ASSIGNMENT_PERIOD_NOT_EDITABLE",
            "message": "A payroll period in review, approved, locked or archived state overlaps "
                       "the affected interval; the Driver assignment boundary cannot change.",
            "payroll_period_ids": [p["payrollperiodid"] for p in blocking],
        })
    return periods


async def assignment_payroll_fact_exists(
    company_id: int,
    driver_assignment_id: int,
    db: AsyncConnection,
) -> bool:
    """Whether any payroll fact or evidence references the assignment."""
    result = await db.execute(text("""
        SELECT
            EXISTS (SELECT 1 FROM payroll.PayrollDraftLines
                    WHERE CompanyID = :company_id AND DriverAssignmentID = :aid)
         OR EXISTS (SELECT 1 FROM payroll.PayrollPeriodDriverDayEntryState
                    WHERE CompanyID = :company_id AND DriverAssignmentID = :aid)
         OR EXISTS (SELECT 1 FROM payroll.PayrollBonusEvents
                    WHERE CompanyID = :company_id AND DriverAssignmentID = :aid)
         OR EXISTS (SELECT 1 FROM payroll.PayrollFinalLines
                    WHERE CompanyID = :company_id AND DriverAssignmentID = :aid)
         OR EXISTS (SELECT 1 FROM payroll.PayrollCalculationDriverTotals
                    WHERE CompanyID = :company_id AND DriverAssignmentID = :aid)
         OR EXISTS (SELECT 1 FROM payroll.PayrollPeriodAuditEvidenceEvents
                    WHERE CompanyID = :company_id AND DriverAssignmentID = :aid)
         OR EXISTS (SELECT 1 FROM payroll.DriverRates
                    WHERE CompanyID = :company_id AND DriverAssignmentID = :aid)
    """), {"company_id": company_id, "aid": driver_assignment_id})
    return bool(result.scalar_one())


def frozen_day_overlap_sql(window_alias: str, zone_sql: str, day_sql: str) -> str:
    """The one predicate: the assignment window intersects a WorkDate's day in a zone.

    ``_lifecycle_eligible_assignment_ids`` applies the same test (window range against
    ``fn_WorkDateRange(frozen zone, day, day)``) to a row's captured days; the coverage
    check below builds on this so the two cannot drift (a parity test pins them).
    """
    return (
        f"core.fn_DriverEffectiveRange({window_alias}.EffectiveFromAt, {window_alias}.EffectiveToAt) "
        f"&& core.fn_WorkDateRange({zone_sql}, {day_sql}, {day_sql})"
    )


async def assert_assignment_payroll_covered(
    company_id: int,
    branch_id: int,
    driver_assignment_id: int,
    db: AsyncConnection,
) -> None:
    """Refuse with 409 when payroll source sits where the assignment no longer covers.

    Evaluated against the assignment's current window, so call it after the boundary
    UPDATE in the same transaction and under the period locks; the 409 rolls the change
    back. Coverage is judged in the zone that governs each fact's period: the zone
    captured on the assignment's eligibility row of that period (the Branch's current
    zone only when the period has no row for the assignment).

    * Daily facts (non-void draft lines and day-entry states, final lines) must sit on a
      WorkDate whose day still intersects the window.
    * Every Active Bonus event needs a captured eligible WorkDate of its period row that
      still intersects the window, which is what lifecycle eligibility requires of a new
      Bonus.
    """
    daily = await db.execute(text(f"""
        WITH facts AS (
            SELECT PayrollPeriodID, WorkDate FROM payroll.PayrollDraftLines
            WHERE CompanyID = :company_id AND DriverAssignmentID = :aid AND Status <> 'Void'
            UNION
            SELECT PayrollPeriodID, WorkDate FROM payroll.PayrollPeriodDriverDayEntryState
            WHERE CompanyID = :company_id AND DriverAssignmentID = :aid AND IsVoided = FALSE
            UNION
            SELECT PayrollPeriodID, WorkDate FROM payroll.vw_FinalizedWorkDates
            WHERE CompanyID = :company_id AND DriverAssignmentID = :aid
        )
        SELECT 1
        FROM facts f
        JOIN core.DriverAssignments a
          ON a.CompanyID = :company_id AND a.DriverAssignmentID = :aid
        JOIN core.Branches br ON br.CompanyID = a.CompanyID AND br.BranchID = a.BranchID
        LEFT JOIN payroll.PayrollPeriodDriverEligibility pde
          ON pde.PayrollPeriodID = f.PayrollPeriodID AND pde.DriverAssignmentID = a.DriverAssignmentID
        WHERE f.WorkDate IS NOT NULL
          AND NOT ({frozen_day_overlap_sql("a", "COALESCE(pde.BranchTimeZoneIdSnapshot, br.TimeZoneId)", "f.WorkDate")})
        LIMIT 1
    """), {"company_id": company_id, "aid": driver_assignment_id})
    if daily.first() is not None:
        raise HTTPException(status_code=409, detail={
            "code": "DRIVER_ASSIGNMENT_PAYROLL_FACTS_UNCOVERED",
            "message": "Payroll facts exist on WorkDates the changed Driver assignment window no longer covers.",
        })

    bonus = await db.execute(text(f"""
        SELECT 1
        FROM payroll.PayrollBonusEvents b
        JOIN core.DriverAssignments a
          ON a.CompanyID = b.CompanyID AND a.DriverAssignmentID = b.DriverAssignmentID
        WHERE b.CompanyID = :company_id AND b.DriverAssignmentID = :aid AND b.Status = 'Active'
          AND NOT EXISTS (
              SELECT 1
              FROM payroll.PayrollPeriodDriverEligibility pde
              CROSS JOIN LATERAL unnest(pde.EligibleWorkDates) AS d(WorkDate)
              WHERE pde.PayrollPeriodID = b.PayrollPeriodID
                AND pde.DriverAssignmentID = b.DriverAssignmentID
                AND {frozen_day_overlap_sql("a", "pde.BranchTimeZoneIdSnapshot", "d.WorkDate")}
          )
        LIMIT 1
    """), {"company_id": company_id, "aid": driver_assignment_id})
    if bonus.first() is not None:
        raise HTTPException(status_code=409, detail={
            "code": "DRIVER_ASSIGNMENT_PAYROLL_FACTS_UNCOVERED",
            "message": "An active Bonus would be left without any eligible WorkDate in the changed window.",
        })


async def assignment_eligibility_in_noneditable_period(
    company_id: int,
    driver_assignment_id: int,
    db: AsyncConnection,
) -> bool:
    """Whether the assignment has an eligibility row in a period past Draft/Open/Returned.

    A Cancelled period never blocks: it carries no submission or evidence and its roster
    rows are deleted together with the editable ones when the assignment is disposed of.
    """
    result = await db.execute(text("""
        SELECT EXISTS (
            SELECT 1
            FROM payroll.PayrollPeriodDriverEligibility pde
            JOIN payroll.PayrollPeriods pp ON pp.PayrollPeriodID = pde.PayrollPeriodID
            WHERE pde.CompanyID = :company_id AND pde.DriverAssignmentID = :aid
              AND pp.Status <> 'Cancelled'
              AND NOT (pp.Status = ANY(:statuses))
        )
    """), {"company_id": company_id, "aid": driver_assignment_id,
           "statuses": list(SOURCE_ENTRY_STATUSES)})
    return bool(result.scalar_one())


async def delete_assignment_eligibility_rows(
    company_id: int,
    driver_assignment_id: int,
    db: AsyncConnection,
) -> None:
    """Remove the (never-effective) assignment's roster rows before it is deleted."""
    await db.execute(text("""
        DELETE FROM payroll.PayrollPeriodDriverEligibility
        WHERE CompanyID = :company_id AND DriverAssignmentID = :aid
    """), {"company_id": company_id, "aid": driver_assignment_id})


async def recapture_assignment_eligibility(
    company_id: int,
    branch_id: int,
    driver_assignment_id: int,
    periods: list[dict],
    actor_user_id: int,
    db: AsyncConnection,
) -> None:
    """Re-capture the one named assignment's roster row in the locked editable periods.

    ``periods`` comes from ``lock_boundary_periods`` (all Draft/Open/Returned).
    Only rows of this assignment in this Branch are deleted and rebuilt with the
    canonical capture SQL; rows of other assignments are untouched. A period
    Bonus needs at least one captured WorkDate, so an active Bonus left without
    one refuses the operation.
    """
    for period in periods:
        if period["branchid"] != branch_id:
            continue
        period_id = period["payrollperiodid"]
        # The captured zone is immutable: the rebuilt row keeps the zone of the row it
        # replaces and recomputes its dates under it.
        captured_zone = (await db.execute(text("""
            SELECT BranchTimeZoneIdSnapshot
            FROM payroll.PayrollPeriodDriverEligibility
            WHERE PayrollPeriodID = :pid AND CompanyID = :cid AND BranchID = :bid
              AND DriverAssignmentID = :aid
        """), {"pid": period_id, "cid": company_id, "bid": branch_id,
               "aid": driver_assignment_id})).scalar_one_or_none()
        await db.execute(text("""
            DELETE FROM payroll.PayrollPeriodDriverEligibility
            WHERE PayrollPeriodID = :pid AND CompanyID = :cid AND BranchID = :bid
              AND DriverAssignmentID = :aid
        """), {"pid": period_id, "cid": company_id, "bid": branch_id, "aid": driver_assignment_id})
        frozen = period["status"] in {"Open", "Returned"}
        await _create_period_driver_eligibility_rows(
            period_id, company_id, branch_id, db,
            snapshot_source="Generated", created_by_user_id=actor_user_id,
            freeze=frozen, frozen_by_user_id=actor_user_id if frozen else None,
            only_driver_assignment_id=driver_assignment_id,
            time_zone_id=captured_zone,
        )
        uncovered_bonus = await db.execute(text("""
            SELECT 1
            FROM payroll.PayrollBonusEvents b
            WHERE b.PayrollPeriodID = :pid AND b.DriverAssignmentID = :aid AND b.Status = 'Active'
              AND NOT EXISTS (
                  SELECT 1 FROM payroll.PayrollPeriodDriverEligibility pde
                  WHERE pde.PayrollPeriodID = b.PayrollPeriodID
                    AND pde.DriverAssignmentID = b.DriverAssignmentID
                    AND cardinality(pde.EligibleWorkDates) > 0
              )
            LIMIT 1
        """), {"pid": period_id, "aid": driver_assignment_id})
        if uncovered_bonus.first() is not None:
            raise HTTPException(status_code=409, detail={
                "code": "DRIVER_ASSIGNMENT_PAYROLL_FACTS_UNCOVERED",
                "message": "An active Bonus would be left without any eligible WorkDate.",
            })


async def _regenerate_period_driver_eligibility_rows(
    period_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
    created_by_user_id: int | None = None,
    frozen_by_user_id: int | None = None,
) -> None:
    """Drop provisional snapshot rows and re-create frozen for Draft→Open promotion."""
    await db.execute(
        text(
            "DELETE FROM payroll.payrollperioddrivereligibility "
            "WHERE payrollperiodid = :pid"
        ),
        {"pid": period_id},
    )
    await _create_period_driver_eligibility_rows(
        period_id, company_id, branch_id, db,
        snapshot_source="Generated",
        freeze=True,
        created_by_user_id=created_by_user_id,
        frozen_by_user_id=frozen_by_user_id,
    )
    await _freeze_period_driver_eligibility_snapshot(
        period_id, db, frozen_by_user_id=frozen_by_user_id,
    )


async def _freeze_period_driver_eligibility_snapshot(
    period_id: int,
    db: AsyncConnection,
    frozen_by_user_id: int | None = None,
) -> None:
    """Freeze all unfrozen eligibility rows and the marker row for a period."""
    now = datetime.now(UTC)
    await db.execute(
        text("""
            UPDATE payroll.payrollperioddrivereligibility
            SET frozenatutc    = CAST(:now AS TIMESTAMPTZ),
                frozenbyuserid = :uid,
                updatedatutc   = CAST(:now AS TIMESTAMPTZ)
            WHERE payrollperiodid = :pid
              AND frozenatutc IS NULL
        """),
        {"pid": period_id, "now": now, "uid": frozen_by_user_id},
    )
    # Also freeze the marker row
    await db.execute(
        text("""
            UPDATE payroll.payrollperiodeligibilitysnapshots
            SET frozenatutc    = CAST(:now AS TIMESTAMPTZ),
                frozenbyuserid = :uid,
                updatedatutc   = CAST(:now AS TIMESTAMPTZ)
            WHERE payrollperiodid = :pid
              AND frozenatutc IS NULL
        """),
        {"pid": period_id, "now": now, "uid": frozen_by_user_id},
    )

# ── End CP-2E helpers ─────────────────────────────────────────────────────────



# ---------------------------------------------------------------------------
# Period-eligible drivers (P1 #1) (Stage B4-21)
# ---------------------------------------------------------------------------

async def get_period_eligible_drivers(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[dict]:
    """
    Return drivers eligible for a new Bonus in the given period.

    Candidates come from captured eligible rows whose exact WorkDates still
    overlap a live assignment. Transferred and terminated historical profiles
    remain eligible only when that captured Branch-local day is covered.
    IncludedByExistingData never gains new Bonus authority from old source.

    DRIVER/Self users are blocked unconditionally (same boundary as day-grid).
    payroll.view OR payroll.entry permission is required.
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await require_non_driver_subject(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: the Bonus eligible driver list is a financial path — block for Draft.
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Period eligible drivers are not available for Prepared (Draft) periods.",
        )

    await check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    await require_period_eligibility_snapshot(period_id, company_id, period.branch_id, db)
    # Active / TerminatedHistorical / Transferred → prospective choices.
    # IncludedByExistingData is excluded: it cannot create new Bonus money.
    snap_result = await db.execute(
        text("""
                SELECT ppde.driverassignmentid,
                       COALESCE(ppde.drivernamesnapshot, '') AS drivername,
                       COALESCE(ppde.drivernumbersnapshot, '') AS drivernumber
                FROM   payroll.payrollperioddrivereligibility ppde
                WHERE  ppde.payrollperiodid = :period_id
                  AND  ppde.companyid       = :cid
                  AND  ppde.branchid        = :bid
                  AND  ppde.iseligibleforperiod = TRUE
                  AND  ppde.eligibilityreasoncode <> 'IncludedByExistingData'
                ORDER BY ppde.drivernamesnapshot
        """),
        {"period_id": period_id, "cid": company_id, "bid": period.branch_id},
    )
    snapshot_rows = snap_result.mappings().all()
    live_eligible_ids = await _lifecycle_eligible_assignment_ids(
        period_id, company_id, period.branch_id, None,
        [int(r["driverassignmentid"]) for r in snapshot_rows], db,
    )
    return [
        {
            "driver_assignment_id": int(r["driverassignmentid"]),
            "driver_name":          r["drivername"],
            "driver_number":        r["drivernumber"],
        }
        for r in snapshot_rows
        if int(r["driverassignmentid"]) in live_eligible_ids
    ]
