"""
Shared payroll access/guard helpers.

Extracted from app.payroll.service (Stage B4-2A) as a dependency-closed leaf
module — no behavior change, pure relocation. These four functions were
proven, by caller audit, to be genuinely shared by at least the Rates and
Driver Pay Rules domains (several are also called directly from Lifecycle,
Finalization, Calculation, Day Grid, and Current Payroll Hub code) rather
than owned by any single domain. This module exists to
remove that incorrect ownership from service.py so both Rates and a future
Driver Pay Rules module can depend on a neutral leaf instead of importing
app.payroll.service.

Do not add unrelated helpers here. This is not a general utilities module.
"""
from datetime import date

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.policy import require_non_driver_subject
from app.core.service import _check_any_permission


async def _require_non_driver_rate_subject(
    company_id: int, user_id: int, db: AsyncConnection,
) -> None:
    """Generic rate administration is unavailable to every DRIVER subject."""
    await require_non_driver_subject(company_id, user_id, db)


# ---------------------------------------------------------------------------
# Finalized-period guard — shared by approve_rate and batch_save_rates
# ---------------------------------------------------------------------------

async def _check_not_in_finalized_period(
    company_id: int,
    branch_id: int,
    effective_from: date,
    db: AsyncConnection,
    *,
    label: str = "rate",
) -> None:
    """
    Raise HTTP 422 if effective_from falls inside a Locked or Archived payroll
    period for the same company and branch.

    Called before approving or auto-approving a rate (label="rate") or creating/
    copying a pay rule (label="pay rule") so that payroll history cannot be
    retroactively altered inside finalized pay periods.
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

    Performs:
      1. Driver lookup — 404 if not in this company.
      2. Permission check with driver's branch_id — 403 if no payrates.* permission.
      3. DRIVER/Self ceiling — generic rate administration is denied.

    Returns the driver's branch_id on success.

    Note: explicit _check_branch_access is not called separately because
    _check_any_permission with the driver's actual branch_id is sufficient
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
    await _check_any_permission(
        company_id, user_id, branch_id,
        ["payrates.view", "payrates.edit", "settings.manage", "setup.manage"], db,
    )
    return branch_id
