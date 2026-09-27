/**
 * Pure authority/presentation helpers for Company & Branches (Phase 6 U7).
 *
 * PURE MODULE: no React, no HTTP client of any kind, no `Date` usage of any
 * kind. This module never computes payroll readiness, the effective
 * Setup/Version, or assignment continuity — the POST /settings/branches
 * create response and the /payroll/schedule page are the sole authority for
 * that. It only decides what the Company & Branches page may show and what
 * it should send, given values the caller already has.
 */
import type { UserProfile } from '../../../store/authStore';
import type { BranchAdmin } from '../../../types/settings';
import {
  canAssignPayrollSetups,
  canCreateBranches,
  canManageSettingsAdmin,
  canViewBranchPayrollSchedule,
  canViewPayrollSetups,
} from '../../../lib/permissions.ts';
import { describeReadiness } from '../../../lib/payrollSetupReadiness.ts';

// ── Capabilities ─────────────────────────────────────────────────────────────

export interface CompanyBranchesCapabilities {
  canCreate: boolean;
  canAdmin: boolean;
  canOnboard: boolean;
  canViewSetups: boolean;
  canSetDefaultOnCreate: boolean;
}

/**
 * Derives this page's capability set from the user's authority. `canOnboard`
 * is deliberately independent of `canViewSetups`, `canAdmin`,
 * payroll_setup.manage, and payroll_setup.publish — it requires exactly
 * branch creation plus payroll_setup.assign, mirroring the backend's
 * additional-permission-when-a-date-is-sent contract. `canSetDefaultOnCreate`
 * mirrors the backend's additional-permission-when-is_default-is-sent
 * contract: branch creation plus settings-admin (canAdmin) — the same
 * authority the dedicated set-default endpoint requires.
 */
export function companyBranchesCapabilities(user: UserProfile | null): CompanyBranchesCapabilities {
  if (!user) {
    return {
      canCreate: false, canAdmin: false, canOnboard: false, canViewSetups: false,
      canSetDefaultOnCreate: false,
    };
  }
  const canCreate = canCreateBranches(user);
  const canAdmin = canManageSettingsAdmin(user);
  return {
    canCreate,
    canAdmin,
    canOnboard: canCreate && canAssignPayrollSetups(user),
    canViewSetups: canViewPayrollSetups(user),
    canSetDefaultOnCreate: canCreate && canAdmin,
  };
}

/** The read-only/limited-access banner message for this page, or null when the user has full access. */
export function accessNotice(caps: CompanyBranchesCapabilities): string | null {
  if (caps.canAdmin && caps.canCreate) return null;
  if (caps.canAdmin && !caps.canCreate) {
    return 'You can edit the Company and existing Branches. Creating Branches requires the branches.create permission.';
  }
  if (!caps.canAdmin && caps.canCreate) {
    return 'You can create Branches. Editing the Company, existing Branches and the default Branch requires settings administration permission.';
  }
  return "You don't have permission to edit company or branch settings.";
}

// ── Branch form ───────────────────────────────────────────────────────────────

export interface BranchFormValues {
  branch_name: string;
  branch_code: string;
  status: string;
  is_default: boolean;
  address_line1: string;
  city: string;
  state_province: string;
  postal_code: string;
  country: string;
  notes: string;
}

export const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Validates the optional first-payroll-start-date field: blank is valid
 * (the field is optional), an ISO YYYY-MM-DD string is valid, anything else
 * is rejected. No calendar/range checks — the backend decides whether the
 * date is usable.
 */
export function validateFirstPayrollStartDate(raw: string): string | null {
  if (raw.trim() === '') return null;
  if (ISO_DATE_PATTERN.test(raw)) return null;
  return 'Enter the first payroll start date as YYYY-MM-DD.';
}

/** Body for PATCH /settings/branches/{id} — never is_default, never first_payroll_start_date. */
export function buildBranchUpdatePayload(form: BranchFormValues) {
  return {
    branch_name: form.branch_name.trim(),
    branch_code: form.branch_code.trim() || undefined,
    status: form.status,
    address_line1: form.address_line1 || null,
    city: form.city || null,
    state_province: form.state_province || null,
    postal_code: form.postal_code || null,
    country: form.country || null,
    notes: form.notes || null,
  };
}

/**
 * Body for POST /settings/branches. Includes `first_payroll_start_date` only
 * when the caller can onboard (create + payroll_setup.assign), the user
 * actually checked "Set up payroll for this branch now" (`setUpPayroll`),
 * and the trimmed date is non-empty — otherwise the key is entirely absent,
 * not null/undefined-valued. Unchecking the checkbox (or there being no
 * company default to onboard against) must never leak a stale date through
 * even if one is still sitting in form state. `is_default` is forced to
 * `false` unless the caller has `canSetDefaultOnCreate` (create +
 * settings-admin) — it can never be sent as `true` without that authority,
 * mirroring the backend's additional-permission-when-is_default-is-sent
 * contract. Never includes setup_id, version_id, assignment_id, or any
 * readiness field: the backend resolves the Company default Setup on its
 * own.
 */
export function buildBranchCreatePayload(
  form: BranchFormValues,
  firstPayrollStartDate: string,
  options: { canOnboard: boolean; setUpPayroll: boolean; canSetDefaultOnCreate: boolean },
) {
  const u = buildBranchUpdatePayload(form);
  const trimmedDate = firstPayrollStartDate.trim();
  return {
    branch_name: u.branch_name,
    branch_code: u.branch_code,
    status: u.status,
    is_default: options.canSetDefaultOnCreate && form.is_default,
    address_line1: u.address_line1,
    city: u.city,
    state_province: u.state_province,
    postal_code: u.postal_code,
    country: u.country,
    notes: u.notes,
    ...(options.canOnboard && options.setUpPayroll && trimmedDate !== ''
      ? { first_payroll_start_date: trimmedDate }
      : {}),
  };
}

// ── Onboarding checkbox / create-enabled rule (Unit B) ─────────────────────

/**
 * Whether the "Set up payroll for this branch now" checkbox should even be
 * shown — onboarding requires the same authority as sending
 * first_payroll_start_date at all (create + payroll_setup.assign).
 */
export function showOnboardingCheckbox(caps: Pick<CompanyBranchesCapabilities, 'canOnboard'>): boolean {
  return caps.canOnboard;
}

/**
 * Whether "Create" should be enabled, given the onboarding checkbox state.
 * Unchecked -> always true (branch-only creation never depends on payroll
 * dates). Checked but no company default -> still true (the branch can be
 * created without payroll; there is nothing to date-pick). Checked with a
 * company default -> only once the boundary choices are current for the
 * chosen date and the backend says that date is valid — this never
 * computes date validity itself.
 */
export function canCreateBranchWithOnboarding(params: {
  setUpPayrollNow: boolean;
  hasCompanyDefault: boolean;
  choicesCurrentAndValid: boolean;
}): boolean {
  if (!params.setUpPayrollNow) return true;
  if (!params.hasCompanyDefault) return true;
  return params.choicesCurrentAndValid;
}

// ── Onboarding result presentation ────────────────────────────────────────────

export type OnboardingTone = 'ready' | 'incomplete' | 'unknown';

export interface OnboardingResult {
  tone: OnboardingTone;
  code: string | null;
  message: string;
  evaluatedDate: string | null;
}

/**
 * Presents the create response's onboarding outcome, verbatim from the
 * server. Only called when a first payroll start date was submitted.
 */
export function onboardingResult(
  created: Pick<BranchAdmin, 'schedule_readiness_reason' | 'schedule_readiness_date'>,
): OnboardingResult {
  const reason = created.schedule_readiness_reason;
  const evaluatedDate = created.schedule_readiness_date;

  if (reason === null) {
    return {
      tone: 'unknown',
      code: null,
      message: 'Branch created. The server did not report an onboarding result.',
      evaluatedDate,
    };
  }

  if (reason === 'READY') {
    return {
      tone: 'ready',
      code: reason,
      message: 'Branch created and its Payroll Setup is ready for the evaluated payroll start.',
      evaluatedDate,
    };
  }

  if (reason === 'NO_COMPANY_DEFAULT') {
    return {
      tone: 'incomplete',
      code: reason,
      message: 'Branch created, but no Company default Payroll Setup was available for automatic onboarding.',
      evaluatedDate,
    };
  }

  const described = describeReadiness(reason);
  if (described.known) {
    return {
      tone: 'incomplete',
      code: reason,
      message: `Branch created, but payroll onboarding is not ready: ${described.label} (${reason}). ${described.description}`,
      evaluatedDate,
    };
  }

  return {
    tone: 'unknown',
    code: reason,
    message: `Branch created. The server reported onboarding state ${reason}.`,
    evaluatedDate,
  };
}

// ── Follow-up links ────────────────────────────────────────────────────────────

export type SetupNeededLink =
  | { kind: 'setups'; to: '/settings/payroll' }
  | { kind: 'schedule'; to: string }
  | { kind: 'none' };

export function payrollScheduleHref(branchId: number): string {
  return `/payroll/schedule?branchId=${branchId}`;
}

/**
 * Where the "Setup Needed" badge should navigate: Payroll Setups when the
 * user may view them, else the branch's read-only Payroll Schedule when the
 * user may view that, else nowhere (a non-interactive badge).
 */
export function setupNeededLink(user: UserProfile | null, branchId: number): SetupNeededLink {
  if (!user) return { kind: 'none' };
  if (canViewPayrollSetups(user)) return { kind: 'setups', to: '/settings/payroll' };
  if (canViewBranchPayrollSchedule(user, branchId)) {
    return { kind: 'schedule', to: payrollScheduleHref(branchId) };
  }
  return { kind: 'none' };
}

/**
 * The "View Payroll Schedule" link shown under the Payroll Status badge,
 * regardless of setup-done state. Never depends on payroll_setup.view.
 */
export function branchScheduleLink(user: UserProfile | null, branchId: number): string | null {
  return user && canViewBranchPayrollSchedule(user, branchId) ? payrollScheduleHref(branchId) : null;
}

/** Title-attribute text for a readiness reason, or undefined when there is none to show. */
export function readinessTitle(reason: string | null): string | undefined {
  if (reason === null) return undefined;
  return `${describeReadiness(reason).label} (${reason})`;
}
