

// ---------------------------------------------------------------------------
// CP-3A — Canonical bonus events
// ---------------------------------------------------------------------------

export interface BonusEvent {
  bonus_event_id: number;
  driver_assignment_id: number;
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
  voided_by_user_id: number | null;
  voided_at_utc: string | null;
  void_reason: string | null;
  created_by_user_id: number | null;
  created_at_utc: string;
  updated_by_user_id: number | null;
  updated_at_utc: string | null;
}

export interface BonusBatchItem {
  driver_assignment_id: number;
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
  driver_assignment_id: number;
  driver_id: number;
  driver_number: string | null;
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