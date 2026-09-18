"""
Period-day calendar validation — shared WorkDate/calendar-boundary check
against an existing payroll period.

Extracted from app.payroll.service (Stage B4-13B) as a dependency-closed leaf
module — no behavior change, pure relocation.

_validate_period_work_date is a read/validate accessor: it verifies a
work_date against a period's StartDate/EndDate bounds and, when
PayrollPeriodDays rows exist for the period (post-0052 periods), also
verifies the work_date appears in that day snapshot. It performs no writes,
acquires no locks, and infers no missing period days.

B4-4A ownership history: this helper was briefly moved into
app.payroll.period_creation and returned, because that module only ever
*writes* PayrollPeriodDays once (via its own _create_period_day_rows) and
has no caller of this accessor inside its own logic. It is NOT owned by
period_creation.py — that module owns creation/writing of period structures,
not shared validation against an existing period/day calendar. It remains
consumed by Draft-line CRUD (add_draft_line) and Day Grid (get_day_grid,
save_day_grid), both of which stay in app.payroll.service in this unit, and
by app.payroll.off_drivers (get_selected_day_off_drivers), which now imports
it directly rather than through the service.py facade.
"""
from datetime import date

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


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
