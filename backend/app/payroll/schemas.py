"""
Pydantic schemas for the payroll domain (periods, status changes, draft lines,
pay rates / driver rate matrix).
"""
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


# ---------------------------------------------------------------------------
# Period summary (used for list and single-period responses)
# ---------------------------------------------------------------------------

class PeriodSummary(BaseModel):
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
    status: str               # Draft | Open | InReview | Approved | Locked | Cancelled | Archived
    notes: str | None = None
    created_by_user_id: int | None = None
    created_at_utc: datetime
    # Aggregate counts from vw_PayrollPeriodList
    draft_drivers: int = 0
    draft_lines: int = 0
    draft_lines_needing_attention: int = 0
    final_lines: int = 0
    # Ledger aggregates (vw_PayrollPeriodList 0024 — populated only for Locked/Archived)
    final_gross: Decimal = Decimal("0")
    final_driver_count: int = 0


# ---------------------------------------------------------------------------
# Period creation
# ---------------------------------------------------------------------------

_VALID_PERIOD_TYPES = {"Week", "Biweek", "Month", "Custom"}


class PeriodCreate(BaseModel):
    branch_id: int
    period_type: str = "Week"
    start_date: date
    end_date: date
    pay_date: date | None = None
    period_name: str | None = None   # auto-generated from dates if omitted
    notes: str | None = None

    @field_validator("period_type")
    @classmethod
    def period_type_valid(cls, v: str) -> str:
        if v not in _VALID_PERIOD_TYPES:
            raise ValueError(
                f"period_type must be one of {sorted(_VALID_PERIOD_TYPES)}"
            )
        return v

    @model_validator(mode="after")
    def end_after_start(self) -> "PeriodCreate":
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be strictly after start_date")
        return self


# ---------------------------------------------------------------------------
# Status change
# ---------------------------------------------------------------------------

# Transitions that can be performed via PATCH /periods/{id}/status.
# Approved → Locked is intentionally excluded here — that path goes through
# POST /periods/{id}/finalize (Milestone 5) which also creates PayrollFinalLines.
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "Draft":    {"Open", "Cancelled"},
    "Open":     {"InReview", "Cancelled"},
    # M16: InReview → Approved is removed from PATCH /status.
    # Approval now flows exclusively through the review decision system:
    #   POST /review/items/{id}/decide with decision='Approved'  → period Approved
    #   POST /review/items/{id}/decide with decision='Rejected'  → period Open
    #   POST /review/items/{id}/decide with decision='EditRequested' → period Open
    # InReview → Open is kept for manual return / payroll admin override.
    "InReview": {"Open", "Cancelled"},
    # CP-0C: "InReview" removed — Approved→InReview left a period in InReview
    # with no active Pending PeriodApproval item and no review path forward.
    # The only valid exit from Approved is Cancelled (admin) or Locked (finalize).
    "Approved": {"Cancelled"},
    "Locked":   {"Archived"},
    "Cancelled": set(),
    "Archived":  set(),
}

_ALL_STATUSES = set(_VALID_TRANSITIONS.keys())

# "Locked" is a valid DB status but must NEVER be set via PATCH /status —
# it is only reachable through POST /periods/{id}/finalize.
_PATCH_RESERVED_STATUSES = {"Locked"}


class NextPeriodDates(BaseModel):
    """
    Computed suggested dates for the next payroll period of a branch.

    For Custom frequency with a saved interval:
      is_custom=True, start_date/end_date are populated, custom_interval_days is set.

    For Custom frequency without a saved interval (setup incomplete):
      is_custom=True, start_date=None, end_date=None, custom_interval_days=None.

    For all other frequencies:
      is_custom=False, start_date/end_date populated, custom_interval_days=None.
    """
    branch_id: int
    period_type: str               # Week | Biweek | Month | Custom
    anchor_start_date: date        # from BranchPayrollSettings
    last_period_end_date: date | None   # MAX(end_date) of non-cancelled periods; None = first ever
    start_date: date | None        # None when Custom cadence is incomplete
    end_date: date | None          # None when Custom cadence is incomplete
    is_custom: bool
    custom_interval_days: int | None = None  # inclusive period length for Custom cadence


class PeriodEntryCount(BaseModel):
    """
    Draft payroll data summary for a period — used to gate cancellation warnings.
    """
    period_id: int
    driver_count: int    # distinct drivers with any non-voided draft lines
    entry_count: int     # total non-voided draft lines (daily + period-pay)
    has_data: bool       # True when entry_count > 0


class PeriodStatusChange(BaseModel):
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
# Draft line schemas
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# M13a note: _VALID_LINE_TYPES (the old hardcoded set) has been removed.
# Line-type validation is now done in the service layer using PayItems-driven
# logic.  See payroll/service.py — _validate_line_type() and _SYSTEM_LINE_TYPES.
# ---------------------------------------------------------------------------

_VALID_SOURCE_TYPES   = {"Manual", "Import", "System"}
_VALID_LINE_STATUSES  = {"Active", "NeedsReview", "Rejected", "Void"}

# Periods must be in one of these statuses to accept new/modified entries.
# CP-0A: Only Open periods accept source mutations.
# InReview and all later/terminal statuses are read-only for operational data.
ENTRY_ALLOWED_STATUSES = {"Open"}


class DraftLineSummary(BaseModel):
    draft_line_id: int
    period_id: int
    branch_id: int
    driver_id: int
    driver_name: str
    work_date: date | None = None
    line_type: str
    # M14: 'Daily' for daily draft lines; 'Period' for period-level pay lines.
    # Populated from the LineScope column added in migration 0010.
    line_scope: str = "Daily"
    quantity: Decimal
    rate_amount: Decimal | None = None
    calculated_amount: Decimal | None = None
    source_type: str
    status: str
    needs_manager_review: bool
    notes: str | None = None
    added_by_user_id: int | None = None
    added_at_utc: datetime


class DriverPeriodSummary(BaseModel):
    """Aggregated totals per driver × line_type for one period."""
    driver_id: int
    driver_name: str
    period_id: int
    period_name: str
    line_type: str
    total_quantity: Decimal
    total_calculated_amount: Decimal
    line_count: int
    lines_needing_attention: int


class DraftLineCreate(BaseModel):
    driver_id: int
    work_date: date | None = None
    line_type: str
    quantity: Decimal = Decimal("0")
    rate_amount: Decimal | None = None
    notes: str | None = None
    source_type: str = "Manual"
    needs_manager_review: bool = False

    @field_validator("line_type")
    @classmethod
    def line_type_non_empty(cls, v: str) -> str:
        """
        Basic sanity check only — actual validation is PayItems-driven in the
        service layer (add_draft_line calls _validate_line_type).
        """
        v = v.strip()
        if not v:
            raise ValueError("line_type must not be blank")
        return v

    @field_validator("source_type")
    @classmethod
    def source_type_valid(cls, v: str) -> str:
        if v not in _VALID_SOURCE_TYPES:
            raise ValueError(
                f"source_type must be one of {sorted(_VALID_SOURCE_TYPES)}"
            )
        return v

    @field_validator("quantity")
    @classmethod
    def quantity_non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("quantity must be non-negative")
        return v


class DraftLineUpdate(BaseModel):
    """All fields optional — only supplied (non-None) fields are changed."""
    quantity: Decimal | None = None
    rate_amount: Decimal | None = None
    notes: str | None = None
    status: str | None = None
    needs_manager_review: bool | None = None

    @field_validator("quantity")
    @classmethod
    def quantity_non_negative(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v < 0:
            raise ValueError("quantity must be non-negative")
        return v

    @field_validator("status")
    @classmethod
    def status_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_LINE_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_VALID_LINE_STATUSES)}"
            )
        return v


# ---------------------------------------------------------------------------
# Final line schemas (Milestone 5 — finalization / ledger)
# ---------------------------------------------------------------------------

class FinalLineSummary(BaseModel):
    """One locked payroll line as written to PayrollFinalLines."""
    final_line_id: int
    period_id: int
    branch_id: int
    driver_id: int
    driver_name: str
    draft_line_id: int | None = None
    work_date: date | None = None
    line_type: str
    # M14: 'Daily' for daily final lines; 'Period' for period-level pay lines.
    # Copied from PayrollDraftLines.LineScope during finalization (migration 0011).
    line_scope: str = "Daily"
    quantity: Decimal
    rate_amount: Decimal | None = None
    final_amount: Decimal
    source_type: str
    approved_by_user_id: int | None = None
    approved_at_utc: datetime
    locked_at_utc: datetime | None = None
    notes: str | None = None
    # Phase 3B: source snapshot — explains how FinalAmount was produced.
    # NULL for pre-snapshot rows (finalized before migration 0034).
    pay_item_id: int | None = None
    rate_type_id: int | None = None
    driver_rate_id: int | None = None
    resolved_rate_amount: Decimal | None = None
    rate_behavior: str | None = None
    # Phase 9: rich JSONB audit record of rate/pay-item metadata at finalization time.
    # NULL for rows finalized before migration 0039.
    source_snapshot: dict | None = None


# ---------------------------------------------------------------------------
# Pay rates — rate type catalog + driver rate matrix (Milestone 6)
# ---------------------------------------------------------------------------

class RateTypeSummary(BaseModel):
    """One entry from payroll.RateTypes — the catalog of what can be rated."""
    rate_type_id: int
    rate_code: str
    rate_name: str
    unit_name: str
    is_active: bool


_VALID_RATE_STATUSES = {"PendingApproval", "Approved", "Superseded", "Voided"}
# Only PendingApproval rates can be edited or voided by the creator.
# Only Approved rates can be superseded (by approving a newer one for same driver+type).

_VALID_ROUNDING_RULES = {"Floor", "Ceiling", "NearestHalfUp"}


# ---------------------------------------------------------------------------
# Tier schemas (M13c — OrdinalTier / RangeBracket / RangeProgressive / Block)
# ---------------------------------------------------------------------------

class TierSummary(BaseModel):
    """One tier row from payroll.DriverRateTiers."""
    tier_sequence: int
    from_unit: Decimal    # derived for range tiers; user-provided for ordinal tiers
    to_unit: Decimal | None = None   # NULL = open-ended (last tier only)
    tier_amount: Decimal


class OrdinalTierCreate(BaseModel):
    """
    One tier for OrdinalTier rates.  from_unit and to_unit are 1-based integer
    ordinal positions.  Caller provides both; the service validates contiguity.
    """
    tier_sequence: int
    from_unit: int          # 1-based ordinal; must be a positive integer
    to_unit: int | None     # NULL only on the last tier
    tier_amount: Decimal    # must be > 0

    @field_validator("from_unit")
    @classmethod
    def from_unit_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("from_unit must be >= 1 for OrdinalTier")
        return v

    @field_validator("tier_amount")
    @classmethod
    def amount_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("tier_amount must be > 0")
        return v


class RangeTierCreate(BaseModel):
    """
    One tier for RangeBracket or RangeProgressive rates.
    from_unit is intentionally absent — it is derived by the service from
    the previous tier's to_unit (Tier 1 gets from_unit=0 automatically).
    Providing from_unit in the API payload is rejected with HTTP 422
    via the extra='forbid' model config.
    """
    model_config = ConfigDict(extra="forbid")

    tier_sequence: int
    to_unit: Decimal | None = None   # NULL only on the last tier
    tier_amount: Decimal             # must be > 0

    @field_validator("tier_amount")
    @classmethod
    def amount_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("tier_amount must be > 0")
        return v


class DriverRateSummary(BaseModel):
    """One row from payroll.DriverRates — a driver's rate for a specific type."""
    driver_rate_id: int
    company_id: int
    branch_id: int
    driver_id: int
    driver_name: str
    rate_type_id: int
    rate_code: str
    rate_name: str
    unit_name: str
    amount: Decimal
    effective_from: date
    effective_to: date | None = None
    status: str
    created_by_user_id: int | None = None
    created_at_utc: datetime
    approved_by_user_id: int | None = None
    approved_at_utc: datetime | None = None
    notes: str | None = None
    # M13c additions — None for list responses; populated in detail responses.
    block_size: Decimal | None = None
    rounding_rule: str | None = None
    # tiers: None = not loaded (list endpoint); [] = no tiers; [...] = tiered rate
    tiers: list[TierSummary] | None = None


class DriverRateCreate(BaseModel):
    """Payload to create a new driver rate (starts as PendingApproval)."""
    driver_id: int
    rate_type_id: int
    amount: Decimal
    effective_from: date
    effective_to: date | None = None
    notes: str | None = None
    # M13c: supply exactly one of these groups based on the PayItem's RateBehavior
    ordinal_tiers: list[OrdinalTierCreate] | None = None   # OrdinalTier
    range_tiers:   list[RangeTierCreate]   | None = None   # RangeBracket / RangeProgressive
    block_size:    Decimal | None = None                    # Block
    rounding_rule: str | None = None                        # Block

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v

    @field_validator("rounding_rule")
    @classmethod
    def rounding_rule_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_ROUNDING_RULES:
            raise ValueError(f"rounding_rule must be one of {sorted(_VALID_ROUNDING_RULES)}")
        return v

    @model_validator(mode="after")
    def effective_to_after_from(self) -> "DriverRateCreate":
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be strictly after effective_from")
        return self


class DriverRateUpdate(BaseModel):
    """
    Partial update for a PendingApproval rate — only non-None fields are changed.
    Status transitions (approve / void) use dedicated endpoints.
    """
    amount: Decimal | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    notes: str | None = None
    # M13c: supplying either tiers field replaces ALL existing tiers for this rate
    ordinal_tiers: list[OrdinalTierCreate] | None = None
    range_tiers:   list[RangeTierCreate]   | None = None
    block_size:    Decimal | None = None
    rounding_rule: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("amount must be positive")
        return v

    @field_validator("rounding_rule")
    @classmethod
    def rounding_rule_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_ROUNDING_RULES:
            raise ValueError(f"rounding_rule must be one of {sorted(_VALID_ROUNDING_RULES)}")
        return v


# ---------------------------------------------------------------------------
# Period Pay schemas (M14 — period-level lump-sum lines)
# ---------------------------------------------------------------------------

class PeriodPayLineCreate(BaseModel):
    """
    Add a period-level pay line (Bonus, Adjustment, custom Period item).

    Period Pay lines are stored in PayrollDraftLines with WorkDate = NULL.
    Amount is the exact dollar value for this line; sign determines direction
    (positive = bonus/addition, negative = deduction/adjustment).

    Quantity is always 1; RateAmount is always NULL; CalculatedAmount = amount.
    NeedsManagerReview is always False (no rate lookup needed).
    """
    driver_id: int
    line_type: str                 # must map to an ItemScope='Period' pay item
    amount: Decimal                # non-zero; positive or negative
    notes: str | None = None

    @field_validator("line_type")
    @classmethod
    def line_type_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("line_type must not be blank")
        return v

    @field_validator("amount")
    @classmethod
    def amount_non_zero(cls, v: Decimal) -> Decimal:
        if v == 0:
            raise ValueError("amount must be non-zero (positive or negative)")
        return v


class PeriodPayLineUpdate(BaseModel):
    """
    Partial update for a Period Pay line (only amount and/or notes).
    The period must still be Open or InReview.
    Updating amount rewrites CalculatedAmount to the new value immediately.
    """
    amount: Decimal | None = None   # non-zero if supplied
    notes: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_non_zero(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v == 0:
            raise ValueError("amount must be non-zero (positive or negative)")
        return v


class RateLookupResult(BaseModel):
    """
    Result of GET /payroll/rates/lookup — the single rate that applies to a
    driver+rate_type on a specific work date.

    Both Approved and Superseded rows are searched so that historically-closed
    rates (superseded when a newer rate was approved) are still usable for
    payroll lines whose work date falls inside their effective range.

    ``found`` is False when no rate exists for this driver+type on the given date.
    """
    found: bool
    work_date: date
    driver_id: int
    rate_type_id: int
    # Populated when found=True:
    rate: DriverRateSummary | None = None


# ---------------------------------------------------------------------------
# Driver Rate Matrix (Phase 1)
# ---------------------------------------------------------------------------

class RateMatrixCurrentRate(BaseModel):
    """Snapshot of the currently-approved rate for a pay item / rate type."""
    driver_rate_id: int
    amount: Decimal
    effective_from: date
    effective_to: date | None = None
    status: str


class RateMatrixGroup(BaseModel):
    """One pay-item + rate-type combination in the matrix."""
    group_key: str          # f"{pay_item_id}:{rate_type_id}" — composite key for frontend
    pay_item_id: int
    pay_item_name: str
    item_scope: str
    rate_behavior: str
    rate_type_id: int
    rate_code: str
    rate_name: str
    unit_name: str
    current_rate: RateMatrixCurrentRate | None = None
    pending_rate: RateMatrixCurrentRate | None = None
    is_required: bool
    is_missing: bool
    # Payroll activation date: when this item becomes usable in the day-grid.
    # None when the item uses the IsDefaultBranchActive fallback (no explicit config row).
    pay_item_effective_from: date | None = None


class DriverRateMatrix(BaseModel):
    """Full rate matrix for a driver as-of a given date."""
    driver_id: int
    driver_name: str
    driver_code: str | None = None
    branch_id: int
    branch_name: str
    as_of: date
    groups: list[RateMatrixGroup]


# ---------------------------------------------------------------------------
# Driver Pay Rules (M15 — Minimum / Maximum Pay)
# ---------------------------------------------------------------------------

class DriverPayRuleSummary(BaseModel):
    """One row from payroll.DriverPayRules."""
    driver_pay_rule_id: int
    company_id: int
    branch_id: int
    driver_id: int
    rule_type: str           # 'MinimumPay' | 'MaximumPay'
    amount: Decimal          # > 0
    effective_from: date
    effective_to: date | None = None
    status: str              # 'Active' | 'Ended' | 'Voided'
    created_by_user_id: int | None = None
    created_at_utc: datetime
    updated_by_user_id: int | None = None
    updated_at_utc: datetime | None = None
    notes: str | None = None


_VALID_RULE_TYPES = {"MinimumPay", "MaximumPay"}


class DriverPayRuleCreate(BaseModel):
    driver_id: int
    rule_type: str
    amount: Decimal
    effective_from: date
    effective_to: date | None = None
    notes: str | None = None

    @field_validator("rule_type")
    @classmethod
    def rule_type_valid(cls, v: str) -> str:
        if v not in _VALID_RULE_TYPES:
            raise ValueError(f"rule_type must be one of {sorted(_VALID_RULE_TYPES)}")
        return v

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be > 0 (use End or Void to stop a rule)")
        return v

    @model_validator(mode="after")
    def effective_to_after_from(self) -> "DriverPayRuleCreate":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to must be on or after effective_from")
        return self


class DriverPayRuleEnd(BaseModel):
    """Payload for POST /driver-pay-rules/{id}/end — close the rule at a specific date."""
    effective_to: date


class DriverPayRuleNotesUpdate(BaseModel):
    """Payload for PATCH /driver-pay-rules/{id} — notes-only update."""
    notes: str | None = None


# ---------------------------------------------------------------------------
# Batch rate save (Phase 2A)
# ---------------------------------------------------------------------------

class BatchRateChange(BaseModel):
    """
    One rate change in a batch save request.

    Both pay_item_id and rate_type_id are required so the exact
    PayItemRateTypeMap row can be validated.  This prevents saving a rate
    against an active RateType that is not actively mapped to the named
    PayItem for the driver's branch.
    """
    pay_item_id: int    # must match an active PayItemRateTypeMap row for this driver's branch
    rate_type_id: int
    amount: Decimal
    notes: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v


class BatchRateRequest(BaseModel):
    """
    Payload for POST /payroll/drivers/{driver_id}/rates/batch.

    All changes share the same effective_from.  Tiers and block config are
    not supported in Phase 2A — rates with OrdinalTier, RangeBracket,
    RangeProgressive, or Block behaviors must be saved individually.
    """
    effective_from: date
    notes: str | None = None   # batch-level note (applied to each created rate)
    changes: list[BatchRateChange]

    @field_validator("changes")
    @classmethod
    def changes_not_empty(cls, v: list) -> list:
        if not v:
            raise ValueError("changes must not be empty")
        return v


class BatchRateSaveResult(BaseModel):
    """Summary response from POST /payroll/drivers/{driver_id}/rates/batch."""
    driver_id: int
    effective_from: date
    allow_self_approval: bool
    created_count: int
    updated_pending_count: int
    approved_count: int
    pending_count: int
    rates: list[DriverRateSummary]


# ---------------------------------------------------------------------------
# Phase 2B — driver rate summary / pending / history
# ---------------------------------------------------------------------------

class DriverRatesSummary(BaseModel):
    """
    Quick status counts for a driver — used by the right-panel header badges
    in the Pay Rates UI.

    missing_required_count: number of required pay-item/rate-type combinations
    (from the matrix INNER JOIN) that have no current Approved or Superseded rate
    as of today.  Derived from the same query as the rate matrix; returns None
    only when the computation fails unexpectedly.
    """
    driver_id: int
    pending_count: int          # PendingApproval rates
    future_approved_count: int  # Approved rates whose effective_from > today
    missing_required_count: int | None = None  # Required matrix slots with no current rate


# ---------------------------------------------------------------------------
# Copy Rates From Driver (Phase 2C)
# ---------------------------------------------------------------------------

class CopyRatesRequest(BaseModel):
    """
    Payload for POST /payroll/drivers/{target_driver_id}/rates/copy-from/{source_driver_id}.

    Copies current Approved rates from the source driver (as-of effective_from)
    to the target driver, creating new rates effective from the given date.

    Rules:
    - Only Approved rates that are current as-of effective_from are copied.
    - PendingApproval rates from source are never copied.
    - Each copied rate becomes PendingApproval unless AllowSelfApproval=True.
    - If AllowSelfApproval=True, copied rates auto-approve (same as batch save).
    - Existing target rates are not deleted; the same create/supersede logic applies.
    - All-or-nothing: if any copied rate would be invalid, the whole request fails.
    - If include_pay_rules=True, Active MinimumPay/MaximumPay rules from the source
      are also copied (as new Active rules on the target). Rejected with 422 if
      the target already has an overlapping rule of the same type.
    """
    effective_from: date
    include_pay_rules: bool = False

    @model_validator(mode="after")
    def effective_from_not_future_only(self) -> "CopyRatesRequest":
        # No date restriction here — backdating guard runs in the service
        return self


class CopyRatesResult(BaseModel):
    """Response from the copy-from endpoint."""
    target_driver_id: int
    source_driver_id: int
    effective_from: date
    allow_self_approval: bool
    rates_copied: int        # number of rate rows created
    rates_approved: int      # subset that were auto-approved
    rates_pending: int       # subset left as PendingApproval
    pay_rules_copied: int    # 0 if include_pay_rules=False


# ---------------------------------------------------------------------------
# CP-1 — Day Grid schemas
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CP-2 P1 #1 — Period-eligible drivers (for Bonus driver dropdown)
# ---------------------------------------------------------------------------

class PeriodEligibleDriver(BaseModel):
    driver_id: int
    driver_name: str
    driver_code: str | None = None


class PeriodEligibleDriversResponse(BaseModel):
    drivers: list[PeriodEligibleDriver]


class DayGridColumn(BaseModel):
    pay_item_code: str
    label: str
    rate_behavior: str
    is_time: bool


class DayGridStatusKey(BaseModel):
    status_key_id: int
    key_code: str
    label: str
    is_off_reason: bool
    hours_value: Decimal | None = None


class DayGridLineValue(BaseModel):
    line_id: int | None = None
    quantity: str | None = None
    calculated_amount: str | None = None
    needs_manager_review: bool = False


class DayGridRow(BaseModel):
    driver_id: int
    driver_name: str
    driver_code: str | None = None
    status_key: str | None = None
    status_label: str | None = None
    is_off: bool = False
    notes: str | None = None
    values: dict[str, DayGridLineValue] = {}


class DayGridSummary(BaseModel):
    total_drivers: int
    worked: int
    pto: int
    off: int
    total_hours: str
    total_miles: str
    gross_total: str
    needs_attention: int


class DayGridPeriod(BaseModel):
    period_id: int
    period_name: str
    start_date: date
    end_date: date
    pay_date: date | None = None
    status: str
    branch_id: int
    branch_name: str


class DayGridResponse(BaseModel):
    period: DayGridPeriod
    work_date: date
    columns: list[DayGridColumn]
    status_keys: list[DayGridStatusKey]
    rows: list[DayGridRow]
    summary: DayGridSummary


class DayGridSaveRow(BaseModel):
    driver_id: int
    values: dict[str, str] = {}
    status_key: str | None = None
    notes: str | None = None


class DayGridSaveRequest(BaseModel):
    work_date: date
    rows: list[DayGridSaveRow]


# ---------------------------------------------------------------------------
# CP-2.5 — Period-level Drivers Off
# ---------------------------------------------------------------------------

class DriversOffEntry(BaseModel):
    driver_id: int
    driver_name: str
    driver_code: str | None = None
    work_date: date
    status_key_code: str
    status_label: str | None = None
    notes: str | None = None


class DriversOffResponse(BaseModel):
    period_id: int
    entries: list[DriversOffEntry]
    total_count: int


# ---------------------------------------------------------------------------
# CP-3A — Finalization Preview (read-only)
# ---------------------------------------------------------------------------

class FinalizationPreviewLine(BaseModel):
    """One draft line as it would appear in FinalLines after finalization."""
    draft_line_id: int
    driver_id: int
    driver_name: str | None
    work_date: date | None
    line_type: str
    line_scope: str
    quantity: Decimal | None
    rate_amount: Decimal | None
    calculated_amount: Decimal | None
    final_amount: Decimal  # COALESCE(calculated_amount, quantity * COALESCE(rate_amount, 0))
    needs_manager_review: bool
    # Phase 3B: rate source details mirroring what FinalLines will store.
    rate_behavior: str | None = None
    driver_rate_id: int | None = None
    rate_type_id: int | None = None
    resolved_rate_amount: Decimal | None = None


class FinalizationPreviewSysAdjustment(BaseModel):
    """A SYS_MIN_TOPUP or SYS_MAX_CAP row that would be written by finalization."""
    driver_id: int
    driver_name: str | None
    adjustment_type: str          # "SYS_MIN_TOPUP" or "SYS_MAX_CAP"
    gross_before: Decimal
    adjustment_amount: Decimal    # positive for top-up, negative for cap
    final_pay: Decimal


class FinalizationPreviewDriverTotal(BaseModel):
    """Aggregated pay summary for one driver in the preview."""
    driver_id: int
    driver_name: str | None
    daily_pay: Decimal
    period_pay: Decimal
    gross_pay: Decimal
    sys_adjustment: Decimal       # sum of SYS_MIN_TOPUP / SYS_MAX_CAP deltas
    final_pay: Decimal
    line_count: int


class FinalizationPreviewResponse(BaseModel):
    """Full read-only preview of what finalize_period would do."""
    period_id: int
    period_name: str
    period_status: str
    branch_id: int
    branch_name: str | None
    can_finalize: bool
    blockers: list[str]
    warnings: list[str]
    driver_totals: list[FinalizationPreviewDriverTotal]
    sys_adjustments: list[FinalizationPreviewSysAdjustment]
    lines: list[FinalizationPreviewLine]
    total_final_gross: Decimal
    # Line count semantics (explicit to avoid frontend guessing):
    draft_line_count: int        # non-Void draft lines that will be inserted as FinalLines
    sys_adjustment_count: int    # number of SYS_MIN_TOPUP / SYS_MAX_CAP rows to be inserted
    final_line_count_estimate: int  # draft_line_count + sys_adjustment_count
    driver_count: int
