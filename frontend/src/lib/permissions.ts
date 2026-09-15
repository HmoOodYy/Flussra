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

const PEOPLE_ACCESS = [
  'users.view',
  'users.edit',
  'users.create',
  'settings.manage',
  'setup.manage',
] as const;

/** Current Payroll page: any payroll read/write permission. */
export function canViewCurrentPayroll(user: UserProfile): boolean {
  return !isDriverUser(user) && _hasAny(user, PAYROLL_READ);
}

/** Current Payroll reports are UI-only and require the dedicated report permission. */
export function canViewPayrollReports(user: UserProfile): boolean {
  return !isDriverUser(user) && user.active_permissions.includes('reports.view');
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

/** People page: users.view/edit/create or admin fallback. */
export function canViewPeople(user: UserProfile): boolean {
  return !isDriverUser(user) && _hasAny(user, PEOPLE_ACCESS);
}

/** Settings pages: setup/settings admin or any role/user admin permission. */
export function canViewSettings(user: UserProfile): boolean {
  return (
    !isDriverUser(user) &&
    (user.has_setup_manage ||
      user.active_permissions.some(
        (p) => p.startsWith('roles.') || p.startsWith('users.')
      ))
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

/** Finalize button / period lifecycle admin actions for a specific branch. */
export function canFinalizePayroll(user: UserProfile, branchId: number): boolean {
  return hasAuthorityPermission(user.authority, 'payroll.finalize', branchId);
}

/** Approve / return review items for a specific branch. */
export function canDecideReview(user: UserProfile, branchId: number): boolean {
  return hasAuthorityPermission(user.authority, 'review.decide', branchId);
}

/** Edit/approve pay rates. */
export function canEditPayRates(user: UserProfile): boolean {
  return _hasAny(user, ['payrates.edit', 'settings.manage', 'setup.manage']);
}

/** Create new people/users. */
export function canCreatePeople(user: UserProfile): boolean {
  return _hasAny(user, ['users.create', 'settings.manage', 'setup.manage']);
}

/** Edit existing people (profile, role, password, extra perms, deactivate). */
export function canEditPeople(user: UserProfile): boolean {
  return _hasAny(user, ['users.edit', 'users.deactivate', 'settings.manage', 'setup.manage']);
}

/** View or edit roles and their permission assignments. */
export function canManageRoles(user: UserProfile): boolean {
  return _hasAny(user, ['roles.view', 'roles.edit', 'settings.manage', 'setup.manage']);
}

/** Full settings admin (company, branches, payroll setup, pay items). */
export function canManageSettingsAdmin(user: UserProfile): boolean {
  return user.has_setup_manage;
}

/**
 * True when the user should be able to reach the Daily Pay Items page.
 *
 * Two paths:
 *   1. Full settings admin (has_setup_manage) — existing behavior, unchanged.
 *   2. Any user with payitems.edit somewhere — needed to browse CDPI-approved
 *      items.  Note: this is a flat union check (see hasPayItemsEdit docs).
 *      Legacy mutation actions on the page remain gated behind isAdmin (which
 *      requires has_setup_manage + AllCompanyBranches) and are unaffected.
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
 * Transfer write actions (approve, decide, complete, cancel, create).
 * Backend enforces branch-level drivers.edit — frontend gating is UI-only.
 */
export function canEditTransfers(user: UserProfile): boolean {
  return !isDriverUser(user) && _hasAny(user, ['drivers.edit', 'settings.manage', 'setup.manage']);
}

// ── CDPI (Custom Daily Pay Item) helpers ──────────────────────────────────────
//
// Mirror backend authorization rules from app/cdpi/guards.py.
// Backend remains the authoritative security boundary — these helpers drive
// UI visibility only.  A 403 from the backend is always authoritative.
//
// ── Auth model gap ────────────────────────────────────────────────────────────
// BranchAccess (from /auth/me) exposes: scope, role_code, role_name — but NOT
// a per-assignment permission code list.  active_permissions on UserProfile is a
// flat union across ALL active role assignments, so it is impossible to prove
// "this specific AllCompanyBranches assignment has payitems.edit" vs "some
// SpecificBranch assignment has payitems.edit."
//
// To avoid overgrant, helpers below return false in any ambiguous mixed-scope
// case.  They return true only when per-assignment scope can be inferred safely:
//   • All assignments are AllCompanyBranches → any permission in active_permissions
//     must have come from an AllCompanyBranches assignment.
//   • Exactly one SpecificBranch assignment matching the target branch → all
//     permissions come from that one assignment.
//
// When the auth model is extended to include per-assignment permission codes,
// these helpers should be updated to use that data instead.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * True when the user holds `payitems.edit` somewhere across their assignments.
 *
 * IMPORTANT: this is a flat union check — it does NOT prove which assignment
 * scope holds the permission.  Do NOT use this alone to grant branch-scoped or
 * company-scoped CDPI actions; use canManageCdpiForBranch / canReviewCdpiCompanyWide
 * which perform the additional scope safety check.
 */
export function hasPayItemsEdit(user: UserProfile | null | undefined): boolean {
  if (!user) return false;
  return user.active_permissions.includes('payitems.edit');
}

/**
 * Private: returns true only when EVERY branch assignment is AllCompanyBranches
 * AND active_permissions includes payitems.edit.
 *
 * When all assignments are AllCompanyBranches, payitems.edit cannot come from a
 * SpecificBranch row, so the flat union check is safe.
 */
function _allBranchesAllCompanyWithPayItemsEdit(user: UserProfile): boolean {
  return (
    user.branches.length > 0 &&
    user.branches.every((b) => b.scope === 'AllCompanyBranches') &&
    user.active_permissions.includes('payitems.edit')
  );
}

/**
 * True when the frontend can prove the user holds payitems.edit from an
 * assignment that covers the given branch.
 *
 * Safe cases (provable without per-assignment permission codes):
 *   1. All assignments are AllCompanyBranches and active_permissions includes
 *      payitems.edit — payitems.edit must come from an AllCompanyBranches row.
 *   2. User has exactly one assignment, it is SpecificBranch for this branchId,
 *      and active_permissions includes payitems.edit — the single assignment
 *      uniquely determines the permission source.
 *
 * All other cases (mixed scope, multiple SpecificBranch rows) are ambiguous and
 * return false conservatively.  The backend will enforce the real boundary.
 *
 * Auth model gap: BranchAccess lacks per-assignment permission codes.
 * Multi-branch SpecificBranch users with payitems.edit will see false here until
 * the auth model exposes per-assignment permissions.
 */
export function canManageCdpiForBranch(
  user: UserProfile | null | undefined,
  branchId: number,
): boolean {
  if (!user) return false;

  // Case 1: every assignment is AllCompanyBranches → permission source is proven.
  if (_allBranchesAllCompanyWithPayItemsEdit(user)) return true;

  // Case 2: exactly one SpecificBranch assignment for this branch → unambiguous.
  if (
    user.branches.length === 1 &&
    user.branches[0].scope === 'SpecificBranch' &&
    user.branches[0].branch_id === branchId &&
    user.active_permissions.includes('payitems.edit')
  ) {
    return true;
  }

  // All other cases: mixed assignments or multiple SpecificBranch rows.
  // Cannot safely attribute payitems.edit to the target branch → false.
  return false;
}

/**
 * True when the frontend can prove the user holds payitems.edit from an
 * AllCompanyBranches assignment.
 *
 * Safe only when every branch row is AllCompanyBranches — then the flat
 * active_permissions check cannot be contaminated by a SpecificBranch permission.
 * Returns false when any SpecificBranch assignments exist (ambiguous).
 *
 * Gates: company review queue (approve / return / reject), direct company item creation.
 *
 * Auth model gap: BranchAccess lacks per-assignment permission codes.
 */
export function canReviewCdpiCompanyWide(
  user: UserProfile | null | undefined,
): boolean {
  if (!user) return false;
  return _allBranchesAllCompanyWithPayItemsEdit(user);
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
