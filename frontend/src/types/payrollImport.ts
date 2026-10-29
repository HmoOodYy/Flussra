export type ImportDateAuthority = 'column' | 'filename' | 'sheet';
export type ImportTransform = 'Direct' | 'Sum';
export type ImportRowMode = 'RejectRepeated' | 'SumRows' | 'FilterThenSum';
export type ImportCategoryDecision = 'Include' | 'Exclude';

export interface ImportSelectorOccurrence {
  duplicate_signature: string | null;
  occurrence_ordinal: number | null;
}

export interface ImportColumnSelector {
  header: string;
  occurrences?: ImportSelectorOccurrence[];
}

export interface ImportHeaderOccurrence extends ImportSelectorOccurrence {
  source_column: number;
  label: string;
  normalized_label: string;
  occurrence_count: number;
  occurrence_id: string;
}

export interface ImportHeaderPage {
  available: boolean;
  layout_id: number;
  offset: number;
  limit: number;
  total: number | null;
  next_offset: number | null;
  items: Array<{
    source_column: number;
    label: string;
    normalized_label: string;
    occurrence_ordinal?: number;
    occurrence_count?: number;
    occurrence_id?: string;
    duplicate_signature?: string;
  }>;
}

export interface ImportPeriodPolicy {
  policy_version: number;
  policy_fingerprint: string;
  company_id: number;
  branch_id: number;
  period_id: number;
  product_policy: {
    max_company_pay_items: number;
    max_branch_input_concepts: number;
    max_import_combined_columns: number;
    max_import_canonical_file_bytes: number;
  };
  import_resource_policy: {
    max_file_bytes: number;
    max_total_file_bytes: number;
    max_files: number;
    max_worksheets_per_file: number;
    max_source_units: number;
    max_layouts: number;
    max_rows_per_unit: number;
    max_columns_per_unit: number;
    max_cells_per_unit: number;
    max_selected_cells: number;
    max_data_rows: number;
    max_candidates: number;
    max_inspection_bytes: number;
    max_response_bytes: number;
    row_page_default: number;
    row_page_max: number;
    issue_page_default: number;
    issue_page_max: number;
    source_page_default: number;
    source_page_max: number;
    source_cell_page_default: number;
    source_cell_page_max: number;
    target_page_default: number;
    target_page_max: number;
    header_page_default: number;
    header_page_max: number;
    category_page_default: number;
    category_page_max: number;
    row_detail_sample: number;
    layout_header_sample: number;
    date_review_day_sample: number;
    max_header_label_chars: number;
    max_mapping_request_bytes: number;
  };
}

export interface ImportSourceUnit {
  source_unit_id: number;
  name: string;
  index: number;
  kind: 'CSV' | 'Worksheet' | string;
  selected: boolean;
  header_row_number: number | null;
  date_order: 'DMY' | 'MDY' | 'YMD' | null;
  date_source: { kind?: ImportDateAuthority; value?: string };
  inspection: Record<string, unknown> & { available: boolean; header_cells?: string[] };
  layout_id: number;
}

export interface ImportSourceFile {
  file_source_id: number;
  ordinal: number;
  name: string;
  type: 'csv' | 'xlsx' | string;
  size_bytes: number;
  sha256: string;
  canonical_data_bytes: number | null;
  content_purged_at: string | null;
  inspection: Record<string, unknown>;
  units: ImportSourceUnit[];
}

export interface ImportRulePayload {
  payload_version: number;
  transform: ImportTransform;
  inputs: ImportColumnSelector[];
  shared_columns: Partial<Record<'DriverNumber' | 'EmployeeKey' | 'DriverName' | 'WorkDate' | 'Filter', ImportColumnSelector>>;
  row_handling?: {
    version: number;
    mode: 'SumRows' | 'FilterThenSum';
    choice_count?: number;
    choices?: Array<{ category: { type: string; value: unknown }; decision: ImportCategoryDecision }>;
    choices_sample?: Array<{ category: { type: string; value: unknown }; decision: ImportCategoryDecision }>;
    choices_truncated?: boolean;
  };
  date_interpretation?: { version: number; profiles: unknown[] };
}

export interface ImportRuleState {
  state: 'head_changed' | 'pending' | 'missing_inputs' | 'reusable' | 'remap_required' | 'unmapped' | string;
  current_head_revision: number;
  current_rule_version_id: number | null;
  pending_rule_version_id: number | null;
  pending_revision: number | null;
  pending_expected_head_revision: number | null;
  pending_rule: ImportRulePayload | null;
  current_rule: ImportRulePayload | null;
  reusable: boolean;
  reason: string;
  repeat_notice?: Record<string, unknown> | null;
}

export interface ImportTarget {
  target: string;
  label: string;
  code?: string;
  input_type?: string;
  unit?: string | null;
  template_header?: string;
  rule_state?: ImportRuleState;
}

export interface ImportLayout {
  layout_id: number;
  structure_fingerprint: string;
  label: string;
  mapping: Record<string, string> | null;
  mapping_fingerprint: string | null;
  inspection: {
    available: boolean;
    header_cells: string[];
    header_count: number | null;
    headed_column_count: number | null;
    duplicate_header_signatures: Array<{ signature: string; count: number; columns: number[] }> | null;
    header_cells_truncated: boolean;
  };
  mapping_suggestions: Record<string, string>;
  source_units: Array<{ source_unit_id: number; file_ordinal: number; file_name: string; unit_name: string; selected: boolean }>;
}

export interface ImportMetadata {
  import_id: number;
  company_id: number;
  branch_id: number;
  period_id: number;
  status: string;
  revision: number;
  period_status: string;
  source_editable: boolean;
  files: ImportSourceFile[];
  layouts: ImportLayout[];
  mapping_targets: ImportTarget[];
  mapping_target_page: { offset: number; limit: number; total: number; next_offset: number | null };
  header_page: ImportHeaderPage | null;
  header_structure: { available: boolean; combined_column_count: number | null; file_count: number; headed_column_counts: Array<{ source_unit_id: number; file_ordinal: number; count: number }> | null };
  preview: {
    mapping_fingerprint: string | null;
    content_fingerprint: string | null;
    plan_fingerprint: string | null;
    counts: Record<string, number> | null;
    summary: Record<string, unknown>;
  };
}

export interface ImportPage<T> {
  revision: number;
  plan_fingerprint: string;
  offset: number;
  limit: number;
  filtered_count: number;
  next_offset: number | null;
  items: T[];
  summary?: Record<string, unknown>;
}

export interface ImportCategoryPage {
  revision: number;
  offset: number;
  limit: number;
  filtered_count: number;
  next_offset: number | null;
  items: ImportCategoryValue[];
  suitable: boolean;
  explanation: string;
  repetition_observed: boolean;
  repeated_occurrence_count: number;
  distinct_count: number;
  observed_distinct_count: number;
  inspected_row_count: number;
  invalid_value_count: number;
  invalid_samples: unknown[];
}

export interface ImportPreviewRow {
  driver_id: number | null;
  driver: Record<string, unknown> & { driver_name?: string; driver_number?: string | null; employee_key?: string | null };
  work_date: string | null;
  error_count: number;
  warning_count: number;
  conflict_count: number;
  values: Record<string, {
    payroll_period_definition_id?: number;
    code?: string;
    name?: string;
    unit?: string;
    candidate_count: number;
    nonblank_candidate_count: number;
    candidates: Array<Record<string, unknown>>;
  }>;
  source_records: Array<Record<string, unknown>>;
  source_record_count: number;
  source_records_sampled: boolean;
}

export interface ImportIssue {
  code: string;
  severity: 'Error' | 'Warning' | 'Conflict' | string;
  message: string;
  work_date?: string | null;
  definition_id?: number | null;
  source_record_id?: number | null;
  [key: string]: unknown;
}

export interface ImportSourceRecord extends Record<string, unknown> {
  source_record_id: number;
  source_row_number: number;
  cells: Record<string, unknown>;
  values_json: Record<string, unknown>;
  row_handling?: Record<string, unknown> | null;
  date_interpretation?: Record<string, unknown> | null;
  category?: { type: string; value: unknown } | null;
  category_decision?: string | null;
  proposed_exclusion?: Record<string, unknown> | null;
  cell_offset: number;
  cell_limit: number;
  cell_count: number;
  next_cell_offset: number | null;
}

export interface ImportCategoryValue {
  category: { type: string; value: unknown };
  count: number;
  decision: ImportCategoryDecision | null;
  [key: string]: unknown;
}

export interface ImportDateReviewPage extends ImportPage<{ date: string }> {
  kind: 'recognized_days' | 'missing_coverage_days' | 'overlap_unit_days';
}

export interface ImportInterpretationRequest {
  expected_revision: number;
  units: Array<{
    source_unit_id: number;
    selected: boolean;
    header_row_number: number;
    date_order: 'DMY' | 'MDY' | 'YMD' | null;
    date_source: { kind: ImportDateAuthority; value?: string };
  }>;
}

export interface ImportMappingRequest {
  expected_revision: number;
  shared_columns: Record<string, ImportColumnSelector>;
  ignored_columns: ImportColumnSelector[];
  targets: Array<{
    target: string;
    transform: ImportTransform;
    inputs: ImportColumnSelector[];
    expected_head_revision: number;
    replace_existing: boolean;
  }>;
}
