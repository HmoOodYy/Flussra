"""HTTP API for company-owned Payroll Setup policy and branch authority."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.service import _check_branch_access, _check_permission, _require_not_driver_role
from app.dependencies import get_current_user, get_db

from . import policy, reads
from .chronology import Schedule
from .errors import PolicyError
from .schemas import (
    AssignmentCreateRequest,
    AssignmentResponse,
    BranchHistoryResponse,
    DefaultSetupRequest,
    DefaultSetupResponse,
    DraftCreateRequest,
    DraftResponse,
    DraftUpdateRequest,
    EffectiveAuthorityResponse,
    PublicationImpactRequest,
    PublicationImpactResponse,
    PublishRequest,
    ReassignmentImpactRequest,
    ReassignmentImpactResponse,
    ReassignmentRequest,
    SetupCreateRequest,
    SetupResponse,
    SetupUpdateRequest,
    VersionResponse,
    WithdrawalRequest,
)
from .security import require_policy_permission

router = APIRouter()
TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep = Annotated[AsyncConnection, Depends(get_db)]


def _setup_response(row: dict) -> SetupResponse:
    return SetupResponse(
        setup_id=row.get("setup_id", row.get("payrollsetupid")),
        setup_code=row.get("setup_code", row.get("setupcode")),
        setup_name=row.get("setup_name", row.get("setupname")),
        description=row.get("description"), status=row.get("status"),
    )


def _version_response(row: dict) -> VersionResponse:
    return VersionResponse.model_validate(row)


async def _branch_read_access(company_id: int, user_id: int, branch_id: int,
                              db: AsyncConnection) -> None:
    companywide, branch_ids = await _check_branch_access(company_id, user_id, db)
    if not companywide and branch_id not in branch_ids:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Branch is outside the user's access scope.",
        )
    await _require_not_driver_role(company_id, user_id, db)
    await _check_permission(company_id, user_id, branch_id, "payroll.view", db)


@router.get("/setups", response_model=list[SetupResponse])
async def get_setups(token: TokenDep, db: DbDep):
    rows = await policy.list_setups(int(token["cid"]), int(token["sub"]), db)
    return [_setup_response(row) for row in rows]


@router.post("/setups", response_model=SetupResponse, status_code=201)
async def post_setup(body: SetupCreateRequest, token: TokenDep, db: DbDep):
    setup_id = await policy.create_setup(
        int(token["cid"]), int(token["sub"]), body.setup_code, body.setup_name,
        db, description=body.description,
    )
    return _setup_response(await reads.get_setup(int(token["cid"]), setup_id, db))


@router.get("/setups/{setup_id}", response_model=SetupResponse)
async def get_setup(setup_id: int, token: TokenDep, db: DbDep):
    row = await policy.get_setup(int(token["cid"]), int(token["sub"]), setup_id, db)
    return _setup_response(row)


@router.put("/setups/{setup_id}", response_model=SetupResponse)
async def put_setup(setup_id: int, body: SetupUpdateRequest, token: TokenDep, db: DbDep):
    await policy.update_setup_metadata(
        int(token["cid"]), int(token["sub"]), setup_id, body.setup_name,
        db, description=body.description,
    )
    return _setup_response(await reads.get_setup(int(token["cid"]), setup_id, db))


@router.post("/setups/{setup_id}/archive", status_code=204)
async def post_archive_setup(setup_id: int, token: TokenDep, db: DbDep):
    await policy.archive_setup(int(token["cid"]), int(token["sub"]), setup_id, db)
    return Response(status_code=204)


@router.get("/setups/{setup_id}/drafts", response_model=list[DraftResponse])
async def get_drafts(setup_id: int, token: TokenDep, db: DbDep):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    return await reads.list_drafts(company_id, setup_id, db)


@router.post("/setups/{setup_id}/drafts", response_model=DraftResponse, status_code=201)
async def post_draft(setup_id: int, body: DraftCreateRequest, token: TokenDep, db: DbDep):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    draft_id = await policy.create_draft(
        company_id, user_id, setup_id, db,
        payroll_frequency=body.payroll_frequency,
        anchor_start_date=body.anchor_start_date,
        custom_interval_days=body.custom_interval_days,
        normal_days_off_mask=body.normal_days_off_mask,
    )
    return await reads.get_draft(company_id, setup_id, draft_id, db)


@router.put("/setups/{setup_id}/drafts/{draft_id}", response_model=DraftResponse)
async def put_draft(
    setup_id: int, draft_id: int, body: DraftUpdateRequest, token: TokenDep, db: DbDep,
):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    await require_policy_permission(company_id, user_id, "payroll_setup.manage", db)
    await reads.get_draft(company_id, setup_id, draft_id, db)
    await policy.edit_draft(
        company_id, user_id, setup_id, draft_id, db,
        payroll_frequency=body.payroll_frequency,
        anchor_start_date=body.anchor_start_date,
        custom_interval_days=body.custom_interval_days,
        normal_days_off_mask=body.normal_days_off_mask,
    )
    return await reads.get_draft(company_id, setup_id, draft_id, db)


@router.delete("/setups/{setup_id}/drafts/{draft_id}", status_code=204)
async def delete_draft(setup_id: int, draft_id: int, token: TokenDep, db: DbDep):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    await require_policy_permission(company_id, user_id, "payroll_setup.manage", db)
    await reads.get_draft(company_id, setup_id, draft_id, db)
    await policy.discard_draft(company_id, user_id, setup_id, draft_id, db)
    return Response(status_code=204)


@router.get("/setups/{setup_id}/versions", response_model=list[VersionResponse])
async def get_versions(setup_id: int, token: TokenDep, db: DbDep):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    return [_version_response(row) for row in await reads.list_versions(company_id, setup_id, db)]


@router.post(
    "/setups/{setup_id}/drafts/{draft_id}/publication-impact",
    response_model=PublicationImpactResponse,
)
async def post_publication_impact(
    setup_id: int, draft_id: int, body: PublicationImpactRequest,
    token: TokenDep, db: DbDep,
):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    schedule_data = await reads.get_publication_schedule(company_id, setup_id, draft_id, db)
    schedule = Schedule(
        schedule_data["frequency"], schedule_data["anchor_start_date"],
        schedule_data["custom_interval_days"], schedule_data["normal_days_off_mask"],
    )
    impact = await policy.preview_policy_impact(
        company_id, user_id, setup_id, body.effective_from_date, schedule, db,
        replaces_version_id=body.replaces_version_id,
    )
    return {
        **impact,
        "successor_schedule": {
            "payroll_frequency": impact["successor_schedule"]["frequency"],
            "anchor_start_date": impact["successor_schedule"]["anchor_start_date"],
            "custom_interval_days": impact["successor_schedule"]["custom_interval_days"],
            "normal_days_off_mask": impact["successor_schedule"]["normal_days_off_mask"],
        },
    }


@router.post(
    "/setups/{setup_id}/drafts/{draft_id}/publish",
    response_model=VersionResponse,
    status_code=201,
)
async def post_publish(
    setup_id: int, draft_id: int, body: PublishRequest, token: TokenDep, db: DbDep,
):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    await require_policy_permission(company_id, user_id, "payroll_setup.publish", db)
    await reads.get_draft(company_id, setup_id, draft_id, db)
    version_id = await policy.publish_version(
        company_id, user_id, setup_id, draft_id, body.effective_from_date, db,
        replaces_version_id=body.replaces_version_id,
    )
    versions = await reads.list_versions(company_id, setup_id, db)
    version = next((row for row in versions if row["version_id"] == version_id), None)
    if version is None:
        raise PolicyError("VERSION_NOT_FOUND", "Published Version was not found")
    return _version_response(version)


@router.get("/default", response_model=DefaultSetupResponse)
async def get_default(token: TokenDep, db: DbDep):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    setup = await reads.get_default_setup(company_id, db)
    return {"setup": _setup_response(setup) if setup else None}


@router.put("/default", status_code=204)
async def put_default(body: DefaultSetupRequest, token: TokenDep, db: DbDep):
    await policy.set_default_setup(
        int(token["cid"]), int(token["sub"]), body.setup_id, db,
    )
    return Response(status_code=204)


@router.get("/branches/{branch_id}/assignments", response_model=list[AssignmentResponse])
async def get_assignments(branch_id: int, token: TokenDep, db: DbDep):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    await require_policy_permission(company_id, user_id, "payroll_setup.view", db)
    return await reads.list_assignments(company_id, branch_id, db)


@router.post(
    "/branches/{branch_id}/assignments", response_model=AssignmentResponse, status_code=201,
)
async def post_assignment(
    branch_id: int, body: AssignmentCreateRequest, token: TokenDep, db: DbDep,
):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    assignment_id = await policy.assign_setup(
        company_id, user_id, branch_id, body.setup_id, body.effective_from_date,
        db, reason=body.reason,
    )
    return await reads.get_assignment(company_id, assignment_id, db)


@router.post(
    "/branches/{branch_id}/reassignments",
    response_model=AssignmentResponse,
    status_code=201,
)
async def post_reassignment(
    branch_id: int, body: ReassignmentRequest, token: TokenDep, db: DbDep,
):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    assignment_id = await policy.reassign_setup(
        company_id, user_id, branch_id, body.destination_setup_id,
        body.effective_from_date, db, reason=body.reason,
    )
    return await reads.get_assignment(company_id, assignment_id, db)


@router.post(
    "/branches/{branch_id}/reassignment-impact",
    response_model=ReassignmentImpactResponse,
)
async def post_reassignment_impact(
    branch_id: int, body: ReassignmentImpactRequest, token: TokenDep, db: DbDep,
):
    return await policy.preview_reassignment_impact(
        int(token["cid"]), int(token["sub"]), branch_id,
        body.destination_setup_id, body.effective_from_date, db,
    )


@router.post("/assignments/{assignment_id}/withdraw", status_code=204)
async def post_withdrawal(
    assignment_id: int, token: TokenDep, db: DbDep,
    body: WithdrawalRequest = Body(default=WithdrawalRequest()),
):
    await policy.withdraw_assignment(
        int(token["cid"]), int(token["sub"]), assignment_id, db,
        reason=body.reason,
    )
    return Response(status_code=204)


@router.get("/branches/{branch_id}/history", response_model=BranchHistoryResponse)
async def get_branch_history(branch_id: int, token: TokenDep, db: DbDep):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    await _branch_read_access(company_id, user_id, branch_id, db)
    return await reads.get_branch_history(company_id, branch_id, db)


@router.get(
    "/branches/{branch_id}/effective", response_model=EffectiveAuthorityResponse,
)
async def get_effective_authority(
    branch_id: int, token: TokenDep, db: DbDep,
    period_start_date: date = Query(...),
):
    company_id, user_id = int(token["cid"]), int(token["sub"])
    await _branch_read_access(company_id, user_id, branch_id, db)
    return await reads.get_effective_authority(
        company_id, branch_id, period_start_date, db,
    )
