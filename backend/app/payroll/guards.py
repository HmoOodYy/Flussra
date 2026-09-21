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

from app.core.service import _check_any_permission


# ---------------------------------------------------------------------------
# OwnDriverDataOnly scope guard — shared by all rate read/write endpoints
# ---------------------------------------------------------------------------

async def _get_oda_own_driver_id(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> int | None:
    """
    Returns the caller's own driver_id if and only if they have exactly one
    active role assignment AND it has OwnDriverDataOnly scope.

    Returns None (no ODA restriction) when:
    - No active assignment exists.
    - The single active assignment is SpecificBranch or AllCompanyBranches.
    - Multiple active assignments all have non-ODA scope (unusual but safe).

    Raises HTTP 403 (fail-closed) when multiple active assignments exist and
    ANY of them has OwnDriverDataOnly scope — this indicates data corruption
    that assign_company_role's revoke logic should have prevented.

    Raises HTTP 403 if ODA scope is confirmed but the user has no linked
    driver profile (Users.EmployeeID → Employees → Drivers).
    """
    scope_result = await db.execute(
        text("""
            SELECT scopetype, userbranchroleid
            FROM   sec.userbranchroles
            WHERE  userid    = :uid
              AND  companyid = :cid
              AND  isactive  = TRUE
            ORDER  BY userbranchroleid DESC
        """),
        {"uid": user_id, "cid": company_id},
    )
    scope_rows = scope_result.mappings().all()

    if not scope_rows:
        return None  # No active assignment — no ODA restriction

    if len(scope_rows) > 1:
        # Multiple active assignments: fail-closed if any is ODA (bad data).
        scope_types = {r["scopetype"] for r in scope_rows}
        if "OwnDriverDataOnly" in scope_types:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Ambiguous role assignments: multiple active company-role assignments "
                    "exist, including OwnDriverDataOnly scope. Access denied until "
                    "assignments are resolved by an administrator."
                ),
            )
        return None  # Multiple non-ODA assignments — no ODA restriction here

    # Exactly one active assignment
    if scope_rows[0]["scopetype"] != "OwnDriverDataOnly":
        return None  # AllCompanyBranches or SpecificBranch — no ODA restriction

    # ODA confirmed: resolve caller's linked driver profile
    own_drv_result = await db.execute(
        text("""
            SELECT d.driverid
            FROM   sec.users      u
            JOIN   core.employees e ON e.employeeid = u.employeeid
            JOIN   core.drivers   d ON d.employeeid = e.employeeid
                                    AND d.companyid  = :cid
                                    AND d.driverstatus NOT IN ('Transferred', 'Terminated')
            WHERE  u.userid = :uid
        """),
        {"uid": user_id, "cid": company_id},
    )
    own_drv_row = own_drv_result.mappings().first()
    if own_drv_row is None:
        raise HTTPException(
            status_code=403,
            detail="OwnDriverDataOnly: no linked driver profile found for your account.",
        )
    return int(own_drv_row["driverid"])


async def _check_own_driver_only(
    company_id: int,
    user_id: int,
    target_driver_id: int,
    db: AsyncConnection,
) -> None:
    """
    If the caller's single active assignment has OwnDriverDataOnly scope,
    verify that *target_driver_id* is their own linked driver.

    Raises HTTP 403 if the scope is OwnDriverDataOnly and the target driver
    does not match, if no linked driver profile exists, or if multiple active
    assignments exist with conflicting ODA scope (fail-closed).

    No-op for AllCompanyBranches and SpecificBranch scopes.

    Call this AFTER the branch-access check so that scope type is already
    known to be within the caller's company.
    """
    own_driver_id = await _get_oda_own_driver_id(company_id, user_id, db)
    if own_driver_id is None:
        return  # Not ODA — no restriction
    if own_driver_id != target_driver_id:
        raise HTTPException(
            status_code=403,
            detail="OwnDriverDataOnly: you may only access your own driver's rates.",
        )


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
      3. OwnDriverDataOnly scope check — 403 if ODA mismatch or ambiguous assignments.

    Returns the driver's branch_id on success.

    Note: explicit _check_branch_access is not called separately because
    _check_any_permission with the driver's actual branch_id is sufficient
    to enforce branch scope (fn_UserHasPermission returns FALSE for
    SpecificBranch users on a different branch).
    """
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
    await _check_own_driver_only(company_id, user_id, driver_id, db)
    return branch_id
