

// ---------------------------------------------------------------------------
// CP-4B — Open/Returned live calculation preview
// ---------------------------------------------------------------------------

export interface CalculationPreviewLine {
  source_type: string;
  source_id: string | null;
  /** Status / system lines only; ordinary lines carry payroll_period_definition_id. */
  line_type: string | null;
  work_date: string | null;
  payroll_period_definition_id: number | null;
  pay_definition_id: number | null;
  definition_name: string | null;
  calculation_status: string | null;
  rate_column_id: number | null;
  driver_assignment_id: number;
  quantity: string | null;
  resolved_rate: string | null;
  calculated_amount: string | null;
  needs_manager_review: boolean;
  blocker_reason: string | null;
}

export interface CalculationPreviewDriver {
  driver_assignment_id: number;
  driver_id: number;
  driver_number: string | null;
  driver_name: string | null;
  daily_pay: string;
  status_pay: string;
  normal_base: string;
  minimum_adjustment: string;
  maximum_adjustment: string;
  bonus_total: string;
  expected_pay: string;
  needs_manager_review: boolean;
  blockers: string[];
  lines: CalculationPreviewLine[];
}

export interface CalculationPreviewResponse {
  currency_code: string | null;
  currency_minor_unit_digits: number | null;
  payroll_period_id: number;
  company_id: number;
  branch_id: number;
  branch_name: string | null;
  status: 'Open' | 'Returned';
  provisional: boolean;
  financials_available: boolean;
  has_blockers: boolean;
  blockers: string[];
  warnings: string[];
  drivers: CalculationPreviewDriver[];
  total_expected_pay: string;
}