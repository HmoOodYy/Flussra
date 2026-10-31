"""Admin access gates for company-scoped operations."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def ensure_scope(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> None:
    """
    Gate 1/2: user/company active + AllCompanyBranches scope.

    Catches stale JWTs for deactivated users, revoked roles, or suspended
    companies without relying on the app.vw_UserBranchAccess view.
    """
    result = await db.execute(
        text("""
            SELECT 1
            FROM   sec.userbranchroles ubr
            JOIN   sec.users           u ON u.userid    = ubr.userid
            JOIN   core.companies      c ON c.companyid = ubr.companyid
            WHERE  ubr.userid    = :uid
              AND  ubr.companyid = :cid
              AND  ubr.isactive  = TRUE
              AND  ubr.scopetype = 'AllCompanyBranches'
              AND  u.isactive    = TRUE
              AND  u.canlogin    = TRUE
              AND  c.status      = 'Active'
              AND  c.issuspended = FALSE
        """),
        {"uid": user_id, "cid": company_id},
    )
    if not result.fetchone():
        raise HTTPException(
            status_code=403,
            detail="This action requires company-level (all-branches) access.",
        )


async def ensure_any_permission(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *permission_codes: str,
) -> None:
    """
    Raise 403 if the caller has NONE of the given permission codes.

    Always enforces scope first (via ensure_scope), then checks permissions
    using sec.fn_UserHasPermission — which resolves CompanyRole grants and
    explicit overrides. Returns as soon as one passing code is found.

    Callers pass only the specific codes that authorize the action:

        await ensure_any_permission(cid, uid, db, "users.view", "roles.view")
    """
    await ensure_scope(company_id, user_id, db)
    for code in permission_codes:
        result = await db.execute(
            text("SELECT sec.fn_UserHasPermission(:uid, :cid, NULL, :perm)"),
            {"uid": user_id, "cid": company_id, "perm": code},
        )
        if result.scalar_one():
            return
    raise HTTPException(
        status_code=403,
        detail=(
            "You do not have permission to perform this action "
            f"(required: one of {list(permission_codes)})."
        ),
    )
