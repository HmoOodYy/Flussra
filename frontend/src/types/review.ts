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

export interface ReviewPayrollSnapshotDriverTotal {
  driver_id: number;
  driver_code_snapshot: string | null;
  driver_name_snapshot: string | null;
  daily_pay: string;
  status_pay: string;
  period_pay: string;
  minimum_adjustment: string;
  maximum_adjustment: string;
  bonus_total: string;
  expected_pay: string;
}

export interface ReviewPayrollSnapshotLine {
  driver_id: number;
  source_type: string;
  line_type: string;
  line_scope: string | null;
  work_date: string | null;
  pay_item_id: number | null;
  quantity: string | null;
  resolved_rate_amount: string | null;
  calculated_amount: string;
}

export interface ReviewPayrollSnapshot {
  review_item_id: number;
  payroll_period_id: number;
  revision_number: number;
  captured_at_utc: string;
  total_expected_pay: string;
  driver_totals: ReviewPayrollSnapshotDriverTotal[];
  lines: ReviewPayrollSnapshotLine[];
}

export interface ReviewDecideRequest {
  decision: string; // 'Approved' | 'Rejected' | 'EditRequested' | 'Comment'
  decision_reason?: string;
}
