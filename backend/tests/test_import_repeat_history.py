from __future__ import annotations

import pytest

from app.imports.batch_service import _completed_receipt_snapshot
from app.imports.errors import ImportFailure
from app.imports.policy import ImportResourcePolicy
from app.imports.semantic_identity import SEMANTIC_INPUT_VERSION


class _Rows:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one(self):
        return self.value

    def all(self):
        return self.value


class _SnapshotDb:
    def __init__(self, bounds, rows):
        self.bounds = bounds
        self.rows = rows
        self.calls = 0

    async def execute(self, _statement, _params):
        self.calls += 1
        return _Rows(self.bounds if self.calls == 1 else self.rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("bounds,policy,expected_calls", [
    ({"import_count": 2, "byte_count": 0}, ImportResourcePolicy(max_completed_lookup_imports=1), 1),
    ({"import_count": 1, "byte_count": 9}, ImportResourcePolicy(max_completed_lookup_bytes=8), 1),
])
async def test_completed_receipt_lookup_refuses_bounds_without_truncating(bounds, policy, expected_calls):
    db = _SnapshotDb(bounds, [])
    with pytest.raises(ImportFailure) as caught:
        await _completed_receipt_snapshot(db, company_id=1, branch_id=2, period_id=3, policy=policy)
    assert caught.value.code == "IMPORT_REPEAT_LOOKUP_LIMIT_EXCEEDED"
    assert db.calls == expected_calls


@pytest.mark.asyncio
async def test_completed_receipt_lookup_rejects_declared_unsupported_evidence():
    db = _SnapshotDb({"import_count": 1, "byte_count": 64}, [{
        "importid": 7, "completedbyuserid": 8, "completedatutc": None,
        "receipt": {"version": SEMANTIC_INPUT_VERSION + 1, "targets": []},
    }])
    with pytest.raises(ImportFailure) as caught:
        await _completed_receipt_snapshot(db, company_id=1, branch_id=2, period_id=3,
                                          policy=ImportResourcePolicy())
    assert caught.value.code == "IMPORT_REPEAT_RECEIPT_UNSUPPORTED"
