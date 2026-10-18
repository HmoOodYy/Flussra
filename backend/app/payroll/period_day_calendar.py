"""
Validate a WorkDate against period bounds and any frozen PayrollPeriodDays.

Validation performs no writes, takes no locks, and does not infer missing day rows.
"""
from datetime import date

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.business_clock import branch_today


async def resolve_period_work_date(
    company_id: int,
    branch_id: int,
    start_date: date,
    end_date: date,
    requested: date | None,
    db: AsyncConnection,
) -> date:
    """Resolve an omitted work date from the owning Branch's canonical clock."""
    if requested is not None:
        return requested
    today = await branch_today(company_id, branch_id, db)
    return today if start_date <= today <= end_date else start_date


async def _validate_period_work_date(
    period_id: int,
    work_date: date,
    start_date: date,
    end_date: date,
    db: AsyncConnection,
) -> None:
    """
    Validate that work_date is valid for a period.

    1. Existing StartDate/EndDate bounds check (always applied).
    2. If PayrollPeriodDays rows exist for the period, work_date must appear
       in the snapshot. Missing dates are rejected (400).
    3. Configured-off days (IsConfiguredOffDay=TRUE) are NOT rejected in CP-2B
       — IsScheduledWorkDay is metadata-only in this version.

    Legacy periods (created before 0052, no day rows) fall back to step 1 only.
    """
    if not (start_date <= work_date <= end_date):
        raise HTTPException(
            status_code=400,
            detail=(
                f"work_date {work_date} is outside the period range "
                f"({start_date} to {end_date})."
            ),
        )

    has_snapshot = (await db.execute(
        text("SELECT 1 FROM payroll.PayrollPeriodDays WHERE payrollperiodid = :pid LIMIT 1"),
        {"pid": period_id},
    )).first()

    if has_snapshot is not None:
        day_row = (await db.execute(
            text("""
                SELECT 1 FROM payroll.PayrollPeriodDays
                WHERE payrollperiodid = :pid AND workdate = :dt
            """),
            {"pid": period_id, "dt": work_date},
        )).first()
        if day_row is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"work_date {work_date} is not in the period day snapshot. "
                    "The requested date was not part of this period's calendar."
                ),
            )
