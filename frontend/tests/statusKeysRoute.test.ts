import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { canManageSettingsAdmin } from '../src/lib/permissions.ts';
import { toUserProfile } from '../src/store/authStore.ts';
import type {
  BranchAccess,
  PermissionAuthority,
  UserInfoResponse,
  UserProfile,
} from '../src/store/authStore.ts';

// ── Fixtures (mirrors tests/payrollSetupAuthority.test.ts) ─────────────────

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

// ── Source files (read once) ────────────────────────────────────────────────

const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const appShellSource = readFileSync(new URL('../src/components/AppShell.tsx', import.meta.url), 'utf8');
const statusKeysPageSource = readFileSync(
  new URL('../src/pages/settings/status-keys/StatusKeysPage.tsx', import.meta.url), 'utf8');
const statusKeysCssSource = readFileSync(
  new URL('../src/pages/settings/status-keys/StatusKeysPage.module.css', import.meta.url), 'utf8');

// ── A. App.tsx route wiring ─────────────────────────────────────────────────

test('App.tsx: status-keys route is gated by canManageSettingsAdmin and renders StatusKeysPage', () => {
  const routeRe = /<Route\s+path=["']status-keys["']\s+element=\{<Gate\s+check=\{canManageSettingsAdmin\}\s*>\s*<StatusKeysPage\s*\/>\s*<\/Gate>\}\s*\/>/;
  assert.match(appSource, routeRe);
});

test('App.tsx: payroll route now renders PayrollSetupsPage gated by canViewPayrollSetups', () => {
  const routeRe =
    /<Route\s+path=["']payroll["']\s+element=\{\s*<LegacyStatusKeysRedirect>\s*<Gate\s+check=\{canViewPayrollSetups\}\s*>\s*<PayrollSetupsPage\s*\/>\s*<\/Gate>\s*<\/LegacyStatusKeysRedirect>\s*\}\s*\/>/;
  assert.match(appSource, routeRe);
});

// ── B. AppShell.tsx nav wiring ──────────────────────────────────────────────

test('AppShell.tsx: Status Keys nav item exists and is conditioned on canManageSettingsAdmin(user)', () => {
  assert.match(appShellSource, /'\/settings\/status-keys'/);
  assert.match(appShellSource, /label:\s*'Status Keys'/);

  // The nav item text and the canManageSettingsAdmin( guard must appear in the
  // same statement/expression — find the line(s) containing the status-keys
  // nav item and assert the guard is present in that same expression block.
  const idx = appShellSource.indexOf("'/settings/status-keys'");
  assert.notEqual(idx, -1, 'status-keys nav item not found');
  // Look backwards up to 200 chars for the guard (same conditional expression).
  const windowStart = Math.max(0, idx - 200);
  const surrounding = appShellSource.slice(windowStart, idx + 200);
  assert.match(surrounding, /canManageSettingsAdmin\(user\)/);
});

test('AppShell.tsx: usePageTitle maps /settings/status-keys to "Status Keys"', () => {
  const re = /pathname\.startsWith\(['"]\/settings\/status-keys['"]\)\)\s*return\s*'Status Keys'/;
  assert.match(appShellSource, re);
});

// ── C. Authority ────────────────────────────────────────────────────────────

test('canManageSettingsAdmin: company-wide setup.manage grants access (positive control)', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: ['setup.manage'] }),
  });
  assert.equal(canManageSettingsAdmin(user), true);
});

test('canManageSettingsAdmin: DRIVER SpecificBranch user without company setup.manage is denied', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 10, scope: 'SpecificBranch', role_code: 'DRIVER', role_name: 'Driver' })],
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['setup.manage'] }] }),
  });
  assert.equal(canManageSettingsAdmin(user), false);
});

test('canManageSettingsAdmin: DRIVER/Self user with branch_permissions incl. setup.manage is denied', () => {
  const user = makeUser({
    self_assignments: [{ role_code: 'DRIVER', role_name: 'Driver', scope: 'Self' }],
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['setup.manage'] }] }),
  });
  assert.equal(canManageSettingsAdmin(user), false);
});

test('canManageSettingsAdmin: SpecificBranch user with branch-scoped setup.manage is denied', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 10, scope: 'SpecificBranch' })],
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['setup.manage'] }] }),
  });
  assert.equal(canManageSettingsAdmin(user), false);
});

test('canManageSettingsAdmin: company users holding only payroll_setup.* codes are denied (no payroll_setup dependency)', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({
      company_permissions: ['payroll_setup.view', 'payroll_setup.manage', 'payroll_setup.publish', 'payroll_setup.assign'],
    }),
  });
  assert.equal(canManageSettingsAdmin(user), false);
});

// ── D. Unrelated settings routes are untouched ──────────────────────────────

test('App.tsx: company-branches (U7 gate), pay-items, roles routes present with expected gates', () => {
  assert.match(
    appSource,
    /<Route\s+path=["']company-branches["']\s+element=\{<Gate\s+check=\{canAccessCompanyBranches\}\s*>\s*<CompanyBranchesPage\s*\/>\s*<\/Gate>\}\s*\/>/,
  );
  assert.match(
    appSource,
    /<Route\s+path=["']pay-items["']\s+element=\{<Gate\s+check=\{canViewDailyPayItems\}\s*>\s*<PayItemsPage\s*\/>\s*<\/Gate>\}\s*\/>/,
  );
  assert.match(
    appSource,
    /<Route\s+path=["']roles["']\s+element=\{<Gate\s+check=\{canManageRoles\}\s*>\s*<RolesPage\s*\/>\s*<\/Gate>\}\s*\/>/,
  );
});

// ── F. StatusKeysPage.tsx source hygiene ────────────────────────────────────

test('StatusKeysPage.tsx: no payroll_setup / payroll-setup strings', () => {
  assert.doesNotMatch(statusKeysPageSource, /payroll_setup/);
  assert.doesNotMatch(statusKeysPageSource, /payroll-setup/);
});

test('StatusKeysPage.tsx: uses canManageSettingsAdmin', () => {
  assert.match(statusKeysPageSource, /canManageSettingsAdmin/);
});

test('StatusKeysPage.tsx: every apiClient call targets /settings/branches', () => {
  const callRe = /apiClient\.(get|post|patch|delete|put)(?:<[^>]*>)?\(\s*(`[^`]*`|'[^']*'|"[^"]*")/g;
  const matches = [...statusKeysPageSource.matchAll(callRe)];
  assert.ok(matches.length > 0, 'expected at least one apiClient call in StatusKeysPage.tsx');
  for (const m of matches) {
    const pathLiteral = m[2];
    assert.match(
      pathLiteral,
      /^[`'"]\/settings\/branches/,
      `apiClient.${m[1]} call path ${pathLiteral} does not start with /settings/branches`,
    );
  }
});

// ── G. No premature tab redirect; legacy tab intact ─────────────────────────

test('App.tsx has no hard-coded tab=status-keys string; redirect logic lives in payrollSetupsView.ts', () => {
  assert.doesNotMatch(appSource, /tab=status-keys/);
});

test('legacy ?tab=status-keys URL is handled by LegacyStatusKeysRedirect in App.tsx', () => {
  assert.match(appSource, /LegacyStatusKeysRedirect/);
  assert.match(appSource, /legacyPayrollSettingsRedirect/);
});

// ── H. Every styles.X / s.X class referenced in StatusKeysPage.tsx is defined in its CSS module ──

test('StatusKeysPage.module.css: every referenced class is defined', () => {
  // Collect styles.X / s.X identifiers, but exclude non-CSS-module property
  // accesses on unrelated `s`-named objects (e.g. reducer state params).
  const usageRe = /\b(?:styles|s)\.([A-Za-z][A-Za-z0-9]*)/g;
  const used = new Set<string>();
  for (const m of statusKeysPageSource.matchAll(usageRe)) {
    used.add(m[1]);
  }

  // Known false positives: identifiers accessed on locally-scoped variables
  // also named `s` that are NOT the styles module (e.g. the keysLoadReducer's
  // `s: KeysLoadState` parameter, accessed as `s.selectedKeyId`).
  const NOT_CSS_CLASSES = new Set(['selectedKeyId']);
  for (const skip of NOT_CSS_CLASSES) used.delete(skip);

  const definedRe = /^\.([A-Za-z][A-Za-z0-9]*)/gm;
  const defined = new Set<string>();
  for (const m of statusKeysCssSource.matchAll(definedRe)) {
    defined.add(m[1]);
  }

  const missing = [...used].filter(c => !defined.has(c));
  assert.deepEqual(missing, [], `CSS classes referenced but not defined: ${missing.join(', ')}`);
});
