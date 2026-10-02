"""Resource-aware Access policy for the current DRIVER/Self product surface."""
from datetime import date
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.workforce.effective import resolve_effective_driver_profile


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
                LEFT JOIN sec.Roles r ON r.RoleID = ubr.RoleID
                WHERE ubr.UserID = :uid
                  AND ubr.CompanyID = :cid
                  AND ubr.IsActive
                  AND (ubr.ScopeType = 'Self'
                       OR cr.RoleCode = 'DRIVER'
                       OR r.RoleCode = 'DRIVER')
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


async def resolve_driver_self_profile(
    company_id: int,
    user_id: int,
    operation_date: date,
    db: AsyncConnection,
    *,
    target_driver_id: int | None = None,
) -> dict[str, Any]:
    """Resolve and optionally match the linked Employee's effective Driver profile."""
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
                  LEFT JOIN sec.Roles r ON r.RoleID = ubr.RoleID
                  WHERE ubr.UserID = u.UserID
                    AND ubr.CompanyID = u.CompanyID
                    AND ubr.IsActive
                    AND ubr.ScopeType = 'Self'
                    AND (cr.RoleCode = 'DRIVER' OR r.RoleCode = 'DRIVER')
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

    profile = await resolve_effective_driver_profile(
        company_id, int(row["employeeid"]), operation_date, db
    )
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No effective current Driver profile is available for this operation.",
        )

    own_driver_id = int(profile["driverid"])
    if target_driver_id is not None and own_driver_id != target_driver_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A DRIVER Self account may only act on its own effective Driver profile.",
        )
    return profile