import { test } from 'node:test';
import assert from 'node:assert/strict';
import { toUserProfile } from '../src/store/authStore.ts';
import type { BranchAccess, PermissionAuthority, UserInfoResponse, UserProfile } from '../src/store/authStore.ts';
import {
  canManagePayDefinitionsForBranch,
  canGovernPayDefinitionsCompanyWide,
  canCreatePayDefinitionDirectly,
  canConfigureAllBranchPayDefinitions,
  canConfigureBranchPayDefinitions,
  canViewDailyPayItems,
} from '../src/lib/permissions.ts';

// ── Fixture helpers (duplicated from authAuthority.test.ts / peopleRolesAuthority.test.ts —
// see project notes: those files are already oversized, so this suite keeps its own copies) ──

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
    self_assignments: [],
    active_permissions: [],
    authority: makeAuthority(),
    ...overrides,
  };
}

function makeUser(overrides: Partial<UserInfoResponse> = {}): UserProfile {
  return toUserProfile(makeUserInfoResponse(overrides));
}

/** Company-wide payitems.edit grant — active_permissions deliberately empty (proves no flat-union dependency). */
function companyPayItemsEditUser(): UserProfile {
  return makeUser({
    active_permissions: [],
    branches: [makeBranch({ scope: 'AllCompanyBranches', branch_id: null })],
    authority: makeAuthority({ company_permissions: ['payitems.edit'] }),
  });
}

/** Branch-only payitems.edit grant for a single branch — active_permissions still contains it (flat-union trap). */
function branchOnlyPayItemsEditUser(branchId: number): UserProfile {
  return makeUser({
    active_permissions: ['payitems.edit'],
    branches: [makeBranch({ branch_id: branchId })],
    authority: makeAuthority({ branch_permissions: [{ branch_id: branchId, permissions: ['payitems.edit'] }] }),
  });
}

// ── null user is always denied ──────────────────────────────────────────────

test('canManagePayDefinitionsForBranch: null user is denied', () => {
  assert.equal(canManagePayDefinitionsForBranch(null, 10), false);
});

test('canGovernPayDefinitionsCompanyWide: null user is denied', () => {
  assert.equal(canGovernPayDefinitionsCompanyWide(null), false);
});

test('canCreatePayDefinitionDirectly: null user is denied', () => {
  assert.equal(canCreatePayDefinitionDirectly(null), false);
});

// ── Company-scoped payitems.edit grants branch + company actions ───────────

test('canGovernPayDefinitionsCompanyWide: company payitems.edit authorizes even with empty active_permissions', () => {
  assert.equal(canGovernPayDefinitionsCompanyWide(companyPayItemsEditUser()), true);
});

test('canCreatePayDefinitionDirectly: company payitems.edit authorizes direct create even with empty active_permissions', () => {
  assert.equal(canCreatePayDefinitionDirectly(companyPayItemsEditUser()), true);
});

test('canManagePayDefinitionsForBranch: company payitems.edit authorizes any concrete branch even with empty active_permissions', () => {
  const user = companyPayItemsEditUser();
  assert.equal(canManagePayDefinitionsForBranch(user, 10), true);
  assert.equal(canManagePayDefinitionsForBranch(user, 999), true);
});

// ── Branch-only payitems.edit grants only its own branch, never company scope ──

test('canManagePayDefinitionsForBranch: branch-only payitems.edit authorizes only its own branch', () => {
  const user = branchOnlyPayItemsEditUser(10);
  assert.equal(canManagePayDefinitionsForBranch(user, 10), true);
  assert.equal(canManagePayDefinitionsForBranch(user, 20), false);
});

test('canGovernPayDefinitionsCompanyWide: branch-only payitems.edit does not authorize company-wide review', () => {
  assert.equal(canGovernPayDefinitionsCompanyWide(branchOnlyPayItemsEditUser(10)), false);
});

test('canCreatePayDefinitionDirectly: branch-only payitems.edit does not authorize direct create', () => {
  assert.equal(canCreatePayDefinitionDirectly(branchOnlyPayItemsEditUser(10)), false);
});

// ── Multiple SpecificBranch authority entries are independently honored ────
// (former false negative: legacy helper required exactly one branch assignment
// and returned false for any user with more than one SpecificBranch row)

test('canManagePayDefinitionsForBranch: multiple SpecificBranch authority entries are independently honored', () => {
  const user = makeUser({
    active_permissions: ['payitems.edit'],
    branches: [makeBranch({ branch_id: 10 }), makeBranch({ branch_id: 20 })],
    authority: makeAuthority({
      branch_permissions: [
        { branch_id: 10, permissions: ['payitems.edit'] },
        { branch_id: 20, permissions: ['payitems.edit'] },
      ],
    }),
  });
  assert.equal(canManagePayDefinitionsForBranch(user, 10), true);
  assert.equal(canManagePayDefinitionsForBranch(user, 20), true);
  assert.equal(canManagePayDefinitionsForBranch(user, 30), false);
});

// ── Flat active_permissions / branch metadata alone must not grant ─────────

test('canManagePayDefinitionsForBranch: flat active_permissions without an authority grant does not authorize', () => {
  const user = makeUser({
    active_permissions: ['payitems.edit'],
    branches: [makeBranch({ branch_id: 10 })],
    authority: makeAuthority(), // no branch or company grant
  });
  assert.equal(canManagePayDefinitionsForBranch(user, 10), false);
});

test('canGovernPayDefinitionsCompanyWide: AllCompanyBranches branch metadata + flat active_permissions without company authority does not authorize', () => {
  const user = makeUser({
    active_permissions: ['payitems.edit'],
    branches: [makeBranch({ scope: 'AllCompanyBranches', branch_id: null })],
    authority: makeAuthority(), // no company grant despite matching branch metadata
  });
  assert.equal(canGovernPayDefinitionsCompanyWide(user), false);
  assert.equal(canCreatePayDefinitionDirectly(user), false);
  assert.equal(canManagePayDefinitionsForBranch(user, 10), false);
});

// ── Branch applicability configuration ─────────────────────────────────────

test('canConfigureBranchPayDefinitions: payitems.edit on the branch or company setup.manage authorizes; flat permissions do not', () => {
  assert.equal(canConfigureBranchPayDefinitions(null, 10), false);
  assert.equal(canConfigureBranchPayDefinitions(branchOnlyPayItemsEditUser(10), 10), true);
  assert.equal(canConfigureBranchPayDefinitions(branchOnlyPayItemsEditUser(10), 20), false);
  assert.equal(canConfigureBranchPayDefinitions(companyPayItemsEditUser(), 999), true);
  const setupAdmin = makeUser({
    authority: makeAuthority({ company_permissions: ['setup.manage'] }),
  });
  assert.equal(canConfigureBranchPayDefinitions(setupAdmin, 10), true);
  const flatOnly = makeUser({ active_permissions: ['payitems.edit', 'setup.manage'] });
  assert.equal(canConfigureBranchPayDefinitions(flatOnly, 10), false);
});

test('canConfigureAllBranchPayDefinitions: needs company-wide authority, not a single branch grant', () => {
  assert.equal(canConfigureAllBranchPayDefinitions(null), false);
  assert.equal(canConfigureAllBranchPayDefinitions(branchOnlyPayItemsEditUser(10)), false);
  assert.equal(canConfigureAllBranchPayDefinitions(companyPayItemsEditUser()), true);
});

// ── Branch-scoped setup.manage (backend: payitems.edit OR setup.manage for the Branch) ──

function branchOnlySetupManageUser(branchId: number): UserProfile {
  return makeUser({
    active_permissions: [],
    branches: [makeBranch({ branch_id: branchId })],
    authority: makeAuthority({
      branch_permissions: [{ branch_id: branchId, permissions: ['setup.manage'] }],
    }),
  });
}

test('branch setup.manage: configures its own Branch only', () => {
  const user = branchOnlySetupManageUser(10);
  assert.equal(canConfigureBranchPayDefinitions(user, 10), true);
  assert.equal(canConfigureBranchPayDefinitions(user, 20), false);
});

test('branch setup.manage: the Pay Items page is discoverable', () => {
  assert.equal(canViewDailyPayItems(branchOnlySetupManageUser(10)), true);
  assert.equal(canViewDailyPayItems(makeUser()), false);
});

test('branch setup.manage: grants no company-wide PayDefinition operation', () => {
  const user = branchOnlySetupManageUser(10);
  assert.equal(canCreatePayDefinitionDirectly(user), false);
  assert.equal(canGovernPayDefinitionsCompanyWide(user), false);
  assert.equal(canConfigureAllBranchPayDefinitions(user), false);
  assert.equal(canManagePayDefinitionsForBranch(user, 10), false);
});

test('company setup.manage: configures every Branch and the all-branch operation', () => {
  const user = makeUser({
    branches: [makeBranch({ scope: 'AllCompanyBranches', branch_id: null })],
    authority: makeAuthority({ company_permissions: ['setup.manage'] }),
  });
  assert.equal(canConfigureBranchPayDefinitions(user, 10), true);
  assert.equal(canConfigureBranchPayDefinitions(user, 999), true);
  assert.equal(canConfigureAllBranchPayDefinitions(user), true);
});

test('setup.manage in flat active_permissions alone is never authority', () => {
  const user = makeUser({ active_permissions: ['setup.manage'] });
  assert.equal(canConfigureBranchPayDefinitions(user, 10), false);
  assert.equal(canViewDailyPayItems(user), false);
});
