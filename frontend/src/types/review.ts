/**
 * Review domain types — manager review items and decisions.
 * Mirrors backend app/review/schemas.py.
 */

export interface ReviewDecision {
  review_decision_id: number;
  review_item_id: number;
  decided_by_user_id: number;
  decided_by: string | null;
  decision: string; // 'Approved' | 'Rejected' | 'EditRequested' | 'Comment'
  decision_reason: string | null;
  created_at_utc: string;
}

export interface ReviewItemSummary {
  review_item_id: number;
  company_id: number;
  branch_id: number;
  branch_name: string | null;
  requested_by_user_id: number | null;
  requested_by: string | null;
  request_type: string;
  entity_schema: string | null;
  entity_name: string | null;
  /** For PeriodApproval items: the payroll_period_id as a string */
  entity_id: string | null;
  title: string;
  description: string | null;
  status: string; // 'Pending' | 'Approved' | 'Rejected' | 'EditRequested' | 'Cancelled'
  priority: string;
  created_at_utc: string;
  due_at_utc: string | null;
  final_decision_by_user_id: number | null;
  final_decision_by: string | null;
  final_decision_at_utc: string | null;
  final_decision_reason: string | null;
}

export interface ReviewItemDetail extends ReviewItemSummary {
  old_value_json: string | null;
  new_value_json: string | null;
  decisions: ReviewDecision[];
}

export type ReviewComponentKind =
  | 'ORDINARY'
  | 'STATUS'
  | 'BONUS'
  | 'MINIMUM_ADJUSTMENT'
  | 'MAXIMUM_ADJUSTMENT';

/** One official amount of the frozen snapshot, rounded once from its pre-rounding sum. */
export interface ReviewPayrollSnapshotComponent {
  component_kind: ReviewComponentKind;
  label: string | null;
  payroll_period_definition_id: number | null;
  pay_definition_id: number | null;
  rate_definition_id: number | null;
  calculation_method: string | null;
  calculation_method_version: number | null;
  status_rate_column_id: number | null;
  driver_pay_rule_id: number | null;
  pre_rounding_amount: string;
  official_amount: string;
  source_count: number;
}

export interface ReviewPayrollSnapshotMinMaxRule {
  driver_pay_rule_id: number;
  rule_type: string;
  amount: string;
  rule_status: string;
  effective_from: string;
  effective_to: string | null;
  prorated_amount: string | null;
  calendar_frequency: string;
  calendar_anchor_start_date: string;
  calendar_custom_interval_days: number | null;
}

/** The frozen Min/Max evaluation: explains the adjustments on its own. */
export interface ReviewPayrollSnapshotMinMax {
  status: string;
  selection_date: string;
  proration_version: string;
  numerator: number | null;
  denominator: number | null;
  base_amount: string;
  base_ordinary_amount: string;
  base_status_amount: string;
  excluded_bonus_amount: string;
  counted_work_dates: string[];
  rules: ReviewPayrollSnapshotMinMaxRule[];
}

export interface ReviewPayrollSnapshotDriverTotal {
  driver_assignment_id: number;
  driver_id: number;
  employee_id: number;
  branch_id: number;
  driver_number_snapshot: string | null;
  driver_name_snapshot: string | null;
  daily_pay: string;
  status_pay: string;
  minimum_adjustment: string;
  maximum_adjustment: string;
  bonus_total: string;
  expected_pay: string;
  components: ReviewPayrollSnapshotComponent[];
  min_max: ReviewPayrollSnapshotMinMax | null;
}

export interface ReviewPayrollSnapshotLine {
  driver_assignment_id: number;
  source_type: string;
  source_id: string;
  /** ORDINARY / STATUS / BONUS: the kind of the component the line contributes to. */
  line_type: string;
  line_scope: string | null;
  work_date: string | null;
  payroll_period_definition_id: number | null;
  driver_rate_schedule_id: number | null;
  quantity: string | null;
  resolved_rate_amount: string | null;
  calculated_amount: string;
}

export interface ReviewPayrollSnapshot {
  currency_code: string;
  currency_minor_unit_digits: number;
  review_item_id: number;
  payroll_period_id: number;
  snapshot_id: number;
  revision_number: number;
  snapshot_hash: string;
  calculation_version: string;
  payable_rounding_version: string;
  min_max_proration_version: string;
  period_start_date: string;
  period_end_date: string;
  scheduled_work_dates: string[];
  warnings: string[];
  captured_at_utc: string;
  total_expected_pay: string;
  driver_totals: ReviewPayrollSnapshotDriverTotal[];
  lines: ReviewPayrollSnapshotLine[];
}

export interface ReviewDecideRequest {
  decision: string; // 'Approved' | 'Rejected' | 'EditRequested' | 'Comment'
  decision_reason?: string;
}
