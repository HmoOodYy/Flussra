

// ---------------------------------------------------------------------------
// CP-4 — Final Lines (Ledger)
// ---------------------------------------------------------------------------

export interface FinalLineSummary {
  currency_code: string;
  currency_minor_unit_digits: number;
  final_line_id: number;
  period_id: number;
  branch_id: number;
  driver_assignment_id: number;
  driver_id: number;
  driver_number: string | null;
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
  driver_assignment_id: number;
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
  driver_assignment_id: number;
  driver_name: string | null;
  adjustment_type: string; // "SYS_MIN_TOPUP" | "SYS_MAX_CAP"
  gross_before: string;
  adjustment_amount: string;
  final_pay: string;
}

export interface FinalizationPreviewDriverTotal {
  driver_assignment_id: number;
  driver_id: number;
  driver_number: string | null;
  driver_name: string | null;
  daily_pay: string;
  status_pay: string;
  gross_pay: string;
  sys_adjustment: string;
  bonus_total: string;
  final_pay: string;
  line_count: number;
}

export interface FinalizationPreviewResponse {
  currency_code: string;
  currency_minor_unit_digits: number;
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