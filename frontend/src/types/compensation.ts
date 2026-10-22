// Target Compensation contracts: Company PayDefinitions, Branch applicability and
// Driver rate assignments. Mirrors backend/app/compensation/schemas.py.

export type PayDefinitionInputType = 'Decimal' | 'WholeNumber';
export type PayDefinitionCalculationMethod = 'PerUnit' | 'OrdinalTier';
export type PayDefinitionStatus = 'Active' | 'Inactive' | 'Retired';

export type PayDefinitionRequestStatus =
  | 'Draft'
  | 'PendingCompanyApproval'
  | 'Rejected'
  | 'Approved';

export type PayDefinitionDecisionAction = 'ReturnToDraft' | 'Reject' | 'Approve';

/**
 * One tier of an OrdinalTier definition's topology: the ordinal positions it covers. The last
 * tier is open-ended (`ordinal_to` null); a valid topology is 1-based, gapless and has exactly
 * that one open-ended tier.
 */
export interface OrdinalTierInput {
  ordinal_from: number;
  ordinal_to: number | null;
}

export interface PayDefinitionRequestSummary {
  request_id: string;
  company_id: number;
  requesting_branch_id: number;
  definition_code: string | null;
  definition_name: string | null;
  input_type: PayDefinitionInputType | null;
  unit: string | null;
  calculation_method: PayDefinitionCalculationMethod | null;
  ordinal_tiers: OrdinalTierInput[] | null;
  notes: string | null;
  status: PayDefinitionRequestStatus;
  revision: number;
  approved_pay_definition_id: number | null;
  copied_from_request_id: string | null;
  submitted_by_user_id: number | null;
  submitted_at_utc: string | null;
  created_by_user_id: number;
  created_at_utc: string;
  updated_by_user_id: number | null;
  updated_at_utc: string | null;
}

export interface PayDefinitionRequestEvent {
  event_id: string;
  request_id: string;
  event_type: string;
  from_status: PayDefinitionRequestStatus | null;
  to_status: PayDefinitionRequestStatus;
  actor_user_id: number;
  reason: string | null;
  request_revision: number;
  occurred_at_utc: string;
}

export interface PayDefinitionRequestCreatePayload {
  requesting_branch_id: number;
  definition_code?: string | null;
  definition_name?: string | null;
  input_type?: PayDefinitionInputType | null;
  unit?: string | null;
  calculation_method?: PayDefinitionCalculationMethod | null;
  ordinal_tiers?: OrdinalTierInput[] | null;
  notes?: string | null;
}

export interface PayDefinitionRequestUpdatePayload {
  expected_revision: number;
  definition_code?: string | null;
  definition_name?: string | null;
  input_type?: PayDefinitionInputType | null;
  unit?: string | null;
  calculation_method?: PayDefinitionCalculationMethod | null;
  ordinal_tiers?: OrdinalTierInput[] | null;
  notes?: string | null;
}

export interface PayDefinitionRequestSubmitPayload {
  expected_revision: number;
}

export interface PayDefinitionRequestDecisionPayload {
  action: PayDefinitionDecisionAction;
  expected_revision: number;
  reason: string;
}

export interface PayDefinitionRequestListParams {
  status?: PayDefinitionRequestStatus;
  branch_id?: number;
}

export interface PayDefinitionDirectCreatePayload {
  definition_code?: string | null;
  definition_name: string;
  input_type: PayDefinitionInputType;
  unit?: string | null;
  calculation_method: PayDefinitionCalculationMethod;
  ordinal_tiers?: OrdinalTierInput[] | null;
}

export interface PayDefinitionProvenance {
  creation_mode: 'Request' | 'DirectCreate';
  source_request_id: string | null;
  requesting_branch_id: number | null;
  created_by_user_id: number;
  submitted_by_user_id: number | null;
  submitted_at_utc: string | null;
  approved_by_user_id: number | null;
  approved_at_utc: string | null;
  governance_schema_version: number;
  calculation_method_version: number;
  created_at_utc: string;
}

export interface RateComponentSummary {
  rate_component_definition_id: number;
  sequence_no: number;
  ordinal_from: number | null;
  ordinal_to: number | null;
}

export interface PayDefinitionSummary {
  pay_definition_id: number;
  company_id: number;
  definition_code: string;
  definition_name: string;
  input_type: PayDefinitionInputType;
  unit: string | null;
  calculation_method: PayDefinitionCalculationMethod;
  status: PayDefinitionStatus;
  rate_definition_id: number | null;
  rate_shape: string | null;
  structure_locked_at_utc: string | null;
  components: RateComponentSummary[];
  provenance: PayDefinitionProvenance | null;
}

// ─── Branch applicability ─────────────────────────────────────────────────────

export interface BranchConfigVersion {
  config_id: number;
  is_active: boolean;
  notes: string | null;
  effective_from: string;
  effective_to: string | null;
  created_at_utc: string;
}

export interface BranchPayDefinitionState {
  pay_definition_id: number;
  definition_code: string;
  definition_name: string;
  input_type: PayDefinitionInputType;
  unit: string | null;
  calculation_method: PayDefinitionCalculationMethod;
  definition_status: PayDefinitionStatus;
  rate_definition_id: number | null;
  is_configured: boolean;
  is_active: boolean;
  notes: string | null;
  current_config: BranchConfigVersion | null;
  pending_config: BranchConfigVersion | null;
  has_open_periods: boolean;
}

export interface BranchConfigUpdatePayload {
  is_active: boolean;
  effective_from?: string | null;
  notes?: string | null;
}

export type BranchConfigTarget = 'AllBranches' | 'SelectedBranches';

export interface BulkBranchConfigUpdatePayload {
  target: BranchConfigTarget;
  branch_ids?: number[] | null;
  is_active: boolean;
  effective_from?: string | null;
  notes?: string | null;
}

export interface BulkBranchConfigBranchResult {
  branch_id: number;
  branch_name: string;
  status: 'Created' | 'Updated' | 'Versioned';
  config_id: number;
  effective_from: string;
}

export interface BulkBranchConfigResult {
  pay_definition_id: number;
  target: BranchConfigTarget;
  requested_branch_count: number;
  updated_branch_count: number;
  results: BulkBranchConfigBranchResult[];
}

// ─── Driver rate schedules (owned by the permanent Driver) ────────────────────

export type ScheduleStatus = 'Pending' | 'Approved' | 'Superseded' | 'Voided';

export interface ScheduleBrief {
  driver_rate_schedule_id: number;
  status: ScheduleStatus;
  effective_from: string;
  effective_to: string | null;
  /** The one value of a scalar schedule; null for any other shape (see `values`). */
  amount: string | null;
  /** One entry per component of the definition, a missing value as null (never zero). */
  values: ScheduleValue[];
}

/** What owns a rate definition: a PayDefinition, or a Company Status rate column. */
export type RateOwnerKind = 'PayDefinition' | 'StatusRateColumn';

/** One rate definition the Driver has schedules for, whatever its PayDefinition's status. */
export interface DriverRateSummaryItem {
  rate_definition_id: number;
  kind: RateOwnerKind;
  pay_definition_id: number | null;
  status_rate_column_id: number | null;
  status_rate_column_name: string | null;
  definition_code: string | null;
  definition_name: string | null;
  unit: string | null;
  /** 'Active' definitions are authorable; anything else (Retired) is history only. */
  pay_definition_status: string | null;
  approved_schedule_id: number | null;
  approved_effective_from: string | null;
  pending_schedule_id: number | null;
}

/**
 * One authorable rate of a Driver: a PayDefinition, or a Company Status rate column (a Status
 * is paid hours x this rate; it has no code, no Branch applicability and a fixed hourly unit).
 */
export interface DriverPayRateRow {
  kind: RateOwnerKind;
  pay_definition_id: number | null;
  status_rate_column_id: number | null;
  status_rate_column_name: string | null;
  definition_code: string | null;
  definition_name: string;
  input_type: PayDefinitionInputType;
  unit: string | null;
  calculation_method: PayDefinitionCalculationMethod | null;
  /** 'Scalar' (one value) or 'OrdinalTierSchedule' (one value per tier in `components`). */
  rate_shape: 'Scalar' | 'OrdinalTierSchedule';
  rate_definition_id: number;
  /** The component of a scalar rate; null for a tiered one (see `components`). */
  rate_component_definition_id: number | null;
  components: RateComponentSummary[];
  /**
   * False when the definition has no active Branch configuration in the Branch of the Driver's
   * context tenure on the reference date; null when the Driver has no tenure that day.
   * Authoring is still allowed: the flag only warns.
   */
  applicable_in_context_branch: boolean | null;
  current: ScheduleBrief | null;
  future: ScheduleBrief | null;
  pending: ScheduleBrief | null;
}

/**
 * GET /compensation/drivers/{id}/compensation-access: the backend's own decisions about what
 * the caller may see and do with one Driver's compensation. The client never re-derives them.
 */
export interface CompensationAccess {
  /** Whole history, Pending rows included (the read scope's `full`). */
  full_history: boolean;
  /** May discard a Pending row (discard authority over the Driver now). */
  can_discard_pending: boolean;
  /** Any tenure that has not ended; false means the Driver's Pending rows are orphaned. */
  has_current_or_pending_tenure: boolean;
}

export interface ScheduleValue {
  rate_component_definition_id: number;
  sequence_no: number;
  ordinal_from: number | null;
  ordinal_to: number | null;
  amount: string | null;
}

export interface ScheduleSummary {
  driver_rate_schedule_id: number;
  company_id: number;
  driver_id: number;
  /** Null for a Status rate column schedule. */
  pay_definition_id: number | null;
  status_rate_column_id: number | null;
  rate_definition_id: number;
  effective_from: string;
  effective_to: string | null;
  status: ScheduleStatus;
  created_by_user_id: number | null;
  created_at_utc: string;
  updated_by_user_id: number | null;
  updated_at_utc: string | null;
  approved_by_user_id: number | null;
  approved_at_utc: string | null;
  voided_by_user_id: number | null;
  voided_at_utc: string | null;
  void_reason: string | null;
  notes: string | null;
  values: ScheduleValue[];
}

export interface ScheduleCreatePayload {
  driver_id: number;
  rate_definition_id: number;
  effective_from: string;
  effective_to?: string | null;
  notes?: string | null;
}

export interface ScheduleValuePayload {
  rate_component_definition_id: number;
  amount: string | null;
}
