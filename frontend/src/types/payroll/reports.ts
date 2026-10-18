

// ---------------------------------------------------------------------------
// CP-5C — Current Payroll calculation reports
// ---------------------------------------------------------------------------

export type CalculationReportView = 'drivers' | 'period-work' | 'period-pay' | 'mixed';

export interface ReportMetadata {
  currency_code: string | null;
  currency_minor_unit_digits: number | null;
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: string;
  branch_id: number;
  report_type: string;
  authority_kind: string;
  financials_available: boolean;
  unavailable_reason: string | null;
  snapshot_id: number | null;
  revision_number: number | null;
  snapshot_hash: string | null;
  report_evidence_available: boolean;
  report_evidence_version: number | null;
  report_evidence_hash: string | null;
  blockers: string[];
  warnings: string[];
  generated_at_utc: string;
}

export interface ReportColumn {
  payroll_period_definition_id: number;
  code: string;
  label: string;
  input_type: string;
  unit: string | null;
  sort_order: number;
}

export interface ReportWorkSection {
  daily_rows: Record<string, unknown>[];
  status_entries: Record<string, unknown>[];
  status_summaries: Record<string, unknown>[];
}

export interface DefinitionAmount {
  payroll_period_definition_id: number;
  amount: string;
}

export interface DefinitionQuantity {
  payroll_period_definition_id: number;
  quantity: string;
}

export interface ReportPaySection {
  daily_pay: string;
  status_pay: string;
  minimum_adjustment: string;
  maximum_adjustment: string;
  bonus_total: string;
  total_pay: string;
  gross_pay: string;
  definition_amounts: DefinitionAmount[];
  driver_number: string | null;
  driver_name: string | null;
  financial_lines: Record<string, unknown>[];
}

export interface ReportDriver {
  driver_assignment_id: number;
  driver_id: number;
  driver_number: string | null;
  driver_name: string | null;
  work: ReportWorkSection;
  pay: ReportPaySection | null;
  bonus_events: Record<string, unknown>[];
}

export interface CalculationReportResponse {
  metadata: ReportMetadata;
  columns: ReportColumn[];
  definition_columns: ReportColumn[];
  drivers: ReportDriver[];
  work_totals: DefinitionQuantity[];
  pay_totals: Record<string, string> | null;
  definition_totals: DefinitionAmount[] | null;
}