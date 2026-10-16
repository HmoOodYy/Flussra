"""
Payroll access and finalized-period guards shared with Compensation.

Internal cross-area contract: ``check_not_in_finalized_period`` performs a
read-only company/branch/date check and raises HTTP 422 when the effective
date falls inside a Locked or Archived period (inclusive). It owns no
authorization, lock, commit, or caller ordering.
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

    Called for rate approval, batch rate auto-approval, Compensation assignment
    approval, and pay-rule creation to prevent retroactive finalized changes.
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


async def _check_driver_read_access(
    driver_id: int,
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
        text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": driver_id, "cid": company_id},
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
