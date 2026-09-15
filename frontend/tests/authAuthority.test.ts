import { test } from 'node:test';
import assert from 'node:assert/strict';
import { hasAuthorityPermission, toUserProfile } from '../src/store/authStore.ts';
import type { PermissionAuthority, UserInfoResponse } from '../src/store/authStore.ts';

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
