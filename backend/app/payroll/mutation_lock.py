"""
Lock a period row and recheck source-entry editability before mutation.

This FOR UPDATE guard is separate from the branch-level workflow advisory lock.
The caller owns the ambient transaction and retains the row lock until it ends.
"""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.schemas import SOURCE_ENTRY_STATUSES


async def _lock_period_for_mutation(
    period_id: int,
    company_id: int,
    db: AsyncConnection,
) -> None:
    """
    Lock the PayrollPeriods row FOR UPDATE and recheck SOURCE_ENTRY_STATUSES
    (Draft, Open, or Returned).

    Must be called immediately before the first DML write in every source
    mutation path.  The lock ensures that a concurrent status transition
    cannot commit after this mutation has already passed the upfront status
    check but before it writes.

    Raises HTTP 409 Conflict when the period is missing or no longer source-editable.
    """
    result = await db.execute(
        text(
            "SELECT status FROM payroll.payrollperiods "
            "WHERE payrollperiodid = :pid AND companyid = :cid "
            "FOR UPDATE"
        ),
        {"pid": period_id, "cid": company_id},
    )
    row = result.mappings().first()
    current_status = row["status"] if row else "unknown"
    if current_status not in SOURCE_ENTRY_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Period is no longer editable (current status: '{current_status}'). "
                "Only Open, Returned, or Prepared (Draft) periods accept source mutations. "
                "The mutation was rejected to preserve payroll data integrity."
            ),
        )
