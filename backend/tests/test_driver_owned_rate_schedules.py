"""Driver-owned rate schedules through the real application: transfers, authority, visibility,
the canonical finalized guard and the lock order.

Ordinary rate schedules belong to the permanent Driver. These tests build real Drivers with
Workforce, move them with real permanent and Scheduled transfers, and drive the Compensation
API as Company-wide admins and as Branch-scoped managers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.compensation import resolver
from app.compensation.resolver import ResolutionKey
from tests.builders.access import create_company_role_with_permissions
from tests.builders.transfer_scenarios import (
    approved_transfer,
    auth,
    branch_today,
    db_now,
    make_branch,
    make_driver,
    open_period,
    scheduled_transfer,
    sfx,
)
from tests.test_ended_assignment_write_guard import (
    _definition,
    _expect,
    _manager,
    _values,
)

pytestmark = pytest.mark.asyncio

SCHEDULES = "/compensation/driver-rate-schedules"
LONG_AGO = "2026-01-01"  # inside the first tenure (scenario Drivers start 2020-01-01)


# ---------------------------------------------------------------------------
# Scenario building
# ---------------------------------------------------------------------------

@dataclass
class World:
    admin: str
    driver_id: int
    employee_id: int
    source: int  # the first home DriverAssignment
    old_branch: int
    new_branch: int
    other_branch: int
    old_manager: str
    new_manager: str
    other_manager: str


async def _world(client, admin, db) -> World:
    """A Driver in the old Branch (no transfer yet), three Branch managers, one unrelated."""
    old_branch, new_branch, other_branch = [await make_branch(db) for _ in range(3)]
    role_id = await create_company_role_with_permissions(
        client, admin, f"DOS manager {sfx()}",
        ["drivers.view", "payrates.view", "payrates.edit", "payitems.view"])
    driver = await make_driver(client, admin, old_branch)
    return World(
        admin=admin, driver_id=driver["driver_id"], employee_id=driver["employee_id"],
        source=driver["assignment_id"], old_branch=old_branch, new_branch=new_branch,
        other_branch=other_branch,
        old_manager=await _manager(client, admin, role_id, old_branch),
        new_manager=await _manager(client, admin, role_id, new_branch),
        other_manager=await _manager(client, admin, role_id, other_branch),
    )


async def _transfer(client, w: World) -> int:
    """Complete an immediate permanent transfer; returns the destination assignment id."""
    request_id = await approved_transfer(client, w.admin, w.driver_id, w.new_branch, None)
    completed = await client.post(
        f"/driver-transfers/{request_id}/complete", json={}, headers=auth(w.admin))
    await _expect(completed, 200)
    assert completed.json()["status"] == "Completed"
    return completed.json()["destination_driver_assignment_id"]


async def _create(client, token, w: World, definition, start, end=None):
    return await client.post(SCHEDULES, json={
        "driver_id": w.driver_id, "rate_definition_id": definition["rate_definition_id"],
        "effective_from": str(start), "effective_to": None if end is None else str(end),
    }, headers=auth(token))


async def _author(client, token, w: World, definition, start, end=None, amount="25") -> int:
    """Create, fill and approve one schedule; returns its id."""
    created = await _expect(await _create(client, token, w, definition, start, end), 201)
    schedule_id = created.json()["driver_rate_schedule_id"]
    await _expect(await _values(client, token, schedule_id, definition, amount), 200)
    await _expect(await client.post(f"{SCHEDULES}/{schedule_id}/approve", headers=auth(token)), 200)
    return schedule_id


async def _filled(client, token, w: World, definition, start, end=None, amount="25") -> int:
    created = await _expect(await _create(client, token, w, definition, start, end), 201)
    schedule_id = created.json()["driver_rate_schedule_id"]
    await _expect(await _values(client, token, schedule_id, definition, amount), 200)
    return schedule_id


async def _approve(client, token, schedule_id):
    return await client.post(f"{SCHEDULES}/{schedule_id}/approve", headers=auth(token))


def _code(response) -> str:
    return response.json()["detail"]["code"]


async def _resolution(client, token, driver_id, definition, day):
    return await client.get(
        f"/compensation/drivers/{driver_id}/rate-definitions/{definition['rate_definition_id']}"
        f"/resolution", params={"work_date": str(day)}, headers=auth(token))


async def _schedule_count(db, driver_id) -> int:
    return (await db.execute(text(
        "SELECT count(*) FROM payroll.driverrateschedules WHERE driverid = :d"),
        {"d": driver_id})).scalar_one()


# ---------------------------------------------------------------------------
# A permanent transfer continues compensation with zero steps
# ---------------------------------------------------------------------------

async def test_rates_follow_a_permanent_transfer_with_zero_manual_steps(
    client, auth_token, direct_db, test_engine,
):
    w = await _world(client, auth_token, direct_db)
    definition = await _definition(client, auth_token)
    schedule_id = await _author(client, w.old_manager, w, definition, LONG_AGO, amount="31.5")
    assert await _schedule_count(direct_db, w.driver_id) == 1

    destination = await _transfer(client, w)
    today = await branch_today(direct_db, w.new_branch)

    # The same schedule answers for a WorkDate in the old tenure and one in the new tenure.
    for day in (date(2026, 2, 1), today, today + timedelta(days=30)):
        resolved = await _resolution(client, auth_token, w.driver_id, definition, day)
        assert resolved.status_code == 200, (day, resolved.text)
        body = resolved.json()
        assert body["driver_rate_schedule_id"] == schedule_id
        assert body["driver_id"] == w.driver_id
        assert Decimal(body["components"][0]["amount"]) == Decimal("31.5")
        assert "branch_id" not in body and "driver_assignment_id" not in body
    async with test_engine.connect() as db:
        keys = [ResolutionKey(w.driver_id, definition["rate_definition_id"], day)
                for day in (date(2026, 2, 1), today)]
        resolved = await resolver.resolve_many(1, keys, db)
    assert {r.driver_rate_schedule_id for r in resolved.values()} == {schedule_id}

    # The transfer copied, moved and rewrote nothing.
    assert await _schedule_count(direct_db, w.driver_id) == 1
    assert (await direct_db.execute(text(
        "SELECT status, effectivefrom::text FROM payroll.driverrateschedules WHERE driverid = :d"),
        {"d": w.driver_id})).one() == ("Approved", LONG_AGO)
    assert destination != w.source


# ---------------------------------------------------------------------------
# D1: on the transfer day the context is the destination (latest-starting tenure)
# ---------------------------------------------------------------------------

async def test_the_transfer_day_context_is_the_destination(client, auth_token, direct_db):
    w = await _world(client, auth_token, direct_db)
    destination = await _transfer(client, w)
    today = await branch_today(direct_db, w.new_branch)
    # Both tenures touch today (the source ends, the destination starts, within it).
    context = (await direct_db.execute(text(
        "SELECT core.fn_DriverAssignmentForBusinessDate(1, :d, :day)"),
        {"d": w.driver_id, "day": today})).scalar_one()
    assert context == destination

    definition = await _definition(client, auth_token)
    refused = await _create(client, w.old_manager, w, definition, today)
    assert refused.status_code == 403  # the source Branch holds no authority on the transfer day
    allowed = await _create(client, w.new_manager, w, definition, today)
    assert allowed.status_code == 201, allowed.text
    assert (await _create(client, auth_token, w, await _definition(client, auth_token), today)
            ).status_code == 201


# ---------------------------------------------------------------------------
# D2: backdating inside a current tenure, never inside an ended one
# ---------------------------------------------------------------------------

async def test_backdating_is_allowed_inside_the_current_tenure(client, auth_token, direct_db):
    w = await _world(client, auth_token, direct_db)
    definition = await _definition(client, auth_token)
    # The first tenure began in 2020 and is still current: any date inside it is a valid start.
    schedule_id = await _author(client, w.old_manager, w, definition, "2026-01-05")
    assert schedule_id > 0


async def test_a_start_date_inside_an_ended_tenure_is_refused_for_everyone(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _transfer(client, w)
    definition = await _definition(client, auth_token)
    for token, expected in ((auth_token, 409), (w.old_manager, 409), (w.new_manager, 403)):
        response = await _create(client, token, w, definition, "2026-01-06")
        assert response.status_code == expected, (token, response.text)
    admin = await _create(client, auth_token, w, definition, "2026-01-06")
    assert _code(admin) == "DRIVER_COMPENSATION_HISTORICAL"
    assert await _schedule_count(direct_db, w.driver_id) == 0


# ---------------------------------------------------------------------------
# Scheduled transfer: authority starts at the EffectiveAt day, never earlier
# ---------------------------------------------------------------------------

async def test_a_scheduled_transfers_destination_has_authority_from_its_effective_day(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    effective_at = await db_now(direct_db) + timedelta(days=3)
    await scheduled_transfer(client, auth_token, w.driver_id, w.new_branch, effective_at)
    transfer_day = await branch_today(direct_db, w.new_branch) + timedelta(days=3)
    today = await branch_today(direct_db, w.old_branch)
    definition = await _definition(client, auth_token)

    # From the EffectiveAt day the destination Branch is the authority, the source is not.
    created = await _create(client, w.new_manager, w, definition, transfer_day)
    assert created.status_code == 201, created.text
    pending = created.json()["driver_rate_schedule_id"]
    await _expect(await _values(client, w.new_manager, pending, definition), 200)
    await _expect(await _approve(client, w.new_manager, pending), 200)
    assert (await _create(client, w.old_manager, w, await _definition(client, auth_token),
                          transfer_day)).status_code == 403
    # Before it, the source is still the authority and the destination has none.
    assert (await _create(client, w.new_manager, w, await _definition(client, auth_token),
                          today)).status_code == 403
    assert (await _create(client, w.old_manager, w, await _definition(client, auth_token),
                          today)).status_code == 201


async def test_a_transfer_that_is_not_yet_scheduled_grants_the_target_branch_nothing(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    effective_at = await db_now(direct_db) + timedelta(days=3)
    await approved_transfer(client, auth_token, w.driver_id, w.new_branch, effective_at)  # Approved
    transfer_day = await branch_today(direct_db, w.new_branch) + timedelta(days=3)
    definition = await _definition(client, auth_token)
    refused = await _create(client, w.new_manager, w, definition, transfer_day)
    assert refused.status_code == 403
    # The source tenure is still open-ended, so it keeps the authority for that date.
    assert (await _create(client, w.old_manager, w, definition, transfer_day)).status_code == 201


# ---------------------------------------------------------------------------
# D3: visibility
# ---------------------------------------------------------------------------

async def _visibility_world(client, auth_token, db):
    """A transferred Driver with a schedule inside the old tenure, one after the transfer and a
    Pending one after it, on three different definitions."""
    w = await _world(client, auth_token, db)
    in_old, in_new, pending_def = [await _definition(client, auth_token) for _ in range(3)]
    old_schedule = await _author(
        client, w.old_manager, w, in_old, LONG_AGO, "2026-06-30", amount="10")
    await _transfer(client, w)
    tomorrow = await branch_today(db, w.new_branch) + timedelta(days=1)
    new_schedule = await _author(client, w.new_manager, w, in_new, tomorrow, amount="20")
    pending_schedule = (await _expect(
        await _create(client, w.new_manager, w, pending_def, tomorrow), 201)
    ).json()["driver_rate_schedule_id"]
    return w, (in_old, in_new, pending_def), (old_schedule, new_schedule, pending_schedule), tomorrow


async def _summary_definitions(client, token, driver_id) -> set[int]:
    response = await client.get(
        f"/compensation/drivers/{driver_id}/rate-schedules/summary", headers=auth(token))
    assert response.status_code == 200, response.text
    return {row["rate_definition_id"] for row in response.json()}


async def test_a_past_branch_sees_only_overlapping_rows_read_only_and_never_a_pending_one(
    client, auth_token, direct_db,
):
    w, (in_old, in_new, pending_def), (old_s, new_s, pending_s), tomorrow = \
        await _visibility_world(client, auth_token, direct_db)
    token = w.old_manager

    assert (await client.get(f"{SCHEDULES}/{old_s}", headers=auth(token))).status_code == 200
    assert (await client.get(f"{SCHEDULES}/{new_s}", headers=auth(token))).status_code == 404
    assert (await client.get(f"{SCHEDULES}/{pending_s}", headers=auth(token))).status_code == 404
    assert await _summary_definitions(client, token, w.driver_id) == {in_old["rate_definition_id"]}

    def history(definition):
        return client.get(
            f"/compensation/drivers/{w.driver_id}/rate-definitions/"
            f"{definition['rate_definition_id']}/rate-schedules", headers=auth(token))

    assert [row["driver_rate_schedule_id"] for row in (await history(in_old)).json()] == [old_s]
    assert (await history(in_new)).json() == []
    assert (await history(pending_def)).json() == []

    # The resolution only answers for dates the Branch may read.
    assert (await _resolution(client, token, w.driver_id, in_old, date(2026, 2, 1))).status_code == 200
    gone = await _resolution(client, token, w.driver_id, in_new, tomorrow + timedelta(days=5))
    assert gone.status_code == 404 and _code(gone) == "NO_EFFECTIVE_SCHEDULE"

    rows = {row["rate_definition_id"]: row for row in (await client.get(
        f"/compensation/drivers/{w.driver_id}/pay-rates", headers=auth(token))).json()}
    assert rows[in_new["rate_definition_id"]]["future"] is None
    assert rows[pending_def["rate_definition_id"]]["pending"] is None

    # Read-only: every write for a date inside the old tenure is refused.
    refused = await _create(client, token, w, await _definition(client, auth_token), "2026-03-01")
    assert refused.status_code == 409 and _code(refused) == "DRIVER_COMPENSATION_HISTORICAL"


async def test_authority_branches_and_company_wide_readers_see_the_full_history(
    client, auth_token, direct_db,
):
    w, definitions, (old_s, new_s, pending_s), _ = \
        await _visibility_world(client, auth_token, direct_db)
    every_definition = {d["rate_definition_id"] for d in definitions}
    for token in (w.new_manager, auth_token):
        for schedule_id in (old_s, new_s, pending_s):
            response = await client.get(f"{SCHEDULES}/{schedule_id}", headers=auth(token))
            assert response.status_code == 200, (token, schedule_id, response.text)
        assert await _summary_definitions(client, token, w.driver_id) == every_definition
        history = (await client.get(
            f"/compensation/drivers/{w.driver_id}/rate-definitions/"
            f"{definitions[1]['rate_definition_id']}/rate-schedules", headers=auth(token))).json()
        assert [row["driver_rate_schedule_id"] for row in history] == [new_s]


async def test_an_unrelated_branch_manager_sees_nothing(client, auth_token, direct_db):
    w, definitions, (old_s, _, pending_s), _ = \
        await _visibility_world(client, auth_token, direct_db)
    token = w.other_manager
    for path in (
        f"{SCHEDULES}/{old_s}", f"{SCHEDULES}/{pending_s}",
        f"/compensation/drivers/{w.driver_id}/pay-rates",
        f"/compensation/drivers/{w.driver_id}/rate-schedules/summary",
        f"/compensation/drivers/{w.driver_id}/rate-definitions/"
        f"{definitions[0]['rate_definition_id']}/rate-schedules",
        f"/compensation/drivers/{w.driver_id}/rate-definitions/"
        f"{definitions[0]['rate_definition_id']}/resolution?work_date=2026-02-01",
    ):
        response = await client.get(path, headers=auth(token))
        assert response.status_code in (403, 404), (path, response.status_code)
    assert (await _create(client, token, w, definitions[0], date.today())).status_code == 403


# ---------------------------------------------------------------------------
# D4: a Pending schedule whose start date is now in an ended tenure
# ---------------------------------------------------------------------------

async def test_an_orphaned_pending_schedule_cannot_be_approved_but_can_be_discarded_by_authority(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    by_new, by_admin, by_old = [await _definition(client, auth_token) for _ in range(3)]
    orphans = {}
    for name, definition in (("new", by_new), ("admin", by_admin), ("old", by_old)):
        orphans[name] = await _filled(client, w.old_manager, w, definition, "2026-02-01")
    await _transfer(client, w)

    # Nobody can approve it: the start date lies in the ended tenure.
    for token, expected in ((auth_token, 409), (w.old_manager, 409), (w.new_manager, 403)):
        response = await _approve(client, token, orphans["new"])
        assert response.status_code == expected, (token, response.text)
    assert _code(await _approve(client, auth_token, orphans["new"])) == "DRIVER_COMPENSATION_HISTORICAL"

    # Discard needs authority over the Driver now: the destination Branch and company-wide
    # may, the past Branch may not (a Pending row has no payroll effect, so no tenure check).
    assert (await client.delete(
        f"{SCHEDULES}/{orphans['old']}", headers=auth(w.old_manager))).status_code == 403
    assert (await client.delete(
        f"{SCHEDULES}/{orphans['new']}", headers=auth(w.new_manager))).status_code == 204
    assert (await client.delete(
        f"{SCHEDULES}/{orphans['admin']}", headers=auth(auth_token))).status_code == 204
    assert (await client.get(
        f"{SCHEDULES}/{orphans['old']}", headers=auth(auth_token))).status_code == 200


# ---------------------------------------------------------------------------
# D5: any company-Active scalar definition is authorable; the UI flags applicability
# ---------------------------------------------------------------------------

async def test_a_definition_not_applicable_in_the_context_branch_is_authorable_and_flagged(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    await _transfer(client, w)
    definition = await _definition(client, auth_token)  # no Branch configuration anywhere
    created = await _create(client, w.new_manager, w, definition, await branch_today(
        direct_db, w.new_branch))
    assert created.status_code == 201, created.text

    def flag():
        async def read():
            rows = (await client.get(
                f"/compensation/drivers/{w.driver_id}/pay-rates", headers=auth(auth_token))).json()
            return {row["pay_definition_id"]: row for row in rows}[
                definition["pay_definition_id"]]["applicable_in_context_branch"]
        return read()

    assert await flag() is False
    await direct_db.execute(text("""
        INSERT INTO payroll.branchpaydefinitionconfig
            (companyid, branchid, paydefinitionid, isactive, effectivefrom)
        VALUES (1, :b, :d, TRUE, DATE '2000-01-01')
    """), {"b": w.new_branch, "d": definition["pay_definition_id"]})
    assert await flag() is True  # applicable in the CONTEXT Branch (the destination), not the old one


# ---------------------------------------------------------------------------
# The canonical finalized guard: any Branch, any assignment of the Driver
# ---------------------------------------------------------------------------

async def test_a_finalized_period_in_the_old_branch_blocks_a_change_authored_in_the_new_one(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    today = await branch_today(direct_db, w.old_branch)
    # A Locked period of the OLD Branch that spans the transfer, rostering the old assignment.
    period = await open_period(direct_db, w.old_branch, today - timedelta(days=3),
                               today + timedelta(days=3), "Locked")
    await _transfer(client, w)

    blocked_def, free_def = await _definition(client, auth_token), await _definition(client, auth_token)
    inside = await _filled(client, w.new_manager, w, blocked_def, today + timedelta(days=1))
    refused = await _approve(client, w.new_manager, inside)
    assert refused.status_code == 422, refused.text
    assert _code(refused) == "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD"
    assert refused.json()["detail"]["payroll_period_ids"] == [period]
    assert (await direct_db.execute(text(
        "SELECT status FROM payroll.driverrateschedules WHERE driverratescheduleid = :s"),
        {"s": inside})).scalar_one() == "Pending"

    # A change that starts after the finalized period is not touched by it.
    later = await _filled(client, w.new_manager, w, free_def, today + timedelta(days=10))
    assert (await _approve(client, w.new_manager, later)).status_code == 200


async def test_an_unrelated_drivers_finalized_period_does_not_block(client, auth_token, direct_db):
    w = await _world(client, auth_token, direct_db)
    other_driver = await make_driver(client, auth_token, w.other_branch)  # noqa: F841 - rostered
    today = await branch_today(direct_db, w.other_branch)
    await open_period(direct_db, w.other_branch, today - timedelta(days=3),
                      today + timedelta(days=3), "Locked")
    definition = await _definition(client, auth_token)
    schedule_id = await _filled(client, w.old_manager, w, definition, today + timedelta(days=1))
    assert (await _approve(client, w.old_manager, schedule_id)).status_code == 200


async def test_the_supersession_tail_over_a_later_finalized_period_is_refused(
    client, auth_token, direct_db,
):
    """Approving a successor that ends early still supersedes the open-ended prior schedule
    from the successor's start onward; a start-date-only check missed the later period."""
    w = await _world(client, auth_token, direct_db)
    definition = await _definition(client, auth_token)
    await _author(client, w.old_manager, w, definition, LONG_AGO, amount="10")
    period = await open_period(direct_db, w.old_branch, date(2026, 5, 4), date(2026, 5, 10), "Locked")

    successor = await _filled(client, w.old_manager, w, definition, "2026-04-01", "2026-04-30")
    refused = await _approve(client, w.old_manager, successor)
    assert refused.status_code == 422, refused.text
    assert _code(refused) == "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD"
    assert refused.json()["detail"]["payroll_period_ids"] == [period]
    assert await _schedule_count(direct_db, w.driver_id) == 2

    # The same successor over a prior schedule that already ended before the period is fine.
    bounded = await _definition(client, auth_token)
    await _author(client, w.old_manager, w, bounded, LONG_AGO, "2026-04-30", amount="10")
    ok = await _filled(client, w.old_manager, w, bounded, "2026-04-15", "2026-04-30")
    assert (await _approve(client, w.old_manager, ok)).status_code == 200


async def test_voiding_a_schedule_whose_window_overlaps_a_finalized_period_is_refused(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    inside, outside = await _definition(client, auth_token), await _definition(client, auth_token)
    touched = await _author(client, w.old_manager, w, inside, "2026-08-01", "2026-08-31")
    free = await _author(client, w.old_manager, w, outside, "2026-09-01", "2026-09-30")
    period = await open_period(direct_db, w.old_branch, date(2026, 8, 10), date(2026, 8, 16), "Locked")

    refused = await client.post(
        f"{SCHEDULES}/{touched}/void", json={"reason": "wrong"}, headers=auth(w.old_manager))
    assert refused.status_code == 422
    assert _code(refused) == "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD"
    assert refused.json()["detail"]["payroll_period_ids"] == [period]
    allowed = await client.post(
        f"{SCHEDULES}/{free}/void", json={"reason": "wrong"}, headers=auth(w.old_manager))
    assert allowed.status_code == 200, allowed.text


# ---------------------------------------------------------------------------
# Lock order: the Branch workflow lock before the schedule rows
# ---------------------------------------------------------------------------

async def _wait_blocked_behind(db, pid: int, task: asyncio.Task) -> None:
    for _ in range(500):
        blocked = (await db.execute(text(
            "SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))"),
            {"pid": pid})).first()
        if blocked:
            return
        if task.done():
            raise AssertionError(f"the writer finished instead of blocking: {task.result().text}")
        await asyncio.sleep(0.02)
    raise AssertionError("the writer never blocked behind the lock holder")


@pytest.mark.parametrize("finalize", [True, False], ids=["period-locks-first", "holder-rolls-back"])
async def test_an_approval_waiting_on_the_branch_workflow_lock_sees_a_period_finalized_meanwhile(
    client, auth_token, direct_db, test_engine, finalize,
):
    w = await _world(client, auth_token, direct_db)
    definition = await _definition(client, auth_token)
    period = await open_period(
        direct_db, w.old_branch, date(2026, 7, 6), date(2026, 7, 12), "Approved")
    pending = await _filled(client, w.old_manager, w, definition, "2026-07-08")

    holder = await test_engine.connect()
    task = None
    try:
        holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        # A finalize in flight: it holds the Branch workflow lock and moves the period to Locked.
        await holder.execute(text("SELECT pg_advisory_xact_lock(1, :b)"), {"b": w.old_branch})
        await holder.execute(text("""
            UPDATE payroll.payrollperiods
            SET status = 'Locked', lockedatutc = now(), lockedbyuserid = 1
            WHERE payrollperiodid = :p
        """), {"p": period})
        task = asyncio.create_task(_approve(client, w.old_manager, pending))
        await _wait_blocked_behind(direct_db, holder_pid, task)
        if finalize:
            await holder.commit()
        else:
            await holder.rollback()
        response = await asyncio.wait_for(task, 30)
    finally:
        if task is not None and not task.done():
            task.cancel()
        await holder.close()

    status = (await direct_db.execute(text(
        "SELECT status FROM payroll.driverrateschedules WHERE driverratescheduleid = :s"),
        {"s": pending})).scalar_one()
    if finalize:
        assert response.status_code == 422, response.text
        assert _code(response) == "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD"
        assert status == "Pending"
    else:
        assert response.status_code == 200, response.text
        assert status == "Approved"


async def test_an_approval_waiting_while_its_context_tenure_ends_is_refused_without_change(
    client, auth_token, direct_db, test_engine,
):
    """Mutation-instant: a Scheduled transfer takes effect a few seconds ahead; the approval
    blocks on the Branch workflow lock past that instant and is decided after the wait."""
    w = await _world(client, auth_token, direct_db)
    definition = await _definition(client, auth_token)
    pending = await _filled(client, w.old_manager, w, definition, "2026-07-08")
    effective_at = await db_now(direct_db) + timedelta(seconds=6)
    await scheduled_transfer(client, auth_token, w.driver_id, w.new_branch, effective_at)

    holder = await test_engine.connect()
    task = None
    try:
        holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        await holder.execute(text("SELECT pg_advisory_xact_lock(1, :b)"), {"b": w.old_branch})
        task = asyncio.create_task(_approve(client, w.old_manager, pending))
        await _wait_blocked_behind(direct_db, holder_pid, task)
        assert (await direct_db.execute(
            text("SELECT :b > clock_timestamp()"), {"b": effective_at})).scalar_one()
        while not (await direct_db.execute(
                text("SELECT clock_timestamp() > :b"),
                {"b": effective_at + timedelta(milliseconds=200)})).scalar_one():
            await asyncio.sleep(0.1)
        await holder.rollback()
        response = await asyncio.wait_for(task, 30)
    finally:
        if task is not None and not task.done():
            task.cancel()
        await holder.close()

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "DRIVER_COMPENSATION_HISTORICAL"
    assert (await direct_db.execute(text(
        "SELECT status FROM payroll.driverrateschedules WHERE driverratescheduleid = :s"),
        {"s": pending})).scalar_one() == "Pending"


# ---------------------------------------------------------------------------
# Plain reads
# ---------------------------------------------------------------------------

async def test_unknown_drivers_and_schedules_are_not_found(
    client, auth_token, direct_db,
):
    w = await _world(client, auth_token, direct_db)
    definition = await _definition(client, auth_token)
    schedule_id = await _author(client, w.old_manager, w, definition, LONG_AGO)
    unknown = await client.get(
        "/compensation/drivers/2000000000/pay-rates", headers=auth(auth_token))
    assert unknown.status_code == 404
    assert (await client.get(f"{SCHEDULES}/{schedule_id}", headers=auth(auth_token))).status_code == 200
    assert (await client.get(f"{SCHEDULES}/2000000000", headers=auth(auth_token))).status_code == 404
