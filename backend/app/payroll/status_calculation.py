"""Direct Status compensation, derived live from canonical Status selections.

A Status selection (``PayrollPeriodDriverDayEntryState.StatusKeyID`` of a DriverAssignment on a
WorkDate) is paid ``HoursValue x`` the permanent Driver's rate for the Status's rate column:

    selection -> Status key -> Status rate column -> its scalar RateDefinition
              -> (Driver, RateDefinition, WorkDate) -> whole DriverRateSchedule
              -> the one scalar value -> the per-unit kernel

The Driver is the permanent Driver of the selection's DriverAssignment, so the schedule is the
same on both sides of a transfer and nothing is copied. Every selection of the period is
resolved set-wise through ``compensation.resolver.resolve_many`` (never one query per row) and
calculated by the same scalar kernel as an ordinary PerUnit line, so Status pay is rounded and
reconciled exactly like any other earning.

* A Status without a rate column is non-monetary: it is omitted here and is never a blocker.
* A monetary Status with no applicable schedule is MISSING_RATE and one whose schedule lacks its
  value is INCOMPLETE_RATE; both need manager review (a blocker), as for ordinary earnings.
* An explicit rate of zero is a complete value: the line is calculated and contributes 0.

This module is read-only: no INSERT, UPDATE or DELETE, and nothing is projected into
``PayrollDraftLines``.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.compensation.resolver import ResolutionKey, resolve_many
from app.payroll.definition_calculation import DefinitionCalculation, calculate_scalar_rate


@dataclass(frozen=True)
class LiveStatusLine:
    """One monetary Status selection with its live calculation."""

    driver_assignment_id: int
    driver_id: int
    entry_state_id: int
    work_date: date
    status_key_id: int
    status_code: str
    status_label: str
    status_rate_column_id: int
    status_rate_column_name: str
    rate_definition_id: int
    hours: Decimal
    calculation: DefinitionCalculation


async def load_live_status_lines(
    period_id: int,
    company_id: int,
    branch_id: int,
    db: AsyncConnection,
) -> list[LiveStatusLine]:
    """Every monetary Status selection of the period (non-void, with a Status bound to a rate
    column), ordered by DriverAssignment and WorkDate, with its live calculation."""
    rows = (await db.execute(
        text("""
            SELECT es.driverassignmentid, a.driverid,
                   es.payrollperioddriverdayentrystateid AS entrystateid, es.workdate,
                   sk.statuskeyid, sk.statuscode, sk.keyname, sk.hoursvalue,
                   c.statusratecolumnid, c.columnname, rd.ratedefinitionid
            FROM   payroll.payrollperioddriverdayentrystate es
            JOIN   payroll.payrollstatuskeys sk
                   ON sk.statuskeyid = es.statuskeyid AND sk.companyid = es.companyid
            JOIN   core.driverassignments a
                   ON a.driverassignmentid = es.driverassignmentid AND a.companyid = es.companyid
            JOIN   payroll.statusratecolumns c
                   ON c.statusratecolumnid = sk.statusratecolumnid AND c.companyid = sk.companyid
            JOIN   payroll.ratedefinitions rd
                   ON rd.statusratecolumnid = c.statusratecolumnid AND rd.companyid = c.companyid
            WHERE  es.payrollperiodid = :pid
              AND  es.companyid       = :cid
              AND  es.branchid        = :bid
              AND  es.isvoided        = FALSE
              AND  es.statuskeyid IS NOT NULL
            ORDER  BY es.driverassignmentid, es.workdate
        """),
        {"pid": period_id, "cid": company_id, "bid": branch_id},
    )).mappings().all()
    if not rows:
        return []

    keys = [ResolutionKey(r["driverid"], r["ratedefinitionid"], r["workdate"]) for r in rows]
    resolved = await resolve_many(company_id, keys, db)

    lines: list[LiveStatusLine] = []
    for row, key in zip(rows, keys, strict=True):
        hours = Decimal(str(row["hoursvalue"]))
        lines.append(LiveStatusLine(
            driver_assignment_id=int(row["driverassignmentid"]),
            driver_id=int(row["driverid"]),
            entry_state_id=int(row["entrystateid"]),
            work_date=row["workdate"],
            status_key_id=int(row["statuskeyid"]),
            status_code=row["statuscode"],
            status_label=row["keyname"],
            status_rate_column_id=int(row["statusratecolumnid"]),
            status_rate_column_name=row["columnname"],
            rate_definition_id=int(row["ratedefinitionid"]),
            hours=hours,
            calculation=calculate_scalar_rate(resolved[key], hours),
        ))
    return lines
