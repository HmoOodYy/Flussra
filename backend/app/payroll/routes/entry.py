"""Source-line, Off-driver read, and Day Grid routes."""

from datetime import date

from fastapi import APIRouter, Query, Response

from app.payroll import (
    day_grid,
    draft_line_mutation,
    off_drivers,
    source_line_read,
)
from app.payroll.schemas import (
    DayGridResponse,
    DayGridSaveRequest,
    DraftLineCreate,
    DraftLineSummary,
    DraftLineUpdate,
    DriverPeriodSummary,
    OffDriversSummaryResponse,
    SelectedDayOffDriversResponse,
)

from .dependencies import DbDep, TokenDep

lines_router = APIRouter()
off_drivers_router = APIRouter()
day_grid_router = APIRouter()


@lines_router.get(
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
    driver_assignment_id: int | None = Query(None, description="Filter to a specific driver"),
    work_date: date | None = Query(None, description="Filter to a specific work date (YYYY-MM-DD)"),
    line_status: str | None = Query(
        None,
        alias="status",
        description="Active | NeedsReview | Rejected | Void",
    ),
) -> list[DraftLineSummary]:
    return await source_line_read.get_period_lines(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        driver_assignment_id=driver_assignment_id,
        work_date=work_date,
        line_status=line_status,
    )


@lines_router.get(
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
    return await source_line_read.get_period_draft_summary(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@lines_router.post(
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
    return await draft_line_mutation.add_draft_line(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@lines_router.patch(
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
    return await draft_line_mutation.update_draft_line(
        period_id=period_id,
        draft_line_id=line_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )


@lines_router.delete(
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
    await draft_line_mutation.void_draft_line(
        period_id=period_id,
        draft_line_id=line_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )
    return Response(status_code=204)


@off_drivers_router.get(
    "/periods/{period_id}/off-drivers/summary",
    response_model=OffDriversSummaryResponse,
    summary="Get the distinct fully-off drivers for a payroll period",
    responses={
        403: {"description": "DRIVER/Self user or no payroll.view/payroll.entry permission"},
        404: {"description": "Period not found"},
    },
)
async def get_period_off_drivers_summary(
    period_id: int,
    token: TokenDep,
    db: DbDep,
) -> OffDriversSummaryResponse:
    return await off_drivers.get_off_drivers_summary(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@off_drivers_router.get(
    "/periods/{period_id}/off-drivers",
    response_model=SelectedDayOffDriversResponse,
    summary="Get Off drivers for one payroll period work date",
    responses={
        400: {"description": "work_date outside period bounds or period day snapshot"},
        403: {"description": "DRIVER/Self user or no payroll.view/payroll.entry permission"},
        404: {"description": "Period not found"},
    },
)
async def get_period_off_drivers(
    period_id: int,
    token: TokenDep,
    db: DbDep,
    work_date: date | None = Query(default=None, description="Date inside the payroll period; omitted resolves from Branch business date"),
) -> SelectedDayOffDriversResponse:
    return await off_drivers.get_selected_day_off_drivers(
        period_id=period_id,
        work_date=work_date,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
    )


@day_grid_router.get(
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
    return await day_grid.get_day_grid(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        work_date=work_date,
        db=db,
    )


@day_grid_router.post(
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
    return await day_grid.save_day_grid(
        period_id=period_id,
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=body,
        db=db,
    )
