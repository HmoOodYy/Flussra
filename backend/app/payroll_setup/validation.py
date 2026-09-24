"""Payroll-chronology checks shared by policy publication and assignments."""

from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .chronology import Schedule, is_period_start
from .errors import PolicyError


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


async def next_version_boundary(db: AsyncConnection, setup_id: int, after: date) -> date | None:
    result = await db.execute(text("""
        SELECT MIN(EffectiveFromDate) FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
          AND EffectiveFromDate > :after
    """), {"sid": setup_id, "after": after})
    return result.scalar_one_or_none()


async def validate_next_version(
    db: AsyncConnection, company_id: int, branch_id: int, setup_id: int,
    schedule: Schedule, after: date, *, assignment_end: date | None = None,
) -> None:
    """Check every future Version boundary reached by this Branch assignment."""
    result = await db.execute(text("""
        SELECT DISTINCT EffectiveFromDate FROM payroll.PayrollSetupVersions
        WHERE PayrollSetupID = :sid AND LifecycleState = 'Published'
          AND EffectiveFromDate > :after
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
