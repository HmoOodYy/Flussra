"""Two-connection races between payroll source writers and Driver boundary changes.

A boundary change that shrinks coverage (reschedule, cancel, immediate or scheduled
complete, termination) holds the PayrollPeriod row locks of every period it touches; a
source writer authorizes its write against the roster and window only after taking the
same period row lock. Either order is therefore safe:

* writer first: the boundary change waits for the writer, then sees its fact and refuses
  (or, for complete/termination, proceeds after it);
* boundary change first: the writer waits for the lock, then sees the new window and is
  rejected instead of committing a fact outside coverage or on a deleted roster row.

Determinism comes from pausing one side while it holds the real period row lock.
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.payroll import bonus as bonus_module
from app.payroll import eligibility as eligibility_module
from tests.builders.transfer_scenarios import (
    approved_transfer,
    auth,
    branch_today,
    local_boundary,
    make_branch,
    make_driver,
    open_period,
    scheduled_transfer,
)

pytestmark = pytest.mark.asyncio


class Gate:
    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()


def pause_after(monkeypatch, module, name: str) -> Gate:
    """Let the first call of ``module.name`` finish, then hold its transaction open."""
    real = getattr(module, name)
    gate = Gate()
    state = {"paused": False}

    async def wrapper(*args, **kwargs):
        result = await real(*args, **kwargs)
        if not state["paused"]:
            state["paused"] = True
            gate.reached.set()
            await gate.release.wait()
        return result

    monkeypatch.setattr(module, name, wrapper)
    return gate


async def _bonus(client, token, period_id, assignment_id):
    return await client.post(
        f"/payroll/periods/{period_id}/bonuses",
        json={"driver_assignment_id": assignment_id, "amount": "10.00", "reason": "race"},
        headers=auth(token),
    )


async def _bonus_count(db, period_id, assignment_id) -> int:
    return (await db.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollBonusEvents
        WHERE PayrollPeriodID = :period_id AND DriverAssignmentID = :aid
    """), {"period_id": period_id, "aid": assignment_id})).scalar_one()


async def _scenario(client, token, db, *, transfer_day: int, period: tuple[int, int]):
    source_branch, target_branch = await make_branch(db), await make_branch(db)
    driver = await make_driver(client, token, source_branch)
    today = await branch_today(db, source_branch)
    period_id = await open_period(
        db, source_branch, today + timedelta(days=period[0]), today + timedelta(days=period[1]))
    transfer = await scheduled_transfer(
        client, token, driver["driver_id"], target_branch,
        await local_boundary(db, source_branch, today + timedelta(days=transfer_day)))
    return {"source_branch": source_branch, "target_branch": target_branch, "driver": driver,
            "today": today, "period_id": period_id, "transfer": transfer}


async def _reschedule(client, token, request_id, instant):
    return await client.post(
        f"/driver-transfers/{request_id}/reschedule",
        json={"effective_at": instant.isoformat()}, headers=auth(token),
    )


async def test_reschedule_waits_for_an_in_flight_writer_then_refuses_its_uncovered_fact(
    client, auth_token, direct_db, monkeypatch,
):
    s = await _scenario(client, auth_token, direct_db, transfer_day=10, period=(2, 8))
    writer_gate = pause_after(monkeypatch, bonus_module, "_increment_bonus_data_revision")
    bonus = asyncio.create_task(_bonus(client, auth_token, s["period_id"], s["driver"]["assignment_id"]))
    await asyncio.wait_for(writer_gate.reached.wait(), timeout=15)

    earlier = await local_boundary(direct_db, s["source_branch"], s["today"] + timedelta(days=1))
    reschedule = asyncio.create_task(_reschedule(client, auth_token, s["transfer"]["request_id"], earlier))
    await asyncio.sleep(1.0)
    assert not reschedule.done(), "the reschedule must wait for the writer's period lock"

    writer_gate.release.set()
    assert (await asyncio.wait_for(bonus, timeout=15)).status_code == 201
    refused = await asyncio.wait_for(reschedule, timeout=15)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "DRIVER_ASSIGNMENT_PAYROLL_FACTS_UNCOVERED"
    # Refused and rolled back: the boundary did not move, the writer's fact stands.
    moved = (await direct_db.execute(text(
        "SELECT EffectiveToAt FROM core.DriverAssignments WHERE DriverAssignmentID = :id"
    ), {"id": s["transfer"]["source_id"]})).scalar_one()
    assert moved == s["transfer"]["effective_at"]
    assert await _bonus_count(direct_db, s["period_id"], s["driver"]["assignment_id"]) == 1


async def test_writer_that_waits_for_a_reschedule_is_rejected_against_the_new_window(
    client, auth_token, direct_db, monkeypatch,
):
    """HEAD authorized the write before taking the period lock and committed a stale bonus."""
    s = await _scenario(client, auth_token, direct_db, transfer_day=10, period=(2, 8))
    gate = pause_after(monkeypatch, eligibility_module, "recapture_assignment_eligibility")
    earlier = await local_boundary(direct_db, s["source_branch"], s["today"] + timedelta(days=1))
    reschedule = asyncio.create_task(_reschedule(client, auth_token, s["transfer"]["request_id"], earlier))
    await asyncio.wait_for(gate.reached.wait(), timeout=15)

    bonus = asyncio.create_task(_bonus(client, auth_token, s["period_id"], s["driver"]["assignment_id"]))
    await asyncio.sleep(1.0)
    assert not bonus.done(), "the writer must wait for the period lock the reschedule holds"

    gate.release.set()
    assert (await asyncio.wait_for(reschedule, timeout=15)).status_code == 200
    rejected = await asyncio.wait_for(bonus, timeout=15)
    assert rejected.status_code == 422, rejected.text
    assert await _bonus_count(direct_db, s["period_id"], s["driver"]["assignment_id"]) == 0


async def test_day_grid_writer_that_waits_for_a_reschedule_cannot_commit_an_uncovered_workdate(
    client, auth_token, direct_db, monkeypatch,
):
    s = await _scenario(client, auth_token, direct_db, transfer_day=6, period=(0, 9))
    gate = pause_after(monkeypatch, eligibility_module, "recapture_assignment_eligibility")
    earlier = await local_boundary(direct_db, s["source_branch"], s["today"] + timedelta(days=3))
    reschedule = asyncio.create_task(_reschedule(client, auth_token, s["transfer"]["request_id"], earlier))
    await asyncio.wait_for(gate.reached.wait(), timeout=15)

    uncovered_day = s["today"] + timedelta(days=4)  # covered before the reschedule, not after
    save = asyncio.create_task(client.post(
        f"/payroll/periods/{s['period_id']}/day-grid",
        json={"work_date": uncovered_day.isoformat(),
              "rows": [{"driver_assignment_id": s["driver"]["assignment_id"], "values": {},
                        "status_key": None, "notes": "race note"}]},
        headers=auth(auth_token),
    ))
    await asyncio.sleep(1.0)
    assert not save.done(), "the day-grid save must wait for the period lock"

    gate.release.set()
    assert (await asyncio.wait_for(reschedule, timeout=15)).status_code == 200
    rejected = await asyncio.wait_for(save, timeout=15)
    assert rejected.status_code == 422, rejected.text
    facts = (await direct_db.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollDraftLines
        WHERE PayrollPeriodID = :period_id AND DriverAssignmentID = :aid AND WorkDate = :day
    """), {"period_id": s["period_id"], "aid": s["driver"]["assignment_id"],
           "day": uncovered_day})).scalar_one()
    assert facts == 0


async def test_writer_for_the_destination_is_rejected_when_a_cancel_commits_first(
    client, auth_token, direct_db, monkeypatch,
):
    """HEAD let the writer pass on a roster row the cancel was deleting, then hit a broken FK."""
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    period_id = await open_period(
        direct_db, target_branch, today + timedelta(days=3), today + timedelta(days=8))
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch,
        await local_boundary(direct_db, source_branch, today + timedelta(days=3)))
    gate = pause_after(monkeypatch, eligibility_module, "recapture_assignment_eligibility")
    cancel = asyncio.create_task(client.post(
        f"/driver-transfers/{transfer['request_id']}/cancel", json={}, headers=auth(auth_token)))
    await asyncio.wait_for(gate.reached.wait(), timeout=15)

    bonus = asyncio.create_task(_bonus(client, auth_token, period_id, transfer["destination_id"]))
    await asyncio.sleep(1.0)
    assert not bonus.done()

    gate.release.set()
    assert (await asyncio.wait_for(cancel, timeout=15)).status_code == 200
    rejected = await asyncio.wait_for(bonus, timeout=15)
    assert rejected.status_code == 422, rejected.text
    assert await _bonus_count(direct_db, period_id, transfer["destination_id"]) == 0


async def test_complete_waits_for_an_in_flight_source_writer(
    client, auth_token, direct_db, monkeypatch,
):
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    period_id = await open_period(direct_db, source_branch, today, today + timedelta(days=9))
    request_id = await approved_transfer(client, auth_token, driver["driver_id"], target_branch, None)

    gate = pause_after(monkeypatch, bonus_module, "_lock_period_for_mutation")
    bonus = asyncio.create_task(_bonus(client, auth_token, period_id, driver["assignment_id"]))
    await asyncio.wait_for(gate.reached.wait(), timeout=15)
    complete = asyncio.create_task(client.post(
        f"/driver-transfers/{request_id}/complete", json={}, headers=auth(auth_token)))
    await asyncio.sleep(1.0)
    assert not complete.done(), "completion removes source coverage and must wait for the writer"

    gate.release.set()
    assert (await asyncio.wait_for(bonus, timeout=15)).status_code == 201
    assert (await asyncio.wait_for(complete, timeout=15)).status_code == 200


async def test_termination_waits_for_an_in_flight_source_writer(
    client, auth_token, direct_db, monkeypatch,
):
    source_branch = await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    period_id = await open_period(direct_db, source_branch, today, today + timedelta(days=9))

    gate = pause_after(monkeypatch, bonus_module, "_lock_period_for_mutation")
    bonus = asyncio.create_task(_bonus(client, auth_token, period_id, driver["assignment_id"]))
    await asyncio.wait_for(gate.reached.wait(), timeout=15)
    terminate = asyncio.create_task(client.post(
        f"/workforce/employees/{driver['employee_id']}/terminate",
        json={"termination_date": (today + timedelta(days=1)).isoformat(), "reason": "race"},
        headers=auth(auth_token)))
    await asyncio.sleep(1.0)
    assert not terminate.done(), "termination caps coverage and must wait for the writer"

    gate.release.set()
    assert (await asyncio.wait_for(bonus, timeout=15)).status_code == 201
    assert (await asyncio.wait_for(terminate, timeout=15)).status_code == 200
