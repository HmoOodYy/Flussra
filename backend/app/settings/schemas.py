"""
Pydantic schemas for the settings domain.

Covers:
  - Company profile (read + partial update)
  - Branch administration (full CRUD + metrics)
  - Branch payroll setup (upsert)
  - Payroll status keys (full CRUD)
  - Pay items & branch configuration (read + patch)
  - Company custom pay items: catalog reads, usage and retirement (definition is CDPI)
"""
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, field_validator

from app.payroll_setup.schemas import BoundaryChoicesResponse, SetupResponse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BRANCH_STATUSES = {"Active", "Inactive", "Closed"}

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
    currency_code: str | None = None
    currency_name: str | None = None
    currency_minor_unit_digits: int | None = None
    currency_change_locked: bool = False
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
    currency_code: str | None = None

    @field_validator("currency_code")
    @classmethod
    def normalize_currency_code(cls, value: str | None) -> str | None:
        return value.strip().upper() if value is not None else None

    @field_validator("company_name")
    @classmethod
    def company_name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("company_name must not be blank")
        return v.strip()


class SupportedCurrency(BaseModel):
    currency_code: str
    currency_name: str
    numeric_code: str
    minor_unit_digits: int


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
    # Compatibility readiness flag is derived from canonical Payroll Setup authority.
    payroll_setup_done: bool = False
    # Null unless caller has branch payroll.view/non-driver access or onboarding assign access.
    schedule_readiness_reason: str | None = None
    # Period start date the readiness reason was evaluated against; same visibility as the reason.
    schedule_readiness_date: date | None = None
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
    first_payroll_start_date: date | None = None

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


class OnboardingOptionsResponse(BaseModel):
    """
    Returned by GET /settings/branches/onboarding-options.

    default_setup is the company's current default Payroll Setup, or null when
    none is configured. choices is the canonical boundary-choices navigation
    (nearest valid previous/next first-payroll dates, a suggested date, and
    why an explicit `around` date is invalid) computed against that Setup's
    onboarding window; it is null whenever default_setup is null. An archived
    default Setup still returns choices — with a SETUP_NOT_ACTIVE conflict —
    rather than being special-cased here.
    """
    default_setup: SetupResponse | None
    choices: BoundaryChoicesResponse | None


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
