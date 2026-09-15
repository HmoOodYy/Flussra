import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  canEditPayRates,
  canManageCdpiForBranch,
  canViewCurrentPayroll,
  canViewDailyPayItems,
  canViewFinalizedLibrary,
  canViewFinalLines,
  canViewLedger,
  canViewPayRates,
  canViewReview,
  canViewTransfers,
  hasPayItemsEdit,
} from '../src/lib/permissions.ts';
import { toUserProfile } from '../src/store/authStore.ts';
import type {
  BranchAccess,
  PermissionAuthority,
  UserInfoResponse,
  UserProfile,
} from '../src/store/authStore.ts';

const DISCOVERY_PERMISSIONS = [
  'payroll.view',
  'ledger.view',
  'review.decide',
  'payrates.view',
  'drivers.view',
  'payitems.edit',
] as const;

const VISIBILITY_CHECKS = [
  ['Current Payroll', canViewCurrentPayroll],
  ['Finalized Library', canViewFinalizedLibrary],
  ['legacy FinalLines', canViewFinalLines],
  ['Ledger', canViewLedger],
  ['Review', canViewReview],
  ['Pay Rates', canViewPayRates],
  ['Transfers', canViewTransfers],
  ['Pay Items grant', hasPayItemsEdit],
  ['Daily Pay Items', canViewDailyPayItems],
] as const satisfies readonly (readonly [string, (user: UserProfile) => boolean])[];

const DRIVER_EXCLUDED_CHECKS = [
  ['Current Payroll', canViewCurrentPayroll],
  ['Finalized Library', canViewFinalizedLibrary],
  ['legacy FinalLines', canViewFinalLines],
  ['Ledger', canViewLedger],
  ['Review', canViewReview],
  ['Pay Rates', canViewPayRates],
  ['Transfers', canViewTransfers],
] as const satisfies readonly (readonly [string, (user: UserProfile) => boolean])[];

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

function makeUser(overrides: Partial<UserInfoResponse> = {}): UserProfile {
  return toUserProfile({
    user_id: 1,
    username: 'operator',
    display_name: 'Payroll Operator',
    company_id: 1,
    company_name: 'Demo Logistics',
    branches: [makeBranch()],
    active_permissions: [],
    authority: makeAuthority(),
    ...overrides,
  });
}

test('page visibility: company authority grants broad discovery without active_permissions', async (t) => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: DISCOVERY_PERMISSIONS }),
  });

  for (const [label, check] of VISIBILITY_CHECKS) {
    await t.test(label, () => {
      assert.equal(check(user), true);
    });
  }
});

test('page visibility: one branch authority grant enables broad discovery', async (t) => {
  const user = makeUser({
    authority: makeAuthority({
      branch_permissions: [{ branch_id: 10, permissions: DISCOVERY_PERMISSIONS }],
    }),
  });

  for (const [label, check] of VISIBILITY_CHECKS) {
    await t.test(label, () => {
      assert.equal(check(user), true);
    });
  }
});

test('page visibility: flat active_permissions without authority grants nothing', async (t) => {
  const user = makeUser({
    active_permissions: [...DISCOVERY_PERMISSIONS],
    authority: makeAuthority(),
  });

  for (const [label, check] of VISIBILITY_CHECKS) {
    await t.test(label, () => {
      assert.equal(check(user), false);
    });
  }
});

test('page visibility: DRIVER assignments remain excluded from operational pages', async (t) => {
  const user = makeUser({
    branches: [makeBranch({ role_code: 'DRIVER', role_name: 'Driver' })],
    authority: makeAuthority({ company_permissions: DISCOVERY_PERMISSIONS }),
  });

  for (const [label, check] of DRIVER_EXCLUDED_CHECKS) {
    await t.test(label, () => {
      assert.equal(check(user), false);
    });
  }
});

test('page visibility: OwnDriverDataOnly assignments remain excluded from operational pages', async (t) => {
  const user = makeUser({
    branches: [makeBranch({ scope: 'OwnDriverDataOnly', role_code: 'DRIVER', role_name: 'Driver' })],
    authority: makeAuthority({
      branch_permissions: [{ branch_id: 10, permissions: DISCOVERY_PERMISSIONS }],
    }),
  });

  for (const [label, check] of DRIVER_EXCLUDED_CHECKS) {
    await t.test(label, () => {
      assert.equal(check(user), false);
    });
  }
});

test('page visibility: branch discovery does not widen concrete-branch actions', () => {
  const user = makeUser({
    authority: makeAuthority({
      branch_permissions: [{ branch_id: 10, permissions: ['payrates.edit', 'payitems.edit'] }],
    }),
  });

  assert.equal(canViewPayRates(user), true);
  assert.equal(canEditPayRates(user, 20), false);
  assert.equal(canViewDailyPayItems(user), true);
  assert.equal(canManageCdpiForBranch(user, 20), false);
});
