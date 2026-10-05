"""Row-lock authority for the canonical core.companies row.

Two strengths, deliberately named so they cannot be confused:

- lock_company_for_monetary_use: FOR SHARE. Every transaction whose monetary
  correctness depends on the Company currency staying stable until commit takes
  this. Any number of monetary writers may hold it at once.
- lock_company_for_mutation: FOR NO KEY UPDATE. Taken first by any transaction
  that will mutate the Company row or its currency. It conflicts with every
  monetary SHARE guard and with other mutators, and still allows FK KEY SHARE
  on Company.

Never acquire the monetary guard and then mutate the same Company row in one
transaction: two such transactions would each hold SHARE and deadlock while
upgrading. Mutation paths take lock_company_for_mutation up front instead.

Because these are plain PostgreSQL row locks, a direct SQL UPDATE of
core.companies conflicts with the monetary guard without any application help.

This is not a general lock framework. Branch workflow sequencing
(app.payroll.workflow_lock), Period source mutation
(app.payroll.mutation_lock) and PayItem source writes
(app.payroll.pay_item_write_lock) are separate concurrency domains.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def lock_company_for_monetary_use(company_id: int, db: AsyncConnection) -> None:
    """Shared Company row guard held until the transaction ends."""
    await db.execute(
        text("SELECT companyid FROM core.companies WHERE companyid = :cid FOR SHARE"),
        {"cid": company_id},
    )


async def lock_company_for_mutation(company_id: int, db: AsyncConnection) -> None:
    """Exclusive-strength Company row lock held until the transaction ends."""
    await db.execute(
        text("SELECT companyid FROM core.companies WHERE companyid = :cid FOR NO KEY UPDATE"),
        {"cid": company_id},
    )
