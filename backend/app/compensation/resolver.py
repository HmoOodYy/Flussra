"""Canonical target Compensation schedule resolver.

Resolves configuration only: for a permanent Driver, a RateDefinition and a WorkDate it
selects exactly one whole DriverRateSchedule and returns that schedule's complete value
set. It does not calculate payroll. A transfer never changes the answer: schedules belong
to the Driver, not to the Branch or assignment the work was done under.

Only Approved and Superseded schedules are authoritative; Pending and Voided schedules
are never selected. Windows are inclusive on both ends.

The result is shape-agnostic: ``ResolvedCompensation.components`` carries every
RateComponentDefinition of the RateDefinition with its own value, in sequence
order. A Scalar definition resolves to one component; an OrdinalTierSchedule
resolves to its ordinal ranges. Components are never resolved independently:
the schedule is selected first and all of its values come from it.

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
    """The identity of one resolution: a permanent Driver, a RateDefinition and a WorkDate."""

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

    found_drivers = {
        row["driverid"]
        for row in (await db.execute(
            text("""
                SELECT driverid FROM core.drivers
                WHERE  companyid = :cid AND driverid = ANY(:driver_ids)
            """),
            {"cid": company_id, "driver_ids": driver_ids},
        )).mappings().all()
    }
    if set(driver_ids) - found_drivers:
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
                   s.driverratescheduleid, s.status, s.effectivefrom, s.effectiveto
            FROM   unnest(CAST(:driver_ids AS integer[]),
                          CAST(:rids AS integer[]),
                          CAST(:dates AS date[]))
                   AS k(driverid, ratedefinitionid, workdate)
            JOIN   payroll.driverrateschedules s
                   ON  s.companyid        = :cid
                   AND s.driverid         = k.driverid
                   AND s.ratedefinitionid = k.ratedefinitionid
                   AND s.status IN ('Approved', 'Superseded')
                   AND s.effectivefrom   <= k.workdate
                   AND (s.effectiveto IS NULL OR s.effectiveto >= k.workdate)
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
                "More than one authoritative schedule applies on the requested date.")
        selected[key] = dict(row)

    schedule_ids = sorted({row["driverratescheduleid"] for row in selected.values()})
    components_by_schedule: dict[int, list[ResolvedComponent]] = {}
    if schedule_ids:
        component_rows = (await db.execute(
            text("""
                SELECT s.driverratescheduleid AS schedule_id,
                       c.ratecomponentdefinitionid AS rate_component_definition_id,
                       c.shape AS shape, c.sequenceno AS sequence_no,
                       c.ordinalfrom AS ordinal_from, c.ordinalto AS ordinal_to,
                       v.amount AS amount
                FROM   payroll.driverrateschedules s
                JOIN   payroll.ratecomponentdefinitions c
                       ON c.ratedefinitionid = s.ratedefinitionid
                LEFT JOIN payroll.driverrateschedulevalues v
                       ON v.ratecomponentdefinitionid = c.ratecomponentdefinitionid
                      AND v.driverratescheduleid = s.driverratescheduleid
                WHERE  s.driverratescheduleid = ANY(:sids)
                ORDER  BY s.driverratescheduleid, c.sequenceno
            """),
            {"sids": schedule_ids},
        )).mappings().all()
        for row in component_rows:
            components_by_schedule.setdefault(row["schedule_id"], []).append(
                ResolvedComponent.model_validate(
                    {k: v for k, v in dict(row).items() if k != "schedule_id"}))

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
            pay_definition_id=definition["paydefinitionid"],
            rate_definition_id=key.rate_definition_id,
            rate_shape=definition["shape"],
            calculation_method=definition["calculationmethod"],
            driver_rate_schedule_id=row["driverratescheduleid"],
            schedule_status=row["status"],
            effective_from=row["effectivefrom"],
            effective_to=row["effectiveto"],
            components=list(components_by_schedule.get(row["driverratescheduleid"], [])),
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
