"""Resource-aware Access policy for the current DRIVER/Self product surface."""
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.workforce.effective import resolve_effective_driver_assignment, resolve_employee_driver


async def is_driver_self_subject(
    company_id: int, user_id: int, db: AsyncConnection
) -> bool:
    """Return whether any active assignment invokes the DRIVER/Self ceiling."""
    result = await db.execute(
        text("""
            SELECT EXISTS (
                SELECT 1
                FROM sec.UserBranchRoles ubr
                LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
                WHERE ubr.UserID = :uid
                  AND ubr.CompanyID = :cid
                  AND ubr.IsActive
                  AND (ubr.ScopeType = 'Self'
                       OR cr.RoleCode = 'DRIVER')
            )
        """),
        {"uid": user_id, "cid": company_id},
    )
    return bool(result.scalar_one())


async def require_non_driver_subject(
    company_id: int, user_id: int, db: AsyncConnection
) -> None:
    """Deny DRIVER/Self subjects from generic operational or admin surfaces."""
    if await is_driver_self_subject(company_id, user_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This operation is not available to DRIVER Self accounts.",
        )


async def resolve_driver_self(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    target_driver_id: int | None = None,
    at: datetime | None = None,
    require_assignment: bool = True,
) -> dict[str, Any]:
    """Resolve a DRIVER Self account to its own Driver and effective assignment.

    Own identity is always User -> Employee -> Driver -> effective DriverAssignment
    at the operation instant (database NOW() when ``at`` is None). It never comes
    from an Access scope. Every fail-closed case stays: staged, unlinked,
    cross-company, inactive or suspended accounts, a missing Driver, and, for
    current actions (``require_assignment=True``), a missing effective assignment
    (pending, terminated or transferred out).

    Returns ``{"employee_id", "driver_id", "driver", "assignment"}`` where
    ``assignment`` is the effective DriverAssignment row (or None for historical use).
    """
    account = await db.execute(
        text("""
            SELECT u.EmployeeID
            FROM sec.Users u
            JOIN core.Companies c ON c.CompanyID = u.CompanyID
            JOIN core.Employees e ON e.EmployeeID = u.EmployeeID
                                   AND e.CompanyID = u.CompanyID
            WHERE u.UserID = :uid
              AND u.CompanyID = :cid
              AND u.IsActive
              AND u.CanLogin
              AND NOT u.IsStaged
              AND c.Status = 'Active'
              AND NOT c.IsSuspended
              AND EXISTS (
                  SELECT 1
                  FROM sec.UserBranchRoles ubr
                  LEFT JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
                  WHERE ubr.UserID = u.UserID
                    AND ubr.CompanyID = u.CompanyID
                    AND ubr.IsActive
                    AND ubr.ScopeType = 'Self'
                    AND cr.RoleCode = 'DRIVER'
              )
        """),
        {"uid": user_id, "cid": company_id},
    )
    row = account.mappings().first()
    if row is None or row["employeeid"] is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A valid linked DRIVER Self account is required.",
        )

    employee_id = int(row["employeeid"])
    driver = await resolve_employee_driver(company_id, employee_id, db)
    if driver is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No Driver is linked to this DRIVER Self account.",
        )
    own_driver_id = int(driver["driverid"])
    if target_driver_id is not None and own_driver_id != target_driver_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A DRIVER Self account may only act on its own Driver.",
        )
    assignment = await resolve_effective_driver_assignment(company_id, own_driver_id, at, db)
    if assignment is None and require_assignment:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No effective current Driver assignment is available for this operation.",
        )
    return {
        "employee_id": employee_id,
        "driver_id": own_driver_id,
        "driver": driver,
        "assignment": assignment,
    }


async def require_self_owns_assignment(
    company_id: int,
    user_id: int,
    driver_assignment_id: int,
    db: AsyncConnection,
    *,
    current_only: bool = False,
) -> dict[str, Any]:
    """Prove that a DRIVER Self account owns a DriverAssignment.

    Ownership is ``assignment.DriverID == own DriverID``. This is the one helper
    every Self access to an assignment-addressed resource uses. Historical
    assignments of the own Driver are owned; ``current_only`` restricts to the
    effective one. Another Driver's assignment, a missing assignment and a
    cross-company assignment are all the same 403.
    """
    own = await resolve_driver_self(
        company_id, user_id, db, require_assignment=current_only,
    )
    result = await db.execute(
        text("""
            SELECT DriverAssignmentID, DriverID
            FROM core.DriverAssignments
            WHERE DriverAssignmentID = :aid AND CompanyID = :cid
        """),
        {"aid": driver_assignment_id, "cid": company_id},
    )
    target = result.mappings().first()
    if target is None or int(target["driverid"]) != own["driver_id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A DRIVER Self account may only access its own Driver assignments.",
        )
    if current_only and own["assignment"]["driverassignmentid"] != driver_assignment_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the effective current Driver assignment is available for this operation.",
        )
    return {**own, "target_assignment_id": driver_assignment_id}
