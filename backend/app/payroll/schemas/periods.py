"""Payroll period lifecycle, candidate creation, workflow, and Current Payroll contracts."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

# Compatibility metadata: Pydantic models retain the legacy app.payroll.schemas identity.


# ---------------------------------------------------------------------------
# Period summary (used for list and single-period responses)
# ---------------------------------------------------------------------------

class PeriodSummary(BaseModel):
    __module__ = "app.payroll.schemas"
    payroll_period_id: int
    branch_id: int
    branch_name: str
    parent_period_id: int | None = None
    period_code: str
    period_name: str
    period_type: str          # Week | Biweek | Month | Custom
    start_date: date
    end_date: date
    pay_date: date | None = None
    status: str               # Draft | Open | InReview | Returned | Approved | Locked | Cancelled | Archived
    notes: str | None = None
    created_by_user_id: int | None = None
    created_at_utc: datetime
    # CP-1A: set when status='Returned'; cleared on resubmission.
    current_return_review_item_id: int | None = None
    # Aggregate counts from vw_PayrollPeriodList
    draft_drivers: int = 0
    draft_lines: int = 0
    draft_lines_needing_attention: int = 0
    final_lines: int = 0
    # Ledger aggregates (vw_PayrollPeriodList 0024 — populated only for Locked/Archived)
    final_gross: Decimal = Decimal("0")
    final_driver_count: int = 0



# ---------------------------------------------------------------------------
# Status change
# ---------------------------------------------------------------------------

# Transitions that can be performed via PATCH /periods/{id}/status.
# Approved → Locked is intentionally excluded here — that path goes through
# POST /periods/{id}/finalize. Financial transitions remain subject to the evidence gate.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "Draft":    {"Cancelled"},
    "Open":     {"InReview", "Cancelled"},
    # CP-1A: InReview has no PATCH exits.
    #   Approved    → period Approved  (via POST /review/items/{id}/decide)
    #   Rejected    → period Returned  (via POST /review/items/{id}/decide + reason)
    #   EditRequested → period Returned (via POST /review/items/{id}/decide + reason)
    # Manual InReview→Open and InReview→Cancelled are both blocked.
    "InReview": set(),
    # CP-1A: Returned has no PATCH exits.
    #   Resubmit via POST /payroll/periods/{id}/resubmissions → InReview.
    #   Direct PATCH to Returned is also forbidden (see _PATCH_RESERVED_STATUSES).
    "Returned": set(),
    # CP-1A: Approved has no PATCH exits.
    #   Approved → Locked goes through POST /periods/{id}/finalize (not PATCH).
    #   Approved → Cancelled is blocked — only Draft and Open can be cancelled.
    "Approved": set(),
    "Locked":   {"Archived"},
    "Cancelled": set(),
    "Archived":  set(),
}


_ALL_STATUSES = set(_VALID_TRANSITIONS.keys())


# "Locked" and "Returned" must NEVER be set via PATCH /status:
#   Locked   — reachable only through POST /periods/{id}/finalize.
#   Returned — reachable only through the review decision flow (POST /review/items/{id}/decide).
_PATCH_RESERVED_STATUSES = {"Locked", "Returned"}



class PeriodEntryCount(BaseModel):
    """
    Draft payroll data summary for a period — used to gate cancellation warnings.
    """
    __module__ = "app.payroll.schemas"
    period_id: int
    driver_count: int    # distinct drivers with any non-voided draft lines
    entry_count: int     # total non-voided draft lines
    has_data: bool       # True when entry_count > 0



class PeriodStatusChange(BaseModel):
    __module__ = "app.payroll.schemas"
    status: str
    notes: str | None = None

    @field_validator("status")
    @classmethod
    def status_valid(cls, v: str) -> str:
        if v not in _ALL_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_ALL_STATUSES)}"
            )
        if v in _PATCH_RESERVED_STATUSES:
            raise ValueError(
                f"'{v}' cannot be set via this endpoint. "
                f"Use POST /payroll/periods/{{id}}/finalize to transition a period to Locked."
            )
        return v



# ---------------------------------------------------------------------------
# CP-1C: Branch-locked candidate-based period creation schemas
# No PayDate field anywhere in these schemas.
# ---------------------------------------------------------------------------

class CandidateSelectedInfo(BaseModel):
    """Info about the currently selected/previewed candidate period."""
    __module__ = "app.payroll.schemas"
    candidate_key: str
    target_status: str      # "Open" | "Draft"
    start_date: date
    end_date: date
    period_type: str
    label: str
    creatable: bool
    blocked_reason: str | None = None



class CandidateNavigationInfo(BaseModel):
    """Signed cursors for navigating to adjacent candidates (future periods only)."""
    __module__ = "app.payroll.schemas"
    previous_cursor: str | None = None
    next_cursor: str | None = None



class CandidatePreviewResponse(BaseModel):
    """Full response for GET /payroll/branches/{branch_id}/period-candidates."""
    __module__ = "app.payroll.schemas"
    mode: str               # "OPEN_CREATION" | "PREPARED_CREATION"
    selected: CandidateSelectedInfo
    navigation: CandidateNavigationInfo



class PeriodCreationRequest(BaseModel):
    """Body for POST /payroll/branches/{branch_id}/period-creations.

    extra="forbid" ensures stray fields (start_date, status, pay_date, etc.)
    are rejected with HTTP 422 rather than silently ignored.
    """
    __module__ = "app.payroll.schemas"
    model_config = ConfigDict(extra="forbid")

    candidate_key: str



class PeriodCreationResponse(BaseModel):
    """Response for POST /payroll/branches/{branch_id}/period-creations."""
    __module__ = "app.payroll.schemas"
    result: str             # "CREATED" | "ALREADY_EXISTS"
    payroll_period_id: int
    branch_id: int
    period_code: str
    period_name: str
    period_type: str
    start_date: date
    end_date: date
    status: str
    created_at_utc: datetime | None = None



# ===========================================================================
# CP-1E: Current Workflow — hub-ready workflow slots, alerts, capabilities
# ===========================================================================

class WorkflowCapability(BaseModel):
    __module__ = "app.payroll.schemas"
    allowed: bool
    reason_code: str | None = None
    reason_message: str | None = None



class WorkflowSlotItem(BaseModel):
    __module__ = "app.payroll.schemas"
    period_id: int
    branch_id: int
    branch_name: str
    status: str                         # actual DB status
    display_status: str                 # Draft → "Prepared"; others unchanged
    period_name: str
    period_code: str
    period_type: str
    start_date: date
    end_date: date
    submitted_at_utc: datetime | None = None
    current_return_review_item_id: int | None = None
    is_active_workflow_slot: bool
    is_read_only: bool
    read_only_reason_code: str | None = None
    lifecycle_position: int             # Returned=1, Open=2, Draft/Prepared=3, InReview=4



class WorkflowBranchSlots(BaseModel):
    __module__ = "app.payroll.schemas"
    open: WorkflowSlotItem | None = None
    prepared: WorkflowSlotItem | None = None    # Draft periods surface here
    in_review: WorkflowSlotItem | None = None
    returned: WorkflowSlotItem | None = None



class WorkflowAlert(BaseModel):
    __module__ = "app.payroll.schemas"
    code: str
    severity: str                       # blocker | warning | info
    title: str
    message: str
    related_period_id: int | None = None
    affected_action_codes: list[str] = []



class PeriodWorkflowCapabilities(BaseModel):
    __module__ = "app.payroll.schemas"
    can_enter_source: WorkflowCapability
    can_submit_for_review: WorkflowCapability
    can_resubmit_returned: WorkflowCapability
    can_view_review: WorkflowCapability
    can_cancel: WorkflowCapability
    can_open_day_grid: WorkflowCapability



class BranchWorkflowCapabilities(BaseModel):
    __module__ = "app.payroll.schemas"
    can_view_current_workflow: WorkflowCapability
    can_create_open_candidate: WorkflowCapability
    can_create_prepared_candidate: WorkflowCapability
    can_view_candidates: WorkflowCapability
    periods: dict[str, PeriodWorkflowCapabilities] = {}  # keyed by str(period_id)



class BranchWorkflowEntry(BaseModel):
    __module__ = "app.payroll.schemas"
    branch_id: int
    branch_name: str
    setup_status: str                   # "complete" | "missing" | "incomplete" | "inactive"
    slots: WorkflowBranchSlots
    capabilities: BranchWorkflowCapabilities
    alerts: list[WorkflowAlert] = []



class CurrentWorkflowResponse(BaseModel):
    __module__ = "app.payroll.schemas"
    scope: str                          # "company" | "branch"
    company_id: int
    requested_branch_id: int | None = None
    branches: list[BranchWorkflowEntry] = []



# ---------------------------------------------------------------------------
# CP-5A: Current Payroll Hub
# ---------------------------------------------------------------------------

class CurrentPayrollHubMetrics(BaseModel):
    """Operational, period-scoped driver counts for one active workflow slot."""
    __module__ = "app.payroll.schemas"
    total_eligible_drivers: int
    working_drivers: int
    fully_off_drivers: int



class CurrentPayrollHubDriverSummary(BaseModel):
    """A compact, authority-backed expected-income driver summary."""
    __module__ = "app.payroll.schemas"
    driver_id: int
    driver_code: str | None = None
    driver_name: str | None = None
    expected_pay: Decimal



class CurrentPayrollHubFinancialSummary(BaseModel):
    """Compact CP-4B live calculation data for an Open or Returned slot only."""
    __module__ = "app.payroll.schemas"
    authority_kind: str = "LIVE"
    total_expected_pay: Decimal
    normal_pay: Decimal
    bonus_total: Decimal
    system_adjustments: Decimal
    has_blockers: bool
    blockers: list[str] = []
    warnings: list[str] = []
    top_drivers: list[CurrentPayrollHubDriverSummary] = []



class CurrentPayrollHubPeriodSlot(WorkflowSlotItem):
    """Workflow slot metadata plus only the financial authority valid for its state."""
    __module__ = "app.payroll.schemas"
    financials_available: bool
    financial_summary: CurrentPayrollHubFinancialSummary | None = None
    metrics: CurrentPayrollHubMetrics



class CurrentPayrollHubSlots(BaseModel):
    __module__ = "app.payroll.schemas"
    open: CurrentPayrollHubPeriodSlot | None = None
    prepared: CurrentPayrollHubPeriodSlot | None = None
    in_review: CurrentPayrollHubPeriodSlot | None = None
    returned: CurrentPayrollHubPeriodSlot | None = None



class CurrentPayrollHubBranch(BaseModel):
    __module__ = "app.payroll.schemas"
    branch_id: int
    branch_name: str
    setup_status: str
    slots: CurrentPayrollHubSlots
    capabilities: BranchWorkflowCapabilities
    alerts: list[WorkflowAlert] = []



class CurrentPayrollHubResponse(BaseModel):
    __module__ = "app.payroll.schemas"
    scope: str
    company_id: int
    requested_branch_id: int | None = None
    generated_at_utc: datetime
    branches: list[CurrentPayrollHubBranch] = []
