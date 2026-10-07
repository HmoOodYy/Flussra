"""Target Compensation HTTP surface, mounted at /compensation.

Generic PayDefinition governance and Driver rate assignment authoring for the
target model. These routes are not used by payroll runtime or by the current
frontend; legacy PayItems, CDPI and DriverRates remain the operational path.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.compensation import assignments, definitions, resolver
from app.compensation.errors import CompensationOwnershipError, compensation_error
from app.compensation.guards import load_company_driver_branch, require_rate_read
from app.compensation.schemas import (
    AssignmentCreate,
    AssignmentSummary,
    AssignmentUpdate,
    AssignmentValuesReplace,
    AssignmentVoid,
    DriverRateSummaryItem,
    PayDefinitionDirectCreate,
    PayDefinitionRequestCreate,
    PayDefinitionRequestDecision,
    PayDefinitionRequestEvent,
    PayDefinitionRequestSubmit,
    PayDefinitionRequestSummary,
    PayDefinitionRequestUpdate,
    PayDefinitionSummary,
    ResolvedCompensation,
)
from app.dependencies import get_current_user, get_db

router = APIRouter()

TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep = Annotated[object, Depends(get_db)]


def _ids(token: dict) -> tuple[int, int]:
    return int(token["cid"]), int(token["sub"])


# ---------------------------------------------------------------------------
# PayDefinition governance
# ---------------------------------------------------------------------------

@router.post("/pay-definition-requests", response_model=PayDefinitionRequestSummary,
             status_code=201, summary="Create a PayDefinition request draft")
async def create_request(
    token: TokenDep, db: DbDep, body: PayDefinitionRequestCreate,
) -> PayDefinitionRequestSummary:
    company_id, user_id = _ids(token)
    return await definitions.create_draft(company_id, user_id, body, db)


@router.get("/pay-definition-requests", response_model=list[PayDefinitionRequestSummary],
            summary="List PayDefinition requests")
async def list_requests(
    token: TokenDep, db: DbDep,
    status_filter: str | None = Query(None, alias="status"),
    branch_id: int | None = Query(None),
) -> list[PayDefinitionRequestSummary]:
    company_id, user_id = _ids(token)
    return await definitions.list_requests(
        company_id, user_id, db, status_filter=status_filter, branch_id=branch_id)


@router.get("/pay-definition-requests/{request_id}", response_model=PayDefinitionRequestSummary,
            summary="Get a PayDefinition request")
async def get_request(token: TokenDep, db: DbDep, request_id: UUID) -> PayDefinitionRequestSummary:
    company_id, user_id = _ids(token)
    return await definitions.get_request(company_id, user_id, request_id, db)


@router.patch("/pay-definition-requests/{request_id}", response_model=PayDefinitionRequestSummary,
              summary="Update a PayDefinition request draft")
async def update_request(
    token: TokenDep, db: DbDep, request_id: UUID, body: PayDefinitionRequestUpdate,
) -> PayDefinitionRequestSummary:
    company_id, user_id = _ids(token)
    return await definitions.update_draft(company_id, user_id, request_id, body, db)


@router.get("/pay-definition-requests/{request_id}/events",
            response_model=list[PayDefinitionRequestEvent],
            summary="List the append-only history of a PayDefinition request")
async def list_request_events(
    token: TokenDep, db: DbDep, request_id: UUID,
) -> list[PayDefinitionRequestEvent]:
    company_id, user_id = _ids(token)
    return await definitions.list_events(company_id, user_id, request_id, db)


@router.post("/pay-definition-requests/{request_id}/submit",
             response_model=PayDefinitionRequestSummary,
             summary="Submit a PayDefinition request draft")
async def submit_request(
    token: TokenDep, db: DbDep, request_id: UUID, body: PayDefinitionRequestSubmit,
) -> PayDefinitionRequestSummary:
    company_id, user_id = _ids(token)
    return await definitions.submit_draft(company_id, user_id, request_id, body, db)


@router.post("/pay-definition-requests/{request_id}/decide",
             response_model=PayDefinitionRequestSummary,
             summary="Return, reject or approve a pending PayDefinition request")
async def decide_request(
    token: TokenDep, db: DbDep, request_id: UUID, body: PayDefinitionRequestDecision,
) -> PayDefinitionRequestSummary:
    company_id, user_id = _ids(token)
    return await definitions.decide_request(company_id, user_id, request_id, body, db)


@router.post("/pay-definition-requests/{request_id}/copy",
             response_model=PayDefinitionRequestSummary, status_code=201,
             summary="Copy a rejected PayDefinition request into a new draft")
async def copy_request(
    token: TokenDep, db: DbDep, request_id: UUID,
) -> PayDefinitionRequestSummary:
    company_id, user_id = _ids(token)
    return await definitions.copy_rejected(company_id, user_id, request_id, db)


@router.post("/pay-definitions", response_model=PayDefinitionSummary, status_code=201,
             summary="Create a Company PayDefinition directly")
async def create_pay_definition(
    token: TokenDep, db: DbDep, body: PayDefinitionDirectCreate,
) -> PayDefinitionSummary:
    company_id, user_id = _ids(token)
    return await definitions.create_direct(company_id, user_id, body, db)


@router.get("/pay-definitions", response_model=list[PayDefinitionSummary],
            summary="List Company PayDefinitions")
async def list_pay_definitions(token: TokenDep, db: DbDep) -> list[PayDefinitionSummary]:
    company_id, user_id = _ids(token)
    return await definitions.list_definitions(company_id, user_id, db)


@router.get("/pay-definitions/{pay_definition_id}", response_model=PayDefinitionSummary,
            summary="Get a Company PayDefinition with its structure and provenance")
async def get_pay_definition(
    token: TokenDep, db: DbDep, pay_definition_id: int,
) -> PayDefinitionSummary:
    company_id, user_id = _ids(token)
    return await definitions.get_definition(company_id, user_id, pay_definition_id, db)


# ---------------------------------------------------------------------------
# Driver rate assignments
# ---------------------------------------------------------------------------

@router.post("/driver-rate-assignments", response_model=AssignmentSummary, status_code=201,
             summary="Create a Pending Driver rate assignment")
async def create_assignment(
    token: TokenDep, db: DbDep, body: AssignmentCreate,
) -> AssignmentSummary:
    company_id, user_id = _ids(token)
    return await assignments.create_pending(company_id, user_id, body, db)


@router.get("/driver-rate-assignments/{assignment_id}", response_model=AssignmentSummary,
            summary="Get a Driver rate assignment with its complete value set")
async def get_assignment(token: TokenDep, db: DbDep, assignment_id: int) -> AssignmentSummary:
    company_id, user_id = _ids(token)
    return await assignments.get_assignment(company_id, user_id, assignment_id, db)


@router.patch("/driver-rate-assignments/{assignment_id}", response_model=AssignmentSummary,
              summary="Edit a Pending Driver rate assignment")
async def update_assignment(
    token: TokenDep, db: DbDep, assignment_id: int, body: AssignmentUpdate,
) -> AssignmentSummary:
    company_id, user_id = _ids(token)
    return await assignments.update_pending(company_id, user_id, assignment_id, body, db)


@router.put("/driver-rate-assignments/{assignment_id}/values", response_model=AssignmentSummary,
            summary="Replace the value set of a Pending Driver rate assignment")
async def replace_assignment_values(
    token: TokenDep, db: DbDep, assignment_id: int, body: AssignmentValuesReplace,
) -> AssignmentSummary:
    company_id, user_id = _ids(token)
    return await assignments.replace_values(company_id, user_id, assignment_id, body, db)


@router.post("/driver-rate-assignments/{assignment_id}/approve",
             response_model=AssignmentSummary,
             summary="Approve a Pending Driver rate assignment, superseding the current one")
async def approve_assignment(
    token: TokenDep, db: DbDep, assignment_id: int,
) -> AssignmentSummary:
    company_id, user_id = _ids(token)
    return await assignments.approve(company_id, user_id, assignment_id, db)


@router.post("/driver-rate-assignments/{assignment_id}/void",
             response_model=AssignmentSummary,
             summary="Void a previously authoritative Driver rate assignment")
async def void_assignment(
    token: TokenDep, db: DbDep, assignment_id: int, body: AssignmentVoid,
) -> AssignmentSummary:
    company_id, user_id = _ids(token)
    return await assignments.void(company_id, user_id, assignment_id, body, db)


@router.delete("/driver-rate-assignments/{assignment_id}", status_code=204,
               summary="Discard a Pending Driver rate assignment")
async def discard_assignment(token: TokenDep, db: DbDep, assignment_id: int) -> Response:
    company_id, user_id = _ids(token)
    await assignments.discard(company_id, user_id, assignment_id, db)
    return Response(status_code=204)


@router.get("/drivers/{driver_id}/rate-definitions/{rate_definition_id}/assignments",
            response_model=list[AssignmentSummary],
            summary="Driver rate assignment history for one rate definition")
async def assignment_history(
    token: TokenDep, db: DbDep, driver_id: int, rate_definition_id: int,
) -> list[AssignmentSummary]:
    company_id, user_id = _ids(token)
    return await assignments.history(company_id, user_id, driver_id, rate_definition_id, db)


@router.get("/drivers/{driver_id}/rate-assignments/summary",
            response_model=list[DriverRateSummaryItem],
            summary="Current and Pending assignment per rate definition for a Driver")
async def driver_assignment_summary(
    token: TokenDep, db: DbDep, driver_id: int,
) -> list[DriverRateSummaryItem]:
    company_id, user_id = _ids(token)
    return await assignments.driver_summary(company_id, user_id, driver_id, db)


@router.get("/drivers/{driver_id}/rate-definitions/{rate_definition_id}/resolution",
            response_model=ResolvedCompensation,
            summary="Resolve the whole assignment effective on a work date")
async def resolve_assignment(
    token: TokenDep, db: DbDep, driver_id: int, rate_definition_id: int,
    work_date: date = Query(...),
) -> ResolvedCompensation:
    company_id, user_id = _ids(token)
    branch_id = await load_company_driver_branch(company_id, user_id, driver_id, db)
    await require_rate_read(company_id, user_id, branch_id, db)
    try:
        resolved = await resolver.resolve(company_id, driver_id, rate_definition_id, work_date, db)
    except CompensationOwnershipError as exc:
        raise compensation_error("RATE_DEFINITION_NOT_FOUND", str(exc), 404) from exc
    if resolved is None:
        raise compensation_error(
            "NO_EFFECTIVE_ASSIGNMENT",
            "No authoritative assignment is effective on that date.", 404)
    return resolved
