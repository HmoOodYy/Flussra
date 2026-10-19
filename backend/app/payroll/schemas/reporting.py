"""Payroll report contracts and shared finalized-section availability metadata."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

# Compatibility metadata: Pydantic models retain the legacy app.payroll.schemas identity.



# Shared {state, reason_code} availability shape -- also used by the P6A
# Finalized Payroll Information Library contracts in finalized.py.
class FinalizedSectionAvailability(BaseModel):
    __module__ = "app.payroll.schemas"
    state: str
    reason_code: str | None = None



# ---------------------------------------------------------------------------
# CP-5C: Calculation reports. These are semantic read models, not export rows.
# ---------------------------------------------------------------------------

class ReportMetadata(BaseModel):
    __module__ = "app.payroll.schemas"
    currency_code: str | None
    currency_minor_unit_digits: int | None
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
    """One frozen period definition column. Code and label are display metadata."""
    __module__ = "app.payroll.schemas"
    payroll_period_definition_id: int
    code: str
    label: str
    input_type: str
    unit: str | None = None
    sort_order: int



class DefinitionAmount(BaseModel):
    __module__ = "app.payroll.schemas"
    payroll_period_definition_id: int
    amount: Decimal



class DefinitionQuantity(BaseModel):
    __module__ = "app.payroll.schemas"
    payroll_period_definition_id: int
    quantity: Decimal



class ReportWorkSection(BaseModel):
    __module__ = "app.payroll.schemas"
    daily_rows: list[dict] = []
    status_entries: list[dict] = []
    status_summaries: list[dict] = []



class ReportPaySection(BaseModel):
    __module__ = "app.payroll.schemas"
    daily_pay: Decimal
    status_pay: Decimal
    minimum_adjustment: Decimal
    maximum_adjustment: Decimal
    bonus_total: Decimal
    total_pay: Decimal
    gross_pay: Decimal
    definition_amounts: list[DefinitionAmount] = []
    driver_number: str | None = None
    driver_name: str | None = None
    financial_lines: list[dict] = []



class ReportDriver(BaseModel):
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_id: int
    driver_number: str | None = None
    driver_name: str | None = None
    work: ReportWorkSection
    pay: ReportPaySection | None = None
    bonus_events: list[dict] = []



class CalculationReportResponse(BaseModel):
    __module__ = "app.payroll.schemas"
    metadata: ReportMetadata
    columns: list[ReportColumn] = []
    definition_columns: list[ReportColumn] = []
    drivers: list[ReportDriver] = []
    work_totals: list[DefinitionQuantity] = []
    pay_totals: dict[str, Decimal] | None = None
    definition_totals: list[DefinitionAmount] | None = None



class DriversReportResponse(CalculationReportResponse):
    __module__ = "app.payroll.schemas"
    pass



class PeriodWorkReportResponse(CalculationReportResponse):
    __module__ = "app.payroll.schemas"
    pass



class PeriodPayReportResponse(CalculationReportResponse):
    __module__ = "app.payroll.schemas"
    pass



class MixedReportResponse(CalculationReportResponse):
    __module__ = "app.payroll.schemas"
    pass
