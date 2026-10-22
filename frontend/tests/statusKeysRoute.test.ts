import { makeAuthority, makeBranchAccess as makeBranch } from './authorityTestFixtures.ts';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { canManageSettingsAdmin } from '../src/lib/permissions.ts';
import { toUserProfile } from '../src/store/authStore.ts';
import type {
  UserInfoResponse,
  UserProfile,
} from '../src/store/authStore.ts';

// ── Fixtures (mirrors tests/payrollSetupAuthority.test.ts) ─────────────────

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
const statusKeysBranchHookSource = readFileSync(
  new URL('../src/pages/settings/status-keys/useStatusKeyBranches.ts', import.meta.url), 'utf8');
const statusKeysCollectionHookSource = readFileSync(
  new URL('../src/pages/settings/status-keys/useStatusKeyCollection.ts', import.meta.url), 'utf8');
const statusKeysEditorHookSource = readFileSync(
  new URL('../src/pages/settings/status-keys/useStatusKeyEditor.ts', import.meta.url), 'utf8');
const statusKeysModelSource = readFileSync(
  new URL('../src/pages/settings/status-keys/statusKeysModel.ts', import.meta.url), 'utf8');
const statusKeysWorkspaceSource = readFileSync(
  new URL('../src/pages/settings/status-keys/StatusKeysWorkspace.tsx', import.meta.url), 'utf8');
const statusKeysEditorDialogSource = readFileSync(
  new URL('../src/pages/settings/status-keys/StatusKeyEditorDialog.tsx', import.meta.url), 'utf8');
const statusKeyDetailSource = readFileSync(
  new URL('../src/pages/settings/status-keys/StatusKeyDetail.tsx', import.meta.url), 'utf8');
const statusKeysDisplaySource = readFileSync(
  new URL('../src/pages/settings/status-keys/StatusKeysDisplay.tsx', import.meta.url), 'utf8');
const statusColumnsPanelSource = readFileSync(
  new URL('../src/pages/settings/status-keys/StatusRateColumnsPanel.tsx', import.meta.url), 'utf8');
const statusLimitsPanelSource = readFileSync(
  new URL('../src/pages/settings/status-keys/StatusLimitsPanel.tsx', import.meta.url), 'utf8');
const statusColumnsHookSource = readFileSync(
  new URL('../src/pages/settings/status-keys/useStatusRateColumns.ts', import.meta.url), 'utf8');
const statusLimitsHookSource = readFileSync(
  new URL('../src/pages/settings/status-keys/useBranchStatusLimits.ts', import.meta.url), 'utf8');
const settingsApiSource = readFileSync(new URL('../src/lib/settingsApi.ts', import.meta.url), 'utf8');
const statusKeysCssSource = readFileSync(
  new URL('../src/pages/settings/status-keys/StatusKeysPage.module.css', import.meta.url), 'utf8');
const statusKeysFeatureSources = [statusKeysPageSource, statusKeysBranchHookSource, statusKeysCollectionHookSource,
  statusKeysEditorHookSource, statusKeysModelSource, statusKeysWorkspaceSource, statusKeysEditorDialogSource,
  statusKeyDetailSource, statusKeysDisplaySource, statusColumnsPanelSource, statusLimitsPanelSource,
  statusColumnsHookSource, statusLimitsHookSource].join('\n');
const statusKeysUiSources = [statusKeysPageSource, statusKeysWorkspaceSource, statusKeysEditorDialogSource,
  statusKeyDetailSource, statusKeysDisplaySource, statusColumnsPanelSource, statusLimitsPanelSource].join('\n');

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

test('Status Keys feature modules: no payroll_setup / payroll-setup strings', () => {
  assert.doesNotMatch(statusKeysFeatureSources, /payroll_setup/);
  assert.doesNotMatch(statusKeysFeatureSources, /payroll-setup/);
});

test('StatusKeysPage.tsx: uses canManageSettingsAdmin', () => {
  assert.match(statusKeysPageSource, /canManageSettingsAdmin/);
});

test('Status Keys network access stays behind exact settingsApi operations', () => {
  assert.doesNotMatch(statusKeysFeatureSources, /apiClient\./);
  const apiFunction = (name: string) => {
    const functionSource = settingsApiSource.match(new RegExp(`export function ${name}\\([\\s\\S]*?(?=\\nexport function|$)`))?.[0];
    assert.ok(functionSource, `expected settingsApi.${name}`);
    return functionSource;
  };
  // The catalog is Company-level: no Status route is addressed through a Branch except the limits.
  assert.match(statusKeysBranchHookSource, /getSettingsBranches\(\)/);
  assert.match(statusKeysCollectionHookSource, /getStatusKeys\(true\)/);
  assert.match(apiFunction('getStatusKeys'), /apiClient\.get<StatusKey\[]>\('\/settings\/status-keys', \{\s*params: \{ include_inactive: includeInactive \},\s*\}\)/);
  assert.match(statusKeysEditorHookSource, /createStatusKey\(buildCreatePayload\(keyForm\)\)/);
  assert.match(apiFunction('createStatusKey'), /apiClient\.post<StatusKey>\('\/settings\/status-keys', payload\)/);
  assert.match(statusKeysEditorHookSource, /updateStatusKey\(editingKey\.status_key_id, payload\)/);
  assert.match(statusKeysCollectionHookSource, /updateStatusKey\(k\.status_key_id, \{ is_active: true \}\)/);
  assert.match(apiFunction('updateStatusKey'), /apiClient\.patch<StatusKey>\(`\/settings\/status-keys\/\$\{statusKeyId\}`, payload\)/);
  assert.match(statusKeysCollectionHookSource, /deactivateStatusKey\(k\.status_key_id\)/);
  assert.match(apiFunction('deactivateStatusKey'), /apiClient\.delete<StatusKey>\(`\/settings\/status-keys\/\$\{statusKeyId\}`\)/);
  assert.match(statusColumnsHookSource, /getStatusRateColumns\(true\)/);
  assert.match(apiFunction('getStatusRateColumns'), /apiClient\.get<StatusRateColumn\[]>\('\/settings\/status-rate-columns'/);
  assert.match(apiFunction('createStatusRateColumn'), /apiClient\.post<StatusRateColumn>\('\/settings\/status-rate-columns', payload\)/);
  assert.match(apiFunction('updateStatusRateColumn'), /apiClient\.patch<StatusRateColumn>\(`\/settings\/status-rate-columns\/\$\{columnId\}`, payload\)/);
  assert.match(statusLimitsHookSource, /getBranchStatusKeyLimits\(branchId\)/);
  assert.match(apiFunction('getBranchStatusKeyLimits'), /\/settings\/branches\/\$\{branchId\}\/status-key-limits/);
  assert.match(statusLimitsHookSource, /putBranchStatusKeyLimits\(branchId, statusKeyId, limitsToPayload\(form\)\)/);
  assert.match(apiFunction('putBranchStatusKeyLimits'), /apiClient\.put<StatusKeyBranchLimits>\(\s*`\/settings\/branches\/\$\{branchId\}\/status-keys\/\$\{statusKeyId\}\/limits`, payload\)/);

  const owners = ['getStatusKeys', 'createStatusKey', 'updateStatusKey', 'deactivateStatusKey', 'getStatusRateColumns',
    'createStatusRateColumn', 'updateStatusRateColumn', 'getBranchStatusKeyLimits', 'putBranchStatusKeyLimits']
    .map(apiFunction).join('\n');
  const callRe = /apiClient\.(get|post|patch|delete|put)(?:<[^>]*>)?\(\s*(`[^`]*`|'[^']*'|"[^"]*")/g;
  const matches = [...owners.matchAll(callRe)];
  assert.equal(matches.length, 9);
  for (const match of matches) {
    assert.match(match[2], /^[`'"]\/settings\/(status-keys|status-rate-columns|branches\/\$\{branchId\}\/(status-key-limits|status-keys\/\$\{statusKeyId\}\/limits))/,
      `apiClient.${match[1]} path ${match[2]} is outside the Company Status routes`);
  }
});

test('the Status page has no Branch picker for the catalog and no removed Branch-scoped route or field', () => {
  assert.doesNotMatch(statusKeysFeatureSources, /getBranchStatusKeys|createBranchStatusKey|updateBranchStatusKey|deactivateBranchStatusKey/);
  assert.doesNotMatch(settingsApiSource, /branches\/\$\{branchId\}\/status-keys`|status_keys_count/);
  // The only Branch selector is the one of the usage-limits panel.
  assert.doesNotMatch(statusKeysPageSource, /branchSelect|<select/);
  assert.match(statusLimitsPanelSource, /aria-label="Branch"/);
  assert.doesNotMatch(statusKeysFeatureSources, /limit_uses_per_period_enabled:\s*keyForm|deducts_from_yearly_allowance|allowance_category/);
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

test('StatusKeysPage.module.css: every referenced class across moved UI files is defined', () => {
  // Collect styles.X / s.X identifiers, but exclude non-CSS-module property
  // accesses on unrelated `s`-named objects (e.g. reducer state params).
  const usageRe = /\b(?:styles|s)\.([A-Za-z][A-Za-z0-9]*)/g;
  const used = new Set<string>();
  for (const m of statusKeysUiSources.matchAll(usageRe)) {
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
