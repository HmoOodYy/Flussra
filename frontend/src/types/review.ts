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

export interface ReviewDecideRequest {
  decision: string; // 'Approved' | 'Rejected' | 'EditRequested' | 'Comment'
  decision_reason?: string;
}
