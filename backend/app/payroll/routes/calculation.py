"""Payroll calculation route handlers."""

from fastapi import APIRouter

from app.payroll import (
    finalization,
    period_calculation,
)
from app.payroll.schemas import (
    CalculationPreviewResponse,
    FinalizationPreviewResponse,
    PeriodSummary,
)

from .dependencies import DbDep, TokenDep

router = APIRouter()


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
    return await finalization.finalize_period(
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
        403: {"description": "DRIVER/Self user, or no payroll.finalize permission"},
        404: {"description": "Period not found"},
        422: {"description": "Period is not in Approved status"},
    },
)
async def get_finalization_preview(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> FinalizationPreviewResponse:
    return await finalization.get_finalization_preview(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/periods/{period_id}/calculation-preview",
    response_model=CalculationPreviewResponse,
    summary="Open/Returned live read-only calculation preview (CP-4B)",
    description=(
        "Returns a read-only, live provisional expected-income breakdown "
        "for an **Open** or **Returned** period, calculated from current "
        "effective source/config data. Never a submitted snapshot. "
        "Makes **no** DB mutations."
    ),
    responses={
        403: {"description": "DRIVER/Self user, or no payroll.view/payroll.entry permission"},
        404: {"description": "Period not found"},
        422: {"description": "Period is not in Open or Returned status"},
    },
)
async def get_calculation_preview(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> CalculationPreviewResponse:
    return await period_calculation.get_calculation_preview(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
