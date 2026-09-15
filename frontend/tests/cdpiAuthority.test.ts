import { test } from 'node:test';
import assert from 'node:assert/strict';
import { toUserProfile } from '../src/store/authStore.ts';
import type { BranchAccess, PermissionAuthority, UserInfoResponse, UserProfile } from '../src/store/authStore.ts';
import {
  canManageCdpiForBranch,
  canReviewCdpiCompanyWide,
  canDirectCreateCdpiCompanyItem,
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

test('canManageCdpiForBranch: null user is denied', () => {
  assert.equal(canManageCdpiForBranch(null, 10), false);
});

test('canReviewCdpiCompanyWide: null user is denied', () => {
  assert.equal(canReviewCdpiCompanyWide(null), false);
});

test('canDirectCreateCdpiCompanyItem: null user is denied', () => {
  assert.equal(canDirectCreateCdpiCompanyItem(null), false);
});

// ── Company-scoped payitems.edit grants branch + company actions ───────────

test('canReviewCdpiCompanyWide: company payitems.edit authorizes even with empty active_permissions', () => {
  assert.equal(canReviewCdpiCompanyWide(companyPayItemsEditUser()), true);
});

test('canDirectCreateCdpiCompanyItem: company payitems.edit authorizes direct create even with empty active_permissions', () => {
  assert.equal(canDirectCreateCdpiCompanyItem(companyPayItemsEditUser()), true);
});

test('canManageCdpiForBranch: company payitems.edit authorizes any concrete branch even with empty active_permissions', () => {
  const user = companyPayItemsEditUser();
  assert.equal(canManageCdpiForBranch(user, 10), true);
  assert.equal(canManageCdpiForBranch(user, 999), true);
});

// ── Branch-only payitems.edit grants only its own branch, never company scope ──

test('canManageCdpiForBranch: branch-only payitems.edit authorizes only its own branch', () => {
  const user = branchOnlyPayItemsEditUser(10);
  assert.equal(canManageCdpiForBranch(user, 10), true);
  assert.equal(canManageCdpiForBranch(user, 20), false);
});

test('canReviewCdpiCompanyWide: branch-only payitems.edit does not authorize company-wide review', () => {
  assert.equal(canReviewCdpiCompanyWide(branchOnlyPayItemsEditUser(10)), false);
});

test('canDirectCreateCdpiCompanyItem: branch-only payitems.edit does not authorize direct create', () => {
  assert.equal(canDirectCreateCdpiCompanyItem(branchOnlyPayItemsEditUser(10)), false);
});

// ── Multiple SpecificBranch authority entries are independently honored ────
// (former false negative: legacy helper required exactly one branch assignment
// and returned false for any user with more than one SpecificBranch row)

test('canManageCdpiForBranch: multiple SpecificBranch authority entries are independently honored', () => {
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
  assert.equal(canManageCdpiForBranch(user, 10), true);
  assert.equal(canManageCdpiForBranch(user, 20), true);
  assert.equal(canManageCdpiForBranch(user, 30), false);
});

// ── Flat active_permissions / branch metadata alone must not grant ─────────

test('canManageCdpiForBranch: flat active_permissions without an authority grant does not authorize', () => {
  const user = makeUser({
    active_permissions: ['payitems.edit'],
    branches: [makeBranch({ branch_id: 10 })],
    authority: makeAuthority(), // no branch or company grant
  });
  assert.equal(canManageCdpiForBranch(user, 10), false);
});

test('canReviewCdpiCompanyWide: AllCompanyBranches branch metadata + flat active_permissions without company authority does not authorize', () => {
  const user = makeUser({
    active_permissions: ['payitems.edit'],
    branches: [makeBranch({ scope: 'AllCompanyBranches', branch_id: null })],
    authority: makeAuthority(), // no company grant despite matching branch metadata
  });
  assert.equal(canReviewCdpiCompanyWide(user), false);
  assert.equal(canDirectCreateCdpiCompanyItem(user), false);
  assert.equal(canManageCdpiForBranch(user, 10), false);
});
