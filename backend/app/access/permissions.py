"""Access-owned permission checks for company and branch operations."""
from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.policy import is_driver_self_subject, require_non_driver_subject


async def check_permission(
    company_id: int,
    user_id: int,
    branch_id: int | None,
    permission_code: str,
    db: AsyncConnection,
) -> None:
    """
    Raise HTTP 403 if the user does not hold *permission_code* for the
    given company + branch.

    Delegates to ``sec.fn_UserHasPermission`` which resolves both
    ``SpecificBranch`` and ``AllCompanyBranches`` scope transparently.

    branch_id may be None for company-level operations (settings, admin
    writes).  Passing None means only AllCompanyBranches assignments will
    match — SpecificBranch users are correctly denied.

    This is a second security gate applied AFTER branch-access has been
    confirmed.  Branch access (``check_branch_access``) answers "can the
    user see data in this branch?" — this function answers "is the user
    allowed to perform this specific action?"
    """
    await require_non_driver_subject(company_id, user_id, db)
    result = await db.execute(
        text("SELECT sec.fn_UserHasPermission(:uid, :cid, :bid, :perm)"),
        {
            "uid":  user_id,
            "cid":  company_id,
            "bid":  branch_id,
            "perm": permission_code,
        },
    )
    if not result.scalar_one():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You do not have permission to perform this action "
                f"(required: {permission_code})."
            ),
        )


async def check_any_permission(
    company_id: int,
    user_id: int,
    branch_id: int | None,
    permission_codes: list[str],
    db: AsyncConnection,
) -> None:
    """
    Raise HTTP 403 if the user holds NONE of the given permission codes.

    Accepts a list and passes when the user has at least one.
    Use this when an action is reachable via multiple roles
    (e.g. payrates.view OR payrates.edit OR settings.manage).
    """
    await require_non_driver_subject(company_id, user_id, db)
    for code in permission_codes:
        result = await db.execute(
            text("SELECT sec.fn_UserHasPermission(:uid, :cid, :bid, :perm)"),
            {"uid": user_id, "cid": company_id, "bid": branch_id, "perm": code},
        )
        if result.scalar_one():
            return  # user has at least one — grant access

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "You do not have permission to perform this action "
            f"(required one of: {', '.join(permission_codes)})."
        ),
    )


async def has_any_permission(
    company_id: int,
    user_id: int,
    branch_id: int | None,
    permission_codes: list[str],
    db: AsyncConnection,
) -> bool:
    """Return True if the user holds at least one of the given permission codes; False otherwise.

    Unlike check_any_permission this never raises — callers decide what to do.
    """
    if await is_driver_self_subject(company_id, user_id, db):
        return False
    for code in permission_codes:
        result = await db.execute(
            text("SELECT sec.fn_UserHasPermission(:uid, :cid, :bid, :perm)"),
            {"uid": user_id, "cid": company_id, "bid": branch_id, "perm": code},
        )
        if result.scalar_one():
            return True
    return False
