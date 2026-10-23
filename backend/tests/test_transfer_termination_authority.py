"""Termination authority, wall-clock decisions and Cancelled-period handling for transfers.

Business rule: employment termination is authoritative over future-effective transfers.
A transfer whose EffectiveAt is at or after the source's termination cutoff is Cancelled and
its destination assignment never becomes effective (it is disposed of, not kept as an empty
window); the source ends at its cutoff as Terminated; no later cancel or reschedule can
reopen it. Decisions use one wall-clock instant read after every lock is held.
"""

import asyncio
from datetime import timedelta

import psycopg2
import pytest
from sqlalchemy import text

from app.workforce.effective import resolve_effective_driver_assignment
from tests.builders.transfer_scenarios import (
    approved_transfer,
    auth,
    branch_today,
    db_now,
    local_boundary,
    make_branch,
    make_driver,
    open_period,
    scheduled_transfer,
)
from tests.target_seed import seed_period_definition, seed_target_line

pytestmark = pytest.mark.asyncio


async def _terminate(client, token, employee_id, day):
    return await client.post(
        f"/workforce/employees/{employee_id}/terminate",
        json={"termination_date": day.isoformat(), "reason": "Scenario termination"},
        headers=auth(token),
    )


async def _assignment(db, assignment_id):
    return (await db.execute(text("""
        SELECT EndReason, EffectiveFromAt, EffectiveToAt, TransferredToAssignmentID
        FROM core.DriverAssignments WHERE DriverAssignmentID = :id
    """), {"id": assignment_id})).mappings().first()


async def _request(db, request_id):
    return (await db.execute(text("""
        SELECT Status, DestinationDriverAssignmentID, CancelReason
        FROM core.DriverTransferRequests WHERE TransferRequestID = :id
    """), {"id": request_id})).mappings().one()


async def test_a_surviving_scheduled_transfer_cannot_be_cancelled_or_rescheduled_after_termination(
    client, auth_token, direct_db,
):
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    effective_at = (await local_boundary(direct_db, source_branch, today + timedelta(days=1))
                    + timedelta(hours=12))
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch, effective_at)

    # The termination cutoff (end of tomorrow) is after the transfer: the transfer survives.
    terminated = await _terminate(client, auth_token, driver["employee_id"], today + timedelta(days=1))
    assert terminated.status_code == 200, terminated.text
    assert terminated.json()["cancelled_transfer_request_ids"] == []
    cutoff = await local_boundary(direct_db, target_branch, today + timedelta(days=2))
    destination = await _assignment(direct_db, transfer["destination_id"])
    assert (destination["endreason"], destination["effectivetoat"]) == ("Terminated", cutoff)

    cancel = await client.post(
        f"/driver-transfers/{transfer['request_id']}/cancel", json={}, headers=auth(auth_token),
    )
    assert cancel.status_code == 409, cancel.text
    assert cancel.json()["detail"]["code"] == "TRANSFER_EMPLOYEE_TERMINATED"
    reschedule = await client.post(
        f"/driver-transfers/{transfer['request_id']}/reschedule",
        json={"effective_at": (effective_at + timedelta(hours=1)).isoformat()},
        headers=auth(auth_token),
    )
    assert reschedule.status_code == 409, reschedule.text
    assert reschedule.json()["detail"]["code"] == "TRANSFER_EMPLOYEE_TERMINATED"

    # Nothing was reopened or moved: the transfer still takes effect at its EffectiveAt.
    assert (await _request(direct_db, transfer["request_id"]))["status"] == "Scheduled"
    source = await _assignment(direct_db, transfer["source_id"])
    assert (source["endreason"], source["effectivetoat"]) == ("Transferred", effective_at)
    destination = await _assignment(direct_db, transfer["destination_id"])
    assert (destination["endreason"], destination["effectivetoat"]) == ("Terminated", cutoff)
    assert destination["effectivefromat"] == effective_at
    assert (await resolve_effective_driver_assignment(
        1, driver["driver_id"], effective_at + timedelta(minutes=1), direct_db,
    ))["driverassignmentid"] == transfer["destination_id"]
    assert await resolve_effective_driver_assignment(1, driver["driver_id"], cutoff, direct_db) is None


async def test_termination_disposes_of_a_later_timezone_destination_instead_of_keeping_a_window(
    client, auth_token, direct_db,
):
    """The destination Branch's local cutoff is later than the source's: HEAD kept a live window."""
    source_branch = await make_branch(direct_db, "UTC")
    target_branch = await make_branch(direct_db, "America/Los_Angeles")
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    source_cutoff = await local_boundary(direct_db, source_branch, today + timedelta(days=3))
    effective_at = source_cutoff + timedelta(hours=1)
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch, effective_at)
    target_cutoff = await local_boundary(direct_db, target_branch, today + timedelta(days=3))
    assert effective_at < target_cutoff  # the destination window would be non-empty

    terminated = await _terminate(client, auth_token, driver["employee_id"], today + timedelta(days=2))
    assert terminated.status_code == 200, terminated.text
    assert terminated.json()["cancelled_transfer_request_ids"] == [transfer["request_id"]]

    request = await _request(direct_db, transfer["request_id"])
    assert request["status"] == "Cancelled"
    assert request["destinationdriverassignmentid"] is None
    assert "terminated" in request["cancelreason"].lower()
    assert await _assignment(direct_db, transfer["destination_id"]) is None
    source = await _assignment(direct_db, transfer["source_id"])
    assert (source["endreason"], source["effectivetoat"], source["transferredtoassignmentid"]) == (
        "Terminated", source_cutoff, None,
    )
    for offset in (0, 1, 4, 7, 12):
        assert await resolve_effective_driver_assignment(
            1, driver["driver_id"], effective_at + timedelta(hours=offset), direct_db,
        ) is None, f"an assignment is effective {offset}h after the cancelled EffectiveAt"
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM core.DriverAssignments WHERE DriverID = :id"
    ), {"id": driver["driver_id"]})).scalar_one() == 1


async def test_termination_refuses_when_the_disposed_destination_already_has_payroll_facts(
    client, auth_token, direct_db,
):
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    effective_at = await local_boundary(direct_db, source_branch, today + timedelta(days=5))
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch, effective_at)
    period_id = await open_period(
        direct_db, target_branch, today + timedelta(days=5), today + timedelta(days=9))
    definition = await seed_period_definition(direct_db, period_id=period_id, branch_id=target_branch)
    await seed_target_line(
        direct_db, period_id=period_id, branch_id=target_branch,
        driver_assignment_id=transfer["destination_id"], definition=definition,
        quantity=1, work_date=today + timedelta(days=6))
    await direct_db.commit()

    refused = await _terminate(client, auth_token, driver["employee_id"], today + timedelta(days=1))
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "TRANSFER_DESTINATION_HAS_PAYROLL_FACTS"
    assert (await direct_db.execute(text(
        "SELECT EmploymentStatus FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": driver["employee_id"]})).scalar_one() == "Active"
    assert (await _request(direct_db, transfer["request_id"]))["status"] == "Scheduled"
    assert await _assignment(direct_db, transfer["destination_id"]) is not None


async def test_cancel_that_waited_for_the_lock_while_the_transfer_took_effect_is_refused(
    client, auth_token, direct_db, pg_instance,
):
    """HEAD judged the request by its transaction start and cancelled an effective transfer."""
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    effective_at = (await db_now(direct_db)) + timedelta(seconds=10)
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch, effective_at)

    blocker = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT 1 FROM core.employees WHERE employeeid = %s FOR UPDATE",
                           (driver["employee_id"],))
        cancel = asyncio.create_task(client.post(
            f"/driver-transfers/{transfer['request_id']}/cancel", json={}, headers=auth(auth_token),
        ))
        await asyncio.sleep(1.0)
        assert not cancel.done(), "the cancel must be waiting on the Employee lock"
        remaining = (effective_at - await db_now(direct_db)).total_seconds()
        await asyncio.sleep(max(remaining, 0) + 0.7)
        blocker.commit()
        response = await asyncio.wait_for(cancel, timeout=20)
    finally:
        blocker.close()

    assert response.status_code == 422, response.text
    assert "already 'Completed'" in response.json()["detail"]
    assert (await _request(direct_db, transfer["request_id"]))["status"] == "Scheduled"
    assert await _assignment(direct_db, transfer["destination_id"]) is not None
    assert (await _assignment(direct_db, transfer["source_id"]))["endreason"] == "Transferred"


async def test_reschedule_that_waited_for_the_lock_while_the_transfer_took_effect_is_refused(
    client, auth_token, direct_db, pg_instance,
):
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    effective_at = (await db_now(direct_db)) + timedelta(seconds=10)
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch, effective_at)

    blocker = psycopg2.connect(client_encoding="utf-8", **pg_instance.dsn())
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT 1 FROM core.employees WHERE employeeid = %s FOR UPDATE",
                           (driver["employee_id"],))
        reschedule = asyncio.create_task(client.post(
            f"/driver-transfers/{transfer['request_id']}/reschedule",
            json={"effective_at": (effective_at + timedelta(days=2)).isoformat()},
            headers=auth(auth_token),
        ))
        await asyncio.sleep(1.0)
        assert not reschedule.done()
        remaining = (effective_at - await db_now(direct_db)).total_seconds()
        await asyncio.sleep(max(remaining, 0) + 0.7)
        blocker.commit()
        response = await asyncio.wait_for(reschedule, timeout=20)
    finally:
        blocker.close()

    assert response.status_code == 422, response.text
    assert (await _assignment(direct_db, transfer["source_id"]))["effectivetoat"] == effective_at


async def test_an_unrelated_cancelled_period_never_blocks_cancel_or_reschedule(
    client, auth_token, direct_db,
):
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    effective_at = await local_boundary(direct_db, source_branch, today + timedelta(days=6))
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch, effective_at)
    # A Cancelled period of each Branch overlaps the affected interval and carries a roster.
    await open_period(direct_db, source_branch, today + timedelta(days=4),
                      today + timedelta(days=8), status="Cancelled")
    await open_period(direct_db, target_branch, today + timedelta(days=4),
                      today + timedelta(days=8), status="Cancelled")

    moved = effective_at + timedelta(days=1)
    reschedule = await client.post(
        f"/driver-transfers/{transfer['request_id']}/reschedule",
        json={"effective_at": moved.isoformat()}, headers=auth(auth_token),
    )
    assert reschedule.status_code == 200, reschedule.text
    cancel = await client.post(
        f"/driver-transfers/{transfer['request_id']}/cancel", json={}, headers=auth(auth_token),
    )
    assert cancel.status_code == 200, cancel.text
    assert await _assignment(direct_db, transfer["destination_id"]) is None
    source = await _assignment(direct_db, transfer["source_id"])
    assert (source["endreason"], source["effectivetoat"]) == (None, None)


async def test_a_cancelled_period_holding_the_destination_roster_does_not_block_disposal(
    client, auth_token, direct_db,
):
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    effective_at = await local_boundary(direct_db, source_branch, today + timedelta(days=3))
    # The destination Branch's period exists before the transfer is scheduled, so the
    # destination joins its roster; the period is then cancelled.
    period_id = await open_period(
        direct_db, target_branch, today + timedelta(days=2), today + timedelta(days=8))
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch, effective_at)
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodID = :period_id AND DriverAssignmentID = :id
    """), {"period_id": period_id, "id": transfer["destination_id"]})).scalar_one() == 1
    await direct_db.execute(text(
        "UPDATE payroll.PayrollPeriods SET Status = 'Cancelled' WHERE PayrollPeriodID = :id"
    ), {"id": period_id})
    await direct_db.commit()

    cancel = await client.post(
        f"/driver-transfers/{transfer['request_id']}/cancel", json={}, headers=auth(auth_token),
    )
    assert cancel.status_code == 200, cancel.text
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM payroll.PayrollPeriodDriverEligibility WHERE DriverAssignmentID = :id"
    ), {"id": transfer["destination_id"]})).scalar_one() == 0
    assert await _assignment(direct_db, transfer["destination_id"]) is None


async def test_a_review_stage_period_still_blocks_cancel_and_leaves_everything_unchanged(
    client, auth_token, direct_db,
):
    """Only Cancelled periods stop blocking; a period in review keeps the 409."""
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    effective_at = await local_boundary(direct_db, source_branch, today + timedelta(days=6))
    transfer = await scheduled_transfer(
        client, auth_token, driver["driver_id"], target_branch, effective_at)
    await open_period(direct_db, source_branch, today + timedelta(days=4),
                      today + timedelta(days=8), status="InReview")
    cancel = await client.post(
        f"/driver-transfers/{transfer['request_id']}/cancel", json={}, headers=auth(auth_token),
    )
    assert cancel.status_code == 409, cancel.text
    assert cancel.json()["detail"]["code"] == "DRIVER_ASSIGNMENT_PERIOD_NOT_EDITABLE"
    assert (await _request(direct_db, transfer["request_id"]))["status"] == "Scheduled"
    assert await _assignment(direct_db, transfer["destination_id"]) is not None


async def test_complete_refuses_to_orphan_source_facts_after_the_boundary(
    client, auth_token, direct_db,
):
    """HEAD closed the source over WorkDates that already carried payroll source."""
    source_branch, target_branch = await make_branch(direct_db), await make_branch(direct_db)
    driver = await make_driver(client, auth_token, source_branch)
    today = await branch_today(direct_db, source_branch)
    period_id = await open_period(
        direct_db, source_branch, today, today + timedelta(days=9))
    definition = await seed_period_definition(direct_db, period_id=period_id, branch_id=source_branch)
    await seed_target_line(
        direct_db, period_id=period_id, branch_id=source_branch,
        driver_assignment_id=driver["assignment_id"], definition=definition,
        quantity=1, work_date=today + timedelta(days=5))
    await direct_db.commit()

    effective_at = await local_boundary(direct_db, source_branch, today + timedelta(days=3))
    request_id = await approved_transfer(
        client, auth_token, driver["driver_id"], target_branch, effective_at)
    scheduled = await client.post(
        f"/driver-transfers/{request_id}/complete", json={}, headers=auth(auth_token))
    assert scheduled.status_code == 409, scheduled.text
    assert scheduled.json()["detail"]["code"] == "DRIVER_ASSIGNMENT_PAYROLL_FACTS_UNCOVERED"
    # The refusal rolled everything back: still approved, source still open, no destination.
    assert (await _request(direct_db, request_id))["status"] == "Approved"
    assert (await _assignment(direct_db, driver["assignment_id"]))["effectivetoat"] is None
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM core.DriverAssignments WHERE DriverID = :id"
    ), {"id": driver["driver_id"]})).scalar_one() == 1

    # An immediate completion removes the same future coverage and is refused the same way.
    cancelled = await client.post(
        f"/driver-transfers/{request_id}/cancel", json={}, headers=auth(auth_token))
    assert cancelled.status_code == 200, cancelled.text
    second = await approved_transfer(client, auth_token, driver["driver_id"], target_branch, None)
    immediate = await client.post(
        f"/driver-transfers/{second}/complete", json={}, headers=auth(auth_token))
    assert immediate.status_code == 409, immediate.text
    assert immediate.json()["detail"]["code"] == "DRIVER_ASSIGNMENT_PAYROLL_FACTS_UNCOVERED"
