"""Bounded retry of complete PostgreSQL transactions."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})
MAX_ATTEMPTS = 3
_logger = logging.getLogger(__name__)


def retryable_sqlstate(exc: DBAPIError) -> str | None:
    original = exc.orig
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


def is_retryable_transaction_failure(exc: DBAPIError) -> bool:
    """Return whether PostgreSQL says the entire transaction may be retried."""
    return retryable_sqlstate(exc) in _RETRYABLE_SQLSTATES


async def run_retryable_transaction[T](
    engine: AsyncEngine,
    operation: Callable[[AsyncConnection], Awaitable[T]],
    *,
    operation_name: str,
    max_attempts: int = MAX_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Re-run the whole operation with a new transaction after 40001/40P01."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    for attempt in range(1, max_attempts + 1):
        try:
            async with engine.begin() as db:
                return await operation(db)
        except DBAPIError as exc:
            state = retryable_sqlstate(exc)
            if not is_retryable_transaction_failure(exc):
                raise
            _logger.info(
                "Retrying %s transaction after SQLSTATE %s (attempt %s/%s)",
                operation_name, state, attempt, max_attempts,
            )
            if attempt == max_attempts:
                raise HTTPException(
                    status_code=409,
                    detail="Payroll changed concurrently. Refresh and retry the submission.",
                ) from exc
            await sleep(0.01 * (2 ** (attempt - 1)) + random.uniform(0, 0.005))
    raise AssertionError("unreachable")
