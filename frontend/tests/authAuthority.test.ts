import { test } from 'node:test';
import assert from 'node:assert/strict';
import { hasAuthorityPermission, toUserProfile } from '../src/store/authStore.ts';
import type { BranchAccess, PermissionAuthority, UserInfoResponse, UserProfile } from '../src/store/authStore.ts';
import {
  canCreatePeriod,
  canEntryPayroll,
  canFinalizePayroll,
  canDecideReview,
  canEditTransfers,
  canEditPayRates,
} from '../src/lib/permissions.ts';

function makeAuthority(overrides: Partial<PermissionAuthority> = {}): PermissionAuthority {
  return {
    company_permissions: [],
    branch_permissions: [],
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

// ── AllCompanyBranches: company-wide grant applies everywhere ─────────────────

test('hasAuthorityPermission: AllCompanyBranches permission grants company scope', () => {
  const authority = makeAuthority({ company_permissions: ['payroll.entry'] });
  assert.equal(hasAuthorityPermission(authority, 'payroll.entry', null), true);
});

test('hasAuthorityPermission: AllCompanyBranches permission grants any concrete branch', () => {
  const authority = makeAuthority({ company_permissions: ['payroll.entry'] });
  assert.equal(hasAuthorityPermission(authority, 'payroll.entry', 10), true);
  assert.equal(hasAuthorityPermission(authority, 'payroll.entry', 99), true);
});

// ── SpecificBranch: no company-wide grant ──────────────────────────────────────

test('hasAuthorityPermission: SpecificBranch-only user is denied at company scope', () => {
  const authority = makeAuthority({
    company_permissions: [],
    branch_permissions: [{ branch_id: 10, permissions: ['payroll.view'] }],
  });
  assert.equal(hasAuthorityPermission(authority, 'payroll.view', null), false);
});

test('hasAuthorityPermission: SpecificBranch grants only its own branch', () => {
  const authority = makeAuthority({
    company_permissions: [],
    branch_permissions: [{ branch_id: 10, permissions: ['payroll.view'] }],
  });
  assert.equal(hasAuthorityPermission(authority, 'payroll.view', 10), true);
  assert.equal(hasAuthorityPermission(authority, 'payroll.view', 20), false);
});

// ── Mixed AllCompanyBranches + SpecificBranch ──────────────────────────────────

test('hasAuthorityPermission: mixed scope unions company and branch-specific grants', () => {
  const authority = makeAuthority({
    company_permissions: ['drivers.view'],
    branch_permissions: [{ branch_id: 10, permissions: ['drivers.view', 'payroll.view'] }],
  });
  assert.equal(hasAuthorityPermission(authority, 'drivers.view', null), true);
  assert.equal(hasAuthorityPermission(authority, 'payroll.view', null), false);
  assert.equal(hasAuthorityPermission(authority, 'drivers.view', 10), true);
  assert.equal(hasAuthorityPermission(authority, 'payroll.view', 10), true);
  // Company-wide grant applies even to a branch with no explicit entry.
  assert.equal(hasAuthorityPermission(authority, 'drivers.view', 999), true);
  assert.equal(hasAuthorityPermission(authority, 'payroll.view', 999), false);
});

// ── Multiple distinct SpecificBranch assignments ───────────────────────────────

test('hasAuthorityPermission: multiple branches are scoped independently', () => {
  const authority = makeAuthority({
    branch_permissions: [
      { branch_id: 10, permissions: ['payroll.view'] },
      { branch_id: 20, permissions: ['drivers.view'] },
    ],
  });
  assert.equal(hasAuthorityPermission(authority, 'payroll.view', 10), true);
  assert.equal(hasAuthorityPermission(authority, 'drivers.view', 10), false);
  assert.equal(hasAuthorityPermission(authority, 'drivers.view', 20), true);
  assert.equal(hasAuthorityPermission(authority, 'payroll.view', 20), false);
});

// ── Unknown permission code ─────────────────────────────────────────────────────

test('hasAuthorityPermission: unknown permission code returns false everywhere', () => {
  const authority = makeAuthority({
    company_permissions: ['payroll.entry'],
    branch_permissions: [{ branch_id: 10, permissions: ['payroll.view'] }],
  });
  assert.equal(hasAuthorityPermission(authority, 'nonexistent.code', null), false);
  assert.equal(hasAuthorityPermission(authority, 'nonexistent.code', 10), false);
});

// ── toUserProfile carries authority through unchanged ──────────────────────────

test('toUserProfile: passes authority through onto UserProfile', () => {
  const authority = makeAuthority({
    company_permissions: ['setup.manage'],
    branch_permissions: [{ branch_id: 10, permissions: ['payroll.view'] }],
  });
  const info = makeUserInfoResponse({ authority });
  const profile = toUserProfile(info);
  assert.deepEqual(profile.authority, authority);
});

test('toUserProfile: preserves active_permissions unchanged alongside authority', () => {
  const info = makeUserInfoResponse({
    active_permissions: ['payroll.view', 'drivers.view'],
    authority: makeAuthority({ company_permissions: ['payroll.view', 'drivers.view'] }),
  });
  const profile = toUserProfile(info);
  assert.deepEqual(profile.active_permissions, ['payroll.view', 'drivers.view']);
});

// ── permissions.ts action helpers: branch-aware authority, not active_permissions union ─────

test('canEntryPayroll: a grant scoped to branch A does not authorize branch B', () => {
  const user = makeUser({
    active_permissions: ['payroll.entry'], // simulates the flat union still containing the code
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['payroll.entry'] }] }),
  });
  assert.equal(canEntryPayroll(user, 20), false);
});

test('canEntryPayroll: a grant scoped to branch B authorizes branch B', () => {
  const user = makeUser({
    active_permissions: ['payroll.entry'],
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['payroll.entry'] }] }),
  });
  assert.equal(canEntryPayroll(user, 10), true);
});

test('canEntryPayroll: an AllCompanyBranches grant authorizes any concrete branch', () => {
  const user = makeUser({
    active_permissions: [], // deliberately empty: proves the check does not read the flat union
    authority: makeAuthority({ company_permissions: ['payroll.entry'] }),
  });
  assert.equal(canEntryPayroll(user, 10), true);
  assert.equal(canEntryPayroll(user, 999), true);
});

test('canEntryPayroll: mixed company + branch-specific assignments authorize per branch', () => {
  const user = makeUser({
    active_permissions: [],
    authority: makeAuthority({
      company_permissions: ['drivers.view'],
      branch_permissions: [{ branch_id: 10, permissions: ['payroll.entry'] }],
    }),
  });
  assert.equal(canEntryPayroll(user, 10), true);
  assert.equal(canEntryPayroll(user, 20), false);
});

test('canEntryPayroll: unioned active_permissions alone no longer authorizes the helper', () => {
  const user = makeUser({
    active_permissions: ['payroll.entry'],
    authority: makeAuthority(), // no company grant, no branch grant
  });
  assert.equal(canEntryPayroll(user, 10), false);
});

test('canCreatePeriod: a grant scoped to branch A does not authorize branch B', () => {
  const user = makeUser({
    active_permissions: ['payroll.period.create'],
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['payroll.period.create'] }] }),
  });
  assert.equal(canCreatePeriod(user, 10), true);
  assert.equal(canCreatePeriod(user, 20), false);
});

test('canFinalizePayroll: a grant scoped to branch A does not authorize branch B', () => {
  const user = makeUser({
    active_permissions: ['payroll.finalize'],
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['payroll.finalize'] }] }),
  });
  assert.equal(canFinalizePayroll(user, 10), true);
  assert.equal(canFinalizePayroll(user, 20), false);
});

test('canDecideReview: a grant scoped to branch A does not authorize branch B', () => {
  const user = makeUser({
    active_permissions: ['review.decide'],
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['review.decide'] }] }),
  });
  assert.equal(canDecideReview(user, 10), true);
  assert.equal(canDecideReview(user, 20), false);
});

// ── canEditTransfers / canEditPayRates: branch-aware authority ──────────────

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

/** User granted `code` only for `grantBranchId` — active_permissions deliberately still contains it. */
function scopedUser(code: string, grantBranchId: number, branchOverrides: Partial<BranchAccess> = {}): UserProfile {
  return makeUser({
    active_permissions: [code],
    branches: [makeBranch({ branch_id: grantBranchId, ...branchOverrides })],
    authority: makeAuthority({ branch_permissions: [{ branch_id: grantBranchId, permissions: [code] }] }),
  });
}

function companyUser(code: string): UserProfile {
  return makeUser({
    active_permissions: [],
    branches: [makeBranch({ scope: 'AllCompanyBranches', branch_id: null })],
    authority: makeAuthority({ company_permissions: [code] }),
  });
}

test('canEditTransfers: branch-scoped drivers.edit denies the wrong branch and authorizes its own', () => {
  const user = scopedUser('drivers.edit', 10);
  assert.equal(canEditTransfers(user, 20), false);
  assert.equal(canEditTransfers(user, 10), true);
});

test('canEditTransfers: company-wide drivers.edit authorizes any concrete branch', () => {
  const user = companyUser('drivers.edit');
  assert.equal(canEditTransfers(user, 10), true);
  assert.equal(canEditTransfers(user, 999), true);
});

test('canEditTransfers: settings.manage/setup.manage grants do not serve as a fallback', () => {
  const user = makeUser({
    active_permissions: ['settings.manage', 'setup.manage'],
    branches: [makeBranch({ branch_id: 10 })],
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['settings.manage', 'setup.manage'] }] }),
  });
  assert.equal(canEditTransfers(user, 10), false);
});

test('canEditTransfers: driver and ODA users are denied despite a matching branch grant', () => {
  assert.equal(canEditTransfers(scopedUser('drivers.edit', 10, { role_code: 'DRIVER' }), 10), false);
  assert.equal(canEditTransfers(scopedUser('drivers.edit', 10, { scope: 'OwnDriverDataOnly' }), 10), false);
});

test('canEditPayRates: branch-scoped payrates.edit denies the wrong branch and authorizes its own', () => {
  const user = scopedUser('payrates.edit', 10);
  assert.equal(canEditPayRates(user, 20), false);
  assert.equal(canEditPayRates(user, 10), true);
});

test('canEditPayRates: branch-scoped settings.manage also authorizes its own branch', () => {
  const user = scopedUser('settings.manage', 10);
  assert.equal(canEditPayRates(user, 10), true);
  assert.equal(canEditPayRates(user, 20), false);
});

test('canEditPayRates: company-wide setup.manage authorizes any concrete branch', () => {
  const user = companyUser('setup.manage');
  assert.equal(canEditPayRates(user, 10), true);
  assert.equal(canEditPayRates(user, 999), true);
});

test('canEditPayRates: driver and ODA users are denied despite a matching branch grant', () => {
  assert.equal(canEditPayRates(scopedUser('payrates.edit', 10, { role_code: 'DRIVER' }), 10), false);
  assert.equal(canEditPayRates(scopedUser('payrates.edit', 10, { scope: 'OwnDriverDataOnly' }), 10), false);
});
