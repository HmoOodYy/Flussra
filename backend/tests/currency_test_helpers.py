"""Shared fixtures for the P3a Company currency tests.

A complete Approved Driver-owned schedule is durable monetary history. Pending
schedules and operational source quantities do not lock a Company currency.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import text


async def create_branch_and_driver(db, company_id: int, marker: str) -> tuple[int, int]:
    """Create a branch and an active driver for an existing company."""
    branch_id = int((await db.execute(text("""
        INSERT INTO core.branches(companyid, branchcode, branchname, status, isdefault, timezoneid)
        VALUES (:cid, :code, 'P3a currency', 'Active', TRUE, 'UTC') RETURNING branchid
    """), {"cid": company_id, "code": f"B_{marker}"[:20]})).scalar_one())
    employee_id = int((await db.execute(text("""
        INSERT INTO core.employees(companyid, branchid, fullname, employmentstatus, createdbyuserid)
        VALUES (:cid, :bid, 'P3a currency', 'Active', 1) RETURNING employeeid
    """), {"cid": company_id, "bid": branch_id})).scalar_one())
    driver_id = int((await db.execute(text("""
        INSERT INTO core.drivers(companyid, employeeid, drivernumber, driverstatus)
        VALUES (:cid, :eid, :code, 'Active') RETURNING driverid
    """), {"cid": company_id, "eid": employee_id, "code": f"D_{marker}"[:20]})).scalar_one())
    driver_assignment_id = int((await db.execute(text("""
        INSERT INTO core.driverassignments(companyid, branchid, employeeid, driverid, effectivefromat)
        VALUES (:cid, :bid, :eid, :did, NOW() - INTERVAL '1 day') RETURNING driverassignmentid
    """), {"cid": company_id, "bid": branch_id, "eid": employee_id, "did": driver_id})).scalar_one())
    return branch_id, driver_assignment_id


async def create_rate_requirement(db, company_id: int) -> dict[str, int]:
    from uuid import uuid4

    definition_id = int((await db.execute(text("""
        INSERT INTO payroll.PayDefinitions
            (CompanyID, DefinitionCode, DefinitionName, InputType, CalculationMethod)
        VALUES (:cid, :code, 'Currency test measure', 'Decimal', 'PerUnit')
        RETURNING PayDefinitionID
    """), {"cid": company_id, "code": "CURRENCY_" + uuid4().hex[:12]})).scalar_one())
    rate_id = int((await db.execute(text("""
        INSERT INTO payroll.RateDefinitions (CompanyID, PayDefinitionID, Shape)
        VALUES (:cid, :pd, 'Scalar') RETURNING RateDefinitionID
    """), {"cid": company_id, "pd": definition_id})).scalar_one())
    component_id = int((await db.execute(text("""
        INSERT INTO payroll.RateComponentDefinitions (RateDefinitionID, Shape, SequenceNo)
        VALUES (:rd, 'Scalar', 1) RETURNING RateComponentDefinitionID
    """), {"rd": rate_id})).scalar_one())
    return {"pay_definition_id": definition_id, "rate_definition_id": rate_id,
            "component_id": component_id}


async def insert_approved_schedule(db, params: dict) -> int:
    """Write a complete target schedule through its Pending/value/approval lifecycle."""
    schedule_id = int((await db.execute(text("""
        INSERT INTO payroll.DriverRateSchedules
            (CompanyID, DriverID, PayDefinitionID, RateDefinitionID, EffectiveFrom, CreatedByUserID)
        SELECT :cid, a.DriverID, :pay_definition_id, :rate_definition_id, DATE '2099-01-01', 1
        FROM core.DriverAssignments a WHERE a.DriverAssignmentID = :did
        RETURNING DriverRateScheduleID
    """), params)).scalar_one())
    await db.execute(text("""
        INSERT INTO payroll.DriverRateScheduleValues
            (DriverRateScheduleID, RateDefinitionID, RateComponentDefinitionID, Amount)
        VALUES (:sid, :rate_definition_id, :component_id, :amount)
    """), {**params, "sid": schedule_id, "amount": Decimal(str(params["amount"]))})
    await db.execute(text("""
        UPDATE payroll.DriverRateSchedules
        SET Status = 'Approved', ApprovedByUserID = 1, ApprovedAtUtc = NOW()
        WHERE DriverRateScheduleID = :sid
    """), {"sid": schedule_id})
    return schedule_id
