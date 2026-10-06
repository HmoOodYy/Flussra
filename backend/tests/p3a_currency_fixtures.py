"""Shared fixtures for the P3a Company currency tests.

DriverRate is the current durable monetary row the currency tests use: it is
checked by core.fn_company_has_durable_monetary_state and needs only a
company, branch, driver and RateType to exist.
"""
from __future__ import annotations

from sqlalchemy import text

INSERT_DRIVER_RATE = text("""
    INSERT INTO payroll.driverrates
        (companyid, branchid, driverid, ratetypeid, amount, effectivefrom, status,
         createdbyuserid, approvedbyuserid, approvedatutc)
    VALUES (:cid, :bid, :did, :rid, :amount, DATE '2099-01-01', 'Approved', 1, 1, NOW())
    RETURNING driverrateid
""")


async def create_branch_and_driver(db, company_id: int, marker: str) -> tuple[int, int]:
    """Create a branch and an active driver for an existing company."""
    branch_id = int((await db.execute(text("""
        INSERT INTO core.branches(companyid, branchcode, branchname, status, isdefault)
        VALUES (:cid, :code, 'P3a currency', 'Active', TRUE) RETURNING branchid
    """), {"cid": company_id, "code": f"B_{marker}"[:20]})).scalar_one())
    employee_id = int((await db.execute(text("""
        INSERT INTO core.employees(companyid, branchid, fullname, employeetype, employmentstatus, createdbyuserid)
        VALUES (:cid, :bid, 'P3a currency', 'Driver', 'Active', 1) RETURNING employeeid
    """), {"cid": company_id, "bid": branch_id})).scalar_one())
    driver_id = int((await db.execute(text("""
        INSERT INTO core.drivers(companyid, branchid, employeeid, drivercode, driverstatus)
        VALUES (:cid, :bid, :eid, :code, 'Active') RETURNING driverid
    """), {"cid": company_id, "bid": branch_id, "eid": employee_id, "code": f"D_{marker}"[:20]})).scalar_one())
    return branch_id, driver_id


async def first_rate_type_id(db) -> int:
    return int((await db.execute(
        text("SELECT ratetypeid FROM payroll.ratetypes ORDER BY ratetypeid LIMIT 1")
    )).scalar_one())
