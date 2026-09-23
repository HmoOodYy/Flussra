"""Focused unit coverage for Phase 2 Payroll Setup policy helpers."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import JSONB

from app.payroll_setup.audit import write_policy_audit
from app.payroll_setup.locks import lock_branches, lock_company, lock_setups
from app.payroll_setup.security import require_policy_permission


class _Result:
    def __init__(self, value=None, rows=None):
        self.value = value
        self.rows = rows or []

    def scalar_one(self):
        return self.value

    def mappings(self):
        return self

    def all(self):
        return self.rows


@pytest.mark.asyncio
async def test_policy_permission_requires_all_company_access_before_driver_and_permission():
    db = AsyncMock()
    with (
        patch("app.payroll_setup.security._check_branch_access", new_callable=AsyncMock) as access,
        patch("app.payroll_setup.security._require_not_driver_role", new_callable=AsyncMock) as driver,
        patch("app.payroll_setup.security._check_permission", new_callable=AsyncMock) as permission,
    ):
        access.return_value = (True, [])
        await require_policy_permission(3, 8, "payroll_setup.manage", db)
        access.assert_awaited_once_with(3, 8, db)
        driver.assert_awaited_once_with(3, 8, db)
        permission.assert_awaited_once_with(3, 8, None, "payroll_setup.manage", db)


@pytest.mark.asyncio
async def test_policy_permission_rejects_branch_scoped_access():
    db = AsyncMock()
    with (
        patch("app.payroll_setup.security._check_branch_access", new_callable=AsyncMock) as access,
        patch("app.payroll_setup.security._require_not_driver_role", new_callable=AsyncMock) as driver,
        patch("app.payroll_setup.security._check_permission", new_callable=AsyncMock) as permission,
    ):
        access.return_value = (False, [11])
        with pytest.raises(HTTPException) as error:
            await require_policy_permission(3, 8, "payroll_setup.assign", db)
        assert error.value.status_code == 403
        driver.assert_not_awaited()
        permission.assert_not_awaited()


@pytest.mark.asyncio
async def test_lock_helpers_sort_and_deduplicate_ids():
    db = AsyncMock()
    await lock_company(4, db)
    assert db.execute.await_args_list[0].args[1] == {"cid": 4}

    db.execute.reset_mock()
    await lock_setups(4, [9, 2, 9], db)
    assert [call.args[1]["sid"] for call in db.execute.await_args_list] == [2, 9]

    with patch("app.payroll_setup.locks._acquire_branch_workflow_lock", new_callable=AsyncMock) as lock:
        await lock_branches(4, [7, 3, 7], db)
        assert [call.args[1] for call in lock.await_args_list] == [3, 7]


@pytest.mark.asyncio
async def test_audit_inserts_event_jsonb_and_exact_distinct_branch_set():
    db = AsyncMock()
    db.execute.side_effect = [_Result(81), _Result()]
    event_id = await write_policy_audit(
        db,
        company_id=4,
        actor_user_id=8,
        event_type="SetupMetadataChanged",
        payroll_setup_id=12,
        old_state={"name": "Old"},
        new_state={"name": "New"},
        affected_branch_ids=[9, 3, 9],
    )
    assert event_id == 81
    event_stmt = db.execute.await_args_list[0].args[0]
    assert isinstance(event_stmt._bindparams["old_state"].type, JSONB)
    assert db.execute.await_args_list[0].args[1]["new_state"] == {"name": "New"}
    assert db.execute.await_args_list[1].args[1] == [
        {"event_id": 81, "company_id": 4, "branch_id": 3},
        {"event_id": 81, "company_id": 4, "branch_id": 9},
    ]


@pytest.mark.asyncio
async def test_audit_without_affected_branches_emits_no_child_insert():
    db = AsyncMock()
    db.execute.return_value = _Result(82)
    assert await write_policy_audit(
        db, company_id=4, actor_user_id=8, event_type="DefaultChanged"
    ) == 82
    db.execute.assert_awaited_once()
