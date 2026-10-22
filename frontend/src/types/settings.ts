// ─── Company ──────────────────────────────────────────────────────────────────

export interface CompanyProfile {
  company_id: number;
  company_code: string;
  company_name: string;
  legal_name: string | null;
  status: string;
  is_suspended: boolean;
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
  time_zone_id: string;
  business_date: string;
  // Operational metrics
  payroll_setup_done: boolean;
  // Canonical readiness (U0): null unless the caller has non-driver payroll.view on this branch,
  // or in the POST /settings/branches response when first_payroll_start_date was supplied.
  schedule_readiness_reason: string | null;
  // Period start date the reason was evaluated against (ISO date); same visibility as the reason.
  schedule_readiness_date: string | null;
  total_people_count: number | null;
  active_drivers_count: number | null;
  pending_approvals_count: number | null;
}

export interface BranchUpdate {
  branch_name: string;
  branch_code?: string;
  status: string;
  address_line1: string | null;
  city: string | null;
  state_province: string | null;
  postal_code: string | null;
  country: string | null;
  notes: string | null;
  time_zone_id?: string;
}

export interface BranchCreate extends BranchUpdate {
  is_default: boolean;
  time_zone_id: string;
  first_payroll_start_date?: string;
}

export type SupportedTimeZones = string[];

/**
 * GET /settings/branches/onboarding-options?around=
 * `choices` is null when there is no company default Setup to onboard against.
 */
export interface OnboardingOptionsResponse {
  default_setup: SetupResponse | null;
  choices: BoundaryChoicesResponse | null;
}

// ─── Status catalog (Company-owned) ───────────────────────────────────────────

/** The fixed Category set. A Category groups and reports; it never decides pay. */
export const STATUS_CATEGORIES = [
  'Vacation', 'Sick', 'Bereavement', 'Jury Duty', 'Personal', 'Holiday', 'Unpaid', 'Other',
] as const;
export type StatusCategory = (typeof STATUS_CATEGORIES)[number];

/**
 * One Status of the Company catalog, shared by every Branch.
 * `key_name` is the canonical primary name every display shows; `aliases` are input
 * shortcuts only (a selection stores the Status, never the alias); `status_code` is the
 * generated technical code.
 */
export interface StatusKey {
  status_key_id: number;
  company_id: number;
  key_name: string;
  aliases: string[];
  status_code: string;
  category: StatusCategory;
  hours_value: number | string;      // 0-24 (the server sends a decimal)
  /** The Status counts as a full day off: it cannot share a day with ordinary work. */
  is_off_reason: boolean;
  deducts_from_yearly_allowance: boolean;
  is_active: boolean;
  display_order: number;
  /** The Status rate column that makes the Status monetary; null means it is not paid. */
  status_rate_column_id: number | null;
  /** A selection or frozen evidence references it: hours, off semantic, Category and column are locked. */
  is_in_use: boolean;
  created_at_utc: string;
  updated_at_utc: string | null;
}

export interface StatusKeyCreate {
  key_name: string;
  category: StatusCategory;
  aliases?: string[];
  hours_value?: number;
  is_off_reason?: boolean;
  is_active?: boolean;
  display_order?: number;
  status_rate_column_id?: number | null;
}

/** Partial update: only the fields sent change; `aliases` replaces the whole alias set. */
export interface StatusKeyUpdate {
  key_name?: string;
  category?: StatusCategory;
  aliases?: string[];
  hours_value?: number;
  is_off_reason?: boolean;
  is_active?: boolean;
  display_order?: number;
  status_rate_column_id?: number | null;
}

/** A Company Status rate column: what makes a Status monetary (hours x the Driver's rate). */
export interface StatusRateColumn {
  status_rate_column_id: number;
  company_id: number;
  column_name: string;
  is_default: boolean;
  is_active: boolean;
  rate_definition_id: number;
  rate_component_definition_id: number;
  created_at_utc: string;
  updated_at_utc: string | null;
}

export interface StatusRateColumnCreate {
  column_name: string;
  is_default?: boolean;
}

export interface StatusRateColumnUpdate {
  column_name?: string;
  is_default?: boolean;
  is_active?: boolean;
}

/** The four optional hard usage limits of one Status in one Branch. */
export interface StatusKeyBranchLimits {
  branch_id: number;
  status_key_id: number;
  key_name: string;
  limit_uses_per_period_enabled: boolean;
  limit_uses_per_period: number | null;
  limit_uses_per_driver_enabled: boolean;
  limit_uses_per_driver: number | null;
  limit_uses_across_drivers_enabled: boolean;
  limit_uses_across_drivers: number | null;
  limit_uses_per_day_enabled: boolean;
  limit_uses_per_day: number | null;
}

export type StatusKeyBranchLimitsUpdate = Omit<StatusKeyBranchLimits, 'branch_id' | 'status_key_id' | 'key_name'>;
