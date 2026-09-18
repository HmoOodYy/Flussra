"""
Period read model — list periods and fetch a single period by ID.

Extracted from app.payroll.service (Stage B4-7) as a dependency-closed leaf
module — no behavior change, pure relocation.

_BASE_SELECT and _row_to_summary are private implementation details of this
module: both have exactly two consumers, get_periods and get_period_by_id,
and neither is referenced anywhere else in the backend or tests.

get_period_by_id is a genuine cross-domain dependency gate: ~31 call sites
remain in app.payroll.service (Period Create, Lifecycle, Resubmission,
Draft-line CRUD, Finalization, Calculation, Period Pay Lines, Bonus,
Drivers Off, Day Grid) and app.payroll.off_drivers accesses it via qualified
module attribute (service.get_period_by_id). app.payroll.service re-exports
it via facade for its own internal callers; app.payroll.off_drivers is left
unchanged (its qualified access continues to resolve through that facade,
and redirecting it adds no architectural leverage while changing
module-attribute-patch visibility).

get_periods has no remaining caller in app.payroll.service after this move —
router.py is its only consumer and now imports it directly.
"""
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import (
    _build_in_clause,
    _check_any_permission,
    _check_branch_access,
    _has_any_permission,
    _require_not_driver_role,
)
from app.payroll.schemas import PeriodSummary


def _row_to_summary(r: Any) -> PeriodSummary:
    return PeriodSummary(
        payroll_period_id=r["payrollperiodid"],
        branch_id=r["branchid"],
        branch_name=r["branchname"],
        parent_period_id=r["parentpayrollperiodid"],
        period_code=r["periodcode"],
        period_name=r["periodname"],
        period_type=r["periodtype"],
        start_date=r["startdate"],
        end_date=r["enddate"],
        pay_date=r["paydate"],
        status=r["status"],
        notes=r["notes"],
        created_by_user_id=r["createdbyuserid"],
        created_at_utc=r["createdatutc"],
        current_return_review_item_id=r.get("currentreturnreviewitemid"),
        draft_drivers=r["draftdrivers"] or 0,
        draft_lines=r["draftlines"] or 0,
        draft_lines_needing_attention=r["draftlinesneedingattention"] or 0,
        final_lines=r["finallines"] or 0,
        final_gross=Decimal(str(r["finalgross"])) if r["finalgross"] is not None else Decimal("0"),
        final_driver_count=r["finaldrivercount"] or 0,
    )


# Base SELECT that joins the view with the base table for the extra fields
# (paydate, notes, createdbyuserid, createdatutc, currentreturnreviewitemid).
_BASE_SELECT = """
    SELECT
        v.payrollperiodid,
        v.branchid,
        v.branchname,
        v.parentpayrollperiodid,
        v.periodcode,
        v.periodname,
        v.periodtype,
        v.startdate,
        v.enddate,
        v.status,
        v.draftdrivers,
        v.draftlines,
        v.draftlinesneedingattention,
        v.finallines,
        v.finalgross,
        v.finaldrivercount,
        p.paydate,
        p.notes,
        p.createdbyuserid,
        p.createdatutc,
        p.currentreturnreviewitemid
    FROM   app.vw_payrollperiodlist v
    JOIN   payroll.payrollperiods   p ON p.payrollperiodid = v.payrollperiodid
"""


# ---------------------------------------------------------------------------
# List periods
# ---------------------------------------------------------------------------

async def get_periods(
    company_id: int,
    user_id: int,
    db: AsyncConnection,
    *,
    branch_id: int | None = None,
    period_status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[PeriodSummary]:
    # ── Driver-role hard-block (Current Payroll is not a Driver Screen) ─────── #
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    # ── Payroll read permission gate ─────────────────────────────────────────── #
    # Operational users must have payroll.view OR payroll.entry on at least one
    # accessible branch.  Branch access alone (e.g. drivers.view only) is not
    # sufficient to read payroll data.
    #
    # can_see_all only proves branch *access*, not which branches hold payroll
    # permission — a user's AllCompanyBranches assignment can grant an
    # unrelated permission while a separate SpecificBranch assignment grants
    # payroll.view.  sec.fn_UserHasPermission never matches a SpecificBranch
    # row when p_BranchID is NULL (SQL's `x = NULL` is NULL, not TRUE), so
    # every candidate branch must be checked individually.
    _PAYROLL_READ_PERMS = ["payroll.view", "payroll.entry", "payroll.finalize"]
    if can_see_all:
        candidate_rows = (await db.execute(
            text("SELECT branchid FROM core.branches WHERE companyid = :company_id"),
            {"company_id": company_id},
        )).mappings().all()
        candidate_branch_ids = [int(r["branchid"]) for r in candidate_rows]
    else:
        candidate_branch_ids = branch_ids

    permitted_branches = [
        bid for bid in candidate_branch_ids
        if await _has_any_permission(company_id, user_id, bid, _PAYROLL_READ_PERMS, db)
    ]
    if not permitted_branches:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You do not have payroll.view, payroll.entry, or payroll.finalize "
                "permission on any accessible branch."
            ),
        )

    conditions: list[str] = ["v.companyid = :company_id"]
    params: dict[str, Any] = {"company_id": company_id}

    if branch_id is not None:
        if branch_id not in permitted_branches:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to the requested branch.",
            )
        conditions.append("v.branchid = :branch_id")
        params["branch_id"] = branch_id
    else:
        in_clause, in_params = _build_in_clause(permitted_branches, "pb")
        conditions.append(f"v.branchid IN ({in_clause})")
        params.update(in_params)

    if period_status:
        conditions.append("v.status = :period_status")
        params["period_status"] = period_status

    where = " AND ".join(conditions)
    params["limit"] = limit
    params["offset"] = offset

    result = await db.execute(
        text(
            f"{_BASE_SELECT}"
            f"WHERE  {where} "
            f"ORDER  BY v.startdate DESC, v.branchname "
            f"LIMIT  :limit OFFSET :offset"
        ),
        params,
    )
    return [_row_to_summary(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Single period
# ---------------------------------------------------------------------------

async def get_period_by_id(
    company_id: int,
    user_id: int,
    period_id: int,
    db: AsyncConnection,
) -> PeriodSummary:
    # ── Driver-role hard-block — must be first, before any data is read ─────── #
    await _require_not_driver_role(company_id, user_id, db)

    can_see_all, branch_ids = await _check_branch_access(company_id, user_id, db)

    result = await db.execute(
        text(
            f"{_BASE_SELECT}"
            f"WHERE  v.payrollperiodid = :period_id "
            f"  AND  v.companyid       = :company_id"
        ),
        {"period_id": period_id, "company_id": company_id},
    )
    row = result.mappings().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Payroll period not found.",
        )

    if not can_see_all and row["branchid"] not in branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied to this period's branch.",
        )

    # ── Payroll permission gate — any payroll-capable role is sufficient ─────── #
    await _check_any_permission(
        company_id, user_id, row["branchid"],
        ["payroll.view", "payroll.entry", "payroll.finalize"],
        db,
    )

    return _row_to_summary(row)
