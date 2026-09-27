"""Payroll-chronology checks shared by policy publication and assignments."""

from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .chronology import Schedule, ScheduleSegment, is_period_start
from .errors import PolicyError


async def terminal_segments(db: AsyncConnection, setup_id: int) -> list[ScheduleSegment]:
    """The Setup's version timeline: terminal Published Versions ascending by effective date."""
    result = await db.execute(text("""
        SELECT v.* FROM payroll.PayrollSetupVersions v
        WHERE v.PayrollSetupID = :sid AND v.LifecycleState = 'Published'
          AND NOT EXISTS (
              SELECT 1 FROM payroll.PayrollSetupVersions child
              WHERE child.ReplacesVersionID = v.PayrollSetupVersionID
          )
        ORDER BY v.EffectiveFromDate
    """), {"sid": setup_id})
    rows = result.mappings().all()
    segments: list[ScheduleSegment] = []
    seen_dates: set = set()
    for row in rows:
        effective_from = row["effectivefromdate"]
        if effective_from in seen_dates:
            raise PolicyError("VERSION_TIMELINE_INVALID", "Multiple terminal versions at one date")
        seen_dates.add(effective_from)
        segments.append(ScheduleSegment(effective_from, version_schedule(row)))
    return segments


async def terminal_version(
    db: AsyncConnection, setup_id: int, on_date: date,
):
    result = await db.execute(text("""
        SELECT v.* FROM payroll.PayrollSetupVersions v
        WHERE v.PayrollSetupID = :sid AND v.LifecycleState = 'Published'
          AND v.EffectiveFromDate = (
              SELECT MAX(EffectiveFromDate) FROM payroll.PayrollSetupVersions
              WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
                AND EffectiveFromDate <= :on_date
          )
          AND NOT EXISTS (
              SELECT 1 FROM payroll.PayrollSetupVersions child
              WHERE child.ReplacesVersionID = v.PayrollSetupVersionID
          )
    """), {"sid": setup_id, "on_date": on_date})
    rows = result.mappings().all()
    if len(rows) > 1:
        raise PolicyError("VERSION_TIMELINE_INVALID", "Multiple terminal versions at one date")
    return rows[0] if rows else None


def version_schedule(version) -> Schedule | None:
    if version is None:
        return None
    return Schedule(version["payrollfrequency"], version["anchorstartdate"],
                    version["customintervaldays"], version["normaldaysoffmask"])


async def validate_boundary(
    db: AsyncConnection, company_id: int, branch_id: int, change_date: date,
    predecessor: Schedule | None, successor: Schedule | None,
    *, affected_until: date | None = None, protect_future_periods: bool = True,
) -> None:
    """Protect non-cancelled periods and both sides of a proposed cadence change."""
    await validate_period_history(
        db, company_id, branch_id, change_date,
        affected_until=affected_until,
        protect_future_periods=protect_future_periods,
    )

    result = await db.execute(text("""
        SELECT MAX(EndDate) FROM payroll.PayrollPeriods
        WHERE CompanyID = :cid AND BranchID = :bid AND Status <> 'Cancelled'
          AND EndDate < :change_date
    """), {"cid": company_id, "bid": branch_id, "change_date": change_date})
    prior_end = result.scalar_one_or_none()
    if predecessor is not None:
        prior_origin = predecessor.anchor_start_date
        if prior_end is not None:
            prior_origin = max(prior_origin, prior_end + timedelta(days=1))
        if not is_period_start(predecessor, change_date, origin=prior_origin):
            raise PolicyError("PREDECESSOR_BOUNDARY_INVALID", "Change splits predecessor cadence")
    if successor is not None and not is_period_start(successor, change_date):
        raise PolicyError("SUCCESSOR_BOUNDARY_INVALID", "Change is not a successor period start")


async def validate_period_history(
    db: AsyncConnection, company_id: int, branch_id: int, change_date: date,
    *, affected_until: date | None = None, protect_future_periods: bool = True,
) -> None:
    """Raise the established history conflict before cadence explanations."""
    result = await db.execute(text("""
        SELECT PayrollPeriodID, StartDate, EndDate FROM payroll.PayrollPeriods
        WHERE CompanyID = :cid AND BranchID = :bid AND Status <> 'Cancelled'
          AND EndDate >= :change_date
          AND (:protect_future OR StartDate < :change_date)
          AND (CAST(:until AS DATE) IS NULL OR StartDate < CAST(:until AS DATE))
        ORDER BY StartDate, PayrollPeriodID
    """), {"cid": company_id, "bid": branch_id, "change_date": change_date,
           "until": affected_until, "protect_future": protect_future_periods})
    conflicts = result.mappings().all()
    if conflicts:
        raise PolicyError("PERIOD_HISTORY_CONFLICT", "Non-cancelled payroll periods protect this authority interval")


async def next_version_boundary(db: AsyncConnection, setup_id: int, after: date) -> date | None:
    result = await db.execute(text("""
        SELECT MIN(v.EffectiveFromDate) FROM payroll.PayrollSetupVersions v
        WHERE v.PayrollSetupID = :sid AND v.LifecycleState = 'Published'
          AND NOT EXISTS (
              SELECT 1 FROM payroll.PayrollSetupVersions child
              WHERE child.ReplacesVersionID = v.PayrollSetupVersionID
          )
          AND v.EffectiveFromDate > :after
    """), {"sid": setup_id, "after": after})
    return result.scalar_one_or_none()


async def validate_policy_version_transition(
    db: AsyncConnection, setup_id: int, effective_from_date: date,
    schedule: Schedule,
) -> None:
    """Validate a candidate against the Setup's terminal Published timeline.

    This is deliberately independent of Branch assignments. A policy's
    Published Version chronology must be valid before any Branch follows it.
    Same-date replacements are handled naturally because ``terminal_segments``
    excludes the replaced row, leaving the real predecessor and successor
    terminal points on either side of the candidate date.
    """
    if effective_from_date < schedule.anchor_start_date:
        raise PolicyError("INVALID_EFFECTIVE_DATE", "Publication precedes the schedule anchor")
    if not is_period_start(schedule, effective_from_date):
        raise PolicyError("SUCCESSOR_BOUNDARY_INVALID", "Effective date is not a schedule boundary")

    segments = await terminal_segments(db, setup_id)
    predecessor = next(
        (segment for segment in reversed(segments)
         if segment.effective_from < effective_from_date),
        None,
    )
    successor = next(
        (segment for segment in segments
         if segment.effective_from > effective_from_date),
        None,
    )

    if predecessor is not None and not is_period_start(
        predecessor.schedule, effective_from_date,
    ):
        raise PolicyError(
            "PREDECESSOR_BOUNDARY_INVALID",
            f"Effective date {effective_from_date.isoformat()} splits the predecessor payroll period",
        )
    if successor is not None and not is_period_start(
        schedule, successor.effective_from,
    ):
        raise PolicyError(
            "SUCCESSOR_BOUNDARY_INVALID",
            f"Existing scheduled update on {successor.effective_from.isoformat()} "
            "would not start on a valid boundary under this schedule",
        )


async def validate_next_version(
    db: AsyncConnection, company_id: int, branch_id: int, setup_id: int,
    schedule: Schedule, after: date, *, assignment_end: date | None = None,
) -> None:
    """Check every future Version boundary reached by this Branch assignment."""
    result = await db.execute(text("""
        SELECT DISTINCT v.EffectiveFromDate FROM payroll.PayrollSetupVersions v
        WHERE v.PayrollSetupID = :sid AND v.LifecycleState = 'Published'
          AND NOT EXISTS (
              SELECT 1 FROM payroll.PayrollSetupVersions child
              WHERE child.ReplacesVersionID = v.PayrollSetupVersionID
          )
          AND v.EffectiveFromDate > :after
          AND (CAST(:until AS DATE) IS NULL
               OR EffectiveFromDate < CAST(:until AS DATE))
        ORDER BY EffectiveFromDate
    """), {"sid": setup_id, "after": after, "until": assignment_end})
    dates = [row[0] for row in result.all()]
    segment_start = after
    for next_date in dates:
        next_schedule = version_schedule(await terminal_version(db, setup_id, next_date))
        result = await db.execute(text("""
            SELECT MAX(EndDate) FROM payroll.PayrollPeriods
            WHERE CompanyID = :cid AND BranchID = :bid AND Status <> 'Cancelled'
              AND StartDate >= :segment_start AND EndDate < :next_date
        """), {"cid": company_id, "bid": branch_id,
               "segment_start": segment_start, "next_date": next_date})
        prior_end = result.scalar_one_or_none()
        origin = max(segment_start, prior_end + timedelta(days=1)) if prior_end else segment_start
        if not is_period_start(schedule, next_date, origin=origin):
            raise PolicyError("PREDECESSOR_BOUNDARY_INVALID", "Future Version splits cadence")
        await validate_boundary(db, company_id, branch_id, next_date,
                                None, next_schedule, protect_future_periods=False)
        schedule = next_schedule
        segment_start = next_date
