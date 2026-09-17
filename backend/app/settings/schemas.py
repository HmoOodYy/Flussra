"""
Pydantic schemas for the settings domain.

Covers:
  - Company profile (read + partial update)
  - Branch administration (full CRUD + metrics)
  - Branch payroll setup (upsert)
  - Payroll status keys (full CRUD)
  - Pay items & branch configuration (read + patch)
  - Custom pay items (M12): catalog CRUD + branch request/approval flow
"""
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from pydantic import BaseModel, field_validator, model_validator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BRANCH_STATUSES = {"Active", "Inactive", "Closed"}

_PAYROLL_FREQUENCIES = {"Week", "Biweek", "Month", "Custom"}

_ALLOWANCE_CATEGORIES = {
    "Vacation", "Sick", "Bereavement", "Jury Duty", "Personal", "Other",
}


# ---------------------------------------------------------------------------
# Company
# ---------------------------------------------------------------------------

class CompanyProfile(BaseModel):
    """Full company profile returned by GET and PATCH /settings/company."""
    company_id: int
    company_code: str
    company_name: str
    legal_name: str | None = None
    status: str
    is_suspended: bool
    timezone_name: str
    notes: str | None = None
    default_branch_id: int | None = None
    default_branch_name: str | None = None
    allow_self_approval: bool = True
    created_at_utc: datetime
    updated_at_utc: datetime | None = None


class CompanyUpdate(BaseModel):
    """
    Fields that may be changed via PATCH /settings/company.

    Status and IsSuspended are system-controlled — they cannot be changed
    through this endpoint.
    """
    company_name: str
    legal_name: str | None = None
    timezone_name: str | None = None
    notes: str | None = None
    allow_self_approval: bool | None = None

    @field_validator("company_name")
    @classmethod
    def company_name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("company_name must not be blank")
        return v.strip()


# ---------------------------------------------------------------------------
# Branch administration
# ---------------------------------------------------------------------------

class BranchAdmin(BaseModel):
    """
    Full branch record with operational metrics.
    Returned by list and single-branch endpoints.
    Metrics can be None when the underlying query is unavailable.
    """
    branch_id: int
    company_id: int
    branch_code: str
    branch_name: str
    status: str
    is_default: bool
    address_line1: str | None = None
    city: str | None = None
    state_province: str | None = None
    postal_code: str | None = None
    country: str | None = None
    notes: str | None = None
    created_at_utc: datetime
    updated_at_utc: datetime | None = None
    # Operational metrics (gracefully degraded — None if query failed)
    payroll_setup_done: bool = False
    status_keys_count: int | None = None
    total_people_count: int | None = None
    active_drivers_count: int | None = None
    pending_approvals_count: int | None = None


class BranchCreate(BaseModel):
    """Payload to create a new branch."""
    branch_name: str
    branch_code: str | None = None     # auto-generated from name if omitted
    status: str = "Active"
    is_default: bool = False
    address_line1: str | None = None
    city: str | None = None
    state_province: str | None = None
    postal_code: str | None = None
    country: str | None = None
    notes: str | None = None

    @field_validator("branch_name")
    @classmethod
    def branch_name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("branch_name must not be blank")
        return v.strip()

    @field_validator("branch_code")
    @classmethod
    def branch_code_non_empty(cls, v: str | None) -> str | None:
        # Treat None, "", and whitespace-only as "no explicit code" → auto-generate.
        if v is None or not v.strip():
            return None
        return v.strip().upper()

    @field_validator("status")
    @classmethod
    def status_valid(cls, v: str) -> str:
        if v not in _BRANCH_STATUSES:
            raise ValueError(f"status must be one of {sorted(_BRANCH_STATUSES)}")
        return v


class BranchUpdate(BaseModel):
    """
    Partial branch update — only non-None fields are applied.

    The is_default flag is intentionally excluded; use
    POST /settings/branches/{id}/set-default to promote a branch.
    """
    branch_name: str | None = None
    branch_code: str | None = None
    status: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state_province: str | None = None
    postal_code: str | None = None
    country: str | None = None
    notes: str | None = None

    @field_validator("branch_name")
    @classmethod
    def branch_name_non_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("branch_name must not be blank if provided")
        return v.strip() if v else None

    @field_validator("branch_code")
    @classmethod
    def branch_code_non_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("branch_code must not be blank if provided")
        return v.strip().upper() if v else None

    @field_validator("status")
    @classmethod
    def status_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _BRANCH_STATUSES:
            raise ValueError(f"status must be one of {sorted(_BRANCH_STATUSES)}")
        return v


# ---------------------------------------------------------------------------
# Branch payroll setup
# ---------------------------------------------------------------------------

class PayrollSetup(BaseModel):
    """
    Branch payroll-schedule configuration returned by GET and PUT
    /settings/branches/{id}/payroll-setup.
    """
    settings_id: int
    company_id: int
    branch_id: int
    branch_name: str | None = None
    payroll_frequency: str            # Week | Biweek | Month | Custom
    anchor_start_date: date           # reference date for period calculation
    pay_date_offset_days: int = 0
    pay_day_of_week: int | None = None  # 1=Sun … 7=Sat
    first_pay_date: date | None = None
    include_pay_day_as_work_day: bool = False
    normal_days_off_mask: int | None = None  # bitmask (bit 0=Sun … bit 6=Sat)
    # Custom cadence: inclusive period length in days (required when frequency=Custom)
    custom_interval_days: int | None = None
    is_active: bool = True
    notes: str | None = None
    created_at_utc: datetime
    updated_at_utc: datetime | None = None
    # CP-2A: immutable schedule version bound at last setup update (or backfill).
    schedule_version_id: int | None = None


class PayrollSetupUpsert(BaseModel):
    """
    Payload to create or replace the branch payroll-schedule configuration.

    Sent via PUT /settings/branches/{id}/payroll-setup.
    An existing configuration is fully replaced; missing optional fields
    revert to their defaults.

    Custom frequency:
      Either ``custom_interval_days`` (inclusive period length in days, > 0)
      or ``first_custom_end_date`` (the end date of the first period, from which
      the interval is derived as ``first_custom_end_date - anchor_start_date + 1 day``)
      must be provided.  ``custom_interval_days`` takes precedence if both are sent.
    """
    payroll_frequency: str
    anchor_start_date: date
    pay_day_of_week: int | None = None
    first_pay_date: date | None = None
    include_pay_day_as_work_day: bool = False
    normal_days_off_mask: int | None = None
    notes: str | None = None
    # Custom cadence fields
    custom_interval_days: int | None = None
    first_custom_end_date: date | None = None  # alternative — derive interval from this

    @field_validator("payroll_frequency")
    @classmethod
    def frequency_valid(cls, v: str) -> str:
        if v not in _PAYROLL_FREQUENCIES:
            raise ValueError(
                f"payroll_frequency must be one of {sorted(_PAYROLL_FREQUENCIES)}"
            )
        return v

    @field_validator("pay_day_of_week")
    @classmethod
    def day_of_week_range(cls, v: int | None) -> int | None:
        if v is not None and v not in range(1, 8):
            raise ValueError("pay_day_of_week must be 1 (Sun) through 7 (Sat)")
        return v

    @field_validator("normal_days_off_mask")
    @classmethod
    def mask_range(cls, v: int | None) -> int | None:
        if v is not None and not (0 <= v <= 127):
            raise ValueError("normal_days_off_mask must be 0–127 (7-bit bitmask)")
        return v

    @field_validator("custom_interval_days")
    @classmethod
    def interval_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("custom_interval_days must be a positive integer (≥ 1)")
        return v

    @model_validator(mode="after")
    def validate_custom_cadence(self) -> "PayrollSetupUpsert":
        from datetime import timedelta
        if self.payroll_frequency != "Custom":
            return self

        # Derive interval from first_custom_end_date if not provided directly
        if self.custom_interval_days is None and self.first_custom_end_date is not None:
            delta = (self.first_custom_end_date - self.anchor_start_date).days + 1
            if delta <= 0:
                raise ValueError(
                    "first_custom_end_date must be on or after anchor_start_date"
                )
            self.custom_interval_days = delta

        if self.custom_interval_days is None:
            raise ValueError(
                "custom_interval_days (or first_custom_end_date) is required "
                "when payroll_frequency is 'Custom'"
            )
        return self


# ---------------------------------------------------------------------------
# Payroll status keys
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Status rate columns (CP-2D2)
# ---------------------------------------------------------------------------

class StatusRateColumn(BaseModel):
    """A named rate-column config for status payment, backed by a RateType."""
    status_rate_column_id: int
    company_id: int
    branch_id: int
    rate_type_id: int
    rate_type_code: str
    column_name: str
    is_default: bool
    is_active: bool
    created_at_utc: datetime


class StatusRateColumnCreate(BaseModel):
    """
    Create a custom status rate column.

    The service auto-creates a new company-owned RateType backed by this column.
    Do NOT supply rate_type_id — it is derived internally.
    unit_name defaults to "Hour" (status payment is always hour-based).
    """
    column_name: str
    unit_name: str = "Hour"
    is_default: bool = False

    @field_validator("column_name")
    @classmethod
    def name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("column_name must not be blank")
        return v.strip()

    @field_validator("unit_name")
    @classmethod
    def unit_name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("unit_name must not be blank")
        return v.strip()


class StatusKey(BaseModel):
    """
    A single branch payroll status key — returned by all status-key endpoints.

    key_name      : user-facing label (e.g. "Vacation", "Sick Day").
                    This is what the user creates and edits.
    status_code   : internal code generated by the service (SK_XXXXXXXX).
                    Kept for backward compat; not shown in the UI.
    normalized_status_code : uppercase/normalized version of status_code.
                             Used for the uniqueness constraint.
    display_order : legacy field, kept for DB compat but not exposed in the UI.
    status_rate_column_id : if set, this status key generates a System draft line
                            using HoursValue × driver rate from that column.
                            NULL = no money for this status key.
    """
    status_key_id: int
    company_id: int
    branch_id: int
    key_name: str
    status_code: str
    normalized_status_code: str
    hours_value: Decimal
    is_off_reason: bool
    deducts_from_yearly_allowance: bool
    allowance_category: str | None = None
    is_active: bool
    display_order: int = 0
    status_rate_column_id: int | None = None
    # Usage limits
    limit_uses_per_period_enabled: bool = False
    limit_uses_per_period: int | None = None
    limit_uses_per_driver_enabled: bool = False
    limit_uses_per_driver: int | None = None
    limit_uses_across_drivers_enabled: bool = False
    limit_uses_across_drivers: int | None = None
    limit_uses_per_day_enabled: bool = False
    limit_uses_per_day: int | None = None
    created_at_utc: datetime
    updated_at_utc: datetime | None = None


class StatusKeyCreate(BaseModel):
    """
    Payload to create a new status key for a branch.

    key_name is the only required user-facing field.
    status_code is generated server-side (SK_XXXXXXXX) and must not be sent.
    display_order is managed internally (always 0 for new keys); omit it.
    status_rate_column_id: optional; if set, generates a System payment line
                           on save using HoursValue × driver rate for that column.
    """
    key_name: str
    hours_value: Decimal = Decimal("0")
    is_off_reason: bool = True
    deducts_from_yearly_allowance: bool = False
    allowance_category: str | None = None
    is_active: bool = True
    status_rate_column_id: int | None = None
    # Usage limits
    limit_uses_per_period_enabled: bool = False
    limit_uses_per_period: int | None = None
    limit_uses_per_driver_enabled: bool = False
    limit_uses_per_driver: int | None = None
    limit_uses_across_drivers_enabled: bool = False
    limit_uses_across_drivers: int | None = None
    limit_uses_per_day_enabled: bool = False
    limit_uses_per_day: int | None = None

    @field_validator("key_name")
    @classmethod
    def name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("key_name must not be blank")
        return v.strip()

    @field_validator("hours_value")
    @classmethod
    def hours_in_range(cls, v: Decimal) -> Decimal:
        if not (Decimal("0") <= v <= Decimal("24")):
            raise ValueError("hours_value must be between 0 and 24")
        return v

    @field_validator("deducts_from_yearly_allowance")
    @classmethod
    def yearly_allowance_not_available(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "Yearly allowance tracking is not available yet."
            )
        return v

    @field_validator("allowance_category")
    @classmethod
    def category_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _ALLOWANCE_CATEGORIES:
            raise ValueError(
                f"allowance_category must be one of {sorted(_ALLOWANCE_CATEGORIES)}"
            )
        return v

    @field_validator("limit_uses_per_period")
    @classmethod
    def period_limit_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("limit_uses_per_period must be a positive integer")
        return v

    @field_validator("limit_uses_per_driver")
    @classmethod
    def driver_limit_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("limit_uses_per_driver must be a positive integer")
        return v

    @field_validator("limit_uses_across_drivers")
    @classmethod
    def across_drivers_limit_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("limit_uses_across_drivers must be a positive integer")
        return v

    @field_validator("limit_uses_per_day")
    @classmethod
    def day_limit_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("limit_uses_per_day must be a positive integer")
        return v


class StatusKeyUpdate(BaseModel):
    """
    Partial status-key update — only non-None fields are applied.
    Sent via PATCH /settings/branches/{id}/status-keys/{key_id}.

    key_name is the user-editable label.
    status_code is immutable after creation (SK_ code never changes).
    display_order is legacy; it can be patched for backward compat but is not
    exposed in the UI.
    status_rate_column_id: use -1 as sentinel to clear (set to NULL).
    """
    key_name: str | None = None
    hours_value: Decimal | None = None
    is_off_reason: bool | None = None
    deducts_from_yearly_allowance: bool | None = None
    allowance_category: str | None = None
    is_active: bool | None = None
    display_order: int | None = None   # legacy, kept for backward compat
    status_rate_column_id: int | None = None  # -1 = clear to NULL
    # Usage limits
    limit_uses_per_period_enabled: bool | None = None
    limit_uses_per_period: int | None = None
    limit_uses_per_driver_enabled: bool | None = None
    limit_uses_per_driver: int | None = None
    limit_uses_across_drivers_enabled: bool | None = None
    limit_uses_across_drivers: int | None = None
    limit_uses_per_day_enabled: bool | None = None
    limit_uses_per_day: int | None = None

    @field_validator("key_name")
    @classmethod
    def name_non_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("key_name must not be blank if provided")
        return v.strip() if v else None

    @field_validator("hours_value")
    @classmethod
    def hours_in_range(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and not (Decimal("0") <= v <= Decimal("24")):
            raise ValueError("hours_value must be between 0 and 24")
        return v

    @field_validator("deducts_from_yearly_allowance")
    @classmethod
    def yearly_allowance_not_available(cls, v: bool | None) -> bool | None:
        if v:
            raise ValueError(
                "Yearly allowance tracking is not available yet."
            )
        return v

    @field_validator("allowance_category")
    @classmethod
    def category_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _ALLOWANCE_CATEGORIES:
            raise ValueError(
                f"allowance_category must be one of {sorted(_ALLOWANCE_CATEGORIES)}"
            )
        return v

    @field_validator("limit_uses_per_period")
    @classmethod
    def period_limit_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("limit_uses_per_period must be a positive integer")
        return v

    @field_validator("limit_uses_per_driver")
    @classmethod
    def driver_limit_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("limit_uses_per_driver must be a positive integer")
        return v

    @field_validator("limit_uses_across_drivers")
    @classmethod
    def across_drivers_limit_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("limit_uses_across_drivers must be a positive integer")
        return v

    @field_validator("limit_uses_per_day")
    @classmethod
    def day_limit_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("limit_uses_per_day must be a positive integer")
        return v


# ---------------------------------------------------------------------------
# Pay items & branch configuration
# ---------------------------------------------------------------------------

class BranchPayItemConfigVersion(BaseModel):
    """
    One effective-dated version of a branch pay item configuration.

    config rows with effective_to=None are the currently-open version.
    closed rows (effective_to set) are historical.
    """
    config_id: int
    is_active: bool
    notes: str | None = None
    effective_from: date
    effective_to: date | None = None   # None = open / in effect
    created_at_utc: datetime


class BranchPayItemState(BaseModel):
    """
    A pay item combined with its branch-level activation state.

    Returned by GET and PATCH /settings/branches/{id}/pay-items endpoints.

    current_config: the open config row whose effective_from <= today.
                    None when no config is yet in effect (using default).
    pending_config: the open config row whose effective_from > today.
                    None when no future change is scheduled.

    At most one open row exists per (company, branch, item) — so
    current_config and pending_config cannot both be non-None.

    is_active / notes are derived convenience fields so callers do not
    need to drill into current_config for the most common case.
    """
    # --- System item fields (platform-managed, read-only) ---
    pay_item_id: int
    pay_item_code: str
    pay_item_name: str
    category: str
    data_type: str
    unit: str | None = None
    sort_order: int
    appears_in_payroll_entry: bool
    appears_in_ledger: bool
    appears_in_reports: bool
    requires_rate: bool
    is_system_standard: bool
    item_scope: str       # Daily | Period | Summary
    rate_behavior: str    # PerUnit | Fixed | Calculated | None
    item_status: str      # Active | Retired

    # --- Derived effective state ---
    is_active: bool       # current_config.is_active  OR  IsDefaultBranchActive
    notes: str | None = None  # current_config.notes   OR  None
    is_using_default: bool    # True when current_config is None

    # --- Structured config versions ---
    current_config: BranchPayItemConfigVersion | None = None
    pending_config: BranchPayItemConfigVersion | None = None

    # --- Catalog relationships (read-only) ---
    line_type_mappings: list[str] = []  # PayrollDraftLines.LineType values
    rate_type_mappings: list[str] = []  # RateType codes

    # --- Write-response warning ---
    # True when the change takes effect today but open payroll periods exist.
    # Always False on GET responses.
    has_open_periods: bool = False


class PayItemConfigUpdate(BaseModel):
    """
    Payload to update a branch's configuration for one pay item.
    Sent via PATCH /settings/branches/{id}/pay-items/{item_id}.

    effective_from: the date from which the new config takes effect.
                    Defaults to today when omitted.
                    Must not be in the past.

    Versioning semantics:
    - If effective_from == existing open row's effective_from → UPDATE in place
      (same-day amendment, no new version created).
    - If effective_from >  existing open row's effective_from → close the current
      open row at (effective_from - 1 day) and INSERT a new open row.
    - If effective_from <  existing open row's effective_from (pending future
      change) → replace the pending row's effective_from and values in place.
    - No open row → INSERT a new open row.
    """
    is_active: bool
    notes: str | None = None
    effective_from: date | None = None

    @field_validator("effective_from")
    @classmethod
    def not_in_past(cls, v: date | None) -> date | None:
        from datetime import date as _d
        if v is not None and v < _d.today():
            raise ValueError("effective_from cannot be in the past")
        return v


# ---------------------------------------------------------------------------
# Bulk branch pay item configuration (M12)
# ---------------------------------------------------------------------------

class BulkPayItemTarget(str, Enum):
    """Whether the bulk update applies to all active branches or a selected subset."""
    AllBranches      = "AllBranches"
    SelectedBranches = "SelectedBranches"


class BulkPayItemConfigUpdate(BaseModel):
    """
    Request body for PATCH /settings/branches/pay-items/{item_id}/bulk-config.

    Applies a single pay item configuration change atomically across one or
    more branches within the same transaction.  Either all target branches are
    updated or none are (validation fails fast before any write occurs).

    target:
      - AllBranches      — every active branch in the company
      - SelectedBranches — only the branch_ids listed; must be non-empty and all
                           must belong to this company

    effective_from semantics follow the single-branch PATCH rules:
      - null → backend schedules safely (today, or day after any open period)
      - explicit date → validated per-branch; rejected if inside any open period
    """
    target: BulkPayItemTarget
    branch_ids: list[int] | None = None
    is_active: bool
    notes: str | None = None
    effective_from: date | None = None

    @field_validator("effective_from")
    @classmethod
    def not_in_past(cls, v: date | None) -> date | None:
        from datetime import date as _d
        if v is not None and v < _d.today():
            raise ValueError("effective_from cannot be in the past")
        return v

    @model_validator(mode="after")
    def check_branch_ids(self) -> "BulkPayItemConfigUpdate":
        if self.target == BulkPayItemTarget.AllBranches:
            if self.branch_ids is not None:
                raise ValueError(
                    "branch_ids must be omitted (or null) when target=AllBranches"
                )
        elif self.target == BulkPayItemTarget.SelectedBranches:
            if not self.branch_ids:
                raise ValueError(
                    "branch_ids must be provided and non-empty when "
                    "target=SelectedBranches"
                )
            # Deduplicate while preserving order
            seen: set[int] = set()
            deduped: list[int] = []
            for bid in self.branch_ids:
                if bid not in seen:
                    seen.add(bid)
                    deduped.append(bid)
            self.branch_ids = deduped
        return self


class BulkPayItemBranchResult(BaseModel):
    """Outcome for a single branch within a bulk config update."""
    branch_id:    int
    branch_name:  str
    status:       Literal["Created", "Updated", "Versioned"]
    config_id:    int
    effective_from: date


class BulkPayItemConfigResult(BaseModel):
    """
    Response for PATCH /settings/branches/pay-items/{item_id}/bulk-config.
    All requested_branch_count branches were updated (updated_branch_count == requested).
    """
    pay_item_id:            int
    pay_item_code:          str
    target:                 str
    requested_branch_count: int
    updated_branch_count:   int
    results:                list[BulkPayItemBranchResult]


# ---------------------------------------------------------------------------
# Custom Pay Items (M12)
# ---------------------------------------------------------------------------

# Valid ItemScope and RateBehavior combos.
# M13c adds OrdinalTier, RangeBracket, RangeProgressive, Block for Daily items.
_M12_ITEM_SCOPES     = {"Daily", "Period"}
# All valid rate behaviors across M12 + M13c.
_VALID_RATE_BEHAVIORS = {"PerUnit", "EnteredAmount", "Fixed", "Calculated", "None",
                         "OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
# Daily items may use any rate behavior except EnteredAmount.
_DAILY_RATE_BEHAVIORS = {"PerUnit", "OrdinalTier", "RangeBracket", "RangeProgressive", "Block"}
# Legacy name kept for internal backward-compat reference.
_M12_RATE_BEHAVIORS  = _VALID_RATE_BEHAVIORS
_M12_CUSTOM_STATUSES = {"Active", "Inactive", "Retired"}
_REQUEST_STATUSES    = {"PendingApproval", "Approved", "Rejected"}
_DECISION_VALUES     = {"Approved", "Rejected"}


class CustomPayItem(BaseModel):
    """
    A company-level custom pay item — returned by admin catalog endpoints.

    item_scope     : 'Daily'  (appears in daily entry, requires WorkDate)
                   | 'Period' (applies to whole period, no WorkDate)
    rate_behavior  : 'PerUnit'          (quantity × single rate)
                   | 'EnteredAmount'    (user enters dollar directly — Period Money only)
                   | 'OrdinalTier'      (different rate by item number: 1st/2nd/3rd+)
                   | 'Block'            (pay by blocks, each block has a rate)
                   | 'RangeBracket'     (total falls into one bracket, that rate applies)
                   | 'RangeProgressive' (progressive tiers, each tier has a rate)
    status         : 'Active' | 'Inactive' | 'Retired'
                     Retired items are hidden from normal lists; history is preserved.
    value_type     : wizard-captured value type — 'Time' | 'Number' | 'Money' | None
                     Stored as datatype ('Time', 'Decimal', 'Currency') in the DB.
    rate_names     : ordered list of pay rate names configured at creation time.
                     Stored in payitemsettings (settingkey = rate_name_1, rate_name_2 …).
                     These will become column headers in Pay Rates configuration.
    requesting_branch_id: the branch that originally requested this item via the
                          approval flow, or None for admin-direct creates.
    """
    pay_item_id:          int
    company_id:           int
    pay_item_code:        str
    display_label:        str | None = None
    pay_item_name:        str
    category:             str
    data_type:            str        # 'Time' | 'Decimal' | 'Integer' | 'Currency' | …
    unit:                 str | None = None
    item_scope:           str
    rate_behavior:        str
    status:               str
    sort_order:           int
    appears_in_payroll_entry: bool
    appears_in_ledger:    bool
    appears_in_reports:   bool
    requires_rate:        bool
    is_system_standard:   bool
    requesting_branch_id: int | None = None
    notes:                str | None = None
    created_at_utc:       datetime
    updated_at_utc:       datetime | None = None
    # Wizard-captured metadata (stored in payitemsettings)
    rate_names:           list[str] = []


_VALID_VALUE_TYPES = {"Time", "Number", "Money"}

# value_type → (datatype stored in DB, default unit)
_VALUE_TYPE_DATATYPE_MAP: dict[str, tuple[str, str | None]] = {
    "Time":   ("Time",     "Hour"),
    "Number": ("Decimal",  None),
    "Money":  ("Currency", None),
}


class CustomPayItemCreate(BaseModel):
    """
    Payload for admin-direct custom item creation (wizard or API).

    Wizard path (new):
      value_type controls the user-visible question "what type of value?":
        'Time'   → datatype='Time',    unit='Hour',  requires rate setup
        'Number' → datatype='Decimal', unit=null,    requires rate setup
        'Money'  → datatype='Currency', unit=null,   rate_behavior forced to 'EnteredAmount'
                   (Period items only — direct dollar amount entry)

      When value_type is provided the scope/behavior validator is relaxed:
        - Daily + Time/Number  → any _DAILY_RATE_BEHAVIORS allowed
        - Period + Money       → rate_behavior forced to 'EnteredAmount'
        - Period + Time/Number → any _DAILY_RATE_BEHAVIORS allowed

    Legacy API path (backward compat, value_type=None):
        - Daily  → rate_behavior must be in _DAILY_RATE_BEHAVIORS; unit required
        - Period → rate_behavior must be 'EnteredAmount'

    rate_names: pay rate column names captured in the wizard.
      Stored in payroll.payitemsettings (key = rate_name_1, rate_name_2 …).
      These will be the column headers in the Pay Rates configuration page.
      Phase 2 gap: rate names are persisted but not yet linked to payroll.ratetypes
      (which lacks companyid) — rate-type linkage requires a separate backend step.

    pay_item_code: Optional — backend auto-generates CPI_XXXXXXXX when omitted.
    category:      Optional — defaults to 'Custom'.  Not user-facing.
    sort_order:    Optional — auto-assigned (MAX company sort_order + 10) when omitted.
    """
    pay_item_code:  str | None = None
    display_label:  str | None = None
    pay_item_name:  str
    category:       str = "Custom"
    unit:           str | None = None
    item_scope:     str
    rate_behavior:  str
    sort_order:     int | None = None
    notes:          str | None = None
    # Wizard fields
    value_type:     str | None = None          # 'Time' | 'Number' | 'Money'
    rate_names:     list[str] = []

    @field_validator("pay_item_code")
    @classmethod
    def code_non_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None  # will be auto-generated in the service
        v = v.strip().upper()
        if not v:
            raise ValueError("pay_item_code must not be blank if provided")
        return v

    @field_validator("pay_item_name")
    @classmethod
    def name_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("pay_item_name must not be blank")
        return v

    @field_validator("category")
    @classmethod
    def category_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("category must not be blank")
        return v

    @field_validator("value_type")
    @classmethod
    def value_type_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in _VALID_VALUE_TYPES:
            raise ValueError(f"value_type must be one of {sorted(_VALID_VALUE_TYPES)}")
        return v

    @field_validator("item_scope")
    @classmethod
    def scope_valid(cls, v: str) -> str:
        if v not in _M12_ITEM_SCOPES:
            raise ValueError(f"item_scope must be one of {sorted(_M12_ITEM_SCOPES)}")
        return v

    @field_validator("rate_behavior")
    @classmethod
    def behavior_valid(cls, v: str) -> str:
        if v not in _VALID_RATE_BEHAVIORS:
            raise ValueError(
                f"rate_behavior must be one of {sorted(_VALID_RATE_BEHAVIORS)}"
            )
        return v

    @field_validator("rate_names")
    @classmethod
    def rate_names_clean(cls, v: list[str]) -> list[str]:
        return [n.strip() for n in v if n.strip()]

    @model_validator(mode="after")
    def validate_scope_behavior_combo(self) -> "CustomPayItemCreate":
        vt = self.value_type

        # Daily items never accept direct money entry
        if self.item_scope == "Daily":
            if vt == "Money":
                raise ValueError(
                    "Daily items cannot be money-type. "
                    "Choose Time/Hours or Regular Number."
                )
            if self.rate_behavior not in _DAILY_RATE_BEHAVIORS:
                raise ValueError(
                    f"Daily custom items must use one of {sorted(_DAILY_RATE_BEHAVIORS)}"
                )

        elif self.item_scope == "Period":
            raise ValueError(
                "Custom Pay Period items are not supported. "
                "Create a Daily custom item instead."
            )

        # Unit validation for rate-based behaviors
        if self.rate_behavior in _DAILY_RATE_BEHAVIORS:
            if vt == "Time":
                pass   # service will set unit = 'Hour' automatically
            elif not (self.unit and self.unit.strip()):
                if vt is None:
                    # Legacy API: unit was always required for rate-based items
                    raise ValueError("unit is required for rate-based items")
                # vt == 'Number': unit is optional (plain quantity, no unit label)

        # EnteredAmount items have no rate unit
        if self.rate_behavior == "EnteredAmount" and self.unit:
            self.unit = None

        return self


class CustomPayItemUpdate(BaseModel):
    """
    Partial update for a custom pay item (admin only).

    Mutable fields: display_label, pay_item_name, category, unit, sort_order, notes.
    Immutable fields (PayItemCode, ItemScope, RateBehavior) cannot be changed
    after creation because they define the meaning of historical draft/final lines.
    """
    display_label: str | None = None
    pay_item_name: str | None = None
    category:      str | None = None
    unit:          str | None = None
    sort_order:    int | None = None
    notes:         str | None = None

    @field_validator("pay_item_name")
    @classmethod
    def name_non_empty(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("pay_item_name must not be blank if provided")
        return v

    @field_validator("category")
    @classmethod
    def category_non_empty(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("category must not be blank if provided")
        return v


class CustomPayItemUsage(BaseModel):
    """
    Usage check result for smart delete.

    can_physical_delete  : True when no meaningful lines exist, no driver rates exist,
                            and the item is not an approved CDPI definition.
    deletion_would_retire: True when meaningful or final lines exist, driver rates exist,
                            or the item is an approved CDPI definition.
    """
    pay_item_id:                    int
    pay_item_code:                  str
    has_meaningful_usage:           bool
    has_final_lines:                bool
    meaningful_draft_line_count:    int
    final_line_count:               int
    non_meaningful_draft_line_count: int
    driver_rates_count:             int = 0
    has_cdpi_definition:            bool = False
    can_physical_delete:            bool
    deletion_would_retire:          bool


class CustomPayItemDeleteResult(BaseModel):
    """
    Result of the smart delete operation.

    deletion_type : 'physical' — row removed from database
                  | 'retired'  — row kept, Status set to 'Retired'
    pay_item_id   : None when physically deleted (row no longer exists).
    """
    pay_item_id:         int | None = None
    pay_item_code:       str
    deletion_type:       str   # 'physical' | 'retired'
    cleaned_draft_lines: int = 0


# ---------------------------------------------------------------------------
# Custom Pay Item Requests (branch request / admin approval flow)
# ---------------------------------------------------------------------------

class CustomPayItemRequest(BaseModel):
    """
    A branch request for a new custom pay item — returned by request endpoints.
    """
    request_id:             int
    company_id:             int
    requesting_branch_id:   int
    requesting_branch_name: str | None = None
    requested_by_user_id:   int
    requested_by:           str | None = None
    requested_at_utc:       datetime
    pay_item_code:          str
    display_label:          str | None = None
    pay_item_name:          str
    item_scope:             str
    rate_behavior:          str
    category:               str
    unit:                   str | None = None
    notes:                  str | None = None
    sort_order:             int
    status:                 str   # PendingApproval | Approved | Rejected
    decided_by_user_id:     int | None = None
    decided_by:             str | None = None
    decided_at_utc:         datetime | None = None
    decision_reason:        str | None = None
    approved_pay_item_id:   int | None = None


class CustomPayItemRequestCreate(BaseModel):
    """
    Payload for a branch user submitting a new custom pay item request.
    Same M12 validation rules as CustomPayItemCreate apply.
    """
    branch_id:     int
    pay_item_code: str
    display_label: str | None = None
    pay_item_name: str
    category:      str
    unit:          str | None = None
    item_scope:    str
    rate_behavior: str
    sort_order:    int = 0
    notes:         str | None = None

    @field_validator("pay_item_code")
    @classmethod
    def code_non_empty(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("pay_item_code must not be blank")
        return v

    @field_validator("pay_item_name")
    @classmethod
    def name_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("pay_item_name must not be blank")
        return v

    @field_validator("category")
    @classmethod
    def category_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("category must not be blank")
        return v

    @field_validator("item_scope")
    @classmethod
    def scope_valid(cls, v: str) -> str:
        if v not in _M12_ITEM_SCOPES:
            raise ValueError(f"item_scope must be one of {sorted(_M12_ITEM_SCOPES)}")
        return v

    @field_validator("rate_behavior")
    @classmethod
    def behavior_valid(cls, v: str) -> str:
        if v not in _VALID_RATE_BEHAVIORS:
            raise ValueError(
                f"rate_behavior must be one of {sorted(_VALID_RATE_BEHAVIORS)}"
            )
        return v

    @model_validator(mode="after")
    def validate_scope_behavior_combo(self) -> "CustomPayItemRequestCreate":
        if self.item_scope == "Daily" and self.rate_behavior not in _DAILY_RATE_BEHAVIORS:
            raise ValueError(
                f"Daily custom items must use one of {sorted(_DAILY_RATE_BEHAVIORS)}"
            )
        if self.item_scope == "Period":
            raise ValueError(
                "Custom Pay Period items are not supported. "
                "Create a Daily custom item instead."
            )
        if self.rate_behavior in _DAILY_RATE_BEHAVIORS and not (self.unit and self.unit.strip()):
            raise ValueError("unit is required for rate-based items")
        if self.rate_behavior == "EnteredAmount" and self.unit:
            self.unit = None
        return self


class CustomPayItemRequestDecide(BaseModel):
    """
    Admin decision on a pending custom pay item request.
    decision       : 'Approved' | 'Rejected'
    decision_reason: required for Rejected; strongly recommended for Approved.
    """
    decision:        str
    decision_reason: str | None = None

    @field_validator("decision")
    @classmethod
    def decision_valid(cls, v: str) -> str:
        if v not in _DECISION_VALUES:
            raise ValueError(f"decision must be one of {sorted(_DECISION_VALUES)}")
        return v


# ---------------------------------------------------------------------------
# M13: PayItemRateTypeMap — assign a rate type to a custom PerUnit item
# ---------------------------------------------------------------------------

class PayItemRateTypeMapCreate(BaseModel):
    """
    Payload to assign (or replace) a rate type mapping for a custom PerUnit pay item.

    The mapping tells the calculation engine which RateType to use when looking
    up the driver's approved DriverRate for a given PayItem.  Required for
    custom PerUnit items to produce a calculatedamount at draft-line entry time.
    """
    rate_type_id: int
    is_primary: bool = True


class PayItemRateTypeMapSummary(BaseModel):
    """Result of POST /settings/pay-items/{id}/rate-type-map."""
    pay_item_rate_type_map_id: int
    pay_item_id: int
    rate_type_id: int
    rate_code: str
    rate_name: str
    is_primary: bool
    status: str


# ---------------------------------------------------------------------------
# Pay item ordering
# ---------------------------------------------------------------------------

class PayItemOrderEntry(BaseModel):
    """One item's new sort order position."""
    pay_item_id: int
    sort_order:  int


class PayItemOrderUpdate(BaseModel):
    """
    Payload for PATCH /settings/pay-items/order.

    Replaces the sort_order of each listed pay item within the company's catalog.
    All pay_item_ids must be valid and visible to this company (system or custom).
    Duplicate pay_item_id values are rejected.

    Typical usage: send the complete ordered list of all visible items after the
    user has dragged to rearrange them.  Sort orders are not required to be
    contiguous — gaps are allowed (e.g. 10, 20, 30 …).
    """
    items: list[PayItemOrderEntry]

    @model_validator(mode="after")
    def validate_items(self) -> "PayItemOrderUpdate":
        if not self.items:
            raise ValueError("items must not be empty")
        ids = [i.pay_item_id for i in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate pay_item_id values are not allowed")
        return self
