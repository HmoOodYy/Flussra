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
  currency_code: string | null;
  currency_name: string | null;
  currency_minor_unit_digits: number | null;
  currency_change_locked: boolean;
  created_at_utc: string;
  updated_at_utc: string | null;
}

export interface CompanyUpdate {
  company_name: string;
  legal_name?: string | null;
  timezone_name?: string | null;
  notes?: string | null;
  allow_self_approval?: boolean | null;
  currency_code?: string | null;
}

export interface SupportedCurrency {
  currency_code: string;
  currency_name: string;
  numeric_code: string;
  minor_unit_digits: number;
}

import type { BoundaryChoicesResponse, SetupResponse } from './payrollSetup';

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
  // Canonical readiness (U0): null unless the caller has non-driver payroll.view on this branch,
  // or in the POST /settings/branches response when first_payroll_start_date was supplied.
  schedule_readiness_reason: string | null;
  // Period start date the reason was evaluated against (ISO date); same visibility as the reason.
  schedule_readiness_date: string | null;
  status_keys_count: number | null;
  total_people_count: number | null;
  active_drivers_count: number | null;
  pending_approvals_count: number | null;
}

/**
 * GET /settings/branches/onboarding-options?around=
 * `choices` is null when there is no company default Setup to onboard against.
 */
export interface OnboardingOptionsResponse {
  default_setup: SetupResponse | null;
  choices: BoundaryChoicesResponse | null;
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
