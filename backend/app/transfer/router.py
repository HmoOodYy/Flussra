"""FastAPI router for the Driver Transfer workflow."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncConnection

from app.dependencies import get_current_user, get_db
from app.transfer import service
from app.transfer.schemas import (
    CancelRequest,
    DriverTransferCreate,
    DriverTransferResponse,
    SourceApprovalRequest,
    TargetDecisionRequest,
    TransferListResponse,
)

router = APIRouter()

TokenDep = Annotated[dict, Depends(get_current_user)]
DbDep    = Annotated[AsyncConnection, Depends(get_db)]


@router.post("", response_model=DriverTransferResponse, status_code=201)
async def create_transfer(
    data: DriverTransferCreate,
    token: TokenDep,
    db: DbDep,
) -> DriverTransferResponse:
    """Create a new driver transfer request."""
    return await service.create_driver_transfer_request(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        data=data,
        db=db,
    )


@router.get("", response_model=TransferListResponse)
async def list_transfers(
    token: TokenDep,
    db: DbDep,
    branch_id: int | None = None,
    status: str | None = None,
) -> TransferListResponse:
    """List driver transfer requests visible to the caller."""
    return await service.list_transfer_requests(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        db=db,
        branch_id=branch_id,
        status_filter=status,
    )


@router.get("/{transfer_request_id}", response_model=DriverTransferResponse)
async def get_transfer(
    transfer_request_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverTransferResponse:
    """Get a single transfer request by ID."""
    return await service.get_transfer_request(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        transfer_request_id=transfer_request_id,
        db=db,
    )


@router.post("/{transfer_request_id}/approve-source", response_model=DriverTransferResponse)
async def approve_source(
    transfer_request_id: int,
    data: SourceApprovalRequest,
    token: TokenDep,
    db: DbDep,
) -> DriverTransferResponse:
    """Source-branch manager approves a PendingSourceApproval request."""
    return await service.approve_source_transfer(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        transfer_request_id=transfer_request_id,
        data=data,
        db=db,
    )


@router.post("/{transfer_request_id}/decide-target", response_model=DriverTransferResponse)
async def decide_target(
    transfer_request_id: int,
    data: TargetDecisionRequest,
    token: TokenDep,
    db: DbDep,
) -> DriverTransferResponse:
    """Target-branch manager approves, rejects, or returns the request."""
    return await service.decide_target_transfer(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        transfer_request_id=transfer_request_id,
        data=data,
        db=db,
    )


@router.post("/{transfer_request_id}/complete", response_model=DriverTransferResponse)
async def complete_transfer(
    transfer_request_id: int,
    token: TokenDep,
    db: DbDep,
) -> DriverTransferResponse:
    """
    Complete an Approved transfer: creates new driver profile in target branch,
    closes old profile, updates employee branch.
    """
    return await service.complete_driver_transfer(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        transfer_request_id=transfer_request_id,
        db=db,
    )


@router.post("/{transfer_request_id}/cancel", response_model=DriverTransferResponse)
async def cancel_transfer(
    transfer_request_id: int,
    data: CancelRequest,
    token: TokenDep,
    db: DbDep,
) -> DriverTransferResponse:
    """Cancel an in-flight transfer request."""
    return await service.cancel_driver_transfer(
        company_id=int(token["cid"]),
        user_id=int(token["sub"]),
        transfer_request_id=transfer_request_id,
        data=data,
        db=db,
    )
