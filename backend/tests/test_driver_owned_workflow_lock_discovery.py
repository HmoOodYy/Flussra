"""Driver-owned writers serialize against the finalize of EVERY Branch the guard can judge.

The finalized-payroll guard (``payroll.fn_DriverFinalizedPayrollTouched``) judges frozen
eligibility rows of the Driver's assignments in any Branch. The writers' Branch workflow-lock
discovery used the live Branch-local tenure days instead. After a Branch timezone change the
live days of a historical Branch can stop reaching a footprint that one of that Branch's
periods (captured under the old zone) still overlaps, so no workflow lock was taken there and
a concurrent finalize was not serialized. Both now come from one database predicate,
``payroll.fn_DriverPayrollPeriodsForFootprint``.

Race (two connections, real writer paths): connection A holds the Branch workflow advisory lock
of the historical Branch, the writer starts through the API and must BLOCK on it
(``pg_blocking_pids``), A then commits the period's Approved -> Locked transition, and the
writer must be refused with DRIVER_COMPENSATION_IN_FINALIZED_PERIOD and change nothing. The
twin leaves the period Approved and the writer must succeed.
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.compensation.driver_context import load_driver_tenures
from tests.builders.pay_rules import ensure_branch_pay_calendar
from tests.builders.transfer_scenarios import (
    auth,
    branch_today,
    local_boundary,
    make_branch,
    make_driver,
    open_period,
    scheduled_transfer,
)
from tests.test_compensation_write_boundary_race import _wait_until_blocked
from tests.test_ended_assignment_write_guard import _definition, _expect, _pending, _values

pytestmark = pytest.mark.asyncio

FINALIZED = "DRIVER_COMPENSATION_IN_FINALIZED_PERIOD"


async def _scene(client, token, db):
    """Branch A (UTC) holds a one-day period P captured in UTC and Approved; the Driver's A
    tenure ends at P 06:00Z by a Scheduled transfer to Branch B; A's zone is then changed to
    Los Angeles, so the live Branch-local last day of the A tenure is P-1 (it no longer
    reaches P). Footprints starting at P are therefore outside the live A tenure."""
    branch_a, branch_b = await make_branch(db), await make_branch(db)
    for branch_id in (branch_a, branch_b):
        await ensure_branch_pay_calendar(db, 1, branch_id)
    driver = await make_driver(client, token, branch_a)
    today = await branch_today(db, branch_a)
    day = today + timedelta(days=8)
    midnight = await local_boundary(db, branch_a, day)
    period = await open_period(db, branch_a, day, day, status="Approved")
    await scheduled_transfer(client, token, driver["driver_id"], branch_b,
                             midnight + timedelta(hours=6))
    await db.execute(text("UPDATE core.branches SET timezoneid = 'America/Los_Angeles' "
                          "WHERE branchid = :b"), {"b": branch_a})
    await db.commit()
    live = [t for t in await load_driver_tenures(1, driver["driver_id"], db)
            if t.branch_id == branch_a]
    assert [t.last_day for t in live] == [day - timedelta(days=1)]  # the live days miss P
    return driver, branch_a, branch_b, day, period


async def _status(db, period) -> str:
    return (await db.execute(text(
        "SELECT status FROM payroll.payrollperiods WHERE payrollperiodid = :p"), {"p": period})
    ).scalar_one()


async def _race(engine, db, branch_a, period, send, *, finalize: bool):
    """Hold A's workflow lock, run the writer until it blocks, then commit (or not)."""
    holder = await engine.connect()
    task = None
    try:
        holder_pid = (await holder.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        await holder.execute(text("SELECT pg_advisory_xact_lock(1, :b)"), {"b": branch_a})
        task = asyncio.create_task(send())
        waiting = await _wait_until_blocked(db, task, holder_pid)
        assert "pg_advisory_xact_lock" in waiting, waiting
        if finalize:
            await holder.execute(text(
                "UPDATE payroll.payrollperiods SET status = 'Locked' WHERE payrollperiodid = :p"),
                {"p": period})
            await holder.commit()
        else:
            await holder.rollback()
        return await asyncio.wait_for(task, 30)
    finally:
        if task is not None and not task.done():
            task.cancel()
        await holder.close()


async def test_schedule_approve_waits_for_the_historical_branch_finalize(
    client, auth_token, direct_db, test_engine,
):
    driver, branch_a, _, day, period = await _scene(client, auth_token, direct_db)
    definition = await _definition(client, auth_token)
    schedule = await _pending(client, auth_token, driver["driver_id"], definition, str(day))
    await _expect(await _values(client, auth_token, schedule, definition, "20"), 200)

    response = await _race(
        test_engine, direct_db, branch_a, period, finalize=True,
        send=lambda: client.post(
            f"/compensation/driver-rate-schedules/{schedule}/approve", headers=auth(auth_token)))

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == FINALIZED
    assert response.json()["detail"]["payroll_period_ids"] == [period]
    assert await _status(direct_db, period) == "Locked"
    assert (await direct_db.execute(text(
        "SELECT status FROM payroll.driverrateschedules WHERE driverratescheduleid = :s"),
        {"s": schedule})).scalar_one() == "Pending"


async def test_schedule_approve_waits_and_then_succeeds_when_the_period_is_not_finalized(
    client, auth_token, direct_db, test_engine,
):
    driver, branch_a, _, day, period = await _scene(client, auth_token, direct_db)
    definition = await _definition(client, auth_token)
    schedule = await _pending(client, auth_token, driver["driver_id"], definition, str(day))
    await _expect(await _values(client, auth_token, schedule, definition, "20"), 200)

    response = await _race(
        test_engine, direct_db, branch_a, period, finalize=False,
        send=lambda: client.post(
            f"/compensation/driver-rate-schedules/{schedule}/approve", headers=auth(auth_token)))

    await _expect(response, 200)
    assert await _status(direct_db, period) == "Approved"


@pytest.mark.parametrize("finalize", [True, False], ids=["finalized", "still-approved"])
async def test_pay_rule_create_waits_for_the_historical_branch_finalize(
    client, auth_token, direct_db, test_engine, finalize,
):
    driver, branch_a, _, day, period = await _scene(client, auth_token, direct_db)

    response = await _race(
        test_engine, direct_db, branch_a, period, finalize=finalize,
        send=lambda: client.post("/payroll/driver-pay-rules", json={
            "driver_id": driver["driver_id"], "rule_type": "MinimumPay", "amount": "50",
            "effective_from": str(day)}, headers=auth(auth_token)))

    count = (await direct_db.execute(text(
        "SELECT count(*) FROM payroll.driverpayrules WHERE driverid = :d"),
        {"d": driver["driver_id"]})).scalar_one()
    if finalize:
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == FINALIZED
        assert count == 0
    else:
        await _expect(response, 201)
        assert count == 1


async def test_pay_rule_end_waits_for_the_historical_branch_finalize(
    client, auth_token, direct_db, test_engine,
):
    driver, branch_a, _, day, period = await _scene(client, auth_token, direct_db)
    created = await _expect(await client.post("/payroll/driver-pay-rules", json={
        "driver_id": driver["driver_id"], "rule_type": "MinimumPay", "amount": "50",
        "effective_from": str(day - timedelta(days=8))}, headers=auth(auth_token)), 201)
    rule = created.json()["driver_pay_rule_id"]

    response = await _race(
        test_engine, direct_db, branch_a, period, finalize=True,
        send=lambda: client.post(
            f"/payroll/driver-pay-rules/{rule}/end",
            json={"effective_to": str(day - timedelta(days=1))}, headers=auth(auth_token)))

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == FINALIZED
    assert (await direct_db.execute(text(
        "SELECT status FROM payroll.driverpayrules WHERE driverpayruleid = :r"),
        {"r": rule})).scalar_one() == "Active"
