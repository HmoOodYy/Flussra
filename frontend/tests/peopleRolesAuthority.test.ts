import { test } from 'node:test';
import assert from 'node:assert/strict';
import { toUserProfile } from '../src/store/authStore.ts';
import type { BranchAccess, PermissionAuthority, UserInfoResponse, UserProfile } from '../src/store/authStore.ts';
import {
  canViewPeople,
  canCreatePeople,
  canEditPeople,
  canTogglePeopleActive,
  canAssignPeopleRole,
  canManageRoles,
  canCreateRoles,
  canEditRoles,
  canDeleteRoles,
  canViewSettings,
} from '../src/lib/permissions.ts';

// ── Fixture helpers (duplicated from authAuthority.test.ts — see project notes:
// that file is already oversized, so this suite keeps its own copies) ─────────

function makeAuthority(overrides: Partial<PermissionAuthority> = {}): PermissionAuthority {
  return {
    company_permissions: [],
    branch_permissions: [],
    ...overrides,
  };
}

function makeBranch(overrides: Partial<BranchAccess> = {}): BranchAccess {
  return {
    branch_id: 10,
    branch_name: 'Branch 10',
    scope: 'SpecificBranch',
    role_code: 'BRANCH_MANAGER',
    role_name: 'Branch Manager',
    ...overrides,
  };
}

function makeUserInfoResponse(overrides: Partial<UserInfoResponse> = {}): UserInfoResponse {
  return {
    user_id: 1,
    username: 'admin',
    display_name: 'Admin User',
    company_id: 1,
    company_name: 'Demo Logistics',
    branches: [],
    active_permissions: [],
    authority: makeAuthority(),
    ...overrides,
  };
}

function makeUser(overrides: Partial<UserInfoResponse> = {}): UserProfile {
  return toUserProfile(makeUserInfoResponse(overrides));
}

/** Branch-only grant for `code` — active_permissions deliberately still contains it (flat union trap). */
function branchOnlyUser(code: string, branchId = 10): UserProfile {
  return makeUser({
    active_permissions: [code],
    branches: [makeBranch({ branch_id: branchId })],
    authority: makeAuthority({ branch_permissions: [{ branch_id: branchId, permissions: [code] }] }),
  });
}

/** Company-wide grant for `code` — active_permissions deliberately empty (proves no union dependency). */
function companyUser(code: string): UserProfile {
  return makeUser({
    active_permissions: [],
    branches: [makeBranch({ scope: 'AllCompanyBranches', branch_id: null })],
    authority: makeAuthority({ company_permissions: [code] }),
  });
}

/** Mixed: one Driver branch row + a valid company-scoped grant for `code`. */
function mixedDriverPlusCompanyUser(code: string): UserProfile {
  return makeUser({
    active_permissions: [],
    branches: [
      makeBranch({ branch_id: 20, role_code: 'DRIVER', scope: 'SpecificBranch' }),
      makeBranch({ scope: 'AllCompanyBranches', branch_id: null }),
    ],
    authority: makeAuthority({ company_permissions: [code] }),
  });
}

// ── canViewPeople ────────────────────────────────────────────────────────────

test('canViewPeople: a branch-only users.view grant does not authorize the page', () => {
  assert.equal(canViewPeople(branchOnlyUser('users.view')), false);
});

test('canViewPeople: a company-wide users.view grant authorizes even with empty active_permissions', () => {
  assert.equal(canViewPeople(companyUser('users.view')), true);
});

test('canViewPeople: company settings.manage/setup.manage authorize as admin fallback', () => {
  assert.equal(canViewPeople(companyUser('settings.manage')), true);
  assert.equal(canViewPeople(companyUser('setup.manage')), true);
});

test('canViewPeople: a mixed Driver branch row + valid company users.view grant is authorized', () => {
  assert.equal(canViewPeople(mixedDriverPlusCompanyUser('users.view')), true);
});

// ── canCreatePeople ──────────────────────────────────────────────────────────

test('canCreatePeople: a branch-only users.create grant does not authorize', () => {
  assert.equal(canCreatePeople(branchOnlyUser('users.create')), false);
});

test('canCreatePeople: a company-wide users.create grant authorizes with empty active_permissions', () => {
  assert.equal(canCreatePeople(companyUser('users.create')), true);
});

test('canCreatePeople: company admin fallback authorizes', () => {
  assert.equal(canCreatePeople(companyUser('setup.manage')), true);
});

// ── canEditPeople ────────────────────────────────────────────────────────────

test('canEditPeople: a branch-only users.edit grant does not authorize', () => {
  assert.equal(canEditPeople(branchOnlyUser('users.edit')), false);
});

test('canEditPeople: a company-wide users.edit grant authorizes with empty active_permissions', () => {
  assert.equal(canEditPeople(companyUser('users.edit')), true);
});

test('canEditPeople: users.deactivate alone does NOT authorize profile/password/override editing', () => {
  assert.equal(canEditPeople(companyUser('users.deactivate')), false);
  assert.equal(canEditPeople(branchOnlyUser('users.deactivate')), false);
});

test('canEditPeople: mixed Driver branch row + valid company users.edit grant is authorized', () => {
  assert.equal(canEditPeople(mixedDriverPlusCompanyUser('users.edit')), true);
});

// ── canTogglePeopleActive (activation/deactivation helper) ─────────────────

test('canTogglePeopleActive: company users.deactivate authorizes toggle', () => {
  assert.equal(canTogglePeopleActive(companyUser('users.deactivate')), true);
});

test('canTogglePeopleActive: branch-only users.deactivate does not authorize', () => {
  assert.equal(canTogglePeopleActive(branchOnlyUser('users.deactivate')), false);
});

test('canTogglePeopleActive: company users.edit also authorizes toggle', () => {
  assert.equal(canTogglePeopleActive(companyUser('users.edit')), true);
});

test('canTogglePeopleActive: company admin fallback authorizes toggle', () => {
  assert.equal(canTogglePeopleActive(companyUser('settings.manage')), true);
});

// ── canAssignPeopleRole (company-role assignment helper) ───────────────────

test('canAssignPeopleRole: company roles.edit authorizes assignment', () => {
  assert.equal(canAssignPeopleRole(companyUser('roles.edit')), true);
});

test('canAssignPeopleRole: branch-only roles.edit does NOT authorize assignment', () => {
  assert.equal(canAssignPeopleRole(branchOnlyUser('roles.edit')), false);
});

test('canAssignPeopleRole: company users.edit also authorizes assignment', () => {
  assert.equal(canAssignPeopleRole(companyUser('users.edit')), true);
});

test('canAssignPeopleRole: company admin fallback authorizes assignment', () => {
  assert.equal(canAssignPeopleRole(companyUser('setup.manage')), true);
});

test('canAssignPeopleRole: mixed Driver branch row + valid company roles.edit grant is authorized', () => {
  assert.equal(canAssignPeopleRole(mixedDriverPlusCompanyUser('roles.edit')), true);
});

// ── Role helpers: view/create/edit/delete stay distinct ─────────────────────

test('canManageRoles: company roles.view authorizes; branch-only roles.view does not', () => {
  assert.equal(canManageRoles(companyUser('roles.view')), true);
  assert.equal(canManageRoles(branchOnlyUser('roles.view')), false);
});

test('canManageRoles: company admin fallback authorizes without roles.view', () => {
  assert.equal(canManageRoles(companyUser('settings.manage')), true);
});

test('canCreateRoles: company roles.create authorizes; branch-only roles.create does not', () => {
  assert.equal(canCreateRoles(companyUser('roles.create')), true);
  assert.equal(canCreateRoles(branchOnlyUser('roles.create')), false);
});

test('canCreateRoles: does not authorize on roles.view or roles.edit alone', () => {
  assert.equal(canCreateRoles(companyUser('roles.view')), false);
  assert.equal(canCreateRoles(companyUser('roles.edit')), false);
});

test('canCreateRoles: company admin fallback authorizes', () => {
  assert.equal(canCreateRoles(companyUser('setup.manage')), true);
});

test('canEditRoles: company roles.edit authorizes; branch-only roles.edit does not', () => {
  assert.equal(canEditRoles(companyUser('roles.edit')), true);
  assert.equal(canEditRoles(branchOnlyUser('roles.edit')), false);
});

test('canEditRoles: does not authorize on roles.view or roles.delete alone', () => {
  assert.equal(canEditRoles(companyUser('roles.view')), false);
  assert.equal(canEditRoles(companyUser('roles.delete')), false);
});

test('canEditRoles: company admin fallback authorizes', () => {
  assert.equal(canEditRoles(companyUser('settings.manage')), true);
});

test('canDeleteRoles: company roles.delete authorizes; branch-only roles.delete does not', () => {
  assert.equal(canDeleteRoles(companyUser('roles.delete')), true);
  assert.equal(canDeleteRoles(branchOnlyUser('roles.delete')), false);
});

test('canDeleteRoles: does not authorize on roles.view or roles.edit alone', () => {
  assert.equal(canDeleteRoles(companyUser('roles.view')), false);
  assert.equal(canDeleteRoles(companyUser('roles.edit')), false);
});

test('canDeleteRoles: company admin fallback authorizes', () => {
  assert.equal(canDeleteRoles(companyUser('setup.manage')), true);
});

// ── Cross-check: role helpers stay independent of each other ───────────────

test('role helpers are independently gated: a roles.create-only grant cannot edit or delete', () => {
  const user = companyUser('roles.create');
  assert.equal(canCreateRoles(user), true);
  assert.equal(canEditRoles(user), false);
  assert.equal(canDeleteRoles(user), false);
});

// ── canViewSettings: company-scoped authority, not union-derived has_setup_manage ──

test('canViewSettings: branch-only settings.manage (union-derived has_setup_manage) is denied', () => {
  const user = branchOnlyUser('settings.manage');
  assert.equal(user.has_setup_manage, true); // proves the flat-union trap this helper must not fall into
  assert.equal(canViewSettings(user), false);
});

test('canViewSettings: company setup.manage authorizes with empty active_permissions', () => {
  assert.equal(canViewSettings(companyUser('setup.manage')), true);
});

test('canViewSettings: mixed Driver branch row + company roles.view is authorized', () => {
  assert.equal(canViewSettings(mixedDriverPlusCompanyUser('roles.view')), true);
});
