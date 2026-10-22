"""Payroll finalized route handlers."""

from fastapi import APIRouter, Query

from app.payroll import (
    finalized_audit,
    finalized_library_read_model,
)
from app.payroll.schemas import (
    FinalizedAuditResponse,
    FinalizedCalculationReportResponse,
    FinalizedOffDriversResponse,
    FinalizedOverviewResponse,
    FinalizedPeriodListItem,
    FinalizedRatesUsedResponse,
)

from .dependencies import DbDep, TokenDep

list_router = APIRouter()
router = APIRouter()


@list_router.get(
    "/finalized",
    response_model=list[FinalizedPeriodListItem],
    summary="List finalized payroll periods in the ledger scope",
)
async def list_finalized_periods(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None, description="Filter to a specific accessible branch"),
    period_status: str | None = Query(
        None,
        alias="status",
        pattern="^(Locked|Archived)$",
        description="Locked or Archived",
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[FinalizedPeriodListItem]:
    return await finalized_library_read_model.list_finalized_periods(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
        period_status=period_status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/finalized/{period_id}/overview",
    response_model=FinalizedOverviewResponse,
    summary="Get the lightweight finalized payroll overview",
)
async def get_finalized_overview(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> FinalizedOverviewResponse:
    return await finalized_library_read_model.build_overview(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/finalized/{period_id}/reports/{view}",
    response_model=FinalizedCalculationReportResponse,
    summary="Get an immutable finalized payroll report",
)
async def get_finalized_report(
    period_id: int,
    view: str,
    token: TokenDep,
    db: DbDep,
) -> FinalizedCalculationReportResponse:
    return await finalized_library_read_model.build_finalized_report(
        report_type=view,
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/finalized/{period_id}/off-drivers",
    response_model=FinalizedOffDriversResponse,
    summary="Get immutable finalized Off and Status evidence",
)
async def get_finalized_off_drivers(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> FinalizedOffDriversResponse:
    return await finalized_library_read_model.build_finalized_off_drivers(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/finalized/{period_id}/rates-used",
    response_model=FinalizedRatesUsedResponse,
    summary="Get immutable rates, rules, and Bonus evidence used by finalized payroll",
)
async def get_finalized_rates_used(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> FinalizedRatesUsedResponse:
    return await finalized_library_read_model.build_finalized_rates_used(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/finalized/{period_id}/audit",
    response_model=FinalizedAuditResponse,
    summary="Get immutable finalized payroll audit and security detail",
)
async def get_finalized_audit(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> FinalizedAuditResponse:
    return await finalized_audit.build_finalized_audit(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
