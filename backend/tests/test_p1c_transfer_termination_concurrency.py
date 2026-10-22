"""Real concurrent request regressions for P1c Employee lifecycle locks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def suffix() -> str:
    return uuid4().hex[:10]


async def current_instant(db) -> datetime:
    return (await db.execute(text("SELECT NOW()"))).scalar_one()


async def branch_today(db, branch_id: int):
    return (await db.execute(
        text("SELECT core.fn_BranchToday(1, :branch_id)"), {"branch_id": branch_id},
    )).scalar_one()


async def create_driver(client, token: str, branch_id: int) -> tuple[int, int, int]:
    """Create an Employee with a Driver: (EmployeeID, permanent DriverID, first DriverAssignmentID)."""
    response = await client.post(
        "/workforce/employees",
        json={
            "branch_id": branch_id,
            "full_name": f"P1C Race {suffix()}",
            "hire_date": "2020-01-01",
            "driver": {
                "driver_number": f"P1C-RACE-{suffix()}",
            },
        },
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    return (
        data["employee_id"],
        data["driver"]["driver_id"],
        data["current_or_pending_assignment"]["driver_assignment_id"],
    )


async def create_transfer(
    client, token: str, driver_id: int, target_branch_id: int, effective_at: datetime,
):
    response = await client.post(
        "/driver-transfers",
        json={
            "driver_id": driver_id,
            "target_branch_id": target_branch_id,
            "effective_at": effective_at.isoformat(),
            "initiated_by": "SourceBranch",
            "reason": "P1c race",
        },
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["transfer_request_id"]


async def approve(client, token: str, request_id: int) -> None:
    response = await client.post(
        f"/driver-transfers/{request_id}/decide-target",
        json={"decision": "Approved"},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_two_completions_create_exactly_one_destination(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    effective_at = await current_instant(direct_db) + timedelta(days=30, hours=3, minutes=17)
    employee_id, driver_id, _ = await create_driver(client, auth_token, paytest_branch_id)
    request_id = await create_transfer(client, auth_token, driver_id, hq_branch_id, effective_at)
    await approve(client, auth_token, request_id)

    async def complete():
        return await client.post(
            f"/driver-transfers/{request_id}/complete", json={}, headers=auth(auth_token),
        )

    first, second = await asyncio.gather(complete(), complete())
    assert sorted([first.status_code, second.status_code]) == [200, 422]
    # One permanent Driver, exactly two assignments (source and one destination).
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM core.Drivers WHERE EmployeeID = :employee_id
    """), {"employee_id": employee_id})).scalar_one() == 1
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM core.DriverAssignments WHERE EmployeeID = :employee_id
    """), {"employee_id": employee_id})).scalar_one() == 2
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM core.DriverTransferRequests
        WHERE TransferRequestID = :request_id AND Status = 'Scheduled'
    """), {"request_id": request_id})).scalar_one() == 1


@pytest.mark.asyncio
async def test_two_transfer_creations_for_employee_serialize(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int,
    direct_db,
):
    effective_at = await current_instant(direct_db) + timedelta(days=45, hours=2, minutes=13)
    _, driver_id, _ = await create_driver(client, auth_token, paytest_branch_id)

    async def create(target):
        return await client.post(
            "/driver-transfers",
            json={
                "driver_id": driver_id,
                "target_branch_id": target,
                "effective_at": effective_at.isoformat(),
                "initiated_by": "SourceBranch",
                "reason": "Competing transfer creation",
            },
            headers=auth(auth_token),
        )

    first, second = await asyncio.gather(create(hq_branch_id), create(hq_branch_id))
    assert sorted([first.status_code, second.status_code]) == [201, 422]
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM core.DriverTransferRequests
        WHERE DriverID = :driver_id
          AND core.fn_DriverTransferStatus(Status, EffectiveAt) NOT IN ('Completed', 'Cancelled', 'Rejected')
    """), {"driver_id": driver_id})).scalar_one() == 1


@pytest.mark.asyncio
async def test_transfer_completion_and_termination_serialize_without_partial_assignments(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    current_date = await branch_today(direct_db, paytest_branch_id)
    employee_id, driver_id, _ = await create_driver(client, auth_token, paytest_branch_id)
    request_id = await create_transfer(
        client, auth_token, driver_id, hq_branch_id,
        await current_instant(direct_db) + timedelta(days=25, hours=1, minutes=19),
    )
    await approve(client, auth_token, request_id)

    async def complete():
        return await client.post(
            f"/driver-transfers/{request_id}/complete", json={}, headers=auth(auth_token),
        )

    async def terminate():
        return await client.post(
            f"/workforce/employees/{employee_id}/terminate",
            json={"termination_date": current_date.isoformat(), "reason": "Concurrent close"},
            headers=auth(auth_token),
        )

    completion, termination = await asyncio.gather(complete(), terminate())
    assert termination.status_code == 200, termination.text
    assert completion.status_code in (200, 422)
    rows = (await direct_db.execute(text("""
        SELECT DriverAssignmentID, EffectiveFromAt, EffectiveToAt, EndReason
        FROM core.DriverAssignments WHERE EmployeeID = :employee_id ORDER BY DriverAssignmentID
    """), {"employee_id": employee_id})).mappings().all()
    assert len(rows) in (1, 2)
    assert all(
        row["effectivetoat"] is None
        or row["effectivetoat"] > row["effectivefromat"]
        or (row["endreason"] == "Terminated"
            and row["effectivetoat"] == row["effectivefromat"])
        for row in rows
    )
    assert (await direct_db.execute(text(
        "SELECT EmploymentStatus FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one() == "Terminated"
    if len(rows) == 2:
        now = await current_instant(direct_db)
        future = next(row for row in rows if row["effectivefromat"] > now)
        assert (future["endreason"], future["effectivetoat"]) == (
            "Terminated", future["effectivefromat"],
        )
        # A Scheduled transfer at or after the termination cap is cancelled with it.
        assert (await direct_db.execute(text(
            "SELECT Status FROM core.DriverTransferRequests WHERE TransferRequestID = :id"
        ), {"id": request_id})).scalar_one() == "Cancelled"


@pytest.mark.asyncio
async def test_driver_edit_and_transfer_completion_do_not_lose_update(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    employee_id, driver_id, _ = await create_driver(client, auth_token, paytest_branch_id)
    original_number = (await direct_db.execute(text(
        "SELECT DriverNumber FROM core.Drivers WHERE DriverID = :id"
    ), {"id": driver_id})).scalar_one()
    request_id = await create_transfer(
        client, auth_token, driver_id, hq_branch_id,
        await current_instant(direct_db) + timedelta(days=30, hours=4, minutes=11),
    )
    await approve(client, auth_token, request_id)
    new_number = f"P1C-EDIT-{suffix()}"

    async def edit():
        return await client.patch(
            f"/workforce/drivers/{driver_id}", json={"driver_number": new_number}, headers=auth(auth_token),
        )

    async def complete():
        return await client.post(
            f"/driver-transfers/{request_id}/complete", json={}, headers=auth(auth_token),
        )

    edit_response, complete_response = await asyncio.gather(edit(), complete())
    assert complete_response.status_code == 200, complete_response.text
    assert edit_response.status_code in (200, 422)
    stored = (await direct_db.execute(text(
        "SELECT DriverNumber FROM core.Drivers WHERE DriverID = :id"
    ), {"id": driver_id})).scalar_one()
    if edit_response.status_code == 200:
        assert stored == new_number
    else:
        assert stored == original_number


@pytest.mark.asyncio
async def test_two_termination_attempts_apply_one_lifecycle_transition(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    today_date = await branch_today(direct_db, hq_branch_id)
    employee_id, _, assignment_id = await create_driver(client, auth_token, hq_branch_id)

    async def terminate(reason):
        return await client.post(
            f"/workforce/employees/{employee_id}/terminate",
            json={"termination_date": today_date.isoformat(), "reason": reason},
            headers=auth(auth_token),
        )

    first, second = await asyncio.gather(terminate("Race A"), terminate("Race B"))
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM audit.AuditLog WHERE EntitySchema = 'core'
          AND EntityName = 'Employees' AND EntityID = :id
          AND ActionCode = 'DRIVER_EMPLOYEE_TERMINATED'
    """), {"id": str(employee_id)})).scalar_one() == 1
    assert (await direct_db.execute(text(
        "SELECT EndReason FROM core.DriverAssignments WHERE DriverAssignmentID = :id"
    ), {"id": assignment_id})).scalar_one() == "Terminated"


@pytest.mark.asyncio
async def test_stale_target_decision_and_cancel_cannot_overwrite_terminal_state(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    request_id = await create_transfer(
        client, auth_token, (await create_driver(client, auth_token, paytest_branch_id))[1],
        hq_branch_id, await current_instant(direct_db) + timedelta(days=30, hours=2, minutes=37),
    )

    async def reject():
        return await client.post(
            f"/driver-transfers/{request_id}/decide-target",
            json={"decision": "Rejected", "decision_notes": "Race"},
            headers=auth(auth_token),
        )

    async def cancel():
        return await client.post(
            f"/driver-transfers/{request_id}/cancel",
            json={"cancel_reason": "Race"}, headers=auth(auth_token),
        )

    first, second = await asyncio.gather(reject(), cancel())
    assert sorted([first.status_code, second.status_code]) == [200, 422]
    status = (await direct_db.execute(text(
        "SELECT Status FROM core.DriverTransferRequests WHERE TransferRequestID = :id"
    ), {"id": request_id})).scalar_one()
    assert status in {"Rejected", "Cancelled"}


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_path", ["create", "completion"])
async def test_driver_no_key_update_lock_allows_snapshot_foreign_key_capture(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int,
    hq_branch_id: int, direct_db, test_engine, lock_path: str,
):
    """Create and completion Driver locks must remain compatible with FK KEY SHARE."""
    from app.payroll.eligibility import _create_period_driver_eligibility_rows
    from app.transfer.service import _lock_employee_for_transfer_create, _lock_transfer_lifecycle

    employee_id, driver_id, assignment_id = await create_driver(client, auth_token, paytest_branch_id)
    branch_today_date = await branch_today(direct_db, paytest_branch_id)
    company_id = (await direct_db.execute(text(
        "SELECT companyid FROM core.branches WHERE branchid = :bid"
    ), {"bid": paytest_branch_id})).scalar_one()
    period_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate, Status)
        VALUES (:cid, :bid, :code, 'Driver FK lock compatibility', 'Custom', :start, :end, 'Cancelled')
        RETURNING PayrollPeriodID
    """), {"cid": company_id, "bid": paytest_branch_id, "code": f"P1C-{suffix()}",
          "start": branch_today_date, "end": branch_today_date + timedelta(days=30)})).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriodEligibilitySnapshots
            (PayrollPeriodID, CompanyID, BranchID, SnapshotSource)
        VALUES (:pid, :cid, :bid, 'Generated')
    """), {"pid": period_id, "cid": company_id, "bid": paytest_branch_id})

    transfer_request_id = None
    if lock_path == "completion":
        transfer_request_id = await create_transfer(
            client, auth_token, driver_id, hq_branch_id,
            await current_instant(direct_db) + timedelta(days=30),
        )

    async with test_engine.connect() as lock_conn:
        tx = await lock_conn.begin()
        if lock_path == "create":
            await _lock_employee_for_transfer_create(driver_id, company_id, lock_conn)
        else:
            await _lock_transfer_lifecycle(transfer_request_id, company_id, lock_conn)
        try:
            async with test_engine.begin() as capture_conn:
                await asyncio.wait_for(
                    _create_period_driver_eligibility_rows(
                        period_id, company_id, paytest_branch_id, capture_conn,
                        snapshot_source="Generated", freeze=True, only_driver_assignment_id=assignment_id,
                    ),
                    timeout=5,
                )
        finally:
            await tx.rollback()

    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodID = :pid AND DriverAssignmentID = :aid
    """), {"pid": period_id, "aid": assignment_id})).scalar_one() == 1
