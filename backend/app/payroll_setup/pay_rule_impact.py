"""Read-only warning: Driver Minimum/Maximum pay rules a calendar change would orphan.

A Driver pay rule records the pay calendar it was set for and applies only to a payroll
period that is exactly one period of that calendar. A new Payroll Setup version, or a Branch
moving to another Setup, can therefore leave rules that no longer fit the Branch's first
period under the new schedule; payroll for those Drivers then cannot be submitted until each
rule is ended and set again. This module only reports those rules. It never ends, rewrites
or blocks anything, and it carries no amounts.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .chronology import Schedule, period_end, period_matches_schedule


@dataclass(frozen=True)
class SchedulePhase:
    """A Branch span, [start, end_exclusive), that runs the new schedule."""

    branch_id: int
    start: date
    end_exclusive: date | None


async def affected_pay_rules(
    company_id: int, phases: Iterable[SchedulePhase], successor: Schedule,
    db: AsyncConnection,
) -> list[dict]:
    """Rules in force on or after the span start, of Drivers with a Branch tenure that
    reaches the span, whose recorded calendar is not exactly the first period of the new
    schedule starting on the span start."""
    found: dict[tuple[int, int, int], dict] = {}
    for phase in phases:
        first_end = period_end(successor, phase.start)
        rows = (await db.execute(text("""
            SELECT dr.driverid, dr.drivernumber, e.fullname, b.branchid, b.branchname,
                   r.driverpayruleid, r.ruletype, r.calendarfrequency,
                   r.calendarcustomintervaldays, r.calendaranchorstartdate,
                   r.effectivefrom, r.effectiveto
            FROM core.driverassignments a
            JOIN core.branches b ON b.branchid = a.branchid AND b.companyid = a.companyid
            JOIN core.drivers dr ON dr.driverid = a.driverid AND dr.companyid = a.companyid
            JOIN core.employees e ON e.employeeid = dr.employeeid AND e.companyid = dr.companyid
            JOIN payroll.driverpayrules r
              ON r.driverid = dr.driverid AND r.companyid = dr.companyid
            WHERE a.companyid = :cid AND a.branchid = :bid
              AND (a.effectivetoat IS NULL OR a.effectivetoat > a.effectivefromat)
              AND core.fn_BusinessDate(b.timezoneid, a.effectivefromat)
                  < COALESCE(CAST(:end AS date), DATE '9999-12-31')
              AND (a.effectivetoat IS NULL
                   OR core.fn_BusinessDate(
                          b.timezoneid, a.effectivetoat - INTERVAL '1 microsecond') >= :start)
              AND r.status <> 'Voided'
              AND (r.effectiveto IS NULL OR r.effectiveto >= :start)
              AND r.effectivefrom < COALESCE(CAST(:end AS date), DATE '9999-12-31')
            ORDER BY e.fullname, dr.driverid, r.ruletype, r.driverpayruleid
        """), {"cid": company_id, "bid": phase.branch_id, "start": phase.start,
               "end": phase.end_exclusive})).mappings().all()
        for row in rows:
            rule_schedule = Schedule(
                row["calendarfrequency"], row["calendaranchorstartdate"],
                row["calendarcustomintervaldays"], 0,
            )
            if period_matches_schedule(rule_schedule, phase.start, first_end):
                continue
            key = (int(row["branchid"]), int(row["driverid"]), int(row["driverpayruleid"]))
            found.setdefault(key, {
                "driver_id": int(row["driverid"]),
                "driver_name": row["fullname"],
                "driver_number": row["drivernumber"],
                "branch_id": int(row["branchid"]),
                "branch_name": row["branchname"],
                "rule_id": int(row["driverpayruleid"]),
                "rule_type": row["ruletype"],
                "calendar_frequency": row["calendarfrequency"],
                "calendar_custom_interval_days": row["calendarcustomintervaldays"],
                "calendar_anchor_start_date": row["calendaranchorstartdate"],
                "effective_from": row["effectivefrom"],
                "effective_to": row["effectiveto"],
            })
    return sorted(found.values(), key=lambda item: (
        item["driver_name"], item["driver_id"], item["branch_id"],
        item["rule_type"], item["rule_id"]))
