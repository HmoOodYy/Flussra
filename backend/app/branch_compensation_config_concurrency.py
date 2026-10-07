"""Branch-level authority for compensation configuration snapshots.

A payroll period freezes the Branch's whole PayDefinition layout in one snapshot. The
layout is a function of every BranchPayItemConfig version of the Branch, so the writers
of that configuration and the snapshot itself must be ordered against each other as
whole units, not row by row.

``lock_branch_compensation_config`` takes one transaction-scoped advisory lock per
Company + Branch. It is shared by:

- canonical period creation (the layout snapshot);
- every Branch applicability writer (single, bulk, and activation of a newly approved
  PayDefinition).

READ COMMITTED snapshots (and the Branch workflow lock's own re-reads after a wait)
rule out REPEATABLE READ for the creator, so this lock is what gives the snapshot a
single coherent state: a configuration change is wholly before or wholly after it.

Global lock order (every path acquires in this order and never against it):

  1. Branch workflow lock (period creation, lifecycle, review)
  2. Branch compensation-config lock(s) -- bulk writers in ascending BranchID order
  3. RateDefinition structure locks -- ascending RateDefinitionID
  4. PayDefinition row lock (FOR SHARE by config writers, FOR UPDATE by retirement)
  5. per Company+Branch+PayDefinition config advisory lock, then config rows
  6. assignment rows

Retirement takes only (3) then (4). Assignment authoring takes only (3) then (6).
Config writers never take (1) or (3). No path takes (2) after (3) or (4), so the
graph is acyclic.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def lock_branch_compensation_config(
    company_id: int, branch_id: int, db: AsyncConnection,
) -> None:
    """Take the Branch compensation-config lock until the transaction ends.

    Idempotent within one transaction.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"branch-compensation-config:{company_id}:{branch_id}"},
    )
