"""
Payroll driver eligibility helpers.

Extracted from app.payroll.service (Stage B4-1) as a dependency-closed leaf
module — no behavior change, pure relocation. Two of these symbols
(_period_has_driver_eligibility_snapshot, _is_snapshot_row_eligible_for_workdate)
are consumed directly by app.payroll.current_hub, app.payroll.off_drivers,
and app.payroll.finalized_library_read_model (retargeted off the
app.payroll.service compatibility facade in Stage B4-22).

Stage B4-21 moved get_period_eligible_drivers here from app.payroll.service —
pure relocation, no behavior change. This module already owned the table its
primary (snapshot) branch reads, payroll.payrollperioddrivereligibility, via
_period_has_driver_eligibility_snapshot/_create_period_driver_eligibility_rows
above, and this module's own _assert_driver_eligible_for_period docstring
already documented parity with get_period_eligible_drivers's period-pay
eligibility list — table ownership, not physical adjacency, is why it moved
here rather than to period_read.py, off_drivers.py, or period_pay.py. This
is the one symbol in the B4 residue that costs this module its status as a
zero-app-dependency leaf: it now imports app.core.service (permission/role
guards) and app.payroll.period_read (the period access gate), verified to
introduce no import cycle in either direction.
"""
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _build_in_clause, _check_any_permission, _require_not_driver_role
from app.payroll.period_read import get_period_by_id


# ---------------------------------------------------------------------------
# Driver eligibility helpers (shared by all payroll write/finalization paths)
# ---------------------------------------------------------------------------

async def _assert_driver_eligible_for_date(
    company_id: int,
    driver_id: int,
    branch_id: int,
    work_date: "date",
    db: AsyncConnection,
) -> None:
    """
    Raise HTTP 422 if the driver is not eligible to work on *work_date* in the
    given branch.

    Uses the exact same criteria as the day-grid eligibility query so that the
    write paths are consistent with what the grid shows:
      - driver belongs to company and branch
      - employee employment_status = 'Active'
      - driver_status = 'Active'
        OR (driver_status = 'Transferred' AND effectiveto IS NOT NULL AND effectiveto >= work_date)
      - hire_date IS NULL OR hire_date <= work_date
      - termination_date IS NULL OR termination_date >= work_date
      - effective_from IS NULL OR effective_from <= work_date
      - effective_to   IS NULL OR effective_to   >= work_date
    """
    result = await db.execute(
        text("""
            SELECT 1
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            WHERE  d.driverid         = :did
              AND  d.companyid        = :cid
              AND  d.branchid         = :bid
              AND  e.employmentstatus = 'Active'
              AND  (
                       d.driverstatus = 'Active'
                    OR (d.driverstatus = 'Transferred'
                        AND d.effectiveto IS NOT NULL
                        AND d.effectiveto >= :dt)
                   )
              AND  (e.hiredate IS NULL OR e.hiredate <= :dt)
              AND  (e.terminationdate IS NULL OR e.terminationdate >= :dt)
              AND  (d.effectivefrom IS NULL OR d.effectivefrom <= :dt)
              AND  (d.effectiveto   IS NULL OR d.effectiveto   >= :dt)
        """),
        {"did": driver_id, "cid": company_id, "bid": branch_id, "dt": work_date},
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Driver is not eligible for this payroll period, branch, or work date."
            ),
        )


async def _assert_driver_eligible_for_period(
    company_id: int,
    driver_id: int,
    branch_id: int,
    period_start: "date",
    period_end: "date",
    db: AsyncConnection,
) -> None:
    """
    Raise HTTP 422 if the driver is not eligible for at least one day in the
    payroll period.

    Mirrors the period-pay eligibility list in get_period_eligible_drivers:
      - driver belongs to company and branch
      - employment_status = 'Active'
      - driver_status = 'Active'  (no in-transfer; transferred drivers keep their
        old profile Active until the effective date passes)
      - hire/termination window overlaps the period
      - driver effective window (effectivefrom/effectiveto) overlaps the period

    This intentionally excludes 'Transferred' drivers (driverstatus = 'Transferred'
    means they have already left this branch).
    """
    result = await db.execute(
        text("""
            SELECT 1
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            WHERE  d.driverid         = :did
              AND  d.companyid        = :cid
              AND  d.branchid         = :bid
              AND  e.employmentstatus = 'Active'
              AND  d.driverstatus     = 'Active'
              AND  (e.hiredate IS NULL OR e.hiredate <= :period_end)
              AND  (e.terminationdate IS NULL OR e.terminationdate >= :period_start)
              AND  (d.effectivefrom IS NULL OR d.effectivefrom <= :period_end)
              AND  (d.effectiveto   IS NULL OR d.effectiveto   >= :period_start)
        """),
        {
            "did":          driver_id,
            "cid":          company_id,
            "bid":          branch_id,
            "period_start": period_start,
            "period_end":   period_end,
        },
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Driver is not eligible for this payroll period or branch."
            ),
        )


# ── CP-2E: Canonical Eligibility Snapshot helpers ────────────────────────────

async def _period_has_driver_eligibility_snapshot(
    period_id: int, db: AsyncConnection
) -> bool:
    """Check marker table — one row per snapshotted period, even if zero drivers eligible."""
    row = (await db.execute(
        text(
            "SELECT 1 FROM payroll.payrollperiodeligibilitysnapshots "
            "WHERE payrollperiodid = :pid LIMIT 1"
        ),
        {"pid": period_id},
    )).first()
    return row is not None


async def _get_driver_eligibility_row(
    period_id: int, driver_id: int, company_id: int, branch_id: int,
    db: AsyncConnection,
):
    return (await db.execute(
        text("""
            SELECT payrollperioddrivereligibilityid,
                   eligibilityreasoncode,
                   hiredatesnapshot,
                   terminationdatesnapshot,
                   drivereffectivefromsnapshot,
                   drivereffectivetosnapshot,
                   frozenatutc,
                   drivernamesnapshot,
                   drivercodesnapshot,
                   employeekeysnapshot,
                   iseligibleforperiod
            FROM   payroll.payrollperioddrivereligibility
            WHERE  payrollperiodid = :pid
              AND  driverid        = :did
              AND  companyid       = :cid
              AND  branchid        = :bid
        """),
        {"pid": period_id, "did": driver_id, "cid": company_id, "bid": branch_id},
    )).first()


def _is_snapshot_row_eligible_for_workdate(row, work_date: date) -> bool:
    """Derive date-level eligibility from a snapshot row.

    Returns True only if the driver's snapshotted date windows cover work_date.
    IncludedByExistingData always returns False here — such drivers require an
    existing-source check (DB query) and are never eligible purely by window.
    Generated-row reason codes (Active/TerminatedHistorical/Transferred) are
    evaluated against their snapshotted hire/termination/effectivefrom/effectiveto.
    """
    if row is None or not row.iseligibleforperiod:
        return False
    reason = row.eligibilityreasoncode

    # IBED is never window-eligible; always requires existing-source rescue
    if reason == "IncludedByExistingData":
        return False

    hire     = row.hiredatesnapshot
    term     = row.terminationdatesnapshot
    eff_from = row.drivereffectivefromsnapshot
    eff_to   = row.drivereffectivetosnapshot

    if hire is not None and hire > work_date:
        return False
    if eff_from is not None and eff_from > work_date:
        return False
    if eff_to is not None and eff_to < work_date:
        return False

    if reason == "TerminatedHistorical":
        if term is None or term < work_date:
            return False
    else:
        if term is not None and term < work_date:
            return False
    return True


async def _driver_has_existing_daily_source_on_date(
    period_id: int, driver_id: int, work_date: date, db: AsyncConnection
) -> bool:
    r = (await db.execute(
        text("""
            SELECT 1 FROM payroll.payrolldraftlines
            WHERE payrollperiodid = :pid AND driverid = :did
              AND workdate = :dt AND status != 'Void'
            LIMIT 1
        """),
        {"pid": period_id, "did": driver_id, "dt": work_date},
    )).first()
    if r:
        return True
    r2 = (await db.execute(
        text("""
            SELECT 1 FROM payroll.payrollperioddriverdayentrystate
            WHERE payrollperiodid = :pid AND driverid = :did
              AND workdate = :dt AND isvoided = FALSE
            LIMIT 1
        """),
        {"pid": period_id, "did": driver_id, "dt": work_date},
    )).first()
    return r2 is not None


async def _driver_has_existing_period_pay_source(
    period_id: int, driver_id: int, db: AsyncConnection
) -> bool:
    r = (await db.execute(
        text("""
            SELECT 1 FROM payroll.payrolldraftlines
            WHERE payrollperiodid = :pid AND driverid = :did
              AND linescope = 'Period' AND status != 'Void'
            LIMIT 1
        """),
        {"pid": period_id, "did": driver_id},
    )).first()
    return r is not None


async def _assert_driver_eligible_for_workdate_via_snapshot(
    company_id: int, branch_id: int, period_id: int,
    driver_id: int, work_date: date, db: AsyncConnection,
    allow_existing_source_rescue: bool = True,
) -> None:
    """Check eligibility for a specific work date, using snapshot when available.

    Primary gate: date-window check via _is_snapshot_row_eligible_for_workdate.
    Secondary gate (rescue): if allow_existing_source_rescue=True, a driver
    whose date window does not cover work_date is still allowed if they have
    existing daily source (DraftLine or EntryState) on that exact date.
    This rescue applies to ALL reason codes, not just IncludedByExistingData —
    a generated-row driver (Active/TerminatedHistorical/Transferred) may have
    data outside their eligibility window from before the snapshot was frozen.

    Callers:
    - save_day_grid:         allow_existing_source_rescue=True  (default)
    - update_draft_line:     allow_existing_source_rescue=True  (default)
    - add_draft_line:        allow_existing_source_rescue=False (new source — no rescue)
    """
    if not await _period_has_driver_eligibility_snapshot(period_id, db):
        await _assert_driver_eligible_for_date(company_id, driver_id, branch_id, work_date, db)
        return
    row = await _get_driver_eligibility_row(period_id, driver_id, company_id, branch_id, db)
    if row is None:
        raise HTTPException(
            status_code=422,
            detail="Driver has no eligibility snapshot for this period.",
        )
    # Primary: date-window check
    if _is_snapshot_row_eligible_for_workdate(row, work_date):
        return
    # Secondary rescue: existing source on exact date (any reason code)
    if allow_existing_source_rescue:
        has_existing = await _driver_has_existing_daily_source_on_date(
            period_id, driver_id, work_date, db
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
    driver_id: int, db: AsyncConnection,
) -> None:
    """Check period-level eligibility, using snapshot when available."""
    if not await _period_has_driver_eligibility_snapshot(period_id, db):
        period = (await db.execute(
            text(
                "SELECT startdate, enddate FROM payroll.payrollperiods "
                "WHERE payrollperiodid = :pid"
            ),
            {"pid": period_id},
        )).first()
        await _assert_driver_eligible_for_period(
            company_id, driver_id, branch_id, period.startdate, period.enddate, db
        )
        return
    row = await _get_driver_eligibility_row(period_id, driver_id, company_id, branch_id, db)
    if row is None:
        raise HTTPException(
            status_code=422,
            detail="Driver has no eligibility snapshot for this period.",
        )
    if row.eligibilityreasoncode == "IncludedByExistingData":
        has_existing = await _driver_has_existing_period_pay_source(period_id, driver_id, db)
        if not has_existing:
            raise HTTPException(
                status_code=422,
                detail="Driver is not eligible for new period-pay entries in this period.",
            )
        return
    # Active / TerminatedHistorical / Transferred — present in snapshot = period-eligible


async def _create_period_driver_eligibility_rows(
    period_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
    snapshot_source: str = "Generated",
    freeze: bool = False,
    created_by_user_id: int | None = None,
    frozen_by_user_id: int | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    frozen_at = now if freeze else None

    # Paths 1–3: Active / TerminatedHistorical / Transferred
    await db.execute(text("""
        INSERT INTO payroll.payrollperioddrivereligibility
            (companyid, branchid, payrollperiodid, driverid, sourceemployeeid,
             drivercodesnapshot, drivernamesnapshot, employeekeysnapshot,
             driverstatussnapshot,
             employmentstatussnapshot,
             transferredfromdriveridsnapshot, transferredtodriveridsnapshot,
             hiredatesnapshot, terminationdatesnapshot,
             drivereffectivefromsnapshot, drivereffectivetosnapshot,
             iseligibleforperiod, eligibilityreasoncode, snapshotsource,
             createdatutc, createdbyuserid, updatedatutc, frozenatutc, frozenbyuserid)
        SELECT
            pp.companyid, pp.branchid, pp.payrollperiodid,
            d.driverid, e.employeeid,
            d.drivercode, e.fullname, e.employeekey, d.driverstatus, e.employmentstatus,
            d.transferredfromdriverid, d.transferredtodriverid,
            e.hiredate, e.terminationdate, d.effectivefrom, d.effectiveto,
            TRUE,
            CASE
                WHEN d.driverstatus = 'Terminated' THEN 'TerminatedHistorical'
                WHEN d.driverstatus = 'Transferred' THEN 'Transferred'
                ELSE 'Active'
            END,
            :src, CAST(:now AS TIMESTAMPTZ), CAST(:uid AS INTEGER), CAST(:now AS TIMESTAMPTZ), CAST(:frozen_at AS TIMESTAMPTZ), CAST(:fuid AS INTEGER)
        FROM payroll.payrollperiods pp
        JOIN core.drivers d ON d.companyid = pp.companyid AND d.branchid = pp.branchid
        JOIN core.employees e ON e.employeeid = d.employeeid
        WHERE pp.payrollperiodid = :pid
          AND (
            (    d.driverstatus     = 'Active'
             AND e.employmentstatus = 'Active'
             AND (e.hiredate IS NULL OR e.hiredate <= pp.enddate)
             AND (e.terminationdate IS NULL OR e.terminationdate >= pp.startdate)
             AND (d.effectivefrom IS NULL OR d.effectivefrom <= pp.enddate)
             AND (d.effectiveto   IS NULL OR d.effectiveto   >= pp.startdate)
            )
            OR
            (    d.driverstatus     = 'Terminated'
             AND e.employmentstatus = 'Terminated'
             AND e.terminationdate IS NOT NULL
             AND e.terminationdate >= pp.startdate
             AND (e.hiredate IS NULL OR e.hiredate <= pp.enddate)
             AND (d.effectivefrom IS NULL OR d.effectivefrom <= pp.enddate)
             AND (d.effectiveto   IS NULL OR d.effectiveto   >= pp.startdate)
            )
            OR
            (    d.driverstatus     = 'Transferred'
             AND e.employmentstatus = 'Active'
             AND d.effectiveto IS NOT NULL
             AND d.effectiveto >= pp.startdate
             AND (d.effectivefrom IS NULL OR d.effectivefrom <= pp.enddate)
             AND (e.hiredate IS NULL OR e.hiredate <= pp.enddate)
             AND (e.terminationdate IS NULL OR e.terminationdate >= pp.startdate)
            )
          )
        ON CONFLICT (payrollperiodid, driverid) DO NOTHING
    """), {
        "pid": period_id, "src": snapshot_source, "now": now,
        "uid": created_by_user_id, "frozen_at": frozen_at, "fuid": frozen_by_user_id,
    })

    # Path 4: IncludedByExistingData
    await db.execute(text("""
        INSERT INTO payroll.payrollperioddrivereligibility
            (companyid, branchid, payrollperiodid, driverid, sourceemployeeid,
             drivercodesnapshot, drivernamesnapshot, employeekeysnapshot,
             driverstatussnapshot,
             employmentstatussnapshot,
             transferredfromdriveridsnapshot, transferredtodriveridsnapshot,
             hiredatesnapshot, terminationdatesnapshot,
             drivereffectivefromsnapshot, drivereffectivetosnapshot,
             iseligibleforperiod, eligibilityreasoncode, snapshotsource,
             createdatutc, createdbyuserid, updatedatutc, frozenatutc, frozenbyuserid)
        SELECT DISTINCT
            pp.companyid, pp.branchid, pp.payrollperiodid,
            d.driverid, e.employeeid,
            d.drivercode, e.fullname, e.employeekey, d.driverstatus, e.employmentstatus,
            d.transferredfromdriverid, d.transferredtodriverid,
            e.hiredate, e.terminationdate, d.effectivefrom, d.effectiveto,
            TRUE, 'IncludedByExistingData', :src,
            CAST(:now AS TIMESTAMPTZ), CAST(:uid AS INTEGER), CAST(:now AS TIMESTAMPTZ), CAST(:frozen_at AS TIMESTAMPTZ), CAST(:fuid AS INTEGER)
        FROM payroll.payrollperiods pp
        JOIN (
            SELECT payrollperiodid, driverid, companyid
            FROM   payroll.payrolldraftlines
            WHERE  status != 'Void'
            UNION
            SELECT payrollperiodid, driverid, companyid
            FROM   payroll.payrollperioddriverdayentrystate
            WHERE  isvoided = FALSE
        ) src2 ON src2.payrollperiodid = pp.payrollperiodid
               AND src2.companyid = pp.companyid
        JOIN core.drivers   d ON d.driverid  = src2.driverid
                              AND d.companyid = pp.companyid
                              AND d.branchid  = pp.branchid
        JOIN core.employees e ON e.employeeid = d.employeeid
        WHERE pp.payrollperiodid = :pid
        ON CONFLICT (payrollperiodid, driverid) DO NOTHING
    """), {
        "pid": period_id, "src": snapshot_source, "now": now,
        "uid": created_by_user_id, "frozen_at": frozen_at, "fuid": frozen_by_user_id,
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
        ON CONFLICT (payrollperiodid) DO NOTHING
    """), {
        "pid": period_id, "src": snapshot_source, "now": now,
        "uid": created_by_user_id, "frozen_at": frozen_at, "fuid": frozen_by_user_id,
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
    now = datetime.now(timezone.utc)
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
    Return drivers eligible for a Bonus (or other period-pay line) for the
    given period.

    Eligibility = period-scoped, NOT day-scoped:
      1. Active drivers (employmentstatus='Active' AND driverstatus='Active')
         whose hire/termination window overlaps the period dates.
      2. OR any driver who already has period-pay lines in this period —
         so existing bonuses stay voidable even if the driver was later
         terminated.

    ODA/Driver users are blocked unconditionally (same boundary as day-grid).
    payroll.view OR payroll.entry permission is required.
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await _require_not_driver_role(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Period Pay / Bonus eligible driver list is a financial path — block for Draft.
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Period eligible drivers are not available for Prepared (Draft) periods.",
        )

    await _check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    # CP-2E: For snapshotted periods use the snapshot roster instead of live tables.
    _has_snap = await _period_has_driver_eligibility_snapshot(period_id, db)
    if _has_snap:
        # Active / TerminatedHistorical / Transferred → prospective choices
        # IncludedByExistingData → only if they already have a period-pay line
        snap_result = await db.execute(
            text("""
                SELECT ppde.driverid,
                       COALESCE(ppde.drivernamesnapshot, '') AS drivername,
                       COALESCE(ppde.drivercodesnapshot, '') AS drivercode,
                       ppde.eligibilityreasoncode
                FROM   payroll.payrollperioddrivereligibility ppde
                WHERE  ppde.payrollperiodid = :period_id
                  AND  ppde.companyid       = :cid
                  AND  ppde.branchid        = :bid
                  AND  ppde.iseligibleforperiod = TRUE
                ORDER BY ppde.drivernamesnapshot
            """),
            {"period_id": period_id, "cid": company_id, "bid": period.branch_id},
        )
        snap_rows = list(snap_result.mappings().all())

        # For IBED: check which have existing period-pay lines
        ibed_ids = [r["driverid"] for r in snap_rows if r["eligibilityreasoncode"] == "IncludedByExistingData"]
        ibed_with_period_pay: set[int] = set()
        if ibed_ids:
            in_cl, in_pr = _build_in_clause(ibed_ids, "ibed")
            ibed_res = await db.execute(
                text(f"""
                    SELECT DISTINCT driverid FROM payroll.payrolldraftlines
                    WHERE payrollperiodid = :period_id AND linescope = 'Period'
                      AND status != 'Void'
                      AND driverid IN ({in_cl})
                """),
                {"period_id": period_id, **in_pr},
            )
            ibed_with_period_pay = {r["driverid"] for r in ibed_res.mappings().all()}

        out = []
        for r in snap_rows:
            if r["eligibilityreasoncode"] == "IncludedByExistingData":
                if r["driverid"] not in ibed_with_period_pay:
                    continue
            out.append({
                "driver_id":   int(r["driverid"]),
                "driver_name": r["drivername"],
                "driver_code": r["drivercode"],
            })
        return out

    result = await db.execute(
        text("""
            SELECT DISTINCT d.driverid, e.fullname AS drivername, d.drivercode
            FROM   core.drivers   d
            JOIN   core.employees e ON e.employeeid = d.employeeid
            WHERE  d.companyid = :cid
              AND  d.branchid  = :bid
              AND  (
                    -- Active driver whose hire/termination window overlaps the period
                    (    e.employmentstatus = 'Active'
                     AND d.driverstatus     = 'Active'
                     AND (e.hiredate IS NULL OR e.hiredate <= :period_end)
                     AND (e.terminationdate IS NULL OR e.terminationdate >= :period_start)
                    )
                    OR
                    -- Driver who already has period-pay lines in this period
                    -- (keeps existing bonuses voidable even if driver was terminated)
                    EXISTS (
                        SELECT 1
                        FROM   payroll.payrolldraftlines pdl
                        WHERE  pdl.driverid        = d.driverid
                          AND  pdl.payrollperiodid = :period_id
                          AND  pdl.linescope        = 'Period'
                    )
              )
            ORDER BY e.fullname
        """),
        {
            "cid":          company_id,
            "bid":          period.branch_id,
            "period_start": period.start_date,
            "period_end":   period.end_date,
            "period_id":    period_id,
        },
    )
    rows = result.mappings().all()
    return [
        {
            "driver_id":   int(r["driverid"]),
            "driver_name": r["drivername"],
            "driver_code": r["drivercode"],
        }
        for r in rows
    ]
