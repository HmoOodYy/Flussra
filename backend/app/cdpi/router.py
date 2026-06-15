"""
CDPI (Custom Daily Pay Item) -- HTTP router.

Mounted at /settings/cdpi via main.py.

Routes:
  POST   /requests                    -- create a new Draft
  GET    /requests                    -- list requests visible to the caller
  GET    /requests/{id}               -- read a single request
  PATCH  /requests/{id}               -- update a Draft (optimistic concurrency)
  POST   /requests/{id}/submit        -- submit Draft -> PendingCompanyApproval
  POST   /requests/{id}/decide        -- return or reject a Pending request
  POST   /requests/{id}/copy          -- copy a Rejected request to a new Draft
"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_db, get_current_user
from app.cdpi import service
from app.cdpi.schemas import (
    CdpiRequestCreate,
    CdpiRequestSummary,
    CdpiRequestUpdate,
    CdpiSubmitRequest,
    CdpiDecideRequest,
)

router = APIRouter()

TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep    = Annotated[object, Depends(get_db)]


@router.post(
    "/requests",
    response_model=CdpiRequestSummary,
    status_code=201,
    summary="Create CDPI Draft",
)
async def create_draft(
    token: TokenDep,
    db:    DbDep,
    body:  CdpiRequestCreate,
) -> CdpiRequestSummary:
    company_id: int = token["cid"]
    user_id:    int = token["sub"]
    return await service.create_draft(company_id, user_id, body, db)


@router.get(
    "/requests",
    response_model=list[CdpiRequestSummary],
    summary="List CDPI requests",
)
async def list_requests(
    token:         TokenDep,
    db:            DbDep,
    status_filter: str | None = Query(None, alias="status"),
    branch_id:     int | None = Query(None),
) -> list[CdpiRequestSummary]:
    company_id: int = token["cid"]
    user_id:    int = token["sub"]
    return await service.list_requests(
        company_id,
        user_id,
        db,
        status_filter=status_filter,
        branch_id=branch_id,
    )


@router.get(
    "/requests/{request_id}",
    response_model=CdpiRequestSummary,
    summary="Get CDPI request",
)
async def get_request(
    token:      TokenDep,
    db:         DbDep,
    request_id: UUID,
) -> CdpiRequestSummary:
    company_id: int = token["cid"]
    user_id:    int = token["sub"]
    return await service.get_request(company_id, user_id, request_id, db)


@router.patch(
    "/requests/{request_id}",
    response_model=CdpiRequestSummary,
    summary="Update CDPI Draft",
)
async def update_draft(
    token:      TokenDep,
    db:         DbDep,
    request_id: UUID,
    body:       CdpiRequestUpdate,
) -> CdpiRequestSummary:
    company_id: int = token["cid"]
    user_id:    int = token["sub"]
    return await service.update_draft(company_id, user_id, request_id, body, db)


@router.post(
    "/requests/{request_id}/submit",
    response_model=CdpiRequestSummary,
    summary="Submit CDPI Draft",
)
async def submit_draft(
    token:      TokenDep,
    db:         DbDep,
    request_id: UUID,
    body:       CdpiSubmitRequest,
) -> CdpiRequestSummary:
    company_id: int = token["cid"]
    user_id:    int = token["sub"]
    return await service.submit_draft(company_id, user_id, request_id, body, db)


@router.post(
    "/requests/{request_id}/decide",
    response_model=CdpiRequestSummary,
    summary="Decide CDPI request (ReturnToDraft or Reject)",
)
async def decide_request(
    token:      TokenDep,
    db:         DbDep,
    request_id: UUID,
    body:       CdpiDecideRequest,
) -> CdpiRequestSummary:
    company_id: int = token["cid"]
    user_id:    int = token["sub"]
    return await service.decide_request(company_id, user_id, request_id, body, db)


@router.post(
    "/requests/{request_id}/copy",
    response_model=CdpiRequestSummary,
    status_code=201,
    summary="Copy Rejected CDPI request to new Draft",
)
async def copy_rejected(
    token:      TokenDep,
    db:         DbDep,
    request_id: UUID,
) -> CdpiRequestSummary:
    company_id: int = token["cid"]
    user_id:    int = token["sub"]
    return await service.copy_rejected(company_id, user_id, request_id, db)
