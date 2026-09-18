"""
Shared branch-level payroll workflow advisory lock.

Extracted from app.payroll.service (Stage B4-4A ownership correction) as a
dependency-closed leaf module — no behavior change, pure relocation.

_acquire_branch_workflow_lock has no period-creation-specific logic; it is a
generic branch-level pg_advisory_xact_lock primitive used to serialize
period-workflow-mutating operations. It was briefly kept in
app.payroll.period_creation only because create_period_from_candidate had a
structural (not architectural) need for it and that module must not import
app.payroll.service. It is genuinely shared across Period Creation
(app.payroll.period_creation), Lifecycle and Finalization (app.payroll.service:
create_period, change_period_status, resubmit_period, finalize_period), and
Review (app.review.service) — none of those domains is more entitled to own
it than the others, so it now lives in this small neutral module instead.

Do not add unrelated helpers here. This is not a general utilities module.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def _acquire_branch_workflow_lock(
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> None:
    """
    Acquire a transaction-level advisory lock for branch period creation.
    Released automatically when the transaction commits or rolls back.
    Idempotent within the same transaction.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
        {"cid": company_id, "bid": branch_id},
    )
