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
import { hasAuthorityPermission } from '../store/authStore.ts';

// ── Private helpers ───────────────────────────────────────────────────────────

function _hasAny(user: UserProfile, codes: readonly string[]): boolean {
  return codes.some((c) => user.active_permissions.includes(c));
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
 * True when the user has any Driver or ODA assignment.
 *
 * Fails closed — any of the following marks the user as driver/ODA:
 *   • Any branch row with scope === 'OwnDriverDataOnly'
 *   • Any branch row with role_code === 'DRIVER'
 *   • top-level scope_type === 'OwnDriverDataOnly' (pure-ODA, all rows matched)
 *
 * This correctly handles:
 *   - Pure ODA accounts        (all branches have ODA scope)
 *   - Driver + SpecificBranch  (one branch has role_code DRIVER)
 *   - Mixed ODA + operational  (at least one ODA row present)
 *
 * Do NOT use scope_type === 'OwnDriverDataOnly' alone — that only fires
 * when every row is ODA and misses Driver+SpecificBranch.
 */
export function isDriverUser(user: UserProfile): boolean {
  if (user.scope_type === 'OwnDriverDataOnly') return true;
  return user.branches.some(
    (b) => b.scope === 'OwnDriverDataOnly' || b.role_code === 'DRIVER'
  );
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
  return !isDriverUser(user) && _hasAny(user, PAYROLL_READ);
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
  return !isDriverUser(user) && user.active_permissions.includes('ledger.view');
}

/** Legacy raw FinalLines: mirror its operational backend read permissions. */
export function canViewFinalLines(user: UserProfile): boolean {
  return (
    !isDriverUser(user) &&
    _hasAny(user, ['payroll.view', 'payroll.entry', 'payroll.finalize'])
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
  return !isDriverUser(user) && _hasAny(user, [...PAYROLL_READ, 'review.decide']);
}

/** Pay Rates page: payrates.view/edit or admin fallback. */
export function canViewPayRates(user: UserProfile): boolean {
  return !isDriverUser(user) && _hasAny(user, PAYRATES_ACCESS);
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
 * authority.company_permissions (prefix inspection) — not user.has_setup_manage
 * (which is derived from the flat active_permissions union and so cannot be
 * trusted for a company-scope route decision) and not the flat
 * active_permissions union directly — so a mixed Driver + valid company
 * roles/users assignment still reaches the nested Roles route, while a
 * branch-only settings.manage grant does not.
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
 * _ensure_company_admin, not the flat has_setup_manage union.
 */
export function canManageSettingsAdmin(user: UserProfile): boolean {
  return _hasCompanyPermission(user, 'setup.manage');
}

/**
 * True when the user should be able to reach the Daily Pay Items page.
 *
 * Two paths:
 *   1. Full settings admin — canonical company-scoped setup.manage
 *      (canManageSettingsAdmin).
 *   2. Any user with payitems.edit somewhere — needed to browse CDPI-approved
 *      items.  Note: this is a flat union check (see hasPayItemsEdit docs);
 *      actual branch/company CDPI browsing and actions are governed by the
 *      branch-aware helpers below (canManageCdpiForBranch,
 *      canReviewCdpiCompanyWide), not by this union check alone.
 */
export function canViewDailyPayItems(user: UserProfile | null | undefined): boolean {
  if (!user) return false;
  return canManageSettingsAdmin(user) || hasPayItemsEdit(user);
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
  return !isDriverUser(user) && _hasAny(user, TRANSFER_READ);
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

// ── CDPI (Custom Daily Pay Item) helpers ──────────────────────────────────────
//
// Mirror backend authorization rules from app/cdpi/guards.py.
// Backend remains the authoritative security boundary — these helpers drive
// UI visibility only.  A 403 from the backend is always authoritative.
//
// Backed by the canonical authority contract (hasAuthorityPermission), never
// the flat active_permissions union or reconstructed branch/role metadata.

/**
 * True when the user holds `payitems.edit` somewhere across their assignments.
 *
 * IMPORTANT: this is a flat union check — it does NOT prove which assignment
 * scope holds the permission.  Do NOT use this alone to grant branch-scoped or
 * company-scoped CDPI actions; use canManageCdpiForBranch / canReviewCdpiCompanyWide
 * which are backed by the canonical branch-aware authority contract instead.
 */
export function hasPayItemsEdit(user: UserProfile | null | undefined): boolean {
  if (!user) return false;
  return user.active_permissions.includes('payitems.edit');
}

/**
 * True when the user holds payitems.edit for the given branch — via a company-wide
 * grant (applies everywhere) or a grant scoped to that specific branch.
 */
export function canManageCdpiForBranch(
  user: UserProfile | null | undefined,
  branchId: number,
): boolean {
  if (!user) return false;
  return hasAuthorityPermission(user.authority, 'payitems.edit', branchId);
}

/**
 * True when the user holds a company-wide payitems.edit grant.
 *
 * Gates: company review queue (approve / return / reject), direct company item creation.
 */
export function canReviewCdpiCompanyWide(
  user: UserProfile | null | undefined,
): boolean {
  if (!user) return false;
  return hasAuthorityPermission(user.authority, 'payitems.edit', null);
}

/**
 * True when the user can directly create CDPI company items (bypassing the
 * request workflow).  Same gate as canReviewCdpiCompanyWide.
 */
export function canDirectCreateCdpiCompanyItem(
  user: UserProfile | null | undefined,
): boolean {
  return canReviewCdpiCompanyWide(user);
}
