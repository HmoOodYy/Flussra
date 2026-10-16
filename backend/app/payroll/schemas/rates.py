"""Payroll rate, tier, lookup, matrix, batch, and copy contracts."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# Compatibility metadata: Pydantic models retain the legacy app.payroll.schemas identity.



# ---------------------------------------------------------------------------
# Pay rates — rate type catalog + driver rate matrix (Milestone 6)
# ---------------------------------------------------------------------------

class RateTypeSummary(BaseModel):
    """One entry from payroll.RateTypes — the catalog of what can be rated."""
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
    tier_sequence: int
    from_unit: Decimal    # derived for range tiers; user-provided for ordinal tiers
    to_unit: Decimal | None = None   # NULL = open-ended (last tier only)
    tier_amount: Decimal



class OrdinalTierCreate(BaseModel):
    """
    One tier for OrdinalTier rates.  from_unit and to_unit are 1-based integer
    ordinal positions.  Caller provides both; the service validates contiguity.
    """
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
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



class RateLookupResult(BaseModel):
    """
    Result of GET /payroll/rates/lookup — the single rate that applies to a
    driver+rate_type on a specific work date.

    Both Approved and Superseded rows are searched so that historically-closed
    rates (superseded when a newer rate was approved) are still usable for
    payroll lines whose work date falls inside their effective range.

    ``found`` is False when no rate exists for this driver+type on the given date.
    """
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
    driver_rate_id: int
    amount: Decimal
    effective_from: date
    effective_to: date | None = None
    status: str



class RateMatrixGroup(BaseModel):
    """One rate group in the driver pay-rate matrix.

    rate_source distinguishes PayItem-backed groups from StatusRateColumn groups.
    For PayItem groups: pay_item_id, pay_item_name, rate_behavior are set.
    For StatusRateColumn groups: status_rate_column_id is set; pay_item_* are None.
    """
    __module__ = "app.payroll.schemas"
    group_key: str          # "{pay_item_id}:{rate_type_id}" or "SRC:{src_id}:{rate_type_id}"
    rate_source: str        # "PayItem" | "StatusRateColumn"
    pay_item_id: int | None = None
    pay_item_name: str | None = None
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
    __module__ = "app.payroll.schemas"
    driver_id: int
    driver_name: str
    driver_code: str | None = None
    branch_id: int
    branch_name: str
    as_of: date
    groups: list[RateMatrixGroup]



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
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
    effective_from: date
    include_pay_rules: bool = False

    @model_validator(mode="after")
    def effective_from_not_future_only(self) -> "CopyRatesRequest":
        # No date restriction here — backdating guard runs in the service
        return self



class CopyRatesResult(BaseModel):
    """Response from the copy-from endpoint."""
    __module__ = "app.payroll.schemas"
    target_driver_id: int
    source_driver_id: int
    effective_from: date
    allow_self_approval: bool
    rates_copied: int        # number of rate rows created
    rates_approved: int      # subset that were auto-approved
    rates_pending: int       # subset left as PendingApproval
    pay_rules_copied: int    # 0 if include_pay_rules=False
