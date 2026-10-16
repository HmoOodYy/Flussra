"""Authorization helpers for company-wide Payroll Setup policy operations."""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.branch_access import check_branch_access
from app.access.permissions import check_permission
from app.access.policy import require_non_driver_subject


async def require_policy_permission(
    company_id: int,
    user_id: int,
    permission_code: str,
    db: AsyncConnection,
) -> None:
    """Require active all-company access, a non-driver role, and policy permission."""
    can_see_all, _ = await check_branch_access(company_id, user_id, db)
    if not can_see_all:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Payroll Setup policy operations require company-wide branch access.",
        )
    await require_non_driver_subject(company_id, user_id, db)
    await check_permission(company_id, user_id, None, permission_code, db)
