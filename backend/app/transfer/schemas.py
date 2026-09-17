"""Pydantic schemas for the Driver Transfer workflow."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

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
    reason: Optional[str] = None
    notes: Optional[str] = None


class SourceApprovalRequest(BaseModel):
    """Source-branch manager approves a PendingSourceApproval request."""
    notes: Optional[str] = None


class TargetDecisionRequest(BaseModel):
    """Target-branch manager accepts or rejects a PendingTargetApproval request."""
    decision: Literal["Approved", "Rejected", "Returned"]
    decision_notes: Optional[str] = None


class CancelRequest(BaseModel):
    cancel_reason: Optional[str] = None


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
    reason: Optional[str]
    notes: Optional[str]
    source_approved_by_user_id: Optional[int]
    source_approved_at_utc: Optional[datetime]
    target_decided_by_user_id: Optional[int]
    target_decided_at_utc: Optional[datetime]
    target_decision_notes: Optional[str]
    new_driver_id: Optional[int]
    completed_at_utc: Optional[datetime]
    completed_by_user_id: Optional[int]
    cancelled_at_utc: Optional[datetime]
    cancelled_by_user_id: Optional[int]
    cancel_reason: Optional[str]
    created_at_utc: datetime
    updated_at_utc: Optional[datetime]

    # Denormalized for display
    driver_name: Optional[str] = None
    source_branch_name: Optional[str] = None
    target_branch_name: Optional[str] = None
    requested_by_name: Optional[str] = None

    model_config = {"from_attributes": True}


class TransferListResponse(BaseModel):
    items: list[DriverTransferResponse]
    total: int
