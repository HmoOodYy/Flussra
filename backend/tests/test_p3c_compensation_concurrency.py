"""Real PostgreSQL race coverage for target Compensation authoring."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.compensation import assignments
from app.compensation.schemas import (
    AssignmentCreate,
    AssignmentValueInput,
    AssignmentValuesReplace,
    AssignmentVoid,
)
from tests.p3b_fixtures import p3b_cursor, p3b_database  # noqa: F401 - register fixtures
from tests.p3c_fixtures import (  # noqa: F401 - register fixtures
    approve_assignment,
    authored_assignment,
    create_definition,
    create_pending,
    grant_applicability,
    p3c_application,
    p3c_database_engine,
    p3c_http_client,
    p3c_tenant,
    set_scalar_value,
)

pytestmark = pytest.mark.asyncio

BASE = "/compensation/driver-rate-assignments"


def _id(assignment: dict) -> int:
    return assignment["driver_rate_assignment_id"]


async def _run(engine, operation, pids):
    try:
        async with engine.begin() as conn:
            pids.append((await conn.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            return await operation(conn)
    except Exception as exc:  # noqa: BLE001 - the outcome is asserted by the test
        return exc


async def _blocked(engine, operation) -> asyncio.Task:
    """Start a transaction that must wait on a lock held by the caller."""
    task, _ = await _blocked_with_pid(engine, operation)
    return task


async def _blocked_with_pid(engine, operation) -> tuple[asyncio.Task, int]:
    pids: list[int] = []
    task = asyncio.create_task(_run(engine, operation, pids))
    async with engine.connect() as observer:
        for _ in range(250):
            if pids:
                wait = (await observer.execute(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pids[0]})).scalar_one_or_none()
                if wait == "Lock":
                    return task, pids[0]
            if task.done():
                raise AssertionError(f"operation finished instead of waiting: {task.result()!r}")
            await asyncio.sleep(0.02)
    task.cancel()
    raise AssertionError("operation never waited on a lock")


def _values(definition, amount):
    return AssignmentValuesReplace(values=[AssignmentValueInput(
        rate_component_definition_id=definition["components"][0]["rate_component_definition_id"],
        amount=amount)])


async def _filled_pending(client, tenant, definition, amount="10", **kwargs):
    pending = await create_pending(client, tenant, definition, **kwargs)
    assert (await set_scalar_value(client, tenant, pending, definition, amount)).status_code == 200
    return pending


async def _currency(engine, tenant) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(
            text("SELECT currencycode FROM core.companies WHERE companyid = :cid"),
            {"cid": tenant.company_id})).scalar_one()


async def _status(engine, assignment) -> str:
    async with engine.connect() as conn:
        return (await conn.execute(
            text("SELECT status FROM payroll.driverrateassignments "
                 "WHERE driverrateassignmentid = :aid"), {"aid": _id(assignment)})).scalar_one()


def _change_currency(tenant, code):
    async def operation(conn):
        await conn.execute(
            text("UPDATE core.companies SET currencycode = :code WHERE companyid = :cid"),
            {"code": code, "cid": tenant.company_id})
    return operation


def _code(result) -> str:
    assert isinstance(result, HTTPException), result
    return result.detail["code"]


# ---------------------------------------------------------------------------
# Pending creation and approval races
# ---------------------------------------------------------------------------

async def test_concurrent_pending_creation_for_one_identity_admits_one(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)
    body = {"driver_id": tenant.driver_a, "rate_definition_id": definition["rate_definition_id"],
            "effective_from": "2026-01-01"}
    results = await asyncio.gather(
        p3c_client.post(BASE, json=body, headers=tenant.admin),
        p3c_client.post(BASE, json=body, headers=tenant.admin))
    assert sorted(r.status_code for r in results) == [201, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["detail"]["code"] == "PENDING_ASSIGNMENT_EXISTS"


async def test_concurrent_approval_of_one_assignment_admits_one(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled_pending(p3c_client, tenant, definition)
    results = await asyncio.gather(
        approve_assignment(p3c_client, tenant, pending),
        approve_assignment(p3c_client, tenant, pending))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert next(r for r in results if r.status_code == 409).json()["detail"]["code"] \
        == "ASSIGNMENT_NOT_PENDING"


async def test_concurrent_approvals_for_different_drivers_both_commit(p3c_client, tenant):
    definition = await create_definition(p3c_client, tenant)
    first = await _filled_pending(p3c_client, tenant, definition, driver_id=tenant.driver_a)
    second = await _filled_pending(p3c_client, tenant, definition, driver_id=tenant.driver_a2)
    results = await asyncio.gather(
        approve_assignment(p3c_client, tenant, first),
        approve_assignment(p3c_client, tenant, second))
    assert [r.status_code for r in results] == [200, 200]


async def test_value_write_after_a_concurrent_approval_is_refused(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled_pending(p3c_client, tenant, definition, "10")

    async def edit(conn):
        return await assignments.replace_values(
            tenant.company_id, tenant.owner, _id(pending), _values(definition, "99"), conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await assignments.approve(tenant.company_id, tenant.owner, _id(pending), holder)
            edit_task = await _blocked(p3c_engine, edit)
    assert _code(await edit_task) == "ASSIGNMENT_NOT_PENDING"
    final = await p3c_client.get(f"{BASE}/{_id(pending)}", headers=tenant.admin)
    assert final.json()["values"][0]["amount"] == "10.0000"


async def test_discard_racing_a_value_write_never_leaves_orphaned_values(
    p3c_client, p3c_engine, tenant, cur,
):
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled_pending(p3c_client, tenant, definition)

    async def edit(conn):
        return await assignments.replace_values(
            tenant.company_id, tenant.owner, _id(pending), _values(definition, "2"), conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await assignments.discard(tenant.company_id, tenant.owner, _id(pending), holder)
            edit_task = await _blocked(p3c_engine, edit)
    assert _code(await edit_task) == "ASSIGNMENT_NOT_FOUND"
    cur.execute("SELECT count(*) FROM payroll.driverratevalues WHERE driverrateassignmentid = %s",
                (_id(pending),))
    assert cur.fetchone()[0] == 0


async def test_successor_approval_serializes_with_voiding_the_current_schedule(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    current = await authored_assignment(p3c_client, tenant, definition, "25",
                                        effective_from="2026-01-01")
    successor = await _filled_pending(p3c_client, tenant, definition, "30",
                                      effective_from="2026-06-01")

    async def void(conn):
        return await assignments.void(
            tenant.company_id, tenant.owner, _id(current), AssignmentVoid(reason="wrong"), conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await assignments.approve(tenant.company_id, tenant.owner, _id(successor), holder)
            void_task = await _blocked(p3c_engine, void)
    voided = await void_task
    assert not isinstance(voided, Exception), voided
    assert voided.status == "Voided"
    assert voided.effective_to.isoformat() == "2026-05-31"
    assert await _status(p3c_engine, successor) == "Approved"


async def test_unrelated_rate_definitions_do_not_contend(p3c_client, p3c_engine, tenant):
    first = await create_definition(p3c_client, tenant)
    second = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, first, tenant.branch_a)
    grant_applicability(tenant, second, tenant.branch_a)

    async def create_other(conn):
        return await assignments.create_pending(
            tenant.company_id, tenant.owner,
            AssignmentCreate(driver_id=tenant.driver_a,
                             rate_definition_id=second["rate_definition_id"],
                             effective_from="2026-01-01"), conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await assignments.create_pending(
                tenant.company_id, tenant.owner,
                AssignmentCreate(driver_id=tenant.driver_a,
                                 rate_definition_id=first["rate_definition_id"],
                                 effective_from="2026-01-01"), holder)
            pids: list[int] = []
            result = await asyncio.wait_for(_run(p3c_engine, create_other, pids), timeout=5)
    assert not isinstance(result, Exception), result
    assert result.status == "Pending"


async def test_same_definition_drivers_serialize_without_failing(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant)
    grant_applicability(tenant, definition, tenant.branch_a)

    async def create_second(conn):
        return await assignments.create_pending(
            tenant.company_id, tenant.owner,
            AssignmentCreate(driver_id=tenant.driver_a2,
                             rate_definition_id=definition["rate_definition_id"],
                             effective_from="2026-01-01"), conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await assignments.create_pending(
                tenant.company_id, tenant.owner,
                AssignmentCreate(driver_id=tenant.driver_a,
                                 rate_definition_id=definition["rate_definition_id"],
                                 effective_from="2026-01-01"), holder)
            second_task = await _blocked(p3c_engine, create_second)
    second = await second_task
    assert not isinstance(second, Exception), second
    assert second.status == "Pending"


# ---------------------------------------------------------------------------
# Company currency races
# ---------------------------------------------------------------------------

async def test_value_write_holds_the_currency_until_it_commits(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await assignments.replace_values(
                tenant.company_id, tenant.owner, _id(pending), _values(definition, "5"), holder)
            change = await _blocked(p3c_engine, _change_currency(tenant, "EUR"))
            assert await _currency(p3c_engine, tenant) == "USD"
    assert not isinstance(await change, Exception)
    assert await _currency(p3c_engine, tenant) == "EUR"
    assert await _status(p3c_engine, pending) == "Pending"


async def test_currency_change_in_flight_holds_back_a_value_write(p3c_client, p3c_engine, tenant):
    definition = await create_definition(p3c_client, tenant)
    pending = await create_pending(p3c_client, tenant, definition)

    async def write(conn):
        return await assignments.replace_values(
            tenant.company_id, tenant.owner, _id(pending), _values(definition, "5"), conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await _change_currency(tenant, "EUR")(holder)
            write_task = await _blocked(p3c_engine, write)
    assert not isinstance(await write_task, Exception)
    assert await _currency(p3c_engine, tenant) == "EUR"


async def test_approval_blocks_a_concurrent_currency_change_for_good(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled_pending(p3c_client, tenant, definition)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await assignments.approve(tenant.company_id, tenant.owner, _id(pending), holder)
            change = await _blocked(p3c_engine, _change_currency(tenant, "EUR"))
    error = await change
    assert isinstance(error, Exception) and "COMPANY_CURRENCY_CHANGE_BLOCKED" in str(error)
    assert await _currency(p3c_engine, tenant) == "USD"
    assert await _status(p3c_engine, pending) == "Approved"


async def test_currency_change_in_flight_holds_back_an_approval_then_approves_under_it(
    p3c_client, p3c_engine, tenant,
):
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled_pending(p3c_client, tenant, definition)

    async def approve(conn):
        return await assignments.approve(tenant.company_id, tenant.owner, _id(pending), conn)

    async with p3c_engine.connect() as holder:
        async with holder.begin():
            await _change_currency(tenant, "EUR")(holder)
            approval = await _blocked(p3c_engine, approve)
    result = await approval
    assert not isinstance(result, Exception), result
    assert await _currency(p3c_engine, tenant) == "EUR"
    assert await _status(p3c_engine, pending) == "Approved"
    async with p3c_engine.begin() as conn:
        with pytest.raises(Exception, match="COMPANY_CURRENCY_CHANGE_BLOCKED"):
            await _change_currency(tenant, "USD")(conn)


# ---------------------------------------------------------------------------
# Monetary lock acquisition order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("operation_name", ["replace_values", "approve"])
async def test_monetary_mutations_lock_rate_definition_then_company_then_assignment(
    p3c_client, p3c_engine, tenant, operation_name,
):
    """B takes the RateDefinition lock, waits on the Company guard, and has not locked
    the assignment row yet."""
    definition = await create_definition(p3c_client, tenant)
    pending = await _filled_pending(p3c_client, tenant, definition, "10")

    async def operation(conn):
        if operation_name == "approve":
            return await assignments.approve(tenant.company_id, tenant.owner, _id(pending), conn)
        return await assignments.replace_values(
            tenant.company_id, tenant.owner, _id(pending), _values(definition, "11"), conn)

    async with p3c_engine.connect() as company_holder:
        async with company_holder.begin():
            holder_pid = (await company_holder.execute(
                text("SELECT pg_backend_pid()"))).scalar_one()
            await company_holder.execute(
                text("SELECT companyid FROM core.companies WHERE companyid = :cid "
                     "FOR NO KEY UPDATE"), {"cid": tenant.company_id})

            task, waiter_pid = await _blocked_with_pid(p3c_engine, operation)
            async with p3c_engine.connect() as observer:
                blockers = (await observer.execute(
                    text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid})).scalar_one()
            assert blockers == [holder_pid]

            # The assignment row is still free: the operation has not locked it.
            async with p3c_engine.begin() as probe:
                await probe.execute(
                    text("SELECT 1 FROM payroll.driverrateassignments "
                         "WHERE driverrateassignmentid = :aid FOR UPDATE NOWAIT"),
                    {"aid": _id(pending)})

            # The RateDefinition structural lock is already held by the operation.
            with pytest.raises(Exception, match="could not obtain lock"):
                async with p3c_engine.begin() as probe:
                    await probe.execute(
                        text("SELECT 1 FROM payroll.ratedefinitions "
                             "WHERE ratedefinitionid = :rid FOR NO KEY UPDATE NOWAIT"),
                        {"rid": definition["rate_definition_id"]})
    result = await task
    assert not isinstance(result, Exception), result
    expected = "Approved" if operation_name == "approve" else "Pending"
    assert await _status(p3c_engine, pending) == expected
