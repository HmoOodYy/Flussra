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
  // CP-1A: set when status='Returned'; cleared on resubmission.
  current_return_review_item_id: number | null;
  draft_drivers: number;
  draft_lines: number;
  draft_lines_needing_attention: number;
  financial_authority?: string | null;
  financial_authority_reason?: string | null;
}

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

// Backend PeriodWorkflowCapabilities (per-period entry inside
// BranchWorkflowCapabilities.periods, keyed by str(period_id)). Present only
// for periods in the Hub's active workflow slot set (Draft/Open/InReview/
// Returned) — Approved (and later) periods have no entry here.
export interface PeriodWorkflowCapabilities {
  can_enter_source: WorkflowCapability;
  can_submit_for_review: WorkflowCapability;
  can_resubmit_returned: WorkflowCapability;
  can_view_review: WorkflowCapability;
  can_cancel: WorkflowCapability;
  can_open_day_grid: WorkflowCapability;
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
    periods: Record<string, PeriodWorkflowCapabilities>;
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
  driver_assignment_id: number;
  driver_id: number;
  driver_number: string | null;
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