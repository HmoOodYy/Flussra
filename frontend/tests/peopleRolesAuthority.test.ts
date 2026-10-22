import { makeAuthority, makeBranchAccess as makeBranch } from './authorityTestFixtures.ts';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { toUserProfile } from '../src/store/authStore.ts';
import type { UserInfoResponse, UserProfile } from '../src/store/authStore.ts';
import {
  canViewAccess,
  canCreatePeople,
  canEditPeople,
  canTogglePeopleActive,
  canAssignPeopleRole,
  canManageRoles,
  canProvisionPeopleAccount,
  canCreateRoles,
  canEditRoles,
  canDeleteRoles,
  canViewSettings,
} from '../src/lib/permissions.ts';

const accessControllerSource = readFileSync(new URL('../src/pages/people/access/useAccessController.ts', import.meta.url), 'utf8');
const accessActionsSource = readFileSync(new URL('../src/pages/people/access/useAccessActions.ts', import.meta.url), 'utf8');
const accessDetailSource = readFileSync(new URL('../src/pages/people/access/AccessDetail.tsx', import.meta.url), 'utf8');
const peopleFormControlsSource = readFileSync(new URL('../src/pages/people/PeopleFormControls.tsx', import.meta.url), 'utf8');

// Authority and BranchAccess defaults are shared; this suite keeps its admin profile local.

function makeUserInfoResponse(overrides: Partial<UserInfoResponse> = {}): UserInfoResponse {
  return {
    user_id: 1,
    username: 'admin',
    display_name: 'Admin User',
    company_id: 1,
    company_name: 'Demo Logistics',
    branches: [],
    self_assignments: [],
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

// ── canViewAccess ────────────────────────────────────────────────────────────

test('canViewAccess: a branch-only users.view grant does not authorize the page', () => {
  assert.equal(canViewAccess(branchOnlyUser('users.view')), false);
});

test('canViewAccess: a company-wide users.view grant authorizes even with empty active_permissions', () => {
  assert.equal(canViewAccess(companyUser('users.view')), true);
});

test('canViewAccess: company settings.manage/setup.manage authorize as admin fallback', () => {
  assert.equal(canViewAccess(companyUser('settings.manage')), true);
  assert.equal(canViewAccess(companyUser('setup.manage')), true);
});

test('canViewAccess: a mixed Driver branch row + valid company users.view grant is authorized', () => {
  assert.equal(canViewAccess(mixedDriverPlusCompanyUser('users.view')), true);
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

test('canProvisionPeopleAccount requires users.edit and visible role choices', () => {
  const roleEditorWithoutUserEdit = makeUser({
    authority: makeAuthority({ company_permissions: ['users.create', 'roles.edit', 'roles.view'] }),
  });
  assert.equal(canProvisionPeopleAccount(roleEditorWithoutUserEdit), false);

  const editorWithoutRoleVisibility = companyUser('users.edit');
  assert.equal(canProvisionPeopleAccount(editorWithoutRoleVisibility), false);

  const editorWithRoleVisibility = makeUser({
    authority: makeAuthority({ company_permissions: ['users.edit', 'roles.view'] }),
  });
  assert.equal(canProvisionPeopleAccount(editorWithRoleVisibility), true);
  assert.match(accessControllerSource, /provision: authUser \? canProvisionPeopleAccount\(authUser\) : false/);
  assert.match(accessDetailSource, /can\.provision/);
});

test('scope choices give DRIVER only Self and keep ordinary scopes for other roles', () => {
  assert.match(peopleFormControlsSource, /!isDriverRole && \(\s*<option value="AllCompanyBranches">/);
  assert.match(peopleFormControlsSource, /!isDriverRole && <option value="SpecificBranch">/);
  assert.match(peopleFormControlsSource, /isDriverRole && <option value="Self">Self — own current Driver identity<\/option>/);
  assert.match(peopleFormControlsSource, /DRIVER assignments use Self without a branch/);
});

test('ownership replacement options exclude Driver without removing it from normal role choices', () => {
  assert.match(accessControllerSource, /ownerTransferRoles = st\.roles\.filter\(r => r\.is_active && r\.role_code !== 'COMPANY_OWNER' && r\.role_code !== 'DRIVER'\)/);
  assert.match(accessControllerSource, /assignableRoles = st\.roles\.filter\(r => r\.role_code !== 'COMPANY_OWNER' && r\.is_active\)/);
});

test('account creation and provisioning are single atomic requests that keep the dialog open on failure', () => {
  assert.match(accessActionsSource, /createAdminUser\(payload\)/);
  assert.match(accessActionsSource, /provisionAdminUser\(selected\.user_id, payload\)/);
  assert.match(accessActionsSource, /Nothing was persisted: the dialog stays open/);
  assert.match(accessActionsSource, /type: 'PROVISION_ERROR'/);
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

// ── canViewSettings: company-scoped authority, not unioned active_permissions ──

test('canViewSettings: branch-only settings.manage in active_permissions is denied', () => {
  const user = branchOnlyUser('settings.manage');
  assert.equal(canViewSettings(user), false);
});

test('canViewSettings: company setup.manage authorizes with empty active_permissions', () => {
  assert.equal(canViewSettings(companyUser('setup.manage')), true);
});

test('canViewSettings: mixed Driver branch row + company roles.view is authorized', () => {
  assert.equal(canViewSettings(mixedDriverPlusCompanyUser('roles.view')), true);
});
