"""CP-5B read model for period Fully-Off and selected-day Off drivers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_any_permission
from app.access.policy import require_non_driver_subject
from app.payroll import status_evidence
from app.payroll.eligibility import (
    _is_snapshot_row_eligible_for_workdate,
    require_period_eligibility_snapshot,
)
from app.payroll.period_day_calendar import _validate_period_work_date, resolve_period_work_date
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import (
    FullyOffDriverSummary,
    OffDriversSummaryResponse,
    PeriodSummary,
    SelectedDayOffDriver,
    SelectedDayOffDriversResponse,
)

_FINALIZED_STATUSES = ("Locked", "Archived")


@dataclass(frozen=True)
class _DriverIdentity:
    driver_assignment_id: int
    driver_id: int
    driver_name: str
    driver_number: str | None


@dataclass(frozen=True)
class _StatusEntry:
    status_key_id: int | None
    status_code: str | None
    status_label: str | None
    is_off_reason: bool
    note: str | None


def _period_id(period) -> int:
    value = getattr(period, "payroll_period_id", None)
    if value is None:
        value = period.period_id
    return int(value)


async def _readable_period(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
):
    """Apply the established Current Payroll read boundary before new reads."""
    await require_non_driver_subject(company_id, user_id, db)
    period = await get_period_by_id(company_id, user_id, period_id, db)
    await check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db,
    )
    return period


async def _scheduled_work_days(period, db: AsyncConnection) -> set[date]:
    """Read the immutable calendar when present; treat every day as scheduled otherwise."""
    rows = (await db.execute(
        text("""
            SELECT workdate, isdefaultworkday, isaddedworkday
            FROM payroll.payrollperioddays
            WHERE payrollperiodid = :period_id
            ORDER BY workdate
        """),
        {"period_id": _period_id(period)},
    )).mappings().all()
    if rows:
        return {
            row["workdate"]
            for row in rows
            if row["isdefaultworkday"] or row["isaddedworkday"]
        }

    # Retained fallback when no PeriodDay snapshot exists: treat every day
    # within the period bounds as scheduled.
    days: set[date] = set()
    current = period.start_date
    while current <= period.end_date:
        days.add(current)
        current += timedelta(days=1)
    return days


async def _eligible_driver_days(
    period,
    company_id: int,
    db: AsyncConnection,
    *,
    candidate_days: set[date] | None = None,
) -> dict[int, tuple[_DriverIdentity, set[date]]]:
    """Return date eligibility exclusively from the required frozen roster."""
    await require_period_eligibility_snapshot(
        _period_id(period), company_id, period.branch_id, db,
    )
    eligible_dates = candidate_days if candidate_days is not None else await _scheduled_work_days(period, db)
    if not eligible_dates:
        return {}
    rows = (await db.execute(text("""
        SELECT pde.driverassignmentid,
               (SELECT a.driverid FROM core.driverassignments a
                 WHERE a.driverassignmentid = pde.driverassignmentid) AS permanentdriverid,
               COALESCE(pde.drivernamesnapshot, '') AS drivername,
               pde.drivernumbersnapshot AS drivernumber,
               pde.iseligibleforperiod, pde.eligibilityreasoncode,
               pde.eligibleworkdates
        FROM payroll.payrollperioddrivereligibility pde
        WHERE pde.payrollperiodid = :period_id AND pde.companyid = :company_id
          AND pde.branchid = :branch_id AND pde.iseligibleforperiod = TRUE
        ORDER BY COALESCE(pde.drivernamesnapshot, ''),
                 pde.drivernumbersnapshot, pde.driverassignmentid
    """), {
        "period_id": _period_id(period), "company_id": company_id,
        "branch_id": period.branch_id,
    })).mappings().all()
    output: dict[int, tuple[_DriverIdentity, set[date]]] = {}
    for row in rows:
        snapshot = SimpleNamespace(**dict(row))
        eligible_days = {
            work_date for work_date in eligible_dates
            if _is_snapshot_row_eligible_for_workdate(snapshot, work_date)
        }
        if eligible_days:
            driver_assignment_id = int(row["driverassignmentid"])
            output[driver_assignment_id] = (
                _DriverIdentity(
                    driver_assignment_id, int(row["permanentdriverid"]),
                    row["drivername"], row["drivernumber"],
                ),
                eligible_days,
            )
    return output


async def _finalized_status_entries(
    period, company_id: int, db: AsyncConnection,
) -> tuple[dict[tuple[int, date], _StatusEntry], dict[str, str | None]]:
    """
    Stage B3 Unit 8C-5: Locked/Archived Status evidence.

    Reuses the same Approved-PeriodApproval-review-item snapshot authority
    and shared read primitives as Day Grid (Unit 8C-3) via
    status_evidence.py -- never the EntryState freeze columns
    (StatusCodeSnapshot/StatusLabelSnapshot/StatusIsOffReasonSnapshot/
    FinalizedAtUtc), never live PayrollStatusKeys, never legacy DraftLines.
    """
    period_id = _period_id(period)
    branch_id = period.branch_id
    evidence_rows, evidence_state = await status_evidence.finalized_status_evidence(
        db, period_id=period_id, company_id=company_id, branch_id=branch_id,
    )
    # NoteText is not part of the immutable Status-evidence table's contract
    # (see status_evidence.status_entries_from_content) -- read it separately from
    # canonical EntryState, which no write path can change once a period is
    # Locked (Locked/Archived are in schemas._WRITE_BLOCKED_STATUSES).
    note_rows = (await db.execute(
        text("""
            SELECT driverassignmentid, workdate, notetext
            FROM payroll.payrollperioddriverdayentrystate
            WHERE payrollperiodid = :period_id
              AND companyid = :company_id
              AND isvoided = FALSE
        """),
        {"period_id": period_id, "company_id": company_id},
    )).mappings().all()
    notes_by_day = {
        (int(row["driverassignmentid"]), row["workdate"]): row["notetext"] for row in note_rows
    }

    if evidence_rows is None:
        # No usable, verified snapshot -- never fall back to another
        # snapshot or to mutable current state; evidence is unavailable.
        return {}, evidence_state

    entries: dict[tuple[int, date], _StatusEntry] = {}
    for row in evidence_rows:
        key = (row["driver_assignment_id"], row["work_date"])
        entries[key] = _StatusEntry(
            status_key_id=row["status_key_id"],
            status_code=row["status_code"],
            status_label=row["status_label"],
            is_off_reason=row["is_off_reason"],
            note=notes_by_day.get(key),
        )
    return entries, evidence_state


async def _status_entries(
    period, company_id: int, db: AsyncConnection,
) -> tuple[dict[tuple[int, date], _StatusEntry], dict[str, str | None] | None]:
    """
    Canonical entry state wins; DailyStatus/DailyNote is legacy compatibility
    only. Unchanged for Draft/Open/InReview/Returned/Approved.

    Stage B3 Unit 8C-5: for Locked/Archived, delegates to
    _finalized_status_entries -- Status meaning comes only from immutable
    calculation-snapshot evidence. The second return value is None for
    non-finalized periods and {state, reason_code} for Locked/Archived.
    """
    if period.status in _FINALIZED_STATUSES:
        return await _finalized_status_entries(period, company_id, db)

    canonical_rows = (await db.execute(
        text("""
            SELECT es.driverassignmentid, es.workdate, es.statuskeyid, es.notetext,
                   es.finalizedatutc, es.statuscodesnapshot, es.statuslabelsnapshot,
                   es.statusisoffreasonsnapshot, sk.statuscode, sk.keyname, sk.isoffreason
            FROM payroll.payrollperioddriverdayentrystate es
            LEFT JOIN payroll.payrollstatuskeys sk ON sk.statuskeyid = es.statuskeyid
            WHERE es.payrollperiodid = :period_id
              AND es.companyid = :company_id
              AND es.isvoided = FALSE
        """),
        {"period_id": _period_id(period), "company_id": company_id},
    )).mappings().all()
    entries: dict[tuple[int, date], _StatusEntry] = {}
    for row in canonical_rows:
        if row["statuskeyid"] is None:
            continue
        frozen = row["finalizedatutc"] is not None
        entries[(int(row["driverassignmentid"]), row["workdate"])] = _StatusEntry(
            status_key_id=int(row["statuskeyid"]),
            status_code=(row["statuscodesnapshot"] if frozen else row["statuscode"]),
            status_label=(row["statuslabelsnapshot"] if frozen else row["keyname"]),
            is_off_reason=bool(
                row["statusisoffreasonsnapshot"] if frozen else row["isoffreason"]
            ),
            note=row["notetext"],
        )

    legacy_rows = (await db.execute(
        text("""
            SELECT ds.driverassignmentid, ds.workdate, ds.notes AS statuscode,
                   sk.statuskeyid, sk.keyname, sk.isoffreason, dn.notes AS note
            FROM payroll.payrolldraftlines ds
            LEFT JOIN payroll.payrollstatuskeys sk
              ON sk.companyid = ds.companyid
             AND sk.statuscode = ds.notes
            LEFT JOIN payroll.payrolldraftlines dn
              ON dn.payrollperiodid = ds.payrollperiodid
             AND dn.driverassignmentid = ds.driverassignmentid
             AND dn.workdate = ds.workdate
             AND dn.linetype = 'DailyNote'
             AND dn.status != 'Void'
            WHERE ds.payrollperiodid = :period_id
              AND ds.companyid = :company_id
              AND ds.linetype = 'DailyStatus'
              AND ds.status != 'Void'
        """),
        {"period_id": _period_id(period), "company_id": company_id},
    )).mappings().all()
    for row in legacy_rows:
        key = (int(row["driverassignmentid"]), row["workdate"])
        entries.setdefault(key, _StatusEntry(
            status_key_id=int(row["statuskeyid"]) if row["statuskeyid"] is not None else None,
            status_code=row["statuscode"],
            status_label=row["keyname"],
            is_off_reason=bool(row["isoffreason"]),
            note=row["note"],
        ))
    return entries, None


async def _normal_work_pairs(period, company_id: int, db: AsyncConnection) -> set[tuple[int, date]]:
    """Ordinary source against an active period definition identifies work, never money."""
    rows = (await db.execute(
        text("""
            SELECT DISTINCT dl.driverassignmentid, dl.workdate
            FROM payroll.payrolldraftlines dl
            JOIN payroll.payrollperioddefinitions ppd
              ON ppd.payrollperioddefinitionid = dl.payrollperioddefinitionid
             AND ppd.payrollperiodid = dl.payrollperiodid
            WHERE dl.payrollperiodid = :period_id
              AND dl.companyid = :company_id
              AND dl.branchid = :branch_id
              AND dl.status != 'Void'
              AND dl.workdate >= :period_start
              AND dl.workdate <= :period_end
              AND dl.quantity IS NOT NULL
              AND dl.quantity <> 0
              AND dl.sourcetype <> 'System'
              AND ppd.isactiveinperiod = TRUE
        """),
        {
            "period_id": _period_id(period),
            "company_id": company_id,
            "branch_id": period.branch_id,
            "period_start": period.start_date,
            "period_end": period.end_date,
        },
    )).mappings().all()
    return {(int(row["driverassignmentid"]), row["workdate"]) for row in rows}


async def resolve_fully_off_drivers(
    period,
    company_id: int,
    db: AsyncConnection,
    *,
    precomputed_statuses: dict[tuple[int, date], _StatusEntry] | None = None,
) -> list[FullyOffDriverSummary]:
    """Resolve the official distinct-driver Fully-Off KPI for one payroll period.

    precomputed_statuses lets a caller that already resolved _status_entries
    (to also read its evidence-availability state, e.g. get_off_drivers_summary)
    avoid a second read; unset by existing callers (e.g. current_hub.py),
    which keep computing it here exactly as before.
    """
    eligible = await _eligible_driver_days(period, company_id, db)
    if not eligible:
        return []
    if precomputed_statuses is not None:
        statuses = precomputed_statuses
    else:
        statuses, _unused_state = await _status_entries(period, company_id, db)
    normal_work = await _normal_work_pairs(period, company_id, db)
    fully_off: list[FullyOffDriverSummary] = []
    for driver_assignment_id, (driver, days) in eligible.items():
        if all(
            statuses.get((driver_assignment_id, work_date), _StatusEntry(None, None, None, False, None)).is_off_reason
            and (driver_assignment_id, work_date) not in normal_work
            for work_date in days
        ):
            fully_off.append(FullyOffDriverSummary(
                driver_assignment_id=driver.driver_assignment_id,
                driver_id=driver.driver_id,
                driver_number=driver.driver_number,
                driver_name=driver.driver_name,
                eligible_scheduled_day_count=len(days),
                off_day_count=len(days),
            ))
    return fully_off


async def get_off_drivers_summary(
    period_id: int, company_id: int, user_id: int, db: AsyncConnection,
) -> OffDriversSummaryResponse:
    period = await _readable_period(period_id, company_id, user_id, db)
    statuses, finalized_state = await _status_entries(period, company_id, db)
    drivers = await resolve_fully_off_drivers(
        period, company_id, db, precomputed_statuses=statuses,
    )
    return OffDriversSummaryResponse(
        period_id=_period_id(period),
        start_date=period.start_date,
        end_date=period.end_date,
        total_fully_off_drivers=len(drivers),
        fully_off_drivers=drivers,
        status_evidence=finalized_state,
    )


async def get_selected_day_off_drivers(
    period_id: int,
    work_date: date | None,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> SelectedDayOffDriversResponse:
    period = await _readable_period(period_id, company_id, user_id, db)
    work_date = await resolve_period_work_date(
        company_id, period.branch_id, period.start_date, period.end_date, work_date, db,
    )
    await _validate_period_work_date(
        _period_id(period), work_date, period.start_date, period.end_date, db,
    )
    eligible = await _eligible_driver_days(
        period, company_id, db, candidate_days={work_date},
    )
    statuses, finalized_state = await _status_entries(period, company_id, db)
    drivers: list[SelectedDayOffDriver] = []
    for driver_assignment_id, (driver, eligible_days) in eligible.items():
        if work_date not in eligible_days:
            continue
        status = statuses.get((driver_assignment_id, work_date))
        if status is None or not status.is_off_reason:
            continue
        drivers.append(SelectedDayOffDriver(
            driver_assignment_id=driver.driver_assignment_id,
            driver_id=driver.driver_id,
            driver_number=driver.driver_number,
            driver_name=driver.driver_name,
            work_date=work_date,
            day_name=work_date.strftime("%A"),
            status_key_id=status.status_key_id,
            status_code=status.status_code,
            status_label=status.status_label,
            is_off_reason=True,
            has_note=bool(status.note),
            note=status.note,
        ))
    drivers.sort(key=lambda item: (item.driver_name or "", item.driver_number or "", item.driver_assignment_id))
    return SelectedDayOffDriversResponse(
        period_id=_period_id(period),
        work_date=work_date,
        day_name=work_date.strftime("%A"),
        total_count=len(drivers),
        drivers=drivers,
        status_evidence=finalized_state,
    )


# ---------------------------------------------------------------------------
# Legacy period-wide Drivers Off (GET /periods/{id}/drivers-off)
# ---------------------------------------------------------------------------
#
# This is the legacy period-wide surface (all work dates, DailyStatus-driven
# for non-finalized periods), distinct from the surface above (Fully-Off
# KPI + selected-day, EntryState-first). The two contracts are deliberately
# NOT unified: get_drivers_off reads legacy DailyStatus DraftLines + live
# PayrollStatusKeys + a DailyNote join for Draft/Open/InReview/Returned/
# Approved periods, while _finalized_drivers_off_entries reads only
# immutable calculation-snapshot Status evidence (via status_evidence.py)
# for Locked/Archived periods — never live PayrollStatusKeys, never the
# EntryState freeze columns. NoteText is read separately from canonical
# EntryState in the finalized path because it is not part of the immutable
# Status-evidence table's contract (see status_evidence.status_entries_from_content).

async def _finalized_drivers_off_entries(
    period: PeriodSummary,
    company_id: int,
    db: AsyncConnection,
) -> tuple[list[dict], dict[str, str | None]]:
    """
    Stage B3 Unit 8C-7: Locked/Archived Status evidence for CP-2.5 Drivers Off.

    Reuses the same Approved-PeriodApproval-review-item snapshot authority
    and shared read primitives as Day Grid (Unit 8C-3) and CP-5B Off Drivers
    (Unit 8C-5) via status_evidence.py -- never the EntryState freeze columns
    (StatusCodeSnapshot/StatusLabelSnapshot/StatusIsOffReasonSnapshot/
    FinalizedAtUtc), never live PayrollStatusKeys, never legacy DailyStatus
    DraftLines. Returns (entries, {state, reason_code}); entries is always
    [] unless state is AVAILABLE with at least one captured off-reason row.
    """
    status_rows, evidence_state = await status_evidence.finalized_status_evidence(
        db, period_id=period.payroll_period_id, company_id=company_id, branch_id=period.branch_id,
    )
    if status_rows is None:
        # No usable, verified snapshot -- never fall back to another
        # snapshot or to mutable current state; evidence is unavailable.
        return [], evidence_state

    off_rows = [row for row in status_rows if row["is_off_reason"]]
    if not off_rows:
        return [], evidence_state

    driver_assignment_ids = sorted({row["driver_assignment_id"] for row in off_rows})
    identity_rows = (await db.execute(
        text("""
            SELECT a.driverassignmentid, a.driverid, e.fullname AS drivername, d.drivernumber
            FROM core.driverassignments a
            JOIN core.drivers d ON d.driverid = a.driverid
            JOIN core.employees e ON e.employeeid = a.employeeid
            WHERE a.companyid = :company_id
              AND a.driverassignmentid  = ANY(:driver_assignment_ids)
        """),
        {"company_id": company_id, "driver_assignment_ids": driver_assignment_ids},
    )).mappings().all()
    identities = {int(r["driverassignmentid"]): r for r in identity_rows}

    # NoteText is not part of the immutable Status-evidence table's contract
    # (see status_evidence.status_entries_from_content) -- read it separately from
    # canonical EntryState, which no write path can change once a period is
    # Locked (Locked/Archived are in _WRITE_BLOCKED_STATUSES).
    note_rows = (await db.execute(
        text("""
            SELECT driverassignmentid, workdate, notetext
            FROM payroll.payrollperioddriverdayentrystate
            WHERE payrollperiodid = :period_id
              AND companyid = :company_id
              AND isvoided = FALSE
        """),
        {"period_id": period.payroll_period_id, "company_id": company_id},
    )).mappings().all()
    notes_by_day = {
        (int(row["driverassignmentid"]), row["workdate"]): row["notetext"] for row in note_rows
    }

    entries: list[dict] = []
    for row in sorted(off_rows, key=lambda r: (r["work_date"], r["driver_assignment_id"])):
        identity = identities.get(row["driver_assignment_id"])
        if identity is None:
            continue
        entries.append({
            "driver_assignment_id":       row["driver_assignment_id"],
            "driver_id":       int(identity["driverid"]),
            "driver_name":     identity["drivername"],
            "driver_number":     identity["drivernumber"],
            "work_date":       row["work_date"],
            "status_key_code": row["status_code"],
            "status_label":    row["status_label"],
            "notes":           notes_by_day.get((row["driver_assignment_id"], row["work_date"])),
        })
    return entries, evidence_state


async def get_drivers_off(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> tuple[list[dict], dict[str, str | None] | None]:
    """
    Return all off-driver records for the entire period (all work dates).

    Draft/Open/InReview/Returned/Approved (unchanged): an off-driver record
    is a legacy DailyStatus line whose status code maps to a live
    PayrollStatusKeys row with IsOffReason = TRUE. The status key code is
    stored in the Notes column of DailyStatus lines. An optional DailyNote
    line for the same driver/date is joined to supply the driver-level notes
    text.

    Locked/Archived (Stage B3 Unit 8C-7): Status meaning comes only from
    immutable calculation-snapshot evidence via _finalized_drivers_off_entries
    -- never live PayrollStatusKeys, never legacy DailyStatus DraftLines.
    Returns (entries, {state, reason_code}) instead of (entries, None).

    DRIVER/Self users are blocked unconditionally.
    payroll.view OR payroll.entry permission is required.
    """
    # ── Driver-role hard-block ───────────────────────────────────────────────── #
    await require_non_driver_subject(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    await check_any_permission(
        company_id, user_id, period.branch_id, ["payroll.view", "payroll.entry"], db
    )

    if period.status in ("Locked", "Archived"):
        entries, evidence_state = await _finalized_drivers_off_entries(period, company_id, db)
        return entries, evidence_state

    result = await db.execute(
        text("""
            SELECT
                a.driverassignmentid,
                a.driverid,
                e.fullname       AS drivername,
                d.drivernumber,
                dl.workdate,
                dl.notes         AS status_key_code,
                sk.keyname       AS status_label,
                dn.notes         AS driver_notes
            FROM payroll.payrolldraftlines dl
            JOIN core.driverassignments a ON a.driverassignmentid = dl.driverassignmentid
            JOIN core.drivers   d  ON d.driverid  = a.driverid
            JOIN core.employees e  ON e.employeeid = a.employeeid
            JOIN payroll.payrollstatuskeys sk
                ON  sk.companyid  = dl.companyid
                AND sk.statuscode = dl.notes
                AND sk.isoffreason = TRUE
                AND sk.isactive    = TRUE
            LEFT JOIN payroll.payrolldraftlines dn
                ON  dn.payrollperiodid = dl.payrollperiodid
                AND dn.driverassignmentid        = dl.driverassignmentid
                AND dn.workdate        = dl.workdate
                AND dn.linetype        = 'DailyNote'
                AND dn.status         != 'Void'
            WHERE dl.payrollperiodid = :period_id
              AND dl.companyid       = :company_id
              AND dl.linetype        = 'DailyStatus'
              AND dl.status         != 'Void'
            ORDER BY dl.workdate, e.fullname
        """),
        {"period_id": period_id, "company_id": company_id},
    )

    rows = result.mappings().all()
    return [
        {
            "driver_assignment_id":        int(r["driverassignmentid"]),
            "driver_id":        int(r["driverid"]),
            "driver_name":      r["drivername"],
            "driver_number":      r["drivernumber"],
            "work_date":        r["workdate"],
            "status_key_code":  r["status_key_code"],
            "status_label":     r["status_label"],
            "notes":            r["driver_notes"],
        }
        for r in rows
    ], None
