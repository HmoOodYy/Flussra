"""Compensation HTTP surface, mounted at /compensation.

Company-owned PayDefinition governance, Branch applicability and Driver rate
schedule authoring. These routes are the operational authority for
PayDefinitions, their Branch configuration and ordinary PerUnit rates; the
Settings Pay Items and Pay Rates pages call them.

Status rates retain their separate legacy path. Payroll owns period layout and
live calculation; its evidence gate still refuses target submission/finalization.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.compensation import branch_config, definitions, resolver, schedules
from app.compensation.driver_context import (
    driver_compensation_access,
    driver_compensation_read_scope,
)
from app.compensation.errors import CompensationOwnershipError, compensation_error
from app.compensation.schemas import (
    BranchConfigUpdate,
    BranchConfigVersion,
    BranchPayDefinitionState,
    BulkBranchConfigResult,
    BulkBranchConfigUpdate,
    CompensationAccess,
    DriverPayRateRow,
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
    ScheduleCreate,
    ScheduleSummary,
    ScheduleUpdate,
    ScheduleValuesReplace,
    ScheduleVoid,
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
async def list_pay_definitions(
    token: TokenDep, db: DbDep, include_retired: bool = Query(True),
) -> list[PayDefinitionSummary]:
    company_id, user_id = _ids(token)
    return await definitions.list_definitions(
        company_id, user_id, db, include_retired=include_retired)


@router.get("/pay-definitions/{pay_definition_id}", response_model=PayDefinitionSummary,
            summary="Get a Company PayDefinition with its structure and provenance")
async def get_pay_definition(
    token: TokenDep, db: DbDep, pay_definition_id: int,
) -> PayDefinitionSummary:
    company_id, user_id = _ids(token)
    return await definitions.get_definition(company_id, user_id, pay_definition_id, db)


@router.post("/pay-definitions/{pay_definition_id}/retire", response_model=PayDefinitionSummary,
             summary="Retire a Company PayDefinition without deleting any history")
async def retire_pay_definition(
    token: TokenDep, db: DbDep, pay_definition_id: int,
) -> PayDefinitionSummary:
    company_id, user_id = _ids(token)
    return await definitions.retire_definition(company_id, user_id, pay_definition_id, db)


# ---------------------------------------------------------------------------
# Branch applicability
# ---------------------------------------------------------------------------

@router.get("/branches/{branch_id}/pay-definitions",
            response_model=list[BranchPayDefinitionState],
            summary="Company PayDefinitions with their applicability to one Branch")
async def list_branch_pay_definitions(
    token: TokenDep, db: DbDep, branch_id: int,
) -> list[BranchPayDefinitionState]:
    company_id, user_id = _ids(token)
    return await branch_config.list_branch_definitions(company_id, user_id, branch_id, db)


@router.patch("/branches/{branch_id}/pay-definitions/{pay_definition_id}",
              response_model=BranchPayDefinitionState,
              summary="Set a PayDefinition's applicability to one Branch")
async def update_branch_pay_definition(
    token: TokenDep, db: DbDep, branch_id: int, pay_definition_id: int,
    body: BranchConfigUpdate,
) -> BranchPayDefinitionState:
    company_id, user_id = _ids(token)
    return await branch_config.update_branch_config(
        company_id, user_id, branch_id, pay_definition_id, body, db)


@router.get("/branches/{branch_id}/pay-definitions/{pay_definition_id}/history",
            response_model=list[BranchConfigVersion],
            summary="Applicability version history of a PayDefinition in one Branch")
async def branch_pay_definition_history(
    token: TokenDep, db: DbDep, branch_id: int, pay_definition_id: int,
) -> list[BranchConfigVersion]:
    company_id, user_id = _ids(token)
    return await branch_config.config_history(
        company_id, user_id, branch_id, pay_definition_id, db)


@router.patch("/pay-definitions/{pay_definition_id}/branch-config",
              response_model=BulkBranchConfigResult,
              summary="Set a PayDefinition's applicability across several Branches atomically")
async def bulk_update_branch_pay_definition(
    token: TokenDep, db: DbDep, pay_definition_id: int, body: BulkBranchConfigUpdate,
) -> BulkBranchConfigResult:
    company_id, user_id = _ids(token)
    return await branch_config.bulk_update_branch_config(
        company_id, user_id, pay_definition_id, body, db)


# ---------------------------------------------------------------------------
# Driver rate schedules (owned by the permanent Driver)
# ---------------------------------------------------------------------------

@router.post("/driver-rate-schedules", response_model=ScheduleSummary, status_code=201,
             summary="Create a Pending Driver rate schedule")
async def create_schedule(
    token: TokenDep, db: DbDep, body: ScheduleCreate,
) -> ScheduleSummary:
    company_id, user_id = _ids(token)
    return await schedules.create_pending(company_id, user_id, body, db)


@router.get("/driver-rate-schedules/{schedule_id}", response_model=ScheduleSummary,
            summary="Get a Driver rate schedule with its complete value set")
async def get_schedule(token: TokenDep, db: DbDep, schedule_id: int) -> ScheduleSummary:
    company_id, user_id = _ids(token)
    return await schedules.get_schedule(company_id, user_id, schedule_id, db)


@router.patch("/driver-rate-schedules/{schedule_id}", response_model=ScheduleSummary,
              summary="Edit a Pending Driver rate schedule")
async def update_schedule(
    token: TokenDep, db: DbDep, schedule_id: int, body: ScheduleUpdate,
) -> ScheduleSummary:
    company_id, user_id = _ids(token)
    return await schedules.update_pending(company_id, user_id, schedule_id, body, db)


@router.put("/driver-rate-schedules/{schedule_id}/values", response_model=ScheduleSummary,
            summary="Replace the value set of a Pending Driver rate schedule")
async def replace_schedule_values(
    token: TokenDep, db: DbDep, schedule_id: int, body: ScheduleValuesReplace,
) -> ScheduleSummary:
    company_id, user_id = _ids(token)
    return await schedules.replace_values(company_id, user_id, schedule_id, body, db)


@router.post("/driver-rate-schedules/{schedule_id}/approve",
             response_model=ScheduleSummary,
             summary="Approve a Pending Driver rate schedule, superseding the current one")
async def approve_schedule(
    token: TokenDep, db: DbDep, schedule_id: int,
) -> ScheduleSummary:
    company_id, user_id = _ids(token)
    return await schedules.approve(company_id, user_id, schedule_id, db)


@router.post("/driver-rate-schedules/{schedule_id}/void",
             response_model=ScheduleSummary,
             summary="Void a previously authoritative Driver rate schedule")
async def void_schedule(
    token: TokenDep, db: DbDep, schedule_id: int, body: ScheduleVoid,
) -> ScheduleSummary:
    company_id, user_id = _ids(token)
    return await schedules.void(company_id, user_id, schedule_id, body, db)


@router.delete("/driver-rate-schedules/{schedule_id}", status_code=204,
               summary="Discard a Pending Driver rate schedule")
async def discard_schedule(token: TokenDep, db: DbDep, schedule_id: int) -> Response:
    company_id, user_id = _ids(token)
    await schedules.discard(company_id, user_id, schedule_id, db)
    return Response(status_code=204)


@router.get("/drivers/{driver_id}/pay-rates", response_model=list[DriverPayRateRow],
            summary="Authorable PayDefinitions and the Driver's scalar rate state")
async def driver_pay_rates(
    token: TokenDep, db: DbDep, driver_id: int, as_of: date | None = Query(None),
) -> list[DriverPayRateRow]:
    company_id, user_id = _ids(token)
    return await schedules.driver_pay_rates(company_id, user_id, driver_id, as_of, db)


@router.get("/drivers/{driver_id}/rate-definitions/{rate_definition_id}/rate-schedules",
            response_model=list[ScheduleSummary],
            summary="Driver rate schedule history for one rate definition")
async def schedule_history(
    token: TokenDep, db: DbDep, driver_id: int, rate_definition_id: int,
) -> list[ScheduleSummary]:
    company_id, user_id = _ids(token)
    return await schedules.history(company_id, user_id, driver_id, rate_definition_id, db)


@router.get("/drivers/{driver_id}/rate-schedules/summary",
            response_model=list[DriverRateSummaryItem],
            summary="Approved and Pending schedule per rate definition for a Driver")
async def driver_schedule_summary(
    token: TokenDep, db: DbDep, driver_id: int,
) -> list[DriverRateSummaryItem]:
    company_id, user_id = _ids(token)
    return await schedules.driver_summary(company_id, user_id, driver_id, db)


@router.get("/drivers/{driver_id}/compensation-access",
            response_model=CompensationAccess,
            summary="What the caller may see and do with the Driver's compensation")
async def compensation_access(
    token: TokenDep, db: DbDep, driver_id: int,
) -> CompensationAccess:
    company_id, user_id = _ids(token)
    scope, can_discard, has_tenure = await driver_compensation_access(
        company_id, user_id, driver_id, db)
    return CompensationAccess(
        full_history=scope.full, can_discard_pending=can_discard,
        has_current_or_pending_tenure=has_tenure)


@router.get("/drivers/{driver_id}/rate-definitions/{rate_definition_id}/resolution",
            response_model=ResolvedCompensation,
            summary="Resolve the whole schedule effective on a work date")
async def resolve_schedule(
    token: TokenDep, db: DbDep, driver_id: int, rate_definition_id: int,
    work_date: date = Query(...),
) -> ResolvedCompensation:
    company_id, user_id = _ids(token)
    scope = await driver_compensation_read_scope(company_id, user_id, driver_id, db)
    if not scope.covers(work_date):
        raise compensation_error(
            "NO_EFFECTIVE_SCHEDULE",
            "No authoritative schedule is effective on that date.", 404)
    try:
        resolved = await resolver.resolve(company_id, driver_id, rate_definition_id, work_date, db)
    except CompensationOwnershipError as exc:
        raise compensation_error("RATE_DEFINITION_NOT_FOUND", str(exc), 404) from exc
    if resolved is None:
        raise compensation_error(
            "NO_EFFECTIVE_SCHEDULE",
            "No authoritative schedule is effective on that date.", 404)
    return resolved
