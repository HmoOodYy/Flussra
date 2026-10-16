"""Payroll rates route handlers."""

from datetime import date

from fastapi import APIRouter, Query, Response

from app.payroll import (
    rate_reads,
    rates,
)
from app.payroll.schemas import (
    BatchRateRequest,
    BatchRateSaveResult,
    CopyRatesRequest,
    CopyRatesResult,
    DriverRateCreate,
    DriverRateMatrix,
    DriverRatesSummary,
    DriverRateSummary,
    DriverRateUpdate,
    RateLookupResult,
    RateTypeSummary,
)

from .dependencies import DbDep, TokenDep

router = APIRouter()


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
    return await rate_reads.get_rate_types(
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
    return await rate_reads.get_rates(
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
    rate = await rate_reads.resolve_rate_for_date(
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
    return await rate_reads.get_rate_by_id(
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
    return await rates.create_rate(
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
    return await rates.update_rate(
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
    return await rates.approve_rate(
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
    await rates.void_rate(
        rate_id=rate_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
    return Response(status_code=204)


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
    return await rate_reads.get_driver_rate_matrix(
        driver_id=driver_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        as_of_date=effective_date,
        db=db,
    )


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
        403: {
            "description": "No access to the driver's branch or payrates.edit permission missing"
        },
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
    return await rates.batch_save_rates(
        driver_id=driver_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


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
        403: {"description": "DRIVER/Self user, or no access to this driver's branch"},
        404: {"description": "Driver not found"},
    },
)
async def get_driver_rates_summary(
    driver_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverRatesSummary:
    return await rate_reads.get_driver_rates_summary(
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
        403: {"description": "DRIVER/Self user, or no access to this driver's branch"},
        404: {"description": "Driver not found"},
    },
)
async def list_driver_rates_pending(
    driver_id: int,
    token: TokenDep,
    db: DbDep,
) -> list[DriverRateSummary]:
    return await rate_reads.get_driver_rates_pending(
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
        403: {"description": "DRIVER/Self user, or no access to this driver's branch"},
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
    return await rate_reads.get_driver_rates_history(
        driver_id=driver_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/drivers/rates-summary",
    response_model=list[DriverRatesSummary],
    summary="Bulk status counts for all accessible drivers (left-panel badges)",
    description=(
        "Returns pending/future-approved counts for every driver the caller can access. "
        "DRIVER/Self users are denied this generic rate administration endpoint. SpecificBranch users see only their branch. "
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
    branch_id: int | None = Query(
        None, description="Optional branch filter for AllCompanyBranches users"
    ),
) -> list[DriverRatesSummary]:
    return await rate_reads.get_bulk_driver_rates_summary(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
    )


@router.post(
    "/drivers/{target_driver_id}/rates/copy-from/{source_driver_id}",
    response_model=CopyRatesResult,
    status_code=200,
    summary="Copy Approved rates from one driver to another",
    description=(
        "Copies current Approved rates from source_driver (as-of effective_from) to "
        "target_driver.  Requires payrates.edit on both driver branches.  "
        "DRIVER/Self users cannot use this endpoint.\n\n"
        "Rates become PendingApproval unless AllowSelfApproval=True, in which case "
        "they auto-approve and supersede existing rates.\n\n"
        "Only rates valid for the target driver's branch are copied; "
        "rate types not configured for the target branch are silently skipped.\n\n"
        "Set include_pay_rules=true to also copy Active MinimumPay/MaximumPay rules."
    ),
    responses={
        403: {"description": "DRIVER/Self user, insufficient permission, or cross-company attempt"},
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
    return await rates.copy_driver_rates(
        target_driver_id=target_driver_id,
        source_driver_id=source_driver_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )
