"""Canonical target Compensation assignment resolver.

Resolves configuration only: for a Driver, a RateDefinition and a WorkDate it
selects exactly one whole DriverRateAssignment and returns that assignment's
complete value set. It does not calculate payroll and is not called by any
payroll runtime path.

Only Approved and Superseded assignments are authoritative schedules; Pending
and Voided assignments are never selected. Windows are inclusive on both ends.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.compensation.errors import CompensationIntegrityError, CompensationOwnershipError
from app.compensation.schemas import ResolvedCompensation, ResolvedComponent


async def resolve(
    company_id: int,
    driver_id: int,
    rate_definition_id: int,
    work_date: date,
    db: AsyncConnection,
) -> ResolvedCompensation | None:
    """Return the schedule effective on work_date, or None when none exists.

    Raises CompensationOwnershipError when the Driver or RateDefinition does not
    belong to the Company, and CompensationIntegrityError when more than one
    authoritative schedule applies (which the database should make impossible).
    """
    driver = (await db.execute(
        text("SELECT branchid FROM core.drivers WHERE driverid = :did AND companyid = :cid"),
        {"did": driver_id, "cid": company_id},
    )).mappings().first()
    if driver is None:
        raise CompensationOwnershipError("Driver does not belong to the Company.")

    definition = (await db.execute(
        text("""
            SELECT rd.shape, rd.paydefinitionid, pd.calculationmethod
            FROM   payroll.ratedefinitions rd
            LEFT JOIN payroll.paydefinitions pd ON pd.paydefinitionid = rd.paydefinitionid
            WHERE  rd.ratedefinitionid = :rid AND rd.companyid = :cid
        """),
        {"rid": rate_definition_id, "cid": company_id},
    )).mappings().first()
    if definition is None:
        raise CompensationOwnershipError("RateDefinition does not belong to the Company.")

    assignments = (await db.execute(
        text("""
            SELECT driverrateassignmentid, status, effectivefrom, effectiveto
            FROM   payroll.driverrateassignments
            WHERE  companyid = :cid AND driverid = :did AND ratedefinitionid = :rid
              AND  status IN ('Approved', 'Superseded')
              AND  effectivefrom <= :work_date
              AND  (effectiveto IS NULL OR effectiveto >= :work_date)
        """),
        {"cid": company_id, "did": driver_id, "rid": rate_definition_id,
         "work_date": work_date},
    )).mappings().all()
    if not assignments:
        return None
    if len(assignments) > 1:
        raise CompensationIntegrityError(
            "More than one authoritative assignment applies on the requested date.")
    assignment = assignments[0]

    components = (await db.execute(
        text("""
            SELECT c.ratecomponentdefinitionid AS rate_component_definition_id,
                   c.sequenceno AS sequence_no, c.ordinalfrom AS ordinal_from,
                   c.ordinalto AS ordinal_to, v.amount AS amount
            FROM   payroll.ratecomponentdefinitions c
            LEFT JOIN payroll.driverratevalues v
                   ON v.ratecomponentdefinitionid = c.ratecomponentdefinitionid
                  AND v.driverrateassignmentid = :aid
            WHERE  c.ratedefinitionid = :rid
            ORDER  BY c.sequenceno
        """),
        {"aid": assignment["driverrateassignmentid"], "rid": rate_definition_id},
    )).mappings().all()

    return ResolvedCompensation(
        company_id=company_id,
        driver_id=driver_id,
        branch_id=driver["branchid"],
        pay_definition_id=definition["paydefinitionid"],
        rate_definition_id=rate_definition_id,
        rate_shape=definition["shape"],
        calculation_method=definition["calculationmethod"],
        driver_rate_assignment_id=assignment["driverrateassignmentid"],
        assignment_status=assignment["status"],
        effective_from=assignment["effectivefrom"],
        effective_to=assignment["effectiveto"],
        components=[ResolvedComponent.model_validate(dict(row)) for row in components],
    )
