"""Payroll bonus and period-eligibility contracts."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, field_validator

# Compatibility metadata: Pydantic models retain the legacy app.payroll.schemas identity.



# ---------------------------------------------------------------------------
# CP-3A — Bonus Events schemas
# ---------------------------------------------------------------------------

class BonusEventResponse(BaseModel):
    """A single canonical bonus event row."""
    __module__ = "app.payroll.schemas"
    bonus_event_id: int
    period_id: int
    company_id: int
    branch_id: int
    driver_assignment_id: int
    amount: Decimal
    reason: str | None
    notes: str | None
    status: str                     # 'Active' | 'Voided'
    data_revision: int
    voided_by_user_id: int | None
    voided_at_utc: datetime | None
    void_reason: str | None
    created_by_user_id: int | None
    created_at_utc: datetime
    updated_by_user_id: int | None
    updated_at_utc: datetime | None



class BonusEventCreate(BaseModel):
    """Create one canonical bonus event."""
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    amount: Decimal
    reason: str | None = None
    notes: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_valid(cls, v: Decimal) -> Decimal:
        return _validate_positive_numeric_18_4(v, amount_label="Bonus amount")



class BonusEventUpdate(BaseModel):
    """Partial update for a bonus event (amount, reason, notes)."""
    __module__ = "app.payroll.schemas"
    amount: Decimal | None = None
    reason: str | None = None
    notes: str | None = None
    data_revision: int | None = None    # optional optimistic-lock value

    @field_validator("amount")
    @classmethod
    def amount_valid(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return None
        return _validate_positive_numeric_18_4(v, amount_label="Bonus amount")



class BonusEventPreviewEntry(BaseModel):
    """One bonus event as it will appear after finalization (preview only)."""
    __module__ = "app.payroll.schemas"
    bonus_event_id: int
    driver_assignment_id: int
    driver_name: str | None
    amount: Decimal
    reason: str | None = None
    notes: str | None = None



class BonusSummaryEvent(BaseModel):
    """One bonus event row inside the zero-inclusive bonus summary (CP-3B1)."""
    __module__ = "app.payroll.schemas"
    bonus_event_id: int
    driver_assignment_id: int
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



class BonusSummaryCapabilities(BaseModel):
    """Backend-owned mutation capabilities for the bonus summary (CP-3B1)."""
    __module__ = "app.payroll.schemas"
    can_create: bool
    can_update: bool
    can_void: bool
    reason_codes: list[str] = []



class BonusSummaryDriver(BaseModel):
    """One eligible driver row in the zero-inclusive bonus summary (CP-3B1).

    The roster comes from the CP-2E period eligibility snapshot — drivers with
    zero bonus events still appear with total_bonus = 0.
    """
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_id: int
    driver_number: str | None
    driver_name: str | None
    eligibility_reason_code: str
    total_bonus: Decimal            # sum of Active events only
    active_event_count: int
    voided_event_count: int
    events: list[BonusSummaryEvent]
    capabilities: BonusSummaryCapabilities



class BonusSummaryResponse(BaseModel):
    """Zero-inclusive bonus summary for a payroll period (CP-3B1)."""
    __module__ = "app.payroll.schemas"
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

# NUMERIC(18,4) supports fourteen integer digits and four fractional digits.
_NUMERIC_18_4_MAX_EXCLUSIVE = Decimal("10") ** 14



def _validate_positive_numeric_18_4(v: Decimal, *, amount_label: str) -> Decimal:
    """Validate a positive monetary amount exactly representable by NUMERIC(18,4)."""
    if not v.is_finite():
        raise ValueError(f"{amount_label} must be a finite number.")
    if v <= 0:
        raise ValueError(f"{amount_label} must be positive (> 0).")
    if abs(v) >= _NUMERIC_18_4_MAX_EXCLUSIVE:
        raise ValueError(f"{amount_label} exceeds the maximum allowed (NUMERIC(18,4)).")

    digits = list(v.as_tuple().digits)
    exponent = v.as_tuple().exponent
    while exponent < -4 and digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if exponent < -4:
        raise ValueError(
            f"{amount_label} may have at most four decimal places (NUMERIC(18,4))."
        )
    return v



_validate_positive_numeric_18_4.__module__ = "app.payroll.schemas"

class BonusBatchItem(BaseModel):
    """One create-only bonus row inside a batch (CP-3B2b)."""
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    amount: Decimal
    reason: str | None = None
    notes: str | None = None

    @field_validator("amount")
    @classmethod
    def amount_valid(cls, v: Decimal) -> Decimal:
        return _validate_positive_numeric_18_4(v, amount_label="Bonus amount")



class BonusBatchCreate(BaseModel):
    """
    Payload for POST /payroll/periods/{period_id}/bonuses/batch (CP-3B2b).

    Create-only: every item creates a separate bonus event.  Duplicate driver
    rows are allowed (each is its own event).  Item order is significant — it
    is part of the request hash, so the same items in a different order form a
    different request for idempotency purposes.
    """
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
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



# ---------------------------------------------------------------------------
# CP-1 — Day Grid schemas
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# CP-2 P1 #1 — Period-eligible drivers (for Bonus driver dropdown)
# ---------------------------------------------------------------------------

class PeriodEligibleDriver(BaseModel):
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_name: str
    driver_number: str | None = None



class PeriodEligibleDriversResponse(BaseModel):
    __module__ = "app.payroll.schemas"
    drivers: list[PeriodEligibleDriver]
