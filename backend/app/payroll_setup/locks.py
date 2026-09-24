"""Transaction-scoped lock helpers for Payroll Setup policy mutations.

Acquire locks in the global order: Company, Setup IDs ascending, Branch IDs
ascending. Callers must keep all locks in the same database transaction.
"""

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.workflow_lock import _acquire_branch_workflow_lock


async def lock_company(company_id: int, db: AsyncConnection) -> None:
    """Lock the company row, serializing company-scoped policy changes."""
    await db.execute(
        text("SELECT companyid FROM core.companies WHERE companyid = :cid FOR UPDATE"),
        {"cid": company_id},
    )


async def lock_setups(
    company_id: int,
    setup_ids: Iterable[int],
    db: AsyncConnection,
) -> None:
    """Lock each distinct Setup row in ascending ID order."""
    for setup_id in sorted(set(setup_ids)):
        await db.execute(
            text(
                "SELECT payrollsetupid FROM payroll.payrollsetups "
                "WHERE companyid = :cid AND payrollsetupid = :sid FOR UPDATE"
            ),
            {"cid": company_id, "sid": setup_id},
        )


async def lock_branches(
    company_id: int,
    branch_ids: Iterable[int],
    db: AsyncConnection,
) -> None:
    """Acquire existing branch workflow advisory locks in ascending ID order."""
    for branch_id in sorted(set(branch_ids)):
        await _acquire_branch_workflow_lock(company_id, branch_id, db)
