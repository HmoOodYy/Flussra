"""Seed helpers for the target period runtime in the shared application test database.

Direct SQL seeding of the target model: a PayDefinition with its RateDefinition and
component, an applicable Branch configuration, the frozen PayrollPeriodDefinition of a
period, approved scalar rates and ordinary source rows. Helpers do not commit; callers
own the transaction. Codes are unique per call and carry no meaning.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

COMPANY_ID = 1


@dataclass(frozen=True)
class SeededDefinition:
    payroll_period_definition_id: int
    pay_definition_id: int
    rate_definition_id: int
    component_id: int
    code: str


async def seed_period_definition(
    db: AsyncConnection, *, period_id: int, branch_id: int, company_id: int = COMPANY_ID,
    name: str = "Item alpha", input_type: str = "Decimal", unit: str | None = "unit",
    active: bool = True,
) -> SeededDefinition:
    """A new PayDefinition frozen into the period's layout (and its Branch configuration)."""
    code = "T" + uuid4().hex[:12].upper()
    pay_definition_id = (await db.execute(text("""
        INSERT INTO payroll.paydefinitions
            (companyid, definitioncode, definitionname, inputtype, unit, calculationmethod)
        VALUES (:cid, :code, :name, :input_type, :unit, 'PerUnit') RETURNING paydefinitionid
    """), {"cid": company_id, "code": code, "name": name, "input_type": input_type,
           "unit": unit})).scalar_one()
    from tests.ownership import record_owned_pay_definition

    record_owned_pay_definition(db, company_id, pay_definition_id)
    rate_definition_id = (await db.execute(text("""
        INSERT INTO payroll.ratedefinitions (companyid, paydefinitionid, shape)
        VALUES (:cid, :pd, 'Scalar') RETURNING ratedefinitionid
    """), {"cid": company_id, "pd": pay_definition_id})).scalar_one()
    component_id = (await db.execute(text("""
        INSERT INTO payroll.ratecomponentdefinitions (ratedefinitionid, shape, sequenceno)
        VALUES (:rd, 'Scalar', 1) RETURNING ratecomponentdefinitionid
    """), {"rd": rate_definition_id})).scalar_one()
    config_id = (await db.execute(text("""
        INSERT INTO payroll.branchpaydefinitionconfig
            (companyid, branchid, paydefinitionid, isactive, effectivefrom)
        VALUES (:cid, :bid, :pd, :active, DATE '2000-01-01') RETURNING configid
    """), {"cid": company_id, "bid": branch_id, "pd": pay_definition_id,
           "active": active})).scalar_one()
    await db.execute(text("""
        UPDATE payroll.ratedefinitions SET structurelockedatutc = now()
        WHERE ratedefinitionid = :rd
    """), {"rd": rate_definition_id})
    ppd_id = (await db.execute(text("""
        INSERT INTO payroll.payrollperioddefinitions
            (payrollperiodid, companyid, branchid, paydefinitionid, ratedefinitionid,
             definitioncodesnapshot, definitionnamesnapshot, inputtypesnapshot, unitsnapshot,
             calculationmethodsnapshot, calculationmethodversionsnapshot, rateshapesnapshot,
             definitionstatusatsnapshot, isactiveinperiod, sortorder,
             sourcebranchconfigid, sourcebranchconfigeffectivefrom)
        VALUES (:pid, :cid, :bid, :pd, :rd, :code, :name, :input_type, :unit,
                'PerUnit', 1, 'Scalar', 'Active', :active,
                (SELECT count(*) FROM payroll.payrollperioddefinitions WHERE payrollperiodid = :pid),
                :config, DATE '2000-01-01')
        RETURNING payrollperioddefinitionid
    """), {"pid": period_id, "cid": company_id, "bid": branch_id, "pd": pay_definition_id,
           "rd": rate_definition_id, "code": code, "name": name, "input_type": input_type,
           "unit": unit, "active": active, "config": config_id})).scalar_one()
    return SeededDefinition(ppd_id, pay_definition_id, rate_definition_id, component_id, code)


async def seed_approved_rate(
    db: AsyncConnection, *, definition: SeededDefinition, driver_assignment_id: int,
    amount: str | Decimal, effective_from: date | str = "2000-01-01",
    effective_to: date | str | None = None, company_id: int = COMPANY_ID, approver_id: int = 1,
) -> int:
    """An Approved scalar schedule for the Driver of the assignment (Pending, valued, approved).

    Schedules belong to the permanent Driver, so the assignment only names which Driver.
    """
    schedule_id = (await db.execute(text("""
        INSERT INTO payroll.driverrateschedules
            (companyid, driverid, paydefinitionid, ratedefinitionid, effectivefrom, effectiveto)
        SELECT :cid, da.driverid, :pd, :rd, :efrom, :eto
        FROM core.driverassignments da WHERE da.driverassignmentid = :did
        RETURNING driverratescheduleid
    """), {"cid": company_id, "did": driver_assignment_id, "pd": definition.pay_definition_id,
           "rd": definition.rate_definition_id, "efrom": _date(effective_from),
           "eto": _date(effective_to)})).scalar_one()
    await db.execute(text("""
        INSERT INTO payroll.driverrateschedulevalues
            (driverratescheduleid, ratedefinitionid, ratecomponentdefinitionid, amount)
        VALUES (:sid, :rd, :component, :amount)
    """), {"sid": schedule_id, "rd": definition.rate_definition_id,
           "component": definition.component_id, "amount": Decimal(str(amount))})
    await db.execute(text("""
        UPDATE payroll.driverrateschedules
        SET status = 'Approved', approvedbyuserid = :uid, approvedatutc = now()
        WHERE driverratescheduleid = :sid
    """), {"sid": schedule_id, "uid": approver_id})
    return int(schedule_id)


async def seed_target_line(
    db: AsyncConnection, *, period_id: int, branch_id: int, driver_assignment_id: int,
    definition: SeededDefinition | int, quantity: Decimal | int | str, work_date: date | str,
    company_id: int = COMPANY_ID, source_type: str = "Manual", user_id: int = 1,
) -> int:
    """An ordinary source fact against a period definition: no LineType, no stored money."""
    ppd_id = (definition if isinstance(definition, int)
              else definition.payroll_period_definition_id)
    return int((await db.execute(text("""
        INSERT INTO payroll.payrolldraftlines
            (companyid, branchid, payrollperiodid, driverassignmentid, workdate,
             payrollperioddefinitionid, quantity, sourcetype, status, addedbyuserid)
        VALUES (:cid, :bid, :pid, :did, :wd, :ppd, :qty, :src, 'Active', :uid)
        RETURNING draftlineid
    """), {"cid": company_id, "bid": branch_id, "pid": period_id, "did": driver_assignment_id,
           "wd": _date(work_date), "ppd": ppd_id, "qty": Decimal(str(quantity)),
           "src": source_type, "uid": user_id})).scalar_one())


def _date(value):
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)
