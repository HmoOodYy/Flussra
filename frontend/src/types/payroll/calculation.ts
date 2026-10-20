

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

export interface CalculationPreviewComponent {
  /** ORDINARY | STATUS | BONUS | MINIMUM_ADJUSTMENT | MAXIMUM_ADJUSTMENT */
  kind: string;
  payroll_period_definition_id: number | null;
  pay_definition_id: number | null;
  status_rate_column_id: number | null;
  driver_pay_rule_id: number | null;
  pre_rounding_amount: string;
  official_amount: string;
  line_count: number;
  unresolved_line_count: number;
}

export interface CalculationPreviewMinMax {
  /** APPLIED, or why neither rule applied (NO_RULE, NO_SCHEDULED_DAYS, ...). */
  status: string;
  proration_version: string;
  numerator: number | null;
  denominator: number | null;
  base_amount: string;
  minimum_rule_id: number | null;
  minimum_rule_amount: string | null;
  prorated_minimum: string | null;
  maximum_rule_id: number | null;
  maximum_rule_amount: string | null;
  prorated_maximum: string | null;
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
  components?: CalculationPreviewComponent[];
  minmax?: CalculationPreviewMinMax | null;
}

export interface CalculationPreviewResponse {
  currency_code: string | null;
  currency_minor_unit_digits: number | null;
  payable_rounding_version?: string | null;
  minmax_proration_version?: string | null;
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