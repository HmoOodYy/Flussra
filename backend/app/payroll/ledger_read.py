"""
Ledger read — final (locked) lines for a payroll period.

Extracted from app.payroll.service (Stage B4-9.5) as a dependency-closed leaf
module — no behavior change, pure relocation.

This is the row-level Ledger contract: it reads the authoritative
payroll.payrollfinallines table directly, gated on period status
(Locked/Archived only) and payroll.view / payroll.entry / payroll.finalize
permission. It is deliberately NOT the same responsibility as
app.payroll.finalized_library_read_model, which owns the P6 finalized-library
evidence/report sections (ledger.view permission, snapshot-provenance
envelopes, section availability state). Both modules independently read
payroll.payrollfinallines for their own distinct projections; that is not
evidence they share ownership.
"""
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_any_permission, _require_not_driver_role
from app.payroll.period_read import get_period_by_id
from app.payroll.schemas import FinalLineSummary

_FINAL_SELECT = """
    SELECT
        fl.finallineid,
        fl.payrollperiodid,
        fl.branchid,
        fl.driverid,
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
        fl.sourcesnapshot
    FROM   payroll.payrollfinallines fl
    JOIN   core.drivers              d  ON d.driverid   = fl.driverid
    JOIN   core.employees            e  ON e.employeeid = d.employeeid
"""


async def get_final_lines(
    period_id: int,
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    driver_id: int | None = None,
) -> list[FinalLineSummary]:
    """Return locked final lines for a period (access-checked via period lookup).

    Ledger is operational/admin only — Driver/ODA users are blocked.
    Period must be Locked or Archived; draft data is never exposed via this path.
    """
    # ── Driver-role hard-block (Ledger is not the Driver Screen) ────────────── #
    await _require_not_driver_role(company_id, user_id, db)

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

    # ── Payroll read permission — view or finalize role required ───────────────── #
    await _check_any_permission(
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

    if driver_id is not None:
        conditions.append("fl.driverid = :driver_id")
        params["driver_id"] = driver_id

    where = " AND ".join(conditions)
    result = await db.execute(
        text(
            f"{_FINAL_SELECT} "
            f"WHERE {where} "
            f"ORDER BY fl.workdate, fl.driverid, fl.linetype"
        ),
        params,
    )
    return [
        FinalLineSummary(
            final_line_id=r["finallineid"],
            period_id=r["payrollperiodid"],
            branch_id=r["branchid"],
            driver_id=r["driverid"],
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
