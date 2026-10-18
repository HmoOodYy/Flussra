"""
Read locked or archived PayrollFinalLines through the operational payroll permission contract.

This differs from finalized_library_read_model, whose evidence/report sections use ledger.view and snapshot provenance.
"""
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.access.permissions import check_any_permission
from app.access.policy import require_non_driver_subject
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import FinalLineSummary

_FINAL_SELECT = """
    SELECT
        fl.finallineid,
        fl.payrollperiodid,
        fl.branchid,
        fl.driverassignmentid,
        fl.driverid,
        dr.drivernumber,
        e.fullname         AS drivername,
        fl.draftlineid,
        fl.workdate,
        fl.linetype,
        fl.linescope,
        fl.quantity,
        fl.rateamount,
        fl.finalamount,
        fl.sourcetype,
        fl.approvedbyuserid,
        fl.approvedatutc,
        fl.lockedatutc,
        fl.notes,
        fl.payitemid,
        fl.ratetypeid,
        fl.driverrateid,
        fl.resolvedrateamount,
        fl.ratebehavior,
        fl.sourcesnapshot,
        fl.currencycode, fl.currencyminorunitdigits
    FROM   payroll.payrollfinallines fl
    JOIN   core.driverassignments    a  ON a.driverassignmentid = fl.driverassignmentid
    JOIN   core.drivers              dr ON dr.driverid  = fl.driverid
    JOIN   core.employees            e  ON e.employeeid = a.employeeid
"""


async def get_final_lines(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    driver_assignment_id: int | None = None,
) -> list[FinalLineSummary]:
    """Return locked final lines for a period (access-checked via period lookup).

    Ledger is operational/admin only — DRIVER/Self users are blocked.
    Period must be Locked or Archived; draft data is never exposed via this path.
    """
    # ── Driver-role hard-block (Ledger is not the Driver Screen) ────────────── #
    await require_non_driver_subject(company_id, user_id, db)

    period = await get_period_by_id(company_id, user_id, period_id, db)

    # ── Period status guard — final lines only exist on Locked/Archived periods ─ #
    if period.status not in ("Locked", "Archived"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Final lines are only available for Locked or Archived periods "
                f"(current status: '{period.status}'). "
                "Finalize the period first via POST /periods/{id}/finalize."
            ),
        )

    # ── Payroll read permission — view, entry, or finalize required ───────────────── #
    await check_any_permission(
        company_id, user_id, period.branch_id,
        ["payroll.view", "payroll.entry", "payroll.finalize"],
        db,
    )

    conditions = [
        "fl.payrollperiodid = :period_id",
        "fl.companyid       = :company_id",
    ]
    params: dict[str, Any] = {
        "period_id": period_id,
        "company_id": company_id,
    }

    if driver_assignment_id is not None:
        conditions.append("fl.driverassignmentid = :driver_assignment_id")
        params["driver_assignment_id"] = driver_assignment_id

    where = " AND ".join(conditions)
    result = await db.execute(
        text(
            f"{_FINAL_SELECT} "
            f"WHERE {where} "
            f"ORDER BY fl.workdate, fl.driverassignmentid, fl.linetype"
        ),
        params,
    )
    return [
        FinalLineSummary(
            currency_code=r["currencycode"],
            currency_minor_unit_digits=r["currencyminorunitdigits"],
            final_line_id=r["finallineid"],
            period_id=r["payrollperiodid"],
            branch_id=r["branchid"],
            driver_assignment_id=r["driverassignmentid"],
            driver_id=r["driverid"],
            driver_number=r["drivernumber"],
            driver_name=r["drivername"],
            draft_line_id=r["draftlineid"],
            work_date=r["workdate"],
            line_type=r["linetype"],
            line_scope=r["linescope"],
            quantity=r["quantity"],
            rate_amount=r["rateamount"],
            final_amount=r["finalamount"],
            source_type=r["sourcetype"],
            approved_by_user_id=r["approvedbyuserid"],
            approved_at_utc=r["approvedatutc"],
            locked_at_utc=r["lockedatutc"],
            notes=r["notes"],
            pay_item_id=r["payitemid"],
            rate_type_id=r["ratetypeid"],
            driver_rate_id=r["driverrateid"],
            resolved_rate_amount=r["resolvedrateamount"],
            rate_behavior=r["ratebehavior"],
            source_snapshot=r["sourcesnapshot"],
        )
        for r in result.mappings().all()
    ]
