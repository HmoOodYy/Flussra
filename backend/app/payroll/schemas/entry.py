"""Payroll source-entry, day-grid, and Off-driver contracts."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from .reporting import FinalizedSectionAvailability

# Compatibility metadata: Pydantic models retain the legacy app.payroll.schemas identity.



# ---------------------------------------------------------------------------
# Draft line schemas
# ---------------------------------------------------------------------------

_VALID_SOURCE_TYPES   = {"Manual", "Import"}

_VALID_LINE_STATUSES  = {"Active", "NeedsReview", "Rejected", "Void"}


# Financial entry accepts Open and Returned periods; ordinary source entry also
# permits Draft through SOURCE_ENTRY_STATUSES below.
ENTRY_ALLOWED_STATUSES = {"Open", "Returned"}


# Draft (Prepared) allows operational source-entry only — not financial paths.
# Use SOURCE_ENTRY_STATUSES for day-grid save and daily-source DraftLine guards only.
# Do NOT use this constant for Bonus or any financial line creation.
SOURCE_ENTRY_STATUSES = {"Draft", "Open", "Returned"}


# Statuses outside financial-entry editability. Ordinary source mutations use
# SOURCE_ENTRY_STATUSES to permit Prepared (Draft) entry as well.
_WRITE_BLOCKED_STATUSES = {"Draft", "InReview", "Approved", "Locked", "Archived", "Cancelled"}



class DraftLineSummary(BaseModel):
    """One source row.

    Ordinary PayDefinition rows carry ``payroll_period_definition_id`` as their identity.
    Status and notes are exposed through the Day Grid's canonical EntryState contract.
    Money is derived live from the effective DriverRateSchedule, never read from stored
    amount columns.
    """
    __module__ = "app.payroll.schemas"
    draft_line_id: int
    period_id: int
    branch_id: int
    driver_assignment_id: int
    driver_id: int
    driver_number: str | None = None
    driver_name: str
    work_date: date
    payroll_period_definition_id: int | None = None
    pay_definition_id: int | None = None
    definition_code: str | None = None
    definition_name: str | None = None
    input_type: str | None = None
    unit: str | None = None
    calculation_method: str | None = None
    quantity: Decimal
    rate_amount: Decimal | None = None
    calculated_amount: Decimal | None = None
    calculation_status: str | None = None
    source_type: str
    status: str
    needs_manager_review: bool
    notes: str | None = None
    added_by_user_id: int | None = None
    added_at_utc: datetime
    # Where the money of this row comes from (LIVE, FROZEN or UNAVAILABLE) and why it is withheld.
    financial_authority: str | None = None
    financial_authority_reason: str | None = None



class DriverPeriodSummary(BaseModel):
    """Aggregated totals per driver x period definition for one period, from its financial authority."""
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_id: int
    driver_number: str | None = None
    driver_name: str
    period_id: int
    period_name: str
    payroll_period_definition_id: int | None = None
    definition_code: str | None = None
    definition_name: str | None = None
    total_quantity: Decimal
    total_calculated_amount: Decimal | None = None
    line_count: int
    lines_needing_attention: int
    financial_authority: str | None = None
    financial_authority_reason: str | None = None



class DraftLineCreate(BaseModel):
    """Ordinary target source input: a quantity against a period definition.

    Money is never an input: a rate, an amount or a review flag is rejected.
    """
    __module__ = "app.payroll.schemas"
    model_config = ConfigDict(extra="forbid")

    driver_assignment_id: int
    work_date: date
    payroll_period_definition_id: int
    quantity: Decimal = Decimal("0")
    notes: str | None = None
    source_type: str = "Manual"

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
    __module__ = "app.payroll.schemas"
    model_config = ConfigDict(extra="forbid")

    quantity: Decimal | None = None
    notes: str | None = None
    status: str | None = None

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



class DayGridColumn(BaseModel):
    """One ordinary column of the grid: a period definition, keyed by its identity.

    Code and name are display metadata only; they never route a save.
    """
    __module__ = "app.payroll.schemas"
    payroll_period_definition_id: int
    pay_definition_id: int
    definition_code: str
    label: str
    input_type: str
    unit: str | None = None
    calculation_method: str



class DayGridStatusKey(BaseModel):
    __module__ = "app.payroll.schemas"
    status_key_id: int
    key_code: str
    label: str
    is_off_reason: bool
    hours_value: Decimal | None = None
    # Input shortcuts of the Status (the label is its canonical primary name).
    aliases: list[str] = []



class DayGridLineValue(BaseModel):
    __module__ = "app.payroll.schemas"
    line_id: int | None = None
    quantity: str | None = None
    calculated_amount: str | None = None
    needs_manager_review: bool = False
    calculation_status: str | None = None



class DayGridRow(BaseModel):
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_id: int
    driver_name: str
    driver_number: str | None = None
    # The canonical identity (id, technical code) and primary name of the Status the day holds
    # (never the alias that was typed). A client that saves the row unchanged sends the id back
    # as `status_key_id`, so the held Status is kept as the identity it is, not re-read as text.
    status_key_id: int | None = None
    status_key: str | None = None
    status_label: str | None = None
    is_off: bool = False
    notes: str | None = None
    # Keyed by str(payroll_period_definition_id).
    values: dict[str, DayGridLineValue] = {}



class DayGridQuantityTotal(BaseModel):
    __module__ = "app.payroll.schemas"
    payroll_period_definition_id: int
    quantity: str



class DayGridSummary(BaseModel):
    __module__ = "app.payroll.schemas"
    total_drivers: int
    worked: int
    pto: int
    off: int
    quantity_totals: list[DayGridQuantityTotal] = []
    gross_total: str | None = None   # CP-2F: None for Draft (Prepared) periods — financials not available
    needs_attention: int
    financials_available: bool = True  # False for a Draft period or a frozen authority that is unavailable
    # Where the money comes from: LIVE (Open/Returned), FROZEN (the verified snapshot of an
    # InReview/Approved/Locked/Archived period) or UNAVAILABLE, with the reason it is withheld.
    financial_authority: str | None = None
    financial_authority_reason: str | None = None



class DayGridPeriod(BaseModel):
    __module__ = "app.payroll.schemas"
    period_id: int
    period_name: str
    start_date: date
    end_date: date
    pay_date: date | None = None
    status: str
    branch_id: int
    branch_name: str



class DayGridResponse(BaseModel):
    __module__ = "app.payroll.schemas"
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



class PeriodEntryOverviewDay(BaseModel):
    """One work date of the overview: exactly the rows and summary the day grid returns for it."""
    __module__ = "app.payroll.schemas"
    work_date: date
    is_configured_off_day: bool
    rows: list[DayGridRow]
    summary: DayGridSummary



class PeriodEntryOverviewResponse(BaseModel):
    __module__ = "app.payroll.schemas"
    period: DayGridPeriod
    columns: list[DayGridColumn]
    status_keys: list[DayGridStatusKey]
    days: list[PeriodEntryOverviewDay]
    # The same period-level value DayGridResponse carries: set only for Locked/Archived periods.
    status_evidence: FinalizedSectionAvailability | None = None



class DayGridSaveRow(BaseModel):
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    # Keyed by str(payroll_period_definition_id); never by code or name.
    values: dict[str, str] = {}
    # The Status the day is saved with, in one of two distinct forms:
    #  * `status_key_id`: the canonical Status the row already holds (as returned by the grid)
    #    or was resolved to. It is the identity and is used as is: no text is read. When it is
    #    given, `status_key` is ignored.
    #  * `status_key`: Status text newly typed (its primary name, an alias or its code). The
    #    server resolves it Company-wide to exactly one Status.
    # Neither (or blank text) clears the day's Status.
    status_key_id: int | None = None
    status_key: str | None = None
    notes: str | None = None



class DayGridSaveRequest(BaseModel):
    __module__ = "app.payroll.schemas"
    work_date: date
    rows: list[DayGridSaveRow]



# ---------------------------------------------------------------------------
# CP-5B — Official Fully-Off and selected-day Off-driver read contracts
# ---------------------------------------------------------------------------

class FullyOffDriverSummary(BaseModel):
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_id: int
    driver_name: str
    driver_number: str | None = None
    eligible_scheduled_day_count: int
    off_day_count: int



class OffDriversSummaryResponse(BaseModel):
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_id: int
    driver_name: str
    driver_number: str | None = None
    work_date: date
    day_name: str
    status_key_id: int | None = None
    status_code: str | None = None
    status_label: str | None = None
    is_off_reason: bool = True
    has_note: bool
    note: str | None = None



class SelectedDayOffDriversResponse(BaseModel):
    __module__ = "app.payroll.schemas"
    period_id: int
    work_date: date
    day_name: str
    total_count: int
    drivers: list[SelectedDayOffDriver] = []
    # Stage B3 Unit 8C-5: see OffDriversSummaryResponse.status_evidence.
    status_evidence: FinalizedSectionAvailability | None = None
