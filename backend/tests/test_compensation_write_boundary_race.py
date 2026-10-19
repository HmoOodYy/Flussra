"""A compensation write is decided at the MUTATION instant, not at the guard's instant.

``require_writable_driver_assignment`` refuses what is already ended, but a writer can
still block after it (Branch advisory lock, row lock, unique-index wait) while a
Scheduled source assignment's future ``EffectiveToAt`` passes. ``FOR SHARE`` freezes the
value, not time. The authority is ``core.trg_CompensationAssignmentWritable``: an AFTER
ROW trigger on every compensation table that re-decides with ``clock_timestamp()`` after
the statement's own waits, mapped to HTTP 409 DRIVER_ASSIGNMENT_ENDED in one place.

Race tests (two connections, real writer paths): connection A holds the real lock the
writer waits on; the writer, started through the API, passes the guard while the source is
current and BLOCKS (asserted through ``pg_blocking_pids`` and the waiting statement);
the source's boundary passes; A releases; the writer must be refused and change nothing.
Positive twins run the identical blocked operation with the boundary far in the future.

Writers exercised (blocking point):
  rates.approve_rate    Branch workflow advisory lock  pg_advisory_xact_lock(cid, bid)
  rates.void_rate       Branch workflow advisory lock  pg_advisory_xact_lock(cid, bid)
  assignments.approve   prior Approved row FOR UPDATE
  driver_pay_rules.create  unique-index wait on the INSERT (open-ended rule per type)

The Values and Tiers tables are children whose writers only mutate them after a parent
statement the same transaction already ran; they have no independent blocking point to
hold, so their trigger is covered by the table-level tests below.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.main import create_app
from app.workforce.effective import (
    DRIVER_ASSIGNMENT_ENDED_SQLSTATE,
    is_driver_assignment_ended_error,
)
from tests.builders.transfer_scenarios import (
    auth,
    db_now,
    make_branch,
    make_driver,
    scheduled_transfer,
)
from tests.db_state import DRIVER_ASSIGNMENT_HISTORY_TRIGGER, suspended_test_triggers
from tests.test_ended_assignment_write_guard import (
    ENDED_MESSAGE,
    _columns,
    _definition,
    _expect,
    _fingerprint,
    _pending,
    _rate,
    _rule,
    _values,
)

pytestmark = pytest.mark.asyncio

# The Scheduled source ends this long after the scenario is built: long enough for the
# writer to start, pass the guard and block, short enough to keep the module quick.
ENDS_IN_SECONDS = 6.0
FAR_FUTURE = timedelta(days=3)
ENDED = {"code": "DRIVER_ASSIGNMENT_ENDED", "message": ENDED_MESSAGE}


# ---------------------------------------------------------------------------
# Scenario building and the two-connection race runner
# ---------------------------------------------------------------------------

@dataclass
class Scene:
    admin: str
    source: int
    destination: int
    source_branch: int
    target_branch: int
    boundary: datetime  # the source assignment's EffectiveToAt


async def _driver(client, admin, db):
    source_branch, target_branch = await make_branch(db), await make_branch(db)
    driver = await make_driver(client, admin, source_branch)
    return driver, source_branch, target_branch


async def _schedule(client, admin, db, driver, source_branch, target_branch, *, ends_soon):
    """Schedule the permanent transfer; the source ends in seconds, or far in the future."""
    boundary = await db_now(db) + (
        timedelta(seconds=ENDS_IN_SECONDS) if ends_soon else FAR_FUTURE)
    transfer = await scheduled_transfer(client, admin, driver["driver_id"], target_branch, boundary)
    stored = (await db.execute(text(
        "SELECT EffectiveToAt FROM core.DriverAssignments WHERE DriverAssignmentID = :a"
    ), {"a": transfer["source_id"]})).scalar_one()
    assert stored == boundary
    return Scene(admin, transfer["source_id"], transfer["destination_id"],
                 source_branch, target_branch, boundary)


async def _wait_until_blocked(db, task: asyncio.Task, holder_pid: int) -> str:
    """The statement a backend blocked behind the holder is currently executing."""
    for _ in range(500):
        blocked = (await db.execute(text("""
            SELECT query FROM pg_stat_activity
            WHERE :holder = ANY(pg_blocking_pids(pid))
        """), {"holder": holder_pid})).scalars().all()
        if blocked:
            assert len(blocked) == 1, blocked
            return blocked[0]
        if task.done():
            response = task.result()
            raise AssertionError(
                f"the writer finished instead of blocking: {response.status_code} {response.text}")
        await asyncio.sleep(0.02)
    raise AssertionError("the writer never blocked behind the lock holder")


async def _sleep_past(db, boundary: datetime) -> None:
    while not (await db.execute(
        text("SELECT clock_timestamp() > :b"), {"b": boundary + timedelta(milliseconds=200)}
    )).scalar_one():
        await asyncio.sleep(0.1)


async def _race(engine, db, scene: Scene, *, hold, send, blocked_on: str, ends_soon: bool):
    """Hold a lock on connection A, run the writer on B until it blocks, then release A."""
    holder = await engine.connect()
    task: asyncio.Task | None = None
    try:
        holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        await hold(holder)
        task = asyncio.create_task(send())
        waiting_statement = await _wait_until_blocked(db, task, holder_pid)
        assert blocked_on in waiting_statement, waiting_statement
        # The writer passed the guard because the source is still current right now.
        assert (await db.execute(
            text("SELECT :b > clock_timestamp()"), {"b": scene.boundary})).scalar_one()
        if ends_soon:
            await _sleep_past(db, scene.boundary)
        await holder.rollback()
        return await asyncio.wait_for(task, 30)
    finally:
        if task is not None and not task.done():
            task.cancel()
        await holder.close()


def _assert_refused_at_the_mutation_instant(response):
    assert response.status_code == 409, (response.status_code, response.text)
    assert response.json() == {"detail": ENDED}


async def _advisory(branch_id):
    async def hold(conn):
        await conn.execute(text("SELECT pg_advisory_xact_lock(1, :b)"), {"b": branch_id})
    return hold


# ---------------------------------------------------------------------------
# rates.approve_rate and rates.void_rate (Branch workflow advisory lock)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ends_soon", [True, False], ids=["source-ends-while-blocked", "source-stays-current"])
async def test_approve_rate_blocked_on_the_branch_lock_is_decided_when_it_resumes(
    client, auth_token, direct_db, test_engine, ends_soon,
):
    driver, source_branch, target_branch = await _driver(client, auth_token, direct_db)
    column = (await _columns(client, auth_token, source_branch, 1))[0]
    prior = await _rate(client, auth_token, driver["assignment_id"], column, "2026-01-01")
    await _expect(await client.post(f"/payroll/rates/{prior}/approve", headers=auth(auth_token)), 200)
    pending = await _rate(client, auth_token, driver["assignment_id"], column, "2026-03-01")
    scene = await _schedule(client, auth_token, direct_db, driver, source_branch, target_branch,
                            ends_soon=ends_soon)
    before = await _fingerprint(direct_db, scene.source)

    response = await _race(
        test_engine, direct_db, scene, ends_soon=ends_soon,
        hold=await _advisory(source_branch), blocked_on="pg_advisory_xact_lock",
        send=lambda: client.post(f"/payroll/rates/{pending}/approve", headers=auth(auth_token)))

    states = dict((await direct_db.execute(text(
        "SELECT driverrateid, status FROM payroll.driverrates WHERE driverrateid IN (:p, :q)"
    ), {"p": prior, "q": pending})).all())
    if ends_soon:
        _assert_refused_at_the_mutation_instant(response)
        assert states == {prior: "Approved", pending: "PendingApproval"}
        assert await _fingerprint(direct_db, scene.source) == before
    else:
        await _expect(response, 200)
        assert states == {prior: "Superseded", pending: "Approved"}


@pytest.mark.parametrize("ends_soon", [True, False], ids=["source-ends-while-blocked", "source-stays-current"])
async def test_void_rate_blocked_on_the_branch_lock_is_decided_when_it_resumes(
    client, auth_token, direct_db, test_engine, ends_soon,
):
    driver, source_branch, target_branch = await _driver(client, auth_token, direct_db)
    column = (await _columns(client, auth_token, source_branch, 1))[0]
    approved = await _rate(client, auth_token, driver["assignment_id"], column, "2026-01-01")
    await _expect(await client.post(f"/payroll/rates/{approved}/approve", headers=auth(auth_token)), 200)
    scene = await _schedule(client, auth_token, direct_db, driver, source_branch, target_branch,
                            ends_soon=ends_soon)
    before = await _fingerprint(direct_db, scene.source)

    response = await _race(
        test_engine, direct_db, scene, ends_soon=ends_soon,
        hold=await _advisory(source_branch), blocked_on="pg_advisory_xact_lock",
        send=lambda: client.delete(f"/payroll/rates/{approved}", headers=auth(auth_token)))

    status = (await direct_db.execute(text(
        "SELECT status FROM payroll.driverrates WHERE driverrateid = :r"), {"r": approved})).scalar_one()
    if ends_soon:
        _assert_refused_at_the_mutation_instant(response)
        assert status == "Approved"
        assert await _fingerprint(direct_db, scene.source) == before
    else:
        await _expect(response, 204)
        assert status == "Voided"


# ---------------------------------------------------------------------------
# assignments.approve (row lock on the prior Approved assignment)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ends_soon", [True, False], ids=["source-ends-while-blocked", "source-stays-current"])
async def test_assignment_approve_blocked_on_the_prior_row_lock_is_decided_when_it_resumes(
    client, auth_token, direct_db, test_engine, ends_soon,
):
    driver, source_branch, target_branch = await _driver(client, auth_token, direct_db)
    definition = await _definition(client, auth_token, direct_db, (source_branch, target_branch))
    source = driver["assignment_id"]
    prior = await _pending(client, auth_token, source, definition, "2026-01-01")
    await _expect(await _values(client, auth_token, prior, definition, "20"), 200)
    await _expect(await client.post(
        f"/compensation/driver-rate-assignments/{prior}/approve", headers=auth(auth_token)), 200)
    successor = await _pending(client, auth_token, source, definition, "2026-03-01")
    await _expect(await _values(client, auth_token, successor, definition, "30"), 200)
    scene = await _schedule(client, auth_token, direct_db, driver, source_branch, target_branch,
                            ends_soon=ends_soon)
    before = await _fingerprint(direct_db, scene.source)

    async def hold(conn):
        await conn.execute(text("""
            SELECT 1 FROM payroll.driverrateassignments
            WHERE driverrateassignmentid = :prior FOR UPDATE
        """), {"prior": prior})

    response = await _race(
        test_engine, direct_db, scene, ends_soon=ends_soon, hold=hold, blocked_on="FOR UPDATE",
        send=lambda: client.post(
            f"/compensation/driver-rate-assignments/{successor}/approve", headers=auth(auth_token)))

    states = dict((await direct_db.execute(text("""
        SELECT driverrateassignmentid, status FROM payroll.driverrateassignments
        WHERE driverrateassignmentid IN (:p, :s)
    """), {"p": prior, "s": successor})).all())
    if ends_soon:
        _assert_refused_at_the_mutation_instant(response)
        assert states == {prior: "Approved", successor: "Pending"}
        assert await _fingerprint(direct_db, scene.source) == before
    else:
        await _expect(response, 200)
        assert states == {prior: "Superseded", successor: "Approved"}


# ---------------------------------------------------------------------------
# driver_pay_rules.create (unique-index wait on the INSERT)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ends_soon", [True, False], ids=["source-ends-while-blocked", "source-stays-current"])
async def test_pay_rule_create_blocked_on_a_unique_index_wait_is_decided_when_it_resumes(
    client, auth_token, direct_db, test_engine, ends_soon,
):
    driver, source_branch, target_branch = await _driver(client, auth_token, direct_db)
    source = driver["assignment_id"]
    scene = await _schedule(client, auth_token, direct_db, driver, source_branch, target_branch,
                            ends_soon=ends_soon)
    admin_id = (await direct_db.execute(text(
        "SELECT UserID FROM sec.Users WHERE Username = 'admin'"))).scalar_one()
    before = await _fingerprint(direct_db, scene.source)

    async def hold(conn):
        # An uncommitted open-ended Active MinimumPay rule on the same assignment: the
        # writer's own INSERT waits on it in ux_DriverPayRules_Assignment_Type_OpenEnded.
        await conn.execute(text("""
            INSERT INTO payroll.driverpayrules
                (companyid, branchid, driverassignmentid, ruletype, amount, effectivefrom,
                 effectiveto, status, createdbyuserid)
            VALUES (1, :b, :a, 'MinimumPay', 10, DATE '2026-01-01', NULL, 'Active', :u)
        """), {"b": source_branch, "a": source, "u": admin_id})

    response = await _race(
        test_engine, direct_db, scene, ends_soon=ends_soon, hold=hold,
        blocked_on="INSERT INTO payroll.driverpayrules",
        send=lambda: client.post("/payroll/driver-pay-rules", json={
            "driver_assignment_id": source, "rule_type": "MinimumPay", "amount": "50",
            "effective_from": "2026-01-01", "effective_to": None}, headers=auth(auth_token)))

    count = (await direct_db.execute(text(
        "SELECT COUNT(*) FROM payroll.driverpayrules WHERE driverassignmentid = :a"
    ), {"a": source})).scalar_one()
    if ends_soon:
        _assert_refused_at_the_mutation_instant(response)
        assert count == 0  # the holder rolled back; the refused writer left nothing
        assert await _fingerprint(direct_db, scene.source) == before
    else:
        await _expect(response, 201)
        assert count == 1


# ---------------------------------------------------------------------------
# The trigger itself: every covered table, every operation, three assignment states
# ---------------------------------------------------------------------------

COVERED_TABLES = (
    "driverrateassignments", "driverratevalues", "driverpayrules", "driverrates",
    "driverratetiers",
)


async def _rows(client, admin, db, assignment_id, branch_id, definitions, columns) -> dict:
    """Pending compensation of every kind on one assignment (written while writable)."""
    rows = {
        "ca": await _pending(client, admin, assignment_id, definitions[0]),
        "ca2": await _pending(client, admin, assignment_id, definitions[1]),
        "pr": await _rule(client, admin, assignment_id, "MinimumPay", "2026-01-01", "2026-03-31"),
        "dr": await _rate(client, admin, assignment_id, columns[0]),
    }
    await _expect(await _values(client, admin, rows["ca"], definitions[0]), 200)
    await db.execute(text("""
        INSERT INTO payroll.driverratetiers (driverrateid, tiersequence, fromunit, tounit, tieramount)
        VALUES (:dr, 1, 0, 10, 1)
    """), {"dr": rows["dr"]})
    return rows


def _operations(rows, assignment_id, branch_id, definitions, columns, admin_id):
    """(table, operation, sql, params): inserts, then updates, then deletes children first."""
    d_new, d_second = definitions[2], definitions[1]
    p = {"a": assignment_id, "b": branch_id, "u": admin_id, **rows}
    return [
        ("driverrateassignments", "INSERT", """
            INSERT INTO payroll.driverrateassignments
                (companyid, branchid, driverassignmentid, ratedefinitionid, effectivefrom)
            VALUES (1, :b, :a, :d_new, DATE '2026-01-01')""",
         {**p, "d_new": d_new["rate_definition_id"]}),
        ("driverratevalues", "INSERT", """
            INSERT INTO payroll.driverratevalues
                (driverrateassignmentid, ratedefinitionid, ratecomponentdefinitionid, amount)
            VALUES (:ca2, :d2, :c2, 5)""",
         {**p, "d2": d_second["rate_definition_id"],
          "c2": d_second["components"][0]["rate_component_definition_id"]}),
        ("driverpayrules", "INSERT", """
            INSERT INTO payroll.driverpayrules
                (companyid, branchid, driverassignmentid, ruletype, amount, effectivefrom,
                 effectiveto, status, createdbyuserid)
            VALUES (1, :b, :a, 'MaximumPay', 70, DATE '2026-04-01', DATE '2026-04-30', 'Active', :u)""", p),
        ("driverrates", "INSERT", """
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverassignmentid, ratetypeid, amount, effectivefrom,
                 status, createdbyuserid)
            VALUES (1, :b, :a, :rt, 3, DATE '2026-02-01', 'PendingApproval', :u)""",
         {**p, "rt": columns[1]["rate_type_id"]}),
        ("driverratetiers", "INSERT", """
            INSERT INTO payroll.driverratetiers (driverrateid, tiersequence, fromunit, tounit, tieramount)
            VALUES (:dr, 2, 11, 20, 2)""", p),
        ("driverrateassignments", "UPDATE", """
            UPDATE payroll.driverrateassignments SET notes = 'probe'
            WHERE driverrateassignmentid = :ca""", p),
        ("driverratevalues", "UPDATE", """
            UPDATE payroll.driverratevalues SET amount = 7
            WHERE driverrateassignmentid = :ca""", p),
        ("driverpayrules", "UPDATE", "UPDATE payroll.driverpayrules SET notes = 'probe' WHERE driverpayruleid = :pr", p),
        ("driverrates", "UPDATE", "UPDATE payroll.driverrates SET notes = 'probe' WHERE driverrateid = :dr", p),
        ("driverratetiers", "UPDATE", """
            UPDATE payroll.driverratetiers SET tieramount = 2
            WHERE driverrateid = :dr AND tiersequence = 1""", p),
        ("driverratetiers", "DELETE", "DELETE FROM payroll.driverratetiers WHERE driverrateid = :dr", p),
        ("driverratevalues", "DELETE", "DELETE FROM payroll.driverratevalues WHERE driverrateassignmentid = :ca", p),
        ("driverrates", "DELETE", "DELETE FROM payroll.driverrates WHERE driverrateid = :dr", p),
        ("driverpayrules", "DELETE", "DELETE FROM payroll.driverpayrules WHERE driverpayruleid = :pr", p),
        ("driverrateassignments", "DELETE", "DELETE FROM payroll.driverrateassignments WHERE driverrateassignmentid = :ca", p),
    ]


async def _tier_count(db, rate_id) -> int:
    return (await db.execute(text(
        "SELECT COUNT(*) FROM payroll.driverratetiers WHERE driverrateid = :r"), {"r": rate_id})).scalar_one()


async def _run_operations(db, operations) -> dict[tuple[str, str], str]:
    """Execute each operation; 'ok', 'ended' (SQLSTATE FL001) or the other SQLSTATE."""
    outcomes = {}
    for table, operation, sql, params in operations:
        try:
            result = await db.execute(text(sql), params)
            assert result.rowcount >= 1, (table, operation)
            outcomes[(table, operation)] = "ok"
        except DBAPIError as exc:
            outcomes[(table, operation)] = (
                "ended" if is_driver_assignment_ended_error(exc) else f"other:{exc.orig}")
    return outcomes


async def _world_for_the_trigger(client, admin, db):
    source_branch, target_branch = await make_branch(db), await make_branch(db)
    definitions = [await _definition(client, admin, db, (source_branch, target_branch))
                   for _ in range(3)]
    columns = {source_branch: await _columns(client, admin, source_branch, 2),
               target_branch: await _columns(client, admin, target_branch, 2)}
    admin_id = (await db.execute(text(
        "SELECT UserID FROM sec.Users WHERE Username = 'admin'"))).scalar_one()
    return source_branch, target_branch, definitions, columns, admin_id


async def test_the_trigger_allows_every_operation_on_current_and_pending_assignments(
    client, auth_token, direct_db,
):
    source_branch, target_branch, definitions, columns, admin_id = await _world_for_the_trigger(
        client, auth_token, direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    current = driver["assignment_id"]
    scheduled = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch,
        await db_now(direct_db) + FAR_FUTURE)
    pending = scheduled["destination_id"]

    for assignment_id, branch_id in ((current, source_branch), (pending, target_branch)):
        rows = await _rows(client, auth_token, direct_db, assignment_id, branch_id,
                           definitions, columns[branch_id])
        outcomes = await _run_operations(direct_db, _operations(
            rows, assignment_id, branch_id, definitions, columns[branch_id], admin_id))
        assert set(outcomes.values()) == {"ok"}, outcomes
        assert len(outcomes) == 15


async def test_the_trigger_refuses_every_operation_on_every_table_for_an_ended_assignment(
    client, auth_token, direct_db,
):
    source_branch, _, definitions, columns, admin_id = await _world_for_the_trigger(
        client, auth_token, direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    assignment_id = driver["assignment_id"]
    rows = await _rows(client, auth_token, direct_db, assignment_id, source_branch,
                       definitions, columns[source_branch])
    async with suspended_test_triggers(direct_db, (DRIVER_ASSIGNMENT_HISTORY_TRIGGER,)):
        await direct_db.execute(text("""
            UPDATE core.DriverAssignments
            SET EffectiveToAt = clock_timestamp() - INTERVAL '1 minute', EndReason = 'Terminated'
            WHERE DriverAssignmentID = :a
        """), {"a": assignment_id})
    before, tiers = await _fingerprint(direct_db, assignment_id), await _tier_count(direct_db, rows["dr"])
    assert tiers == 1

    outcomes = await _run_operations(direct_db, _operations(
        rows, assignment_id, source_branch, definitions, columns[source_branch], admin_id))

    assert set(outcomes.values()) == {"ended"}, outcomes
    assert {table for table, _ in outcomes} == set(COVERED_TABLES)
    assert {operation for _, operation in outcomes} == {"INSERT", "UPDATE", "DELETE"}
    assert await _fingerprint(direct_db, assignment_id) == before
    assert await _tier_count(direct_db, rows["dr"]) == tiers


async def test_the_refusal_carries_the_dedicated_sqlstate_and_message(
    client, auth_token, direct_db,
):
    source_branch, _, definitions, columns, admin_id = await _world_for_the_trigger(
        client, auth_token, direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    async with suspended_test_triggers(direct_db, (DRIVER_ASSIGNMENT_HISTORY_TRIGGER,)):
        await direct_db.execute(text("""
            UPDATE core.DriverAssignments
            SET EffectiveToAt = clock_timestamp() - INTERVAL '1 minute', EndReason = 'Terminated'
            WHERE DriverAssignmentID = :a
        """), {"a": driver["assignment_id"]})
    with pytest.raises(DBAPIError) as refused:
        await direct_db.execute(text("""
            INSERT INTO payroll.driverpayrules
                (companyid, branchid, driverassignmentid, ruletype, amount, effectivefrom,
                 status, createdbyuserid)
            VALUES (1, :b, :a, 'MinimumPay', 1, DATE '2026-01-01', 'Active', :u)
        """), {"b": source_branch, "a": driver["assignment_id"], "u": admin_id})
    original = refused.value.orig
    assert (getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)) \
        == DRIVER_ASSIGNMENT_ENDED_SQLSTATE == "FL001"
    assert "DRIVER_ASSIGNMENT_ENDED" in str(original)
    assert is_driver_assignment_ended_error(refused.value)


async def test_the_trigger_decides_at_clock_time_inside_a_long_transaction(
    client, auth_token, direct_db, test_engine,
):
    source_branch, _, definitions, columns, admin_id = await _world_for_the_trigger(
        client, auth_token, direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    assignment_id = driver["assignment_id"]
    rows = await _rows(client, auth_token, direct_db, assignment_id, source_branch,
                       definitions, columns[source_branch])
    async with suspended_test_triggers(direct_db, (DRIVER_ASSIGNMENT_HISTORY_TRIGGER,)):
        await direct_db.execute(text("""
            UPDATE core.DriverAssignments
            SET EffectiveToAt = clock_timestamp() + INTERVAL '3 seconds', EndReason = 'Terminated'
            WHERE DriverAssignmentID = :a
        """), {"a": assignment_id})
    ends_at = (await direct_db.execute(text(
        "SELECT EffectiveToAt FROM core.DriverAssignments WHERE DriverAssignmentID = :a"
    ), {"a": assignment_id})).scalar_one()

    async with test_engine.connect() as conn:
        transaction_start = (await conn.execute(text("SELECT NOW()"))).scalar_one()
        assert transaction_start < ends_at
        # Before the boundary the write is accepted (and undone with the savepoint).
        async with conn.begin_nested() as savepoint:
            await conn.execute(text(
                "UPDATE payroll.driverrates SET notes = 'before' WHERE driverrateid = :dr"), rows)
            await savepoint.rollback()

        while (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one() <= ends_at:
            await asyncio.sleep(0.2)
        # NOW() is frozen at the transaction start and would still call the assignment open.
        assert (await conn.execute(text("SELECT NOW()"))).scalar_one() == transaction_start < ends_at
        with pytest.raises(DBAPIError) as refused:
            await conn.execute(text(
                "UPDATE payroll.driverrates SET notes = 'after' WHERE driverrateid = :dr"), rows)
        assert is_driver_assignment_ended_error(refused.value)
        await conn.rollback()
    notes = (await direct_db.execute(text(
        "SELECT notes FROM payroll.driverrates WHERE driverrateid = :dr"), rows)).scalar_one()
    assert notes is None


# ---------------------------------------------------------------------------
# One error mapping: the DBAPIError handler
# ---------------------------------------------------------------------------

class _FakeDriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"fake driver error {sqlstate}")
        self.sqlstate = sqlstate


async def test_only_the_ended_assignment_sqlstate_is_mapped_and_everything_else_is_reraised():
    app: FastAPI = create_app()

    @app.get("/__ended")
    async def ended():
        raise DBAPIError("UPDATE x", {}, _FakeDriverError("FL001"))

    @app.get("/__other")
    async def other():
        raise DBAPIError("UPDATE x", {}, _FakeDriverError("23505"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mapped = await client.get("/__ended")
        assert mapped.status_code == 409
        assert mapped.json() == {"detail": ENDED}
        with pytest.raises(DBAPIError) as unchanged:
            await client.get("/__other")
    assert unchanged.value.orig.sqlstate == "23505"
