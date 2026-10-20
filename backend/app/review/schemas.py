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

class ReviewPayrollSnapshotComponent(BaseModel):
    """One official amount of the frozen snapshot: rounded once from its pre-rounding sum."""
    component_kind: str
    label: str | None = None
    payroll_period_definition_id: int | None = None
    pay_definition_id: int | None = None
    rate_definition_id: int | None = None
    calculation_method: str | None = None
    calculation_method_version: int | None = None
    status_rate_column_id: int | None = None
    driver_pay_rule_id: int | None = None
    pre_rounding_amount: Decimal
    official_amount: Decimal
    source_count: int


class ReviewPayrollSnapshotMinMaxRule(BaseModel):
    driver_pay_rule_id: int
    rule_type: str
    amount: Decimal
    rule_status: str
    effective_from: date
    effective_to: date | None = None
    prorated_amount: Decimal | None = None
    calendar_frequency: str
    calendar_anchor_start_date: date
    calendar_custom_interval_days: int | None = None


class ReviewPayrollSnapshotMinMax(BaseModel):
    """The frozen Min/Max evaluation: enough to explain the adjustments on its own."""
    status: str
    selection_date: date
    proration_version: str
    numerator: int | None = None
    denominator: int | None = None
    base_amount: Decimal
    base_ordinary_amount: Decimal
    base_status_amount: Decimal
    excluded_bonus_amount: Decimal
    counted_work_dates: list[date] = []
    rules: list[ReviewPayrollSnapshotMinMaxRule] = []


class ReviewPayrollSnapshotDriverTotal(BaseModel):
    driver_assignment_id: int
    driver_id: int
    employee_id: int
    branch_id: int
    driver_number_snapshot: str | None = None
    driver_name_snapshot: str | None = None
    daily_pay: Decimal
    status_pay: Decimal
    minimum_adjustment: Decimal
    maximum_adjustment: Decimal
    bonus_total: Decimal
    expected_pay: Decimal
    components: list[ReviewPayrollSnapshotComponent] = []
    min_max: ReviewPayrollSnapshotMinMax | None = None


class ReviewPayrollSnapshotLine(BaseModel):
    driver_assignment_id: int
    source_type: str
    source_id: str
    # ORDINARY / STATUS / BONUS: the kind of the component the line contributes to.
    line_type: str
    line_scope: str | None = None
    work_date: date | None = None
    payroll_period_definition_id: int | None = None
    driver_rate_schedule_id: int | None = None
    quantity: Decimal | None = None
    resolved_rate_amount: Decimal | None = None
    calculated_amount: Decimal


class ReviewPayrollSnapshot(BaseModel):
    currency_code: str
    currency_minor_unit_digits: int
    review_item_id: int
    payroll_period_id: int
    snapshot_id: int
    revision_number: int
    snapshot_hash: str
    calculation_version: str
    payable_rounding_version: str
    min_max_proration_version: str
    period_start_date: date
    period_end_date: date
    scheduled_work_dates: list[date] = []
    warnings: list[str] = []
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
