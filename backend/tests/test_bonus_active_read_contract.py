"""Contract for the shared Active BonusEvent reader owner."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.payroll import bonus


class _MappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Connection:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def execute(self, statement, params):
        self.calls.append((statement, params))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_active_bonus_reader_uses_caller_connection_and_returns_raw_rows():
    rows = [{"payrollbonuseventid": 31, "amount": Decimal("1.2345")}, {"payrollbonuseventid": 12, "amount": Decimal("2.5000")}]
    db = _Connection(_MappingsResult(rows))

    result = await bonus._load_active_bonus_events(7, 9, db)

    assert result == rows
    assert result is not rows
    assert all(actual is expected for actual, expected in zip(result, rows))
    assert result[0]["amount"] is rows[0]["amount"]
    assert db.calls[0][1] == {"period_id": 7, "company_id": 9}


@pytest.mark.asyncio
async def test_active_bonus_reader_propagates_database_errors():
    error = RuntimeError("query failed")
    db = _Connection(error=error)

    with pytest.raises(RuntimeError, match="query failed") as raised:
        await bonus._load_active_bonus_events(7, 9, db)

    assert raised.value is error
