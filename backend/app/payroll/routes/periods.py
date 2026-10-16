"""Period reads/transitions, candidate creation, and Current Payroll workflow routes."""

from fastapi import APIRouter, Query, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.transaction_retry import run_retryable_transaction
from app.payroll import (
    current_hub,
    period_creation,
    period_lifecycle,
    period_read,
)
from app.payroll.schemas import (
    CandidatePreviewResponse,
    CurrentPayrollHubResponse,
    CurrentWorkflowResponse,
    PeriodCreationRequest,
    PeriodCreationResponse,
    PeriodEntryCount,
    PeriodStatusChange,
    PeriodSummary,
)

from .dependencies import DbDep, EngineDep, TokenDep

router = APIRouter()
creation_router = APIRouter()
workflow_router = APIRouter()


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
    return await period_read.get_periods(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
        period_status=period_status,
        limit=limit,
        offset=offset,
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
    return await period_read.get_period_entry_count(
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
    return await period_read.get_period_by_id(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        period_id=period_id,
        db=db,
    )


@router.patch(
    "/periods/{period_id}/status",
    response_model=PeriodSummary,
    summary="Transition a period to a new status",
    description=(
        "Allowed transitions:\n"
        "- **Draft** → Cancelled *(Draft→Open is handled atomically by the submit path)*\n"
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
    engine: EngineDep,
) -> PeriodSummary:
    async def operation(db: AsyncConnection) -> PeriodSummary:
        return await period_lifecycle.change_period_status(
            company_id=int(token["cid"]),
            user_id=int(token["sub"]),
            period_id=period_id,
            change=body,
            db=db,
        )

    if body.status == "InReview":
        return await run_retryable_transaction(engine, operation, operation_name="submit")
    async with engine.begin() as db:
        return await operation(db)


@router.post(
    "/periods/{period_id}/resubmissions",
    response_model=PeriodSummary,
    status_code=200,
    summary="Resubmit a Returned period for review",
    description=(
        "Transitions a **Returned** period back to **InReview**.\n\n"
        "Requires `payroll.entry` permission. DRIVER/Self users are blocked.\n\n"
        "Reruns all Open→InReview submission guards (empty-period, NeedsManagerReview, "
        "zero-calc, duplicate-Pending) before creating a new Pending PeriodApproval "
        "review item. Clears `CurrentReturnReviewItemID` atomically.\n\n"
        "The resolved review item from the previous return is preserved unchanged.\n\n"
        "Returns **409** if the period is no longer Returned at the write boundary."
    ),
    responses={
        403: {"description": "No access to this period's branch, or DRIVER/Self user"},
        404: {"description": "Period not found"},
        409: {"description": "Period is no longer Returned — concurrent transition"},
        422: {"description": "Period not Returned, or submission guard failed"},
    },
)
async def resubmit_period(
    period_id: int,
    token: TokenDep,
    engine: EngineDep,
) -> PeriodSummary:
    async def operation(db: AsyncConnection) -> PeriodSummary:
        return await period_lifecycle.resubmit_period(
            company_id=int(token["cid"]),
            user_id=int(token["sub"]),
            period_id=period_id,
            db=db,
        )

    return await run_retryable_transaction(engine, operation, operation_name="resubmit")


@creation_router.get(
    "/branches/{branch_id}/period-candidates",
    response_model=CandidatePreviewResponse,
    summary="Preview the current creatable period candidate for a branch",
    description=(
        "Returns a signed candidate key for the next period that can be created "
        "for the branch. The candidate is deterministic: same branch state → same key. "
        "mode must be OPEN_CREATION or PREPARED_CREATION. "
        "Optionally pass cursor (a previous candidate_key) to navigate to adjacent periods."
    ),
    responses={
        403: {"description": "Access denied or missing payroll.period.create permission"},
        409: {"description": "Branch inactive, setup missing/incomplete, or invalid cursor"},
        422: {"description": "Invalid mode parameter"},
    },
)
async def get_period_candidates(
    branch_id: int,
    token: TokenDep,
    db: DbDep,
    mode: str = Query(..., description="OPEN_CREATION or PREPARED_CREATION"),
    cursor: str | None = Query(
        None, description="Signed cursor from a previous candidate_key for navigation"
    ),
) -> CandidatePreviewResponse:
    return await period_creation.get_period_candidates(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        branch_id=branch_id,
        mode=mode,
        cursor_key=cursor,
        db=db,
    )


@creation_router.post(
    "/branches/{branch_id}/period-creations",
    response_model=PeriodCreationResponse,
    status_code=201,
    summary="Create a payroll period from a signed candidate key",
    description=(
        "Accepts a candidate_key from GET period-candidates and creates the period. "
        "Idempotent: submitting the same key twice returns 200 ALREADY_EXISTS. "
        "The backend re-validates all state under an advisory lock before inserting."
    ),
    responses={
        200: {"description": "Period already exists (ALREADY_EXISTS replay)"},
        201: {"description": "Period created successfully (CREATED)"},
        403: {"description": "Access denied or missing payroll.period.create permission"},
        409: {
            "description": (
                "INVALID_CANDIDATE_KEY | CANDIDATE_NOT_CURRENT | CANDIDATE_STALE | "
                "CANDIDATE_SETUP_CHANGED | CANDIDATE_ALREADY_CANCELLED | "
                "OPEN_REQUIRED | OPEN_FILLED | ACTIVE_PERIOD_SLOTS_FULL | "
                "DRAFT_WITHOUT_OPEN | SLOT_INVARIANT_VIOLATION | "
                "PERIOD_DATE_OVERLAP | BRANCH_INACTIVE"
            )
        },
    },
)
async def create_period_from_candidate(
    branch_id: int,
    body: PeriodCreationRequest,
    token: TokenDep,
    db: DbDep,
    response: Response,
) -> PeriodCreationResponse:
    result = await period_creation.create_period_from_candidate(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        branch_id=branch_id,
        data=body,
        db=db,
    )
    if result.result == "ALREADY_EXISTS":
        response.status_code = 200
    return result


@workflow_router.get(
    "/current",
    response_model=CurrentPayrollHubResponse,
    summary="Get the Current Payroll Hub for accessible branches",
    description=(
        "Returns the backend-owned active workflow aggregate. Open and Returned "
        "financial summaries are live CP-4B calculation data; Prepared and InReview "
        "do not expose live financial authority."
    ),
    responses={
        403: {"description": "No branch access, driver role, or no payroll read permission"},
    },
)
async def get_current_payroll_hub(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None, description="Scope to a specific branch (optional)"),
) -> CurrentPayrollHubResponse:
    return await current_hub.get_current_payroll_hub(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        branch_id=branch_id,
        db=db,
    )


@workflow_router.get(
    "/current-workflow",
    response_model=CurrentWorkflowResponse,
    summary="Get current workflow slots, alerts, and capabilities for accessible branches",
    description=(
        "Returns backend-owned workflow state: active period slots (Open, Prepared/Draft, "
        "InReview, Returned), per-period and per-branch workflow capabilities, and alerts.\n\n"
        "This is NOT a financial hub — it returns no financial totals, no expected income, "
        "no calculation preview, and no report data.\n\n"
        "Pass `branch_id` to scope to a single branch; omit to return all accessible branches."
    ),
    responses={
        403: {"description": "No branch access, driver role, or no payroll read permission"},
    },
)
async def get_current_workflow(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = Query(None, description="Scope to a specific branch (optional)"),
) -> CurrentWorkflowResponse:
    return await current_hub.get_current_workflow(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        branch_id=branch_id,
        db=db,
    )
