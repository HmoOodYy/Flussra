export type PeriodStatus =
  | 'Draft'
  | 'Open'
  | 'InReview'
  | 'Returned'
  | 'Approved'
  | 'Locked'
  | 'Cancelled'
  | 'Archived';

export interface PeriodSummary {
  payroll_period_id: number;
  branch_id: number;
  branch_name: string;
  parent_period_id: number | null;
  period_code: string;
  period_name: string;
  period_type: string;
  start_date: string;
  end_date: string;
  pay_date: string | null;
  status: PeriodStatus;
  notes: string | null;
  created_by_user_id: number | null;
  created_at_utc: string;
  draft_drivers: number;
  draft_lines: number;
  draft_lines_needing_attention: number;
  final_lines: number;
  // Ledger aggregates (migration 0024 — non-zero only for Locked/Archived)
  final_gross: string;
  final_driver_count: number;
}

export interface PeriodCreate {
  branch_id: number;
  period_type: string;
  start_date: string;
  end_date: string;
  pay_date?: string;
  period_name?: string;
  notes?: string;
}

export const PERIOD_TYPES = ['Week', 'Biweek', 'Month', 'Custom'] as const;

// ── Current workflow and candidate-based period creation ───────────────────

export type PeriodCandidateMode = 'OPEN_CREATION' | 'PREPARED_CREATION';

export interface PeriodCandidate {
  candidate_key: string;
  target_status: 'Open' | 'Draft';
  start_date: string;
  end_date: string;
  period_type: string;
  label: string;
  creatable: boolean;
  blocked_reason: string | null;
}

export interface PeriodCandidatePreview {
  mode: PeriodCandidateMode;
  selected: PeriodCandidate;
  navigation: {
    previous_cursor: string | null;
    next_cursor: string | null;
  };
}

export interface PeriodCreationResult {
  result: 'CREATED' | 'ALREADY_EXISTS';
  payroll_period_id: number;
  branch_id: number;
  period_code: string;
  period_name: string;
  period_type: string;
  start_date: string;
  end_date: string;
  status: PeriodStatus;
  created_at_utc: string | null;
}

export interface WorkflowCapability {
  allowed: boolean;
  reason_code: string | null;
  reason_message: string | null;
}

export interface WorkflowSlotPeriod {
  period_id: number;
  branch_id: number;
  branch_name: string;
  status: PeriodStatus;
  display_status: string;
  period_name: string;
  period_code: string;
  period_type: string;
  start_date: string;
  end_date: string;
  submitted_at_utc: string | null;
  current_return_review_item_id: number | null;
  is_active_workflow_slot: boolean;
  is_read_only: boolean;
  read_only_reason_code: string | null;
  lifecycle_position: number;
}

export interface WorkflowAlert {
  code: string;
  severity: 'blocker' | 'warning' | 'info';
  title: string;
  message: string;
  related_period_id: number | null;
  affected_action_codes: string[];
}

export interface BranchCurrentWorkflow {
  branch_id: number;
  branch_name: string;
  setup_status: 'complete' | 'missing' | 'incomplete' | 'inactive';
  slots: {
    open: WorkflowSlotPeriod | null;
    prepared: WorkflowSlotPeriod | null;
    in_review: WorkflowSlotPeriod | null;
    returned: WorkflowSlotPeriod | null;
  };
  capabilities: {
    can_view_current_workflow: WorkflowCapability;
    can_create_open_candidate: WorkflowCapability;
    can_create_prepared_candidate: WorkflowCapability;
    can_view_candidates: WorkflowCapability;
  };
  alerts: WorkflowAlert[];
}

export interface CurrentWorkflow {
  scope: 'company' | 'branch';
  company_id: number;
  requested_branch_id: number | null;
  branches: BranchCurrentWorkflow[];
}

export interface CurrentPayrollHubMetrics {
  total_eligible_drivers: number;
  working_drivers: number;
  fully_off_drivers: number;
}

export interface CurrentPayrollHubDriverSummary {
  driver_id: number;
  driver_code: string | null;
  driver_name: string | null;
  expected_pay: string;
}

export interface CurrentPayrollHubFinancialSummary {
  authority_kind: 'LIVE';
  total_expected_pay: string;
  normal_pay: string;
  bonus_total: string;
  system_adjustments: string;
  has_blockers: boolean;
  blockers: string[];
  warnings: string[];
  top_drivers: CurrentPayrollHubDriverSummary[];
}

export interface CurrentPayrollHubPeriodSlot extends WorkflowSlotPeriod {
  financials_available: boolean;
  financial_summary: CurrentPayrollHubFinancialSummary | null;
  metrics: CurrentPayrollHubMetrics;
}

export interface CurrentPayrollHubBranch {
  branch_id: number;
  branch_name: string;
  setup_status: BranchCurrentWorkflow['setup_status'];
  slots: {
    open: CurrentPayrollHubPeriodSlot | null;
    prepared: CurrentPayrollHubPeriodSlot | null;
    in_review: CurrentPayrollHubPeriodSlot | null;
    returned: CurrentPayrollHubPeriodSlot | null;
  };
  capabilities: BranchCurrentWorkflow['capabilities'];
  alerts: WorkflowAlert[];
}

export interface CurrentPayrollHub {
  scope: 'company' | 'branch';
  company_id: number;
  requested_branch_id: number | null;
  generated_at_utc: string;
  branches: CurrentPayrollHubBranch[];
}

// ── Draft lines ──────────────────────────────────────────────────────────────

export interface DraftLineCreate {
  driver_id: number;
  work_date: string | null;
  line_type: string;
  quantity: number;
  rate_amount?: number;
  notes?: string;
  source_type: 'Manual';
}

export interface DraftLineUpdate {
  quantity?: number;
  rate_amount?: number | null;
  notes?: string;
}

// ---------------------------------------------------------------------------
// CP-1 — Day Grid types
// ---------------------------------------------------------------------------

export interface DayGridColumn {
  pay_item_code: string;
  label: string;
  rate_behavior: string;
  is_time: boolean;
}

export interface DayGridStatusKey {
  status_key_id: number;
  key_code: string;
  label: string;
  is_off_reason: boolean;
  hours_value: number | null;
}

export interface DayGridLineValue {
  line_id: number | null;
  quantity: string | null;
  calculated_amount: string | null;
  needs_manager_review: boolean;
}

export interface DayGridRow {
  driver_id: number;
  driver_name: string;
  driver_code: string | null;
  status_key: string | null;
  status_label: string | null;
  is_off: boolean;
  notes: string | null;
  values: Record<string, DayGridLineValue>;
}

export interface DayGridSummary {
  total_drivers: number;
  worked: number;
  pto: number;
  off: number;
  total_hours: string;
  total_miles: string;
  gross_total: string;
  needs_attention: number;
}

export interface DayGridPeriod {
  period_id: number;
  period_name: string;
  start_date: string;
  end_date: string;
  pay_date: string | null;
  status: string;
  branch_id: number;
  branch_name: string;
}

export interface DayGridResponse {
  period: DayGridPeriod;
  work_date: string;
  columns: DayGridColumn[];
  status_keys: DayGridStatusKey[];
  rows: DayGridRow[];
  summary: DayGridSummary;
}

export interface DayGridSaveRow {
  driver_id: number;
  values: Record<string, string>;
  status_key: string | null;
  notes: string | null;
}

export interface DayGridSaveRequest {
  work_date: string;
  rows: DayGridSaveRow[];
}

// ---------------------------------------------------------------------------
// CP-3A — Canonical bonus events
// ---------------------------------------------------------------------------

export interface BonusEvent {
  bonus_event_id: number;
  driver_id: number;
  amount: string;
  reason: string | null;
  notes: string | null;
  status: 'Active' | 'Voided';
  data_revision: number;
}

export interface BonusEventResponse extends BonusEvent {
  period_id: number;
  company_id: number;
  branch_id: number;
  source_draft_line_id: number | null;
  voided_by_user_id: number | null;
  voided_at_utc: string | null;
  void_reason: string | null;
  created_by_user_id: number | null;
  created_at_utc: string;
  updated_by_user_id: number | null;
  updated_at_utc: string | null;
}

export interface BonusBatchItem {
  driver_id: number;
  amount: string;
  reason?: string | null;
  notes?: string | null;
}

export interface BonusBatchCreate {
  idempotency_key: string;
  expected_bonus_data_revision: number;
  items: BonusBatchItem[];
}

export interface BonusBatchResponse {
  period_id: number;
  branch_id: number;
  batch_request_id: number;
  idempotency_key: string;
  batch_correlation_id: string;
  expected_bonus_data_revision: number;
  result_bonus_data_revision: number;
  created_event_count: number;
  created_event_ids: number[];
  events: BonusEventResponse[];
  replayed: boolean;
}

export interface BonusSummaryCapabilities {
  can_create: boolean;
  can_update: boolean;
  can_void: boolean;
  reason_codes: string[];
}

export interface BonusSummaryDriver {
  driver_id: number;
  driver_code: string | null;
  driver_name: string | null;
  eligibility_reason_code: string;
  total_bonus: string;
  active_event_count: number;
  voided_event_count: number;
  events: BonusEvent[];
  capabilities: BonusSummaryCapabilities;
}

export interface BonusSummary {
  period_id: number;
  branch_id: number;
  period_status: string;
  eligibility_source: string;
  active_event_count: number;
  active_bonus_total: string;
  bonus_data_revision: number;
  drivers: BonusSummaryDriver[];
}

export interface StatusTransitionRequest {
  status: string;
  notes?: string;
}

// ---------------------------------------------------------------------------
// CP-2 — Status colors
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// CP-2.5 — Period-level Drivers Off
// ---------------------------------------------------------------------------

export interface SelectedDayOffDriver {
  driver_id: number;
  driver_name: string;
  driver_code: string | null;
  work_date: string;
  day_name: string;
  status_key_id: number | null;
  status_code: string | null;
  status_label: string | null;
  is_off_reason: boolean;
  has_note: boolean;
  note: string | null;
}

export interface SelectedDayOffDriversResponse {
  period_id: number;
  work_date: string;
  day_name: string;
  total_count: number;
  drivers: SelectedDayOffDriver[];
}

// ---------------------------------------------------------------------------
// CP-4 — Final Lines (Ledger)
// ---------------------------------------------------------------------------

export interface FinalLineSummary {
  final_line_id: number;
  period_id: number;
  branch_id: number;
  driver_id: number;
  driver_name: string;
  draft_line_id: number | null;
  work_date: string | null;
  line_type: string;
  line_scope: string;
  quantity: string;
  rate_amount: string | null;
  final_amount: string;
  source_type: string;
  approved_by_user_id: number | null;
  approved_at_utc: string;
  locked_at_utc: string | null;
  notes: string | null;
  // CP-4 CDPI audit fields (present when backend returns them)
  pay_item_id: number | null;
  rate_type_id: number | null;
  driver_rate_id: number | null;
  resolved_rate_amount: string | null;
  rate_behavior: string | null;
  source_snapshot: Record<string, unknown> | null;
}

// ---------------------------------------------------------------------------
// CP-3A/CP-3B — Finalization Preview types
// ---------------------------------------------------------------------------

export interface FinalizationPreviewLine {
  draft_line_id: number | null;
  source_key: string;
  driver_id: number;
  driver_name: string | null;
  work_date: string | null;
  line_type: string;
  line_scope: string;
  quantity: string | null;
  rate_amount: string | null;
  calculated_amount: string | null;
  /** Exact approved-snapshot amount that Finalize projects into FinalLines. */
  final_amount: string;
  needs_manager_review: boolean;
}

export interface FinalizationPreviewSysAdjustment {
  driver_id: number;
  driver_name: string | null;
  adjustment_type: string; // "SYS_MIN_TOPUP" | "SYS_MAX_CAP"
  gross_before: string;
  adjustment_amount: string;
  final_pay: string;
}

export interface FinalizationPreviewDriverTotal {
  driver_id: number;
  driver_name: string | null;
  daily_pay: string;
  status_pay: string;
  period_pay: string;
  gross_pay: string;
  sys_adjustment: string;
  bonus_total: string;
  final_pay: string;
  line_count: number;
}

export interface FinalizationPreviewResponse {
  period_id: number;
  period_name: string;
  period_status: string;
  branch_id: number;
  branch_name: string | null;
  can_finalize: boolean;
  blockers: string[];
  warnings: string[];
  driver_totals: FinalizationPreviewDriverTotal[];
  sys_adjustments: FinalizationPreviewSysAdjustment[];
  lines: FinalizationPreviewLine[];
  total_final_gross: string;
  draft_line_count: number;
  sys_adjustment_count: number;
  bonus_event_count: number;
  final_line_count_estimate: number;
  driver_count: number;
}

// ---------------------------------------------------------------------------
// CP-5C — Current Payroll calculation reports
// ---------------------------------------------------------------------------

export type CalculationReportView = 'drivers' | 'period-work' | 'period-pay' | 'mixed';

export interface ReportMetadata {
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: string;
  branch_id: number;
  report_type: string;
  authority_kind: string;
  financials_available: boolean;
  unavailable_reason: string | null;
  snapshot_id: number | null;
  revision_number: number | null;
  snapshot_hash: string | null;
  report_evidence_available: boolean;
  report_evidence_version: number | null;
  report_evidence_hash: string | null;
  blockers: string[];
  warnings: string[];
  generated_at_utc: string;
}

export interface ReportColumn {
  pay_item_id: number;
  code: string;
  label: string;
  category: string;
  data_type: string;
  unit: string | null;
  scope: string;
  sort_order: number;
}

export interface ReportWorkSection {
  daily_rows: Record<string, unknown>[];
  status_entries: Record<string, unknown>[];
  status_summaries: Record<string, unknown>[];
}

export interface ReportPaySection {
  daily_pay: string;
  status_pay: string;
  period_pay: string;
  minimum_adjustment: string;
  maximum_adjustment: string;
  bonus_total: string;
  total_pay: string;
  driver_code: string | null;
  driver_name: string | null;
  financial_lines: Record<string, unknown>[];
}

export interface ReportDriver {
  driver_id: number;
  driver_code: string | null;
  driver_name: string | null;
  work: ReportWorkSection;
  pay: ReportPaySection | null;
  bonus_events: Record<string, unknown>[];
}

export interface CalculationReportResponse {
  metadata: ReportMetadata;
  columns: ReportColumn[];
  drivers: ReportDriver[];
  work_totals: Record<string, string>;
  pay_totals: Record<string, string> | null;
}

// ---------------------------------------------------------------------------
// P6A — Finalized Payroll Information Library
// ---------------------------------------------------------------------------

export type FinalizedAvailabilityState = 'AVAILABLE' | 'EMPTY' | 'PARTIAL' | 'UNAVAILABLE';

export interface FinalizedSectionAvailability {
  state: FinalizedAvailabilityState;
  reason_code: string | null;
}

export interface FinalizedFinancialSummary {
  total_pay: string;
  final_line_count: number;
  driver_count: number;
}

export interface FinalizedSnapshotProvenance {
  snapshot_id: number | null;
  revision_number: number | null;
  snapshot_hash: string | null;
  source_config_hash: string | null;
}

export interface FinalizedPeriodListItem {
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: PeriodStatus;
  period_type: string;
  branch_id: number;
  branch_name: string;
  start_date: string;
  end_date: string;
  pay_date: string | null;
  finalized_at_utc: string | null;
}

export interface FinalizedOverviewResponse {
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: string;
  company_id: number;
  branch_id: number;
  branch_name: string;
  finalized_at_utc: string | null;
  finalized_by_user_id: number | null;
  financial_summary: FinalizedFinancialSummary;
  snapshot_provenance: FinalizedSnapshotProvenance;
  section_availability: Record<string, FinalizedSectionAvailability>;
  generated_at_utc: string;
}

export interface FinalizedReportMetadata {
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: string;
  branch_id: number;
  report_type: string;
  authority_kind: string;
  financials_available: boolean;
  snapshot_id: number | null;
  revision_number: number | null;
  snapshot_hash: string | null;
  report_evidence_available: boolean;
  report_evidence_version: number | null;
  report_evidence_hash: string | null;
  section_availability: Record<string, FinalizedSectionAvailability>;
  generated_at_utc: string;
}

export interface FinalizedCalculationReportResponse {
  metadata: FinalizedReportMetadata;
  columns: ReportColumn[];
  drivers: ReportDriver[];
  work_totals: Record<string, string>;
  pay_totals: Record<string, string> | null;
}

export type FinalizedReportView = CalculationReportView;

export interface FinalizedFullyOffDriver {
  driver_id: number;
  driver_name: string;
  driver_code: string | null;
  eligible_scheduled_day_count: number;
  off_day_count: number;
}

export interface FinalizedOffStatusEntry {
  driver_id: number;
  driver_name: string | null;
  driver_code: string | null;
  work_date: string;
  status_key_id: number;
  status_code: string;
  status_label: string;
  is_off_reason: boolean;
}

export interface FinalizedOffDriversMetadata {
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: string;
  branch_id: number;
  authority_kind: string;
  snapshot_id: number | null;
  revision_number: number | null;
  snapshot_hash: string | null;
  report_evidence_available: boolean;
  report_evidence_version: number | null;
  report_evidence_hash: string | null;
  section_availability: Record<string, FinalizedSectionAvailability>;
  generated_at_utc: string;
}

export interface FinalizedOffDriversResponse {
  metadata: FinalizedOffDriversMetadata;
  total_fully_off_drivers: number;
  fully_off_drivers: FinalizedFullyOffDriver[];
  status_entries: FinalizedOffStatusEntry[];
}

export interface FinalizedUsedRateDefinition {
  used_rate_definition_id: number;
  driver_id: number;
  driver_name: string | null;
  driver_code: string | null;
  evidence_kind: string;
  source_type: string;
  pay_item_id: number | null;
  pay_item_code: string | null;
  pay_item_label: string | null;
  rate_type_id: number | null;
  rate_type_code: string | null;
  rate_type_name: string | null;
  unit_name: string | null;
  driver_rate_id: number | null;
  driver_pay_rule_id: number | null;
  rate_behavior: string | null;
  rate_amount: string | null;
  effective_from: string | null;
  effective_to: string | null;
  rate_status: string | null;
  block_size: string | null;
  rounding_rule: string | null;
  rule_type: string | null;
  rule_amount: string | null;
  rule_status: string | null;
  definition_fingerprint: string;
  snapshot_line_ids: number[];
  line_use_count: number;
}

export interface FinalizedBonusEventEvidence {
  bonus_event_id: number;
  driver_id: number;
  driver_name: string | null;
  driver_code: string | null;
  amount: string;
  reason: string | null;
  notes: string | null;
  data_revision: number;
  creator_user_id: number | null;
  creator_display_name: string | null;
  created_at_utc: string;
}

export interface FinalizedRatesUsedMetadata {
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: string;
  branch_id: number;
  authority_kind: string;
  snapshot_id: number | null;
  revision_number: number | null;
  snapshot_hash: string | null;
  rate_evidence_available: boolean;
  report_evidence_available: boolean;
  report_evidence_version: number | null;
  report_evidence_hash: string | null;
  section_availability: Record<string, FinalizedSectionAvailability>;
  generated_at_utc: string;
}

export interface FinalizedRatesUsedResponse {
  metadata: FinalizedRatesUsedMetadata;
  used_rate_definitions: FinalizedUsedRateDefinition[];
  bonus_events: FinalizedBonusEventEvidence[];
}

export interface FinalizedAuditEvent {
  event_id: number;
  domain: string;
  action_code: string;
  source_entity_type: string;
  source_entity_id: string;
  review_item_id: number | null;
  driver_id: number | null;
  work_date: string | null;
  pay_item_id: number | null;
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
  actor_user_id: number;
  actor_display_name: string;
  responsibility_context: Record<string, unknown>;
  reason: string | null;
  correlation_id: string | null;
  source_revision: number | null;
  occurred_at_utc: string;
  snapshot_id: number | null;
  revision_number: number | null;
}

export interface FinalizedWorkflowAuditEvent {
  action_code: string;
  actor_user_id: number;
  actor_display_name: string;
  responsibility_context: Record<string, unknown>;
  required_permission_code: string;
  reason: string | null;
  action_at_utc: string;
  snapshot_id: number | null;
  revision_number: number | null;
  review_item_id: number | null;
  review_decision_id: number | null;
}

export interface FinalizedAuditRevisionGroup {
  snapshot_id: number;
  revision_number: number;
  submit_action: string | null;
  is_final_approved_revision: boolean;
  event_ids: number[];
  review_comment_event_ids: number[];
}

export interface FinalizedAuditMetadata {
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: string;
  branch_id: number;
  authority_kind: string;
  snapshot_id: number | null;
  revision_number: number | null;
  snapshot_hash: string | null;
  complete_period_chronology_available: boolean;
  evidence_version: number | null;
  section_availability: Record<string, FinalizedSectionAvailability>;
  generated_at_utc: string;
}

export interface FinalizedAuditResponse {
  metadata: FinalizedAuditMetadata;
  lifecycle_events: FinalizedWorkflowAuditEvent[];
  source_events: FinalizedAuditEvent[];
  status_note_events: FinalizedAuditEvent[];
  bonus_events: FinalizedAuditEvent[];
  review_events: FinalizedAuditEvent[];
  chronology: FinalizedAuditEvent[];
  revision_groups: FinalizedAuditRevisionGroup[];
  rate_rule_provenance: Array<Record<string, unknown>>;
}

// ---------------------------------------------------------------------------
// CP-4B — Open/Returned live calculation preview
// ---------------------------------------------------------------------------

export interface CalculationPreviewLine {
  source_type: string;
  source_id: string | null;
  line_type: string;
  work_date: string | null;
  pay_item_id: number | null;
  rate_column_id: number | null;
  driver_id: number;
  quantity: string | null;
  resolved_rate: string | null;
  calculated_amount: string | null;
  needs_manager_review: boolean;
  blocker_reason: string | null;
}

export interface CalculationPreviewDriver {
  driver_id: number;
  driver_name: string | null;
  daily_pay: string;
  status_pay: string;
  period_pay: string;
  normal_base: string;
  minimum_adjustment: string;
  maximum_adjustment: string;
  bonus_total: string;
  expected_pay: string;
  needs_manager_review: boolean;
  blockers: string[];
  lines: CalculationPreviewLine[];
}

export interface CalculationPreviewResponse {
  payroll_period_id: number;
  company_id: number;
  branch_id: number;
  branch_name: string | null;
  status: 'Open' | 'Returned';
  provisional: boolean;
  financials_available: boolean;
  has_blockers: boolean;
  blockers: string[];
  warnings: string[];
  drivers: CalculationPreviewDriver[];
  total_expected_pay: string;
}

export const STATUS_COLORS: Record<PeriodStatus, { bg: string; color: string; border: string }> = {
  Draft:     { bg: '#f9fafb', color: '#374151', border: '#e5e7eb' },
  Open:      { bg: '#eff6ff', color: '#1d4ed8', border: '#bfdbfe' },
  InReview:  { bg: '#fffbeb', color: '#92400e', border: '#fde68a' },
  Returned:  { bg: '#fff7ed', color: '#9a3412', border: '#fed7aa' },
  Approved:  { bg: '#f0fdf4', color: '#166534', border: '#bbf7d0' },
  Locked:    { bg: '#f0fdfa', color: '#0f766e', border: '#99f6e4' },
  Cancelled: { bg: '#fef2f2', color: '#991b1b', border: '#fecaca' },
  Archived:  { bg: '#f5f3ff', color: '#5b21b6', border: '#ddd6fe' },
};
