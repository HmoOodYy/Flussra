"""
Source-line read model — shared row-level projection over
payroll.payrolldraftlines.

payroll.payrolldraftlines holds day-bound operational source rows only (WorkDate is
required). Ordinary PayDefinition rows are identified by their PayrollPeriodDefinitionID
and store no money: when a read needs amounts it derives them live, set-wise, through
app.payroll.live_source_calculation. Stored amounts of the temporary Status
compatibility rows are returned as stored.

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

from app.payroll.live_source_calculation import load_live_source_lines
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import DraftLineSummary, DriverPeriodSummary

_LINE_SELECT = """
    SELECT
        dl.draftlineid,
        dl.payrollperiodid,
        dl.branchid,
        dl.driverid,
        e.fullname         AS drivername,
        dl.workdate,
        dl.payrollperioddefinitionid,
        ppd.paydefinitionid,
        ppd.definitioncodesnapshot,
        ppd.definitionnamesnapshot,
        ppd.inputtypesnapshot,
        ppd.unitsnapshot,
        ppd.calculationmethodsnapshot,
        dl.linetype,
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
    JOIN   core.drivers              d  ON d.driverid   = dl.driverid
    JOIN   core.employees            e  ON e.employeeid = d.employeeid
    LEFT JOIN payroll.payrollperioddefinitions ppd
           ON ppd.payrollperioddefinitionid = dl.payrollperioddefinitionid
"""


def _line_row_to_summary(r: Any) -> DraftLineSummary:
    return DraftLineSummary(
        draft_line_id=r["draftlineid"],
        period_id=r["payrollperiodid"],
        branch_id=r["branchid"],
        driver_id=r["driverid"],
        driver_name=r["drivername"],
        work_date=r["workdate"],
        payroll_period_definition_id=r["payrollperioddefinitionid"],
        pay_definition_id=r["paydefinitionid"],
        definition_code=r["definitioncodesnapshot"],
        definition_name=r["definitionnamesnapshot"],
        input_type=r["inputtypesnapshot"],
        unit=r["unitsnapshot"],
        calculation_method=r["calculationmethodsnapshot"],
        line_type=r["linetype"],
        quantity=r["quantity"],
        rate_amount=r["rateamount"],
        calculated_amount=r["calculatedamount"],
        source_type=r["sourcetype"],
        status=r["status"],
        needs_manager_review=r["needsmanagerreview"],
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
        text(f"{_LINE_SELECT} WHERE dl.draftlineid = :lid AND dl.companyid = :cid"),
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
    driver_id: int | None = None,
    work_date: date | None = None,
    line_status: str | None = None,
) -> list[DraftLineSummary]:
    """Return draft lines for a period (access-checked via the period lookup)."""
    period = await get_period_by_id(company_id, user_id, period_id, db)

    conditions = [
        "dl.payrollperiodid  = :period_id",
        "dl.companyid        = :company_id",
    ]
    params: dict[str, Any] = {
        "period_id": period_id,
        "company_id": company_id,
    }

    # CP-2F: for Draft periods, exclude System-sourced lines and STATUS_PAYMENT —
    # these are financial and must not be visible until the period is promoted to Open.
    if period.status == "Draft":
        conditions.append("dl.sourcetype != 'System'")
        conditions.append("dl.linetype != 'STATUS_PAYMENT'")

    if driver_id is not None:
        conditions.append("dl.driverid = :driver_id")
        params["driver_id"] = driver_id

    if work_date is not None:
        conditions.append("dl.workdate = :work_date")
        params["work_date"] = work_date

    if line_status is not None:
        conditions.append("dl.status = :line_status")
        params["line_status"] = line_status

    where = " AND ".join(conditions)
    result = await db.execute(
        text(f"{_LINE_SELECT} WHERE {where} "
             "ORDER BY dl.workdate, dl.driverid, ppd.sortorder NULLS LAST, dl.linetype, "
             "dl.draftlineid"),
        params,
    )
    rows = [_line_row_to_summary(r) for r in result.mappings().all()]

    # Ordinary rows store no money: derive it live (never from a stored column).
    if period.status != "Draft" and any(r.payroll_period_definition_id for r in rows):
        live = {
            line.draft_line_id: line
            for line in await load_live_source_lines(
                period_id, company_id, db,
                driver_ids=[driver_id] if driver_id is not None else None,
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

    # CP-2F: sanitize money fields for Draft so callers never see stale rates/amounts.
    if period.status == "Draft":
        sanitized = []
        for ln in rows:
            ln = ln.model_copy(update={
                "rate_amount": None,
                "calculated_amount": None,
                "needs_manager_review": False,
            })
            sanitized.append(ln)
        return sanitized

    return rows


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
    DriverRateAssignment, never summed from a stored column.
    """
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # Draft periods have no financial summary — block to avoid returning zero totals
    # that could mislead callers into thinking the period is empty.
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Lines summary is not available for Prepared (Draft) periods.",
        )

    names = {
        int(r["driverid"]): r["fullname"]
        for r in (await db.execute(
            text("""
                SELECT d.driverid, e.fullname
                FROM   core.drivers d JOIN core.employees e ON e.employeeid = d.employeeid
                WHERE  d.companyid = :cid
                  AND  d.driverid IN (SELECT driverid FROM payroll.payrolldraftlines
                                      WHERE payrollperiodid = :pid AND companyid = :cid)
            """),
            {"cid": company_id, "pid": period_id},
        )).mappings().all()
    }

    totals: dict[tuple[int, int], dict] = {}
    for line in await load_live_source_lines(period_id, company_id, db):
        entry = totals.setdefault(
            (line.driver_id, line.definition.payroll_period_definition_id),
            {"line": line, "quantity": Decimal("0"), "amount": Decimal("0"),
             "count": 0, "attention": 0},
        )
        entry["quantity"] += line.quantity
        entry["count"] += 1
        if line.calculation.amount is not None:
            entry["amount"] += line.calculation.amount
        if line.calculation.needs_attention:
            entry["attention"] += 1

    summaries = [
        DriverPeriodSummary(
            driver_id=driver_id,
            driver_name=names.get(driver_id, ""),
            period_id=period_id,
            period_name=period.period_name,
            payroll_period_definition_id=definition_id,
            definition_code=entry["line"].definition.code,
            definition_name=entry["line"].definition.name,
            total_quantity=entry["quantity"],
            total_calculated_amount=entry["amount"],
            line_count=entry["count"],
            lines_needing_attention=entry["attention"],
        )
        for (driver_id, definition_id), entry in totals.items()
    ]
    summaries.sort(key=lambda r: (r.driver_name, r.definition_name or "", r.driver_id))
    return summaries
