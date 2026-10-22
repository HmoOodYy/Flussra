"""
Finalized-payroll guards shared with Compensation.

Internal cross-area contract: ``require_no_finalized_payroll`` is the one guard for
Driver-owned compensation (rate schedules of PayDefinitions and of Status rate columns, and
pay rules): it asks the database whether a footprint touches finalized payroll of the Driver
in ANY Branch, through ANY of the Driver's assignments. It owns no authorization, locks,
commits or caller ordering.
"""
from datetime import date

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def require_no_finalized_payroll(
    company_id: int,
    driver_id: int,
    footprint_from: date | None,
    footprint_to: date | None,
    mode: str,
    db: AsyncConnection,
) -> None:
    """Refuse (422) a Driver-owned compensation change whose footprint touches finalized payroll.

    A period is finalized when it is Locked or Archived and any DriverAssignment of the
    Driver has an eligibility row in it, in any Branch. ``mode`` is ``WorkDate`` (a rate
    schedule: the period's work dates overlap the footprint) or ``PeriodStart`` (a pay rule:
    the period's start date is inside the footprint). The footprint is inclusive on both
    ends; ``None`` is unbounded, and an inverted (empty) footprint touches nothing.
    """
    if (footprint_from is not None and footprint_to is not None
            and footprint_from > footprint_to):
        return
    rows = (await db.execute(
        text("""
            SELECT payrollperiodid, startdate, enddate, status
            FROM   payroll.fn_DriverFinalizedPayrollTouched(
                       :cid, :did, daterange(CAST(:lo AS date), CAST(:hi AS date), '[]'), :mode)
        """),
        {"cid": company_id, "did": driver_id, "lo": footprint_from, "hi": footprint_to,
         "mode": mode},
    )).mappings().all()
    if not rows:
        return
    first = rows[0]
    raise HTTPException(
        status_code=422,
        detail={
            "code": "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD",
            "message": (
                "This change touches finalized payroll "
                f"({first['status']} period {first['startdate']} to {first['enddate']})."
            ),
            "payroll_period_ids": [int(row["payrollperiodid"]) for row in rows],
        },
    )


async def driver_payroll_period_branches(
    company_id: int, driver_id: int, from_date: date, db: AsyncConnection,
) -> set[int]:
    """Branches of every payroll period (any status) of the Driver that reaches ``from_date``
    or later, through ANY of the Driver's assignments.

    This is the lock-discovery twin of ``require_no_finalized_payroll``: both ask the same
    database predicate, ``payroll.fn_DriverPayrollPeriodsForFootprint``, so a Branch whose
    period the guard may judge is always a Branch whose workflow lock the writer took. WorkDate
    mode over ``[from_date, infinity)`` is a superset of PeriodStart mode over the same range.
    """
    rows = (await db.execute(
        text("""
            SELECT DISTINCT branchid
            FROM   payroll.fn_DriverPayrollPeriodsForFootprint(
                       :cid, :did, daterange(CAST(:lo AS date), NULL, '[]'), 'WorkDate')
        """),
        {"cid": company_id, "did": driver_id, "lo": from_date},
    )).scalars().all()
    return {int(branch_id) for branch_id in rows}
