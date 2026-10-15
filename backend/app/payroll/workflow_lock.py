"""
Transaction-scoped branch advisory lock shared by Payroll workflow and Payroll Setup operations.
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
