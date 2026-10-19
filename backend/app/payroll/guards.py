"""
Payroll access and finalized-period guards shared with Compensation.

Internal cross-area contract: ``check_not_in_finalized_period`` performs a
read-only company/branch/date check and raises HTTP 422 when the effective
date falls inside a Locked or Archived period (inclusive). It serves the legacy
assignment-owned status-rate writers. ``require_no_finalized_payroll`` is the one
guard for Driver-owned compensation: it asks the database whether a footprint
touches finalized payroll of the Driver in ANY Branch, through ANY of the Driver's
assignments. Neither owns authorization, locks, commits or caller ordering.
"""
from datetime import date

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_any_permission
from app.access.policy import require_non_driver_subject


async def _require_non_driver_rate_subject(
    company_id: int, user_id: int, db: AsyncConnection,
) -> None:
    """Generic rate administration is unavailable to every DRIVER subject."""
    await require_non_driver_subject(company_id, user_id, db)


# ---------------------------------------------------------------------------
# Finalized-period guard — shared by approve_rate and batch_save_rates
# ---------------------------------------------------------------------------

async def check_not_in_finalized_period(
    company_id: int,
    branch_id: int,
    effective_from: date,
    db: AsyncConnection,
    *,
    label: str = "rate",
) -> None:
    """
    Raise HTTP 422 when effective_from falls inside a Locked or Archived period
    for this company and branch. The date comparison includes both period bounds.

    Called for legacy status-rate approval and batch auto-approval to prevent retroactive
    finalized changes. Driver-owned compensation (rate schedules, pay rules) uses
    ``require_no_finalized_payroll`` instead.
    """
    result = await db.execute(
        text("""
            SELECT payrollperiodid, startdate, enddate, status
            FROM   payroll.payrollperiods
            WHERE  companyid = :cid
              AND  branchid  = :bid
              AND  status    IN ('Locked', 'Archived')
              AND  startdate <= CAST(:eff_from AS date)
              AND  enddate   >= CAST(:eff_from AS date)
            LIMIT 1
        """),
        {"cid": company_id, "bid": branch_id, "eff_from": effective_from},
    )
    row = result.mappings().first()
    if row is not None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot set a {label} effective inside a finalized payroll period "
                f"({row['status']} period {row['startdate']} to {row['enddate']})."
            ),
        )


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


async def _check_driver_read_access(
    driver_assignment_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> int:
    """
    Shared security gate for all driver-scoped rate read endpoints
    (pending, history, summary).

    Rejects DRIVER subjects before looking up the Driver in the Company.
    Then requires payrates.view, payrates.edit, settings.manage, or setup.manage
    on the Driver's branch.

    Returns the driver's branch_id on success.

    Note: explicit check_branch_access is not called separately because
    check_any_permission with the driver's actual branch_id is sufficient
    to enforce branch scope (fn_UserHasPermission returns FALSE for
    SpecificBranch users on a different branch).
    """
    await _require_non_driver_rate_subject(company_id, user_id, db)
    drv_result = await db.execute(
        text("SELECT branchid FROM core.driverassignments WHERE driverassignmentid = :did AND companyid = :cid"),
        {"did": driver_assignment_id, "cid": company_id},
    )
    drv_row = drv_result.mappings().first()
    if drv_row is None:
        raise HTTPException(status_code=404, detail="Driver not found.")
    branch_id: int = drv_row["branchid"]
    await check_any_permission(
        company_id, user_id, branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )
    return branch_id
