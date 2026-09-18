"""
Period source-mutation status lock.

Extracted from app.payroll.service (Stage B4-5A) as a dependency-closed leaf
module — no behavior change, pure relocation.

_lock_period_for_mutation is a concurrency-safety/status-validity guard: it
acquires a FOR UPDATE row lock on the target period and verifies the period
is still in an editable status before a source mutation (draft line, period
pay line, bonus event, or day-grid save) proceeds. It is distinct from
app.payroll.workflow_lock._acquire_branch_workflow_lock, which is a
branch-level advisory lock used for period-workflow operations (creation,
resubmission, status change, finalization) — a different granularity and a
different concern (branch-wide serialization vs. this period's own
editability). The two must not be merged.

Genuinely shared by four domains that all still live in app.payroll.service —
Draft-line CRUD, Period Pay Lines, Bonus, and Day Grid — none of which is more
entitled to own it than the others. app.payroll.service imports it back via
facade for all of its current callers, which are not being extracted in this
unit.

Do not add unrelated helpers here. This is not a general utilities module.
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
    CP-0A/CP-1A: Lock the PayrollPeriods row FOR UPDATE and verify it is still
    in an editable status (Open or Returned).

    Must be called immediately before the first DML write in every source
    mutation path.  The lock ensures that a concurrent status transition
    cannot commit after this mutation has already passed the upfront status
    check but before it writes.

    Raises HTTP 409 Conflict when the current status is not Open or Returned.
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
    # CP-2F: Draft (Prepared) is also editable for operational source-entry paths.
    # SOURCE_ENTRY_STATUSES = {"Draft", "Open", "Returned"}
    if current_status not in SOURCE_ENTRY_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Period is no longer editable (current status: '{current_status}'). "
                "Only Open, Returned, or Prepared (Draft) periods accept source mutations. "
                "The mutation was rejected to preserve payroll data integrity."
            ),
        )
