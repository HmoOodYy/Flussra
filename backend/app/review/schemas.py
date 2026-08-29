"""
Pydantic schemas for the review domain — manager review items and decisions.
"""
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Constants — mirror the CHECK constraints in the schema
# ---------------------------------------------------------------------------

_VALID_REQUEST_TYPES = {
    "DriverRateChange", "PayrollDraftChange", "PayrollAdjustment",
    "ImportConfirmation", "Correction", "PeriodApproval", "Override", "Other",
}
_VALID_PRIORITIES = {"Low", "Normal", "High", "Urgent"}
_VALID_STATUSES = {"Pending", "Approved", "Rejected", "EditRequested", "Cancelled"}
_VALID_DECISIONS = {"Approved", "Rejected", "EditRequested", "Comment"}

# Only items in these statuses may receive a new decision.
_DECIDABLE_STATUSES = {"Pending", "EditRequested"}


# ---------------------------------------------------------------------------
# Review decision (child record — one or many per item)
# ---------------------------------------------------------------------------

class ReviewDecisionSummary(BaseModel):
    review_decision_id: int
    review_item_id: int
    decided_by_user_id: int
    decided_by: str | None = None       # display name (joined at read time)
    decision: str
    decision_reason: str | None = None
    created_at_utc: datetime


# ---------------------------------------------------------------------------
# Review item summary (used for list responses)
# ---------------------------------------------------------------------------

class ReviewItemSummary(BaseModel):
    review_item_id: int
    company_id: int
    branch_id: int
    branch_name: str | None = None
    requested_by_user_id: int | None = None
    requested_by: str | None = None     # display name
    request_type: str
    entity_schema: str | None = None
    entity_name: str | None = None
    entity_id: str | None = None
    title: str
    description: str | None = None
    status: str
    priority: str
    created_at_utc: datetime
    due_at_utc: datetime | None = None
    final_decision_by_user_id: int | None = None
    final_decision_by: str | None = None    # display name
    final_decision_at_utc: datetime | None = None
    final_decision_reason: str | None = None


# ---------------------------------------------------------------------------
# Review item detail (single-item response — includes payload and decisions)
# ---------------------------------------------------------------------------

class ReviewItemDetail(ReviewItemSummary):
    old_value_json: str | None = None
    new_value_json: str | None = None
    decisions: list[ReviewDecisionSummary] = []


# ---------------------------------------------------------------------------
# Immutable submitted payroll packet (CP-4E)
# ---------------------------------------------------------------------------

class ReviewPayrollSnapshotDriverTotal(BaseModel):
    driver_id: int
    driver_code_snapshot: str | None = None
    driver_name_snapshot: str | None = None
    daily_pay: Decimal
    status_pay: Decimal
    period_pay: Decimal
    minimum_adjustment: Decimal
    maximum_adjustment: Decimal
    bonus_total: Decimal
    expected_pay: Decimal


class ReviewPayrollSnapshotLine(BaseModel):
    driver_id: int
    source_type: str
    line_type: str
    line_scope: str | None = None
    work_date: date | None = None
    pay_item_id: int | None = None
    quantity: Decimal | None = None
    resolved_rate_amount: Decimal | None = None
    calculated_amount: Decimal


class ReviewPayrollSnapshot(BaseModel):
    review_item_id: int
    payroll_period_id: int
    revision_number: int
    captured_at_utc: datetime
    total_expected_pay: Decimal
    driver_totals: list[ReviewPayrollSnapshotDriverTotal]
    lines: list[ReviewPayrollSnapshotLine]


# ---------------------------------------------------------------------------
# Review item creation
# ---------------------------------------------------------------------------

class ReviewItemCreate(BaseModel):
    branch_id: int
    request_type: str
    title: str
    description: str | None = None
    entity_schema: str | None = None
    entity_name: str | None = None
    entity_id: str | None = None
    old_value_json: str | None = None
    new_value_json: str | None = None
    priority: str = "Normal"
    due_at_utc: datetime | None = None

    @field_validator("request_type")
    @classmethod
    def request_type_valid(cls, v: str) -> str:
        if v not in _VALID_REQUEST_TYPES:
            raise ValueError(
                f"request_type must be one of {sorted(_VALID_REQUEST_TYPES)}"
            )
        return v

    @field_validator("priority")
    @classmethod
    def priority_valid(cls, v: str) -> str:
        if v not in _VALID_PRIORITIES:
            raise ValueError(
                f"priority must be one of {sorted(_VALID_PRIORITIES)}"
            )
        return v

    @field_validator("title")
    @classmethod
    def title_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title must not be blank")
        return v


# ---------------------------------------------------------------------------
# Decision payload
# ---------------------------------------------------------------------------

class ReviewDecide(BaseModel):
    decision: str
    decision_reason: str | None = None

    @field_validator("decision")
    @classmethod
    def decision_valid(cls, v: str) -> str:
        if v not in _VALID_DECISIONS:
            raise ValueError(
                f"decision must be one of {sorted(_VALID_DECISIONS)}"
            )
        return v
