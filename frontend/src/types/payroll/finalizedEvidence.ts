

// ---------------------------------------------------------------------------
// P6A — Finalized Payroll Information Library
// ---------------------------------------------------------------------------

export type FinalizedAvailabilityState = 'AVAILABLE' | 'EMPTY' | 'PARTIAL' | 'UNAVAILABLE';

export interface FinalizedSectionAvailability {
  state: FinalizedAvailabilityState;
  reason_code: string | null;
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
  currency_code: string;
  currency_minor_unit_digits: number;
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