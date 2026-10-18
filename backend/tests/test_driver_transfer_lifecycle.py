"""Scheduled Driver transfer lifecycle: cancel, reschedule, activation by time,
period roster re-capture and Driver Self ownership across assignments.

Time is never slept on. A Scheduled transfer "reaches" its EffectiveAt by moving the
whole materialized window into the past under the exact history-trigger suspension,
which is what the clock passing the instant looks like to every reader.
"""

import datetime
import uuid
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text

from tests.builders.access import create_user_with_role_token, get_company_role_id
from tests.builders.owned_scope import create_owned_branch
from tests.builders.workforce import create_driver_employee_record
from tests.builders.workforce_db import current_driver_assignment_id
from tests.db_state import DRIVER_ASSIGNMENT_HISTORY_TRIGGER, suspended_test_triggers


@pytest_asyncio.fixture(scope="session")
async def source_branch_id(session_client, auth_token, session_db_conn) -> int:
    return await create_owned_branch(session_db_conn, "DTL", "DTL source branch")


@pytest_asyncio.fixture(scope="session")
async def target_branch_id(session_client, auth_token, session_db_conn) -> int:
    return await create_owned_branch(session_db_conn, "DTL", "DTL target branch")


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sfx() -> str:
    return uuid.uuid4().hex[:10]


async def _db_now(db) -> datetime.datetime:
    return (await db.execute(text("SELECT NOW()"))).scalar_one()


async def _local_midnight(db, branch_id: int, day: str) -> datetime.datetime:
    return (await db.execute(text("""
        SELECT core.fn_LocalDateBoundary(b.TimeZoneId, CAST(:day AS DATE))
        FROM core.Branches b WHERE b.BranchID = :bid
    """), {"day": datetime.date.fromisoformat(day), "bid": branch_id})).scalar_one()


async def _create_driver(client, token: str, branch_id: int) -> dict:
    return await create_driver_employee_record(
        client, token, branch_id=branch_id, full_name=f"DTL Driver {_sfx()}",
        driver_number=f"DTL-{_sfx()}", hire_date="2020-01-01",
    )


async def _approved_request(
    client, token: str, driver_id: int, target_branch_id: int,
    effective_at: datetime.datetime | None,
) -> int:
    payload = {
        "driver_id": driver_id, "target_branch_id": target_branch_id,
        "initiated_by": "SourceBranch", "reason": "lifecycle",
    }
    if effective_at is not None:
        payload["effective_at"] = effective_at.isoformat()
    created = await client.post("/driver-transfers", json=payload, headers=auth(token))
    assert created.status_code == 201, created.text
    request_id = created.json()["transfer_request_id"]
    decided = await client.post(
        f"/driver-transfers/{request_id}/decide-target",
        json={"decision": "Approved"}, headers=auth(token),
    )
    assert decided.status_code == 200, decided.text
    return request_id


async def _complete(client, token: str, request_id: int) -> httpx.Response:
    return await client.post(
        f"/driver-transfers/{request_id}/complete", json={}, headers=auth(token),
    )


async def _scheduled(
    client, token: str, db, source_branch: int, target_branch: int, effective_at=None,
) -> dict:
    """An Employee/Driver on the source Branch with a Scheduled transfer to the target."""
    employee = await _create_driver(client, token, source_branch)
    driver_id = employee["driver"]["driver_id"]
    effective_at = effective_at or (await _db_now(db) + timedelta(days=40, minutes=7))
    request_id = await _approved_request(client, token, driver_id, target_branch, effective_at)
    completed = await _complete(client, token, request_id)
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["status"] == "Scheduled" and body["stored_status"] == "Scheduled"
    return {
        "employee_id": employee["employee_id"], "driver_id": driver_id,
        "request_id": request_id, "effective_at": effective_at,
        "source_id": body["source_driver_assignment_id"],
        "destination_id": body["destination_driver_assignment_id"],
    }


async def _assignment(db, assignment_id: int):
    return (await db.execute(text("""
        SELECT BranchID, EffectiveFromAt, EffectiveToAt, EndReason,
               TransferredFromAssignmentID, TransferredToAssignmentID
        FROM core.DriverAssignments WHERE DriverAssignmentID = :id
    """), {"id": assignment_id})).mappings().first()


async def _age_to_elapsed(db, request_id: int) -> None:
    """Make a Scheduled transfer's EffectiveAt a past instant (the clock reaching it)."""
    row = (await db.execute(text("""
        SELECT SourceDriverAssignmentID AS s, DestinationDriverAssignmentID AS d
        FROM core.DriverTransferRequests WHERE TransferRequestID = :id
    """), {"id": request_id})).mappings().one()
    async with suspended_test_triggers(db, (DRIVER_ASSIGNMENT_HISTORY_TRIGGER,)):
        await db.execute(text("""
            UPDATE core.DriverAssignments
            SET EffectiveFromAt = NOW() - INTERVAL '2 hours',
                EffectiveToAt = NOW() - INTERVAL '1 minute'
            WHERE DriverAssignmentID = :s
        """), {"s": row["s"]})
        await db.execute(text("""
            UPDATE core.DriverAssignments SET EffectiveFromAt = NOW() - INTERVAL '1 minute'
            WHERE DriverAssignmentID = :d
        """), {"d": row["d"]})
        await db.execute(text("""
            UPDATE core.DriverTransferRequests SET EffectiveAt = NOW() - INTERVAL '1 minute'
            WHERE TransferRequestID = :id
        """), {"id": request_id})


@pytest.mark.asyncio
async def test_cancel_scheduled_transfer_restores_source_and_removes_destination(
    session_client: httpx.AsyncClient, auth_token: str, source_branch_id: int,
    target_branch_id: int, direct_db,
):
    t = await _scheduled(session_client, auth_token, direct_db, source_branch_id, target_branch_id)
    assert (await _assignment(direct_db, t["source_id"]))["endreason"] == "Transferred"

    cancelled = await session_client.post(
        f"/driver-transfers/{t['request_id']}/cancel",
        json={"cancel_reason": "plans changed"}, headers=auth(auth_token),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "Cancelled"

    source = await _assignment(direct_db, t["source_id"])
    assert (source["effectivetoat"], source["endreason"], source["transferredtoassignmentid"]) == (
        None, None, None,
    )
    assert await _assignment(direct_db, t["destination_id"]) is None
    assert await current_driver_assignment_id(direct_db, t["driver_id"]) == t["source_id"]
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": t["employee_id"]})).scalar_one() == source_branch_id
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM audit.AuditLog
        WHERE ActionCode = 'DRIVER_TRANSFER_CANCELLED' AND EntityID = :id
    """), {"id": str(t["request_id"])})).scalar_one() == 1

    # The Driver is free to be scheduled again; the permanent identity never changed.
    again = await session_client.post("/driver-transfers", json={
        "driver_id": t["driver_id"], "target_branch_id": target_branch_id,
        "initiated_by": "SourceBranch",
        "effective_at": (t["effective_at"] + timedelta(days=1)).isoformat(),
    }, headers=auth(auth_token))
    assert again.status_code == 201, again.text
    assert again.json()["driver_id"] == t["driver_id"]


@pytest.mark.asyncio
async def test_reschedule_scheduled_transfer_moves_both_boundaries_in_place(
    session_client: httpx.AsyncClient, auth_token: str, source_branch_id: int,
    target_branch_id: int, direct_db,
):
    t = await _scheduled(session_client, auth_token, direct_db, source_branch_id, target_branch_id)
    moved_to = t["effective_at"] + timedelta(days=3, hours=1)

    rescheduled = await session_client.post(
        f"/driver-transfers/{t['request_id']}/reschedule",
        json={"effective_at": moved_to.isoformat()}, headers=auth(auth_token),
    )
    assert rescheduled.status_code == 200, rescheduled.text
    body = rescheduled.json()
    assert body["stored_status"] == "Scheduled"
    assert body["destination_driver_assignment_id"] == t["destination_id"]
    assert body["source_driver_assignment_id"] == t["source_id"]

    source = await _assignment(direct_db, t["source_id"])
    destination = await _assignment(direct_db, t["destination_id"])
    assert source["effectivetoat"] == moved_to == destination["effectivefromat"]
    assert source["endreason"] == "Transferred"
    assert source["transferredtoassignmentid"] == t["destination_id"]
    assert destination["transferredfromassignmentid"] == t["source_id"]
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM audit.AuditLog
        WHERE ActionCode = 'DRIVER_TRANSFER_RESCHEDULED' AND EntityID = :id
    """), {"id": str(t["request_id"])})).scalar_one() == 1


@pytest.mark.asyncio
async def test_reschedule_validation_rejects_past_and_pre_source_instants(
    session_client: httpx.AsyncClient, auth_token: str, source_branch_id: int,
    target_branch_id: int, direct_db,
):
    t = await _scheduled(session_client, auth_token, direct_db, source_branch_id, target_branch_id)
    past = await _db_now(direct_db) - timedelta(minutes=5)
    rejected = await session_client.post(
        f"/driver-transfers/{t['request_id']}/reschedule",
        json={"effective_at": past.isoformat()}, headers=auth(auth_token),
    )
    assert rejected.status_code == 422, rejected.text
    naive = await session_client.post(
        f"/driver-transfers/{t['request_id']}/reschedule",
        json={"effective_at": "2099-01-01T00:00:00"}, headers=auth(auth_token),
    )
    assert naive.status_code == 422
    # Nothing moved.
    assert (await _assignment(direct_db, t["source_id"]))["effectivetoat"] == t["effective_at"]


@pytest.mark.asyncio
async def test_reschedule_of_approved_request_changes_only_the_request(
    session_client: httpx.AsyncClient, auth_token: str, source_branch_id: int,
    target_branch_id: int, direct_db,
):
    employee = await _create_driver(session_client, auth_token, source_branch_id)
    driver_id = employee["driver"]["driver_id"]
    first = await _db_now(direct_db) + timedelta(days=50)
    request_id = await _approved_request(
        session_client, auth_token, driver_id, target_branch_id, first,
    )
    second = first + timedelta(days=2)
    response = await session_client.post(
        f"/driver-transfers/{request_id}/reschedule",
        json={"effective_at": second.isoformat()}, headers=auth(auth_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stored_status"] == "Approved"
    assert body["destination_driver_assignment_id"] is None
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM core.DriverAssignments WHERE DriverID = :id"
    ), {"id": driver_id})).scalar_one() == 1

    completed = await _complete(session_client, auth_token, request_id)
    assert completed.status_code == 200, completed.text
    assert datetime.datetime.fromisoformat(completed.json()["effective_at"]) == second


@pytest.mark.asyncio
async def test_scheduled_transfer_blocks_another_request_for_the_same_driver(
    session_client: httpx.AsyncClient, auth_token: str, source_branch_id: int,
    target_branch_id: int, hq_branch_id: int, direct_db,
):
    t = await _scheduled(session_client, auth_token, direct_db, source_branch_id, target_branch_id)
    second = await session_client.post("/driver-transfers", json={
        "driver_id": t["driver_id"], "target_branch_id": hq_branch_id,
        "initiated_by": "SourceBranch",
        "effective_at": (t["effective_at"] + timedelta(days=5)).isoformat(),
    }, headers=auth(auth_token))
    assert second.status_code in (409, 422), second.text


@pytest.mark.asyncio
async def test_elapsed_scheduled_transfer_reads_completed_and_is_final(
    session_client: httpx.AsyncClient, auth_token: str, source_branch_id: int,
    target_branch_id: int, direct_db,
):
    t = await _scheduled(session_client, auth_token, direct_db, source_branch_id, target_branch_id)
    assert await current_driver_assignment_id(direct_db, t["driver_id"]) == t["source_id"]

    await _age_to_elapsed(direct_db, t["request_id"])

    # No job ran: the status is a function of the stored row and the clock.
    got = await session_client.get(f"/driver-transfers/{t['request_id']}", headers=auth(auth_token))
    assert got.status_code == 200, got.text
    assert got.json()["status"] == "Completed"
    assert got.json()["stored_status"] == "Scheduled"
    assert await current_driver_assignment_id(direct_db, t["driver_id"]) == t["destination_id"]

    # An effective transfer can be neither cancelled nor rescheduled.
    cancel = await session_client.post(
        f"/driver-transfers/{t['request_id']}/cancel", json={}, headers=auth(auth_token),
    )
    assert cancel.status_code == 422, cancel.text
    resched = await session_client.post(
        f"/driver-transfers/{t['request_id']}/reschedule",
        json={"effective_at": (await _db_now(direct_db) + timedelta(days=1)).isoformat()},
        headers=auth(auth_token),
    )
    assert resched.status_code == 422, resched.text

    # The Employee read follows the effective assignment into the target Branch.
    employee = await session_client.get(
        f"/workforce/employees/{t['employee_id']}", headers=auth(auth_token),
    )
    assert employee.status_code == 200, employee.text
    assert employee.json()["branch_id"] == target_branch_id


@pytest.mark.asyncio
async def test_immediate_transfer_completes_at_database_now(
    session_client: httpx.AsyncClient, auth_token: str, source_branch_id: int,
    target_branch_id: int, direct_db,
):
    employee = await _create_driver(session_client, auth_token, source_branch_id)
    driver_id = employee["driver"]["driver_id"]
    # Make the source start well before NOW so an immediate boundary is valid.
    async with suspended_test_triggers(direct_db, (DRIVER_ASSIGNMENT_HISTORY_TRIGGER,)):
        await direct_db.execute(text("""
            UPDATE core.DriverAssignments SET EffectiveFromAt = NOW() - INTERVAL '1 day'
            WHERE DriverID = :id
        """), {"id": driver_id})
    request_id = await _approved_request(session_client, auth_token, driver_id, target_branch_id, None)
    completed = await _complete(session_client, auth_token, request_id)
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["status"] == body["stored_status"] == "Completed"
    assert body["completed_at_utc"] is not None and body["scheduled_at_utc"] is None

    source = await _assignment(direct_db, body["source_driver_assignment_id"])
    destination = await _assignment(direct_db, body["destination_driver_assignment_id"])
    assert source["effectivetoat"] == destination["effectivefromat"]
    assert source["endreason"] == "Transferred"
    assert await current_driver_assignment_id(direct_db, driver_id) == body["destination_driver_assignment_id"]
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee["employee_id"]})).scalar_one() == target_branch_id
    # Completed transfers cannot be cancelled.
    cancel = await session_client.post(
        f"/driver-transfers/{request_id}/cancel", json={}, headers=auth(auth_token),
    )
    assert cancel.status_code == 422


async def _roster(db, period_id: int, work_date: str) -> list[int]:
    return [row[0] for row in (await db.execute(text("""
        SELECT pde.DriverAssignmentID
        FROM payroll.PayrollPeriodDriverEligibility pde
        WHERE pde.PayrollPeriodID = :pid AND CAST(:wd AS DATE) = ANY(pde.EligibleWorkDates)
          AND core.fn_DriverAssignmentCoversWorkDate(
                pde.CompanyID, pde.DriverAssignmentID, pde.BranchID, CAST(:wd AS DATE))
        ORDER BY pde.DriverAssignmentID
    """), {"pid": period_id, "wd": datetime.date.fromisoformat(work_date)})).all()]


async def _open_period(db, branch_id: int, start: str, end: str) -> int:
    from app.payroll.eligibility import _create_period_driver_eligibility_rows

    period_id = (await db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, Status, PeriodCode, PeriodName, PeriodType, StartDate, EndDate)
        VALUES (1, :bid, 'Open', :code, 'DTL roster', 'Week', :start, :end)
        RETURNING PayrollPeriodID
    """), {
        "bid": branch_id, "code": f"DTL-{uuid.uuid4().hex[:12]}",
        "start": datetime.date.fromisoformat(start), "end": datetime.date.fromisoformat(end),
    })).scalar_one()
    await db.commit()
    await _create_period_driver_eligibility_rows(
        period_id, 1, branch_id, db, snapshot_source="Generated", freeze=True,
    )
    await db.commit()
    return period_id


@pytest.mark.asyncio
async def test_period_rosters_follow_schedule_reschedule_and_cancel(
    session_client: httpx.AsyncClient, auth_token: str, direct_db,
    session_db_conn,
):
    source_branch = await create_owned_branch(session_db_conn, "DTR", "DTL roster source")
    target_branch = await create_owned_branch(session_db_conn, "DTR", "DTL roster target")
    employee = await _create_driver(session_client, auth_token, source_branch)
    driver_id = employee["driver"]["driver_id"]
    source_period = await _open_period(direct_db, source_branch, "2097-01-24", "2097-01-30")
    target_period = await _open_period(direct_db, target_branch, "2097-01-24", "2097-01-30")

    request_id = await _approved_request(
        session_client, auth_token, driver_id, target_branch,
        await _local_midnight(direct_db, source_branch, "2097-01-27"),
    )
    completed = await _complete(session_client, auth_token, request_id)
    assert completed.status_code == 200, completed.text
    source_id = completed.json()["source_driver_assignment_id"]
    destination_id = completed.json()["destination_driver_assignment_id"]

    # Scheduled: source until 01-26, destination from 01-27.
    assert source_id in await _roster(direct_db, source_period, "2097-01-26")
    assert source_id not in await _roster(direct_db, source_period, "2097-01-27")
    assert destination_id not in await _roster(direct_db, target_period, "2097-01-26")
    assert destination_id in await _roster(direct_db, target_period, "2097-01-27")

    # Rescheduled one day later: the captured days move with the boundary.
    moved = await session_client.post(
        f"/driver-transfers/{request_id}/reschedule",
        json={"effective_at": (await _local_midnight(direct_db, source_branch, "2097-01-28")).isoformat()},
        headers=auth(auth_token),
    )
    assert moved.status_code == 200, moved.text
    assert source_id in await _roster(direct_db, source_period, "2097-01-27")
    assert source_id not in await _roster(direct_db, source_period, "2097-01-28")
    assert destination_id not in await _roster(direct_db, target_period, "2097-01-27")
    assert destination_id in await _roster(direct_db, target_period, "2097-01-28")

    # Cancelled: the source covers the whole period again and the destination left no trace.
    cancelled = await session_client.post(
        f"/driver-transfers/{request_id}/cancel", json={}, headers=auth(auth_token),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert source_id in await _roster(direct_db, source_period, "2097-01-30")
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollPeriodDriverEligibility
        WHERE DriverAssignmentID = :id
    """), {"id": destination_id})).scalar_one() == 0

    for period_id in (source_period, target_period):
        await session_client.patch(
            f"/payroll/periods/{period_id}/status",
            json={"status": "Cancelled"}, headers=auth(auth_token),
        )


@pytest.mark.asyncio
async def test_cancel_is_refused_when_destination_already_has_payroll_facts(
    session_client: httpx.AsyncClient, auth_token: str, source_branch_id: int,
    target_branch_id: int, direct_db,
):
    t = await _scheduled(session_client, auth_token, direct_db, source_branch_id, target_branch_id)
    period_id = await _open_period(direct_db, target_branch_id, "2098-03-02", "2098-03-08")
    try:
        # A draft line on the never-effective destination is a payroll fact.
        await direct_db.execute(text("""
            INSERT INTO payroll.PayrollDraftLines
                (PayrollPeriodID, CompanyID, BranchID, DriverAssignmentID,
                 LineType, WorkDate, Status, SourceType)
            VALUES (:pid, 1, :bid, :aid, 'HistoryTest', '2098-03-03', 'Active', 'Manual')
        """), {"pid": period_id, "bid": target_branch_id, "aid": t["destination_id"]})
        await direct_db.commit()
        cancel = await session_client.post(
            f"/driver-transfers/{t['request_id']}/cancel", json={}, headers=auth(auth_token),
        )
        assert cancel.status_code == 409, cancel.text
        assert cancel.json()["detail"]["code"] == "TRANSFER_DESTINATION_HAS_PAYROLL_FACTS"
        # Nothing was undone.
        assert (await _assignment(direct_db, t["source_id"]))["endreason"] == "Transferred"
        assert await _assignment(direct_db, t["destination_id"]) is not None
    finally:
        await direct_db.execute(text(
            "DELETE FROM payroll.PayrollDraftLines WHERE PayrollPeriodID = :pid"
        ), {"pid": period_id})
        await direct_db.commit()
        await session_client.patch(
            f"/payroll/periods/{period_id}/status",
            json={"status": "Cancelled"}, headers=auth(auth_token),
        )


@pytest.mark.asyncio
async def test_driver_self_ownership_spans_assignments_but_current_follows_the_clock(
    session_client: httpx.AsyncClient, auth_token: str, source_branch_id: int,
    target_branch_id: int, direct_db,
):
    from app.access.policy import require_self_owns_assignment, resolve_driver_self

    roles = await get_company_role_id(session_client, auth_token, "DRIVER")
    username = f"dtl_self_{_sfx()}"
    await create_user_with_role_token(
        session_client, auth_token, username, roles,
        scope_type="Self", driver_branch_id=source_branch_id,
    )
    user_id, employee_id = (await direct_db.execute(text(
        "SELECT UserID, EmployeeID FROM sec.Users WHERE Username = :u"
    ), {"u": username})).one()
    driver_id = (await direct_db.execute(text(
        "SELECT DriverID FROM core.Drivers WHERE EmployeeID = :e"
    ), {"e": employee_id})).scalar_one()
    own_first = await current_driver_assignment_id(direct_db, driver_id)

    request_id = await _approved_request(
        session_client, auth_token, driver_id, target_branch_id,
        await _db_now(direct_db) + timedelta(days=60),
    )
    completed = await _complete(session_client, auth_token, request_id)
    assert completed.status_code == 200, completed.text
    destination_id = completed.json()["destination_driver_assignment_id"]

    # Before the boundary: the effective assignment is the source; the pending one is owned
    # but not current.
    own = await resolve_driver_self(1, user_id, direct_db, target_driver_id=driver_id)
    assert own["driver_id"] == driver_id and own["assignment"]["driverassignmentid"] == own_first
    assert (await require_self_owns_assignment(1, user_id, destination_id, direct_db))[
        "target_assignment_id"] == destination_id
    with pytest.raises(HTTPException) as pending:
        await require_self_owns_assignment(1, user_id, destination_id, direct_db, current_only=True)
    assert pending.value.status_code == 403

    await _age_to_elapsed(direct_db, request_id)

    # After: the destination is current; the closed source is still owned history.
    own = await resolve_driver_self(1, user_id, direct_db)
    assert own["assignment"]["driverassignmentid"] == destination_id
    assert (await require_self_owns_assignment(1, user_id, own_first, direct_db))[
        "target_assignment_id"] == own_first
    with pytest.raises(HTTPException) as closed:
        await require_self_owns_assignment(1, user_id, own_first, direct_db, current_only=True)
    assert closed.value.status_code == 403

    # Another Driver's assignment and an unknown id are the same 403; so is another Driver id.
    other = await _create_driver(session_client, auth_token, source_branch_id)
    other_assignment = other["current_or_pending_assignment"]["driver_assignment_id"]
    for foreign in (other_assignment, 2_000_000_000):
        with pytest.raises(HTTPException) as denied:
            await require_self_owns_assignment(1, user_id, foreign, direct_db)
        assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as other_driver:
        await resolve_driver_self(
            1, user_id, direct_db, target_driver_id=other["driver"]["driver_id"],
        )
    assert other_driver.value.status_code == 403
