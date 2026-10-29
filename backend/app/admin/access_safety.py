"""Safety rules for who may change whose access.

These rules sit on top of the ordinary admin permission checks:

- nobody changes their own role assignment or extra permissions;
- nobody grants a permission they do not hold themselves (the Company Owner holds
  every permission, so the ceiling never binds them);
- nobody edits the permissions of a role they currently hold;
- a terminated Employee is never given sign-in or access again.

Refusals carry a stable ``code`` next to a plain ``message`` so the UI can show its own wording.
"""

from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import has_any_permission

SELF_ACCESS_CHANGE_FORBIDDEN = "SELF_ACCESS_CHANGE_FORBIDDEN"
GRANT_CEILING_EXCEEDED = "GRANT_CEILING_EXCEEDED"
OWN_ROLE_PERMISSIONS_EDIT_FORBIDDEN = "OWN_ROLE_PERMISSIONS_EDIT_FORBIDDEN"
EMPLOYEE_TERMINATED = "EMPLOYEE_TERMINATED"
COMPANY_OWNER_TERMINATION_FORBIDDEN = "COMPANY_OWNER_TERMINATION_FORBIDDEN"
OWN_EMPLOYEE_TERMINATION_FORBIDDEN = "OWN_EMPLOYEE_TERMINATION_FORBIDDEN"


def access_error(status_code: int, code: str, message: str, **extra: object) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, **extra},
    )


def forbid_self_access_change(caller_id: int, target_user_id: int) -> None:
    """Someone else must change a person's role assignment or extra permissions."""
    if caller_id == target_user_id:
        raise access_error(
            403,
            SELF_ACCESS_CHANGE_FORBIDDEN,
            "You cannot change your own role or extra permissions. "
            "Someone else needs to change your access.",
        )


async def is_company_owner(company_id: int, user_id: int, db: AsyncConnection) -> bool:
    """Whether the user holds the active company-wide Company Owner assignment."""
    result = await db.execute(
        text("""
            SELECT EXISTS (
                SELECT 1
                FROM sec.UserBranchRoles ubr
                JOIN sec.CompanyRoles cr ON cr.CompanyRoleID = ubr.CompanyRoleID
                WHERE ubr.UserID = :uid AND ubr.CompanyID = :cid
                  AND ubr.IsActive
                  AND ubr.ScopeType = 'AllCompanyBranches' AND ubr.BranchID IS NULL
                  AND cr.RoleCode = 'COMPANY_OWNER'
            )
        """),
        {"uid": user_id, "cid": company_id},
    )
    return bool(result.scalar_one())


async def ensure_can_grant(
    company_id: int,
    caller_id: int,
    permission_codes: Iterable[str],
    db: AsyncConnection,
) -> None:
    """Refuse unless the caller holds, company-wide, every permission code being granted."""
    codes = sorted(set(permission_codes))
    if not codes or await is_company_owner(company_id, caller_id, db):
        return
    missing = [
        code for code in codes
        if not await has_any_permission(company_id, caller_id, None, [code], db)
    ]
    if missing:
        raise access_error(
            403,
            GRANT_CEILING_EXCEEDED,
            "You can only grant permissions you hold yourself. "
            f"You do not hold: {', '.join(missing)}.",
            permission_codes=missing,
        )


async def role_permission_codes(company_role_id: int, db: AsyncConnection) -> set[str]:
    result = await db.execute(
        text("SELECT PermissionCode FROM sec.CompanyRolePermissions WHERE CompanyRoleID = :rid"),
        {"rid": company_role_id},
    )
    return {row["permissioncode"] for row in result.mappings().all()}


async def ensure_can_assign_role(
    company_id: int,
    caller_id: int,
    company_role_id: int,
    db: AsyncConnection,
) -> None:
    """Assigning a role grants every permission in it, so the caller must hold them all."""
    await ensure_can_grant(
        company_id, caller_id, await role_permission_codes(company_role_id, db), db,
    )


async def ensure_not_editing_held_role(
    company_id: int,
    caller_id: int,
    company_role_id: int,
    db: AsyncConnection,
) -> None:
    """Only the Company Owner may change the permissions of a role they themselves hold."""
    if await is_company_owner(company_id, caller_id, db):
        return
    result = await db.execute(
        text("""
            SELECT EXISTS (
                SELECT 1 FROM sec.UserBranchRoles
                WHERE UserID = :uid AND CompanyID = :cid
                  AND CompanyRoleID = :rid AND IsActive
            )
        """),
        {"uid": caller_id, "cid": company_id, "rid": company_role_id},
    )
    if result.scalar_one():
        raise access_error(
            403,
            OWN_ROLE_PERMISSIONS_EDIT_FORBIDDEN,
            "You hold this role, so you cannot change its permissions. "
            "Someone else needs to change them.",
        )


async def ensure_employee_not_terminated(
    company_id: int,
    employee_id: int | None,
    db: AsyncConnection,
) -> None:
    """A terminated Employee cannot be linked to, or given sign-in or access through, an account."""
    if employee_id is None:
        return
    result = await db.execute(
        text("""
            SELECT EmploymentStatus FROM core.Employees
            WHERE EmployeeID = :eid AND CompanyID = :cid
        """),
        {"eid": employee_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is not None and row["employmentstatus"] == "Terminated":
        raise access_error(
            422,
            EMPLOYEE_TERMINATED,
            "This Employee has been terminated and cannot be given sign-in or access.",
        )
