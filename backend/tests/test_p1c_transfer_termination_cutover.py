"""P1c effective transfer and atomic termination regressions."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.payroll.eligibility import (
    _assert_driver_eligible_for_date,
    _assert_driver_eligible_for_workdate_via_snapshot,
)
from app.workforce.effective import resolve_effective_driver_profile
from app.workforce.projection import (
    sync_company_employee_branch_projections,
    sync_employee_branch_projection,
)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def suffix() -> str:
    return uuid4().hex[:10]


async def company_today(db) -> date:
    return (await db.execute(text("SELECT core.fn_CompanyToday(1)"))).scalar_one()


async def create_driver_employee(client, token: str, branch_id: int) -> dict:
    response = await client.post(
        "/workforce/employees",
        json={
            "branch_id": branch_id,
            "full_name": f"P1C {suffix()}",
            "hire_date": "2020-01-01",
            "driver_profile": {
                "driver_code": f"P1C-{suffix()}",
                "effective_from": "2020-01-01",
            },
        },
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_transfer(client, token: str, driver_id: int, target_branch_id: int, effective_date: date):
    response = await client.post(
        "/driver-transfers",
        json={
            "driver_id": driver_id,
            "target_branch_id": target_branch_id,
            "effective_date": effective_date.isoformat(),
            "initiated_by": "SourceBranch",
            "reason": "P1c lifecycle test",
        },
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def approve_transfer(client, token: str, transfer_id: int) -> None:
    response = await client.post(
        f"/driver-transfers/{transfer_id}/decide-target",
        json={"decision": "Approved"},
        headers=auth(token),
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_future_transfer_is_effective_dated_and_projection_waits_until_date(
    client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    hq_branch_id: int,
    paytest_rate_type_id: int,
    direct_db,
):
    today = await company_today(direct_db)
    effective_date = today + timedelta(days=30)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    employee_id = employee["employee_id"]
    source_id = employee["current_or_pending_driver"]["driver_id"]
    period_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate, Status)
        VALUES (1, :branch_id, :code, 'P1c future transfer evidence', 'Custom', :today, :end_date, 'Cancelled')
        RETURNING PayrollPeriodID
    """), {"branch_id": paytest_branch_id, "code": f"P1C-{suffix()}",
           "today": today, "end_date": effective_date})).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriodEligibilitySnapshots
            (PayrollPeriodID, CompanyID, BranchID, SnapshotSource, FrozenAtUtc)
        VALUES (:period_id, 1, :branch_id, 'Generated', NOW())
    """), {"period_id": period_id, "branch_id": paytest_branch_id})
    eligibility_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriodDriverEligibility
            (CompanyID, BranchID, PayrollPeriodID, DriverID, SourceEmployeeID,
             DriverStatusSnapshot, EmploymentStatusSnapshot, HireDateSnapshot,
             DriverEffectiveFromSnapshot, IsEligibleForPeriod, EligibilityReasonCode,
             SnapshotSource, FrozenAtUtc)
        VALUES (1, :branch_id, :period_id, :driver_id, :employee_id, 'Active', 'Active',
                DATE '2020-01-01', DATE '2020-01-01', TRUE, 'Active', 'Generated', NOW())
        RETURNING PayrollPeriodDriverEligibilityID
    """), {"branch_id": paytest_branch_id, "period_id": period_id,
           "driver_id": source_id, "employee_id": employee_id})).scalar_one()
    rate = await direct_db.execute(text("""
        INSERT INTO payroll.DriverRates
            (CompanyID, BranchID, DriverID, RateTypeID, Amount, EffectiveFrom, Status)
        VALUES (1, :branch_id, :driver_id, :rate_type_id, 42, :today, 'PendingApproval')
        RETURNING DriverRateID
    """), {
        "branch_id": paytest_branch_id,
        "driver_id": source_id,
        "rate_type_id": paytest_rate_type_id,
        "today": today,
    })
    source_rate_id = rate.scalar_one()

    request = await create_transfer(client, auth_token, source_id, hq_branch_id, effective_date)
    await approve_transfer(client, auth_token, request["transfer_request_id"])
    completed = await client.post(
        f"/driver-transfers/{request['transfer_request_id']}/complete",
        json={},
        headers=auth(auth_token),
    )
    assert completed.status_code == 200, completed.text
    destination_id = completed.json()["new_driver_id"]

    profiles = (await direct_db.execute(text("""
        SELECT DriverID, BranchID, DriverStatus, EffectiveFrom, EffectiveTo,
               TransferredFromDriverID, TransferredToDriverID
        FROM core.Drivers WHERE EmployeeID = :employee_id ORDER BY DriverID
    """), {"employee_id": employee_id})).mappings().all()
    by_id = {row["driverid"]: row for row in profiles}
    assert by_id[source_id]["effectiveto"] == effective_date - timedelta(days=1)
    assert by_id[source_id]["driverstatus"] == "Transferred"
    assert by_id[source_id]["transferredtodriverid"] == destination_id
    assert by_id[destination_id]["effectivefrom"] == effective_date
    assert by_id[destination_id]["effectiveto"] is None
    assert by_id[destination_id]["transferredfromdriverid"] == source_id
    frozen_eligibility = (await direct_db.execute(text("""
        SELECT DriverStatusSnapshot, TerminationDateSnapshot, DriverEffectiveToSnapshot, FrozenAtUtc
        FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodDriverEligibilityID = :id
    """), {"id": eligibility_id})).mappings().one()
    assert frozen_eligibility["driverstatussnapshot"] == "Active"
    assert frozen_eligibility["terminationdatesnapshot"] is None
    assert frozen_eligibility["drivereffectivetosnapshot"] is None
    assert frozen_eligibility["frozenatutc"] is not None
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one() == paytest_branch_id

    current_before = await resolve_effective_driver_profile(1, employee_id, today, direct_db)
    current_until_transfer = await resolve_effective_driver_profile(
        1, employee_id, effective_date - timedelta(days=1), direct_db,
    )
    current_on_date = await resolve_effective_driver_profile(1, employee_id, effective_date, direct_db)
    assert current_before["driverid"] == source_id
    assert current_until_transfer["driverid"] == source_id
    assert current_on_date["driverid"] == destination_id
    await _assert_driver_eligible_for_date(
        1, source_id, paytest_branch_id, effective_date - timedelta(days=1), direct_db,
    )
    with pytest.raises(HTTPException):
        await _assert_driver_eligible_for_date(
            1, source_id, paytest_branch_id, effective_date, direct_db,
        )
    await sync_employee_branch_projection(1, employee_id, effective_date, direct_db)
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one() == hq_branch_id

    rates = (await direct_db.execute(text("""
        SELECT DriverID, DriverRateID FROM payroll.DriverRates
        WHERE DriverID IN (:source_id, :destination_id)
    """), {"source_id": source_id, "destination_id": destination_id})).mappings().all()
    assert [(row["driverid"], row["driverrateid"]) for row in rates] == [(source_id, source_rate_id)]
    audit = (await direct_db.execute(text("""
        SELECT NewValueJson FROM audit.AuditLog
        WHERE EntitySchema = 'core' AND EntityName = 'DriverTransferRequests'
          AND EntityID = :request_id AND ActionCode = 'DRIVER_TRANSFER_COMPLETED'
    """), {"request_id": str(request["transfer_request_id"])})).scalar_one()
    assert str(destination_id) in audit
    assert "projection_pending_until_effective_date" in audit

    chained = await client.post(
        "/driver-transfers",
        json={
            "driver_id": source_id,
            "target_branch_id": hq_branch_id,
            "effective_date": (today + timedelta(days=60)).isoformat(),
            "initiated_by": "SourceBranch",
            "reason": "A transferred source cannot start another transfer",
        },
        headers=auth(auth_token),
    )
    assert chained.status_code == 422, chained.text
    pending_as_source = await client.post(
        "/driver-transfers",
        json={
            "driver_id": destination_id,
            "target_branch_id": paytest_branch_id,
            "effective_date": (today + timedelta(days=60)).isoformat(),
            "initiated_by": "SourceBranch",
            "reason": "Pending destinations are not current sources",
        },
        headers=auth(auth_token),
    )
    assert pending_as_source.status_code == 422, pending_as_source.text


@pytest.mark.asyncio
@pytest.mark.parametrize("company_list_reconciliation", [False, True], ids=["employee-get", "employee-list"])
async def test_elapsed_future_termination_projection_uses_profile_effective_on_termination_date(
    client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    hq_branch_id: int,
    direct_db,
    company_list_reconciliation: bool,
):
    today = await company_today(direct_db)
    transfer_date = today + timedelta(days=30)
    termination_date = transfer_date + timedelta(days=15)
    reconcile_date = termination_date + timedelta(days=1)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    employee_id = employee["employee_id"]
    source_id = employee["current_or_pending_driver"]["driver_id"]

    request = await create_transfer(client, auth_token, source_id, hq_branch_id, transfer_date)
    await approve_transfer(client, auth_token, request["transfer_request_id"])
    completed = await client.post(
        f"/driver-transfers/{request['transfer_request_id']}/complete",
        json={}, headers=auth(auth_token),
    )
    assert completed.status_code == 200, completed.text
    destination_id = completed.json()["new_driver_id"]

    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one() == paytest_branch_id
    assert (await resolve_effective_driver_profile(1, employee_id, transfer_date, direct_db))["driverid"] == destination_id

    terminated = await client.post(
        f"/workforce/employees/{employee_id}/terminate",
        json={"termination_date": termination_date.isoformat(), "reason": "Elapsed future lifecycle"},
        headers=auth(auth_token),
    )
    assert terminated.status_code == 200, terminated.text
    # The endpoint's current-date read is still before E, so the projection remains in A.
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one() == paytest_branch_id

    if company_list_reconciliation:
        changed = await sync_company_employee_branch_projections(1, reconcile_date, direct_db)
        assert changed == 1
    else:
        old_branch, new_branch = await sync_employee_branch_projection(
            1, employee_id, reconcile_date, direct_db,
        )
        assert (old_branch, new_branch) == (paytest_branch_id, hq_branch_id)

    projected = (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one()
    assert projected == hq_branch_id
    assert (await resolve_effective_driver_profile(1, employee_id, termination_date, direct_db))["driverid"] == destination_id


@pytest.mark.asyncio
async def test_immediate_transfer_synchronizes_employee_branch_in_completion(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    today = await company_today(direct_db)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    source_id = employee["current_or_pending_driver"]["driver_id"]
    request = await create_transfer(client, auth_token, source_id, hq_branch_id, today - timedelta(days=1))
    await approve_transfer(client, auth_token, request["transfer_request_id"])
    completed = await client.post(
        f"/driver-transfers/{request['transfer_request_id']}/complete", json={}, headers=auth(auth_token),
    )
    assert completed.status_code == 200, completed.text
    destination_id = completed.json()["new_driver_id"]
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee["employee_id"]})).scalar_one() == hq_branch_id
    assert (await resolve_effective_driver_profile(1, employee["employee_id"], today, direct_db))["driverid"] == destination_id


@pytest.mark.asyncio
async def test_termination_before_completed_future_transfer_cancels_future_activity_and_keeps_history(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    today = await company_today(direct_db)
    future = today + timedelta(days=40)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    employee_id = employee["employee_id"]
    source_id = employee["current_or_pending_driver"]["driver_id"]
    request = await create_transfer(client, auth_token, source_id, hq_branch_id, future)
    await approve_transfer(client, auth_token, request["transfer_request_id"])
    complete = await client.post(
        f"/driver-transfers/{request['transfer_request_id']}/complete", json={}, headers=auth(auth_token),
    )
    assert complete.status_code == 200, complete.text
    destination_id = complete.json()["new_driver_id"]
    evidence_period_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate, Status)
        VALUES (1, :branch_id, :code, 'P1c termination evidence', 'Custom', :today, :end_date, 'Cancelled')
        RETURNING PayrollPeriodID
    """), {"branch_id": paytest_branch_id, "code": f"P1C-{suffix()}",
           "today": today, "end_date": future})).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriodEligibilitySnapshots
            (PayrollPeriodID, CompanyID, BranchID, SnapshotSource, FrozenAtUtc)
        VALUES (:period_id, 1, :branch_id, 'Generated', NOW())
    """), {"period_id": evidence_period_id, "branch_id": paytest_branch_id})
    termination_eligibility_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriodDriverEligibility
            (CompanyID, BranchID, PayrollPeriodID, DriverID, SourceEmployeeID,
             DriverStatusSnapshot, EmploymentStatusSnapshot, HireDateSnapshot,
             DriverEffectiveFromSnapshot, DriverEffectiveToSnapshot,
             IsEligibleForPeriod, EligibilityReasonCode, SnapshotSource, FrozenAtUtc)
        VALUES (1, :branch_id, :period_id, :driver_id, :employee_id, 'Active', 'Active',
                DATE '2020-01-01', DATE '2020-01-01', NULL,
                TRUE, 'Active', 'Generated', NOW())
        RETURNING PayrollPeriodDriverEligibilityID
    """), {"branch_id": paytest_branch_id, "period_id": evidence_period_id,
           "driver_id": source_id, "employee_id": employee_id})).scalar_one()
    old_source = (await direct_db.execute(text("""
        SELECT EffectiveFrom, TransferredToDriverID FROM core.Drivers WHERE DriverID = :id
    """), {"id": source_id})).mappings().one()
    before_calculation_snapshots = (await direct_db.execute(text(
        "SELECT COUNT(*) FROM payroll.PayrollCalculationSnapshots"
    ))).scalar_one()

    term = await client.post(
        f"/workforce/employees/{employee_id}/terminate",
        json={"termination_date": today.isoformat(), "reason": "End of employment"},
        headers=auth(auth_token),
    )
    assert term.status_code == 200, term.text
    assert term.json()["employment_status"] == "Terminated"
    source, destination = (await direct_db.execute(text("""
        SELECT DriverID, DriverStatus, EffectiveFrom, EffectiveTo, TransferredToDriverID
        FROM core.Drivers WHERE DriverID IN (:source_id, :destination_id) ORDER BY DriverID
    """), {"source_id": source_id, "destination_id": destination_id})).mappings().all()
    by_id = {row["driverid"]: row for row in (source, destination)}
    assert by_id[source_id]["driverstatus"] == "Terminated"
    assert by_id[source_id]["effectiveto"] == today
    assert by_id[source_id]["effectivefrom"] == old_source["effectivefrom"]
    assert by_id[source_id]["transferredtodriverid"] == destination_id
    assert by_id[destination_id]["driverstatus"] == "Terminated"
    assert by_id[destination_id]["effectiveto"] == future - timedelta(days=1)
    assert await resolve_effective_driver_profile(1, employee_id, today, direct_db)
    assert await resolve_effective_driver_profile(1, employee_id, today + timedelta(days=1), direct_db) is None
    request_status = (await direct_db.execute(text(
        "SELECT Status FROM core.DriverTransferRequests WHERE TransferRequestID = :id"
    ), {"id": request["transfer_request_id"]})).scalar_one()
    assert request_status == "Completed"
    frozen_eligibility = (await direct_db.execute(text("""
        SELECT DriverStatusSnapshot, TerminationDateSnapshot, DriverEffectiveToSnapshot, FrozenAtUtc
        FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodDriverEligibilityID = :id
    """), {"id": termination_eligibility_id})).mappings().one()
    assert frozen_eligibility["driverstatussnapshot"] == "Active"
    assert frozen_eligibility["terminationdatesnapshot"] is None
    assert frozen_eligibility["drivereffectivetosnapshot"] is None
    assert frozen_eligibility["frozenatutc"] is not None
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM payroll.PayrollCalculationSnapshots"
    ))).scalar_one() == before_calculation_snapshots
    audit = (await direct_db.execute(text("""
        SELECT NewValueJson FROM audit.AuditLog WHERE EntitySchema = 'core'
          AND EntityName = 'Employees' AND EntityID = :id
          AND ActionCode = 'DRIVER_EMPLOYEE_TERMINATED'
        ORDER BY AuditID DESC LIMIT 1
    """), {"id": str(employee_id)})).scalar_one()
    assert "End of employment" in audit and str(destination_id) in audit


@pytest.mark.asyncio
@pytest.mark.parametrize("approve_first", [False, True], ids=["pending", "approved"])
async def test_termination_cancels_uncompleted_transfer_request(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int,
    direct_db, approve_first: bool,
):
    today = await company_today(direct_db)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    source_id = employee["current_or_pending_driver"]["driver_id"]
    request = await create_transfer(
        client, auth_token, source_id, hq_branch_id,
        today + timedelta(days=20),
    )
    if approve_first:
        await approve_transfer(client, auth_token, request["transfer_request_id"])
    terminated = await client.post(
        f"/workforce/employees/{employee['employee_id']}/terminate",
        json={"termination_date": today.isoformat(), "reason": "Transfer cancellation"},
        headers=auth(auth_token),
    )
    assert terminated.status_code == 200, terminated.text
    state = (await direct_db.execute(text("""
        SELECT Status, CancelReason FROM core.DriverTransferRequests
        WHERE TransferRequestID = :id
    """), {"id": request["transfer_request_id"]})).mappings().one()
    assert state["status"] == "Cancelled"
    assert "terminated" in state["cancelreason"].lower()
    stale_completion = await client.post(
        f"/driver-transfers/{request['transfer_request_id']}/complete",
        json={}, headers=auth(auth_token),
    )
    assert stale_completion.status_code == 422
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM core.Drivers WHERE EmployeeID = :id"
    ), {"id": employee["employee_id"]})).scalar_one() == 1


@pytest.mark.asyncio
async def test_backdated_termination_reconciles_employee_to_branch_effective_on_date(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    today = await company_today(direct_db)
    termination_date = today - timedelta(days=1)
    employee = await create_driver_employee(client, auth_token, hq_branch_id)
    employee_id = employee["employee_id"]
    driver_id = employee["current_or_pending_driver"]["driver_id"]

    # Model a stale stored projection that disagrees with the profile effective on D.
    await direct_db.execute(text("""
        UPDATE core.Employees SET BranchID = :stale_branch
        WHERE EmployeeID = :employee_id
    """), {"stale_branch": paytest_branch_id, "employee_id": employee_id})
    before = (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :employee_id"
    ), {"employee_id": employee_id})).scalar_one()
    assert before == paytest_branch_id

    terminated = await client.post(
        f"/workforce/employees/{employee_id}/terminate",
        json={"termination_date": termination_date.isoformat(), "reason": "Backdated projection"},
        headers=auth(auth_token),
    )
    assert terminated.status_code == 200, terminated.text
    state = (await direct_db.execute(text("""
        SELECT e.BranchID AS EmployeeBranchID, d.BranchID AS DriverBranchID,
               d.DriverStatus, d.EffectiveTo
        FROM core.Employees e JOIN core.Drivers d ON d.EmployeeID = e.EmployeeID
        WHERE e.EmployeeID = :employee_id AND d.DriverID = :driver_id
    """), {"employee_id": employee_id, "driver_id": driver_id})).mappings().one()
    assert state["driverstatus"] == "Terminated"
    assert state["effectiveto"] == termination_date
    assert state["employeebranchid"] == hq_branch_id
    assert state["driverbranchid"] == hq_branch_id


@pytest.mark.asyncio
async def test_termination_before_first_driver_profile_is_rejected_without_mutation(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    today = await company_today(direct_db)
    future = today + timedelta(days=2)
    employee = await client.post("/workforce/employees", json={
        "branch_id": hq_branch_id,
        "full_name": f"P1C pending termination {suffix()}",
        "hire_date": "2020-01-01",
        "driver_profile": {"driver_code": f"P1C-{suffix()}", "effective_from": future.isoformat()},
    }, headers=auth(auth_token))
    assert employee.status_code == 201, employee.text
    employee_id = employee.json()["employee_id"]
    driver_id = employee.json()["current_or_pending_driver"]["driver_id"]

    rejected = await client.post(
        f"/workforce/employees/{employee_id}/terminate",
        json={"termination_date": today.isoformat(), "reason": "Before first profile"},
        headers=auth(auth_token),
    )
    assert rejected.status_code == 422, rejected.text
    state = (await direct_db.execute(text("""
        SELECT e.EmploymentStatus, e.TerminationDate, d.DriverStatus, d.EffectiveFrom, d.EffectiveTo
        FROM core.Employees e JOIN core.Drivers d ON d.EmployeeID = e.EmployeeID
        WHERE e.EmployeeID = :employee_id AND d.DriverID = :driver_id
    """), {"employee_id": employee_id, "driver_id": driver_id})).mappings().one()
    assert (state["employmentstatus"], state["terminationdate"]) == ("Active", None)
    assert (state["driverstatus"], state["effectivefrom"], state["effectiveto"]) == ("Active", future, None)


@pytest.mark.asyncio
async def test_termination_before_hire_date_is_rejected_without_mutation(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    today = await company_today(direct_db)
    hire_date = today + timedelta(days=1)
    employee = await client.post("/workforce/employees", json={
        "branch_id": hq_branch_id,
        "full_name": f"P1C future hire termination {suffix()}",
        "hire_date": hire_date.isoformat(),
        "driver_profile": {"driver_code": f"P1C-{suffix()}", "effective_from": hire_date.isoformat()},
    }, headers=auth(auth_token))
    assert employee.status_code == 201, employee.text
    employee_id = employee.json()["employee_id"]
    driver_id = employee.json()["current_or_pending_driver"]["driver_id"]

    rejected = await client.post(
        f"/workforce/employees/{employee_id}/terminate",
        json={"termination_date": today.isoformat(), "reason": "Before hire"},
        headers=auth(auth_token),
    )
    assert rejected.status_code == 422, rejected.text
    state = (await direct_db.execute(text("""
        SELECT e.EmploymentStatus, e.TerminationDate, d.DriverStatus, d.EffectiveFrom, d.EffectiveTo
        FROM core.Employees e JOIN core.Drivers d ON d.EmployeeID = e.EmployeeID
        WHERE e.EmployeeID = :employee_id AND d.DriverID = :driver_id
    """), {"employee_id": employee_id, "driver_id": driver_id})).mappings().one()
    assert (state["employmentstatus"], state["terminationdate"]) == ("Active", None)
    assert (state["driverstatus"], state["effectivefrom"], state["effectiveto"]) == ("Active", hire_date, None)


@pytest.mark.asyncio
async def test_termination_rejects_dated_finalized_work_after_date_without_mutation(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    today = await company_today(direct_db)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    driver_id = employee["current_or_pending_driver"]["driver_id"]
    later = today + timedelta(days=3)
    period_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate, Status)
        VALUES (1, :branch_id, :code, 'P1c finalized guard', 'Custom', :start_date, :end_date, 'Locked')
        RETURNING PayrollPeriodID
    """), {
        "branch_id": paytest_branch_id,
        "code": f"P1C-{suffix()}",
        "start_date": today,
        "end_date": later,
    })).scalar_one()
    await direct_db.execute(text(
        "SELECT set_config('app.allow_payroll_final_line_insert', 'true', false)"
    ))
    final_line_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollFinalLines
            (CompanyID, BranchID, PayrollPeriodID, DriverID, WorkDate, LineType, SourceType, FinalAmount)
        VALUES (1, :branch_id, :period_id, :driver_id, :work_date, 'HOURS', 'P1cTest', 100)
        RETURNING FinalLineID
    """), {
        "branch_id": paytest_branch_id, "period_id": period_id,
        "driver_id": driver_id, "work_date": later,
    })).scalar_one()

    rejected = await client.post(
        f"/workforce/employees/{employee['employee_id']}/terminate",
        json={"termination_date": today.isoformat(), "reason": "Too early"},
        headers=auth(auth_token),
    )
    assert rejected.status_code == 422, rejected.text
    status = (await direct_db.execute(text("""
        SELECT e.EmploymentStatus, d.DriverStatus
        FROM core.Employees e JOIN core.Drivers d ON d.EmployeeID = e.EmployeeID
        WHERE e.EmployeeID = :employee_id
    """), {"employee_id": employee["employee_id"]})).mappings().one()
    assert (status["employmentstatus"], status["driverstatus"]) == ("Active", "Active")
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM payroll.PayrollFinalLines WHERE FinalLineID = :id"
    ), {"id": final_line_id})).scalar_one() == 1


@pytest.mark.asyncio
async def test_termination_preserves_snapshot_and_exact_date_existing_source_rescue(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    today = await company_today(direct_db)
    next_day = today + timedelta(days=1)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    driver_id = employee["current_or_pending_driver"]["driver_id"]
    period_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate, Status)
        VALUES (1, :branch_id, :code, 'P1c eligibility', 'Custom', :today, :end_date, 'Cancelled')
        RETURNING PayrollPeriodID
    """), {"branch_id": paytest_branch_id, "code": f"P1C-{suffix()}",
           "today": today, "end_date": next_day})).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriodEligibilitySnapshots
            (PayrollPeriodID, CompanyID, BranchID, SnapshotSource)
        VALUES (:period_id, 1, :branch_id, 'Generated')
    """), {"period_id": period_id, "branch_id": paytest_branch_id})
    eligibility_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriodDriverEligibility
            (CompanyID, BranchID, PayrollPeriodID, DriverID, SourceEmployeeID,
             IsEligibleForPeriod, EligibilityReasonCode, SnapshotSource,
             HireDateSnapshot, TerminationDateSnapshot, DriverEffectiveFromSnapshot)
        VALUES (1, :branch_id, :period_id, :driver_id, :employee_id,
                TRUE, 'Active', 'Generated', DATE '2020-01-01', NULL, DATE '2020-01-01')
        RETURNING PayrollPeriodDriverEligibilityID
    """), {"branch_id": paytest_branch_id, "period_id": period_id,
           "driver_id": driver_id, "employee_id": employee["employee_id"]})).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO payroll.PayrollDraftLines
            (CompanyID, BranchID, PayrollPeriodID, DriverID, WorkDate, LineType, SourceType, Status)
        VALUES (1, :branch_id, :period_id, :driver_id, :work_date, 'HOURS', 'P1cTest', 'Active')
    """), {"branch_id": paytest_branch_id, "period_id": period_id,
           "driver_id": driver_id, "work_date": next_day})
    await client.post(
        f"/workforce/employees/{employee['employee_id']}/terminate",
        json={"termination_date": today.isoformat(), "reason": "Historical cutoff"},
        headers=auth(auth_token),
    )

    await _assert_driver_eligible_for_workdate_via_snapshot(
        1, paytest_branch_id, period_id, driver_id, today, direct_db,
    )
    await _assert_driver_eligible_for_workdate_via_snapshot(
        1, paytest_branch_id, period_id, driver_id, next_day, direct_db,
        allow_existing_source_rescue=True,
    )
    with pytest.raises(HTTPException):
        await _assert_driver_eligible_for_workdate_via_snapshot(
            1, paytest_branch_id, period_id, driver_id, next_day, direct_db,
            allow_existing_source_rescue=False,
        )
    snapshot_row = (await direct_db.execute(text("""
        SELECT TerminationDateSnapshot, DriverEffectiveToSnapshot
        FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodDriverEligibilityID = :id
    """), {"id": eligibility_id})).mappings().one()
    assert snapshot_row["terminationdatesnapshot"] is None
    assert snapshot_row["drivereffectivetosnapshot"] is None


@pytest.mark.asyncio
async def test_termination_audit_failure_rolls_back_employee_and_driver(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db, monkeypatch,
):
    today = await company_today(direct_db)
    employee = await create_driver_employee(client, auth_token, hq_branch_id)
    from app.workforce import service as workforce_service

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("forced P1c audit failure")

    monkeypatch.setattr(workforce_service, "_write_workforce_audit", fail_audit)
    with pytest.raises(RuntimeError, match="forced P1c audit failure"):
        await client.post(
            f"/workforce/employees/{employee['employee_id']}/terminate",
            json={"termination_date": today.isoformat(), "reason": "Rollback proof"},
            headers=auth(auth_token),
        )
    state = (await direct_db.execute(text("""
        SELECT e.EmploymentStatus, e.TerminationDate, d.DriverStatus, d.EffectiveTo
        FROM core.Employees e JOIN core.Drivers d ON d.EmployeeID = e.EmployeeID
        WHERE e.EmployeeID = :employee_id
    """), {"employee_id": employee["employee_id"]})).mappings().one()
    assert state["employmentstatus"] == "Active"
    assert state["terminationdate"] is None
    assert state["driverstatus"] == "Active"
    assert state["effectiveto"] is None


@pytest.mark.asyncio
async def test_workforce_list_and_get_use_effective_branch_when_projection_is_stale(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int,
    direct_db, monkeypatch,
):
    employee = await create_driver_employee(client, auth_token, hq_branch_id)
    employee_id = employee["employee_id"]
    await direct_db.execute(text("""
        UPDATE core.Employees SET BranchID = :stale_branch
        WHERE EmployeeID = :employee_id
    """), {"stale_branch": paytest_branch_id, "employee_id": employee_id})

    listed_in_effective_branch = await client.get(
        f"/workforce/employees?branch_id={hq_branch_id}", headers=auth(auth_token),
    )
    assert listed_in_effective_branch.status_code == 200, listed_in_effective_branch.text
    listed = next(row for row in listed_in_effective_branch.json() if row["employee_id"] == employee_id)
    assert listed["branch_id"] == hq_branch_id

    listed_in_stale_branch = await client.get(
        f"/workforce/employees?branch_id={paytest_branch_id}", headers=auth(auth_token),
    )
    assert listed_in_stale_branch.status_code == 200, listed_in_stale_branch.text
    assert all(row["employee_id"] != employee_id for row in listed_in_stale_branch.json())

    await direct_db.execute(text("""
        UPDATE core.Employees SET BranchID = :stale_branch
        WHERE EmployeeID = :employee_id
    """), {"stale_branch": paytest_branch_id, "employee_id": employee_id})
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :employee_id"
    ), {"employee_id": employee_id})).scalar_one() == paytest_branch_id
    from app.workforce import service as workforce_service

    original_readable_branches = workforce_service._readable_employee_branches

    async def assert_authorized_branch(company_id, user_id, db, requested_branch_id=None):
        assert requested_branch_id == hq_branch_id
        return await original_readable_branches(company_id, user_id, db, requested_branch_id)

    monkeypatch.setattr(workforce_service, "_readable_employee_branches", assert_authorized_branch)
    detail = await client.get(f"/workforce/employees/{employee_id}", headers=auth(auth_token))
    assert detail.status_code == 200, detail.text
    assert detail.json()["branch_id"] == hq_branch_id
    stored_branch = (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :employee_id"
    ), {"employee_id": employee_id})).scalar_one()
    assert stored_branch == hq_branch_id


@pytest.mark.asyncio
async def test_workforce_list_reconciliation_does_not_lock_unrelated_employees(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int,
    direct_db, test_database_url,
):
    from sqlalchemy.ext.asyncio import create_async_engine

    employee = await create_driver_employee(client, auth_token, hq_branch_id)
    await direct_db.execute(text("""
        UPDATE core.Employees SET BranchID = :stale_branch
        WHERE EmployeeID = :employee_id
    """), {"stale_branch": paytest_branch_id, "employee_id": employee["employee_id"]})
    unrelated = await client.post("/workforce/employees", json={
        "branch_id": hq_branch_id,
        "full_name": f"P1C list lock {suffix()}",
    }, headers=auth(auth_token))
    assert unrelated.status_code == 201, unrelated.text
    unrelated_employee_id = unrelated.json()["employee_id"]

    engine = create_async_engine(test_database_url, echo=False)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text("""
                    SELECT EmployeeID FROM core.Employees
                    WHERE EmployeeID = :employee_id FOR UPDATE
                """), {"employee_id": unrelated_employee_id})
                response = await asyncio.wait_for(
                    client.get("/workforce/employees", headers=auth(auth_token)), timeout=5,
                )
                assert response.status_code == 200, response.text
                ids = {row["employee_id"] for row in response.json()}
                assert employee["employee_id"] in ids
                assert unrelated_employee_id in ids
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()

    branch_after = (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :employee_id"
    ), {"employee_id": employee["employee_id"]})).scalar_one()
    assert branch_after == hq_branch_id
