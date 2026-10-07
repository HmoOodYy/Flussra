/**
 * Central frontend permission helpers.
 *
 * These mirror backend authorization rules so the UI can make informed
 * render decisions.  Backend remains the authoritative security boundary —
 * hiding a nav link does NOT replace a backend 403.
 *
 * Permission codes are sourced from backend app/payroll/service.py,
 * app/review/router.py, and app/admin/service.py.
 */
import type { UserProfile } from '../store/authStore';
import {
  hasAuthorityPermission,
  hasAuthorityPermissionAnywhere,
} from '../store/authStore.ts';

// ── Private helpers ───────────────────────────────────────────────────────────

function _hasAnyAuthorityPermission(user: UserProfile, codes: readonly string[]): boolean {
  return codes.some((code) => hasAuthorityPermissionAnywhere(user.authority, code));
}

/**
 * True when `code` is granted at company scope — i.e. via an active
 * AllCompanyBranches assignment.  Backed by the canonical authority contract
 * (hasAuthorityPermission), never the flat active_permissions union or
 * coarse scope_type.
 */
function _hasCompanyPermission(user: UserProfile, code: string): boolean {
  return hasAuthorityPermission(user.authority, code, null);
}

/**
 * Company-scoped admin fallback shared by People/Role helpers — mirrors the
 * backend's `_ensure_any_perm` admin fallback (settings.manage / setup.manage
 * granted at company scope), independent of active_permissions/scope_type.
 */
function _hasCompanyAdminFallback(user: UserProfile): boolean {
  return (
    _hasCompanyPermission(user, 'settings.manage') ||
    _hasCompanyPermission(user, 'setup.manage')
  );
}

// ── Role / scope ──────────────────────────────────────────────────────────────

/**
 * True when any exact DRIVER assignment is present. Self assignments and
 * malformed DRIVER branch assignments both activate the UI capability ceiling.
 */
export function isDriverUser(user: UserProfile): boolean {
  return user.self_assignments.some((a) => a.role_code === 'DRIVER') ||
    user.branches.some((b) => b.role_code === 'DRIVER');
}

// ── Page-level visibility guards ──────────────────────────────────────────────

const PAYROLL_READ = [
  'payroll.view',
  'payroll.edit',           // production seed code (migration 0015)
  'payroll.entry',          // legacy/test-only code (conftest.py / ensure_dev_admin.py)
  'payroll.finalize',
  'payroll.period.create',  // dedicated create permission (migration 0030)
] as const;

const PAYRATES_ACCESS = [
  'payrates.view',
  'payrates.edit',
  'settings.manage',
  'setup.manage',
] as const;

/** Current Payroll page: any payroll read/write permission. */
export function canViewCurrentPayroll(user: UserProfile): boolean {
  return !isDriverUser(user) && _hasAnyAuthorityPermission(user, PAYROLL_READ);
}

/**
 * Current Payroll reports for a specific period's branch: dedicated
 * reports.view permission, branch-aware — mirrors the backend per-branch
 * authority check, not the flat active_permissions union.
 */
export function canViewPayrollReports(user: UserProfile, branchId: number): boolean {
  return !isDriverUser(user) && hasAuthorityPermission(user.authority, 'reports.view', branchId);
}

/** Finalized Payroll Library: backend's dedicated ledger.view contract. */
export function canViewFinalizedLibrary(user: UserProfile): boolean {
  return !isDriverUser(user) && _hasAnyAuthorityPermission(user, ['ledger.view']);
}

/** Legacy raw FinalLines: mirror its operational backend read permissions. */
export function canViewFinalLines(user: UserProfile): boolean {
  return (
    !isDriverUser(user) &&
    _hasAnyAuthorityPermission(user, ['payroll.view', 'payroll.entry', 'payroll.finalize'])
  );
}

/**
 * Ledger navigation remains available to either finalized-library readers or
 * operational payroll readers while the legacy FinalLines surface is retained.
 */
export function canViewLedger(user: UserProfile): boolean {
  return canViewFinalizedLibrary(user) || canViewFinalLines(user);
}

/**
 * Review page: any payroll access OR review.decide.
 * (Reading the review queue mirrors payroll read; deciding requires review.decide.)
 */
export function canViewReview(user: UserProfile): boolean {
  return (
    !isDriverUser(user) &&
    _hasAnyAuthorityPermission(user, [...PAYROLL_READ, 'review.decide'])
  );
}

/** Pay Rates page: payrates.view/edit or admin fallback. */
export function canViewPayRates(user: UserProfile): boolean {
  return !isDriverUser(user) && _hasAnyAuthorityPermission(user, PAYRATES_ACCESS);
}

/**
 * People page: company-scoped users.view, or company admin fallback.
 * Company-scoped, not driver-gated — a mixed Driver + valid company-admin
 * assignment must still reach this page, so isDriverUser() is deliberately
 * not consulted here.
 */
export function canViewPeople(user: UserProfile): boolean {
  return _hasCompanyPermission(user, 'users.view') || _hasCompanyAdminFallback(user);
}

/**
 * Settings pages: company setup/settings admin, or any company-scoped
 * role/user admin permission.  Company-scoped via hasAuthorityPermission and
 * authority.company_permissions (prefix inspection), never the flat
 * active_permissions union or coarse scope_type.  A mixed Driver + valid
 * company roles/users assignment still reaches the nested Roles route, while
 * a branch-only settings.manage grant does not.
 */
export function canViewSettings(user: UserProfile): boolean {
  return (
    _hasCompanyAdminFallback(user) ||
    user.authority.company_permissions.some(
      (p) => p.startsWith('roles.') || p.startsWith('users.')
    )
  );
}

// ── Action-level guards ───────────────────────────────────────────────────────

/**
 * Create payroll periods for a specific branch.
 * Requires the dedicated payroll.period.create permission (migration 0030),
 * granted either at company scope or for the given branch — see
 * hasAuthorityPermission().  This permission is separate from payroll.entry —
 * holding one does not imply the other.
 */
export function canCreatePeriod(user: UserProfile, branchId: number): boolean {
  return hasAuthorityPermission(user.authority, 'payroll.period.create', branchId);
}

/**
 * Enter/edit payroll lines, open periods, submit for review, manage Drivers Off
 * and Bonuses for a specific branch.  Requires payroll.entry — the operational
 * data-entry permission — granted either at company scope or for the given
 * branch.  Does NOT imply create-period; that requires payroll.period.create.
 */
export function canEntryPayroll(user: UserProfile, branchId: number): boolean {
  return hasAuthorityPermission(user.authority, 'payroll.entry', branchId);
}

/**
 * View Expected Payroll (calculation preview) for a specific branch.
 * Mirrors the backend's get_calculation_preview check: payroll.view OR
 * payroll.entry, granted either at company scope or for the given branch.
 */
export function canPreviewCalculation(user: UserProfile, branchId: number): boolean {
  if (isDriverUser(user)) return false;
  return ['payroll.view', 'payroll.entry'].some((code) =>
    hasAuthorityPermission(user.authority, code, branchId)
  );
}

/** Finalize button / period lifecycle admin actions for a specific branch. */
export function canFinalizePayroll(user: UserProfile, branchId: number): boolean {
  return hasAuthorityPermission(user.authority, 'payroll.finalize', branchId);
}

/** Approve / return review items for a specific branch. */
export function canDecideReview(user: UserProfile, branchId: number): boolean {
  return hasAuthorityPermission(user.authority, 'review.decide', branchId);
}

/**
 * Edit/approve pay rates, pay rules, and copy-rate actions for a specific
 * branch.  Branch-aware — mirrors the backend per-branch authority check
 * (payrates.edit / settings.manage / setup.manage), not the flat
 * active_permissions union.
 */
export function canEditPayRates(user: UserProfile, branchId: number): boolean {
  if (isDriverUser(user)) return false;
  return ['payrates.edit', 'settings.manage', 'setup.manage'].some((code) =>
    hasAuthorityPermission(user.authority, code, branchId)
  );
}

/** Create new people/users: company-scoped users.create, or company admin fallback. */
export function canCreatePeople(user: UserProfile): boolean {
  return _hasCompanyPermission(user, 'users.create') || _hasCompanyAdminFallback(user);
}

/**
 * Edit an existing person's profile, reset their password, or edit their
 * extra permission overrides: company-scoped users.edit, or company admin
 * fallback.  Deliberately does NOT include users.deactivate — that permission
 * authorizes the active-only toggle (see canTogglePeopleActive), not
 * profile/password/override edits.
 */
export function canEditPeople(user: UserProfile): boolean {
  return _hasCompanyPermission(user, 'users.edit') || _hasCompanyAdminFallback(user);
}

/**
 * Activate/deactivate a person: company-scoped users.deactivate OR
 * users.edit, or company admin fallback — mirrors the backend's active-only
 * patch guard.
 */
export function canTogglePeopleActive(user: UserProfile): boolean {
  return (
    _hasCompanyPermission(user, 'users.deactivate') ||
    _hasCompanyPermission(user, 'users.edit') ||
    _hasCompanyAdminFallback(user)
  );
}

/**
 * Assign/change a person's company role: company-scoped users.edit OR
 * roles.edit, or company admin fallback — mirrors the backend's
 * company-role-assignment guard.
 */
export function canAssignPeopleRole(user: UserProfile): boolean {
  return (
    _hasCompanyPermission(user, 'users.edit') ||
    _hasCompanyPermission(user, 'roles.edit') ||
    _hasCompanyAdminFallback(user)
  );
}

/** View roles and their permission assignments: company-scoped roles.view, or company admin fallback. */
export function canManageRoles(user: UserProfile): boolean {
  return _hasCompanyPermission(user, 'roles.view') || _hasCompanyAdminFallback(user);
}

/** Provisioning an existing staged account requires edit authority and visible role choices. */
export function canProvisionPeopleAccount(user: UserProfile): boolean {
  return canEditPeople(user) && canAssignPeopleRole(user) && canManageRoles(user);
}

/** Create a new company role: company-scoped roles.create, or company admin fallback. */
export function canCreateRoles(user: UserProfile): boolean {
  return _hasCompanyPermission(user, 'roles.create') || _hasCompanyAdminFallback(user);
}

/** Edit a company role's permission assignments: company-scoped roles.edit, or company admin fallback. */
export function canEditRoles(user: UserProfile): boolean {
  return _hasCompanyPermission(user, 'roles.edit') || _hasCompanyAdminFallback(user);
}

/** Delete a company role: company-scoped roles.delete, or company admin fallback. */
export function canDeleteRoles(user: UserProfile): boolean {
  return _hasCompanyPermission(user, 'roles.delete') || _hasCompanyAdminFallback(user);
}

/**
 * Full settings admin (company, branches, payroll setup, pay items).
 * Canonical company-scoped setup.manage — mirrors the backend's
 * _ensure_company_admin, not flat or coarse frontend projections.
 */
export function canManageSettingsAdmin(user: UserProfile): boolean {
  return _hasCompanyPermission(user, 'setup.manage');
}

/**
 * True when the user should be able to reach the Pay Items page.
 *
 * Two paths:
 *   1. Full settings admin — canonical company-scoped setup.manage
 *      (canManageSettingsAdmin).
 *   2. Any user with payitems.edit somewhere — needed to browse and request
 *      items.  This broad discovery check uses canonical authority, but actual
 *      branch/company PayDefinition browsing and actions are governed by the scoped
 *      helpers below (canManagePayDefinitionsForBranch, canGovernPayDefinitionsCompanyWide).
 */
export function canViewDailyPayItems(user: UserProfile | null | undefined): boolean {
  if (!user) return false;
  return canManageSettingsAdmin(user) || hasPayItemsEdit(user);
}

// ── Payroll Setup policy helpers ────────────────────────────────────────────────
//
// Mirror backend app/payroll_setup/*.py authorization rules (require_policy_permission,
// _require_not_driver_role, _branch_read_access, _ensure_branch_creator). Backend
// remains the authoritative security boundary — these helpers drive UI visibility only.

/**
 * Company Payroll Setup policy permission: company scope only (AllCompanyBranches
 * via authority.company_permissions) and never for DRIVER/Self users, including
 * mixed Driver + company-admin users — mirrors backend require_policy_permission
 * (company-wide access + _require_not_driver_role + permission at company scope).
 */
function _hasCompanyPolicyPermission(user: UserProfile, code: string): boolean {
  return !isDriverUser(user) && _hasCompanyPermission(user, code);
}

/** View Payroll Setups: company-wide payroll_setup.view, never DRIVER/Self. */
export function canViewPayrollSetups(user: UserProfile): boolean {
  return _hasCompanyPolicyPermission(user, 'payroll_setup.view');
}

/** Manage Payroll Setups (Setup create/metadata/archive, Draft create/edit/discard): company-wide payroll_setup.manage, never DRIVER/Self. */
export function canManagePayrollSetups(user: UserProfile): boolean {
  return _hasCompanyPolicyPermission(user, 'payroll_setup.manage');
}

/** Publish Payroll Setups: company-wide payroll_setup.publish, never DRIVER/Self. */
export function canPublishPayrollSetups(user: UserProfile): boolean {
  return _hasCompanyPolicyPermission(user, 'payroll_setup.publish');
}

/** Assign Payroll Setups to branches: company-wide payroll_setup.assign, never DRIVER/Self. */
export function canAssignPayrollSetups(user: UserProfile): boolean {
  return _hasCompanyPolicyPermission(user, 'payroll_setup.assign');
}

/**
 * Branch read-only Payroll Schedule: exactly payroll.view for that branch (a
 * company-wide grant applies everywhere); never DRIVER/Self. Deliberately NOT
 * PAYROLL_READ — payroll.entry etc. must not grant schedule/history visibility.
 * Mirrors backend _branch_read_access.
 */
export function canViewBranchPayrollSchedule(user: UserProfile, branchId: number): boolean {
  return !isDriverUser(user) && hasAuthorityPermission(user.authority, 'payroll.view', branchId);
}

/**
 * Discovery for the Payroll Schedule page: payroll.view at company scope or on
 * at least one branch; never DRIVER/Self.
 */
export function canViewAnyBranchPayrollSchedule(user: UserProfile): boolean {
  return !isDriverUser(user) && hasAuthorityPermissionAnywhere(user.authority, 'payroll.view');
}

/**
 * Branch creation: company-wide branches.create, non-driver — mirrors backend
 * _ensure_branch_creator (payroll_setup.assign is additionally required only
 * when a first payroll start date is sent; combine with canAssignPayrollSetups
 * at the call site).
 */
export function canCreateBranches(user: UserProfile): boolean {
  return !isDriverUser(user) && _hasCompanyPermission(user, 'branches.create');
}

/**
 * Company & Branches page access: settings admin (setup.manage) or branch
 * creator. Entering the page does not grant edit actions; those keep
 * canManageSettingsAdmin.
 */
export function canAccessCompanyBranches(user: UserProfile): boolean {
  return canManageSettingsAdmin(user) || canCreateBranches(user);
}

// ── Driver Transfer helpers ───────────────────────────────────────────────────

const TRANSFER_READ = [
  'drivers.view',
  'drivers.edit',
  'settings.manage',
  'setup.manage',
] as const;

/**
 * Transfer Requests tab: any drivers.view/edit or admin permission.
 * Mirrors backend _get_accessible_branches_for_transfers gate.
 */
export function canViewTransfers(user: UserProfile): boolean {
  return !isDriverUser(user) && _hasAnyAuthorityPermission(user, TRANSFER_READ);
}

/**
 * Transfer write actions (approve, decide, complete, cancel, create) for a
 * specific branch.  Branch-aware — mirrors the backend's per-branch
 * drivers.edit authority check exactly.  No settings.manage/setup.manage
 * fallback: transfer backend mutation guards require drivers.edit.
 */
export function canEditTransfers(user: UserProfile, branchId: number): boolean {
  if (isDriverUser(user)) return false;
  return hasAuthorityPermission(user.authority, 'drivers.edit', branchId);
}

// ── PayDefinition governance helpers ──────────────────────────────────────────
//
// Mirror backend authorization rules from app/compensation/guards.py.
// Backend remains the authoritative security boundary — these helpers drive
// UI visibility only.  A 403 from the backend is always authoritative.
//
// Backed by the canonical authority contract (hasAuthorityPermission), never
// the flat active_permissions union or reconstructed branch/role metadata.

/**
 * True when the user holds `payitems.edit` somewhere across their assignments.
 *
 * IMPORTANT: this broad discovery predicate does NOT prove which assignment
 * scope holds the permission.  Do NOT use it alone to grant branch-scoped or
 * company-scoped PayDefinition actions; use canManagePayDefinitionsForBranch or
 * canGovernPayDefinitionsCompanyWide instead.
 */
export function hasPayItemsEdit(user: UserProfile | null | undefined): boolean {
  if (!user) return false;
  return _hasAnyAuthorityPermission(user, ['payitems.edit']);
}

/**
 * True when the user holds payitems.edit for the given branch — via a company-wide
 * grant (applies everywhere) or a grant scoped to that specific branch.
 */
export function canManagePayDefinitionsForBranch(
  user: UserProfile | null | undefined,
  branchId: number,
): boolean {
  if (!user) return false;
  return hasAuthorityPermission(user.authority, 'payitems.edit', branchId);
}

/**
 * True when the user holds a company-wide payitems.edit grant.
 *
 * Gates: company review queue (approve / return / reject), direct creation and retirement.
 */
export function canGovernPayDefinitionsCompanyWide(
  user: UserProfile | null | undefined,
): boolean {
  if (!user) return false;
  return hasAuthorityPermission(user.authority, 'payitems.edit', null);
}

/**
 * True when the user can directly create Company PayDefinitions (bypassing the
 * request workflow).  Same gate as canGovernPayDefinitionsCompanyWide.
 */
export function canCreatePayDefinitionDirectly(
  user: UserProfile | null | undefined,
): boolean {
  return canGovernPayDefinitionsCompanyWide(user);
}

/**
 * Edit a PayDefinition's applicability in one Branch: payitems.edit for that
 * branch or company-wide setup.manage. Mirrors backend branch_config permission.
 */
export function canConfigureBranchPayDefinitions(
  user: UserProfile | null | undefined,
  branchId: number,
): boolean {
  if (!user) return false;
  return canManagePayDefinitionsForBranch(user, branchId) || canManageSettingsAdmin(user);
}

/** Apply a PayDefinition's applicability across several Branches at once. */
export function canConfigureAllBranchPayDefinitions(
  user: UserProfile | null | undefined,
): boolean {
  if (!user) return false;
  return canGovernPayDefinitionsCompanyWide(user) || canManageSettingsAdmin(user);
}
