"""Authorization helpers for company-wide Payroll Setup policy operations."""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _check_branch_access,
    _check_permission,
    _require_not_driver_role,
)


async def require_policy_permission(
    company_id: int,
    user_id: int,
    permission_code: str,
    db: AsyncConnection,
) -> None:
    """Require active all-company access, a non-driver role, and policy permission."""
    can_see_all, _ = await _check_branch_access(company_id, user_id, db)
    if not can_see_all:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Payroll Setup policy operations require company-wide branch access.",
        )
    await _require_not_driver_role(company_id, user_id, db)
    await _check_permission(company_id, user_id, None, permission_code, db)
