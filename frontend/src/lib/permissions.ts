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

/** Ledger: same gate as Current Payroll (backend uses _PAYROLL_READ_PERMS). */
export function canViewLedger(user: UserProfile): boolean {
  return canViewCurrentPayroll(user);
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
 * Create payroll periods.
 * Requires the dedicated payroll.period.create permission (migration 0030).
 * This permission is separate from payroll.entry — holding one does not imply
 * the other.  Roles that should create periods must be explicitly granted this
 * code via the migration seed.
 */
export function canCreatePeriod(user: UserProfile): boolean {
  return user.active_permissions.includes('payroll.period.create');
}

/**
 * Enter/edit payroll lines, open periods, submit for review, manage Drivers Off
 * and Bonuses.  Requires payroll.entry — the operational data-entry permission.
 * Does NOT imply create-period; that requires payroll.period.create.
 */
export function canEntryPayroll(user: UserProfile): boolean {
  return user.active_permissions.includes('payroll.entry');
}

/** Finalize button / period lifecycle admin actions. */
export function canFinalizePayroll(user: UserProfile): boolean {
  return user.active_permissions.includes('payroll.finalize');
}

/** Approve / return review items. */
export function canDecideReview(user: UserProfile): boolean {
  return user.active_permissions.includes('review.decide');
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
