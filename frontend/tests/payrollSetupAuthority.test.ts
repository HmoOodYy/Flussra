import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  canAccessCompanyBranches,
  canAssignPayrollSetups,
  canCreateBranches,
  canManagePayrollSetups,
  canManageSettingsAdmin,
  canPublishPayrollSetups,
  canViewAnyBranchPayrollSchedule,
  canViewBranchPayrollSchedule,
  canViewPayrollSetups,
} from '../src/lib/permissions.ts';
import { friendlyPermLabel } from '../src/lib/permissionLabels.ts';
import { toUserProfile } from '../src/store/authStore.ts';
import type {
  BranchAccess,
  PermissionAuthority,
  UserInfoResponse,
  UserProfile,
} from '../src/store/authStore.ts';

const ALL_PAYROLL_SETUP_CODES = [
  'payroll_setup.view',
  'payroll_setup.manage',
  'payroll_setup.publish',
  'payroll_setup.assign',
] as const;

const PAYROLL_SETUP_HELPERS = [
  ['payroll_setup.view', canViewPayrollSetups],
  ['payroll_setup.manage', canManagePayrollSetups],
  ['payroll_setup.publish', canPublishPayrollSetups],
  ['payroll_setup.assign', canAssignPayrollSetups],
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
    self_assignments: [],
    active_permissions: [],
    authority: makeAuthority(),
    ...overrides,
  });
}

function assertAllPayrollSetupHelpers(user: UserProfile, expected: boolean, message?: string) {
  for (const [code, helper] of PAYROLL_SETUP_HELPERS) {
    assert.equal(helper(user), expected, message ?? `${code} expected ${expected}`);
  }
}

// ── A. company-wide grant ───────────────────────────────────────────────────

test('canViewPayrollSetups: company-wide payroll_setup.view grants access', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: ['payroll_setup.view'] }),
  });
  assert.equal(canViewPayrollSetups(user), true);
});

// ── B. each helper is granted by exactly its own code ───────────────────────

test('payroll_setup helpers: each company-wide code grants only its own helper', async (t) => {
  for (const [grantedCode] of PAYROLL_SETUP_HELPERS) {
    await t.test(grantedCode, () => {
      const user = makeUser({
        branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
        authority: makeAuthority({ company_permissions: [grantedCode] }),
      });
      for (const [code, helper] of PAYROLL_SETUP_HELPERS) {
        assert.equal(helper(user), code === grantedCode, `${code} given only ${grantedCode}`);
      }
    });
  }
});

// ── C. branch-scoped grant denies; company-wide is the positive control ─────

test('payroll_setup helpers: branch-scoped grant of all four codes denies all helpers', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 10, scope: 'SpecificBranch' })],
    authority: makeAuthority({
      branch_permissions: [{ branch_id: 10, permissions: [...ALL_PAYROLL_SETUP_CODES] }],
    }),
  });
  assertAllPayrollSetupHelpers(user, false);
});

test('payroll_setup helpers: positive control — company-wide grant of all four codes allows all helpers', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: [...ALL_PAYROLL_SETUP_CODES] }),
  });
  assertAllPayrollSetupHelpers(user, true);
});

// ── D. DRIVER denies despite company-wide grant; schedule helpers too ───────

test('payroll_setup helpers: DRIVER role denies all four helpers despite company-wide grant', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 10, scope: 'SpecificBranch', role_code: 'DRIVER', role_name: 'Driver' })],
    authority: makeAuthority({
      company_permissions: [...ALL_PAYROLL_SETUP_CODES, 'payroll.view'],
    }),
  });
  assertAllPayrollSetupHelpers(user, false);
  assert.equal(canViewBranchPayrollSchedule(user, 10), false);
  assert.equal(canViewAnyBranchPayrollSchedule(user), false);
});

// ── E. Self denies generic setup access despite company-wide grant ──────────

test('payroll_setup helpers: Self assignment denies all four helpers despite company-wide grant', () => {
  const user = makeUser({
    self_assignments: [{ role_code: 'DRIVER', role_name: 'Driver', scope: 'Self' }],
    authority: makeAuthority({ company_permissions: [...ALL_PAYROLL_SETUP_CODES] }),
  });
  assertAllPayrollSetupHelpers(user, false);
});

// ── F. mixed Driver + company-admin denies; identical user without driver row is the positive control ──

test('payroll_setup helpers: mixed Driver + company-admin denies all helpers, schedule helpers, and canCreateBranches', () => {
  const branches: BranchAccess[] = [
    makeBranch({ branch_id: null, scope: 'AllCompanyBranches', role_code: 'COMPANY_ADMIN', role_name: 'Company Admin' }),
    makeBranch({ branch_id: 10, scope: 'SpecificBranch', role_code: 'DRIVER', role_name: 'Driver' }),
  ];
  const authority = makeAuthority({
    company_permissions: [...ALL_PAYROLL_SETUP_CODES, 'payroll.view', 'branches.create'],
  });
  const mixedUser = makeUser({ branches, authority });

  assertAllPayrollSetupHelpers(mixedUser, false);
  assert.equal(canViewBranchPayrollSchedule(mixedUser, 10), false);
  assert.equal(canViewAnyBranchPayrollSchedule(mixedUser), false);
  assert.equal(canCreateBranches(mixedUser), false);

  // Positive control: identical user minus the driver row.
  const adminOnlyUser = makeUser({
    branches: [branches[0]],
    authority,
  });
  assertAllPayrollSetupHelpers(adminOnlyUser, true);
  assert.equal(canViewBranchPayrollSchedule(adminOnlyUser, 10), true);
  assert.equal(canViewAnyBranchPayrollSchedule(adminOnlyUser), true);
  assert.equal(canCreateBranches(adminOnlyUser), true);
});

// ── G. canViewBranchPayrollSchedule: branch-scoped payroll.view is branch-exact ──

test('canViewBranchPayrollSchedule: branch-scoped payroll.view authorizes only that branch', () => {
  const user = makeUser({
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['payroll.view'] }] }),
  });
  assert.equal(canViewBranchPayrollSchedule(user, 10), true);
  assert.equal(canViewBranchPayrollSchedule(user, 20), false);
});

// ── H. company-wide payroll.view authorizes any branch and discovery ────────

test('canViewBranchPayrollSchedule / canViewAnyBranchPayrollSchedule: company-wide payroll.view authorizes any branch', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: ['payroll.view'] }),
  });
  assert.equal(canViewBranchPayrollSchedule(user, 10), true);
  assert.equal(canViewBranchPayrollSchedule(user, 20), true);
  assert.equal(canViewAnyBranchPayrollSchedule(user), true);
});

// ── I. payroll.entry (and other PAYROLL_READ codes) alone never grant schedule access ──

test('canViewBranchPayrollSchedule: payroll.entry alone (company-wide) does not grant access', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: ['payroll.entry'] }),
  });
  assert.equal(canViewBranchPayrollSchedule(user, 10), false);
  assert.equal(canViewAnyBranchPayrollSchedule(user), false);

  // Positive control: adding payroll.view flips it true.
  const withView = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: ['payroll.entry', 'payroll.view'] }),
  });
  assert.equal(canViewBranchPayrollSchedule(withView, 10), true);
  assert.equal(canViewAnyBranchPayrollSchedule(withView), true);
});

test('canViewBranchPayrollSchedule: payroll.entry alone (branch-scoped) does not grant access', () => {
  const user = makeUser({
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['payroll.entry'] }] }),
  });
  assert.equal(canViewBranchPayrollSchedule(user, 10), false);
  assert.equal(canViewAnyBranchPayrollSchedule(user), false);

  // Positive control: adding payroll.view flips it true.
  const withView = makeUser({
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['payroll.entry', 'payroll.view'] }] }),
  });
  assert.equal(canViewBranchPayrollSchedule(withView, 10), true);
  assert.equal(canViewAnyBranchPayrollSchedule(withView), true);
});

test('canViewBranchPayrollSchedule: other PAYROLL_READ codes alone do not grant access', () => {
  for (const code of ['payroll.edit', 'payroll.finalize', 'payroll.period.create']) {
    const user = makeUser({
      branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
      authority: makeAuthority({ company_permissions: [code] }),
    });
    assert.equal(canViewBranchPayrollSchedule(user, 10), false, code);
    assert.equal(canViewAnyBranchPayrollSchedule(user), false, code);
  }
});

// ── J. canViewAnyBranchPayrollSchedule: at least one branch with payroll.view ──

test('canViewAnyBranchPayrollSchedule: true when exactly one of several branches has payroll.view', () => {
  const user = makeUser({
    authority: makeAuthority({
      branch_permissions: [
        { branch_id: 10, permissions: ['payroll.entry'] },
        { branch_id: 20, permissions: ['payroll.view'] },
        { branch_id: 30, permissions: [] },
      ],
    }),
  });
  assert.equal(canViewAnyBranchPayrollSchedule(user), true);
});

test('canViewAnyBranchPayrollSchedule: false when no branch has payroll.view', () => {
  const user = makeUser({
    authority: makeAuthority({
      branch_permissions: [
        { branch_id: 10, permissions: ['payroll.entry'] },
        { branch_id: 20, permissions: ['payroll.finalize'] },
      ],
    }),
  });
  assert.equal(canViewAnyBranchPayrollSchedule(user), false);
});

// ── K. flat active_permissions alone grants nothing ──────────────────────────

test('flat active_permissions without authority grants none of the new helpers', () => {
  const user = makeUser({
    active_permissions: [...ALL_PAYROLL_SETUP_CODES, 'payroll.view', 'branches.create'],
    authority: makeAuthority(),
  });
  assertAllPayrollSetupHelpers(user, false);
  assert.equal(canViewBranchPayrollSchedule(user, 10), false);
  assert.equal(canViewAnyBranchPayrollSchedule(user), false);
  assert.equal(canCreateBranches(user), false);
  assert.equal(canAccessCompanyBranches(user), false);
});

// ── L. canCreateBranches / canAccessCompanyBranches ──────────────────────────

test('canCreateBranches: company-wide branches.create grants access', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: ['branches.create'] }),
  });
  assert.equal(canCreateBranches(user), true);
});

test('canCreateBranches: branch-scoped branches.create does not grant access', () => {
  const user = makeUser({
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['branches.create'] }] }),
  });
  assert.equal(canCreateBranches(user), false);
});

test('canCreateBranches: DRIVER denies despite company-wide branches.create', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 10, scope: 'SpecificBranch', role_code: 'DRIVER', role_name: 'Driver' })],
    authority: makeAuthority({ company_permissions: ['branches.create'] }),
  });
  assert.equal(canCreateBranches(user), false);
});

test('canAccessCompanyBranches: true for company-wide setup.manage only', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: ['setup.manage'] }),
  });
  assert.equal(canManageSettingsAdmin(user), true);
  assert.equal(canCreateBranches(user), false);
  assert.equal(canAccessCompanyBranches(user), true);
});

test('canAccessCompanyBranches: true for company-wide branches.create only', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: ['branches.create'] }),
  });
  assert.equal(canManageSettingsAdmin(user), false);
  assert.equal(canCreateBranches(user), true);
  assert.equal(canAccessCompanyBranches(user), true);
});

test('canAccessCompanyBranches: false for branch-scoped setup.manage + branches.create', () => {
  const user = makeUser({
    authority: makeAuthority({
      branch_permissions: [{ branch_id: 10, permissions: ['setup.manage', 'branches.create'] }],
    }),
  });
  assert.equal(canAccessCompanyBranches(user), false);
});

test('canAccessCompanyBranches: false when neither permission is held', () => {
  const user = makeUser();
  assert.equal(canAccessCompanyBranches(user), false);
});

// ── M. friendlyPermLabel ──────────────────────────────────────────────────────

test('friendlyPermLabel: payroll_setup codes', () => {
  assert.equal(friendlyPermLabel('payroll_setup.view'), 'View Payroll Setup');
  assert.equal(friendlyPermLabel('payroll_setup.manage'), 'Manage Payroll Setup');
  assert.equal(friendlyPermLabel('payroll_setup.publish'), 'Publish Payroll Setup');
  assert.equal(friendlyPermLabel('payroll_setup.assign'), 'Assign Payroll Setup');
});

test('friendlyPermLabel: existing labels are unchanged (regression)', () => {
  assert.equal(friendlyPermLabel('payroll.view'), 'View Payroll');
  assert.equal(friendlyPermLabel('users.edit'), 'Edit Members');
});
