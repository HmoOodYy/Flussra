"""Immutable finalized payroll ledger and information-library contracts."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from .entry import FullyOffDriverSummary
from .reporting import (
    DefinitionAmount,
    DefinitionQuantity,
    FinalizedSectionAvailability,
    ReportColumn,
    ReportDriver,
)

# Compatibility metadata: Pydantic models retain the legacy app.payroll.schemas identity.



# ---------------------------------------------------------------------------
# Final line schemas (Milestone 5 — finalization / ledger)
# ---------------------------------------------------------------------------

class FinalLineSummary(BaseModel):
    """One locked payroll line as written to PayrollFinalLines: the projection of one official
    component of the approved calculation snapshot. ``component_kind`` (with the component
    identity) is the classification; ``line_type`` is the ledger display vocabulary only.
    Per-day detail stays readable from the immutable snapshot lines."""
    __module__ = "app.payroll.schemas"
    final_line_id: int
    currency_code: str
    currency_minor_unit_digits: int
    period_id: int
    branch_id: int
    driver_assignment_id: int
    driver_id: int
    employee_id: int
    driver_number: str | None = None
    driver_name: str
    line_type: str
    line_scope: str = "Period"
    component_kind: str
    component_label: str | None = None
    payroll_calculation_snapshot_id: int
    payroll_calculation_snapshot_component_id: int
    payroll_period_definition_id: int | None = None
    pay_definition_id: int | None = None
    final_amount: Decimal
    source_type: str
    approved_by_user_id: int | None = None
    approved_at_utc: datetime
    locked_at_utc: datetime | None = None
    notes: str | None = None
    # The frozen provenance of the line: snapshot id, revision, hash, component identity and
    # contributing source ids.
    source_snapshot: dict | None = None



# ---------------------------------------------------------------------------
# P6A: Finalized Payroll Information Library. These contracts are read-only
# projections over FinalLines plus the exact originating immutable snapshot.
# ---------------------------------------------------------------------------

class FinalizedFinancialSummary(BaseModel):
    __module__ = "app.payroll.schemas"
    total_pay: Decimal | None = None
    final_line_count: int
    driver_count: int



class FinalizedSnapshotProvenance(BaseModel):
    __module__ = "app.payroll.schemas"
    snapshot_id: int | None = None
    revision_number: int | None = None
    snapshot_hash: str | None = None
    source_config_hash: str | None = None



class FinalizedPeriodListItem(BaseModel):
    """Minimal navigation item for the ledger-owned finalized-period list."""
    __module__ = "app.payroll.schemas"

    currency_code: str
    currency_minor_unit_digits: int
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
    __module__ = "app.payroll.schemas"
    currency_code: str
    currency_minor_unit_digits: int
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
    __module__ = "app.payroll.schemas"
    currency_code: str
    currency_minor_unit_digits: int
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
    __module__ = "app.payroll.schemas"
    metadata: FinalizedReportMetadata
    columns: list[ReportColumn] = []
    definition_columns: list[ReportColumn] = []
    drivers: list[ReportDriver] = []
    work_totals: list[DefinitionQuantity] = []
    pay_totals: dict[str, Decimal] | None = None
    definition_totals: list[DefinitionAmount] | None = None



class FinalizedOffStatusEntry(BaseModel):
    __module__ = "app.payroll.schemas"
    driver_assignment_id: int
    driver_id: int
    driver_name: str | None = None
    driver_number: str | None = None
    work_date: date
    status_key_id: int
    status_code: str
    status_label: str
    is_off_reason: bool



class FinalizedOffDriversMetadata(BaseModel):
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
    metadata: FinalizedOffDriversMetadata
    total_fully_off_drivers: int
    fully_off_drivers: list[FullyOffDriverSummary] = []
    status_entries: list[FinalizedOffStatusEntry] = []



class FinalizedUsedRateDefinition(BaseModel):
    """A rate the finalized snapshot used, or a Min/Max rule it evaluated, as frozen.

    ``evidence_kind`` is DriverRateSchedule (a target schedule: ``source_type`` DraftLine for an
    ordinary PayDefinition rate, StatusEntryState for a Status rate column rate) or DriverPayRule
    (a Min/Max rule of the frozen evaluation). For a rule
    ``used_rate_definition_id`` is the id of its evidence row.
    """
    __module__ = "app.payroll.schemas"
    used_rate_definition_id: int
    driver_assignment_id: int
    driver_id: int
    driver_name: str | None = None
    driver_number: str | None = None
    evidence_kind: str
    source_type: str
    driver_rate_schedule_id: int | None = None
    rate_definition_id: int | None = None
    pay_definition_id: int | None = None
    rate_component_definition_id: int | None = None
    pay_definition_code: str | None = None
    pay_definition_label: str | None = None
    status_rate_column_id: int | None = None
    status_rate_column_name: str | None = None
    unit_name: str | None = None
    driver_pay_rule_id: int | None = None
    rate_amount: Decimal | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    rate_status: str | None = None
    rule_type: str | None = None
    rule_amount: Decimal | None = None
    rule_status: str | None = None
    rule_prorated_amount: Decimal | None = None
    min_max_status: str | None = None
    definition_fingerprint: str | None = None
    snapshot_line_ids: list[int] = []
    line_use_count: int



class FinalizedBonusEventEvidence(BaseModel):
    __module__ = "app.payroll.schemas"
    bonus_event_id: int
    driver_assignment_id: int
    driver_id: int
    driver_name: str | None = None
    driver_number: str | None = None
    amount: Decimal
    reason: str | None = None
    notes: str | None = None
    data_revision: int
    creator_user_id: int | None = None
    creator_display_name: str | None = None
    created_at_utc: datetime



class FinalizedRatesUsedMetadata(BaseModel):
    __module__ = "app.payroll.schemas"
    currency_code: str
    currency_minor_unit_digits: int
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
    __module__ = "app.payroll.schemas"
    metadata: FinalizedRatesUsedMetadata
    used_rate_definitions: list[FinalizedUsedRateDefinition] = []
    bonus_events: list[FinalizedBonusEventEvidence] = []



# ---------------------------------------------------------------------------
# P6D: immutable finalized payroll audit/security detail.
# ---------------------------------------------------------------------------

class FinalizedAuditEvent(BaseModel):
    __module__ = "app.payroll.schemas"
    event_id: int
    domain: str
    action_code: str
    source_entity_type: str
    source_entity_id: str
    review_item_id: int | None = None
    driver_assignment_id: int | None = None
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
    __module__ = "app.payroll.schemas"
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
    __module__ = "app.payroll.schemas"
    snapshot_id: int
    revision_number: int
    submit_action: str | None = None
    is_final_approved_revision: bool
    event_ids: list[int] = []
    review_comment_event_ids: list[int] = []



class FinalizedAuditMetadata(BaseModel):
    __module__ = "app.payroll.schemas"
    currency_code: str
    currency_minor_unit_digits: int
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
    __module__ = "app.payroll.schemas"
    metadata: FinalizedAuditMetadata
    lifecycle_events: list[FinalizedWorkflowAuditEvent] = []
    source_events: list[FinalizedAuditEvent] = []
    status_note_events: list[FinalizedAuditEvent] = []
    bonus_events: list[FinalizedAuditEvent] = []
    review_events: list[FinalizedAuditEvent] = []
    chronology: list[FinalizedAuditEvent] = []
    revision_groups: list[FinalizedAuditRevisionGroup] = []
    rate_rule_provenance: list[dict[str, Any]] = []
