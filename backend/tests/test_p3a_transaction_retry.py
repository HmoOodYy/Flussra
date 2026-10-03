"""Whole-transaction retry policy for P3a Submit and Resubmit."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from app.db.transaction_retry import run_retryable_transaction


class SqlstateError(Exception):
    def __init__(self, sqlstate: str):
        self.sqlstate = sqlstate
        super().__init__(sqlstate)


def failure(state: str) -> DBAPIError:
    return DBAPIError("SELECT 1", {}, SqlstateError(state))


class FakeEngine:
    def __init__(self, commit_failures: list[str] | None = None):
        self.attempts = 0
        self.connections = []
        self.commit_failures = commit_failures or []

    def begin(self):
        engine = self

        class Transaction:
            async def __aenter__(self):
                engine.attempts += 1
                self.connection = SimpleNamespace(attempt=engine.attempts)
                engine.connections.append(self.connection)
                return self.connection

            async def __aexit__(self, exc_type, _exc, _tb):
                if exc_type is None and engine.commit_failures:
                    raise failure(engine.commit_failures.pop(0))
                return False

        return Transaction()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["40001", "40P01"])
async def test_retries_whole_operation_in_fresh_transaction(state):
    engine = FakeEngine()
    seen = []
    delays = []

    async def operation(db):
        seen.append(db)
        if len(seen) < 3:
            raise failure(state)
        return "committed"

    async def backoff(seconds):
        delays.append(seconds)

    result = await run_retryable_transaction(engine, operation, operation_name="submit", sleep=backoff)
    assert result == "committed"
    assert engine.attempts == 3
    assert len({id(connection) for connection in seen}) == 3
    assert len(delays) == 2
    assert all(0 < seconds < 0.1 for seconds in delays)


@pytest.mark.asyncio
async def test_retry_includes_commit_failure():
    engine = FakeEngine(commit_failures=["40001"])
    seen = []

    async def operation(db):
        seen.append(db.attempt)
        return db.attempt

    async def no_delay(_seconds):
        return None

    result = await run_retryable_transaction(engine, operation, operation_name="resubmit", sleep=no_delay)
    assert result == 2
    assert seen == [1, 2]


@pytest.mark.asyncio
async def test_exhaustion_returns_friendly_409_after_exactly_three_attempts():
    engine = FakeEngine()

    async def operation(_db):
        raise failure("40001")

    async def no_delay(_seconds):
        return None

    with pytest.raises(HTTPException) as exc:
        await run_retryable_transaction(engine, operation, operation_name="submit", sleep=no_delay)
    assert exc.value.status_code == 409
    assert "Payroll changed concurrently" in exc.value.detail
    assert engine.attempts == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [HTTPException(status_code=422, detail="business"), failure("23505")])
async def test_non_retryable_failure_executes_once(error):
    engine = FakeEngine()

    async def operation(_db):
        raise error

    with pytest.raises(type(error)):
        await run_retryable_transaction(engine, operation, operation_name="submit")
    assert engine.attempts == 1
