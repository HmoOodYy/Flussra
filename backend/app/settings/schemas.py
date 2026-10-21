"""
Pydantic schemas for the settings domain.

Covers:
  - Company profile (read + partial update)
  - Branch administration (full CRUD + metrics)
  - Branch payroll setup (upsert)
  - Company Status catalog (keys, aliases), Branch usage limits and Status rate columns
  - Pay items & branch configuration (read + patch)
  - Company custom pay items: catalog reads, usage and retirement (definition is CDPI)
"""
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.payroll_setup.schemas import BoundaryChoicesResponse, SetupResponse
from app.settings.status_names import STATUS_CATEGORIES, STATUS_NAME_MAX_LENGTH

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BRANCH_STATUSES = {"Active", "Inactive", "Closed"}

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
    model_config = ConfigDict(extra="forbid")

    company_name: str
    legal_name: str | None = None
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
    time_zone_id: str
    business_date: date
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
    total_people_count: int | None = None
    active_drivers_count: int | None = None
    pending_approvals_count: int | None = None


class BranchCreate(BaseModel):
    """Payload to create a new branch."""
    branch_name: str
    time_zone_id: str = Field(min_length=1, max_length=100)
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

    @field_validator("time_zone_id")
    @classmethod
    def timezone_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("time_zone_id must not be blank")
        return value.strip()

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
    time_zone_id: str | None = Field(default=None, max_length=100)
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

    @field_validator("time_zone_id", mode="before")
    @classmethod
    def timezone_non_empty(cls, value):
        if value is None:
            raise ValueError("time_zone_id must not be null when provided")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("time_zone_id must not be blank")
        return value.strip()

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
# Status rate columns (Company-owned)
# ---------------------------------------------------------------------------

class StatusRateColumn(BaseModel):
    """A Company-owned Status rate column. It owns one scalar RateDefinition, against which
    each Driver's Status rate values are authored as ordinary rate schedules."""
    status_rate_column_id: int
    company_id: int
    column_name: str
    is_default: bool
    is_active: bool
    rate_definition_id: int
    created_at_utc: datetime
    updated_at_utc: datetime | None = None


class StatusRateColumnCreate(BaseModel):
    column_name: str = Field(max_length=STATUS_NAME_MAX_LENGTH)
    is_default: bool = False

    @field_validator("column_name")
    @classmethod
    def name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("column_name must not be blank")
        return v.strip()


class StatusRateColumnUpdate(BaseModel):
    """Partial update; only the fields sent change."""
    column_name: str | None = Field(default=None, max_length=STATUS_NAME_MAX_LENGTH)
    is_default: bool | None = None
    is_active: bool | None = None

    @field_validator("column_name")
    @classmethod
    def name_non_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("column_name must not be blank if provided")
        return v.strip() if v is not None else None


# ---------------------------------------------------------------------------
# Company Status catalog
# ---------------------------------------------------------------------------

def _check_hours(v: Decimal | None) -> Decimal | None:
    if v is not None and not (Decimal("0") <= v <= Decimal("24")):
        raise ValueError("hours_value must be between 0 and 24")
    return v


def _check_category(v: str | None) -> str | None:
    if v is not None and v not in STATUS_CATEGORIES:
        raise ValueError(f"category must be one of {list(STATUS_CATEGORIES)}")
    return v


def _check_yearly_allowance(v: bool | None) -> bool | None:
    if v:
        raise ValueError("Yearly allowance tracking is not available yet.")
    return v


def _check_aliases(v: list[str] | None) -> list[str] | None:
    if v is None:
        return None
    for alias in v:
        if not alias.strip():
            raise ValueError("an alias must not be blank")
        if len(alias.strip()) > STATUS_NAME_MAX_LENGTH:
            raise ValueError(f"an alias must be at most {STATUS_NAME_MAX_LENGTH} characters")
    return [alias.strip() for alias in v]


class StatusKey(BaseModel):
    """A Company Status.

    key_name      : the canonical primary name every display shows. It is unique among the
                    Company's active Statuses by normalized form.
    aliases       : input shortcuts only. A selection stores the Status, never the alias.
    status_code   : the generated, immutable technical code (SK_XXXXXXXX). Read-only.
    category      : the fixed Category set; groups and reports, never decides pay.
    is_off_reason : the Status counts as a full day off.
    hours_value   : hours a day of this Status stands for.
    status_rate_column_id : the Status rate column that makes the Status monetary
                    (hours_value x the Driver's rate); null means it is not paid.
    is_in_use     : a selection or frozen evidence references it, so hours_value,
                    is_off_reason, category and status_rate_column_id can no longer change.
    """
    status_key_id: int
    company_id: int
    key_name: str
    aliases: list[str] = []
    status_code: str
    category: str
    hours_value: Decimal
    is_off_reason: bool
    deducts_from_yearly_allowance: bool
    is_active: bool
    display_order: int = 0
    status_rate_column_id: int | None = None
    is_in_use: bool = False
    created_at_utc: datetime
    updated_at_utc: datetime | None = None


class StatusKeyCreate(BaseModel):
    """Create a Company Status. The technical code is generated and must not be sent."""
    key_name: str = Field(max_length=STATUS_NAME_MAX_LENGTH)
    category: str
    aliases: list[str] = []
    hours_value: Decimal = Decimal("0")
    is_off_reason: bool = True
    deducts_from_yearly_allowance: bool = False
    is_active: bool = True
    display_order: int = 0
    status_rate_column_id: int | None = None

    @field_validator("key_name")
    @classmethod
    def name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("key_name must not be blank")
        return v.strip()

    @field_validator("category")
    @classmethod
    def category_valid(cls, v: str) -> str:
        return _check_category(v)

    @field_validator("hours_value")
    @classmethod
    def hours_in_range(cls, v: Decimal) -> Decimal:
        return _check_hours(v)

    @field_validator("aliases")
    @classmethod
    def aliases_valid(cls, v: list[str]) -> list[str]:
        return _check_aliases(v)

    @field_validator("deducts_from_yearly_allowance")
    @classmethod
    def yearly_allowance_not_available(cls, v: bool) -> bool:
        return _check_yearly_allowance(v)


class StatusKeyUpdate(BaseModel):
    """Partial Status update. Only the fields sent change; ``aliases`` replaces the whole
    alias set atomically, and ``status_rate_column_id: null`` makes the Status non-monetary."""
    key_name: str | None = Field(default=None, max_length=STATUS_NAME_MAX_LENGTH)
    category: str | None = None
    aliases: list[str] | None = None
    hours_value: Decimal | None = None
    is_off_reason: bool | None = None
    deducts_from_yearly_allowance: bool | None = None
    is_active: bool | None = None
    display_order: int | None = None
    status_rate_column_id: int | None = None

    @field_validator("key_name")
    @classmethod
    def name_non_empty(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("key_name must not be blank if provided")
        return v.strip() if v is not None else None

    @field_validator("category")
    @classmethod
    def category_valid(cls, v: str | None) -> str | None:
        return _check_category(v)

    @field_validator("hours_value")
    @classmethod
    def hours_in_range(cls, v: Decimal | None) -> Decimal | None:
        return _check_hours(v)

    @field_validator("aliases")
    @classmethod
    def aliases_valid(cls, v: list[str] | None) -> list[str] | None:
        return _check_aliases(v)

    @field_validator("deducts_from_yearly_allowance")
    @classmethod
    def yearly_allowance_not_available(cls, v: bool | None) -> bool | None:
        return _check_yearly_allowance(v)


# ---------------------------------------------------------------------------
# Branch usage limits
# ---------------------------------------------------------------------------

class StatusKeyBranchLimits(BaseModel):
    """The four optional hard usage limits of one Status in one Branch. A Status without a
    row has every limit disabled."""
    branch_id: int
    status_key_id: int
    key_name: str
    limit_uses_per_period_enabled: bool = False
    limit_uses_per_period: int | None = None
    limit_uses_per_driver_enabled: bool = False
    limit_uses_per_driver: int | None = None
    limit_uses_across_drivers_enabled: bool = False
    limit_uses_across_drivers: int | None = None
    limit_uses_per_day_enabled: bool = False
    limit_uses_per_day: int | None = None


class StatusKeyBranchLimitsUpdate(BaseModel):
    """Replace the limits of one Status in one Branch. Every limit disabled removes the row."""
    limit_uses_per_period_enabled: bool = False
    limit_uses_per_period: int | None = None
    limit_uses_per_driver_enabled: bool = False
    limit_uses_per_driver: int | None = None
    limit_uses_across_drivers_enabled: bool = False
    limit_uses_across_drivers: int | None = None
    limit_uses_per_day_enabled: bool = False
    limit_uses_per_day: int | None = None

    @field_validator(
        "limit_uses_per_period", "limit_uses_per_driver",
        "limit_uses_across_drivers", "limit_uses_per_day",
    )
    @classmethod
    def limit_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("a usage limit must be a positive integer")
        return v
