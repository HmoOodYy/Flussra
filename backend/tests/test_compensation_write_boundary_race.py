"""A compensation write is decided at the MUTATION instant, not at the guard's instant.

Two owners, one principle. A writer can block after its early check (Branch advisory lock,
row lock, unique-index wait) while a Scheduled source assignment's future ``EffectiveToAt``
passes; ``FOR SHARE`` freezes the value, not time.

* Assignment-owned status rates: ``require_writable_driver_assignment`` refuses what is
  already ended, and ``core.trg_CompensationAssignmentWritable`` (an AFTER ROW trigger on
  ``driverrates`` and ``driverratetiers``) re-decides with ``clock_timestamp()`` after the
  statement's own waits: HTTP 409 ``DRIVER_ASSIGNMENT_ENDED``.
* Driver-owned rate schedules: the writer reads one decision instant after every lock is
  held and re-checks the context tenure of the change's start date; the database trigger
  ``core.trg_DriverCompensationContextWritable`` decides again at the DML instant: HTTP 409
  ``DRIVER_COMPENSATION_HISTORICAL``. Both map through the one FL001 handler.

Race tests (two connections, real writer paths): connection A holds the real lock the
writer waits on; the writer, started through the API, passes its early check while the source
is current and BLOCKS (asserted through ``pg_blocking_pids`` and the waiting statement);
the source's boundary passes; A releases; the writer must be refused and change nothing.
Positive twins run the identical blocked operation with the boundary far in the future.

Writers exercised (blocking point):
  rates.approve_rate    Branch workflow advisory lock  pg_advisory_xact_lock(cid, bid)
  rates.void_rate       Branch workflow advisory lock  pg_advisory_xact_lock(cid, bid)
  schedules.approve     prior Approved row FOR UPDATE

The Values and Tiers tables are children whose writers only mutate them after a parent
statement the same transaction already ran; they have no independent blocking point to
hold, so their trigger is covered by the table-level tests below.
"""

import asyncio
import json
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
    HISTORICAL_MESSAGE,
    _columns,
    _definition,
    _expect,
    _pending,
    _rate,
    _values,
)

pytestmark = pytest.mark.asyncio

# The Scheduled source ends this long after the scenario is built: long enough for the
# writer to start, pass the guard and block, short enough to keep the module quick.
ENDS_IN_SECONDS = 6.0
FAR_FUTURE = timedelta(days=3)
ENDED = {"code": "DRIVER_ASSIGNMENT_ENDED", "message": ENDED_MESSAGE}
HISTORICAL = {"code": "DRIVER_COMPENSATION_HISTORICAL", "message": HISTORICAL_MESSAGE}


# ---------------------------------------------------------------------------
# Scenario building and the two-connection race runner
# ---------------------------------------------------------------------------

@dataclass
class Scene:
    admin: str
    driver_id: int
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
    return Scene(admin, driver["driver_id"], transfer["source_id"], transfer["destination_id"],
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


def _assert_refused_at_the_mutation_instant(response, refusal=ENDED):
    assert response.status_code == 409, (response.status_code, response.text)
    assert response.json() == {"detail": refusal}


async def _fingerprint(db, scene: "Scene") -> str:
    """Every compensation row of the scene's Driver (schedules) and source assignment (rates)."""
    parts = []
    for query, key in (
        ("SELECT to_jsonb(r) FROM payroll.driverrateschedules r "
         "WHERE r.driverid = :a ORDER BY r.driverratescheduleid", scene.driver_id),
        ("SELECT to_jsonb(v) FROM payroll.driverrateschedulevalues v "
         "JOIN payroll.driverrateschedules r ON r.driverratescheduleid = v.driverratescheduleid "
         "WHERE r.driverid = :a ORDER BY v.driverratescheduleid, v.ratecomponentdefinitionid",
         scene.driver_id),
        ("SELECT to_jsonb(d) FROM payroll.driverrates d "
         "WHERE d.driverassignmentid = :a ORDER BY d.driverrateid", scene.source),
    ):
        parts.append((await db.execute(text(query), {"a": key})).scalars().all())
    return json.dumps(parts, sort_keys=True, default=str)


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
    before = await _fingerprint(direct_db, scene)

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
        assert await _fingerprint(direct_db, scene) == before
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
    before = await _fingerprint(direct_db, scene)

    response = await _race(
        test_engine, direct_db, scene, ends_soon=ends_soon,
        hold=await _advisory(source_branch), blocked_on="pg_advisory_xact_lock",
        send=lambda: client.delete(f"/payroll/rates/{approved}", headers=auth(auth_token)))

    status = (await direct_db.execute(text(
        "SELECT status FROM payroll.driverrates WHERE driverrateid = :r"), {"r": approved})).scalar_one()
    if ends_soon:
        _assert_refused_at_the_mutation_instant(response)
        assert status == "Approved"
        assert await _fingerprint(direct_db, scene) == before
    else:
        await _expect(response, 204)
        assert status == "Voided"


# ---------------------------------------------------------------------------
# schedules.approve (row lock on the prior Approved schedule)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ends_soon", [True, False], ids=["source-ends-while-blocked", "source-stays-current"])
async def test_schedule_approve_blocked_on_the_prior_row_lock_is_decided_when_it_resumes(
    client, auth_token, direct_db, test_engine, ends_soon,
):
    driver, source_branch, target_branch = await _driver(client, auth_token, direct_db)
    definition = await _definition(client, auth_token)
    driver_id = driver["driver_id"]
    prior = await _pending(client, auth_token, driver_id, definition, "2026-01-01")
    await _expect(await _values(client, auth_token, prior, definition, "20"), 200)
    await _expect(await client.post(
        f"/compensation/driver-rate-schedules/{prior}/approve", headers=auth(auth_token)), 200)
    successor = await _pending(client, auth_token, driver_id, definition, "2026-03-01")
    await _expect(await _values(client, auth_token, successor, definition, "30"), 200)
    scene = await _schedule(client, auth_token, direct_db, driver, source_branch, target_branch,
                            ends_soon=ends_soon)
    before = await _fingerprint(direct_db, scene)

    async def hold(conn):
        await conn.execute(text("""
            SELECT 1 FROM payroll.driverrateschedules
            WHERE driverratescheduleid = :prior FOR UPDATE
        """), {"prior": prior})

    response = await _race(
        test_engine, direct_db, scene, ends_soon=ends_soon, hold=hold, blocked_on="FOR UPDATE",
        send=lambda: client.post(
            f"/compensation/driver-rate-schedules/{successor}/approve", headers=auth(auth_token)))

    states = dict((await direct_db.execute(text("""
        SELECT driverratescheduleid, status FROM payroll.driverrateschedules
        WHERE driverratescheduleid IN (:p, :s)
    """), {"p": prior, "s": successor})).all())
    if ends_soon:
        # The start date 2026-03-01 is inside the source tenure, which has ended by the clock.
        _assert_refused_at_the_mutation_instant(response, HISTORICAL)
        assert states == {prior: "Approved", successor: "Pending"}
        assert await _fingerprint(direct_db, scene) == before
    else:
        await _expect(response, 200)
        assert states == {prior: "Superseded", successor: "Approved"}


# ---------------------------------------------------------------------------
# The trigger itself: every covered table, every operation, three assignment states
# ---------------------------------------------------------------------------

COVERED_TABLES = ("driverrates", "driverratetiers")


async def _rows(client, admin, db, assignment_id, branch_id, definitions, columns) -> dict:
    """A status rate with one tier on one assignment (written while writable)."""
    rows = {"dr": await _rate(client, admin, assignment_id, columns[0])}
    await db.execute(text("""
        INSERT INTO payroll.driverratetiers (driverrateid, tiersequence, fromunit, tounit, tieramount)
        VALUES (:dr, 1, 0, 10, 1)
    """), {"dr": rows["dr"]})
    return rows


def _operations(rows, assignment_id, branch_id, definitions, columns, admin_id):
    """(table, operation, sql, params): inserts, then updates, then deletes children first."""
    p = {"a": assignment_id, "b": branch_id, "u": admin_id, **rows}
    return [
        ("driverrates", "INSERT", """
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverassignmentid, ratetypeid, amount, effectivefrom,
                 status, createdbyuserid)
            VALUES (1, :b, :a, :rt, 3, DATE '2026-02-01', 'PendingApproval', :u)""",
         {**p, "rt": columns[1]["rate_type_id"]}),
        ("driverratetiers", "INSERT", """
            INSERT INTO payroll.driverratetiers (driverrateid, tiersequence, fromunit, tounit, tieramount)
            VALUES (:dr, 2, 11, 20, 2)""", p),
        ("driverrates", "UPDATE", "UPDATE payroll.driverrates SET notes = 'probe' WHERE driverrateid = :dr", p),
        ("driverratetiers", "UPDATE", """
            UPDATE payroll.driverratetiers SET tieramount = 2
            WHERE driverrateid = :dr AND tiersequence = 1""", p),
        ("driverratetiers", "DELETE", "DELETE FROM payroll.driverratetiers WHERE driverrateid = :dr", p),
        ("driverrates", "DELETE", "DELETE FROM payroll.driverrates WHERE driverrateid = :dr", p),
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
    definitions = [await _definition(client, admin) for _ in range(3)]
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
        assert len(outcomes) == 6


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
    scene = Scene(auth_token, driver["driver_id"], assignment_id, 0, source_branch, 0, await db_now(direct_db))
    before, tiers = await _fingerprint(direct_db, scene), await _tier_count(direct_db, rows["dr"])
    assert tiers == 1

    outcomes = await _run_operations(direct_db, _operations(
        rows, assignment_id, source_branch, definitions, columns[source_branch], admin_id))

    assert set(outcomes.values()) == {"ended"}, outcomes
    assert {table for table, _ in outcomes} == set(COVERED_TABLES)
    assert {operation for _, operation in outcomes} == {"INSERT", "UPDATE", "DELETE"}
    assert await _fingerprint(direct_db, scene) == before
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
            INSERT INTO payroll.driverrates
                (companyid, branchid, driverassignmentid, ratetypeid, amount, effectivefrom,
                 status, createdbyuserid)
            VALUES (1, :b, :a, :rt, 1, DATE '2026-01-01', 'PendingApproval', :u)
        """), {"b": source_branch, "a": driver["assignment_id"], "u": admin_id,
               "rt": columns[source_branch][0]["rate_type_id"]})
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
    def __init__(self, sqlstate: str, message: str | None = None) -> None:
        super().__init__(message or f"fake driver error {sqlstate}")
        self.sqlstate = sqlstate


async def test_only_the_historical_compensation_sqlstate_is_mapped_by_its_message_prefix():
    app: FastAPI = create_app()

    @app.get("/__ended")
    async def ended():
        raise DBAPIError("UPDATE x", {}, _FakeDriverError("FL001"))

    @app.get("/__historical")
    async def historical():
        raise DBAPIError("UPDATE x", {}, _FakeDriverError(
            "FL001", "DRIVER_COMPENSATION_HISTORICAL: Driver-owned compensation ..."))

    @app.get("/__other")
    async def other():
        raise DBAPIError("UPDATE x", {}, _FakeDriverError("23505"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mapped = await client.get("/__ended")
        assert mapped.status_code == 409
        assert mapped.json() == {"detail": ENDED}
        historical = await client.get("/__historical")
        assert historical.status_code == 409
        assert historical.json() == {"detail": HISTORICAL}
        with pytest.raises(DBAPIError) as unchanged:
            await client.get("/__other")
    assert unchanged.value.orig.sqlstate == "23505"
