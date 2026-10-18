"""Builders for company and branch state used by backend tests."""

from sqlalchemy.ext.asyncio import AsyncConnection

from app.settings.branches import create_branch as create_branch_service
from app.settings.schemas import BranchCreate


async def create_branch(
    db: AsyncConnection,
    company_id: int,
    user_id: int,
    *,
    branch_code: str,
    branch_name: str,
    time_zone_id: str = "UTC",
) -> int:
    """Create an active, non-default branch through the settings service."""
    branch = await create_branch_service(
        company_id,
        user_id,
        BranchCreate(
            branch_code=branch_code,
            branch_name=branch_name,
                time_zone_id=time_zone_id,
            status="Active",
            is_default=False,
        ),
        db,
    )
    return branch.branch_id
