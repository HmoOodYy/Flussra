"""Settings company-scope and branch-creator authorization gates."""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_any_permission, check_permission
from app.access.policy import require_non_driver_subject
from app.admin.security import ADMIN_FALLBACKS


async def ensure_company_admin(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> None:
    """
    Raise HTTP 403 unless the user has AllCompanyBranches scope AND a
    company-level admin permission (``settings.manage`` or ``setup.manage``,
    the same ``ADMIN_FALLBACKS`` authority used by the admin routes).

    Two gates:
      1. Scope gate — check_branch_access raises 403 if no access at all;
         then we verify the scope is company-wide, not just a single branch.
      2. Permission gate — check_any_permission verifies that at least one of
         the admin permissions is granted.
         branch_id=None means only AllCompanyBranches assignments are matched,
         so SpecificBranch users cannot satisfy this even if they somehow pass
         gate 1 (they won't, but the defense-in-depth is intentional).
    """
    can_see_all, _ = await check_branch_access(company_id, user_id, db)
    if not can_see_all:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires company-level (all-branches) access.",
        )
    await check_any_permission(company_id, user_id, None, list(ADMIN_FALLBACKS), db)


async def ensure_branch_creator(
    company_id: int, user_id: int, db: AsyncConnection,
    *, with_payroll_start: bool, with_default: bool,
) -> None:
    """branches.create (company-wide, non-driver); payroll_setup.assign when a first payroll start date is sent; settings-admin (settings.manage or setup.manage via ensure_company_admin) when the new branch is to become the Company default."""
    can_see_all, _ = await check_branch_access(company_id, user_id, db)
    if not can_see_all:
        raise HTTPException(status_code=403, detail="This action requires company-level (all-branches) access.")
    await require_non_driver_subject(company_id, user_id, db)
    await check_permission(company_id, user_id, None, "branches.create", db)
    if with_payroll_start:
        await check_permission(company_id, user_id, None, "payroll_setup.assign", db)
    if with_default:
        await ensure_company_admin(company_id, user_id, db)
