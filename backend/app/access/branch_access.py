"""Branch access checks shared by Access and operational services."""
from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def check_branch_access(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> tuple[bool, list[int]]:
    """
    Query the user's branch access for this company.

    Returns:
        (can_see_all, branch_ids) where:
            can_see_all = True  → user has AllCompanyBranches scope
            branch_ids         → list of specific branch IDs accessible
                                 (empty when can_see_all is True — not needed)

    Raises 403 if the user has no active access to this company at all,
    or if the account is disabled / the company is suspended.

    Stale-token safety: the view join includes sec.Users and core.Companies,
    so a deactivated user, a user with CanLogin=FALSE, a revoked role
    assignment, or a suspended company will all return 0 rows here,
    causing an immediate 403 regardless of token validity.
    """
    result = await db.execute(
        text("""
            SELECT v.branchid, v.scopetype
            FROM   app.vw_userbranchaccess v
            WHERE  v.userid              = :user_id
              AND  v.companyid           = :company_id
              AND  v.userisactive        = TRUE
              AND  v.canlogin            = TRUE
              AND  v.accessisactive      = TRUE
              AND  v.companystatus       = 'Active'
              AND  v.companyissuspended  = FALSE
        """),
        {"user_id": user_id, "company_id": company_id},
    )
    rows = result.mappings().all()

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No branch access for this company.",
        )

    can_see_all = any(r["scopetype"] == "AllCompanyBranches" for r in rows)
    branch_ids = (
        []
        if can_see_all
        else [r["branchid"] for r in rows if r["branchid"] is not None]
    )
    return can_see_all, branch_ids
