import { makeAuthority, makeBranchAccess as makeBranch } from './authorityTestFixtures.ts';
import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  BUSINESS_GROUP_DEFS,
  getRiskTier,
  groupPermissionsByDomain,
  HIDDEN_PERM_CODES,
  isUserVisibleParentMissing,
  PERM_DEPS as ROLES_PERM_DEPS,
  permsReducer,
} from '../src/pages/settings/roles/rolePermissionModel.ts';
import type { PermsState } from '../src/pages/settings/roles/rolePermissionModel.ts';
import {
  applyToggle,
  groupPerms,
  isHiddenPermission,
  MODULE_LABELS,
  MODULE_NOTES,
  PERM_DEPS as PEOPLE_PERM_DEPS,
} from '../src/pages/people/peoplePermissionModel.ts';
import {
  canViewCurrentPayroll,
  canViewReview,
} from '../src/lib/permissions.ts';
import { toUserProfile } from '../src/store/authStore.ts';
import type { UserInfoResponse, UserProfile } from '../src/store/authStore.ts';

// Enforced by backend routes but previously not grantable from either editor.
const WORKFORCE_CODES = ['employees.view', 'employees.manage'];
const LEDGER_CODES = ['ledger.view', 'ledger.audit.view'];

// Catalogued and shown before, but checked by no backend route.
const UNENFORCED_CODES = [
  'company.view', 'company.edit',
  'branches.view', 'branches.edit',
  'dispatch.view', 'dispatch.edit',
  'drivers.create',
  'payroll.approve', 'payroll.edit',
  'settings.view',
];

const MODULE_BY_PREFIX: Record<string, string> = {
  company: 'company', branches: 'company', users: 'users', roles: 'roles', payroll: 'payroll',
  review: 'review', settings: 'settings', setup: 'settings', payitems: 'payitems', payrates: 'payrates',
  drivers: 'drivers', dispatch: 'dispatch', reports: 'reports', employees: 'employees', ledger: 'ledger',
  payroll_setup: 'payroll_setup',
};

function perm(code: string) {
  const prefix = code.slice(0, code.indexOf('.'));
  return { permission_code: code, permission_name: code, module_code: MODULE_BY_PREFIX[prefix] ?? prefix };
}

const ALL_CODES = [
  ...UNENFORCED_CODES,
  ...WORKFORCE_CODES,
  ...LEDGER_CODES,
  'branches.create', 'users.view', 'users.edit', 'roles.view', 'roles.edit',
  'payroll.view', 'payroll.entry', 'payroll.finalize', 'payroll.period.create', 'review.decide',
  'settings.manage', 'payitems.view', 'payrates.view',
  'drivers.view', 'drivers.edit', 'reports.view',
];
const CATALOG = ALL_CODES.map(perm);

function loaded(codes: string[]): PermsState {
  return permsReducer(
    { codes: new Set(), savedCodes: new Set(), loading: false, error: '', dirty: false },
    { type: 'FETCH_OK', codes },
  );
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

// ── Employees and ledger codes can be granted ────────────────────────────────

test('Roles editor groups employees codes under Workforce and ledger codes under Reporting', () => {
  const grouped = groupPermissionsByDomain(CATALOG);
  const codesOf = (id: string) => grouped.find(([g]) => g.id === id)![1].map(p => p.permission_code);
  for (const code of WORKFORCE_CODES) assert.ok(codesOf('workforce').includes(code), code);
  for (const code of LEDGER_CODES) assert.ok(codesOf('reporting').includes(code), code);
});

test('People picker offers employees and ledger modules with plain labels', () => {
  const groups = groupPerms(CATALOG);
  const employees = groups.find(g => g.module === 'employees')!;
  const ledger = groups.find(g => g.module === 'ledger')!;
  assert.equal(employees.label, 'Employees');
  assert.equal(ledger.label, 'Finalized Payroll');
  assert.deepEqual(employees.perms.map(p => p.permission_code).sort(), [...WORKFORCE_CODES].sort());
  assert.deepEqual(ledger.perms.map(p => p.permission_code).sort(), [...LEDGER_CODES].sort());
});

test('employees and ledger permissions carry no company-only note', () => {
  for (const def of BUSINESS_GROUP_DEFS) {
    if (def.id === 'workforce' || def.id === 'reporting') assert.equal(def.note, undefined, def.id);
  }
  assert.equal(MODULE_NOTES.employees, undefined);
  assert.equal(MODULE_NOTES.ledger, undefined);
  assert.ok('employees' in MODULE_LABELS && 'ledger' in MODULE_LABELS);
});

test('manage and audit permissions depend on their view permission in both editors', () => {
  for (const deps of [ROLES_PERM_DEPS, PEOPLE_PERM_DEPS]) {
    assert.equal(deps['employees.manage'], 'employees.view');
    assert.equal(deps['ledger.audit.view'], 'ledger.view');
  }
});

test('Roles permsReducer: enabling employees.manage or ledger.audit.view also enables the view permission', () => {
  const m = permsReducer(loaded([]), { type: 'TOGGLE', code: 'employees.manage' });
  assert.ok(m.codes.has('employees.manage') && m.codes.has('employees.view'));
  const a = permsReducer(loaded([]), { type: 'TOGGLE', code: 'ledger.audit.view' });
  assert.ok(a.codes.has('ledger.audit.view') && a.codes.has('ledger.view'));
});

test('Roles permsReducer: disabling employees.view or ledger.view removes the dependent permission', () => {
  const m = permsReducer(loaded(['employees.view', 'employees.manage']), { type: 'TOGGLE', code: 'employees.view' });
  assert.equal(m.codes.size, 0);
  const a = permsReducer(loaded(['ledger.view', 'ledger.audit.view']), { type: 'TOGGLE', code: 'ledger.view' });
  assert.equal(a.codes.size, 0);
});

test('People applyToggle: employees and ledger dependencies cascade both ways', () => {
  const on = applyToggle(new Set(), 'employees.manage', true);
  assert.deepEqual([...on].sort(), ['employees.manage', 'employees.view']);
  assert.equal(applyToggle(on, 'employees.view', false).size, 0);
  const audit = applyToggle(new Set(), 'ledger.audit.view', true);
  assert.deepEqual([...audit].sort(), ['ledger.audit.view', 'ledger.view']);
  assert.equal(applyToggle(audit, 'ledger.view', false).size, 0);
});

test('Roles getRiskTier: employees.view and ledger codes are read-only, employees.manage is write', () => {
  assert.equal(getRiskTier('employees.view'), 'readonly');
  assert.equal(getRiskTier('ledger.view'), 'readonly');
  assert.equal(getRiskTier('ledger.audit.view'), 'readonly');
  assert.equal(getRiskTier('employees.manage'), 'write');
});

// ── Codes that grant nothing are hidden but preserved ────────────────────────

test('hidden codes list is exactly the ten permissions no backend route checks', () => {
  assert.deepEqual([...HIDDEN_PERM_CODES].sort(), [...UNENFORCED_CODES].sort());
});

test('Roles editor never shows a permission that grants nothing', () => {
  const shown = new Set(groupPermissionsByDomain(CATALOG).flatMap(([, perms]) => perms.map(p => p.permission_code)));
  for (const code of UNENFORCED_CODES) assert.ok(!shown.has(code), code);
  for (const def of BUSINESS_GROUP_DEFS) {
    for (const code of def.codes) assert.ok(!HIDDEN_PERM_CODES.has(code), `${code} listed in group ${def.id}`);
  }
});

test('People picker never shows a permission that grants nothing', () => {
  const shown = new Set(groupPerms(CATALOG).flatMap(g => g.perms.map(p => p.permission_code)));
  for (const code of UNENFORCED_CODES) {
    assert.ok(!shown.has(code), code);
    assert.equal(isHiddenPermission(code), true, code);
  }
  assert.ok(groupPerms(CATALOG).every(g => g.module !== 'dispatch'));
});

test('Roles permsReducer: held hidden codes survive toggling visible permissions', () => {
  let st = loaded([...UNENFORCED_CODES, 'users.view']);
  st = permsReducer(st, { type: 'TOGGLE', code: 'reports.view' });
  st = permsReducer(st, { type: 'TOGGLE', code: 'users.view' });
  st = permsReducer(st, { type: 'SET_MANY', codes: ['roles.view', 'roles.edit'], enable: true });
  st = permsReducer(st, { type: 'SET_MANY', codes: ['roles.view', 'roles.edit'], enable: false });
  const saved = new Set(Array.from(st.codes));
  for (const code of UNENFORCED_CODES) {
    assert.ok(saved.has(code), `${code} must be preserved`);
  }
  assert.ok(saved.has('reports.view'));
  assert.ok(!saved.has('users.view'));
});

test('Roles permsReducer: enabling a visible child adds its hidden parent without touching other codes', () => {
  const settings = permsReducer(loaded([]), { type: 'TOGGLE', code: 'settings.manage' });
  assert.deepEqual([...settings.codes].sort(), ['settings.manage', 'settings.view']);
  const branches = permsReducer(loaded(['dispatch.edit']), { type: 'TOGGLE', code: 'branches.create' });
  assert.deepEqual([...branches.codes].sort(), ['branches.create', 'branches.view', 'dispatch.edit']);
});

test('Roles permsReducer: disabling a visible child leaves hidden parents and unrelated hidden codes', () => {
  const st = permsReducer(
    loaded(['settings.manage', 'settings.view', 'branches.create', 'branches.view', 'company.edit', 'company.view']),
    { type: 'TOGGLE', code: 'settings.manage' },
  );
  assert.deepEqual([...st.codes].sort(), ['branches.create', 'branches.view', 'company.edit', 'company.view', 'settings.view']);
});

test('Roles permsReducer: disabling payroll.view also drops hidden payroll children the backend would otherwise use to restore it', () => {
  const st = permsReducer(
    loaded(['payroll.view', 'payroll.edit', 'payroll.approve', 'payroll.entry', 'drivers.create', 'drivers.view']),
    { type: 'TOGGLE', code: 'payroll.view' },
  );
  assert.deepEqual([...st.codes].sort(), ['drivers.create', 'drivers.view']);
});

test('People applyToggle: held hidden codes survive toggling visible permissions', () => {
  let codes = new Set<string>([...UNENFORCED_CODES, 'reports.view']);
  codes = applyToggle(codes, 'employees.manage', true);
  codes = applyToggle(codes, 'employees.manage', false);
  codes = applyToggle(codes, 'settings.manage', true);
  codes = applyToggle(codes, 'settings.manage', false);
  codes = applyToggle(codes, 'reports.view', false);
  for (const code of UNENFORCED_CODES) assert.ok(codes.has(code), `${code} must be preserved`);
  assert.ok(codes.has('employees.view'));
  assert.ok(!codes.has('reports.view'));
});

test('a hidden parent is never reported as a missing requirement', () => {
  assert.equal(isUserVisibleParentMissing('settings.view', new Set()), false);
  assert.equal(isUserVisibleParentMissing('branches.view', new Set()), false);
  assert.equal(isUserVisibleParentMissing('employees.view', new Set()), true);
  assert.equal(isUserVisibleParentMissing('employees.view', new Set(['employees.view'])), false);
  assert.equal(isUserVisibleParentMissing(undefined, new Set()), false);
});

// ── Pages follow the permissions the backend actually checks ─────────────────

function companyUser(codes: string[]): UserProfile {
  return makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: codes }),
  });
}

test('Current Payroll and Review are not shown to a user holding only payroll.edit', () => {
  const user = companyUser(['payroll.edit']);
  assert.equal(canViewCurrentPayroll(user), false);
  assert.equal(canViewReview(user), false);
});

test('Current Payroll and Review are not shown to a user holding only payroll.period.create', () => {
  const user = companyUser(['payroll.period.create']);
  assert.equal(canViewCurrentPayroll(user), false);
  assert.equal(canViewReview(user), false);
});

test('Current Payroll is shown for each permission the backend accepts for payroll reads', () => {
  for (const code of ['payroll.view', 'payroll.entry', 'payroll.finalize']) {
    assert.equal(canViewCurrentPayroll(companyUser([code])), true, code);
    assert.equal(canViewReview(companyUser([code])), true, code);
  }
  assert.equal(canViewCurrentPayroll(companyUser(['review.decide'])), false);
  assert.equal(canViewReview(companyUser(['review.decide'])), true);
});

test('no page is opened by a permission that no backend route checks', () => {
  for (const code of UNENFORCED_CODES) {
    const user = companyUser([code]);
    assert.equal(canViewCurrentPayroll(user), false, code);
    assert.equal(canViewReview(user), false, code);
  }
});
