import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import {
  companyBranchesCapabilities,
  accessNotice,
  ISO_DATE_PATTERN,
  validateFirstPayrollStartDate,
  buildBranchUpdatePayload,
  buildBranchCreatePayload,
  canCreateBranchWithOnboarding,
  showOnboardingCheckbox,
  onboardingResult,
  setupNeededLink,
  branchScheduleLink,
  payrollScheduleHref,
  readinessTitle,
} from '../src/pages/settings/company-branches/companyBranchesView.ts';
import { canAccessCompanyBranches, canViewSettings } from '../src/lib/permissions.ts';
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

/** Company-wide user holding exactly the given company-scope permission codes. */
function companyWideUser(codes: readonly string[]): UserProfile {
  return makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: [...codes] }),
  });
}

// ── Source files (read once) ────────────────────────────────────────────────

const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const appShellSource = readFileSync(new URL('../src/components/AppShell.tsx', import.meta.url), 'utf8');
const pageSource = readFileSync(
  new URL('../src/pages/settings/company-branches/CompanyBranchesPage.tsx', import.meta.url), 'utf8');
const viewSource = readFileSync(
  new URL('../src/pages/settings/company-branches/companyBranchesView.ts', import.meta.url), 'utf8');
const periodsListPageSource = readFileSync(
  new URL('../src/pages/payroll/PeriodsListPage.tsx', import.meta.url), 'utf8');

// ── A. Route / nav wiring ───────────────────────────────────────────────────

test('A. App.tsx: company-branches route gated by canAccessCompanyBranches', () => {
  const routeRe =
    /<Route\s+path=["']company-branches["']\s+element=\{<Gate\s+check=\{canAccessCompanyBranches\}\s*>\s*<CompanyBranchesPage\s*\/>\s*<\/Gate>\}\s*\/>/;
  assert.match(appSource, routeRe);
});

test('A. AppShell.tsx: Company & Branches nav item preceded by if (canAccessCompanyBranches(user))', () => {
  const idx = appShellSource.indexOf("'/settings/company-branches'");
  assert.notEqual(idx, -1, 'company-branches nav item not found');
  const windowStart = Math.max(0, idx - 200);
  const surrounding = appShellSource.slice(windowStart, idx + 200);
  assert.match(surrounding, /if\s*\(canAccessCompanyBranches\(user\)\)/);
});

test('A. AppShell.tsx: no "U7 will change" comment left', () => {
  assert.doesNotMatch(appShellSource, /U7 will change/);
});

// ── B. Outer settings gate / default redirect ───────────────────────────────

test('B. App.tsx: outer /settings gate expression includes || canCreateBranches(u)', () => {
  const gateRe =
    /Gate\s+check=\{\(u\)\s*=>\s*canViewSettings\(u\)\s*\|\|\s*canViewDailyPayItems\(u\)\s*\|\|\s*canViewPayrollSetups\(u\)\s*\|\|\s*canCreateBranches\(u\)\}/;
  assert.match(appSource, gateRe);
});

test('B. behavior: company-wide branches.create-only user reaches settings gate but not canViewSettings', () => {
  const user = companyWideUser(['branches.create']);
  assert.equal(canAccessCompanyBranches(user), true);
  assert.equal(canViewSettings(user), false);
});

test('B. App.tsx: SettingsDefaultRedirect first check is canAccessCompanyBranches(user) -> /settings/company-branches, before canViewSettings', () => {
  const idx = appSource.indexOf('function SettingsDefaultRedirect');
  assert.notEqual(idx, -1, 'SettingsDefaultRedirect not found');
  const end = appSource.indexOf('\n}', idx);
  const body = appSource.slice(idx, end);
  const accessIdx = body.indexOf('canAccessCompanyBranches(user)');
  const settingsIdx = body.indexOf('canViewSettings(user)');
  assert.notEqual(accessIdx, -1);
  assert.notEqual(settingsIdx, -1);
  assert.ok(accessIdx < settingsIdx, 'canAccessCompanyBranches step must precede canViewSettings step');
  assert.match(
    body,
    /canAccessCompanyBranches\(user\)\)\s*return\s*<Navigate to="\/settings\/company-branches" replace \/>/,
  );
});

// ── C/D/E/F. Capabilities ────────────────────────────────────────────────────

test('C. capabilities: company-wide setup.manage + branches.create -> canAdmin + canCreate', () => {
  const caps = companyBranchesCapabilities(companyWideUser(['setup.manage', 'branches.create']));
  assert.equal(caps.canAdmin, true);
  assert.equal(caps.canCreate, true);
});

test('C. capabilities: branches.create-only -> canCreate true, canAdmin false', () => {
  const caps = companyBranchesCapabilities(companyWideUser(['branches.create']));
  assert.equal(caps.canCreate, true);
  assert.equal(caps.canAdmin, false);
});

test('C. page source: Add Branch guarded by caps.canCreate', () => {
  assert.match(pageSource, /caps\.canCreate\s*&&\s*\(\s*<button className=\{styles\.btnPrimary\} onClick=\{openCreate\}/);
});

test('C. page source: Edit Company guarded by caps.canAdmin', () => {
  assert.match(pageSource, /caps\.canAdmin\s*&&\s*\(\s*<button className=\{styles\.btnSecondary\} onClick=\{startEditCo\}/);
});

test('C. page source: row edit action ("⋮") guarded by caps.canAdmin', () => {
  assert.match(pageSource, /caps\.canAdmin\s*&&\s*\(\s*<td className=\{styles\.tdAction\}>/);
});

test('C. page source: "Set as Default Branch" region requires caps.canAdmin', () => {
  assert.match(pageSource, /modal === 'edit' && caps\.canAdmin && editingBranch\.status === 'Active'/);
});

test('C. page source: no canEdit identifier remains', () => {
  assert.doesNotMatch(pageSource, /canEdit/);
});

test('D. capabilities: company-wide setup.manage WITHOUT branches.create -> canCreate false, canAdmin true, canAccess true', () => {
  const user = companyWideUser(['setup.manage']);
  const caps = companyBranchesCapabilities(user);
  assert.equal(caps.canCreate, false);
  assert.equal(caps.canAdmin, true);
  assert.equal(canAccessCompanyBranches(user), true);
});

test('E. capabilities: company payroll_setup.view(+manage+publish) alone -> canCreate false, canOnboard false, canAccessCompanyBranches false', () => {
  const user = companyWideUser(['payroll_setup.view', 'payroll_setup.manage', 'payroll_setup.publish']);
  const caps = companyBranchesCapabilities(user);
  assert.equal(caps.canCreate, false);
  assert.equal(caps.canOnboard, false);
  assert.equal(canAccessCompanyBranches(user), false);
});

// ── G. canOnboard truth table ────────────────────────────────────────────────

test('G. canOnboard: create + assign -> true', () => {
  const caps = companyBranchesCapabilities(companyWideUser(['branches.create', 'payroll_setup.assign']));
  assert.equal(caps.canOnboard, true);
});

test('G. canOnboard: create only -> false', () => {
  const caps = companyBranchesCapabilities(companyWideUser(['branches.create']));
  assert.equal(caps.canOnboard, false);
});

test('G. canOnboard: assign only (no create) -> false', () => {
  const caps = companyBranchesCapabilities(companyWideUser(['payroll_setup.assign']));
  assert.equal(caps.canOnboard, false);
});

test('G. canOnboard: create + view (no assign) -> false', () => {
  const caps = companyBranchesCapabilities(companyWideUser(['branches.create', 'payroll_setup.view']));
  assert.equal(caps.canOnboard, false);
});

test('G. canOnboard: create + manage + publish (no assign) -> false', () => {
  const caps = companyBranchesCapabilities(
    companyWideUser(['branches.create', 'payroll_setup.manage', 'payroll_setup.publish']),
  );
  assert.equal(caps.canOnboard, false);
});

// ── H/I. buildBranchCreatePayload — first_payroll_start_date gating ─────────

const BASE_FORM = {
  branch_name: 'North Branch',
  branch_code: 'NB',
  status: 'Active',
  is_default: false,
  address_line1: '123 Main St',
  city: 'Cairo',
  state_province: '',
  postal_code: '',
  country: 'Egypt',
  notes: '',
};

test('H. buildBranchCreatePayload: canOnboard false with a date -> no first_payroll_start_date key', () => {
  const payload = buildBranchCreatePayload(BASE_FORM, '2026-10-05', {
    canOnboard: false, setUpPayroll: true, canSetDefaultOnCreate: false,
  });
  assert.equal('first_payroll_start_date' in payload, false);
});

test('I. buildBranchCreatePayload: canOnboard true + setUpPayroll true + date -> key present and equal', () => {
  const payload = buildBranchCreatePayload(BASE_FORM, '2026-10-05', {
    canOnboard: true, setUpPayroll: true, canSetDefaultOnCreate: false,
  });
  assert.equal((payload as Record<string, unknown>).first_payroll_start_date, '2026-10-05');
});

test('I. buildBranchCreatePayload: canOnboard true + setUpPayroll true + blank/whitespace date -> key absent', () => {
  assert.equal('first_payroll_start_date' in buildBranchCreatePayload(BASE_FORM, '', {
    canOnboard: true, setUpPayroll: true, canSetDefaultOnCreate: false,
  }), false);
  assert.equal('first_payroll_start_date' in buildBranchCreatePayload(BASE_FORM, '   ', {
    canOnboard: true, setUpPayroll: true, canSetDefaultOnCreate: false,
  }), false);
});

// ── Unit B: setUpPayroll gating (the "Set up payroll for this branch now" checkbox) ──

test('Unit B: canOnboard true but setUpPayroll false (checkbox unchecked) -> key absent even with a date sitting in state', () => {
  const payload = buildBranchCreatePayload(BASE_FORM, '2026-10-05', {
    canOnboard: true, setUpPayroll: false, canSetDefaultOnCreate: false,
  });
  assert.equal('first_payroll_start_date' in payload, false);
});

test('Unit B: showOnboardingCheckbox mirrors caps.canOnboard exactly', () => {
  assert.equal(showOnboardingCheckbox({ canOnboard: true }), true);
  assert.equal(showOnboardingCheckbox({ canOnboard: false }), false);
});

test('Unit B: canCreateBranchWithOnboarding — unchecked always allows create', () => {
  assert.equal(
    canCreateBranchWithOnboarding({ setUpPayrollNow: false, hasCompanyDefault: true, choicesCurrentAndValid: false }),
    true,
  );
  assert.equal(
    canCreateBranchWithOnboarding({ setUpPayrollNow: false, hasCompanyDefault: false, choicesCurrentAndValid: false }),
    true,
  );
});

test('Unit B: canCreateBranchWithOnboarding — checked but no company default still allows create (payroll-less branch)', () => {
  assert.equal(
    canCreateBranchWithOnboarding({ setUpPayrollNow: true, hasCompanyDefault: false, choicesCurrentAndValid: false }),
    true,
  );
});

test('Unit B: canCreateBranchWithOnboarding — checked with a company default requires current, valid boundary choices', () => {
  assert.equal(
    canCreateBranchWithOnboarding({ setUpPayrollNow: true, hasCompanyDefault: true, choicesCurrentAndValid: false }),
    false,
  );
  assert.equal(
    canCreateBranchWithOnboarding({ setUpPayrollNow: true, hasCompanyDefault: true, choicesCurrentAndValid: true }),
    true,
  );
});

// ── J. buildBranchUpdatePayload ──────────────────────────────────────────────

test('J. buildBranchUpdatePayload: never has first_payroll_start_date or is_default', () => {
  const payload = buildBranchUpdatePayload(BASE_FORM);
  assert.equal('first_payroll_start_date' in payload, false);
  assert.equal('is_default' in payload, false);
});

test('J. page source: PATCH call uses buildBranchUpdatePayload', () => {
  assert.match(pageSource, /const payload = buildBranchUpdatePayload\(bForm\);/);
  assert.match(pageSource, /apiClient\.patch<BranchAdmin>\(`\/settings\/branches\/\$\{editingBranch\.branch_id\}`, payload\)/);
});

test('J. Unit B: onboarding section is shown only for modal === \'create\' AND showOnboardingCheckbox(caps)', () => {
  assert.match(pageSource, /modal === 'create' && showOnboardingCheckbox\(caps\) && \(/);
});

// ── K. Create payload key shape ──────────────────────────────────────────────

test('K. buildBranchCreatePayload: keys exactly equal the expected list (no setup_id/version_id/assignment_id/readiness fields)', () => {
  const payload = buildBranchCreatePayload(BASE_FORM, '2026-10-05', {
    canOnboard: true, setUpPayroll: true, canSetDefaultOnCreate: false,
  });
  assert.deepEqual(Object.keys(payload), [
    'branch_name', 'branch_code', 'status', 'is_default',
    'address_line1', 'city', 'state_province', 'postal_code', 'country', 'notes',
    'first_payroll_start_date',
  ]);
  const withoutDate = buildBranchCreatePayload(BASE_FORM, '', {
    canOnboard: false, setUpPayroll: false, canSetDefaultOnCreate: false,
  });
  assert.deepEqual(Object.keys(withoutDate), [
    'branch_name', 'branch_code', 'status', 'is_default',
    'address_line1', 'city', 'state_province', 'postal_code', 'country', 'notes',
  ]);
  for (const forbidden of ['setup_id', 'version_id', 'assignment_id', 'schedule_readiness_reason', 'schedule_readiness_date']) {
    assert.equal(forbidden in payload, false, forbidden);
  }
});

// ── V. canSetDefaultOnCreate truth table ─────────────────────────────────────

test('V. canSetDefaultOnCreate: create + setup.manage -> true', () => {
  const caps = companyBranchesCapabilities(companyWideUser(['branches.create', 'setup.manage']));
  assert.equal(caps.canSetDefaultOnCreate, true);
});

test('V. canSetDefaultOnCreate: create-only -> false', () => {
  const caps = companyBranchesCapabilities(companyWideUser(['branches.create']));
  assert.equal(caps.canSetDefaultOnCreate, false);
});

test('V. canSetDefaultOnCreate: setup.manage-only (admin, no create) -> false', () => {
  const caps = companyBranchesCapabilities(companyWideUser(['setup.manage']));
  assert.equal(caps.canSetDefaultOnCreate, false);
});

test('V. canSetDefaultOnCreate: create + payroll_setup.assign (no setup.manage) -> false', () => {
  const caps = companyBranchesCapabilities(companyWideUser(['branches.create', 'payroll_setup.assign']));
  assert.equal(caps.canSetDefaultOnCreate, false);
});

test('V. canSetDefaultOnCreate: create + payroll_setup.view/manage/publish (no setup.manage) -> false', () => {
  const caps = companyBranchesCapabilities(
    companyWideUser(['branches.create', 'payroll_setup.view', 'payroll_setup.manage', 'payroll_setup.publish']),
  );
  assert.equal(caps.canSetDefaultOnCreate, false);
});

test('V. canSetDefaultOnCreate: Driver with company branches.create + setup.manage -> false', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 10, scope: 'SpecificBranch', role_code: 'DRIVER', role_name: 'Driver' })],
    authority: makeAuthority({ company_permissions: ['branches.create', 'setup.manage'] }),
  });
  const caps = companyBranchesCapabilities(user);
  assert.equal(caps.canSetDefaultOnCreate, false);
});

test('V. canSetDefaultOnCreate: null user -> false', () => {
  const caps = companyBranchesCapabilities(null);
  assert.equal(caps.canSetDefaultOnCreate, false);
});

// ── W. buildBranchCreatePayload — is_default gating ─────────────────────────

test('W. is_default forced false without canSetDefaultOnCreate even when form.is_default is true', () => {
  const form = { ...BASE_FORM, is_default: true };
  const payload = buildBranchCreatePayload(form, '', { canOnboard: false, setUpPayroll: false, canSetDefaultOnCreate: false });
  assert.equal((payload as Record<string, unknown>).is_default, false);
});

test('W. is_default reflects form value when canSetDefaultOnCreate is true', () => {
  const trueForm = { ...BASE_FORM, is_default: true };
  const truePayload = buildBranchCreatePayload(trueForm, '', { canOnboard: false, setUpPayroll: false, canSetDefaultOnCreate: true });
  assert.equal((truePayload as Record<string, unknown>).is_default, true);

  const falseForm = { ...BASE_FORM, is_default: false };
  const falsePayload = buildBranchCreatePayload(falseForm, '', { canOnboard: false, setUpPayroll: false, canSetDefaultOnCreate: true });
  assert.equal((falsePayload as Record<string, unknown>).is_default, false);
});

test('W. canOnboard stays independent of canSetDefaultOnCreate: date present, is_default false', () => {
  const form = { ...BASE_FORM, is_default: true };
  const payload = buildBranchCreatePayload(form, '2026-10-05', { canOnboard: true, setUpPayroll: true, canSetDefaultOnCreate: false });
  assert.equal((payload as Record<string, unknown>).first_payroll_start_date, '2026-10-05');
  assert.equal((payload as Record<string, unknown>).is_default, false);
});

// ── X. Page source: default-on-create gating ────────────────────────────────

test('X. page source: "Set as default branch" checkbox conditioned on isCreate && canSetDefault', () => {
  assert.match(pageSource, /isCreate\s*&&\s*canSetDefault\s*&&\s*\(/);
});

test('X. page source: help line conditioned on isCreate && !canSetDefault', () => {
  assert.match(pageSource, /isCreate\s*&&\s*!canSetDefault\s*&&\s*\(/);
  assert.match(pageSource, /Only settings administrators can make a new Branch the Company default\./);
});

test('X. page source: modal passes canSetDefault={caps.canSetDefaultOnCreate}', () => {
  assert.match(pageSource, /canSetDefault=\{caps\.canSetDefaultOnCreate\}/);
});

test('X. page source: create call passes canSetDefaultOnCreate: caps.canSetDefaultOnCreate', () => {
  assert.match(pageSource, /canSetDefaultOnCreate:\s*caps\.canSetDefaultOnCreate/);
});

test('X. page source: edit PATCH still uses buildBranchUpdatePayload; set-default POST path present exactly once', () => {
  assert.match(pageSource, /const payload = buildBranchUpdatePayload\(bForm\);/);
  const matches = pageSource.match(/\/set-default/g) ?? [];
  assert.equal(matches.length, 1);
});

// ── L. canOnboard independent of payroll_setup.view; no legacy imports ──────

test('L. canOnboard: create + assign WITHOUT payroll_setup.view -> true', () => {
  const user = companyWideUser(['branches.create', 'payroll_setup.assign']);
  const caps = companyBranchesCapabilities(user);
  assert.equal(caps.canOnboard, true);
  assert.equal(caps.canViewSetups, false);
});

test('L. page source: no /payroll-setup raw route string, no getDefaultPayrollSetup', () => {
  assert.doesNotMatch(pageSource, /\/payroll-setup/);
  assert.doesNotMatch(pageSource, /getDefaultPayrollSetup/);
});

test('L (Unit B): page now imports exactly getBranchOnboardingOptions from lib/payrollSetupApi (the onboarding checkbox flow)', () => {
  const importMatch = pageSource.match(/import\s*\{([^}]*)\}\s*from\s*'\.\.\/\.\.\/\.\.\/lib\/payrollSetupApi'/);
  assert.ok(importMatch, 'expected an import from lib/payrollSetupApi');
  const names = importMatch![1].split(',').map((s) => s.trim()).filter((s) => s.length > 0);
  assert.deepEqual(names, ['getBranchOnboardingOptions']);
});

// ── M/N. onboardingResult ────────────────────────────────────────────────────

test('M. onboardingResult: NO_COMPANY_DEFAULT -> incomplete, verbatim spec text', () => {
  const result = onboardingResult({ schedule_readiness_reason: 'NO_COMPANY_DEFAULT', schedule_readiness_date: '2026-10-05' });
  assert.equal(result.tone, 'incomplete');
  assert.ok(result.message.startsWith('Branch created'));
  assert.equal(
    result.message,
    'Branch created, but no Company default Payroll Setup was available for automatic onboarding.',
  );
});

test('N. onboardingResult: READY message, evaluatedDate verbatim', () => {
  const result = onboardingResult({ schedule_readiness_reason: 'READY', schedule_readiness_date: '2026-11-01' });
  assert.equal(result.tone, 'ready');
  assert.equal(
    result.message,
    'Branch created and its Payroll Setup is ready for the evaluated payroll start.',
  );
  assert.equal(result.evaluatedDate, '2026-11-01');
});

test('N. onboardingResult: unknown code FUTURE_X -> tone unknown, message contains code', () => {
  const result = onboardingResult({ schedule_readiness_reason: 'FUTURE_X', schedule_readiness_date: '2026-11-01' });
  assert.equal(result.tone, 'unknown');
  assert.ok(result.message.includes('FUTURE_X'));
});

test('N. onboardingResult: null reason -> tone unknown, code null', () => {
  const result = onboardingResult({ schedule_readiness_reason: null, schedule_readiness_date: null });
  assert.equal(result.tone, 'unknown');
  assert.equal(result.code, null);
  assert.equal(result.evaluatedDate, null);
});

test('N. onboardingResult: known non-READY (NO_PUBLISHED_VERSION) -> incomplete with label and code', () => {
  const result = onboardingResult({ schedule_readiness_reason: 'NO_PUBLISHED_VERSION', schedule_readiness_date: '2026-11-01' });
  assert.equal(result.tone, 'incomplete');
  assert.equal(result.code, 'NO_PUBLISHED_VERSION');
  assert.ok(result.message.includes('No published version'));
  assert.ok(result.message.includes('NO_PUBLISHED_VERSION'));
});

// ── O. Source hygiene guards ─────────────────────────────────────────────────

test('O. companyBranchesView.ts: no Date/apiClient/axios/react usage', () => {
  assert.doesNotMatch(viewSource, /new Date/);
  assert.doesNotMatch(viewSource, /Date\.now/);
  assert.doesNotMatch(viewSource, /Date\.parse/);
  assert.doesNotMatch(viewSource, /toISOString/);
  assert.doesNotMatch(viewSource, /apiClient/);
  assert.doesNotMatch(viewSource, /axios/);
  assert.doesNotMatch(viewSource, /from 'react'/);
});

test('O. page source: no resolver-logic strings', () => {
  assert.doesNotMatch(pageSource, /resolve/);
  assert.doesNotMatch(pageSource, /effective_from/);
  assert.doesNotMatch(pageSource, /anchor_start_date/);
  assert.doesNotMatch(pageSource, /normal_days_off_mask/);
});

test('O. page source: exactly one "new Date(" occurrence (the pre-existing fmtDate)', () => {
  const matches = pageSource.match(/new Date\(/g) ?? [];
  assert.equal(matches.length, 1);
});

// ── P. setupNeededLink ───────────────────────────────────────────────────────

test('P. setupNeededLink: payroll_setup.view user -> setups', () => {
  const user = companyWideUser(['payroll_setup.view']);
  assert.deepEqual(setupNeededLink(user, 7), { kind: 'setups', to: '/settings/payroll' });
});

test('P. setupNeededLink: branch payroll.view (no setup view) on branch 7 -> schedule link', () => {
  const user = makeUser({
    authority: makeAuthority({ branch_permissions: [{ branch_id: 7, permissions: ['payroll.view'] }] }),
  });
  assert.deepEqual(setupNeededLink(user, 7), { kind: 'schedule', to: '/payroll/schedule?branchId=7' });
});

test('P. setupNeededLink: branches.create-only / setup.manage-only user -> none', () => {
  assert.deepEqual(setupNeededLink(companyWideUser(['branches.create']), 7), { kind: 'none' });
  assert.deepEqual(setupNeededLink(companyWideUser(['setup.manage']), 7), { kind: 'none' });
});

test('P. setupNeededLink: null user -> none', () => {
  assert.deepEqual(setupNeededLink(null, 7), { kind: 'none' });
});

test('P. page source: no literal navigate(\'/settings/payroll\') remains; Setup Needed uses setupNeededLink(', () => {
  assert.doesNotMatch(pageSource, /navigate\(['"]\/settings\/payroll['"]\)/);
  assert.match(pageSource, /setupNeededLink\(/);
});

// ── Q. payrollScheduleHref ────────────────────────────────────────────────────

test('Q. payrollScheduleHref(12) === "/payroll/schedule?branchId=12"', () => {
  assert.equal(payrollScheduleHref(12), '/payroll/schedule?branchId=12');
});

// ── R. branchScheduleLink ─────────────────────────────────────────────────────

test('R. branchScheduleLink: branch payroll.view on 7 -> href for 7, null for 8', () => {
  const user = makeUser({
    authority: makeAuthority({ branch_permissions: [{ branch_id: 7, permissions: ['payroll.view'] }] }),
  });
  assert.equal(branchScheduleLink(user, 7), '/payroll/schedule?branchId=7');
  assert.equal(branchScheduleLink(user, 8), null);
});

test('R. branchScheduleLink: payroll_setup.view-only -> null', () => {
  assert.equal(branchScheduleLink(companyWideUser(['payroll_setup.view']), 7), null);
});

test('R. branchScheduleLink: Driver with payroll.view -> null', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 7, scope: 'SpecificBranch', role_code: 'DRIVER', role_name: 'Driver' })],
    authority: makeAuthority({ branch_permissions: [{ branch_id: 7, permissions: ['payroll.view'] }] }),
  });
  assert.equal(branchScheduleLink(user, 7), null);
});

test('R. page source: uses branchScheduleLink(', () => {
  assert.match(pageSource, /branchScheduleLink\(/);
});

// ── S. No legacy route / string leakage ──────────────────────────────────────

test('S. page source: no legacy payroll-setup route/string, no PayrollSetupPage reference', () => {
  assert.doesNotMatch(pageSource, /\/settings\/branches\/\$\{[^}]*\}\/payroll-setup/);
  assert.doesNotMatch(pageSource, /payroll-setup/);
  assert.doesNotMatch(pageSource, /PayrollSetupPage/);
});

// ── T. PeriodsListPage.tsx untouched / no schedule route string ─────────────

test('T. PeriodsListPage.tsx source does not contain "/payroll/schedule"', () => {
  assert.doesNotMatch(periodsListPageSource, /\/payroll\/schedule/);
});

// ── U. Driver / ODA exclusion ────────────────────────────────────────────────

test('U. Driver role_code with company branches.create + payroll_setup.assign -> canCreate false, canOnboard false', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 10, scope: 'SpecificBranch', role_code: 'DRIVER', role_name: 'Driver' })],
    authority: makeAuthority({ company_permissions: ['branches.create', 'payroll_setup.assign'] }),
  });
  const caps = companyBranchesCapabilities(user);
  assert.equal(caps.canCreate, false);
  assert.equal(caps.canOnboard, false);
});

test('U. DRIVER/Self with company branches.create + payroll_setup.assign -> canCreate false, canOnboard false', () => {
  const user = makeUser({
    self_assignments: [{ role_code: 'DRIVER', role_name: 'Driver', scope: 'Self' }],
    authority: makeAuthority({ company_permissions: ['branches.create', 'payroll_setup.assign'] }),
  });
  const caps = companyBranchesCapabilities(user);
  assert.equal(caps.canCreate, false);
  assert.equal(caps.canOnboard, false);
});

test('U. mixed Driver + company-admin with branches.create -> canCreate false', () => {
  const branches: BranchAccess[] = [
    makeBranch({ branch_id: null, scope: 'AllCompanyBranches', role_code: 'COMPANY_ADMIN', role_name: 'Company Admin' }),
    makeBranch({ branch_id: 10, scope: 'SpecificBranch', role_code: 'DRIVER', role_name: 'Driver' }),
  ];
  const user = makeUser({
    branches,
    authority: makeAuthority({ company_permissions: ['branches.create', 'payroll_setup.assign', 'setup.manage'] }),
  });
  const caps = companyBranchesCapabilities(user);
  assert.equal(caps.canCreate, false);
});

// ── accessNotice ─────────────────────────────────────────────────────────────

test('accessNotice: canAdmin && canCreate -> null', () => {
  assert.equal(accessNotice({ canCreate: true, canAdmin: true, canOnboard: false, canViewSetups: false }), null);
});

test('accessNotice: canAdmin only -> creating branches requires branches.create', () => {
  const msg = accessNotice({ canCreate: false, canAdmin: true, canOnboard: false, canViewSetups: false });
  assert.equal(
    msg,
    'You can edit the Company and existing Branches. Creating Branches requires the branches.create permission.',
  );
});

test('accessNotice: canCreate only -> editing requires settings administration; no "don\'t have permission" text', () => {
  const msg = accessNotice({ canCreate: true, canAdmin: false, canOnboard: false, canViewSetups: false });
  assert.equal(
    msg,
    'You can create Branches. Editing the Company, existing Branches and the default Branch requires settings administration permission.',
  );
  assert.doesNotMatch(msg ?? '', /don't have permission/);
});

test('accessNotice: neither -> generic no-permission message', () => {
  const msg = accessNotice({ canCreate: false, canAdmin: false, canOnboard: false, canViewSetups: false });
  assert.equal(msg, "You don't have permission to edit company or branch settings.");
});

// ── validateFirstPayrollStartDate ────────────────────────────────────────────

test('validateFirstPayrollStartDate: blank/whitespace/valid ISO date are all ok', () => {
  assert.equal(validateFirstPayrollStartDate(''), null);
  assert.equal(validateFirstPayrollStartDate('  '), null);
  assert.equal(validateFirstPayrollStartDate('2026-10-05'), null);
});

test('validateFirstPayrollStartDate: malformed strings are rejected', () => {
  assert.equal(validateFirstPayrollStartDate('05/10/2026'), 'Enter the first payroll start date as YYYY-MM-DD.');
  assert.equal(validateFirstPayrollStartDate('2026-1-5'), 'Enter the first payroll start date as YYYY-MM-DD.');
});

test('ISO_DATE_PATTERN: sanity check', () => {
  assert.equal(ISO_DATE_PATTERN.test('2026-10-05'), true);
  assert.equal(ISO_DATE_PATTERN.test('2026-1-5'), false);
});

// ── readinessTitle ────────────────────────────────────────────────────────────

test('readinessTitle: null -> undefined; known code -> label (code)', () => {
  assert.equal(readinessTitle(null), undefined);
  assert.equal(readinessTitle('NO_ASSIGNMENT'), 'Not assigned (NO_ASSIGNMENT)');
});

// ── Unit B: friendly wording + onboarding checkbox UI ───────────────────────

test('Unit B: branch table uses "Payroll not set up" instead of "Setup Needed"', () => {
  assert.doesNotMatch(pageSource, /Setup Needed/);
  assert.match(pageSource, /Payroll not set up/);
});

test('Unit B: "View payroll schedule" replaces "View Payroll Schedule" as the link label', () => {
  assert.doesNotMatch(pageSource, /View Payroll Schedule/);
  assert.match(pageSource, /View payroll schedule/);
});

test('Unit B: create-mode onboarding uses a checkbox labelled "Set up payroll for this branch now"', () => {
  assert.match(pageSource, /type="checkbox"/);
  assert.match(pageSource, /Set up payroll for this branch now/);
});

test('Unit B: no native type="date" input remains in the page (replaced by PayrollBoundaryDateInput)', () => {
  assert.doesNotMatch(pageSource, /type="date"/);
});

test('Unit B: page renders <PayrollBoundaryDateInput context="onboarding"> and <PayrollErrorNotice>', () => {
  assert.match(pageSource, /<PayrollBoundaryDateInput/);
  assert.match(pageSource, /context="onboarding"/);
  assert.match(pageSource, /<PayrollErrorNotice/);
});

test('Unit B: "no company default" messaging names both the canViewSetups link and the administrator fallback', () => {
  assert.match(pageSource, /No company default payroll policy is set, so payroll can&apos;t be set up for this branch/);
  assert.match(pageSource, /Choose a default payroll policy/);
  assert.match(pageSource, /Ask an administrator to choose a company default payroll policy\./);
});

test('Unit B: the onboarding boundary-choices key includes the default setup id', () => {
  assert.match(pageSource, /`onboarding\|\$\{onboardingDefaultSetup\?\.setup_id \?\? 'none'\}`/);
});

test('Unit B: the Create button is disabled when create mode and !canCreateNow', () => {
  assert.match(pageSource, /disabled=\{savingBranch \|\| \(modal === 'create' && !canCreateNow\)\}/);
});
