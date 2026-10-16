"""Payroll reporting route handlers."""

from fastapi import APIRouter

from app.payroll import (
    report_read_model,
)
from app.payroll.schemas import (
    DriversReportResponse,
    MixedReportResponse,
    PeriodPayReportResponse,
    PeriodWorkReportResponse,
)

from .dependencies import DbDep, TokenDep

router = APIRouter()


@router.get(
    "/periods/{period_id}/reports/drivers",
    response_model=DriversReportResponse,
    summary="Get the driver-centric calculation report",
)
async def get_drivers_report(period_id: int, token: TokenDep, db: DbDep) -> DriversReportResponse:
    return await report_read_model.build_report(
        report_type="drivers",
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/periods/{period_id}/reports/period-work",
    response_model=PeriodWorkReportResponse,
    summary="Get the operational period-work report",
)
async def get_period_work_report(
    period_id: int, token: TokenDep, db: DbDep
) -> PeriodWorkReportResponse:
    return await report_read_model.build_report(
        report_type="period-work",
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/periods/{period_id}/reports/period-pay",
    response_model=PeriodPayReportResponse,
    summary="Get the authoritative period-pay report",
)
async def get_period_pay_report(
    period_id: int, token: TokenDep, db: DbDep
) -> PeriodPayReportResponse:
    return await report_read_model.build_report(
        report_type="period-pay",
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/periods/{period_id}/reports/mixed",
    response_model=MixedReportResponse,
    summary="Get the combined operational work and authoritative pay report",
)
async def get_mixed_report(period_id: int, token: TokenDep, db: DbDep) -> MixedReportResponse:
    return await report_read_model.build_report(
        report_type="mixed",
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
