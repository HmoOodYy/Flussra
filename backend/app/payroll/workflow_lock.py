"""
Shared transaction-scoped branch workflow lock for Payroll, Payroll Setup, and Review.

Internal cross-area contract: ``acquire_branch_workflow_lock(company_id, branch_id, db)``
takes the advisory transaction lock keyed by company and branch on the caller's
active ``AsyncConnection`` transaction. It does not authorize callers or start,
commit, or roll back their transaction; PostgreSQL releases the lock at its end.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def acquire_branch_workflow_lock(
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> None:
    """
    Take the transaction-level advisory lock for one company and branch workflow.
    Released automatically when the caller's transaction commits or rolls back.
    Idempotent within the same transaction.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:cid, :bid)"),
        {"cid": company_id, "bid": branch_id},
    )
