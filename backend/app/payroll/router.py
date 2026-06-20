"""
Payroll domain router — /payroll/periods and /payroll/periods/{id}/lines.

All endpoints require a valid JWT.
Company and user identity come from the token; branch-scoping is enforced
inside the service layer.
"""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from app.payroll.schemas import (
    PeriodSummary, PeriodCreate, PeriodStatusChange, NextPeriodDates, PeriodEntryCount,
    DraftLineSummary, DraftLineCreate, DraftLineUpdate,
    DriverPeriodSummary, FinalLineSummary,
    RateTypeSummary, DriverRateSummary, DriverRateCreate, DriverRateUpdate,
    RateLookupResult,
    PeriodPayLineCreate, PeriodPayLineUpdate,
    PeriodEligibleDriversResponse,
    DriverPayRuleSummary, DriverPayRuleCreate, DriverPayRuleEnd, DriverPayRuleNotesUpdate,
    DriverRateMatrix,
    BatchRateRequest, BatchRateSaveResult,
    DriverRatesSummary,
    CopyRatesRequest, CopyRatesResult,
    DayGridResponse, DayGridSaveRequest,
    DriversOffResponse,
    FinalizationPreviewResponse,
)
from app.payroll import service
from app.dependencies import get_db, get_current_user

router = APIRouter()

TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep    = Annotated[AsyncConnection, Depends(get_db)]


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

@router.get(
    "/periods",
    response_model=list[PeriodSummary],
    summary="List payroll periods in the user's scope",
)
async def list_periods(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None, description="Filter to a specific branch"),
    period_status: str | None = Query(
        None,
        alias="status",
        description="Draft | Open | InReview | Approved | Locked | Cancelled | Archived",
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[PeriodSummary]:
    return await service.get_periods(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
        period_status=period_status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/periods/next-period-dates",
    response_model=NextPeriodDates,
    summary="Compute suggested start/end dates for the next payroll period",
    description=(
        "Returns start_date and end_date for the next period of *branch_id* "
        "based on its BranchPayrollSettings (frequency + anchor_start_date) "
        "and the latest existing non-cancelled period.\n\n"
        "Returns `is_custom=True` and `start_date=null` for Custom frequency — "
        "caller must ask the user for manual dates in that case.\n\n"
        "**Note:** This endpoint must be registered before "
        "`GET /periods/{period_id}` in the router so FastAPI matches the "
        "literal path before the parameterised one."
    ),
    responses={
        403: {"description": "No access to the requested branch"},
        404: {"description": "Branch has no active payroll setup"},
    },
)
async def get_next_period_dates(
    token: TokenDep,
    db: DbDep,
    branch_id: int = Query(..., description="Branch to compute the next period for"),
) -> NextPeriodDates:
    return await service.get_next_period_dates(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        branch_id=branch_id,
        db=db,
    )


@router.get(
    "/periods/{period_id}/entry-count",
    response_model=PeriodEntryCount,
    summary="Count non-voided draft entries in a period (used to gate cancellation warnings)",
    responses={
        403: {"description": "No access to this period's branch"},
        404: {"description": "Period not found"},
    },
)
async def get_period_entry_count(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> PeriodEntryCount:
    return await service.get_period_entry_count(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        period_id=period_id,
        db=db,
    )


@router.get(
    "/periods/{period_id}",
    response_model=PeriodSummary,
    summary="Get a single payroll period by ID",
    responses={
        403: {"description": "No access to this period's branch"},
        404: {"description": "Period not found"},
    },
)
async def get_period(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> PeriodSummary:
    return await service.get_period_by_id(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        period_id=period_id,
        db=db,
    )


@router.post(
    "/periods",
    response_model=PeriodSummary,
    status_code=201,
    summary="Create a new payroll period (starts in Draft status)",
    responses={
        403: {"description": "No access to the target branch"},
        422: {"description": "Validation error or invalid branch"},
    },
)
async def create_period(
    body: PeriodCreate,
    token: TokenDep,
    db: DbDep,
) -> PeriodSummary:
    return await service.create_period(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.patch(
    "/periods/{period_id}/status",
    response_model=PeriodSummary,
    summary="Transition a period to a new status",
    description=(
        "Allowed transitions:\n"
        "- **Draft** → Open | Cancelled\n"
        "- **Open** → InReview | Cancelled\n"
        "- **InReview** → *(no PATCH exits — use POST /review/items/{id}/decide)*\n"
        "  - Approved decision → period Approved\n"
        "  - Rejected / EditRequested decision → period Returned\n"
        "- **Returned** → *(no PATCH exits — use POST /periods/{id}/resubmissions)*\n"
        "- **Approved** → *(no PATCH exits — use POST /periods/{id}/finalize to lock)*\n"
        "- **Locked** → Archived\n"
    ),
    responses={
        403: {"description": "No access to this period's branch"},
        404: {"description": "Period not found"},
        422: {"description": "Transition not allowed from current status"},
    },
)
async def change_period_status(
    period_id: int,
    body: PeriodStatusChange,
    token: TokenDep,
    db: DbDep,
) -> PeriodSummary:
    return await service.change_period_status(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        period_id=period_id,
        change=body,
        db=db,
    )


@router.post(
    "/periods/{period_id}/resubmissions",
    response_model=PeriodSummary,
    status_code=200,
    summary="Resubmit a Returned period for review",
    description=(
        "Transitions a **Returned** period back to **InReview**.\n\n"
        "Requires `payroll.entry` permission. Driver and ODA roles are blocked.\n\n"
        "Reruns all Open→InReview submission guards (empty-period, NeedsManagerReview, "
        "zero-calc, duplicate-Pending) before creating a new Pending PeriodApproval "
        "review item. Clears `CurrentReturnReviewItemID` atomically.\n\n"
        "The resolved review item from the previous return is preserved unchanged.\n\n"
        "Returns **409** if the period is no longer Returned at the write boundary."
    ),
    responses={
        403: {"description": "No access to this period's branch, or driver/ODA role"},
        404: {"description": "Period not found"},
        409: {"description": "Period is no longer Returned — concurrent transition"},
        422: {"description": "Period not Returned, or submission guard failed"},
    },
)
async def resubmit_period(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> PeriodSummary:
    return await service.resubmit_period(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        period_id=period_id,
        db=db,
    )


# ---------------------------------------------------------------------------
# Draft lines (entry)
# ---------------------------------------------------------------------------

@router.get(
    "/periods/{period_id}/lines",
    response_model=list[DraftLineSummary],
    summary="List draft lines for a period",
    responses={
        403: {"description": "No access to this period's branch"},
        404: {"description": "Period not found"},
    },
)
async def list_period_lines(
    period_id: int,
    token: TokenDep,
    db: DbDep,
    driver_id: int | None = Query(None, description="Filter to a specific driver"),
    work_date: date | None = Query(None, description="Filter to a specific work date (YYYY-MM-DD)"),
    line_status: str | None = Query(
        None,
        alias="status",
        description="Active | NeedsReview | Rejected | Void",
    ),
) -> list[DraftLineSummary]:
    return await service.get_period_lines(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        driver_id=driver_id,
        work_date=work_date,
        line_status=line_status,
    )


@router.get(
    "/periods/{period_id}/lines/summary",
    response_model=list[DriverPeriodSummary],
    summary="Aggregated draft totals per driver × line type for a period",
    responses={
        403: {"description": "No access to this period's branch"},
        404: {"description": "Period not found"},
    },
)
async def get_period_lines_summary(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> list[DriverPeriodSummary]:
    return await service.get_period_draft_summary(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/periods/{period_id}/lines",
    response_model=DraftLineSummary,
    status_code=201,
    summary="Add a draft line to a period",
    responses={
        403: {"description": "No access to this period's branch"},
        404: {"description": "Period not found"},
        422: {"description": "Period not Open/InReview, or driver branch mismatch"},
    },
)
async def add_period_line(
    period_id: int,
    body: DraftLineCreate,
    token: TokenDep,
    db: DbDep,
) -> DraftLineSummary:
    return await service.add_draft_line(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.patch(
    "/periods/{period_id}/lines/{line_id}",
    response_model=DraftLineSummary,
    summary="Partially update a draft line",
    responses={
        403: {"description": "No access to this period's branch"},
        404: {"description": "Period or draft line not found"},
        422: {"description": "Period is frozen or line is already voided"},
    },
)
async def update_period_line(
    period_id: int,
    line_id: int,
    body: DraftLineUpdate,
    token: TokenDep,
    db: DbDep,
) -> DraftLineSummary:
    return await service.update_draft_line(
        period_id=period_id,
        draft_line_id=line_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.delete(
    "/periods/{period_id}/lines/{line_id}",
    status_code=204,
    summary="Void (soft-delete) a draft line",
    responses={
        403: {"description": "No access to this period's branch"},
        404: {"description": "Period or draft line not found"},
        422: {"description": "Period is frozen"},
    },
)
async def void_period_line(
    period_id: int,
    line_id: int,
    token: TokenDep,
    db: DbDep,
) -> Response:
    await service.void_draft_line(
        period_id=period_id,
        draft_line_id=line_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Finalization + Ledger
# ---------------------------------------------------------------------------

@router.post(
    "/periods/{period_id}/finalize",
    response_model=PeriodSummary,
    summary="Finalize an Approved period — creates FinalLines and locks the period",
    description=(
        "The period must be in **Approved** status. "
        "All non-Void draft lines are copied to `PayrollFinalLines` "
        "and the period transitions to **Locked**. "
        "This is the only path that creates final lines — "
        "the PATCH /status endpoint intentionally blocks Approved → Locked."
    ),
    responses={
        403: {"description": "No access to this period's branch"},
        404: {"description": "Period not found"},
        422: {"description": "Period is not in Approved status"},
    },
)
async def finalize_period(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> PeriodSummary:
    return await service.finalize_period(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/periods/{period_id}/finalization-preview",
    response_model=FinalizationPreviewResponse,
    summary="Preview finalization for an Approved period (read-only)",
    description=(
        "Returns a read-only preview of what "
        "POST /periods/{id}/finalize would do. "
        "The period must be in **Approved** status. "
        "Makes **no** DB mutations — no PayrollFinalLines are written and "
        "the period status is not changed."
    ),
    responses={
        403: {"description": "ODA user, or no payroll.finalize permission"},
        404: {"description": "Period not found"},
        422: {"description": "Period is not in Approved status"},
    },
)
async def get_finalization_preview(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> FinalizationPreviewResponse:
    return await service.get_finalization_preview(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/periods/{period_id}/final-lines",
    response_model=list[FinalLineSummary],
    summary="List locked final lines for a period",
    responses={
        403: {"description": "No access to this period's branch"},
        404: {"description": "Period not found"},
    },
)
async def list_final_lines(
    period_id: int,
    token: TokenDep,
    db: DbDep,
    driver_id: int | None = Query(None, description="Filter to a specific driver"),
) -> list[FinalLineSummary]:
    return await service.get_final_lines(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        driver_id=driver_id,
    )


# ---------------------------------------------------------------------------
# Pay Rates — rate type catalog + driver rate matrix
# ---------------------------------------------------------------------------

@router.get(
    "/rate-types",
    response_model=list[RateTypeSummary],
    summary="List all active rate types (reference catalog)",
)
async def list_rate_types(
    token: TokenDep,
    db: DbDep,
    include_inactive: bool = Query(False, description="Include inactive rate types"),
) -> list[RateTypeSummary]:
    return await service.get_rate_types(
        company_id=int(token["cid"]),
        db=db,
        active_only=not include_inactive,
    )


@router.get(
    "/rates",
    response_model=list[DriverRateSummary],
    summary="List driver rates (filterable by branch, driver, or status)",
)
async def list_rates(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None, description="Filter to a specific branch"),
    driver_id: int | None = Query(None, description="Filter to a specific driver"),
    rate_status: str | None = Query(
        None,
        alias="status",
        description="PendingApproval | Approved | Superseded | Voided",
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[DriverRateSummary]:
    return await service.get_rates(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
        driver_id=driver_id,
        rate_status=rate_status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/rates/lookup",
    response_model=RateLookupResult,
    summary="Find the driver rate that applies on a specific work date",
    description=(
        "Returns the single rate that covers *work_date* for the given "
        "driver+rate_type combination.  Both **Approved** and **Superseded** "
        "rows are searched so that historically-closed rates (whose effective "
        "date range ends before the current Approved rate began) are correctly "
        "returned for payroll lines whose work date falls inside that range.\n\n"
        "Set `found=false` when no rate exists for the driver+type on that date."
    ),
)
async def lookup_rate_for_date(
    token: TokenDep,
    db: DbDep,
    driver_id: int = Query(..., description="Driver to look up"),
    rate_type_id: int = Query(..., description="Rate type to look up"),
    work_date: date = Query(..., description="Work date (YYYY-MM-DD)"),
) -> RateLookupResult:
    rate = await service.resolve_rate_for_date(
        driver_id=driver_id,
        rate_type_id=rate_type_id,
        work_date=work_date,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
    return RateLookupResult(
        found=rate is not None,
        work_date=work_date,
        driver_id=driver_id,
        rate_type_id=rate_type_id,
        rate=rate,
    )


@router.get(
    "/rates/{rate_id}",
    response_model=DriverRateSummary,
    summary="Get a single driver rate by ID",
    responses={
        403: {"description": "No access to this rate's branch"},
        404: {"description": "Rate not found"},
    },
)
async def get_rate(
    rate_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverRateSummary:
    return await service.get_rate_by_id(
        rate_id=rate_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/rates",
    response_model=DriverRateSummary,
    status_code=201,
    summary="Create a new driver rate (starts in PendingApproval status)",
    responses={
        403: {"description": "No access to the driver's branch"},
        422: {"description": "Driver or rate type not found, or invalid dates"},
    },
)
async def create_rate(
    body: DriverRateCreate,
    token: TokenDep,
    db: DbDep,
) -> DriverRateSummary:
    return await service.create_rate(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.patch(
    "/rates/{rate_id}",
    response_model=DriverRateSummary,
    summary="Partially update a PendingApproval driver rate",
    responses={
        403: {"description": "No access to this rate's branch"},
        404: {"description": "Rate not found"},
        422: {"description": "Rate is not in PendingApproval status"},
    },
)
async def update_rate(
    rate_id: int,
    body: DriverRateUpdate,
    token: TokenDep,
    db: DbDep,
) -> DriverRateSummary:
    return await service.update_rate(
        rate_id=rate_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.post(
    "/rates/{rate_id}/approve",
    response_model=DriverRateSummary,
    summary="Approve a PendingApproval rate (supersedes any prior Approved rate for the same driver+type)",
    responses={
        403: {"description": "No access to this rate's branch"},
        404: {"description": "Rate not found"},
        422: {"description": "Rate is not in PendingApproval status"},
    },
)
async def approve_rate(
    rate_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverRateSummary:
    return await service.approve_rate(
        rate_id=rate_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.delete(
    "/rates/{rate_id}",
    status_code=204,
    summary="Void a driver rate (PendingApproval or Approved only)",
    responses={
        403: {"description": "No access to this rate's branch"},
        404: {"description": "Rate not found"},
        422: {"description": "Rate cannot be voided from its current status"},
    },
)
async def void_rate(
    rate_id: int,
    token: TokenDep,
    db: DbDep,
) -> Response:
    await service.void_rate(
        rate_id=rate_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Driver Rate Matrix — /payroll/drivers/{driver_id}/rate-matrix
# ---------------------------------------------------------------------------

@router.get(
    "/drivers/{driver_id}/rate-matrix",
    response_model=DriverRateMatrix,
    summary="Rate matrix — all required rates for a driver as-of a date",
    responses={
        403: {"description": "No access to this driver's branch"},
        404: {"description": "Driver not found"},
    },
)
async def get_driver_rate_matrix(
    driver_id: int,
    token: TokenDep,
    db: DbDep,
    as_of: date = Query(default=None, description="As-of date (YYYY-MM-DD); defaults to today"),
) -> DriverRateMatrix:
    from datetime import date as _date
    effective_date = as_of if as_of is not None else _date.today()
    return await service.get_driver_rate_matrix(
        driver_id=driver_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        as_of_date=effective_date,
        db=db,
    )


# ---------------------------------------------------------------------------
# Batch rate save — POST /payroll/drivers/{driver_id}/rates/batch
# ---------------------------------------------------------------------------

@router.post(
    "/drivers/{driver_id}/rates/batch",
    response_model=BatchRateSaveResult,
    status_code=200,
    summary="Atomically save multiple rates for a driver (Phase 2A)",
    description=(
        "Create or update multiple rates for a driver in a single atomic operation.\n\n"
        "All changes share the same `effective_from`.\n\n"
        "**Approval behaviour:**\n"
        "- If the company has *AllowSelfApproval* enabled, all rates are auto-approved "
        "inside the same transaction.\n"
        "- If *AllowSelfApproval* is disabled, rates are left in *PendingApproval* "
        "and must be approved via `POST /payroll/rates/{id}/approve`.\n\n"
        "**Tiered / Block rates** (OrdinalTier, RangeBracket, RangeProgressive, Block) "
        "are not supported by this endpoint — use the individual rate endpoints for those.\n\n"
        "**Finalized-period guard:** when auto-approving, the endpoint rejects any "
        "`effective_from` that falls inside a *Locked* or *Archived* payroll period "
        "for the driver's branch."
    ),
    responses={
        403: {"description": "No access to the driver's branch or payrates.edit permission missing"},
        404: {"description": "Driver not found"},
        422: {
            "description": (
                "Empty changes / duplicate rate_type_id / inactive rate type / "
                "tiered or block rate / effective_from inside a finalized period"
            )
        },
    },
)
async def batch_save_driver_rates(
    driver_id: int,
    body: BatchRateRequest,
    token: TokenDep,
    db: DbDep,
) -> BatchRateSaveResult:
    return await service.batch_save_rates(
        driver_id=driver_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


# ---------------------------------------------------------------------------
# Phase 2B — driver rate summary / pending / history
# ---------------------------------------------------------------------------

@router.get(
    "/drivers/{driver_id}/rates/summary",
    response_model=DriverRatesSummary,
    summary="Quick status counts for a driver's rates (pending, future, missing)",
    description=(
        "Returns badge counts for the Pay Rates UI header:\n\n"
        "- **pending_count**: PendingApproval rates\n"
        "- **future_approved_count**: Approved rates whose `effective_from` is in the future\n"
        "- **missing_required_count**: required matrix items with no current Approved rate\n\n"
        "All counts use today's date as the reference point."
    ),
    responses={
        403: {"description": "No access to this driver's branch or ODA mismatch"},
        404: {"description": "Driver not found"},
    },
)
async def get_driver_rates_summary(
    driver_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverRatesSummary:
    return await service.get_driver_rates_summary(
        driver_id=driver_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/drivers/{driver_id}/rates/pending",
    response_model=list[DriverRateSummary],
    summary="List PendingApproval rates for a driver",
    description=(
        "Returns all rates in **PendingApproval** status for the specified driver, "
        "ordered newest first.  These rates have not yet been approved and do not "
        "affect payroll calculations.\n\n"
        "Use `POST /payroll/rates/{id}/approve` to approve and "
        "`DELETE /payroll/rates/{id}` to void a pending rate."
    ),
    responses={
        403: {"description": "No access to this driver's branch or ODA mismatch"},
        404: {"description": "Driver not found"},
    },
)
async def list_driver_rates_pending(
    driver_id: int,
    token: TokenDep,
    db: DbDep,
) -> list[DriverRateSummary]:
    return await service.get_driver_rates_pending(
        driver_id=driver_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/drivers/{driver_id}/rates/history",
    response_model=list[DriverRateSummary],
    summary="Full rate history for a driver (all statuses)",
    description=(
        "Returns all driver rate records regardless of status: "
        "**Approved**, **PendingApproval**, **Superseded**, and **Voided**.\n\n"
        "Sorted by `effective_from DESC`, `created_at_utc DESC`.  "
        "Defaults to 200 rows; paginate with `limit` and `offset`."
    ),
    responses={
        403: {"description": "No access to this driver's branch or ODA mismatch"},
        404: {"description": "Driver not found"},
    },
)
async def list_driver_rates_history(
    driver_id: int,
    token: TokenDep,
    db: DbDep,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[DriverRateSummary]:
    return await service.get_driver_rates_history(
        driver_id=driver_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Phase 2C — D: Bulk driver rates summary
# IMPORTANT: This literal route must be registered BEFORE the
# /drivers/{driver_id}/... parameterised routes.
# FastAPI differentiates by type annotation (driver_id: int) but registering
# the literal route first is the safest practice.
# ---------------------------------------------------------------------------

@router.get(
    "/drivers/rates-summary",
    response_model=list[DriverRatesSummary],
    summary="Bulk status counts for all accessible drivers (left-panel badges)",
    description=(
        "Returns pending/future-approved counts for every driver the caller can access. "
        "ODA users receive only their own driver. SpecificBranch users see only their branch. "
        "missing_required_count is always null in bulk responses (use single-driver summary "
        "endpoint for the full count)."
    ),
    responses={
        403: {"description": "No payrates.view permission"},
    },
)
async def get_bulk_driver_rates_summary(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None, description="Optional branch filter for AllCompanyBranches users"),
) -> list[DriverRatesSummary]:
    return await service.get_bulk_driver_rates_summary(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
    )


# ---------------------------------------------------------------------------
# Phase 2C — C: Copy Rates From Driver
# ---------------------------------------------------------------------------

@router.post(
    "/drivers/{target_driver_id}/rates/copy-from/{source_driver_id}",
    response_model=CopyRatesResult,
    status_code=200,
    summary="Copy Approved rates from one driver to another",
    description=(
        "Copies current Approved rates from source_driver (as-of effective_from) to "
        "target_driver.  Requires payrates.edit on both driver branches.  "
        "ODA users cannot use this endpoint.\n\n"
        "Rates become PendingApproval unless AllowSelfApproval=True, in which case "
        "they auto-approve and supersede existing rates.\n\n"
        "Only rates valid for the target driver's branch are copied; "
        "rate types not configured for the target branch are silently skipped.\n\n"
        "Set include_pay_rules=true to also copy Active MinimumPay/MaximumPay rules."
    ),
    responses={
        403: {"description": "Insufficient permission, ODA user, or cross-company attempt"},
        404: {"description": "Source or target driver not found"},
        422: {"description": "Backdating guard, overlapping pay rule, or validation failure"},
    },
)
async def copy_driver_rates(
    target_driver_id: int,
    source_driver_id: int,
    body: CopyRatesRequest,
    token: TokenDep,
    db: DbDep,
) -> CopyRatesResult:
    return await service.copy_driver_rates(
        target_driver_id=target_driver_id,
        source_driver_id=source_driver_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


# ---------------------------------------------------------------------------
# M14 — Period Pay  (/payroll/periods/{id}/period-pay)
# ---------------------------------------------------------------------------

@router.post(
    "/periods/{period_id}/period-pay",
    response_model=DraftLineSummary,
    status_code=201,
    summary="Add a period-level pay line (Bonus, Adjustment, custom Period item)",
    responses={
        422: {"description": "Invalid item type, inactive item, or period not Open/InReview"},
    },
)
async def add_period_pay_line(
    period_id: int,
    data: PeriodPayLineCreate,
    token: TokenDep,
    db: DbDep,
) -> DraftLineSummary:
    return await service.add_period_pay_line(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=data,
        db=db,
    )


@router.get(
    "/periods/{period_id}/period-pay",
    response_model=list[DraftLineSummary],
    summary="List period-level pay lines for a period",
)
async def list_period_pay_lines(
    period_id: int,
    token: TokenDep,
    db: DbDep,
    driver_id: int | None = Query(None, description="Filter to a specific driver"),
) -> list[DraftLineSummary]:
    return await service.get_period_pay_lines(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        driver_id=driver_id,
    )


@router.patch(
    "/periods/{period_id}/period-pay/{line_id}",
    response_model=DraftLineSummary,
    summary="Update a period-level pay line (amount and/or notes)",
    responses={
        404: {"description": "Line not found in this period"},
        422: {"description": "Period not Open/InReview, or line is Voided"},
    },
)
async def update_period_pay_line(
    period_id: int,
    line_id: int,
    data: PeriodPayLineUpdate,
    token: TokenDep,
    db: DbDep,
) -> DraftLineSummary:
    return await service.update_period_pay_line(
        period_id=period_id,
        line_id=line_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=data,
        db=db,
    )


@router.delete(
    "/periods/{period_id}/period-pay/{line_id}",
    response_model=DraftLineSummary,
    summary="Void a period-level pay line",
    responses={
        404: {"description": "Line not found in this period"},
        422: {"description": "Period not Open/InReview"},
    },
)
async def void_period_pay_line(
    period_id: int,
    line_id: int,
    token: TokenDep,
    db: DbDep,
) -> DraftLineSummary:
    return await service.void_period_pay_line(
        period_id=period_id,
        line_id=line_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# CP-2 P1 #1 — Period-eligible drivers for Bonus dropdown
# ---------------------------------------------------------------------------

@router.get(
    "/periods/{period_id}/eligible-drivers",
    response_model=PeriodEligibleDriversResponse,
    summary="List drivers eligible for a period-pay Bonus (period-scoped, not day-scoped)",
    description=(
        "Returns drivers who were active in the period's branch during the period dates, "
        "plus any driver who already has period-pay lines in this period. "
        "This is the stable source-of-truth for the Bonus driver dropdown — "
        "it does not change when the user navigates to a different day in the grid. "
        "ODA/Driver users receive 403."
    ),
    responses={
        403: {"description": "ODA/Driver user or no payroll.view/payroll.entry permission"},
        404: {"description": "Period not found"},
    },
)
async def get_period_eligible_drivers(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> PeriodEligibleDriversResponse:
    drivers = await service.get_period_eligible_drivers(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
    from app.payroll.schemas import PeriodEligibleDriver
    return PeriodEligibleDriversResponse(
        drivers=[PeriodEligibleDriver(**d) for d in drivers]
    )


# ---------------------------------------------------------------------------
# M15 — Driver Pay Rules  (/payroll/driver-pay-rules, /payroll/drivers/{id}/pay-rules)
# ---------------------------------------------------------------------------

@router.get(
    "/drivers/{driver_id}/pay-rules",
    response_model=list[DriverPayRuleSummary],
    summary="List pay rules (min/max) for a driver",
    responses={
        403: {"description": "No access to this driver's branch"},
        404: {"description": "Driver not found"},
    },
)
async def list_driver_pay_rules(
    driver_id: int,
    token: TokenDep,
    db: DbDep,
    rule_type: str | None = Query(None, description="MinimumPay | MaximumPay"),
    rule_status: str | None = Query(None, alias="status", description="Active | Ended | Voided"),
) -> list[DriverPayRuleSummary]:
    return await service.get_driver_pay_rules(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        driver_id=driver_id,
        db=db,
        rule_type=rule_type,
        rule_status=rule_status,
    )


@router.post(
    "/driver-pay-rules",
    response_model=DriverPayRuleSummary,
    status_code=201,
    summary="Create a new driver pay rule (MinimumPay or MaximumPay)",
    responses={
        403: {"description": "No setup.manage permission"},
        404: {"description": "Driver not found"},
        422: {"description": "Overlapping rule or invalid amount"},
    },
)
async def create_driver_pay_rule(
    body: DriverPayRuleCreate,
    token: TokenDep,
    db: DbDep,
) -> DriverPayRuleSummary:
    return await service.create_driver_pay_rule(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@router.get(
    "/driver-pay-rules/{rule_id}",
    response_model=DriverPayRuleSummary,
    summary="Get a single driver pay rule by ID",
    responses={
        403: {"description": "No access to this rule's branch"},
        404: {"description": "Rule not found"},
    },
)
async def get_driver_pay_rule(
    rule_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverPayRuleSummary:
    return await service.get_driver_pay_rule_by_id(
        rule_id=rule_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/driver-pay-rules/{rule_id}/end",
    response_model=DriverPayRuleSummary,
    summary="End (close) an Active driver pay rule",
    responses={
        403: {"description": "No setup.manage permission"},
        404: {"description": "Rule not found"},
        422: {"description": "Rule not Active, or effective_to before finalized period"},
    },
)
async def end_driver_pay_rule(
    rule_id: int,
    body: DriverPayRuleEnd,
    token: TokenDep,
    db: DbDep,
) -> DriverPayRuleSummary:
    return await service.end_driver_pay_rule(
        rule_id=rule_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        effective_to=body.effective_to,
        db=db,
    )


@router.patch(
    "/driver-pay-rules/{rule_id}",
    response_model=DriverPayRuleSummary,
    summary="Update notes on a driver pay rule (notes-only)",
    responses={
        403: {"description": "No setup.manage permission"},
        404: {"description": "Rule not found"},
        422: {"description": "Rule is Voided"},
    },
)
async def update_driver_pay_rule(
    rule_id: int,
    body: DriverPayRuleNotesUpdate,
    token: TokenDep,
    db: DbDep,
) -> DriverPayRuleSummary:
    return await service.update_driver_pay_rule_notes(
        rule_id=rule_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        notes=body.notes,
        db=db,
    )


@router.post(
    "/driver-pay-rules/{rule_id}/void",
    response_model=DriverPayRuleSummary,
    summary="Void a driver pay rule (only if no finalized period governed it)",
    responses={
        403: {"description": "No setup.manage permission"},
        404: {"description": "Rule not found"},
        422: {"description": "Rule already Voided, or finalized period exists in range"},
    },
)
async def void_driver_pay_rule(
    rule_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverPayRuleSummary:
    return await service.void_driver_pay_rule(
        rule_id=rule_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


# ---------------------------------------------------------------------------
# CP-2.5 — Period-level Drivers Off
# ---------------------------------------------------------------------------

@router.get(
    "/periods/{period_id}/drivers-off",
    response_model=DriversOffResponse,
    summary="All off-driver records for an entire period (all work dates)",
    description=(
        "Returns every DailyStatus line in the period whose status key has "
        "IsOffReason=TRUE.  Unlike the day-grid which shows only one day's "
        "off drivers, this endpoint spans the entire period. "
        "ODA/Driver users receive 403."
    ),
    responses={
        403: {"description": "ODA/Driver user or no payroll.view/payroll.entry permission"},
        404: {"description": "Period not found"},
    },
)
async def get_period_drivers_off(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriversOffResponse:
    entries_raw = await service.get_drivers_off(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
    from app.payroll.schemas import DriversOffEntry
    entries = [DriversOffEntry(**e) for e in entries_raw]
    return DriversOffResponse(
        period_id=period_id,
        entries=entries,
        total_count=len(entries),
    )


# ---------------------------------------------------------------------------
# CP-1 — Day Grid
# ---------------------------------------------------------------------------

@router.get(
    "/periods/{period_id}/day-grid",
    response_model=DayGridResponse,
    summary="Get the daily entry grid for a single work date",
    responses={
        400: {"description": "work_date outside period bounds"},
        403: {"description": "No payroll.view or payroll.entry permission"},
        404: {"description": "Period not found"},
    },
)
async def get_day_grid(
    period_id: int,
    token: TokenDep,
    db: DbDep,
    work_date: date | None = Query(
        default=None,
        description=(
            "Work date (YYYY-MM-DD). "
            "If omitted, the backend picks today if within the period, "
            "otherwise the period start date."
        ),
    ),
) -> DayGridResponse:
    return await service.get_day_grid(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        work_date=work_date,
        db=db,
    )


@router.post(
    "/periods/{period_id}/day-grid",
    response_model=DayGridResponse,
    summary="Batch-save a day grid (upsert all driver rows for one work date)",
    responses={
        400: {"description": "work_date outside period bounds"},
        403: {"description": "No payroll.entry permission, or period not editable"},
        404: {"description": "Period not found"},
        422: {"description": "Validation error"},
    },
)
async def save_day_grid(
    period_id: int,
    body: DayGridSaveRequest,
    token: TokenDep,
    db: DbDep,
) -> DayGridResponse:
    return await service.save_day_grid(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )
