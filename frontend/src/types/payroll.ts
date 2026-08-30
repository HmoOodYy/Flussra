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

// ── Next period dates (from branch Payroll Setup) ────────────────────────────

export interface NextPeriodDates {
  branch_id: number;
  period_type: string;
  anchor_start_date: string;
  last_period_end_date: string | null;
  start_date: string | null;      // null when Custom setup is incomplete
  end_date: string | null;        // null when Custom setup is incomplete
  is_custom: boolean;
  custom_interval_days: number | null;
}

// ── Draft lines ──────────────────────────────────────────────────────────────

export interface DraftLineSummary {
  draft_line_id: number;
  period_id: number;
  branch_id: number;
  driver_id: number;
  driver_name: string;
  work_date: string | null;
  line_type: string;
  line_scope: string;
  quantity: string;               // Decimal serialized as string
  rate_amount: string | null;
  calculated_amount: string | null;
  source_type: string;
  status: string;
  needs_manager_review: boolean;
  notes: string | null;
  added_by_user_id: number | null;
  added_at_utc: string;
}

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

// ── Per-driver period summary ────────────────────────────────────────────────

export interface DriverPeriodSummary {
  driver_id: number;
  driver_name: string;
  period_id: number;
  period_name: string;
  line_type: string;
  total_quantity: string;
  total_calculated_amount: string;
  line_count: number;
  lines_needing_attention: number;
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
// CP-2 — Period Pay Line types
// ---------------------------------------------------------------------------

export interface PeriodPayLine {
  draft_line_id: number;
  period_id: number;
  branch_id: number;
  driver_id: number;
  driver_name: string;
  line_type: string;
  line_scope: string;
  work_date: string | null;
  quantity: string;
  rate_amount: string | null;
  calculated_amount: string | null;
  source_type: string;
  status: string;
  needs_manager_review: boolean;
  notes: string | null;
  added_by_user_id: number | null;
  added_at_utc: string;
}

export interface AddPeriodPayLineRequest {
  driver_id: number;
  line_type: string;
  amount: string;
  notes?: string;
}

export interface StatusTransitionRequest {
  status: string;
  notes?: string;
}

// ---------------------------------------------------------------------------
// CP-2 P1 #1 — Period-eligible drivers (stable Bonus dropdown source)
// ---------------------------------------------------------------------------

export interface EligibleDriver {
  driver_id: number;
  driver_name: string;
  driver_code: string | null;
}

export interface EligibleDriversResponse {
  drivers: EligibleDriver[];
}

// ---------------------------------------------------------------------------
// CP-2 — Status colors
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// CP-2.5 — Period-level Drivers Off
// ---------------------------------------------------------------------------

export interface DriversOffEntry {
  driver_id: number;
  driver_name: string;
  driver_code: string | null;
  work_date: string;
  status_key_code: string;
  status_label: string | null;
  notes: string | null;
}

export interface DriversOffResponse {
  period_id: number;
  entries: DriversOffEntry[];
  total_count: number;
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
  /** Backend-computed: COALESCE(calculated_amount, quantity * COALESCE(rate_amount, 0)) */
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
  final_line_count_estimate: number;
  driver_count: number;
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
