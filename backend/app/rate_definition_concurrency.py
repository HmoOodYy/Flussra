"""Row-lock authority for target RateDefinition structure.

Every target compensation writer serializes on the RateDefinitions row, which
is also the home of the single structural lock (StructureLockedAtUtc):

- Pending DriverRateAssignment creation, edit, approval and discard;
- DriverRateValue writes;
- RateComponentDefinition insert, update and delete;
- RateDefinition shape changes and PayDefinition method/input-type changes.

The lock is FOR NO KEY UPDATE and is taken first, before any dependent row.
The database triggers take it themselves, so a direct SQL writer is protected
too; this helper lets an application transaction take the same lock up front,
in the same order, before reading state it is about to rely on.

This is deliberately narrower than the Company row guards in
app.company_concurrency: unrelated RateDefinitions never contend.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def lock_rate_definition_structure(
    rate_definition_id: int, db: AsyncConnection
) -> bool:
    """Lock one RateDefinition until the transaction ends.

    Returns True when its structure is already locked by an authoritative event.
    """
    locked_at = (await db.execute(
        text("SELECT payroll.fn_LockRateDefinitionStructure(:rid)"),
        {"rid": rate_definition_id},
    )).scalar_one()
    return locked_at is not None
