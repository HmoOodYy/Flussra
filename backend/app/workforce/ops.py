"""Transaction-local workforce operation marker read by the DriverAssignment history trigger."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

WORKFORCE_OPERATIONS = frozenset({"terminate", "transfer_cancel", "transfer_reschedule"})


async def decision_instant(db: AsyncConnection) -> datetime:
    """The one wall-clock instant an operation uses for every future/past decision.

    ``NOW()`` is the transaction start: an operation that waited on the Employee,
    request, Branch or period lock would judge a boundary that has passed meanwhile as
    still in the future. Read this once, after every lock is held, and use it for every
    decision of the operation. The DriverAssignment history trigger reads the clock
    itself later, so the database is at least as strict as this decision.
    """
    return (await db.execute(text("SELECT clock_timestamp()"))).scalar_one()


@asynccontextmanager
async def workforce_operation(db: AsyncConnection, operation: str) -> AsyncIterator[None]:
    """Mark the caller's transaction with one named boundary-changing operation.

    The GUC is transaction-local (``set_config(..., TRUE)``) and is cleared again
    after a successful block so later statements of the same transaction never
    inherit it. A failing block aborts the transaction, which discards the setting.
    """
    if operation not in WORKFORCE_OPERATIONS:
        raise ValueError(f"Unknown workforce operation: {operation}")
    await db.execute(
        text("SELECT set_config('flussra.workforce_op', :operation, TRUE)"),
        {"operation": operation},
    )
    yield
    await db.execute(text("SELECT set_config('flussra.workforce_op', '', TRUE)"))
