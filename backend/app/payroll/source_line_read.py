"""
Source-line read model — shared row-level projection over
payroll.payrolldraftlines.

payroll.payrolldraftlines holds day-bound operational source rows only (WorkDate is
required). Ordinary PayDefinition rows are identified by their PayrollPeriodDefinitionID
and store no money. What a read shows as money depends on the period financial authority
(app.payroll.financial_authority): Open and Returned periods derive it live, set-wise, through
app.payroll.live_source_calculation; InReview, Approved, Locked and Archived periods take it only
from the exact line of the verified frozen snapshot (a row without one has no money); a frozen
authority that is unavailable withholds it. A Draft period has none.

_LINE_SELECT is the canonical SELECT/projection SQL, _line_row_to_summary maps a row
to a DraftLineSummary, and _get_line_by_id is the shared lookup by draft-line id +
company id. get_period_lines and get_period_draft_summary are the list and aggregate
reads.

This is a read-model responsibility only — it contains no source-write locking,
source-evidence/audit, or mutation-locking policy.
"""
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.financial_authority import (
    FinancialAuthority,
    frozen_ordinary_totals,
    frozen_source_lines,
    resolve_financial_authority,
)
from app.payroll.live_source_calculation import load_live_source_lines
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import DraftLineSummary, DriverPeriodSummary

_LINE_SELECT = """
    SELECT
        dl.draftlineid,
        dl.payrollperiodid,
        dl.branchid,
        dl.driverassignmentid,
        a.driverid,
        dr.drivernumber,
        e.fullname         AS drivername,
        dl.workdate,
        dl.payrollperioddefinitionid,
        ppd.paydefinitionid,
        ppd.definitioncodesnapshot,
        ppd.definitionnamesnapshot,
        ppd.inputtypesnapshot,
        ppd.unitsnapshot,
        ppd.calculationmethodsnapshot,
        dl.quantity,
        dl.rateamount,
        dl.calculatedamount,
        dl.sourcetype,
        dl.status,
        dl.needsmanagerreview,
        dl.notes,
        dl.addedbyuserid,
        dl.addedatutc
    FROM   payroll.payrolldraftlines dl
    JOIN   core.driverassignments    a  ON a.driverassignmentid = dl.driverassignmentid
    JOIN   core.drivers              dr ON dr.driverid  = a.driverid
    JOIN   core.employees            e  ON e.employeeid = a.employeeid
    LEFT JOIN payroll.payrollperioddefinitions ppd
           ON ppd.payrollperioddefinitionid = dl.payrollperioddefinitionid
"""


def _line_row_to_summary(r: Any) -> DraftLineSummary:
    # An ordinary target row (PayrollPeriodDefinitionID set) stores no authoritative money
    # and no review flag: the stored RateAmount/CalculatedAmount/NeedsManagerReview columns
    # are never surfaced for it. Live enrichment supplies the derived values, and no scalar
    # rate field exists for the target contract (a schedule may have several components).
    return DraftLineSummary(
        draft_line_id=r["draftlineid"],
        period_id=r["payrollperiodid"],
        branch_id=r["branchid"],
        driver_assignment_id=r["driverassignmentid"],
        driver_id=r["driverid"],
        driver_number=r["drivernumber"],
        driver_name=r["drivername"],
        work_date=r["workdate"],
        payroll_period_definition_id=r["payrollperioddefinitionid"],
        pay_definition_id=r["paydefinitionid"],
        definition_code=r["definitioncodesnapshot"],
        definition_name=r["definitionnamesnapshot"],
        input_type=r["inputtypesnapshot"],
        unit=r["unitsnapshot"],
        calculation_method=r["calculationmethodsnapshot"],
        quantity=r["quantity"],
        rate_amount=None,
        calculated_amount=None,
        source_type=r["sourcetype"],
        status=r["status"],
        needs_manager_review=False,
        notes=r["notes"],
        added_by_user_id=r["addedbyuserid"],
        added_at_utc=r["addedatutc"],
    )


# ---------------------------------------------------------------------------
# Internal: fetch a single line by its primary key
# ---------------------------------------------------------------------------

async def _get_line_by_id(
    draft_line_id: int,
    company_id: int,
    db: AsyncConnection,
) -> DraftLineSummary:
    result = await db.execute(
        text(f"{_LINE_SELECT} WHERE dl.draftlineid = :lid AND dl.companyid = :cid "
             "AND dl.payrollperioddefinitionid IS NOT NULL"),
        {"lid": draft_line_id, "cid": company_id},
    )
    row = result.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Draft line not found.")
    return _line_row_to_summary(row)


# ---------------------------------------------------------------------------
# List lines (Stage B4-21)
# ---------------------------------------------------------------------------

async def get_period_lines(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    driver_assignment_id: int | None = None,
    work_date: date | None = None,
    line_status: str | None = None,
) -> list[DraftLineSummary]:
    """Return draft lines for a period (access-checked via the period lookup)."""
    period = await get_period_by_id(company_id, user_id, period_id, db)

    conditions = [
        "dl.payrollperiodid  = :period_id",
        "dl.companyid        = :company_id",
        "dl.payrollperioddefinitionid IS NOT NULL",
    ]
    params: dict[str, Any] = {
        "period_id": period_id,
        "company_id": company_id,
    }

    # CP-2F: for Draft periods, exclude System-sourced lines: they are financial and must not
    # be visible until the period is promoted to Open.
    if period.status == "Draft":
        conditions.append("dl.sourcetype != 'System'")

    if driver_assignment_id is not None:
        conditions.append("dl.driverassignmentid = :driver_assignment_id")
        params["driver_assignment_id"] = driver_assignment_id

    if work_date is not None:
        conditions.append("dl.workdate = :work_date")
        params["work_date"] = work_date

    if line_status is not None:
        conditions.append("dl.status = :line_status")
        params["line_status"] = line_status

    where = " AND ".join(conditions)
    result = await db.execute(
        text(f"{_LINE_SELECT} WHERE {where} "
             "ORDER BY dl.workdate, dl.driverassignmentid, ppd.sortorder NULLS LAST, "
             "dl.draftlineid"),
        params,
    )
    rows = [_line_row_to_summary(r) for r in result.mappings().all()]
    authority = await resolve_financial_authority(
        db, period_id=period_id, company_id=company_id, branch_id=period.branch_id, withhold=True)
    authority_fields = {
        "financial_authority": authority.financial_authority,
        "financial_authority_reason": authority.financial_authority_reason,
    }

    if authority.is_live:
        # Ordinary rows store no money: derive it live (never from a stored column).
        if any(r.payroll_period_definition_id for r in rows):
            live = {
                line.draft_line_id: line
                for line in await load_live_source_lines(
                    period_id, company_id, db,
                    driver_assignment_ids=[driver_assignment_id] if driver_assignment_id is not None else None,
                    work_date=work_date,
                )
            }
            enriched = []
            for row in rows:
                line = live.get(row.draft_line_id)
                if line is None:
                    enriched.append(row)
                    continue
                enriched.append(row.model_copy(update={
                    "calculated_amount": line.calculation.amount,
                    "calculation_status": line.calculation.status.value,
                    "needs_manager_review": line.calculation.needs_attention,
                }))
            rows = enriched
        return [row.model_copy(update=authority_fields) for row in rows]

    frozen = frozen_source_lines(authority.content) if authority.is_frozen else {}
    result_rows = []
    for row in rows:
        snapshot_line = frozen.get(row.draft_line_id)
        # No stored or live money is ever shown without an authority: a frozen source shows its
        # exact snapshot line; every other row (Void, unavailable authority, Draft) shows none.
        result_rows.append(row.model_copy(update={
            "rate_amount": None if snapshot_line is None else snapshot_line["ResolvedRateAmount"],
            "calculated_amount": None if snapshot_line is None else snapshot_line["CalculatedAmount"],
            "calculation_status": None if snapshot_line is None else snapshot_line["CalculationStatus"],
            "needs_manager_review": False,
            **authority_fields,
        }))
    return result_rows


# ---------------------------------------------------------------------------
# Summary (aggregated per driver x line_type) (Stage B4-21)
# ---------------------------------------------------------------------------

async def get_period_draft_summary(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
) -> list[DriverPeriodSummary]:
    """
    Live totals per driver x period definition for one period.

    Void lines are excluded. Ordinary amounts are derived live from the effective
    DriverRateSchedule, never summed from a stored column.
    """
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # Draft periods have no financial summary — block to avoid returning zero totals
    # that could mislead callers into thinking the period is empty.
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Lines summary is not available for Prepared (Draft) periods.",
        )

    authority = await resolve_financial_authority(
        db, period_id=period_id, company_id=company_id, branch_id=period.branch_id, withhold=True)
    authority_fields = {
        "financial_authority": authority.financial_authority,
        "financial_authority_reason": authority.financial_authority_reason,
    }
    if authority.is_frozen:
        summaries = _frozen_summaries(authority, period_id, period.period_name, authority_fields)
    elif authority.is_live:
        summaries = await _live_summaries(
            period_id, period.period_name, company_id, db, authority_fields)
    else:
        summaries = await _source_only_summaries(
            period_id, period.period_name, company_id, db, authority_fields)
    summaries.sort(key=lambda r: (r.driver_name, r.definition_name or "", r.driver_assignment_id))
    return summaries


def _frozen_summaries(
    authority: FinancialAuthority, period_id: int, period_name: str, authority_fields: dict[str, Any],
) -> list[DriverPeriodSummary]:
    """Per DriverAssignment x definition from the verified snapshot: the ORDINARY component raw
    (pre-rounding) amount, which is exactly the sum the live summary shows, and the quantity and
    line count of its snapshot lines."""
    content = authority.content
    drivers = {total["DriverAssignmentID"]: total for total in content["DriverTotals"]}
    return [
        DriverPeriodSummary(
            driver_assignment_id=driver_assignment_id,
            driver_id=drivers[driver_assignment_id]["DriverID"],
            driver_number=drivers[driver_assignment_id]["DriverNumberSnapshot"],
            driver_name=drivers[driver_assignment_id]["DriverNameSnapshot"],
            period_id=period_id,
            period_name=period_name,
            payroll_period_definition_id=definition_id,
            definition_code=entry["component"]["DefinitionCodeSnapshot"],
            definition_name=entry["component"]["DefinitionNameSnapshot"],
            total_quantity=entry["quantity"],
            total_calculated_amount=entry["amount"],
            line_count=entry["line_count"],
            lines_needing_attention=0,
            **authority_fields,
        )
        for (driver_assignment_id, definition_id), entry in frozen_ordinary_totals(content).items()
    ]


async def _people(period_id: int, company_id: int, db: AsyncConnection) -> dict[int, Any]:
    return {
        int(r["driverassignmentid"]): r
        for r in (await db.execute(
            text("""
                SELECT a.driverassignmentid, a.driverid, dr.drivernumber, e.fullname
                FROM   core.driverassignments a
                JOIN   core.drivers dr ON dr.driverid = a.driverid
                JOIN   core.employees e ON e.employeeid = a.employeeid
                WHERE  a.companyid = :cid
                  AND  a.driverassignmentid IN (SELECT driverassignmentid FROM payroll.payrolldraftlines
                                      WHERE payrollperiodid = :pid AND companyid = :cid)
            """),
            {"cid": company_id, "pid": period_id},
        )).mappings().all()
    }


async def _live_summaries(
    period_id: int, period_name: str, company_id: int, db: AsyncConnection,
    authority_fields: dict[str, Any],
) -> list[DriverPeriodSummary]:
    people = await _people(period_id, company_id, db)
    totals: dict[tuple[int, int], dict] = {}
    for line in await load_live_source_lines(period_id, company_id, db):
        entry = totals.setdefault(
            (line.driver_assignment_id, line.definition.payroll_period_definition_id),
            {"line": line, "quantity": Decimal("0"), "amount": Decimal("0"),
             "count": 0, "attention": 0},
        )
        entry["quantity"] += line.quantity
        entry["count"] += 1
        if line.calculation.amount is not None:
            entry["amount"] += line.calculation.amount
        if line.calculation.needs_attention:
            entry["attention"] += 1
    return [
        DriverPeriodSummary(
            driver_assignment_id=driver_assignment_id,
            driver_id=people[driver_assignment_id]["driverid"],
            driver_number=people[driver_assignment_id]["drivernumber"],
            driver_name=people[driver_assignment_id]["fullname"],
            period_id=period_id,
            period_name=period_name,
            payroll_period_definition_id=definition_id,
            definition_code=entry["line"].definition.code,
            definition_name=entry["line"].definition.name,
            total_quantity=entry["quantity"],
            total_calculated_amount=entry["amount"],
            line_count=entry["count"],
            lines_needing_attention=entry["attention"],
            **authority_fields,
        )
        for (driver_assignment_id, definition_id), entry in totals.items()
    ]


async def _source_only_summaries(
    period_id: int, period_name: str, company_id: int, db: AsyncConnection,
    authority_fields: dict[str, Any],
) -> list[DriverPeriodSummary]:
    """The entry data (quantity and line count) of a period whose money is withheld."""
    people = await _people(period_id, company_id, db)
    rows = (await db.execute(text("""
        SELECT dl.driverassignmentid, dl.payrollperioddefinitionid,
               ppd.definitioncodesnapshot, ppd.definitionnamesnapshot,
               COALESCE(SUM(dl.quantity), 0) AS quantity, COUNT(*) AS line_count
        FROM payroll.payrolldraftlines dl
        JOIN payroll.payrollperioddefinitions ppd
          ON ppd.payrollperioddefinitionid = dl.payrollperioddefinitionid
        WHERE dl.payrollperiodid = :pid AND dl.companyid = :cid AND dl.status <> 'Void'
        GROUP BY dl.driverassignmentid, dl.payrollperioddefinitionid,
                 ppd.definitioncodesnapshot, ppd.definitionnamesnapshot
    """), {"cid": company_id, "pid": period_id})).mappings().all()
    return [
        DriverPeriodSummary(
            driver_assignment_id=int(row["driverassignmentid"]),
            driver_id=people[int(row["driverassignmentid"])]["driverid"],
            driver_number=people[int(row["driverassignmentid"])]["drivernumber"],
            driver_name=people[int(row["driverassignmentid"])]["fullname"],
            period_id=period_id,
            period_name=period_name,
            payroll_period_definition_id=int(row["payrollperioddefinitionid"]),
            definition_code=row["definitioncodesnapshot"],
            definition_name=row["definitionnamesnapshot"],
            total_quantity=Decimal(str(row["quantity"])),
            total_calculated_amount=None,
            line_count=int(row["line_count"]),
            lines_needing_attention=0,
            **authority_fields,
        )
        for row in rows
    ]
