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
  /** Input shortcuts of the Status; `label` is its canonical primary name. */
  aliases: string[];
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
  /** The canonical identity of the Status the day holds; sent back to keep it unchanged. */
  status_key_id: number | null;
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
  off: number;
  quantity_totals: DayGridQuantityTotal[];
  // CP-2F: null and financials_available=false for Draft (Prepared) periods —
  // financial truth is not authoritative until the period leaves Draft.
  gross_total: string | null;
  needs_attention: number;
  financials_available: boolean;
  financial_authority?: string | null;
  financial_authority_reason?: string | null;
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
  driver_assignment_id: number;
  /** Keyed by String(payroll_period_definition_id), never by code or name. */
  values: Record<string, string>;
  /**
   * The Status the row already holds, as an identity: used as is by the server, which then
   * reads no text. Cleared when new Status text is typed.
   */
  status_key_id: number | null;
  /** Newly typed Status text (name, alias or code); resolved by the server. */
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