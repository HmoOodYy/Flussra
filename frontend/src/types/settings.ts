// ─── Company ──────────────────────────────────────────────────────────────────

export interface CompanyProfile {
  company_id: number;
  company_code: string;
  company_name: string;
  legal_name: string | null;
  status: string;
  is_suspended: boolean;
  timezone_name: string;
  notes: string | null;
  default_branch_id: number | null;
  default_branch_name: string | null;
  allow_self_approval: boolean;
  created_at_utc: string;
  updated_at_utc: string | null;
}

export interface CompanyUpdate {
  company_name: string;
  legal_name?: string | null;
  timezone_name?: string | null;
  notes?: string | null;
  allow_self_approval?: boolean | null;
}

// ─── Branches ─────────────────────────────────────────────────────────────────

export interface BranchAdmin {
  branch_id: number;
  company_id: number;
  branch_code: string;
  branch_name: string;
  status: string;               // Active | Inactive | Closed
  is_default: boolean;
  address_line1: string | null;
  city: string | null;
  state_province: string | null;
  postal_code: string | null;
  country: string | null;
  notes: string | null;
  created_at_utc: string;
  updated_at_utc: string | null;
  // Operational metrics
  payroll_setup_done: boolean;
  status_keys_count: number | null;
  total_people_count: number | null;
  active_drivers_count: number | null;
  pending_approvals_count: number | null;
}

// ─── Payroll Setup ────────────────────────────────────────────────────────────

export interface PayrollSetup {
  settings_id: number;
  company_id: number;
  branch_id: number;
  branch_name: string | null;
  payroll_frequency: string;          // Week | Biweek | Month | Custom
  anchor_start_date: string;          // YYYY-MM-DD
  pay_date_offset_days: number;
  pay_day_of_week: number | null;
  first_pay_date: string | null;
  include_pay_day_as_work_day: boolean;
  normal_days_off_mask: number | null; // 7-bit: bit 0=Sun … bit 6=Sat
  /** Inclusive period length in days for Custom fixed-cadence payroll. */
  custom_interval_days: number | null;
  is_active: boolean;
  notes: string | null;
  created_at_utc: string;
  updated_at_utc: string | null;
}

export interface PayrollSetupUpsert {
  payroll_frequency: string;
  anchor_start_date: string;          // YYYY-MM-DD
  normal_days_off_mask: number | null;
  notes: string | null;
  /** Required when payroll_frequency = 'Custom'. Inclusive cycle length in days. */
  custom_interval_days?: number | null;
  /** Alternative to custom_interval_days: backend derives interval from (end − anchor) + 1. */
  first_custom_end_date?: string | null;
}

// ─── Status Keys ──────────────────────────────────────────────────────────────

export interface StatusKey {
  status_key_id: number;
  company_id: number;
  branch_id: number;
  /** User-facing label ("Vacation", "Sick Day", …). Shown in the UI. */
  key_name: string;
  /** Internal generated code (SK_XXXXXXXX). Not user-editable. */
  status_code: string;
  normalized_status_code: string;
  hours_value: number;               // 0–24
  is_off_reason: boolean;
  deducts_from_yearly_allowance: boolean;
  allowance_category: string | null; // Vacation | Sick | Bereavement | Jury Duty | Personal | Other
  is_active: boolean;
  display_order: number;             // legacy — not exposed in UI
  // Usage limits
  limit_uses_per_period_enabled: boolean;
  limit_uses_per_period: number | null;
  limit_uses_per_driver_enabled: boolean;
  limit_uses_per_driver: number | null;
  limit_uses_across_drivers_enabled: boolean;
  limit_uses_across_drivers: number | null;
  limit_uses_per_day_enabled: boolean;
  limit_uses_per_day: number | null;
  created_at_utc: string;
  updated_at_utc: string | null;
}

export interface StatusKeyCreate {
  /** User-facing label — the only required field from the user. */
  key_name: string;
  hours_value?: number;
  is_off_reason?: boolean;
  deducts_from_yearly_allowance?: boolean;
  allowance_category?: string | null;
  is_active?: boolean;
  // Usage limits
  limit_uses_per_period_enabled?: boolean;
  limit_uses_per_period?: number | null;
  limit_uses_per_driver_enabled?: boolean;
  limit_uses_per_driver?: number | null;
  limit_uses_across_drivers_enabled?: boolean;
  limit_uses_across_drivers?: number | null;
  limit_uses_per_day_enabled?: boolean;
  limit_uses_per_day?: number | null;
}

export interface StatusKeyUpdate {
  key_name?: string | null;
  hours_value?: number | null;
  is_off_reason?: boolean | null;
  deducts_from_yearly_allowance?: boolean | null;
  allowance_category?: string | null;
  is_active?: boolean | null;
  // Usage limits
  limit_uses_per_period_enabled?: boolean | null;
  limit_uses_per_period?: number | null;
  limit_uses_per_driver_enabled?: boolean | null;
  limit_uses_per_driver?: number | null;
  limit_uses_across_drivers_enabled?: boolean | null;
  limit_uses_across_drivers?: number | null;
  limit_uses_per_day_enabled?: boolean | null;
  limit_uses_per_day?: number | null;
}

// ─── Pay Items ────────────────────────────────────────────────────────────────

export interface BranchPayItemConfigVersion {
  config_id: number;
  is_active: boolean;
  notes: string | null;
  effective_from: string;          // YYYY-MM-DD
  effective_to: string | null;     // null = currently in effect
  created_at_utc: string;
}

export interface BranchPayItemState {
  pay_item_id: number;
  pay_item_code: string;
  pay_item_name: string;
  category: string;
  data_type: string;
  unit: string | null;
  sort_order: number;
  appears_in_payroll_entry: boolean;
  appears_in_ledger: boolean;
  appears_in_reports: boolean;
  requires_rate: boolean;
  is_system_standard: boolean;
  item_scope: 'Daily' | 'Period' | 'Summary';
  rate_behavior: string;
  item_status: 'Active' | 'Retired';  // server-side item status
  is_active: boolean;                  // derived: current branch config or system default
  notes: string | null;
  is_using_default: boolean;           // true = no custom config row, using system default
  current_config: BranchPayItemConfigVersion | null;
  pending_config: BranchPayItemConfigVersion | null;
  line_type_mappings: string[];
  rate_type_mappings: string[];
  has_open_periods: boolean;           // only populated on PATCH responses
}

export interface PayItemConfigUpdate {
  is_active: boolean;
  notes: string | null;
  effective_from: string | null;       // null = backend schedules safely (today or after open period)
}

// Bulk branch config update
export type BulkPayItemTarget = 'AllBranches' | 'SelectedBranches';

export interface BulkPayItemConfigUpdate {
  target: BulkPayItemTarget;
  branch_ids: number[] | null;         // required when target=SelectedBranches
  is_active: boolean;
  notes: string | null;
  effective_from: string | null;
}

export interface BulkPayItemBranchResult {
  branch_id: number;
  branch_name: string;
  status: 'Created' | 'Updated' | 'Versioned';
  config_id: number;
  effective_from: string;
}

export interface BulkPayItemConfigResult {
  pay_item_id: number;
  pay_item_code: string;
  target: string;
  requested_branch_count: number;
  updated_branch_count: number;
  results: BulkPayItemBranchResult[];
}

// Custom pay items (company-created, not system standard)
export interface CustomPayItem {
  pay_item_id: number;
  company_id: number;
  pay_item_code: string;               // immutable after creation, auto-generated
  display_label: string | null;
  pay_item_name: string;
  category: string;
  data_type: string;                   // 'Time' | 'Decimal' | 'Integer' | 'Currency'
  unit: string | null;
  item_scope: 'Daily' | 'Period';      // immutable after creation
  rate_behavior: string;               // immutable after creation
  status: 'Active' | 'Inactive' | 'Retired';
  sort_order: number;
  appears_in_payroll_entry: boolean;
  appears_in_ledger: boolean;
  appears_in_reports: boolean;
  requires_rate: boolean;
  is_system_standard: boolean;         // always false for custom items
  requesting_branch_id: number | null;
  notes: string | null;
  created_at_utc: string;
  updated_at_utc: string | null;
  /** Rate column names configured at creation. Stored in payitemsettings. */
  rate_names: string[];
}

export type WizardValueType = 'Time' | 'Number' | 'Money';
export type WizardRateMethod = 'PerUnit';

export interface CustomPayItemCreate {
  /** Optional — backend auto-generates a CPI_ prefixed code when omitted. Not shown in the UI. */
  pay_item_code?: string;
  display_label: string | null;
  pay_item_name: string;
  /** Optional — backend defaults to "Custom". Not shown in the UI. */
  category?: string;
  unit?: string | null;
  item_scope: 'Daily' | 'Period';
  rate_behavior: string;
  /** Optional — backend auto-assigns MAX(sort_order)+10 when omitted. */
  sort_order?: number | null;
  notes: string | null;
  /** Wizard field: value type determines datatype+unit in backend. */
  value_type?: WizardValueType | null;
  /** Pay rate column names — persisted to payitemsettings. */
  rate_names?: string[];
}

// Pay item ordering
export interface PayItemOrderEntry {
  pay_item_id: number;
  sort_order: number;
}

export interface PayItemOrderUpdate {
  items: PayItemOrderEntry[];
}

export interface CustomPayItemUpdate {
  display_label?: string | null;
  pay_item_name?: string | null;
  category?: string | null;
  unit?: string | null;
  sort_order?: number | null;
  notes?: string | null;
}

export interface CustomPayItemUsage {
  pay_item_id: number;
  pay_item_code: string;
  has_meaningful_usage: boolean;
  has_final_lines: boolean;
  meaningful_draft_line_count: number;
  final_line_count: number;
  non_meaningful_draft_line_count: number;
  has_cdpi_definition: boolean;
  can_physical_delete: boolean;
  deletion_would_retire: boolean;
}

export interface CustomPayItemDeleteResult {
  pay_item_id: number | null;          // null when physically deleted
  pay_item_code: string;
  deletion_type: 'physical' | 'retired';
  cleaned_draft_lines: number;
}

// ─── CDPI (Custom Daily Pay Item) ─────────────────────────────────────────────

export type CdpiStatus =
  | 'Draft'
  | 'PendingCompanyApproval'
  | 'Rejected'
  | 'Approved';

export type CdpiInputType = 'Time' | 'Number';

export type CdpiCalcMethodKey =
  | 'PerUnit'
  | 'OrdinalTier'
  | 'Block'
  | 'RangeBracket'
  | 'RangeProgressive';

export type SupportedCdpiCalcMethodKey = 'PerUnit';

export type CdpiDecideAction = 'ReturnToDraft' | 'Reject' | 'Approve';

/** Read-side representation of a CDPI request row (backend: CdpiRequestSummary). */
export interface CdpiRequestSummary {
  request_id: string;                    // UUID
  company_id: number;
  requesting_branch_id: number;
  item_name: string | null;
  input_type: CdpiInputType | null;
  unit: string | null;
  calc_method_key: CdpiCalcMethodKey | null;
  notes: string | null;
  status: CdpiStatus;
  revision: number;
  approved_pay_item_id: number | null;
  copied_from_request_id: string | null; // UUID
  submitted_by_user_id: number | null;
  submitted_at_utc: string | null;
  created_by_user_id: number;
  created_at_utc: string;
  updated_by_user_id: number | null;
  updated_at_utc: string | null;
}

/** Body for POST /settings/cdpi/requests. */
export interface CdpiRequestCreatePayload {
  requesting_branch_id: number;
  item_name?: string | null;
  input_type?: CdpiInputType | null;
  unit?: string | null;
  calc_method_key?: SupportedCdpiCalcMethodKey | null;
  notes?: string | null;
}

/** Body for PATCH /settings/cdpi/requests/{id}. */
export interface CdpiRequestUpdatePayload {
  expected_revision: number;
  item_name?: string | null;
  input_type?: CdpiInputType | null;
  unit?: string | null;
  calc_method_key?: SupportedCdpiCalcMethodKey | null;
  notes?: string | null;
}

/** Body for POST /settings/cdpi/requests/{id}/submit. */
export interface CdpiSubmitPayload {
  expected_revision: number;
}

/** Body for POST /settings/cdpi/requests/{id}/decide. */
export interface CdpiDecidePayload {
  action: CdpiDecideAction;
  expected_revision: number;
  reason: string;
}

/** Body for POST /settings/cdpi/direct-company-items. */
export interface CdpiDirectCreatePayload {
  item_name: string;
  input_type: CdpiInputType;
  calc_method_key: SupportedCdpiCalcMethodKey;
  unit?: string | null;
  notes?: string | null;
}

/** Response from POST /settings/cdpi/direct-company-items. */
export interface CdpiDirectCreateSummary {
  pay_item_id: number;
  pay_item_code: string;
  company_id: number;
  item_name: string;
  input_type: CdpiInputType;
  unit: string | null;
  calc_method_key: CdpiCalcMethodKey;
  notes: string | null;
  created_by_user_id: number;
}

/**
 * Branch-level view of a single CDPI PayItem.
 * Returned by GET /settings/cdpi/branches/{id}/items
 * and PATCH /settings/cdpi/branches/{id}/items/{pay_item_id}.
 */
export interface CdpiBranchItem {
  pay_item_id: number;
  pay_item_code: string;
  item_name: string;
  branch_display_name_override: string | null;
  effective_display_name: string;
  is_active: boolean;
  data_type: string;
  unit: string | null;
  rate_behavior: string;
  is_cdpi: boolean;
}

/** Body for PATCH /settings/cdpi/branches/{id}/items/{pay_item_id}. */
export interface CdpiBranchItemUpdatePayload {
  is_active?: boolean | null;
  branch_display_name_override?: string | null;
}

/** Query params for GET /settings/cdpi/requests. */
export interface CdpiRequestListParams {
  status?: CdpiStatus;
  branch_id?: number;
}
