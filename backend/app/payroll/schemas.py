"""
Pydantic schemas for the payroll domain (periods, status changes, draft lines,
pay rates / driver rate matrix).
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any

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
    "Draft":    {"Cancelled"},           # CP-1D: Draft→Open removed; submit path handles promotion atomically
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
# CP-0A: Open is the primary editable status.
# CP-1A: Returned is also editable — corrections must be possible before resubmission.
# InReview and all other statuses are read-only for operational data.
ENTRY_ALLOWED_STATUSES = {"Open", "Returned"}

# CP-2F: Draft (Prepared) allows operational source-entry only — not financial paths.
# Use SOURCE_ENTRY_STATUSES for day-grid save and daily-source DraftLine guards only.
# Do NOT use this constant for Period Pay, Bonus, or any financial line creation.
SOURCE_ENTRY_STATUSES = {"Draft", "Open", "Returned"}


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


# ---------------------------------------------------------------------------
# CP-3A — Bonus Events schemas
# ---------------------------------------------------------------------------

class BonusEventResponse(BaseModel):
    """A single canonical bonus event row."""
    bonus_event_id: int
    period_id: int
    company_id: int
    branch_id: int
    driver_id: int
    amount: Decimal
    reason: str | None
    notes: str | None
    status: str                     # 'Active' | 'Voided'
    data_revision: int
    source_draft_line_id: int | None
    voided_by_user_id: int | None
    voided_at_utc: datetime | None
    void_reason: str | None
    created_by_user_id: int | None
    created_at_utc: datetime
    updated_by_user_id: int | None
    updated_at_utc: datetime | None


class BonusEventCreate(BaseModel):
    """Create one canonical bonus event."""
    driver_id: int
    amount: Decimal
    reason: str | None = None
    notes: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Bonus amount must be positive (> 0).")
        return v


class BonusEventUpdate(BaseModel):
    """Partial update for a bonus event (amount, reason, notes)."""
    amount: Decimal | None = None
    reason: str | None = None
    notes: str | None = None
    data_revision: int | None = None    # optional optimistic-lock value

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v <= 0:
            raise ValueError("Bonus amount must be positive (> 0).")
        return v


class BonusEventPreviewEntry(BaseModel):
    """One bonus event as it will appear after finalization (preview only)."""
    bonus_event_id: int
    driver_id: int
    driver_name: str | None
    amount: Decimal
    reason: str | None = None
    notes: str | None = None


class BonusSummaryEvent(BaseModel):
    """One bonus event row inside the zero-inclusive bonus summary (CP-3B1)."""
    bonus_event_id: int
    driver_id: int
    amount: Decimal
    reason: str | None
    notes: str | None
    status: str                     # 'Active' | 'Voided'
    created_by_user_id: int | None
    created_at_utc: datetime
    updated_by_user_id: int | None
    updated_at_utc: datetime | None
    voided_by_user_id: int | None
    voided_at_utc: datetime | None
    void_reason: str | None
    data_revision: int
    batch_correlation_id: str | None
    idempotency_key: str | None
    source_draft_line_id: int | None


class BonusSummaryCapabilities(BaseModel):
    """Backend-owned mutation capabilities for the bonus summary (CP-3B1)."""
    can_create: bool
    can_update: bool
    can_void: bool
    reason_codes: list[str] = []


class BonusSummaryDriver(BaseModel):
    """One eligible driver row in the zero-inclusive bonus summary (CP-3B1).

    The roster comes from the CP-2E period eligibility snapshot — drivers with
    zero bonus events still appear with total_bonus = 0.
    """
    driver_id: int
    driver_code: str | None
    driver_name: str | None
    eligibility_reason_code: str
    total_bonus: Decimal            # sum of Active events only
    active_event_count: int
    voided_event_count: int
    events: list[BonusSummaryEvent]
    capabilities: BonusSummaryCapabilities


class BonusSummaryResponse(BaseModel):
    """Zero-inclusive bonus summary for a payroll period (CP-3B1)."""
    period_id: int
    branch_id: int
    period_status: str
    eligibility_source: str         # 'PeriodEligibilitySnapshot'
    drivers: list[BonusSummaryDriver]
    active_event_count: int
    active_bonus_total: Decimal
    # CP-3B2a: period-level bonus-mutation concurrency token, sourced from
    # PayrollPeriods.BonusDataRevision — never derived from
    # MAX(PayrollBonusEvents.DataRevision).
    bonus_data_revision: int


# ---------------------------------------------------------------------------
# CP-3B2b — create-only transactional bonus batch
# ---------------------------------------------------------------------------

# NUMERIC(18,2): at most 16 integer digits and exactly 2 fractional digits.
_BONUS_AMOUNT_MAX_EXCLUSIVE = Decimal("10") ** 16   # 10_000_000_000_000_000.00


def _validate_positive_numeric_18_2(v: Decimal) -> Decimal:
    """Shared amount validator for batch items: positive, fits NUMERIC(18,2),
    at most two decimal places (no silent rounding).  Returns the value
    quantized to two decimals so downstream canonical hashing is deterministic.
    """
    if not v.is_finite():
        raise ValueError("Bonus amount must be a finite number.")
    if v <= 0:
        raise ValueError("Bonus amount must be positive (> 0).")
    if abs(v) >= _BONUS_AMOUNT_MAX_EXCLUSIVE:
        raise ValueError("Bonus amount exceeds the maximum allowed (NUMERIC(18,2)).")
    quantized = v.quantize(Decimal("0.01"))
    if quantized != v:
        raise ValueError("Bonus amount may have at most two decimal places.")
    return quantized


class BonusBatchItem(BaseModel):
    """One create-only bonus row inside a batch (CP-3B2b)."""
    driver_id: int
    amount: Decimal
    reason: str | None = None
    notes: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_valid(cls, v: Decimal) -> Decimal:
        return _validate_positive_numeric_18_2(v)


class BonusBatchCreate(BaseModel):
    """
    Payload for POST /payroll/periods/{period_id}/bonuses/batch (CP-3B2b).

    Create-only: every item creates a separate bonus event.  Duplicate driver
    rows are allowed (each is its own event).  Item order is significant — it
    is part of the request hash, so the same items in a different order form a
    different request for idempotency purposes.
    """
    idempotency_key: str
    expected_bonus_data_revision: int
    items: list[BonusBatchItem]

    @field_validator("idempotency_key")
    @classmethod
    def idempotency_key_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("idempotency_key must be non-empty.")
        if len(v) > 200:
            raise ValueError("idempotency_key must be at most 200 characters.")
        return v

    @field_validator("expected_bonus_data_revision")
    @classmethod
    def expected_revision_valid(cls, v: int) -> int:
        if v < 0:
            raise ValueError("expected_bonus_data_revision must be >= 0.")
        return v

    @field_validator("items")
    @classmethod
    def items_valid(cls, v: list) -> list:
        if len(v) < 1:
            raise ValueError("items must contain at least one bonus.")
        if len(v) > 100:
            raise ValueError("items must contain at most 100 bonuses.")
        return v


class BonusBatchResponse(BaseModel):
    """Result of a bonus batch apply or idempotent replay (CP-3B2b)."""
    period_id: int
    branch_id: int
    batch_request_id: int
    idempotency_key: str
    batch_correlation_id: str
    expected_bonus_data_revision: int
    result_bonus_data_revision: int
    created_event_count: int
    created_event_ids: list[int]
    events: list[BonusEventResponse]
    replayed: bool


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
    """One rate group in the driver pay-rate matrix.

    rate_source distinguishes PayItem-backed groups from StatusRateColumn groups.
    For PayItem groups: pay_item_id, pay_item_name, item_scope, rate_behavior are set.
    For StatusRateColumn groups: status_rate_column_id is set; pay_item_* are None.
    """
    group_key: str          # "{pay_item_id}:{rate_type_id}" or "SRC:{src_id}:{rate_type_id}"
    rate_source: str        # "PayItem" | "StatusRateColumn"
    pay_item_id: int | None = None
    pay_item_name: str | None = None
    item_scope: str | None = None
    rate_behavior: str | None = None
    status_rate_column_id: int | None = None
    rate_type_id: int
    rate_code: str
    rate_name: str
    unit_name: str
    current_rate: RateMatrixCurrentRate | None = None
    pending_rate: RateMatrixCurrentRate | None = None
    is_required: bool
    is_missing: bool
    # Payroll activation date (PayItem groups only).
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

    Exactly one of pay_item_id or status_rate_column_id must be set.

    For PayItem-backed rates: pay_item_id + rate_type_id are validated against
    the active PayItemRateTypeMap for the driver's branch.

    For StatusRateColumn-backed rates: status_rate_column_id + rate_type_id are
    validated against the StatusRateColumns table (no PayItemRateTypeMap needed).
    """
    pay_item_id: int | None = None
    status_rate_column_id: int | None = None
    rate_type_id: int
    amount: Decimal
    notes: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("amount must be positive")
        return v

    from pydantic import model_validator

    @model_validator(mode="after")
    def exactly_one_source(self) -> "BatchRateChange":
        has_pi  = self.pay_item_id is not None
        has_src = self.status_rate_column_id is not None
        if has_pi == has_src:  # both set or neither set
            raise ValueError(
                "Exactly one of pay_item_id or status_rate_column_id must be provided."
            )
        return self


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


# Shared {state, reason_code} availability shape -- also used by the P6A
# Finalized Payroll Information Library contracts further below.
class FinalizedSectionAvailability(BaseModel):
    state: str
    reason_code: str | None = None


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
    gross_total: str | None = None   # CP-2F: None for Draft (Prepared) periods — financials not available
    needs_attention: int
    financials_available: bool = True  # CP-2F: False for Draft periods


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
    # Stage B3 Unit 8C-3: set only for Locked/Archived periods, where row
    # status_key/status_label/is_off come from immutable calculation-snapshot
    # evidence rather than live PayrollStatusKeys. None for Draft/Open/
    # InReview/Returned/Approved, which are unaffected by this field.
    status_evidence: FinalizedSectionAvailability | None = None


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
    # Stage B3 Unit 8C-7: set only for Locked/Archived periods, where entries
    # come from immutable calculation-snapshot evidence rather than live
    # PayrollStatusKeys. Reuses the same {state, reason_code} shape already
    # established for Day Grid (Unit 8C-3) and CP-5B Off Drivers (Unit 8C-5).
    # None for Draft/Open/InReview/Returned/Approved.
    status_evidence: FinalizedSectionAvailability | None = None


# ---------------------------------------------------------------------------
# CP-5B — Official Fully-Off and selected-day Off-driver read contracts
# ---------------------------------------------------------------------------

class FullyOffDriverSummary(BaseModel):
    driver_id: int
    driver_name: str
    driver_code: str | None = None
    eligible_scheduled_day_count: int
    off_day_count: int


class OffDriversSummaryResponse(BaseModel):
    period_id: int
    start_date: date
    end_date: date
    total_fully_off_drivers: int
    fully_off_drivers: list[FullyOffDriverSummary] = []
    # Stage B3 Unit 8C-5: set only for Locked/Archived periods, where the
    # Fully-Off result comes from immutable calculation-snapshot evidence
    # rather than live PayrollStatusKeys. Reuses the same {state,
    # reason_code} shape already established for Day Grid (Unit 8C-3) and
    # the P6A Finalized Payroll Information Library. None for Draft/Open/
    # InReview/Returned/Approved.
    status_evidence: FinalizedSectionAvailability | None = None


class SelectedDayOffDriver(BaseModel):
    driver_id: int
    driver_name: str
    driver_code: str | None = None
    work_date: date
    day_name: str
    status_key_id: int | None = None
    status_code: str | None = None
    status_label: str | None = None
    is_off_reason: bool = True
    has_note: bool
    note: str | None = None


class SelectedDayOffDriversResponse(BaseModel):
    period_id: int
    work_date: date
    day_name: str
    total_count: int
    drivers: list[SelectedDayOffDriver] = []
    # Stage B3 Unit 8C-5: see OffDriversSummaryResponse.status_evidence.
    status_evidence: FinalizedSectionAvailability | None = None


# ---------------------------------------------------------------------------
# CP-3A — Finalization Preview (read-only)
# ---------------------------------------------------------------------------

class FinalizationPreviewLine(BaseModel):
    """One draft line as it would appear in FinalLines after finalization."""
    draft_line_id: int | None
    source_key: str
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
    """A SYS_MIN_TOPUP or SYS_MAX_CAP row that would be written by finalization.

    CP-3C: final_pay here is the driver's actual total final pay — the same
    value as this driver's FinalizationPreviewDriverTotal.final_pay and the
    same value the finalized ledger will sum to for this driver. It is NOT
    "normal pay after just this one adjustment" (gross_before + adjustment_amount
    alone) — bonus_total is included so a caller reading only this list still
    sees the true payout, not a bonus-free intermediate figure.
    """
    driver_id: int
    driver_name: str | None
    adjustment_type: str          # "SYS_MIN_TOPUP" or "SYS_MAX_CAP"
    gross_before: Decimal         # normal_base only — excludes bonus
    adjustment_amount: Decimal    # positive for top-up, negative for cap; computed on gross_before (bonus-free)
    bonus_total: Decimal = Decimal("0")   # this driver's total Active bonus, added after min/max
    final_pay: Decimal            # gross_before + adjustment_amount + bonus_total (this driver's true total)


class FinalizationPreviewDriverTotal(BaseModel):
    """Aggregated pay summary for one driver in the preview.

    CP-3C: gross_pay is normal pay only (daily_pay + period_pay) — bonus is
    never folded into it. sys_adjustment (min/max) is computed from that same
    bonus-free base. bonus_total is added back in only after min/max, so
    final_pay = gross_pay + sys_adjustment + bonus_total.
    """
    driver_id: int
    driver_name: str | None
    daily_pay: Decimal
    status_pay: Decimal = Decimal("0")
    period_pay: Decimal
    gross_pay: Decimal            # normal pay only — excludes bonus
    sys_adjustment: Decimal       # sum of SYS_MIN_TOPUP / SYS_MAX_CAP deltas, computed on gross_pay (bonus-free)
    bonus_total: Decimal = Decimal("0")   # sum of Active canonical PayrollBonusEvents for this driver
    final_pay: Decimal            # gross_pay + sys_adjustment + bonus_total
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
    draft_line_count: int        # non-Void, non-BONUS draft lines that will be inserted
    sys_adjustment_count: int    # SYS_MIN_TOPUP / SYS_MAX_CAP rows to be inserted
    bonus_event_count: int = 0   # active PayrollBonusEvents rows to be inserted (CP-3A)
    final_line_count_estimate: int  # draft_line_count + sys_adjustment_count + bonus_event_count
    driver_count: int
    # CP-3A: canonical bonus events (separate from DraftLine-based lines list)
    bonus_events: list["BonusEventPreviewEntry"] = []


# ---------------------------------------------------------------------------
# CP-4B: Open/Returned live read-only calculation preview.
#
# Distinct from FinalizationPreviewResponse above: that contract is
# Approved-only and mirrors exactly what finalize_period would write.
# This contract is for Open/Returned periods only, is always provisional
# (current source/config, not a submitted snapshot), and deliberately
# omits a period-wide calculation_version -- a single version would
# misrepresent the several distinct calculation lanes it aggregates
# (PerUnit, EnteredAmount/manual, canonical Status, bonus, min/max).
# ---------------------------------------------------------------------------

class CalculationPreviewLine(BaseModel):
    """One virtual financial line contributing to a driver's CP-4B total."""
    source_type: str                       # "DraftLine" | "StatusEntryState" | "BonusEvent"
    source_id: str | None
    line_type: str
    work_date: date | None
    pay_item_id: int | None = None
    rate_column_id: int | None = None      # StatusRateColumnID for Status lines only
    driver_id: int
    quantity: Decimal | None
    resolved_rate: Decimal | None
    calculated_amount: Decimal | None
    needs_manager_review: bool
    blocker_reason: str | None = None


class CalculationPreviewDriverTotal(BaseModel):
    """One driver's provisional expected-pay breakdown."""
    driver_id: int
    driver_name: str | None
    daily_pay: Decimal
    status_pay: Decimal
    period_pay: Decimal
    normal_base: Decimal
    minimum_adjustment: Decimal
    maximum_adjustment: Decimal
    bonus_total: Decimal
    expected_pay: Decimal
    needs_manager_review: bool
    blockers: list[str]
    lines: list[CalculationPreviewLine]


class CalculationPreviewResponse(BaseModel):
    """
    Read-only provisional expected-income breakdown for an Open/Returned
    period, calculated live from current effective source/config. Never a
    submitted snapshot (see CP-4C+ for the future immutable-snapshot read).
    """
    payroll_period_id: int
    company_id: int
    branch_id: int
    branch_name: str | None
    status: str
    provisional: bool = True
    financials_available: bool = True
    has_blockers: bool
    blockers: list[str]
    warnings: list[str]
    drivers: list[CalculationPreviewDriverTotal]
    total_expected_pay: Decimal


# ---------------------------------------------------------------------------
# CP-1C: Branch-locked candidate-based period creation schemas
# No PayDate field anywhere in these schemas.
# ---------------------------------------------------------------------------

class CandidateSelectedInfo(BaseModel):
    """Info about the currently selected/previewed candidate period."""
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
    previous_cursor: str | None = None
    next_cursor: str | None = None


class CandidatePreviewResponse(BaseModel):
    """Full response for GET /payroll/branches/{branch_id}/period-candidates."""
    mode: str               # "OPEN_CREATION" | "PREPARED_CREATION"
    selected: CandidateSelectedInfo
    navigation: CandidateNavigationInfo


class PeriodCreationRequest(BaseModel):
    """Body for POST /payroll/branches/{branch_id}/period-creations.

    extra="forbid" ensures stray fields (start_date, status, pay_date, etc.)
    are rejected with HTTP 422 rather than silently ignored.
    """
    model_config = ConfigDict(extra="forbid")

    candidate_key: str


class PeriodCreationResponse(BaseModel):
    """Response for POST /payroll/branches/{branch_id}/period-creations."""
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
    allowed: bool
    reason_code: str | None = None
    reason_message: str | None = None


class WorkflowSlotItem(BaseModel):
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
    open: WorkflowSlotItem | None = None
    prepared: WorkflowSlotItem | None = None    # Draft periods surface here
    in_review: WorkflowSlotItem | None = None
    returned: WorkflowSlotItem | None = None


class WorkflowAlert(BaseModel):
    code: str
    severity: str                       # blocker | warning | info
    title: str
    message: str
    related_period_id: int | None = None
    affected_action_codes: list[str] = []


class PeriodWorkflowCapabilities(BaseModel):
    can_enter_source: WorkflowCapability
    can_submit_for_review: WorkflowCapability
    can_resubmit_returned: WorkflowCapability
    can_view_review: WorkflowCapability
    can_cancel: WorkflowCapability
    can_open_day_grid: WorkflowCapability


class BranchWorkflowCapabilities(BaseModel):
    can_view_current_workflow: WorkflowCapability
    can_create_open_candidate: WorkflowCapability
    can_create_prepared_candidate: WorkflowCapability
    can_view_candidates: WorkflowCapability
    periods: dict[str, PeriodWorkflowCapabilities] = {}  # keyed by str(period_id)


class BranchWorkflowEntry(BaseModel):
    branch_id: int
    branch_name: str
    setup_status: str                   # "complete" | "missing" | "incomplete" | "inactive"
    slots: WorkflowBranchSlots
    capabilities: BranchWorkflowCapabilities
    alerts: list[WorkflowAlert] = []


class CurrentWorkflowResponse(BaseModel):
    scope: str                          # "company" | "branch"
    company_id: int
    requested_branch_id: int | None = None
    branches: list[BranchWorkflowEntry] = []


# ---------------------------------------------------------------------------
# CP-5A: Current Payroll Hub
# ---------------------------------------------------------------------------

class CurrentPayrollHubMetrics(BaseModel):
    """Operational, period-scoped driver counts for one active workflow slot."""
    total_eligible_drivers: int
    working_drivers: int
    fully_off_drivers: int


class CurrentPayrollHubDriverSummary(BaseModel):
    """A compact, authority-backed expected-income driver summary."""
    driver_id: int
    driver_code: str | None = None
    driver_name: str | None = None
    expected_pay: Decimal


class CurrentPayrollHubFinancialSummary(BaseModel):
    """Compact CP-4B live calculation data for an Open or Returned slot only."""
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
    financials_available: bool
    financial_summary: CurrentPayrollHubFinancialSummary | None = None
    metrics: CurrentPayrollHubMetrics


class CurrentPayrollHubSlots(BaseModel):
    open: CurrentPayrollHubPeriodSlot | None = None
    prepared: CurrentPayrollHubPeriodSlot | None = None
    in_review: CurrentPayrollHubPeriodSlot | None = None
    returned: CurrentPayrollHubPeriodSlot | None = None


class CurrentPayrollHubBranch(BaseModel):
    branch_id: int
    branch_name: str
    setup_status: str
    slots: CurrentPayrollHubSlots
    capabilities: BranchWorkflowCapabilities
    alerts: list[WorkflowAlert] = []


class CurrentPayrollHubResponse(BaseModel):
    scope: str
    company_id: int
    requested_branch_id: int | None = None
    generated_at_utc: datetime
    branches: list[CurrentPayrollHubBranch] = []


# ---------------------------------------------------------------------------
# CP-5C: Calculation reports. These are semantic read models, not export rows.
# ---------------------------------------------------------------------------

class ReportMetadata(BaseModel):
    period_id: int
    period_code: str
    period_name: str
    period_status: str
    branch_id: int
    report_type: str
    authority_kind: str
    financials_available: bool
    unavailable_reason: str | None = None
    snapshot_id: int | None = None
    revision_number: int | None = None
    snapshot_hash: str | None = None
    report_evidence_available: bool
    report_evidence_version: int | None = None
    report_evidence_hash: str | None = None
    blockers: list[str] = []
    warnings: list[str] = []
    generated_at_utc: datetime


class ReportColumn(BaseModel):
    pay_item_id: int
    code: str
    label: str
    category: str
    data_type: str
    unit: str | None = None
    scope: str
    sort_order: int


class ReportWorkSection(BaseModel):
    daily_rows: list[dict] = []
    status_entries: list[dict] = []
    status_summaries: list[dict] = []


class ReportPaySection(BaseModel):
    daily_pay: Decimal
    status_pay: Decimal
    period_pay: Decimal
    minimum_adjustment: Decimal
    maximum_adjustment: Decimal
    bonus_total: Decimal
    total_pay: Decimal
    driver_code: str | None = None
    driver_name: str | None = None
    financial_lines: list[dict] = []


class ReportDriver(BaseModel):
    driver_id: int
    driver_code: str | None = None
    driver_name: str | None = None
    work: ReportWorkSection
    pay: ReportPaySection | None = None
    bonus_events: list[dict] = []


class CalculationReportResponse(BaseModel):
    metadata: ReportMetadata
    columns: list[ReportColumn] = []
    drivers: list[ReportDriver] = []
    work_totals: dict[str, Decimal] = {}
    pay_totals: dict[str, Decimal] | None = None


class DriversReportResponse(CalculationReportResponse):
    pass


class PeriodWorkReportResponse(CalculationReportResponse):
    pass


class PeriodPayReportResponse(CalculationReportResponse):
    pass


class MixedReportResponse(CalculationReportResponse):
    pass


# ---------------------------------------------------------------------------
# P6A: Finalized Payroll Information Library. These contracts are read-only
# projections over FinalLines plus the exact originating immutable snapshot.
# ---------------------------------------------------------------------------

class FinalizedFinancialSummary(BaseModel):
    total_pay: Decimal
    final_line_count: int
    driver_count: int


class FinalizedSnapshotProvenance(BaseModel):
    snapshot_id: int | None = None
    revision_number: int | None = None
    snapshot_hash: str | None = None
    source_config_hash: str | None = None


class FinalizedPeriodListItem(BaseModel):
    """Minimal navigation item for the ledger-owned finalized-period list."""

    period_id: int
    period_code: str
    period_name: str
    period_status: str
    period_type: str
    branch_id: int
    branch_name: str
    start_date: date
    end_date: date
    pay_date: date | None = None
    finalized_at_utc: datetime | None = None


class FinalizedOverviewResponse(BaseModel):
    period_id: int
    period_code: str
    period_name: str
    period_status: str
    company_id: int
    branch_id: int
    branch_name: str
    finalized_at_utc: datetime | None = None
    finalized_by_user_id: int | None = None
    financial_summary: FinalizedFinancialSummary
    snapshot_provenance: FinalizedSnapshotProvenance
    section_availability: dict[str, FinalizedSectionAvailability]
    generated_at_utc: datetime


class FinalizedReportMetadata(BaseModel):
    period_id: int
    period_code: str
    period_name: str
    period_status: str
    branch_id: int
    report_type: str
    authority_kind: str = "FINAL_LINES"
    financials_available: bool
    snapshot_id: int | None = None
    revision_number: int | None = None
    snapshot_hash: str | None = None
    report_evidence_available: bool
    report_evidence_version: int | None = None
    report_evidence_hash: str | None = None
    section_availability: dict[str, FinalizedSectionAvailability]
    generated_at_utc: datetime


class FinalizedCalculationReportResponse(BaseModel):
    metadata: FinalizedReportMetadata
    columns: list[ReportColumn] = []
    drivers: list[ReportDriver] = []
    work_totals: dict[str, Decimal] = {}
    pay_totals: dict[str, Decimal] | None = None


class FinalizedOffStatusEntry(BaseModel):
    driver_id: int
    driver_name: str | None = None
    driver_code: str | None = None
    work_date: date
    status_key_id: int
    status_code: str
    status_label: str
    is_off_reason: bool


class FinalizedOffDriversMetadata(BaseModel):
    period_id: int
    period_code: str
    period_name: str
    period_status: str
    branch_id: int
    authority_kind: str = "FINALIZED_STATUS_SNAPSHOT"
    snapshot_id: int | None = None
    revision_number: int | None = None
    snapshot_hash: str | None = None
    report_evidence_available: bool
    report_evidence_version: int | None = None
    report_evidence_hash: str | None = None
    section_availability: dict[str, FinalizedSectionAvailability]
    generated_at_utc: datetime


class FinalizedOffDriversResponse(BaseModel):
    metadata: FinalizedOffDriversMetadata
    total_fully_off_drivers: int
    fully_off_drivers: list[FullyOffDriverSummary] = []
    status_entries: list[FinalizedOffStatusEntry] = []


class FinalizedUsedRateDefinition(BaseModel):
    used_rate_definition_id: int
    driver_id: int
    driver_name: str | None = None
    driver_code: str | None = None
    evidence_kind: str
    source_type: str
    pay_item_id: int | None = None
    pay_item_code: str | None = None
    pay_item_label: str | None = None
    rate_type_id: int | None = None
    rate_type_code: str | None = None
    rate_type_name: str | None = None
    unit_name: str | None = None
    driver_rate_id: int | None = None
    driver_pay_rule_id: int | None = None
    rate_behavior: str | None = None
    rate_amount: Decimal | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    rate_status: str | None = None
    block_size: Decimal | None = None
    rounding_rule: str | None = None
    rule_type: str | None = None
    rule_amount: Decimal | None = None
    rule_status: str | None = None
    definition_fingerprint: str
    snapshot_line_ids: list[int] = []
    line_use_count: int


class FinalizedBonusEventEvidence(BaseModel):
    bonus_event_id: int
    driver_id: int
    driver_name: str | None = None
    driver_code: str | None = None
    amount: Decimal
    reason: str | None = None
    notes: str | None = None
    data_revision: int
    creator_user_id: int | None = None
    creator_display_name: str | None = None
    created_at_utc: datetime


class FinalizedRatesUsedMetadata(BaseModel):
    period_id: int
    period_code: str
    period_name: str
    period_status: str
    branch_id: int
    authority_kind: str = "FINALIZED_RATE_RULE_EVIDENCE"
    snapshot_id: int | None = None
    revision_number: int | None = None
    snapshot_hash: str | None = None
    rate_evidence_available: bool
    report_evidence_available: bool
    report_evidence_version: int | None = None
    report_evidence_hash: str | None = None
    section_availability: dict[str, FinalizedSectionAvailability]
    generated_at_utc: datetime


class FinalizedRatesUsedResponse(BaseModel):
    metadata: FinalizedRatesUsedMetadata
    used_rate_definitions: list[FinalizedUsedRateDefinition] = []
    bonus_events: list[FinalizedBonusEventEvidence] = []


# ---------------------------------------------------------------------------
# P6D: immutable finalized payroll audit/security detail.
# ---------------------------------------------------------------------------

class FinalizedAuditEvent(BaseModel):
    event_id: int
    domain: str
    action_code: str
    source_entity_type: str
    source_entity_id: str
    review_item_id: int | None = None
    driver_id: int | None = None
    work_date: date | None = None
    pay_item_id: int | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None
    actor_user_id: int
    actor_display_name: str
    responsibility_context: dict[str, Any]
    reason: str | None = None
    correlation_id: str | None = None
    source_revision: int | None = None
    occurred_at_utc: datetime
    snapshot_id: int | None = None
    revision_number: int | None = None


class FinalizedWorkflowAuditEvent(BaseModel):
    action_code: str
    actor_user_id: int
    actor_display_name: str
    responsibility_context: dict[str, Any]
    required_permission_code: str
    reason: str | None = None
    action_at_utc: datetime
    snapshot_id: int | None = None
    revision_number: int | None = None
    review_item_id: int | None = None
    review_decision_id: int | None = None


class FinalizedAuditRevisionGroup(BaseModel):
    snapshot_id: int
    revision_number: int
    submit_action: str | None = None
    is_final_approved_revision: bool
    event_ids: list[int] = []
    review_comment_event_ids: list[int] = []


class FinalizedAuditMetadata(BaseModel):
    period_id: int
    period_code: str
    period_name: str
    period_status: str
    branch_id: int
    authority_kind: str = "IMMUTABLE_PERIOD_AUDIT_EVIDENCE"
    snapshot_id: int | None = None
    revision_number: int | None = None
    snapshot_hash: str | None = None
    complete_period_chronology_available: bool
    evidence_version: int | None = None
    section_availability: dict[str, FinalizedSectionAvailability]
    generated_at_utc: datetime


class FinalizedAuditResponse(BaseModel):
    metadata: FinalizedAuditMetadata
    lifecycle_events: list[FinalizedWorkflowAuditEvent] = []
    source_events: list[FinalizedAuditEvent] = []
    status_note_events: list[FinalizedAuditEvent] = []
    bonus_events: list[FinalizedAuditEvent] = []
    review_events: list[FinalizedAuditEvent] = []
    chronology: list[FinalizedAuditEvent] = []
    revision_groups: list[FinalizedAuditRevisionGroup] = []
    rate_rule_provenance: list[dict[str, Any]] = []
