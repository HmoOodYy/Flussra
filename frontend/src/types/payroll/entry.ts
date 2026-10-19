import type { FinalizedSectionAvailability } from './finalizedEvidence';

// ---------------------------------------------------------------------------
// CP-1 — Day Grid types
// ---------------------------------------------------------------------------

/**
 * One ordinary Day Grid column: a frozen period definition. `payroll_period_definition_id`
 * is the identity (value keys and save keys); code and label are display metadata only.
 */
export interface DayGridColumn {
  payroll_period_definition_id: number;
  pay_definition_id: number;
  definition_code: string;
  label: string;
  input_type: 'Decimal' | 'WholeNumber';
  unit: string | null;
  calculation_method: string;
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
  /** Derived live from the effective rate; never a stored amount. */
  calculated_amount: string | null;
  needs_manager_review: boolean;
  calculation_status: string | null;
}

export interface DayGridRow {
  driver_assignment_id: number;
  driver_id: number;
  driver_name: string;
  driver_number: string | null;
  status_key: string | null;
  status_label: string | null;
  is_off: boolean;
  notes: string | null;
  /** Keyed by String(payroll_period_definition_id). */
  values: Record<string, DayGridLineValue>;
}

export interface DayGridQuantityTotal {
  payroll_period_definition_id: number;
  quantity: string;
}

export interface DayGridSummary {
  total_drivers: number;
  worked: number;
  pto: number;
  off: number;
  quantity_totals: DayGridQuantityTotal[];
  // CP-2F: null and financials_available=false for Draft (Prepared) periods —
  // financial truth is not authoritative until the period leaves Draft.
  gross_total: string | null;
  needs_attention: number;
  financials_available: boolean;
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
  // Stage B3 Unit 8C-3: set only for Locked/Archived periods, where row
  // status_key/status_label/is_off come from immutable calculation-snapshot
  // evidence rather than live PayrollStatusKeys. Reuses the same
  // {state, reason_code} shape already established by the P6A Finalized
  // Payroll Information Library contracts. Absent/null for Draft/Open/
  // InReview/Returned/Approved.
  status_evidence?: FinalizedSectionAvailability | null;
}

export interface DayGridSaveRow {
  driver_assignment_id: number;
  /** Keyed by String(payroll_period_definition_id), never by code or name. */
  values: Record<string, string>;
  status_key: string | null;
  notes: string | null;
}

export interface DayGridSaveRequest {
  work_date: string;
  rows: DayGridSaveRow[];
}

// ---------------------------------------------------------------------------
// CP-2.5 — Period-level Drivers Off
// ---------------------------------------------------------------------------

export interface SelectedDayOffDriver {
  driver_assignment_id: number;
  driver_id: number;
  driver_name: string;
  driver_number: string | null;
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