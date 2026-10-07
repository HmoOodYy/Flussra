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

export interface PayDefinitionRequestSummary {
  request_id: string;
  company_id: number;
  requesting_branch_id: number;
  definition_code: string | null;
  definition_name: string | null;
  input_type: PayDefinitionInputType | null;
  unit: string | null;
  calculation_method: PayDefinitionCalculationMethod | null;
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
  notes?: string | null;
}

export interface PayDefinitionRequestUpdatePayload {
  expected_revision: number;
  definition_code?: string | null;
  definition_name?: string | null;
  input_type?: PayDefinitionInputType | null;
  unit?: string | null;
  calculation_method?: PayDefinitionCalculationMethod | null;
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

// ─── Driver rate assignments ──────────────────────────────────────────────────

export type AssignmentStatus = 'Pending' | 'Approved' | 'Superseded' | 'Voided';

export interface AssignmentBrief {
  driver_rate_assignment_id: number;
  status: AssignmentStatus;
  effective_from: string;
  effective_to: string | null;
  amount: string | null;
}

export interface DriverPayRateRow {
  pay_definition_id: number;
  definition_code: string;
  definition_name: string;
  input_type: PayDefinitionInputType;
  unit: string | null;
  rate_definition_id: number;
  rate_component_definition_id: number;
  current: AssignmentBrief | null;
  future: AssignmentBrief | null;
  pending: AssignmentBrief | null;
}

export interface AssignmentValue {
  rate_component_definition_id: number;
  sequence_no: number;
  ordinal_from: number | null;
  ordinal_to: number | null;
  amount: string | null;
}

export interface AssignmentSummary {
  driver_rate_assignment_id: number;
  company_id: number;
  branch_id: number;
  driver_id: number;
  rate_definition_id: number;
  effective_from: string;
  effective_to: string | null;
  status: AssignmentStatus;
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
  values: AssignmentValue[];
}

export interface AssignmentCreatePayload {
  driver_id: number;
  rate_definition_id: number;
  effective_from: string;
  effective_to?: string | null;
  notes?: string | null;
}

export interface AssignmentValuePayload {
  rate_component_definition_id: number;
  amount: string | null;
}
