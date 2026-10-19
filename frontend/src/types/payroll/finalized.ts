import type { PeriodStatus } from './periods';
import type {
  CalculationReportView,
  DefinitionAmount,
  DefinitionQuantity,
  ReportColumn,
  ReportDriver,
} from './reports';
import type { FinalizedSectionAvailability } from './finalizedEvidence';

export interface FinalizedFinancialSummary {
  total_pay: string;
  final_line_count: number;
  driver_count: number;
}

export interface FinalizedSnapshotProvenance {
  snapshot_id: number | null;
  revision_number: number | null;
  snapshot_hash: string | null;
  source_config_hash: string | null;
}

export interface FinalizedPeriodListItem {
  currency_code: string;
  currency_minor_unit_digits: number;
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: PeriodStatus;
  period_type: string;
  branch_id: number;
  branch_name: string;
  start_date: string;
  end_date: string;
  pay_date: string | null;
  finalized_at_utc: string | null;
}

export interface FinalizedOverviewResponse {
  currency_code: string;
  currency_minor_unit_digits: number;
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: string;
  company_id: number;
  branch_id: number;
  branch_name: string;
  finalized_at_utc: string | null;
  finalized_by_user_id: number | null;
  financial_summary: FinalizedFinancialSummary;
  snapshot_provenance: FinalizedSnapshotProvenance;
  section_availability: Record<string, FinalizedSectionAvailability>;
  generated_at_utc: string;
}

export interface FinalizedReportMetadata {
  currency_code: string;
  currency_minor_unit_digits: number;
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: string;
  branch_id: number;
  report_type: string;
  authority_kind: string;
  financials_available: boolean;
  snapshot_id: number | null;
  revision_number: number | null;
  snapshot_hash: string | null;
  report_evidence_available: boolean;
  report_evidence_version: number | null;
  report_evidence_hash: string | null;
  section_availability: Record<string, FinalizedSectionAvailability>;
  generated_at_utc: string;
}

export interface FinalizedCalculationReportResponse {
  metadata: FinalizedReportMetadata;
  columns: ReportColumn[];
  definition_columns: ReportColumn[];
  drivers: ReportDriver[];
  work_totals: DefinitionQuantity[];
  pay_totals: Record<string, string> | null;
  definition_totals: DefinitionAmount[] | null;
}

export type FinalizedReportView = CalculationReportView;

export interface FinalizedFullyOffDriver {
  driver_assignment_id: number;
  driver_id: number;
  driver_name: string;
  driver_number: string | null;
  eligible_scheduled_day_count: number;
  off_day_count: number;
}

export interface FinalizedOffStatusEntry {
  driver_assignment_id: number;
  driver_id: number;
  driver_name: string | null;
  driver_number: string | null;
  work_date: string;
  status_key_id: number;
  status_code: string;
  status_label: string;
  is_off_reason: boolean;
}

export interface FinalizedOffDriversMetadata {
  period_id: number;
  period_code: string;
  period_name: string;
  period_status: string;
  branch_id: number;
  authority_kind: string;
  snapshot_id: number | null;
  revision_number: number | null;
  snapshot_hash: string | null;
  report_evidence_available: boolean;
  report_evidence_version: number | null;
  report_evidence_hash: string | null;
  section_availability: Record<string, FinalizedSectionAvailability>;
  generated_at_utc: string;
}

export interface FinalizedOffDriversResponse {
  metadata: FinalizedOffDriversMetadata;
  total_fully_off_drivers: number;
  fully_off_drivers: FinalizedFullyOffDriver[];
  status_entries: FinalizedOffStatusEntry[];
}

export interface FinalizedUsedRateDefinition {
  used_rate_definition_id: number;
  driver_assignment_id: number;
  driver_id: number;
  driver_name: string | null;
  driver_number: string | null;
  evidence_kind: string;
  source_type: string;
  pay_item_id: number | null;
  pay_item_code: string | null;
  pay_item_label: string | null;
  rate_type_id: number | null;
  rate_type_code: string | null;
  rate_type_name: string | null;
  unit_name: string | null;
  driver_rate_id: number | null;
  driver_pay_rule_id: number | null;
  rate_behavior: string | null;
  rate_amount: string | null;
  effective_from: string | null;
  effective_to: string | null;
  rate_status: string | null;
  block_size: string | null;
  rounding_rule: string | null;
  rule_type: string | null;
  rule_amount: string | null;
  rule_status: string | null;
  definition_fingerprint: string;
  snapshot_line_ids: number[];
  line_use_count: number;
}

export interface FinalizedBonusEventEvidence {
  bonus_event_id: number;
  driver_assignment_id: number;
  driver_id: number;
  driver_name: string | null;
  driver_number: string | null;
  amount: string;
  reason: string | null;
  notes: string | null;
  data_revision: number;
  creator_user_id: number | null;
  creator_display_name: string | null;
  created_at_utc: string;
}

export interface FinalizedRatesUsedMetadata {
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
  rate_evidence_available: boolean;
  report_evidence_available: boolean;
  report_evidence_version: number | null;
  report_evidence_hash: string | null;
  section_availability: Record<string, FinalizedSectionAvailability>;
  generated_at_utc: string;
}

export interface FinalizedRatesUsedResponse {
  metadata: FinalizedRatesUsedMetadata;
  used_rate_definitions: FinalizedUsedRateDefinition[];
  bonus_events: FinalizedBonusEventEvidence[];
}