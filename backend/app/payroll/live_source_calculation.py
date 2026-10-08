"""Live derived money for ordinary target source lines.

Ordinary PayDefinition source rows (PayrollDraftLines with a PayrollPeriodDefinitionID)
store source facts only: a Driver, a WorkDate, a definition and a quantity. This
module derives their money on demand and never reads a stored amount:

    DraftLine -> PayrollPeriodDefinition -> RateDefinition
              -> (Driver, RateDefinition, WorkDate) -> whole DriverRateAssignment
              -> all DriverRateValues -> method calculator

All lines are resolved set-wise through ``resolve_many`` (one query group for the
whole period/date, not one per line) and calculated by the method-owned boundary
in ``definition_calculation``. The WorkDate, not the period, selects the rate.

This module is read-only: no INSERT, UPDATE or DELETE.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.compensation.resolver import ResolutionKey, resolve_many
from app.payroll.definition_calculation import (
    DefinitionCalculation,
    PeriodDefinition,
    calculate_definition_input,
)
from app.payroll.period_definitions import definition_from_row


@dataclass(frozen=True)
class LiveSourceLine:
    draft_line_id: int
    driver_id: int
    work_date: date
    quantity: Decimal
    source_type: str
    notes: str | None
    definition: PeriodDefinition
    calculation: DefinitionCalculation


async def load_live_source_lines(
    period_id: int,
    company_id: int,
    db: AsyncConnection,
    *,
    driver_ids: list[int] | None = None,
    work_date: date | None = None,
) -> list[LiveSourceLine]:
    """Every non-void ordinary target line of the period with its live calculation."""
    filters = ""
    params: dict = {"pid": period_id, "cid": company_id}
    if driver_ids is not None:
        if not driver_ids:
            return []
        filters += " AND dl.driverid = ANY(:driver_ids)"
        params["driver_ids"] = sorted(set(driver_ids))
    if work_date is not None:
        filters += " AND dl.workdate = :work_date"
        params["work_date"] = work_date

    rows = (await db.execute(
        text(f"""
            SELECT dl.draftlineid, dl.driverid, dl.workdate, dl.quantity,
                   dl.sourcetype, dl.notes,
                   ppd.payrollperioddefinitionid, ppd.payrollperiodid, ppd.companyid,
                   ppd.branchid, ppd.paydefinitionid, ppd.ratedefinitionid,
                   ppd.definitioncodesnapshot, ppd.definitionnamesnapshot,
                   ppd.inputtypesnapshot, ppd.unitsnapshot,
                   ppd.calculationmethodsnapshot, ppd.calculationmethodversionsnapshot,
                   ppd.rateshapesnapshot, ppd.isactiveinperiod, ppd.sortorder
            FROM   payroll.payrolldraftlines dl
            JOIN   payroll.payrollperioddefinitions ppd
                   ON  ppd.payrollperioddefinitionid = dl.payrollperioddefinitionid
                   AND ppd.payrollperiodid = dl.payrollperiodid
            WHERE  dl.payrollperiodid = :pid
              AND  dl.companyid       = :cid
              AND  dl.status         != 'Void'
              {filters}
            ORDER  BY dl.driverid, dl.workdate, ppd.sortorder, dl.draftlineid
        """),
        params,
    )).mappings().all()
    if not rows:
        return []

    keys = [ResolutionKey(r["driverid"], r["ratedefinitionid"], r["workdate"]) for r in rows]
    resolved = await resolve_many(company_id, keys, db)

    lines: list[LiveSourceLine] = []
    for row, key in zip(rows, keys, strict=True):
        definition = definition_from_row(row)
        quantity = Decimal(str(row["quantity"]))
        lines.append(LiveSourceLine(
            draft_line_id=int(row["draftlineid"]),
            driver_id=int(row["driverid"]),
            work_date=row["workdate"],
            quantity=quantity,
            source_type=row["sourcetype"],
            notes=row["notes"],
            definition=definition,
            calculation=calculate_definition_input(definition, resolved[key], quantity),
        ))
    return lines
