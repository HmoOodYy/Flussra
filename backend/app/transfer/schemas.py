"""Pydantic schemas for the Driver Transfer workflow."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class DriverTransferCreate(BaseModel):
    """Create a new transfer request.

    Initiated by either a Driver (ODA user) or a SourceBranch manager.
    When a driver initiates, ``initiated_by`` must be 'Driver'.
    When a branch manager initiates on behalf, ``initiated_by`` must be
    'SourceBranch'.
    """
    driver_id: int
    target_branch_id: int
    effective_date: date
    initiated_by: Literal["Driver", "SourceBranch"]
    reason: str | None = None
    notes: str | None = None


class SourceApprovalRequest(BaseModel):
    """Source-branch manager approves a PendingSourceApproval request."""
    notes: str | None = None


class TargetDecisionRequest(BaseModel):
    """Target-branch manager accepts or rejects a PendingTargetApproval request."""
    decision: Literal["Approved", "Rejected", "Returned"]
    decision_notes: str | None = None


class CancelRequest(BaseModel):
    cancel_reason: str | None = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class DriverTransferResponse(BaseModel):
    transfer_request_id: int
    company_id: int
    driver_id: int
    source_branch_id: int
    target_branch_id: int
    requested_by_user_id: int
    initiated_by: str
    status: str
    effective_date: date
    reason: str | None
    notes: str | None
    source_approved_by_user_id: int | None
    source_approved_at_utc: datetime | None
    target_decided_by_user_id: int | None
    target_decided_at_utc: datetime | None
    target_decision_notes: str | None
    new_driver_id: int | None
    completed_at_utc: datetime | None
    completed_by_user_id: int | None
    cancelled_at_utc: datetime | None
    cancelled_by_user_id: int | None
    cancel_reason: str | None
    created_at_utc: datetime
    updated_at_utc: datetime | None

    # Denormalized for display
    driver_name: str | None = None
    source_branch_name: str | None = None
    target_branch_name: str | None = None
    requested_by_name: str | None = None

    model_config = {"from_attributes": True}


class TransferListResponse(BaseModel):
    items: list[DriverTransferResponse]
    total: int
