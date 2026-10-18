"""Payroll calculation-preview and finalization-preview contracts."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from .bonus import BonusEventPreviewEntry

# Compatibility metadata: Pydantic models retain the legacy app.payroll.schemas identity.



# ---------------------------------------------------------------------------
# CP-3A — Finalization Preview (read-only)
# ---------------------------------------------------------------------------

class FinalizationPreviewLine(BaseModel):
    """One draft line as it would appear in FinalLines after finalization."""
    __module__ = "app.payroll.schemas"
    draft_line_id: int | None
    source_key: str
    driver_assignment_id: int
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
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_name: str | None
    adjustment_type: str          # "SYS_MIN_TOPUP" or "SYS_MAX_CAP"
    gross_before: Decimal         # normal_base only — excludes bonus
    adjustment_amount: Decimal    # positive for top-up, negative for cap; computed on gross_before (bonus-free)
    bonus_total: Decimal = Decimal("0")   # this driver's total Active bonus, added after min/max
    final_pay: Decimal            # gross_before + adjustment_amount + bonus_total (this driver's true total)



class FinalizationPreviewDriverTotal(BaseModel):
    """Aggregated pay summary for one driver in the preview.

    CP-3C: gross_pay is normal pay only (daily_pay + status_pay) — bonus is
    never folded into it. sys_adjustment (min/max) is computed from that same
    bonus-free base. bonus_total is added back in only after min/max, so
    final_pay = gross_pay + sys_adjustment + bonus_total.
    """
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_id: int
    driver_number: str | None = None
    driver_name: str | None
    daily_pay: Decimal
    status_pay: Decimal = Decimal("0")
    gross_pay: Decimal            # normal pay only — excludes bonus
    sys_adjustment: Decimal       # sum of SYS_MIN_TOPUP / SYS_MAX_CAP deltas, computed on gross_pay (bonus-free)
    bonus_total: Decimal = Decimal("0")   # sum of Active canonical PayrollBonusEvents for this driver
    final_pay: Decimal            # gross_pay + sys_adjustment + bonus_total
    line_count: int



class FinalizationPreviewResponse(BaseModel):
    """Full read-only preview of what finalize_period would do."""
    __module__ = "app.payroll.schemas"
    currency_code: str
    currency_minor_unit_digits: int
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
    draft_line_count: int        # non-Void draft lines that will be inserted
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
# (PerUnit, canonical Status, bonus, min/max).
# ---------------------------------------------------------------------------

class CalculationPreviewLine(BaseModel):
    """One virtual financial line contributing to a driver's CP-4B total."""
    __module__ = "app.payroll.schemas"
    source_type: str                       # "DraftLine" | "StatusEntryState" | "BonusEvent"
    source_id: str | None
    line_type: str | None = None           # Status / system lines only
    work_date: date | None
    payroll_period_definition_id: int | None = None   # ordinary PayDefinition lines
    pay_definition_id: int | None = None
    definition_name: str | None = None                # display only
    calculation_status: str | None = None
    rate_column_id: int | None = None      # StatusRateColumnID for Status lines only
    driver_assignment_id: int
    quantity: Decimal | None
    resolved_rate: Decimal | None
    calculated_amount: Decimal | None
    needs_manager_review: bool
    blocker_reason: str | None = None



class CalculationPreviewDriverTotal(BaseModel):
    """One driver's provisional expected-pay breakdown."""
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_id: int
    driver_number: str | None = None
    driver_name: str | None
    daily_pay: Decimal
    status_pay: Decimal
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
    __module__ = "app.payroll.schemas"
    currency_code: str | None
    currency_minor_unit_digits: int | None
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
