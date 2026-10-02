import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  BUSINESS_GROUP_DEFS,
  getRiskTier,
  groupPermissionsByDomain,
  PERM_DEPS as ROLES_PERM_DEPS,
  permsReducer,
} from '../src/pages/settings/roles/rolePermissionModel.ts';
import {
  applyToggle,
  groupPerms,
  MODULE_ORDER,
  PERM_DEPS as PEOPLE_PERM_DEPS,
} from '../src/pages/people/peoplePermissionModel.ts';
import { canManagePayrollSetups, canViewPayrollSetups } from '../src/lib/permissions.ts';
import { toUserProfile } from '../src/store/authStore.ts';
import type { BranchAccess, PermissionAuthority, UserInfoResponse, UserProfile } from '../src/store/authStore.ts';

// ── Fixture helpers (duplicated from payrollSetupAuthority.test.ts — see project
// notes: each suite keeps its own copies) ───────────────────────────────────────

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

// ── Realistic permission catalog fixture ─────────────────────────────────────
// Covers every code referenced by both pages' groups, plus the hidden
// dispatch.* codes and a fake, never-grouped 'future.unknown' code.

interface CatalogPermission {
  permission_code: string;
  permission_name: string;
  module_code: string;
}

const CATALOG: CatalogPermission[] = [
  // org-admin / company+roles+users modules
  { permission_code: 'company.view', permission_name: 'View Company', module_code: 'company' },
  { permission_code: 'company.edit', permission_name: 'Edit Company', module_code: 'company' },
  { permission_code: 'branches.view', permission_name: 'View Branches', module_code: 'company' },
  { permission_code: 'branches.create', permission_name: 'Create Branches', module_code: 'company' },
  { permission_code: 'branches.edit', permission_name: 'Edit Branches', module_code: 'company' },
  { permission_code: 'users.view', permission_name: 'View Members', module_code: 'users' },
  { permission_code: 'users.create', permission_name: 'Create Members', module_code: 'users' },
  { permission_code: 'users.edit', permission_name: 'Edit Members', module_code: 'users' },
  { permission_code: 'users.deactivate', permission_name: 'Deactivate Members', module_code: 'users' },
  { permission_code: 'roles.view', permission_name: 'View Roles', module_code: 'roles' },
  { permission_code: 'roles.create', permission_name: 'Create Roles', module_code: 'roles' },
  { permission_code: 'roles.edit', permission_name: 'Edit Roles', module_code: 'roles' },
  { permission_code: 'roles.delete', permission_name: 'Delete Roles', module_code: 'roles' },
  // payroll-ops / payroll module
  { permission_code: 'payroll.view', permission_name: 'View Payroll', module_code: 'payroll' },
  { permission_code: 'payroll.period.create', permission_name: 'Create Payroll Period', module_code: 'payroll' },
  { permission_code: 'payroll.entry', permission_name: 'Payroll Entry', module_code: 'payroll' },
  { permission_code: 'payroll.approve', permission_name: 'Approve Payroll', module_code: 'payroll' },
  { permission_code: 'review.decide', permission_name: 'Decide Review', module_code: 'payroll' },
  { permission_code: 'payroll.finalize', permission_name: 'Finalize Payroll', module_code: 'payroll' },
  // payroll-config / settings+payitems+payrates modules
  { permission_code: 'settings.view', permission_name: 'View Settings', module_code: 'settings' },
  { permission_code: 'settings.manage', permission_name: 'Manage Settings', module_code: 'settings' },
  { permission_code: 'setup.manage', permission_name: 'Manage Setup', module_code: 'settings' },
  { permission_code: 'payitems.view', permission_name: 'View Pay Items', module_code: 'payitems' },
  { permission_code: 'payitems.edit', permission_name: 'Edit Pay Items', module_code: 'payitems' },
  { permission_code: 'payrates.view', permission_name: 'View Pay Rates', module_code: 'payrates' },
  { permission_code: 'payrates.edit', permission_name: 'Edit Pay Rates', module_code: 'payrates' },
  // payroll-setup / payroll_setup module
  { permission_code: 'payroll_setup.view', permission_name: 'View Payroll Setup', module_code: 'payroll_setup' },
  { permission_code: 'payroll_setup.manage', permission_name: 'Manage Payroll Setup', module_code: 'payroll_setup' },
  { permission_code: 'payroll_setup.publish', permission_name: 'Publish Payroll Setup', module_code: 'payroll_setup' },
  { permission_code: 'payroll_setup.assign', permission_name: 'Assign Payroll Setup', module_code: 'payroll_setup' },
  // workforce / drivers module
  { permission_code: 'drivers.view', permission_name: 'View Drivers', module_code: 'drivers' },
  { permission_code: 'drivers.create', permission_name: 'Create Drivers', module_code: 'drivers' },
  { permission_code: 'drivers.edit', permission_name: 'Edit Drivers', module_code: 'drivers' },
  // reporting / reports module
  { permission_code: 'reports.view', permission_name: 'View Reports', module_code: 'reports' },
  // hidden in Roles UI, visible per-user in People UI
  { permission_code: 'dispatch.view', permission_name: 'View Dispatch', module_code: 'dispatch' },
  { permission_code: 'dispatch.edit', permission_name: 'Edit Dispatch', module_code: 'dispatch' },
  // fake code that belongs to neither page's grouping
  { permission_code: 'future.unknown', permission_name: 'Future Unknown', module_code: 'future' },
];

const PAYROLL_SETUP_CODES = ['payroll_setup.view', 'payroll_setup.manage', 'payroll_setup.publish', 'payroll_setup.assign'];

// ── A. Roles: payroll-setup group exists with correct label/note/codes ──────

test('Roles groupPermissionsByDomain: payroll-setup group has correct id/label/note/codes', () => {
  const grouped = groupPermissionsByDomain(CATALOG);
  const entry = grouped.find(([g]) => g.id === 'payroll-setup');
  assert.ok(entry, 'payroll-setup group must be present');
  const [group, perms] = entry!;
  assert.equal(group.label, 'Payroll Setup Policy');
  assert.equal(group.note, 'Effective only on All-Branches role assignments.');
  assert.deepEqual(perms.map(p => p.permission_code), PAYROLL_SETUP_CODES);
});

// ── B. Roles: group placement and setup.manage stays in payroll-config ──────

test('Roles groupPermissionsByDomain: payroll-setup immediately follows payroll-config; setup.manage stays put', () => {
  const grouped = groupPermissionsByDomain(CATALOG);
  const ids = grouped.map(([g]) => g.id);
  const configIdx = ids.indexOf('payroll-config');
  const setupIdx = ids.indexOf('payroll-setup');
  assert.notEqual(configIdx, -1);
  assert.equal(setupIdx, configIdx + 1);

  const [, configPerms] = grouped.find(([g]) => g.id === 'payroll-config')!;
  assert.ok(configPerms.some(p => p.permission_code === 'setup.manage'));

  const [, setupPerms] = grouped.find(([g]) => g.id === 'payroll-setup')!;
  assert.ok(!setupPerms.some(p => p.permission_code === 'setup.manage'));

  for (const [g, perms] of grouped) {
    if (g.id === 'payroll-setup') continue;
    for (const p of perms) {
      assert.ok(!p.permission_code.startsWith('payroll_setup.'), `${p.permission_code} leaked into group ${g.id}`);
    }
  }
});

// ── C. People: payroll_setup module exists with correct label/note/order ────

test('People groupPerms: payroll_setup module has correct label/note/codes, placed right after payroll', () => {
  const groups = groupPerms(CATALOG);
  const modules = groups.map(g => g.module);
  const payrollIdx = modules.indexOf('payroll');
  const setupIdx = modules.indexOf('payroll_setup');
  assert.notEqual(payrollIdx, -1);
  assert.equal(setupIdx, payrollIdx + 1);

  const setupGroup = groups.find(g => g.module === 'payroll_setup')!;
  assert.equal(setupGroup.label, 'Payroll Setup Policy');
  assert.equal(setupGroup.note, 'Effective only for members with an All-Branches role assignment.');
  assert.deepEqual(setupGroup.perms.map(p => p.permission_code), PAYROLL_SETUP_CODES);
});

// ── D/E/F. Enabling a payroll_setup child auto-enables payroll_setup.view ────

test('Roles permsReducer: TOGGLE on for manage/publish/assign each auto-enables payroll_setup.view', () => {
  for (const code of ['payroll_setup.manage', 'payroll_setup.publish', 'payroll_setup.assign']) {
    const start = permsReducer(
      { codes: new Set(), savedCodes: new Set(), loading: false, error: '', dirty: false },
      { type: 'FETCH_OK', codes: [] },
    );
    const next = permsReducer(start, { type: 'TOGGLE', code });
    assert.ok(next.codes.has(code), code);
    assert.ok(next.codes.has('payroll_setup.view'), `${code} must auto-enable payroll_setup.view`);
  }
});

test('People applyToggle: enabling manage/publish/assign each auto-enables payroll_setup.view', () => {
  for (const code of ['payroll_setup.manage', 'payroll_setup.publish', 'payroll_setup.assign']) {
    const next = applyToggle(new Set(), code, true);
    assert.ok(next.has(code), code);
    assert.ok(next.has('payroll_setup.view'), `${code} must auto-enable payroll_setup.view`);
  }
});

test('Roles permsReducer: SET_MANY enabling the whole payroll-setup group includes payroll_setup.view', () => {
  const start = permsReducer(
    { codes: new Set(), savedCodes: new Set(), loading: false, error: '', dirty: false },
    { type: 'FETCH_OK', codes: [] },
  );
  const next = permsReducer(start, { type: 'SET_MANY', codes: PAYROLL_SETUP_CODES, enable: true });
  for (const code of PAYROLL_SETUP_CODES) assert.ok(next.codes.has(code), code);
});

// ── G. Removing payroll_setup.view cascades to remove its dependents only ───

test('Roles permsReducer: TOGGLE off payroll_setup.view removes manage/publish/assign, keeps unrelated codes', () => {
  const initial = {
    codes: new Set(['payroll_setup.view', 'payroll_setup.manage', 'payroll_setup.publish', 'payroll_setup.assign', 'payroll.view', 'future.unknown']),
    savedCodes: new Set<string>(),
    loading: false,
    error: '',
    dirty: false,
  };
  const next = permsReducer(initial, { type: 'TOGGLE', code: 'payroll_setup.view' });
  assert.ok(!next.codes.has('payroll_setup.view'));
  assert.ok(!next.codes.has('payroll_setup.manage'));
  assert.ok(!next.codes.has('payroll_setup.publish'));
  assert.ok(!next.codes.has('payroll_setup.assign'));
  assert.ok(next.codes.has('payroll.view'));
  assert.ok(next.codes.has('future.unknown'));
});

test('People applyToggle: removing payroll_setup.view removes manage/publish/assign, keeps unrelated codes', () => {
  const initial = new Set(['payroll_setup.view', 'payroll_setup.manage', 'payroll_setup.publish', 'payroll_setup.assign', 'payroll.view', 'future.unknown']);
  const next = applyToggle(initial, 'payroll_setup.view', false);
  assert.ok(!next.has('payroll_setup.view'));
  assert.ok(!next.has('payroll_setup.manage'));
  assert.ok(!next.has('payroll_setup.publish'));
  assert.ok(!next.has('payroll_setup.assign'));
  assert.ok(next.has('payroll.view'));
  assert.ok(next.has('future.unknown'));
});

// ── H. Unknown/hidden codes survive toggles and never render ────────────────

test('Roles permsReducer: FETCH_OK then TOGGLE manage keeps hidden/unknown codes intact (save sends Array.from(codes))', () => {
  const start = permsReducer(
    { codes: new Set(), savedCodes: new Set(), loading: false, error: '', dirty: false },
    { type: 'FETCH_OK', codes: ['dispatch.view', 'future.unknown', 'payroll_setup.view'] },
  );
  const next = permsReducer(start, { type: 'TOGGLE', code: 'payroll_setup.manage' });
  const saved = Array.from(next.codes);
  assert.ok(saved.includes('dispatch.view'));
  assert.ok(saved.includes('future.unknown'));
  assert.ok(saved.includes('payroll_setup.manage'));
  assert.ok(saved.includes('payroll_setup.view'));
});

test('People applyToggle: enabling payroll_setup.manage keeps unknown codes intact', () => {
  const initial = new Set(['dispatch.view', 'future.unknown', 'payroll_setup.view']);
  const next = applyToggle(initial, 'payroll_setup.manage', true);
  assert.ok(next.has('dispatch.view'));
  assert.ok(next.has('future.unknown'));
  assert.ok(next.has('payroll_setup.manage'));
});

test('Roles groupPermissionsByDomain: dispatch.view/dispatch.edit and future.unknown never render in any group', () => {
  const grouped = groupPermissionsByDomain(CATALOG);
  for (const [, perms] of grouped) {
    for (const p of perms) {
      assert.notEqual(p.permission_code, 'dispatch.view');
      assert.notEqual(p.permission_code, 'dispatch.edit');
      assert.notEqual(p.permission_code, 'future.unknown');
    }
  }
});

// ── I. Existing groups/order/labels/PERM_DEPS unchanged except the U2 additions ──

const PRE_U2_ROLES_GROUPS: Record<string, { label: string; codes: string[] }> = {
  'org-admin': {
    label: 'Organization Administration',
    codes: ['company.view', 'company.edit', 'branches.view', 'branches.create', 'branches.edit', 'users.view', 'users.create', 'users.edit', 'users.deactivate', 'roles.view', 'roles.create', 'roles.edit', 'roles.delete'],
  },
  'payroll-ops': {
    label: 'Payroll Operations',
    codes: ['payroll.view', 'payroll.period.create', 'payroll.entry', 'payroll.approve', 'review.decide', 'payroll.finalize'],
  },
  'payroll-config': {
    label: 'Payroll Configuration',
    codes: ['settings.view', 'settings.manage', 'setup.manage', 'payitems.view', 'payitems.edit', 'payrates.view', 'payrates.edit'],
  },
  'workforce': {
    label: 'Workforce',
    codes: ['drivers.view', 'drivers.create', 'drivers.edit'],
  },
  'reporting': {
    label: 'Reporting',
    codes: ['reports.view'],
  },
};

test('Roles BUSINESS_GROUP_DEFS: pre-existing groups unchanged, full id order correct', () => {
  assert.deepEqual(BUSINESS_GROUP_DEFS.map(g => g.id), ['org-admin', 'payroll-ops', 'payroll-config', 'payroll-setup', 'workforce', 'reporting']);
  for (const [id, expected] of Object.entries(PRE_U2_ROLES_GROUPS)) {
    const g = BUSINESS_GROUP_DEFS.find(x => x.id === id);
    assert.ok(g, `group ${id} must still exist`);
    assert.equal(g!.label, expected.label);
    assert.deepEqual([...g!.codes], expected.codes);
  }
});

test('People MODULE_ORDER: payroll_setup inserted immediately after payroll, rest unchanged', () => {
  assert.deepEqual(MODULE_ORDER, ['company', 'roles', 'users', 'payroll', 'payroll_setup', 'payitems', 'payrates', 'drivers', 'dispatch', 'reports', 'settings']);
});

const PRE_U2_ROLES_PERM_DEPS: Record<string, string> = {
  'company.edit':          'company.view',
  'branches.create':       'branches.view',
  'branches.edit':         'branches.view',
  'roles.create':          'roles.view',
  'roles.edit':            'roles.view',
  'roles.delete':          'roles.view',
  'users.create':          'users.view',
  'users.edit':            'users.view',
  'users.deactivate':      'users.view',
  'payroll.edit':          'payroll.view',
  'payroll.approve':       'payroll.view',
  'payroll.finalize':      'payroll.view',
  'payroll.entry':         'payroll.view',
  'payroll.period.create': 'payroll.view',
  'review.decide':         'payroll.view',
  'payitems.edit':         'payitems.view',
  'payrates.edit':         'payrates.view',
  'drivers.create':        'drivers.view',
  'drivers.edit':          'drivers.view',
  'dispatch.edit':         'dispatch.view',
  'settings.manage':       'settings.view',
};

const PRE_U2_PEOPLE_PERM_DEPS: Record<string, string> = {
  'company.edit':     'company.view',
  'branches.create':  'branches.view',
  'branches.edit':    'branches.view',
  'roles.create':     'roles.view',
  'roles.edit':       'roles.view',
  'roles.delete':     'roles.view',
  'users.create':     'users.view',
  'users.edit':       'users.view',
  'users.deactivate': 'users.view',
  'payroll.edit':     'payroll.view',
  'payroll.approve':  'payroll.view',
  'payroll.finalize': 'payroll.view',
  'payitems.edit':    'payitems.view',
  'payrates.edit':    'payrates.view',
  'drivers.create':   'drivers.view',
  'drivers.edit':     'drivers.view',
  'dispatch.edit':    'dispatch.view',
  'settings.manage':  'settings.view',
};

const NEW_PAYROLL_SETUP_DEPS: Record<string, string> = {
  'payroll_setup.manage':  'payroll_setup.view',
  'payroll_setup.publish': 'payroll_setup.view',
  'payroll_setup.assign':  'payroll_setup.view',
};

test('Roles PERM_DEPS: pre-existing entries unchanged, only the three new payroll_setup deps added', () => {
  assert.deepEqual(ROLES_PERM_DEPS, { ...PRE_U2_ROLES_PERM_DEPS, ...NEW_PAYROLL_SETUP_DEPS });
});

test('People PERM_DEPS: pre-existing entries unchanged, only the three new payroll_setup deps added', () => {
  assert.deepEqual(PEOPLE_PERM_DEPS, { ...PRE_U2_PEOPLE_PERM_DEPS, ...NEW_PAYROLL_SETUP_DEPS });
});

test('Roles getRiskTier: new payroll_setup tiers and existing examples unchanged', () => {
  assert.equal(getRiskTier('payroll_setup.publish'), 'critical');
  assert.equal(getRiskTier('payroll_setup.assign'), 'approval');
  assert.equal(getRiskTier('payroll_setup.view'), 'readonly');
  assert.equal(getRiskTier('payroll_setup.manage'), 'write');
  assert.equal(getRiskTier('payroll.finalize'), 'critical');
  assert.equal(getRiskTier('review.decide'), 'approval');
  assert.equal(getRiskTier('roles.view'), 'readonly');
  assert.equal(getRiskTier('payroll.entry'), 'write');
});

// ── J. UI dependency is not authority ────────────────────────────────────────

test('UI dependency is not authority: company-wide payroll_setup.manage alone manages but does not view', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: ['payroll_setup.manage'] }),
  });
  assert.equal(canManagePayrollSetups(user), true);
  assert.equal(canViewPayrollSetups(user), false);
});

test('UI dependency is not authority: branch-scoped grant of all four payroll_setup codes never grants view', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 10, scope: 'SpecificBranch' })],
    authority: makeAuthority({
      branch_permissions: [{ branch_id: 10, permissions: [...PAYROLL_SETUP_CODES] }],
    }),
  });
  assert.equal(canViewPayrollSetups(user), false);
});

test('UI dependency is not authority: positive control — company-wide grant of all four codes allows view', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: [...PAYROLL_SETUP_CODES] }),
  });
  assert.equal(canViewPayrollSetups(user), true);
});

test('PERM_DEPS integrity: payroll_setup dependency edges never cross into non-payroll_setup codes', () => {
  for (const permDeps of [ROLES_PERM_DEPS, PEOPLE_PERM_DEPS]) {
    for (const [child, parent] of Object.entries(permDeps)) {
      if (child.startsWith('payroll_setup.')) {
        assert.equal(parent, 'payroll_setup.view', `${child} must depend on exactly payroll_setup.view`);
      } else {
        assert.ok(!parent.startsWith('payroll_setup.'), `${child} must not depend on a payroll_setup code`);
      }
    }
  }
});
