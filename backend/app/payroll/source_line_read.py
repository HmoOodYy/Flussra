"""
Source-line read model — shared row-level projection over
payroll.payrolldraftlines.

Extracted from app.payroll.service (Stage B4-11C) as a dependency-closed leaf
module — no behavior change, pure relocation.

payroll.payrolldraftlines holds day-bound operational source rows only
(WorkDate is required). These three symbols are the common row-level
read/projection contract over that table: _LINE_SELECT is the canonical
SELECT/projection SQL, _line_row_to_summary maps a row to a DraftLineSummary,
and _get_line_by_id is the shared lookup by draft-line id + company id using
the same projection.

This is a read-model responsibility only — it contains no source-write
locking, source-evidence/audit, or mutation-locking policy. Allowed
behaviors and validation remain the responsibility of each caller.

Stage B4-21 moved get_period_lines and get_period_draft_summary here from
app.payroll.service — pure relocation, no behavior change. get_period_lines
is the third consumer of _LINE_SELECT/_line_row_to_summary (now local calls
rather than an imported facade binding). get_period_draft_summary uses its
own direct aggregate SQL, unrelated to _LINE_SELECT, and is not merged with
Day Grid or Ledger read logic.
"""
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

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
"""


def _line_row_to_summary(r: Any) -> DraftLineSummary:
    return DraftLineSummary(
        draft_line_id=r["draftlineid"],
        period_id=r["payrollperiodid"],
        branch_id=r["branchid"],
        driver_id=r["driverid"],
        driver_name=r["drivername"],
        work_date=r["workdate"],
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
        text(f"{_LINE_SELECT} WHERE {where} ORDER BY dl.workdate, dl.driverid, dl.linetype"),
        params,
    )
    rows = [_line_row_to_summary(r) for r in result.mappings().all()]

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
    Aggregated totals per driver × line_type for one period.

    Uses a direct query (not vw_PayrollDraftSummary) so that Void lines
    are explicitly excluded from the aggregation.
    """
    period = await get_period_by_id(company_id, user_id, period_id, db)

    # CP-2F: Draft periods have no financial summary — block to avoid returning
    # zero totals that could mislead callers into thinking the period is empty.
    if period.status == "Draft":
        raise HTTPException(
            status_code=422,
            detail="Lines summary is not available for Prepared (Draft) periods.",
        )

    result = await db.execute(
        text("""
            SELECT
                dl.driverid,
                e.fullname                                                              AS drivername,
                dl.payrollperiodid,
                :period_name                                                            AS periodname,
                dl.linetype,
                SUM(dl.quantity)                                                        AS totalquantity,
                SUM(COALESCE(dl.calculatedamount, 0))                                  AS totalcalculatedamount,
                COUNT(*)                                                                AS linecount,
                SUM(CASE WHEN dl.status IN ('NeedsReview', 'Rejected')
                          OR dl.needsmanagerreview THEN 1 ELSE 0 END)                  AS linesneedingattention
            FROM   payroll.payrolldraftlines dl
            JOIN   core.drivers              d  ON d.driverid   = dl.driverid
            JOIN   core.employees            e  ON e.employeeid = d.employeeid
            WHERE  dl.payrollperiodid = :period_id
              AND  dl.companyid       = :company_id
              AND  dl.status         != 'Void'
            GROUP  BY dl.driverid, e.fullname, dl.payrollperiodid, dl.linetype
            ORDER  BY e.fullname, dl.linetype
        """),
        {
            "period_id":   period_id,
            "company_id":  company_id,
            "period_name": period.period_name,
        },
    )
    return [
        DriverPeriodSummary(
            driver_id=r["driverid"],
            driver_name=r["drivername"],
            period_id=r["payrollperiodid"],
            period_name=r["periodname"],
            line_type=r["linetype"],
            total_quantity=r["totalquantity"],
            total_calculated_amount=r["totalcalculatedamount"],
            line_count=r["linecount"],
            lines_needing_attention=r["linesneedingattention"],
        )
        for r in result.mappings().all()
    ]
