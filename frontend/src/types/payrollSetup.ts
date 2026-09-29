// ─────────────────────────────────────────────────────────────────────────────
// Canonical company-owned Payroll Setup API contract.
//
// Mirrors backend/app/payroll_setup/schemas.py field-for-field (snake_case,
// one interface per Pydantic model). The legacy branch-owned settings types
// were removed in Phase 6 U5a.
//
// Conventions:
//   - Pydantic `date` -> `string` (ISO YYYY-MM-DD), annotated per field.
//   - Pydantic `datetime` -> `string` (ISO 8601 UTC timestamp).
//   - Request fields WITH a default (incl. `Field(default=None, ...)`) are
//     optional TS keys (`?:`).
//   - Request fields WITHOUT a default are required TS keys, even when the
//     value itself is nullable (e.g. DraftUpdateRequest.custom_interval_days,
//     DefaultSetupRequest.setup_id).
//   - Response fields are always-present TS keys; nullable ones use `| null`.
// ─────────────────────────────────────────────────────────────────────────────

/** Backend-validated request value for payroll_frequency (^(Week|Biweek|Month|Custom)$). */
export type PayrollFrequency = 'Week' | 'Biweek' | 'Month' | 'Custom';

// ── Requests ────────────────────────────────────────────────────────────────

export interface SetupCreateRequest {
  // Optional (Phase 6 U C3): when omitted, the server generates a stable
  // code from setup_name.
  setup_code?: string;
  setup_name: string;
  description?: string | null;
}

export interface SetupUpdateRequest {
  setup_name: string;
  description?: string | null;
}

export interface DraftCreateRequest {
  payroll_frequency?: PayrollFrequency | null;
  anchor_start_date?: string | null; // ISO date (YYYY-MM-DD)
  custom_interval_days?: number | null;
  normal_days_off_mask?: number | null;
  planned_effective_from_date?: string | null;
}

export interface DraftUpdateRequest {
  payroll_frequency: PayrollFrequency | null;
  anchor_start_date: string | null; // ISO date (YYYY-MM-DD)
  // int | None = Field(gt=0) has no default -> required key, nullable value.
  custom_interval_days: number | null;
  normal_days_off_mask: number | null;
  planned_effective_from_date: string | null;
}

export interface PublishRequest {
  effective_from_date: string; // ISO date (YYYY-MM-DD)
  replaces_version_id?: number | null;
}

export interface PublicationImpactRequest {
  effective_from_date: string; // ISO date (YYYY-MM-DD)
  replaces_version_id?: number | null;
}

export interface InlineScheduleRequest {
  payroll_frequency: PayrollFrequency;
  anchor_start_date: string;
  custom_interval_days: number | null;
  normal_days_off_mask: number;
}

export interface InlinePublicationRequest extends InlineScheduleRequest {
  effective_from_date: string;
  replaces_version_id?: number | null;
}

export interface DefaultSetupRequest {
  // int | None with no default -> required key, nullable value.
  setup_id: number | null;
}

export interface AssignmentCreateRequest {
  setup_id: number;
  effective_from_date: string; // ISO date (YYYY-MM-DD)
  reason?: string | null;
}

export interface ReassignmentRequest {
  destination_setup_id: number;
  effective_from_date: string; // ISO date (YYYY-MM-DD)
  reason?: string | null;
}

export interface ReassignmentImpactRequest {
  destination_setup_id: number;
  effective_from_date: string; // ISO date (YYYY-MM-DD)
}

export interface WithdrawalRequest {
  reason?: string | null;
}

// ── Responses ───────────────────────────────────────────────────────────────

export interface SetupResponse {
  setup_id: number;
  setup_code: string;
  setup_name: string;
  description: string | null;
  status: string; // known values: Active | Archived
}

export interface ScheduleResponse {
  payroll_frequency: string; // known values: Week | Biweek | Month | Custom
  anchor_start_date: string; // ISO date (YYYY-MM-DD)
  custom_interval_days: number | null;
  normal_days_off_mask: number;
}

export interface DraftResponse {
  setup_id: number;
  version_id: number;
  lifecycle_state: string; // known values: Draft | Published
  payroll_frequency: string | null; // known values: Week | Biweek | Month | Custom
  anchor_start_date: string | null; // ISO date (YYYY-MM-DD)
  custom_interval_days: number | null;
  normal_days_off_mask: number | null;
  planned_effective_from_date: string | null;
  created_at_utc: string; // ISO 8601 datetime
  discarded_at_utc: string | null; // ISO 8601 datetime
}

export interface VersionResponse {
  setup_id: number;
  version_id: number;
  lifecycle_state: string; // known values: Draft | Published
  version_number: number;
  effective_from_date: string; // ISO date (YYYY-MM-DD)
  effective_to_date: string | null; // ISO date (YYYY-MM-DD)
  schedule: ScheduleResponse;
  config_hash: string;
  replaces_version_id: number | null;
  replaced_by_version_id: number | null;
  is_terminal: boolean;
  is_current: boolean; // effective on company-local today (display only)
}

export interface ConflictResponse {
  branch_id: number | null;
  code: string;
  reason: string;
}

export interface PublicationImpactResponse {
  setup_id: number;
  affected_branch_ids: number[];
  effective_date: string; // ISO date (YYYY-MM-DD)
  predecessor_version_id: number | null;
  predecessor_hash: string | null;
  current_same_date_version_id: number | null;
  successor_hash: string;
  successor_schedule: ScheduleResponse;
  next_version_boundary: string | null; // ISO date (YYYY-MM-DD)
  conflicts: ConflictResponse[];
  allowed: boolean;
}

export interface ReassignmentImpactResponse {
  branch_id: number;
  source_setup_id: number | null;
  destination_setup_id: number;
  predecessor_version_id: number | null;
  successor_version_id: number | null;
  effective_date: string; // ISO date (YYYY-MM-DD)
  conflicts: ConflictResponse[];
  allowed: boolean;
}

export interface AssignmentResponse {
  assignment_id: number;
  branch_id: number;
  setup_id: number;
  setup_code: string;
  setup_name: string;
  effective_from_date: string; // ISO date (YYYY-MM-DD)
  effective_to_date: string | null; // ISO date (YYYY-MM-DD)
  reason: string | null;
  created_at_utc: string; // ISO 8601 datetime
  withdrawn_at_utc: string | null; // ISO 8601 datetime
  withdrawal_reason: string | null;
}

export interface VersionSegmentResponse {
  version_id: number;
  version_number: number;
  effective_from_date: string; // ISO date (YYYY-MM-DD)
  effective_to_date: string | null; // ISO date (YYYY-MM-DD)
  schedule: ScheduleResponse;
  config_hash: string;
}

export interface AssignmentHistoryResponse extends AssignmentResponse {
  versions: VersionSegmentResponse[];
}

export interface BranchHistoryResponse {
  branch_id: number;
  assignments: AssignmentHistoryResponse[];
}

export interface DefaultSetupResponse {
  setup: SetupResponse | null;
}

// ── Boundary choices (Phase 6 U C3) ─────────────────────────────────────────
//
// Backing GET /payroll-setup/setups/{setup_id}/drafts/{draft_id}/publication-choices,
// GET /payroll-setup/branches/{branch_id}/assignment-choices, and
// GET /payroll-setup/branches/{branch_id}/reassignment-choices. The backend is
// the only source of which dates are valid chronology boundaries — this
// module never recomputes that; it only carries the response shape.

export type BoundaryRelation = 'past' | 'current' | 'future';

export interface BoundaryChoiceResponse {
  date: string; // ISO date (YYYY-MM-DD)
  period_end_date: string; // ISO date (YYYY-MM-DD)
  payroll_frequency: string; // known values: Week | Biweek | Month | Custom
  custom_interval_days: number | null;
  relation: BoundaryRelation;
  predecessor_payroll_frequency: string | null;
  predecessor_custom_interval_days: number | null;
  predecessor_period_end_date: string | null; // ISO date (YYYY-MM-DD)
  replaces_version_id: number | null;
  replaces_version_number: number | null;
}

export interface BoundaryChoicesResponse {
  reference_date: string; // ISO date (YYYY-MM-DD)
  requested_date: string; // ISO date (YYYY-MM-DD)
  requested_valid: boolean;
  requested: BoundaryChoiceResponse | null;
  conflicts: ConflictResponse[];
  previous: BoundaryChoiceResponse | null;
  next: BoundaryChoiceResponse | null;
  // Only non-null when the request omitted `around` (server suggestion).
  suggested: BoundaryChoiceResponse | null;
  earliest_allowed_date: string | null; // ISO date (YYYY-MM-DD)
}

// ── Branch policy summaries (Phase 6 U C3) ──────────────────────────────────
//
// Backing GET /payroll-setup/branch-summaries.

export interface PolicyAssignmentSummaryResponse {
  assignment_id: number;
  setup_id: number;
  setup_code: string;
  setup_name: string;
  effective_from_date: string; // ISO date (YYYY-MM-DD)
  effective_to_date: string | null; // ISO date (YYYY-MM-DD)
  payroll_frequency: string | null; // known values: Week | Biweek | Month | Custom
  custom_interval_days: number | null;
}

export interface BranchPolicySummaryResponse {
  branch_id: number;
  branch_code: string;
  branch_name: string;
  branch_status: string;
  reference_date: string; // ISO date (YYYY-MM-DD)
  payroll_set_up: boolean;
  current: PolicyAssignmentSummaryResponse | null;
  scheduled_change: PolicyAssignmentSummaryResponse | null;
  upcoming_assignments: PolicyAssignmentSummaryResponse[];
  readiness_reason: string;
  readiness_date: string | null; // ISO date (YYYY-MM-DD)
}

export interface EffectiveAuthorityResponse {
  company_id: number;
  branch_id: number;
  assignment_id: number;
  setup_id: number;
  setup_code: string;
  setup_name: string;
  version_id: number;
  version_number: number;
  schedule: ScheduleResponse;
  config_hash: string;
  period_start_date: string; // ISO date (YYYY-MM-DD)
  period_end_date: string; // ISO date (YYYY-MM-DD)
  next_boundary_date: string | null; // ISO date (YYYY-MM-DD)
  next_boundary_kind: string | null; // known values: Assignment | Version | AssignmentAndVersion
}
