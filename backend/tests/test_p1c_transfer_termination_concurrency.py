"""Real concurrent request regressions for P1c Employee lifecycle locks."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def suffix() -> str:
    return uuid4().hex[:10]


async def today(db) -> date:
    return (await db.execute(text("SELECT core.fn_CompanyToday(1)"))).scalar_one()


async def create_driver(client, token: str, branch_id: int) -> tuple[int, int]:
    response = await client.post(
        "/workforce/employees",
        json={
            "branch_id": branch_id,
            "full_name": f"P1C Race {suffix()}",
            "hire_date": "2020-01-01",
            "driver_profile": {
                "driver_code": f"P1C-RACE-{suffix()}",
                "effective_from": "2020-01-01",
            },
        },
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    data = response.json()
    return data["employee_id"], data["current_or_pending_driver"]["driver_id"]


async def create_transfer(client, token: str, driver_id: int, target_branch_id: int, effective_date: date):
    response = await client.post(
        "/driver-transfers",
        json={
            "driver_id": driver_id,
            "target_branch_id": target_branch_id,
            "effective_date": effective_date.isoformat(),
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
    effective_date = await today(direct_db) + timedelta(days=30)
    employee_id, driver_id = await create_driver(client, auth_token, paytest_branch_id)
    request_id = await create_transfer(client, auth_token, driver_id, hq_branch_id, effective_date)
    await approve(client, auth_token, request_id)

    async def complete():
        return await client.post(
            f"/driver-transfers/{request_id}/complete", json={}, headers=auth(auth_token),
        )

    first, second = await asyncio.gather(complete(), complete())
    assert sorted([first.status_code, second.status_code]) == [200, 422]
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM core.Drivers WHERE EmployeeID = :employee_id
    """), {"employee_id": employee_id})).scalar_one() == 2
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM core.DriverTransferRequests
        WHERE TransferRequestID = :request_id AND Status = 'Completed'
    """), {"request_id": request_id})).scalar_one() == 1


@pytest.mark.asyncio
async def test_two_transfer_creations_for_employee_serialize(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int,
    direct_db,
):
    effective_date = await today(direct_db) + timedelta(days=45)
    _, driver_id = await create_driver(client, auth_token, paytest_branch_id)

    async def create(target):
        return await client.post(
            "/driver-transfers",
            json={
                "driver_id": driver_id,
                "target_branch_id": target,
                "effective_date": effective_date.isoformat(),
                "initiated_by": "SourceBranch",
                "reason": "Competing transfer creation",
            },
            headers=auth(auth_token),
        )

    first, second = await asyncio.gather(create(hq_branch_id), create(hq_branch_id))
    assert sorted([first.status_code, second.status_code]) == [201, 422]
    assert (await direct_db.execute(text("""
        SELECT COUNT(*) FROM core.DriverTransferRequests
        WHERE DriverID = :driver_id AND Status NOT IN ('Completed', 'Cancelled', 'Rejected')
    """), {"driver_id": driver_id})).scalar_one() == 1


@pytest.mark.asyncio
async def test_transfer_completion_and_termination_serialize_without_partial_profiles(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    current_date = await today(direct_db)
    employee_id, driver_id = await create_driver(client, auth_token, paytest_branch_id)
    request_id = await create_transfer(
        client, auth_token, driver_id, hq_branch_id, current_date + timedelta(days=25),
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
        SELECT DriverID, EffectiveFrom, EffectiveTo, DriverStatus
        FROM core.Drivers WHERE EmployeeID = :employee_id ORDER BY DriverID
    """), {"employee_id": employee_id})).mappings().all()
    assert len(rows) in (1, 2)
    assert all(
        row["effectiveto"] is None
        or row["effectiveto"] >= row["effectivefrom"]
        or (row["driverstatus"] == "Terminated"
            and row["effectiveto"] == row["effectivefrom"] - timedelta(days=1))
        for row in rows
    )
    assert (await direct_db.execute(text(
        "SELECT EmploymentStatus FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one() == "Terminated"
    if len(rows) == 2:
        future = next(row for row in rows if row["effectivefrom"] > current_date)
        assert (future["driverstatus"], future["effectiveto"]) == (
            "Terminated", future["effectivefrom"] - timedelta(days=1),
        )


@pytest.mark.asyncio
async def test_driver_edit_and_transfer_completion_do_not_lose_update(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    today_date = await today(direct_db)
    employee_id, driver_id = await create_driver(client, auth_token, paytest_branch_id)
    original_code = (await direct_db.execute(text(
        "SELECT DriverCode FROM core.Drivers WHERE DriverID = :id"
    ), {"id": driver_id})).scalar_one()
    request_id = await create_transfer(
        client, auth_token, driver_id, hq_branch_id, today_date + timedelta(days=30),
    )
    await approve(client, auth_token, request_id)
    new_code = f"P1C-EDIT-{suffix()}"

    async def edit():
        return await client.patch(
            f"/core/drivers/{driver_id}", json={"driver_code": new_code}, headers=auth(auth_token),
        )

    async def complete():
        return await client.post(
            f"/driver-transfers/{request_id}/complete", json={}, headers=auth(auth_token),
        )

    edit_response, complete_response = await asyncio.gather(edit(), complete())
    assert complete_response.status_code == 200, complete_response.text
    assert edit_response.status_code in (200, 422)
    stored = (await direct_db.execute(text(
        "SELECT DriverCode FROM core.Drivers WHERE DriverID = :id"
    ), {"id": driver_id})).scalar_one()
    if edit_response.status_code == 200:
        assert stored == new_code
    else:
        assert stored == original_code


@pytest.mark.asyncio
async def test_two_termination_attempts_apply_one_lifecycle_transition(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    today_date = await today(direct_db)
    employee_id, driver_id = await create_driver(client, auth_token, hq_branch_id)

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
        "SELECT DriverStatus FROM core.Drivers WHERE DriverID = :id"
    ), {"id": driver_id})).scalar_one() == "Terminated"


@pytest.mark.asyncio
async def test_stale_target_decision_and_cancel_cannot_overwrite_terminal_state(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    request_id = await create_transfer(
        client, auth_token, (await create_driver(client, auth_token, paytest_branch_id))[1],
        hq_branch_id, await today(direct_db) + timedelta(days=30),
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
