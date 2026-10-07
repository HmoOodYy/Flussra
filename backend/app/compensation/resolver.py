"""Canonical target Compensation assignment resolver.

Resolves configuration only: for a Driver, a RateDefinition and a WorkDate it
selects exactly one whole DriverRateAssignment and returns that assignment's
complete value set. It does not calculate payroll.

Only Approved and Superseded assignments are authoritative schedules; Pending
and Voided assignments are never selected. Windows are inclusive on both ends.

The result is shape-agnostic: ``ResolvedCompensation.components`` carries every
RateComponentDefinition of the RateDefinition with its own value, in sequence
order. A Scalar definition resolves to one component; an OrdinalTierSchedule
resolves to its ordinal ranges. Components are never resolved independently:
the assignment is selected first and all of its values come from it.

``resolve_many`` is the one implementation. ``resolve`` is the single-key form of
the same call, so the two cannot drift.
"""

from collections.abc import Iterable
from datetime import date
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.compensation.errors import CompensationIntegrityError, CompensationOwnershipError
from app.compensation.schemas import ResolvedCompensation, ResolvedComponent


class ResolutionKey(NamedTuple):
    """The identity of one resolution: a Driver, a RateDefinition and a WorkDate."""

    driver_id: int
    rate_definition_id: int
    work_date: date


async def resolve_many(
    company_id: int,
    keys: Iterable[ResolutionKey],
    db: AsyncConnection,
) -> dict[ResolutionKey, ResolvedCompensation | None]:
    """Resolve every distinct key set-wise.

    Duplicate keys are resolved once. The result has one entry per distinct key:
    the whole effective schedule on that date, or None when none exists.

    Raises CompensationOwnershipError when a Driver or RateDefinition does not
    belong to the Company, and CompensationIntegrityError when more than one
    authoritative schedule applies to one key (which the database makes impossible).
    """
    distinct = sorted(set(keys), key=lambda k: (k.driver_id, k.rate_definition_id, k.work_date))
    if not distinct:
        return {}

    driver_ids = sorted({k.driver_id for k in distinct})
    rate_definition_ids = sorted({k.rate_definition_id for k in distinct})

    drivers = {
        row["driverid"]: row["branchid"]
        for row in (await db.execute(
            text("""
                SELECT driverid, branchid FROM core.drivers
                WHERE  companyid = :cid AND driverid = ANY(:driver_ids)
            """),
            {"cid": company_id, "driver_ids": driver_ids},
        )).mappings().all()
    }
    if set(driver_ids) - drivers.keys():
        raise CompensationOwnershipError("Driver does not belong to the Company.")

    definitions = {
        row["ratedefinitionid"]: row
        for row in (await db.execute(
            text("""
                SELECT rd.ratedefinitionid, rd.shape, rd.paydefinitionid, pd.calculationmethod
                FROM   payroll.ratedefinitions rd
                LEFT JOIN payroll.paydefinitions pd ON pd.paydefinitionid = rd.paydefinitionid
                WHERE  rd.companyid = :cid AND rd.ratedefinitionid = ANY(:rids)
            """),
            {"cid": company_id, "rids": rate_definition_ids},
        )).mappings().all()
    }
    if set(rate_definition_ids) - definitions.keys():
        raise CompensationOwnershipError("RateDefinition does not belong to the Company.")

    matches = (await db.execute(
        text("""
            SELECT k.driverid, k.ratedefinitionid, k.workdate,
                   a.driverrateassignmentid, a.status, a.effectivefrom, a.effectiveto
            FROM   unnest(CAST(:driver_ids AS integer[]),
                          CAST(:rids AS integer[]),
                          CAST(:dates AS date[]))
                   AS k(driverid, ratedefinitionid, workdate)
            JOIN   payroll.driverrateassignments a
                   ON  a.companyid        = :cid
                   AND a.driverid         = k.driverid
                   AND a.ratedefinitionid = k.ratedefinitionid
                   AND a.status IN ('Approved', 'Superseded')
                   AND a.effectivefrom   <= k.workdate
                   AND (a.effectiveto IS NULL OR a.effectiveto >= k.workdate)
        """),
        {
            "cid": company_id,
            "driver_ids": [k.driver_id for k in distinct],
            "rids": [k.rate_definition_id for k in distinct],
            "dates": [k.work_date for k in distinct],
        },
    )).mappings().all()

    selected: dict[ResolutionKey, dict] = {}
    for row in matches:
        key = ResolutionKey(row["driverid"], row["ratedefinitionid"], row["workdate"])
        if key in selected:
            raise CompensationIntegrityError(
                "More than one authoritative assignment applies on the requested date.")
        selected[key] = dict(row)

    assignment_ids = sorted({row["driverrateassignmentid"] for row in selected.values()})
    components_by_assignment: dict[int, list[ResolvedComponent]] = {}
    if assignment_ids:
        component_rows = (await db.execute(
            text("""
                SELECT a.driverrateassignmentid AS assignment_id,
                       c.ratecomponentdefinitionid AS rate_component_definition_id,
                       c.shape AS shape, c.sequenceno AS sequence_no,
                       c.ordinalfrom AS ordinal_from, c.ordinalto AS ordinal_to,
                       v.amount AS amount
                FROM   payroll.driverrateassignments a
                JOIN   payroll.ratecomponentdefinitions c
                       ON c.ratedefinitionid = a.ratedefinitionid
                LEFT JOIN payroll.driverratevalues v
                       ON v.ratecomponentdefinitionid = c.ratecomponentdefinitionid
                      AND v.driverrateassignmentid = a.driverrateassignmentid
                WHERE  a.driverrateassignmentid = ANY(:aids)
                ORDER  BY a.driverrateassignmentid, c.sequenceno
            """),
            {"aids": assignment_ids},
        )).mappings().all()
        for row in component_rows:
            components_by_assignment.setdefault(row["assignment_id"], []).append(
                ResolvedComponent.model_validate(
                    {k: v for k, v in dict(row).items() if k != "assignment_id"}))

    results: dict[ResolutionKey, ResolvedCompensation | None] = {}
    for key in distinct:
        row = selected.get(key)
        if row is None:
            results[key] = None
            continue
        definition = definitions[key.rate_definition_id]
        results[key] = ResolvedCompensation(
            company_id=company_id,
            driver_id=key.driver_id,
            branch_id=drivers[key.driver_id],
            pay_definition_id=definition["paydefinitionid"],
            rate_definition_id=key.rate_definition_id,
            rate_shape=definition["shape"],
            calculation_method=definition["calculationmethod"],
            driver_rate_assignment_id=row["driverrateassignmentid"],
            assignment_status=row["status"],
            effective_from=row["effectivefrom"],
            effective_to=row["effectiveto"],
            components=list(components_by_assignment.get(row["driverrateassignmentid"], [])),
        )
    return results


async def resolve(
    company_id: int,
    driver_id: int,
    rate_definition_id: int,
    work_date: date,
    db: AsyncConnection,
) -> ResolvedCompensation | None:
    """Return the schedule effective on work_date, or None when none exists."""
    key = ResolutionKey(driver_id, rate_definition_id, work_date)
    return (await resolve_many(company_id, [key], db))[key]
