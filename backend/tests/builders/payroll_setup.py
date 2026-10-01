"""Builders for valid Payroll Setup policy state used by backend tests."""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll_setup.payroll_policy import (
    assign_setup,
    create_draft,
    create_setup,
    publish_version,
)


async def create_published_setup_assignment(
    db: AsyncConnection,
    company_id: int,
    user_id: int,
    branch_id: int,
    *,
    setup_code: str,
    setup_name: str,
    payroll_frequency: str,
    anchor_start_date: date,
    normal_days_off_mask: int = 0,
    custom_interval_days: int | None = None,
) -> tuple[int, int, int]:
    """Create, publish, and assign one setup using the current policy service."""
    setup_id = await create_setup(
        company_id, user_id, setup_code, setup_name, db,
    )
    draft_id = await create_draft(
        company_id,
        user_id,
        setup_id,
        db,
        payroll_frequency=payroll_frequency,
        anchor_start_date=anchor_start_date,
        custom_interval_days=custom_interval_days,
        normal_days_off_mask=normal_days_off_mask,
    )
    version_id = await publish_version(
        company_id, user_id, setup_id, draft_id, anchor_start_date, db,
    )
    assignment_id = await assign_setup(
        company_id, user_id, branch_id, setup_id, anchor_start_date, db,
    )
    return setup_id, version_id, assignment_id
