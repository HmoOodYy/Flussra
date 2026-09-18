"""
Source-line read model — shared row-level projection over
payroll.payrolldraftlines.

Extracted from app.payroll.service (Stage B4-11C) as a dependency-closed leaf
module — no behavior change, pure relocation.

Draft CRUD and Period Pay both store their source rows in
payroll.payrolldraftlines, with WorkDate NULL distinguishing Period Pay rows
from daily source rows. These three symbols are the common row-level
read/projection contract over that table: _LINE_SELECT is the canonical
SELECT/projection SQL, _line_row_to_summary maps a row to a DraftLineSummary,
and _get_line_by_id is the shared lookup by draft-line id + company id using
the same projection.

This is a read-model responsibility only — it is neutral between Draft CRUD
and Period Pay, and contains no source-write locking, source-evidence/audit,
mutation-locking, or daily-vs-period-pay policy. Daily-vs-period semantics
(WorkDate filtering, allowed behaviors, validation) remain the responsibility
of each caller's own WHERE clauses and validation, not this module.

Genuinely shared by seven call sites that all still live in
app.payroll.service — Draft-line CRUD (get_period_lines, add_draft_line,
update_draft_line, void_draft_line) and Period Pay (get_period_pay_lines,
add_period_pay_line, update_period_pay_line, void_period_pay_line) — none of
which is more entitled to own it than the others.
"""
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.schemas import DraftLineSummary

_LINE_SELECT = """
    SELECT
        dl.draftlineid,
        dl.payrollperiodid,
        dl.branchid,
        dl.driverid,
        e.fullname         AS drivername,
        dl.workdate,
        dl.linetype,
        dl.linescope,
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
        line_scope=r["linescope"],
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
