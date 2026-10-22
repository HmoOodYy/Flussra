"""Full-day-off integrity: a day that counts as a full day off carries no ordinary work.

A Status with ``IsOffReason`` ("counts as a full day off") cannot coexist, for the same
DriverAssignment and WorkDate, with a nonzero ordinary daily quantity: a non-void DraftLine with
a PayrollPeriodDefinitionID and Quantity > 0. The rule holds in either write order and in every
writer of either side (Day Grid save, ordinary source-line mutation), under the Period mutation
lock the writers already hold, so concurrent writers of one Period are serialized. A partial or
non-off Status may coexist with ordinary work, and clearing the Status or the quantity in the
same save is allowed because the rule judges the final state.

The database repeats the rule as a backstop (migration 0090, ``fn_guard_status_off_work``) for
writers that bypass the application. This module gives the clear 422 first.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

STATUS_OFF_WORK_CONFLICT = "STATUS_OFF_WORK_CONFLICT"


def _conflict(driver_assignment_id: int, work_date: date) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": STATUS_OFF_WORK_CONFLICT,
            "message": (
                f"Driver {driver_assignment_id} cannot have a full-day-off Status and ordinary "
                f"work on {work_date}. Clear the Status or the quantity."
            ),
        },
    )


@dataclass(frozen=True)
class FinalDayState:
    """What one DriverAssignment's day will hold after a Day Grid row is saved.

    ``status_is_off`` is whether the resolved Status (None when the row clears it) is a
    full-day-off Status. ``quantities`` are the submitted ordinary quantities by
    PayrollPeriodDefinitionID (None or zero clears); definitions not submitted keep the
    quantity the day already has.
    """

    driver_assignment_id: int
    status_is_off: bool
    quantities: dict[int, Decimal | None]


async def assert_no_off_status_work_conflicts(
    *,
    period_id: int,
    company_id: int,
    work_date: date,
    rows: list[FinalDayState],
    db: AsyncConnection,
) -> None:
    """Refuse (422 STATUS_OFF_WORK_CONFLICT) when any row's final state has an off-day Status
    and ordinary work. One set-wise read of the existing quantities for the whole batch."""
    candidates = [row for row in rows if row.status_is_off]
    if not candidates:
        return
    existing = (await db.execute(
        text("""
            SELECT driverassignmentid, payrollperioddefinitionid
            FROM   payroll.payrolldraftlines
            WHERE  payrollperiodid = :pid AND companyid = :cid AND workdate = :dt
              AND  driverassignmentid = ANY(:ids)
              AND  status <> 'Void' AND payrollperioddefinitionid IS NOT NULL AND quantity > 0
        """),
        {"pid": period_id, "cid": company_id, "dt": work_date,
         "ids": sorted({row.driver_assignment_id for row in candidates})},
    )).mappings().all()
    kept: dict[int, set[int]] = {}
    for line in existing:
        kept.setdefault(int(line["driverassignmentid"]), set()).add(
            int(line["payrollperioddefinitionid"]))
    for row in candidates:
        worked = {
            definition for definition in kept.get(row.driver_assignment_id, set())
            if definition not in row.quantities
        } | {
            definition for definition, quantity in row.quantities.items()
            if quantity is not None and quantity > 0
        }
        if worked:
            raise _conflict(row.driver_assignment_id, work_date)


async def assert_no_off_status_with_work(
    period_id: int, company_id: int, driver_assignment_id: int, work_date: date,
    db: AsyncConnection,
) -> None:
    """For a source-line writer about to leave ordinary work (quantity > 0) on the day: refuse
    when the day already holds a full-day-off Status."""
    off = (await db.execute(
        text("""
            SELECT 1
            FROM   payroll.payrollperioddriverdayentrystate e
            JOIN   payroll.payrollstatuskeys k
                   ON k.statuskeyid = e.statuskeyid AND k.companyid = e.companyid
            WHERE  e.payrollperiodid = :pid AND e.companyid = :cid
              AND  e.driverassignmentid = :did AND e.workdate = :dt
              AND  e.isvoided = FALSE AND k.isoffreason
            LIMIT 1
        """),
        {"pid": period_id, "cid": company_id, "did": driver_assignment_id, "dt": work_date},
    )).first()
    if off is not None:
        raise _conflict(driver_assignment_id, work_date)
