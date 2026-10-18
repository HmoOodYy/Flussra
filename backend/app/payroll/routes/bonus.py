"""Payroll bonus route handlers."""

from fastapi import APIRouter, Query, Response

from app.payroll import (
    bonus,
    eligibility,
)
from app.payroll.schemas import (
    BonusBatchCreate,
    BonusBatchResponse,
    BonusEventCreate,
    BonusEventResponse,
    BonusEventUpdate,
    BonusSummaryResponse,
    PeriodEligibleDriversResponse,
)

from .dependencies import DbDep, TokenDep

router = APIRouter()


@router.get(
    "/periods/{period_id}/bonuses",
    response_model=list[BonusEventResponse],
    summary="List bonus events for a period",
)
async def list_bonus_events(
    period_id: int,
    token: TokenDep,
    db: DbDep,
    driver_assignment_id: int | None = Query(None, description="Filter to a specific driver"),
) -> list[BonusEventResponse]:
    return await bonus.list_bonus_events(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        driver_assignment_id=driver_assignment_id,
    )


@router.get(
    "/periods/{period_id}/bonuses/summary",
    response_model=BonusSummaryResponse,
    summary="Zero-inclusive bonus summary for a period (CP-3B1)",
    responses={
        422: {"description": "Draft period, or period has no CP-2E eligibility snapshot"},
    },
)
async def get_bonus_summary(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> BonusSummaryResponse:
    return await bonus.get_bonus_summary(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.post(
    "/periods/{period_id}/bonuses/batch",
    response_model=BonusBatchResponse,
    status_code=201,
    summary="Create-only transactional bonus batch (CP-3B2b)",
    responses={
        200: {"description": "Idempotent replay of a previously-applied batch"},
        409: {"description": "Stale expected revision, or idempotency-key payload conflict"},
        422: {"description": "Period not Open/Returned, or an item failed validation"},
    },
)
async def apply_bonus_batch(
    period_id: int,
    data: BonusBatchCreate,
    token: TokenDep,
    db: DbDep,
    response: Response,
) -> BonusBatchResponse:
    result, is_new = await bonus.apply_bonus_batch(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=data,
        db=db,
    )
    # 201 on a fresh apply; 200 on an idempotent replay (nothing was written).
    response.status_code = 201 if is_new else 200
    return result


@router.post(
    "/periods/{period_id}/bonuses",
    response_model=BonusEventResponse,
    status_code=201,
    summary="Add a canonical bonus event to a period",
    responses={
        422: {"description": "Period not Open/Returned, or amount not positive"},
    },
)
async def create_bonus_event(
    period_id: int,
    data: BonusEventCreate,
    token: TokenDep,
    db: DbDep,
) -> BonusEventResponse:
    return await bonus.create_bonus_event(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=data,
        db=db,
    )


@router.patch(
    "/periods/{period_id}/bonuses/{bonus_event_id}",
    response_model=BonusEventResponse,
    summary="Update a bonus event (amount, reason, notes)",
    responses={
        409: {"description": "Optimistic concurrency conflict"},
        422: {"description": "Period not Open/Returned, or event already voided"},
    },
)
async def update_bonus_event(
    period_id: int,
    bonus_event_id: int,
    data: BonusEventUpdate,
    token: TokenDep,
    db: DbDep,
) -> BonusEventResponse:
    return await bonus.update_bonus_event(
        period_id=period_id,
        bonus_event_id=bonus_event_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=data,
        db=db,
    )


@router.delete(
    "/periods/{period_id}/bonuses/{bonus_event_id}",
    response_model=BonusEventResponse,
    summary="Void a bonus event (idempotent)",
    responses={
        422: {"description": "Period not Open/Returned"},
    },
)
async def void_bonus_event(
    period_id: int,
    bonus_event_id: int,
    token: TokenDep,
    db: DbDep,
) -> BonusEventResponse:
    return await bonus.void_bonus_event(
        period_id=period_id,
        bonus_event_id=bonus_event_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@router.get(
    "/periods/{period_id}/eligible-drivers",
    response_model=PeriodEligibleDriversResponse,
    summary="List drivers eligible for a new Bonus (period-scoped, not day-scoped)",
    description=(
        "Returns drivers who were active in the period's branch during the period dates. "
        "Drivers included only because of existing saved source "
        "(IncludedByExistingData) are not offered for new Bonus entries. "
        "This is the stable source-of-truth for the Bonus driver dropdown — "
        "it does not change when the user navigates to a different day in the grid. "
        "DRIVER/Self users receive 403."
    ),
    responses={
        403: {"description": "DRIVER/Self user or no payroll.view/payroll.entry permission"},
        404: {"description": "Period not found"},
    },
)
async def get_period_eligible_drivers(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> PeriodEligibleDriversResponse:
    drivers = await eligibility.get_period_eligible_drivers(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
    from app.payroll.schemas import PeriodEligibleDriver

    return PeriodEligibleDriversResponse(drivers=[PeriodEligibleDriver(**d) for d in drivers])
