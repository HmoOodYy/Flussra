"""P1c effective transfer and atomic termination regressions (Driver / DriverAssignment model)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.payroll.eligibility import _assert_driver_eligible_for_workdate_via_snapshot
from app.settings.branch_reads import fetch_branch_metrics
from app.workforce.effective import resolve_effective_driver_assignment
from app.workforce.projection import (
    count_employee_branch_projections,
    sync_company_employee_branch_projections,
    sync_employee_branch_projection,
)
from tests.builders.final_evidence import insert_final_line, seed_snapshot_component
from tests.builders.pay_rules import insert_pay_rule
from tests.builders.workforce_db import end_driver_assignment, insert_driver_assignment


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def suffix() -> str:
    return uuid4().hex[:10]


def ids_of(employee: dict) -> tuple[int, int]:
    """(permanent DriverID, first DriverAssignmentID) of a created Driver Employee."""
    return (
        employee["driver"]["driver_id"],
        employee["current_or_pending_assignment"]["driver_assignment_id"],
    )


async def branch_today(db, branch_id: int) -> date:
    return (await db.execute(text(
        "SELECT core.fn_BranchToday(1, :branch_id)"
    ), {"branch_id": branch_id})).scalar_one()


async def branch_boundary(db, branch_id: int, value: date) -> datetime:
    return (await db.execute(text("""
        SELECT core.fn_LocalDateBoundary(b.TimeZoneId, CAST(:value AS DATE))
        FROM core.Branches b WHERE b.CompanyID = 1 AND b.BranchID = :branch_id
    """), {"branch_id": branch_id, "value": value})).scalar_one()


async def create_driver_employee(client, token: str, branch_id: int) -> dict:
    response = await client.post(
        "/workforce/employees",
        json={
            "branch_id": branch_id,
            "full_name": f"P1C {suffix()}",
            "hire_date": "2020-01-01",
            "driver": {
                "driver_number": f"P1C-{suffix()}",
                "effective_from_at": "2020-01-01T00:00:00Z",
            },
        },
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_transfer(client, token: str, driver_id: int, target_branch_id: int,
                          effective_at: datetime | None):
    payload = {
        "driver_id": driver_id,
        "target_branch_id": target_branch_id,
        "initiated_by": "SourceBranch",
        "reason": "P1c lifecycle test",
    }
    if effective_at is not None:
        payload["effective_at"] = effective_at.isoformat()
    response = await client.post(
        "/driver-transfers",
        json=payload,
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
async def test_future_transfer_is_effective_at_instant_and_projection_waits_until_boundary(
    client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    hq_branch_id: int,
    direct_db,
):
    today = await branch_today(direct_db, paytest_branch_id)
    effective_date = today + timedelta(days=30)
    effective_at = await branch_boundary(direct_db, paytest_branch_id, effective_date)
    effective_at += timedelta(hours=3, minutes=19, seconds=7)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    employee_id = employee["employee_id"]
    driver_id, source_id = ids_of(employee)
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
            (CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, SourceEmployeeID,
             DriverStatusSnapshot, EmploymentStatusSnapshot, HireDateSnapshot,
             DriverEffectiveFromAtSnapshot, DriverEffectiveToAtSnapshot,
             BranchTimeZoneIdSnapshot, EligibleWorkDates,
             IsEligibleForPeriod, EligibilityReasonCode,
             SnapshotSource, FrozenAtUtc)
        VALUES (1, :branch_id, :period_id, :assignment_id, :employee_id, 'Active', 'Active',
                DATE '2020-01-01', TIMESTAMPTZ '2020-01-01 00:00:00+00', NULL,
                'America/New_York', ARRAY[:today, :last_day]::DATE[],
                TRUE, 'Active', 'Generated', NOW())
        RETURNING PayrollPeriodDriverEligibilityID
    """), {"branch_id": paytest_branch_id, "period_id": period_id,
           "assignment_id": source_id, "employee_id": employee_id,
           "today": today, "last_day": effective_date - timedelta(days=1)})).scalar_one()
    pay_definition_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayDefinitions
            (CompanyID, DefinitionCode, DefinitionName, InputType, CalculationMethod)
        VALUES (1, :code, 'Transfer preservation rate', 'Decimal', 'PerUnit')
        RETURNING PayDefinitionID
    """), {"code": f"P1C-{suffix()}"})).scalar_one()
    rate_definition_id = (await direct_db.execute(text("""
        INSERT INTO payroll.RateDefinitions (CompanyID, PayDefinitionID, Shape)
        VALUES (1, :pay_definition_id, 'Scalar') RETURNING RateDefinitionID
    """), {"pay_definition_id": pay_definition_id})).scalar_one()
    rate_component_id = (await direct_db.execute(text("""
        INSERT INTO payroll.RateComponentDefinitions (RateDefinitionID, Shape, SequenceNo)
        VALUES (:rate_definition_id, 'Scalar', 1)
        RETURNING RateComponentDefinitionID
    """), {"rate_definition_id": rate_definition_id})).scalar_one()
    driver_id = (await direct_db.execute(text(
        "SELECT DriverID FROM core.DriverAssignments WHERE DriverAssignmentID = :a"
    ), {"a": source_id})).scalar_one()
    schedule_id = (await direct_db.execute(text("""
        INSERT INTO payroll.DriverRateSchedules
            (CompanyID, DriverID, PayDefinitionID, RateDefinitionID, EffectiveFrom, Status)
        VALUES (1, :driver_id, :pay_definition_id, :rate_definition_id, :today, 'Pending')
        RETURNING DriverRateScheduleID
    """), {"driver_id": driver_id, "pay_definition_id": pay_definition_id,
          "rate_definition_id": rate_definition_id, "today": today})).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO payroll.DriverRateScheduleValues
            (DriverRateScheduleID, RateDefinitionID, RateComponentDefinitionID, Amount)
        VALUES (:schedule_id, :rate_definition_id, :component_id, 42)
    """), {"schedule_id": schedule_id, "rate_definition_id": rate_definition_id,
           "component_id": rate_component_id})
    await direct_db.execute(text("""
        UPDATE payroll.DriverRateSchedules
        SET Status = 'Approved', ApprovedByUserID = 1, ApprovedAtUtc = NOW()
        WHERE DriverRateScheduleID = :schedule_id
    """), {"schedule_id": schedule_id})
    source_rule_id = await insert_pay_rule(
        direct_db, company_id=1, driver_id=driver_id, branch_id=paytest_branch_id,
        amount="12.50", start=today)

    from app.payroll.eligibility import _create_period_driver_eligibility_rows
    target_work_date = (await direct_db.execute(text("""
        SELECT core.fn_BusinessDate(b.TimeZoneId, CAST(:at AS TIMESTAMPTZ))
        FROM core.Branches b WHERE b.CompanyID = 1 AND b.BranchID = :branch_id
    """), {"at": effective_at, "branch_id": hq_branch_id})).scalar_one()
    target_period_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate, Status)
        VALUES (1, :branch_id, :code, 'Transfer destination editable roster', 'Custom',
                :start_date, :end_date, 'Open')
        RETURNING PayrollPeriodID
    """), {
        "branch_id": hq_branch_id, "code": f"P1C-{suffix()}",
        "start_date": target_work_date - timedelta(days=2),
        "end_date": target_work_date + timedelta(days=2),
    })).scalar_one()
    transfer_actor_id = (await direct_db.execute(text(
        "SELECT UserID FROM sec.Users WHERE CompanyID = 1 AND Username = 'admin' LIMIT 1"
    ))).scalar_one()
    await _create_period_driver_eligibility_rows(
        target_period_id, 1, hq_branch_id, direct_db,
        snapshot_source="Generated", freeze=True, frozen_by_user_id=transfer_actor_id,
    )
    target_rows_before = (await direct_db.execute(text("""
        SELECT DriverAssignmentID, EligibleWorkDates, DriverStatusSnapshot, FrozenAtUtc, FrozenByUserID
        FROM payroll.PayrollPeriodDriverEligibility WHERE PayrollPeriodID = :period_id
        ORDER BY DriverAssignmentID
    """), {"period_id": target_period_id})).mappings().all()
    target_rows_before = [dict(row) for row in target_rows_before]

    request = await create_transfer(client, auth_token, driver_id, hq_branch_id, effective_at)
    assert request["source_driver_assignment_id"] == source_id
    await approve_transfer(client, auth_token, request["transfer_request_id"])
    completed = await client.post(
        f"/driver-transfers/{request['transfer_request_id']}/complete",
        json={},
        headers=auth(auth_token),
    )
    assert completed.status_code == 200, completed.text
    # A future-dated transfer is Scheduled, never Completed, until its EffectiveAt.
    assert completed.json()["status"] == "Scheduled"
    destination_id = completed.json()["destination_driver_assignment_id"]

    assignments = (await direct_db.execute(text("""
        SELECT DriverAssignmentID, DriverID, BranchID, EndReason, EffectiveFromAt, EffectiveToAt,
               TransferredFromAssignmentID, TransferredToAssignmentID
        FROM core.DriverAssignments WHERE EmployeeID = :employee_id ORDER BY DriverAssignmentID
    """), {"employee_id": employee_id})).mappings().all()
    by_id = {row["driverassignmentid"]: row for row in assignments}
    assert by_id[source_id]["effectivetoat"] == effective_at
    assert by_id[source_id]["endreason"] == "Transferred"
    assert by_id[source_id]["transferredtoassignmentid"] == destination_id
    assert by_id[destination_id]["effectivefromat"] == effective_at
    assert by_id[destination_id]["effectivetoat"] is None
    assert by_id[destination_id]["endreason"] is None
    assert by_id[destination_id]["transferredfromassignmentid"] == source_id
    assert by_id[source_id]["driverid"] == by_id[destination_id]["driverid"] == driver_id
    appended = (await direct_db.execute(text("""
        SELECT IsEligibleForPeriod, EligibilityReasonCode, EligibleWorkDates,
               BranchTimeZoneIdSnapshot, DriverEffectiveFromAtSnapshot,
               DriverStatusSnapshot, EndReasonSnapshot, TransferredFromAssignmentIDSnapshot,
               FrozenAtUtc, FrozenByUserID
        FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodID = :period_id AND DriverAssignmentID = :assignment_id
    """), {"period_id": target_period_id, "assignment_id": destination_id})).mappings().one()
    assert appended["iseligibleforperiod"] is True
    assert appended["eligibilityreasoncode"] == "Active"
    assert appended["driverstatussnapshot"] == "Active"
    assert appended["endreasonsnapshot"] is None
    assert appended["transferredfromassignmentidsnapshot"] == source_id
    assert target_work_date in appended["eligibleworkdates"]
    assert appended["branchtimezoneidsnapshot"]
    assert appended["drivereffectivefromatsnapshot"] == effective_at
    assert appended["frozenatutc"] is not None
    assert appended["frozenbyuserid"] == transfer_actor_id
    target_rows_after = (await direct_db.execute(text("""
        SELECT DriverAssignmentID, EligibleWorkDates, DriverStatusSnapshot, FrozenAtUtc, FrozenByUserID
        FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodID = :period_id AND DriverAssignmentID <> :destination_id
        ORDER BY DriverAssignmentID
    """), {"period_id": target_period_id, "destination_id": destination_id})).mappings().all()
    assert [dict(row) for row in target_rows_after] == target_rows_before
    frozen_eligibility = (await direct_db.execute(text("""
        SELECT DriverStatusSnapshot, TerminationDateSnapshot, DriverEffectiveToAtSnapshot, FrozenAtUtc
        FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodDriverEligibilityID = :id
    """), {"id": eligibility_id})).mappings().one()
    assert frozen_eligibility["driverstatussnapshot"] == "Active"
    assert frozen_eligibility["terminationdatesnapshot"] is None
    assert frozen_eligibility["drivereffectivetoatsnapshot"] is None
    assert frozen_eligibility["frozenatutc"] is not None
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one() == paytest_branch_id

    current_before = await resolve_effective_driver_assignment(
        1, driver_id, await branch_boundary(direct_db, paytest_branch_id, today), direct_db,
    )
    current_until_transfer = await resolve_effective_driver_assignment(
        1, driver_id, effective_at - timedelta(microseconds=1), direct_db,
    )
    current_on_date = await resolve_effective_driver_assignment(1, driver_id, effective_at, direct_db)
    current_after = await resolve_effective_driver_assignment(
        1, driver_id, effective_at + timedelta(microseconds=1), direct_db,
    )
    assert current_before["driverassignmentid"] == source_id
    assert current_until_transfer["driverassignmentid"] == source_id
    assert current_on_date["driverassignmentid"] == destination_id
    assert current_after["driverassignmentid"] == destination_id
    await sync_employee_branch_projection(1, employee_id, effective_at, direct_db)
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one() == hq_branch_id

    # The Driver's schedule belongs to the Driver, so the transfer neither copies nor moves it:
    # one row before, one row after, unchanged, and the destination assignment owns nothing.
    target_compensation = (await direct_db.execute(text("""
        SELECT
          (SELECT count(*) FROM payroll.PayrollDraftLines WHERE DriverAssignmentID = :aid) AS draft_lines,
          (SELECT count(*) FROM payroll.PayrollBonusEvents WHERE DriverAssignmentID = :aid) AS bonus_events
    """), {"aid": destination_id})).mappings().one()
    assert tuple(target_compensation.values()) == (0, 0)
    driver_schedules = (await direct_db.execute(text("""
        SELECT s.DriverRateScheduleID, s.Status, v.Amount
        FROM payroll.DriverRateSchedules s
        JOIN payroll.DriverRateScheduleValues v ON v.DriverRateScheduleID = s.DriverRateScheduleID
        WHERE s.DriverID = :driver_id
    """), {"driver_id": driver_id})).all()
    assert [tuple(row) for row in driver_schedules] == [(schedule_id, "Approved", Decimal("42.0000"))]
    driver_rules = (await direct_db.execute(text("""
        SELECT DriverPayRuleID, Status, Amount FROM payroll.DriverPayRules WHERE DriverID = :driver_id
    """), {"driver_id": driver_id})).all()
    assert [tuple(row) for row in driver_rules] == [(source_rule_id, "Active", Decimal("12.5000"))]
    audit = (await direct_db.execute(text("""
        SELECT NewValueJson FROM audit.AuditLog
        WHERE EntitySchema = 'core' AND EntityName = 'DriverTransferRequests'
          AND EntityID = :request_id AND ActionCode = 'DRIVER_TRANSFER_SCHEDULED'
    """), {"request_id": str(request["transfer_request_id"])})).scalar_one()
    assert str(destination_id) in audit
    assert "projection_pending_until_effective_at" in audit

    # While the Scheduled transfer is in flight the Driver cannot start another one.
    chained = await client.post(
        "/driver-transfers",
        json={
            "driver_id": driver_id,
            "target_branch_id": hq_branch_id,
            "effective_at": (effective_at + timedelta(days=30)).isoformat(),
            "initiated_by": "SourceBranch",
            "reason": "A Driver with a Scheduled transfer cannot start another transfer",
        },
        headers=auth(auth_token),
    )
    assert chained.status_code == 422, chained.text
    await direct_db.execute(text(
        "DELETE FROM payroll.PayrollPeriods WHERE PayrollPeriodID = :period_id"
    ), {"period_id": target_period_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("company_list_reconciliation", [False, True], ids=["employee-get", "employee-list"])
async def test_elapsed_future_termination_projection_uses_assignment_effective_on_termination_date(
    client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    hq_branch_id: int,
    direct_db,
    test_engine,
    company_list_reconciliation: bool,
):
    today = await branch_today(direct_db, paytest_branch_id)
    transfer_date = today + timedelta(days=30)
    termination_date = transfer_date + timedelta(days=15)
    reconcile_date = termination_date + timedelta(days=1)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    employee_id = employee["employee_id"]
    driver_id, source_id = ids_of(employee)

    transfer_at = await branch_boundary(direct_db, paytest_branch_id, transfer_date)
    request = await create_transfer(client, auth_token, driver_id, hq_branch_id, transfer_at)
    await approve_transfer(client, auth_token, request["transfer_request_id"])
    completed = await client.post(
        f"/driver-transfers/{request['transfer_request_id']}/complete",
        json={}, headers=auth(auth_token),
    )
    assert completed.status_code == 200, completed.text
    destination_id = completed.json()["destination_driver_assignment_id"]

    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one() == paytest_branch_id
    assert (await resolve_effective_driver_assignment(
        1, driver_id, transfer_at, direct_db,
    ))["driverassignmentid"] == destination_id

    terminated = await client.post(
        f"/workforce/employees/{employee_id}/terminate",
        json={"termination_date": termination_date.isoformat(), "reason": "Elapsed future lifecycle"},
        headers=auth(auth_token),
    )
    assert terminated.status_code == 200, terminated.text
    # A Scheduled transfer taking effect before the termination cap stays and is not cancelled.
    assert (await direct_db.execute(text(
        "SELECT Status FROM core.DriverTransferRequests WHERE TransferRequestID = :id"
    ), {"id": request["transfer_request_id"]})).scalar_one() == "Scheduled"
    # The endpoint's current-date read is still before E, so the projection remains in A.
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one() == paytest_branch_id

    if company_list_reconciliation:
        # Company 1 is shared across tests; roll back list-wide reconciliation so
        # unrelated employees keep their pending branch projections.
        async with test_engine.connect() as reconciliation_db:
            transaction = await reconciliation_db.begin()
            try:
                before_rows = await reconciliation_db.execute(text(
                    "SELECT EmployeeID, BranchID FROM core.Employees WHERE CompanyID = 1"
                ))
                before = {
                    row["employeeid"]: row["branchid"]
                    for row in before_rows.mappings().all()
                }
                changed = await sync_company_employee_branch_projections(
                    1, await branch_boundary(reconciliation_db, hq_branch_id, reconcile_date), reconciliation_db,
                )
                after_rows = await reconciliation_db.execute(text(
                    "SELECT EmployeeID, BranchID FROM core.Employees WHERE CompanyID = 1"
                ))
                after = {
                    row["employeeid"]: row["branchid"]
                    for row in after_rows.mappings().all()
                }
                changed_ids = {
                    candidate_id
                    for candidate_id, old_branch in before.items()
                    if candidate_id in after and after[candidate_id] != old_branch
                }
                assert employee_id in changed_ids
                assert changed == len(changed_ids)
                assert after[employee_id] == hq_branch_id
            finally:
                await transaction.rollback()

        # Persist only this test's employee after proving company-wide behavior.
        old_branch, new_branch = await sync_employee_branch_projection(
            1, employee_id, await branch_boundary(direct_db, hq_branch_id, reconcile_date), direct_db,
        )
        assert (old_branch, new_branch) == (paytest_branch_id, hq_branch_id)
    else:
        old_branch, new_branch = await sync_employee_branch_projection(
            1, employee_id, await branch_boundary(direct_db, hq_branch_id, reconcile_date), direct_db,
        )
        assert (old_branch, new_branch) == (paytest_branch_id, hq_branch_id)

    projected = (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee_id})).scalar_one()
    assert projected == hq_branch_id
    assert (await resolve_effective_driver_assignment(
        1, driver_id, await branch_boundary(direct_db, hq_branch_id, termination_date), direct_db,
    ))["driverassignmentid"] == destination_id
    assert source_id != destination_id


@pytest.mark.asyncio
async def test_immediate_transfer_synchronizes_employee_branch_in_completion(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    driver_id, source_id = ids_of(employee)
    request = await create_transfer(client, auth_token, driver_id, hq_branch_id, None)
    assert request["effective_at"] is None
    await approve_transfer(client, auth_token, request["transfer_request_id"])
    before_completion = (await direct_db.execute(text("SELECT NOW()"))).scalar_one()
    completed = await client.post(
        f"/driver-transfers/{request['transfer_request_id']}/complete", json={}, headers=auth(auth_token),
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "Completed"
    destination_id = completed.json()["destination_driver_assignment_id"]
    after_completion = (await direct_db.execute(text("SELECT NOW()"))).scalar_one()
    boundaries = (await direct_db.execute(text("""
        SELECT r.EffectiveAt, source.EffectiveToAt, destination.EffectiveFromAt
        FROM core.DriverTransferRequests r
        JOIN core.DriverAssignments source ON source.DriverAssignmentID = r.SourceDriverAssignmentID
        JOIN core.DriverAssignments destination ON destination.DriverAssignmentID = r.DestinationDriverAssignmentID
        WHERE r.TransferRequestID = :request_id
    """), {"request_id": request["transfer_request_id"]})).one()
    assert boundaries[0] == boundaries[1] == boundaries[2]
    assert before_completion <= boundaries[0] <= after_completion
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :id"
    ), {"id": employee["employee_id"]})).scalar_one() == hq_branch_id
    from app.workforce.effective import resolve_effective_employee_assignment
    resolved = await resolve_effective_employee_assignment(
        1, employee["employee_id"], None, direct_db
    )
    assert resolved["driverassignmentid"] == destination_id
    assert resolved["driverid"] == driver_id
    assert source_id != destination_id


@pytest.mark.parametrize("effective_delay", [0, 30], ids=["immediate", "future"])
@pytest.mark.asyncio
async def test_transfer_audit_failure_rolls_back_completion(
    client: httpx.AsyncClient,
    auth_token: str,
    paytest_branch_id: int,
    hq_branch_id: int,
    direct_db,
    monkeypatch,
    effective_delay: int,
):
    today = await branch_today(direct_db, paytest_branch_id)
    effective_at = None if effective_delay == 0 else await branch_boundary(
        direct_db, paytest_branch_id, today + timedelta(days=effective_delay),
    )
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    employee_id = employee["employee_id"]
    driver_id, source_id = ids_of(employee)
    request = await create_transfer(client, auth_token, driver_id, hq_branch_id, effective_at)
    transfer_id = request["transfer_request_id"]
    await approve_transfer(client, auth_token, transfer_id)
    from app.workforce import audit as workforce_audit

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("forced transfer audit failure")

    monkeypatch.setattr(workforce_audit, "write_workforce_audit", fail_audit)
    with pytest.raises(RuntimeError, match="forced transfer audit failure"):
        await client.post(
            f"/driver-transfers/{transfer_id}/complete",
            json={},
            headers=auth(auth_token),
        )

    source = (await direct_db.execute(text("""
        SELECT EndReason, EffectiveToAt, TransferredToAssignmentID
        FROM core.DriverAssignments WHERE DriverAssignmentID = :assignment_id
    """), {"assignment_id": source_id})).mappings().one()
    assert source["endreason"] is None
    assert source["effectivetoat"] is None
    assert source["transferredtoassignmentid"] is None
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :employee_id",
    ), {"employee_id": employee_id})).scalar_one() == paytest_branch_id
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM core.DriverAssignments WHERE EmployeeID = :employee_id",
    ), {"employee_id": employee_id})).scalar_one() == 1
    transfer = (await direct_db.execute(text("""
        SELECT Status, DestinationDriverAssignmentID, CompletedAtUtc, ScheduledAtUtc
        FROM core.DriverTransferRequests WHERE TransferRequestID = :transfer_id
    """), {"transfer_id": transfer_id})).mappings().one()
    assert transfer["status"] == "Approved"
    assert transfer["destinationdriverassignmentid"] is None
    assert transfer["completedatutc"] is None
    assert transfer["scheduledatutc"] is None
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM audit.AuditLog "
        "WHERE EntityName = 'DriverTransferRequests' AND EntityID = :transfer_id "
        "AND ActionCode IN ('DRIVER_TRANSFER_COMPLETED', 'DRIVER_TRANSFER_SCHEDULED')",
    ), {"transfer_id": str(transfer_id)})).scalar_one() == 0


@pytest.mark.asyncio
async def test_termination_before_scheduled_transfer_cancels_it_and_keeps_history(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    from app.payroll.eligibility import _create_period_driver_eligibility_rows

    today = await branch_today(direct_db, paytest_branch_id)
    future = today + timedelta(days=40)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    employee_id = employee["employee_id"]
    driver_id, source_id = ids_of(employee)
    future_at = await branch_boundary(direct_db, paytest_branch_id, future)
    request = await create_transfer(client, auth_token, driver_id, hq_branch_id, future_at)
    await approve_transfer(client, auth_token, request["transfer_request_id"])
    complete = await client.post(
        f"/driver-transfers/{request['transfer_request_id']}/complete", json={}, headers=auth(auth_token),
    )
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "Scheduled"
    destination_id = complete.json()["destination_driver_assignment_id"]
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
    await _create_period_driver_eligibility_rows(
        evidence_period_id, 1, paytest_branch_id, direct_db,
        snapshot_source="Generated", freeze=True,
    )
    destination_period_id = (await direct_db.execute(text("""
        INSERT INTO payroll.PayrollPeriods
            (CompanyID, BranchID, PeriodCode, PeriodName, PeriodType, StartDate, EndDate, Status)
        VALUES (1, :branch_id, :code, 'P1c destination Bonus evidence', 'Custom', :start_date, :end_date, 'Cancelled')
        RETURNING PayrollPeriodID
    """), {"branch_id": hq_branch_id, "code": f"P1C-{suffix()}",
           "start_date": future, "end_date": future + timedelta(days=3)})).scalar_one()
    await _create_period_driver_eligibility_rows(
        destination_period_id, 1, hq_branch_id, direct_db,
        snapshot_source="Generated", freeze=True,
    )
    assert (await direct_db.execute(text("""
        SELECT EligibleWorkDates FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodID = :period_id AND DriverAssignmentID = :assignment_id
    """), {"period_id": destination_period_id, "assignment_id": destination_id})).scalar_one()
    termination_eligibility_id = (await direct_db.execute(text("""
        SELECT PayrollPeriodDriverEligibilityID
        FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodID = :period_id AND DriverAssignmentID = :assignment_id
    """), {"period_id": evidence_period_id, "assignment_id": source_id})).scalar_one()
    old_source = (await direct_db.execute(text("""
        SELECT EffectiveFromAt, TransferredToAssignmentID FROM core.DriverAssignments
        WHERE DriverAssignmentID = :id
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
    assert term.json()["cancelled_transfer_request_ids"] == [request["transfer_request_id"]]
    termination_cutoff = await branch_boundary(
        direct_db, paytest_branch_id, today + timedelta(days=1),
    )
    source = (await direct_db.execute(text("""
        SELECT DriverAssignmentID, EndReason, EffectiveFromAt, EffectiveToAt, TransferredToAssignmentID
        FROM core.DriverAssignments WHERE DriverAssignmentID = :source_id
    """), {"source_id": source_id})).mappings().one()
    assert source["endreason"] == "Terminated"
    assert source["effectivetoat"] == termination_cutoff
    assert source["effectivefromat"] == old_source["effectivefromat"]
    assert source["transferredtoassignmentid"] is None
    # The never-effective destination is disposed of, not kept as an empty window, and
    # its roster row (here in a Cancelled period) goes with it.
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM core.DriverAssignments WHERE DriverAssignmentID = :id"
    ), {"id": destination_id})).scalar_one() == 0
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM payroll.PayrollPeriodDriverEligibility WHERE DriverAssignmentID = :id"
    ), {"id": destination_id})).scalar_one() == 0
    assert await resolve_effective_driver_assignment(
        1, driver_id, await branch_boundary(direct_db, paytest_branch_id, today), direct_db,
    )
    assert await resolve_effective_driver_assignment(1, driver_id, termination_cutoff, direct_db) is None
    assert await resolve_effective_driver_assignment(1, driver_id, future_at, direct_db) is None
    # A Scheduled transfer at or after the termination cap can never take effect: Cancelled
    # with no destination.
    request_state = (await direct_db.execute(text(
        "SELECT Status, CancelReason, DestinationDriverAssignmentID FROM core.DriverTransferRequests "
        "WHERE TransferRequestID = :id"
    ), {"id": request["transfer_request_id"]})).mappings().one()
    assert request_state["status"] == "Cancelled"
    assert request_state["destinationdriverassignmentid"] is None
    assert "terminated" in request_state["cancelreason"].lower()
    frozen_eligibility = (await direct_db.execute(text("""
        SELECT DriverStatusSnapshot, EndReasonSnapshot, TerminationDateSnapshot,
               DriverEffectiveToAtSnapshot, FrozenAtUtc
        FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodDriverEligibilityID = :id
    """), {"id": termination_eligibility_id})).mappings().one()
    assert frozen_eligibility["driverstatussnapshot"] == "Active"
    assert frozen_eligibility["endreasonsnapshot"] == "Transferred"
    assert frozen_eligibility["terminationdatesnapshot"] is None
    assert frozen_eligibility["drivereffectivetoatsnapshot"] == future_at
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
    assert "End of employment" in audit and str(request["transfer_request_id"]) in audit


@pytest.mark.asyncio
async def test_skipped_apia_termination_day_clamps_transferred_history_to_next_date_boundary(
    client: httpx.AsyncClient, auth_token: str, direct_db,
):
    marker = suffix()
    apia_branch = int((await direct_db.execute(text("""
        INSERT INTO core.Branches
            (CompanyID, BranchCode, BranchName, Status, IsDefault, TimeZoneId)
        VALUES (1, :code, 'Apia skipped-day source', 'Active', FALSE, 'Pacific/Apia')
        RETURNING BranchID
    """), {"code": f"APIA-{marker}"})).scalar_one())
    new_york_branch = int((await direct_db.execute(text("""
        INSERT INTO core.Branches
            (CompanyID, BranchCode, BranchName, Status, IsDefault, TimeZoneId)
        VALUES (1, :code, 'New York skipped-day destination', 'Active', FALSE, 'America/New_York')
        RETURNING BranchID
    """), {"code": f"NY-{marker}"})).scalar_one())
    created = await client.post("/workforce/employees", json={
        "branch_id": apia_branch,
        "full_name": f"Apia boundary {marker}",
        "hire_date": "2000-01-01",
        "driver": {
            "driver_number": f"APIA-D-{marker}",
            "effective_from_at": "2010-01-01T00:00:00Z",
        },
    }, headers=auth(auth_token))
    assert created.status_code == 201, created.text
    employee_id = created.json()["employee_id"]
    driver_id, source_id = ids_of(created.json())
    original_transfer_boundary = datetime(2011, 12, 30, 12, tzinfo=UTC)
    # Close the source first so the destination does not overlap it, then link them.
    await end_driver_assignment(
        direct_db, source_id, ended_at=original_transfer_boundary, end_reason="Transferred",
    )
    destination_id = await insert_driver_assignment(
        direct_db, company_id=1, branch_id=new_york_branch, employee_id=employee_id,
        driver_id=driver_id, effective_from_at=original_transfer_boundary,
        transferred_from_assignment_id=source_id,
    )
    await end_driver_assignment(
        direct_db, source_id, ended_at=original_transfer_boundary,
        end_reason="Transferred", transferred_to_assignment_id=destination_id,
    )

    terminated = await client.post(
        f"/workforce/employees/{employee_id}/terminate",
        json={"termination_date": "2011-12-30", "reason": "Skipped Apia date boundary"},
        headers=auth(auth_token),
    )
    assert terminated.status_code == 200, terminated.text
    assignments = (await direct_db.execute(text("""
        SELECT DriverAssignmentID, BranchID, EndReason, EffectiveFromAt, EffectiveToAt,
               TransferredFromAssignmentID, TransferredToAssignmentID
        FROM core.DriverAssignments WHERE DriverAssignmentID = ANY(:ids)
    """), {"ids": [source_id, destination_id]})).mappings().all()
    by_id = {row["driverassignmentid"]: row for row in assignments}
    apia_cutoff = datetime(2011, 12, 30, 10, tzinfo=UTC)
    new_york_cutoff = datetime(2011, 12, 31, 5, tzinfo=UTC)
    assert by_id[source_id]["effectivetoat"] == apia_cutoff
    assert by_id[source_id]["endreason"] == "Terminated"
    assert by_id[source_id]["transferredtoassignmentid"] == destination_id
    assert by_id[destination_id]["effectivefromat"] == original_transfer_boundary
    assert by_id[destination_id]["effectivetoat"] == new_york_cutoff
    assert by_id[destination_id]["endreason"] == "Terminated"
    assert by_id[destination_id]["transferredfromassignmentid"] == source_id


@pytest.mark.asyncio
async def test_branch_metrics_count_current_and_historical_projection_without_writing_employee_branch(
    client: httpx.AsyncClient, auth_token: str, direct_db,
):
    marker = suffix()
    branch_rows = (await direct_db.execute(text("""
        INSERT INTO core.Branches
            (CompanyID, BranchCode, BranchName, Status, IsDefault, TimeZoneId)
        VALUES
            (1, :source_code, 'Metrics source', 'Active', FALSE, 'America/New_York'),
            (1, :destination_code, 'Metrics destination', 'Active', FALSE, 'America/Los_Angeles')
        RETURNING BranchID, BranchCode
    """), {
        "source_code": f"MET-S-{marker}", "destination_code": f"MET-D-{marker}",
    })).mappings().all()
    branch_ids_by_code = {row["branchcode"]: int(row["branchid"]) for row in branch_rows}
    source_branch_id = branch_ids_by_code[f"MET-S-{marker}"]
    destination_branch_id = branch_ids_by_code[f"MET-D-{marker}"]
    branch_ids = [source_branch_id, destination_branch_id]
    before = await count_employee_branch_projections(1, branch_ids, direct_db)

    current_employee_response = await client.post("/workforce/employees", json={
        "branch_id": source_branch_id, "full_name": f"Metrics current driver {marker}",
        "hire_date": "2000-01-01",
        "driver": {
            "driver_number": f"MET-S-{marker}",
            "effective_from_at": "2000-01-01T00:00:00Z",
        },
    }, headers=auth(auth_token))
    assert current_employee_response.status_code == 201, current_employee_response.text
    current_employee = current_employee_response.json()
    current_employee_id = current_employee["employee_id"]
    driver_id, source_assignment_id = ids_of(current_employee)
    boundary = datetime(2011, 12, 30, 12, tzinfo=UTC)
    await end_driver_assignment(
        direct_db, source_assignment_id, ended_at=boundary, end_reason="Transferred",
    )
    destination_assignment_id = await insert_driver_assignment(
        direct_db, company_id=1, branch_id=destination_branch_id, employee_id=current_employee_id,
        driver_id=driver_id, effective_from_at=boundary,
        transferred_from_assignment_id=source_assignment_id,
    )
    await end_driver_assignment(
        direct_db, source_assignment_id, ended_at=boundary, end_reason="Transferred",
        transferred_to_assignment_id=destination_assignment_id,
    )

    non_driver = await client.post("/workforce/employees", json={
        "branch_id": source_branch_id, "full_name": f"Metrics non-driver {marker}",
        "hire_date": "2000-01-01",
    }, headers=auth(auth_token))
    assert non_driver.status_code == 201, non_driver.text

    terminated = await client.post("/workforce/employees", json={
        "branch_id": destination_branch_id, "full_name": f"Metrics terminated {marker}",
        "hire_date": "2000-01-01",
        "driver": {
            "driver_number": f"MET-T-{marker}",
            "effective_from_at": "2000-01-01T00:00:00Z",
        },
    }, headers=auth(auth_token))
    assert terminated.status_code == 201, terminated.text
    terminated_employee_id = terminated.json()["employee_id"]
    terminated_driver_id = terminated.json()["driver"]["driver_id"]
    termination = await client.post(
        f"/workforce/employees/{terminated_employee_id}/terminate",
        json={"termination_date": "2011-12-30", "reason": "Metrics history fixture"},
        headers=auth(auth_token),
    )
    assert termination.status_code == 200, termination.text
    await direct_db.execute(text("""
        UPDATE core.Employees SET BranchID = :stale_branch_id
        WHERE EmployeeID = :employee_id AND CompanyID = 1
    """), {"stale_branch_id": source_branch_id, "employee_id": terminated_employee_id})
    assert await resolve_effective_driver_assignment(
        1, terminated_driver_id, None, direct_db,
    ) is None

    metrics = await fetch_branch_metrics(1, branch_ids, direct_db)
    after = await count_employee_branch_projections(1, branch_ids, direct_db)
    assert after.get(source_branch_id, 0) - before.get(source_branch_id, 0) == 1
    assert after.get(destination_branch_id, 0) - before.get(destination_branch_id, 0) == 2
    assert metrics.get(source_branch_id, {}).get("total_people_count", 0) - before.get(source_branch_id, 0) == 1
    assert metrics.get(destination_branch_id, {}).get("total_people_count", 0) - before.get(destination_branch_id, 0) == 2
    assert metrics.get(source_branch_id, {}).get("active_drivers_count", 0) == 0
    assert metrics[destination_branch_id]["active_drivers_count"] == 1
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :employee_id"
    ), {"employee_id": current_employee_id})).scalar_one() == source_branch_id
    assert (await direct_db.execute(text(
        "SELECT BranchID FROM core.Employees WHERE EmployeeID = :employee_id"
    ), {"employee_id": terminated_employee_id})).scalar_one() == source_branch_id

    destination_only = await count_employee_branch_projections(1, [destination_branch_id], direct_db)
    assert destination_only[destination_branch_id] == after[destination_branch_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("approve_first", [False, True], ids=["pending", "approved"])
async def test_termination_cancels_uncompleted_transfer_request(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int,
    direct_db, approve_first: bool,
):
    today = await branch_today(direct_db, paytest_branch_id)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    driver_id, _ = ids_of(employee)
    request = await create_transfer(
        client, auth_token, driver_id, hq_branch_id,
        await branch_boundary(direct_db, paytest_branch_id, today + timedelta(days=20)),
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
        "SELECT COUNT(*) FROM core.DriverAssignments WHERE EmployeeID = :id"
    ), {"id": employee["employee_id"]})).scalar_one() == 1


@pytest.mark.asyncio
async def test_backdated_termination_reconciles_employee_to_branch_effective_on_date(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, hq_branch_id: int, direct_db,
):
    today = await branch_today(direct_db, hq_branch_id)
    termination_date = today - timedelta(days=1)
    employee = await create_driver_employee(client, auth_token, hq_branch_id)
    employee_id = employee["employee_id"]
    _, assignment_id = ids_of(employee)

    # Model a stale stored projection that disagrees with the assignment effective on D.
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
        SELECT e.BranchID AS EmployeeBranchID, a.BranchID AS AssignmentBranchID,
               a.EndReason, a.EffectiveToAt
        FROM core.Employees e JOIN core.DriverAssignments a ON a.EmployeeID = e.EmployeeID
        WHERE e.EmployeeID = :employee_id AND a.DriverAssignmentID = :assignment_id
    """), {"employee_id": employee_id, "assignment_id": assignment_id})).mappings().one()
    assert state["endreason"] == "Terminated"
    assert state["effectivetoat"] == await branch_boundary(
        direct_db, hq_branch_id, termination_date + timedelta(days=1),
    )
    assert state["employeebranchid"] == hq_branch_id
    assert state["assignmentbranchid"] == hq_branch_id


@pytest.mark.asyncio
async def test_termination_before_first_driver_assignment_is_rejected_without_mutation(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    today = await branch_today(direct_db, hq_branch_id)
    future = today + timedelta(days=2)
    future_at = await branch_boundary(direct_db, hq_branch_id, future)
    employee = await client.post("/workforce/employees", json={
        "branch_id": hq_branch_id,
        "full_name": f"P1C pending termination {suffix()}",
        "hire_date": "2020-01-01",
        "driver": {"driver_number": f"P1C-{suffix()}", "effective_from_at": future_at.isoformat()},
    }, headers=auth(auth_token))
    assert employee.status_code == 201, employee.text
    employee_id = employee.json()["employee_id"]
    _, assignment_id = ids_of(employee.json())

    rejected = await client.post(
        f"/workforce/employees/{employee_id}/terminate",
        json={"termination_date": today.isoformat(), "reason": "Before first assignment"},
        headers=auth(auth_token),
    )
    assert rejected.status_code == 422, rejected.text
    state = (await direct_db.execute(text("""
        SELECT e.EmploymentStatus, e.TerminationDate, a.EndReason, a.EffectiveFromAt, a.EffectiveToAt
        FROM core.Employees e JOIN core.DriverAssignments a ON a.EmployeeID = e.EmployeeID
        WHERE e.EmployeeID = :employee_id AND a.DriverAssignmentID = :assignment_id
    """), {"employee_id": employee_id, "assignment_id": assignment_id})).mappings().one()
    assert (state["employmentstatus"], state["terminationdate"]) == ("Active", None)
    assert (state["endreason"], state["effectivefromat"], state["effectivetoat"]) == (None, future_at, None)


@pytest.mark.asyncio
async def test_termination_before_hire_date_is_rejected_without_mutation(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db,
):
    today = await branch_today(direct_db, hq_branch_id)
    hire_date = today + timedelta(days=1)
    hire_at = await branch_boundary(direct_db, hq_branch_id, hire_date)
    employee = await client.post("/workforce/employees", json={
        "branch_id": hq_branch_id,
        "full_name": f"P1C future hire termination {suffix()}",
        "hire_date": hire_date.isoformat(),
        "driver": {"driver_number": f"P1C-{suffix()}", "effective_from_at": hire_at.isoformat()},
    }, headers=auth(auth_token))
    assert employee.status_code == 201, employee.text
    employee_id = employee.json()["employee_id"]
    _, assignment_id = ids_of(employee.json())

    rejected = await client.post(
        f"/workforce/employees/{employee_id}/terminate",
        json={"termination_date": today.isoformat(), "reason": "Before hire"},
        headers=auth(auth_token),
    )
    assert rejected.status_code == 422, rejected.text
    state = (await direct_db.execute(text("""
        SELECT e.EmploymentStatus, e.TerminationDate, a.EndReason, a.EffectiveFromAt, a.EffectiveToAt
        FROM core.Employees e JOIN core.DriverAssignments a ON a.EmployeeID = e.EmployeeID
        WHERE e.EmployeeID = :employee_id AND a.DriverAssignmentID = :assignment_id
    """), {"employee_id": employee_id, "assignment_id": assignment_id})).mappings().one()
    assert (state["employmentstatus"], state["terminationdate"]) == ("Active", None)
    assert (state["endreason"], state["effectivefromat"], state["effectivetoat"]) == (None, hire_at, None)


@pytest.mark.asyncio
async def test_termination_rejects_dated_finalized_work_after_date_without_mutation(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    today = await branch_today(direct_db, paytest_branch_id)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    driver_id, assignment_id = ids_of(employee)
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
    chain = await seed_snapshot_component(
        direct_db, period_id=period_id, driver_assignment_id=assignment_id, work_date=later)
    final_line_id = await insert_final_line(direct_db, chain)

    rejected = await client.post(
        f"/workforce/employees/{employee['employee_id']}/terminate",
        json={"termination_date": today.isoformat(), "reason": "Too early"},
        headers=auth(auth_token),
    )
    assert rejected.status_code == 422, rejected.text
    status = (await direct_db.execute(text("""
        SELECT e.EmploymentStatus, a.EndReason
        FROM core.Employees e JOIN core.DriverAssignments a ON a.EmployeeID = e.EmployeeID
        WHERE e.EmployeeID = :employee_id
    """), {"employee_id": employee["employee_id"]})).mappings().one()
    assert (status["employmentstatus"], status["endreason"]) == ("Active", None)
    assert (await direct_db.execute(text(
        "SELECT COUNT(*) FROM payroll.PayrollFinalLines WHERE FinalLineID = :id"
    ), {"id": final_line_id})).scalar_one() == 1


@pytest.mark.asyncio
async def test_termination_preserves_snapshot_and_exact_date_existing_source_rescue(
    client: httpx.AsyncClient, auth_token: str, paytest_branch_id: int, direct_db,
):
    today = await branch_today(direct_db, paytest_branch_id)
    next_day = today + timedelta(days=1)
    employee = await create_driver_employee(client, auth_token, paytest_branch_id)
    _, assignment_id = ids_of(employee)
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
            (CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, SourceEmployeeID,
             IsEligibleForPeriod, EligibilityReasonCode, SnapshotSource,
             HireDateSnapshot, TerminationDateSnapshot, DriverEffectiveFromAtSnapshot,
             BranchTimeZoneIdSnapshot, EligibleWorkDates)
        VALUES (1, :branch_id, :period_id, :assignment_id, :employee_id,
                TRUE, 'Active', 'Generated', DATE '2020-01-01', NULL,
                TIMESTAMPTZ '2020-01-01 00:00:00+00', 'America/New_York', ARRAY[:today]::DATE[])
        RETURNING PayrollPeriodDriverEligibilityID
    """), {"branch_id": paytest_branch_id, "period_id": period_id,
           "assignment_id": assignment_id, "employee_id": employee["employee_id"],
           "today": today})).scalar_one()
    await direct_db.execute(text("""
        INSERT INTO payroll.PayrollDraftLines
            (CompanyID, BranchID, PayrollPeriodID, DriverAssignmentID, WorkDate, LineType, SourceType, Status)
        VALUES (1, :branch_id, :period_id, :assignment_id, :work_date, 'HOURS', 'P1cTest', 'Active')
    """), {"branch_id": paytest_branch_id, "period_id": period_id,
           "assignment_id": assignment_id, "work_date": next_day})
    await client.post(
        f"/workforce/employees/{employee['employee_id']}/terminate",
        json={"termination_date": today.isoformat(), "reason": "Historical cutoff"},
        headers=auth(auth_token),
    )

    await _assert_driver_eligible_for_workdate_via_snapshot(
        1, paytest_branch_id, period_id, assignment_id, today, direct_db,
    )
    await _assert_driver_eligible_for_workdate_via_snapshot(
        1, paytest_branch_id, period_id, assignment_id, next_day, direct_db,
        allow_existing_source_rescue=True,
    )
    with pytest.raises(HTTPException):
        await _assert_driver_eligible_for_workdate_via_snapshot(
            1, paytest_branch_id, period_id, assignment_id, next_day, direct_db,
            allow_existing_source_rescue=False,
        )
    snapshot_row = (await direct_db.execute(text("""
        SELECT TerminationDateSnapshot, DriverEffectiveToAtSnapshot
        FROM payroll.PayrollPeriodDriverEligibility
        WHERE PayrollPeriodDriverEligibilityID = :id
    """), {"id": eligibility_id})).mappings().one()
    assert snapshot_row["terminationdatesnapshot"] is None
    assert snapshot_row["drivereffectivetoatsnapshot"] is None


@pytest.mark.asyncio
async def test_termination_audit_failure_rolls_back_employee_and_assignment(
    client: httpx.AsyncClient, auth_token: str, hq_branch_id: int, direct_db, monkeypatch,
):
    today = await branch_today(direct_db, hq_branch_id)
    employee = await create_driver_employee(client, auth_token, hq_branch_id)
    from app.workforce import audit as workforce_audit

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("forced P1c audit failure")

    monkeypatch.setattr(workforce_audit, "write_workforce_audit", fail_audit)
    with pytest.raises(RuntimeError, match="forced P1c audit failure"):
        await client.post(
            f"/workforce/employees/{employee['employee_id']}/terminate",
            json={"termination_date": today.isoformat(), "reason": "Rollback proof"},
            headers=auth(auth_token),
        )
    state = (await direct_db.execute(text("""
        SELECT e.EmploymentStatus, e.TerminationDate, a.EndReason, a.EffectiveToAt
        FROM core.Employees e JOIN core.DriverAssignments a ON a.EmployeeID = e.EmployeeID
        WHERE e.EmployeeID = :employee_id
    """), {"employee_id": employee["employee_id"]})).mappings().one()
    assert state["employmentstatus"] == "Active"
    assert state["terminationdate"] is None
    assert state["endreason"] is None
    assert state["effectivetoat"] is None


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
