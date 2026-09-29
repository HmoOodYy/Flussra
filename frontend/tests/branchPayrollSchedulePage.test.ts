import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  canViewAnyBranchPayrollSchedule,
  canViewBranchPayrollSchedule,
} from '../src/lib/permissions.ts';
import { toUserProfile } from '../src/store/authStore.ts';
import type {
  BranchAccess,
  PermissionAuthority,
  UserInfoResponse,
  UserProfile,
} from '../src/store/authStore.ts';

// ── Fixtures (mirrors tests/payrollSetupAuthority.test.ts) ──────────────────

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

// ── Source loading ────────────────────────────────────────────────────────────

const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const appShellSource = readFileSync(new URL('../src/components/AppShell.tsx', import.meta.url), 'utf8');
const pageSource = readFileSync(
  new URL('../src/pages/payroll/schedule/BranchPayrollSchedulePage.tsx', import.meta.url),
  'utf8',
);

// ── A: route wiring in App.tsx ────────────────────────────────────────────────

test('A: App.tsx wires /payroll/schedule with Gate check={canViewAnyBranchPayrollSchedule} outside /settings', () => {
  const routeMatch = appSource.match(
    /<Route\s+path="\/payroll\/schedule"\s+element=\{<Gate check=\{canViewAnyBranchPayrollSchedule\}><BranchPayrollSchedulePage \/><\/Gate>\}\s*\/>/,
  );
  assert.ok(routeMatch, 'expected the exact /payroll/schedule route block');

  const routeIndex = appSource.indexOf(routeMatch![0]);
  assert.ok(routeIndex >= 0);
  assert.ok(!routeMatch![0].startsWith('/settings'));

  const settingsCommentIndex = appSource.indexOf('Settings — outer gate');
  assert.ok(settingsCommentIndex >= 0, 'expected the "Settings — outer gate" comment to exist');
  assert.ok(routeIndex < settingsCommentIndex, 'the schedule route must appear before the Settings block');
});

// ── B: page uses canViewBranchPayrollSchedule from lib/permissions ──────────

test('B: page calls canViewBranchPayrollSchedule( and imports it from lib/permissions', () => {
  assert.ok(pageSource.includes('canViewBranchPayrollSchedule('));
  const importMatch = pageSource.match(/import\s*\{([^}]*)\}\s*from\s*'\.\.\/\.\.\/\.\.\/lib\/permissions'/);
  assert.ok(importMatch, 'expected an import from lib/permissions');
  assert.ok(importMatch![1].includes('canViewBranchPayrollSchedule'));
});

// ── C: no company-policy / settings-admin helpers or codes anywhere ─────────

test('C: page and route line never reference company payroll_setup / settings-admin gates', () => {
  const pathIndex = appSource.indexOf('path="/payroll/schedule"');
  assert.ok(pathIndex >= 0);
  const routeWindow = appSource.slice(Math.max(0, pathIndex - 40), pathIndex + 200);
  const forbidden = [
    'canViewPayrollSetups',
    'canManagePayrollSetups',
    'canAssignPayrollSetups',
    'canPublishPayrollSetups',
    'canManageSettingsAdmin',
    'canViewSettings',
    'payroll_setup.',
    'setup.manage',
    'settings.manage',
  ];
  for (const term of forbidden) {
    assert.ok(!pageSource.includes(term), `page must not contain: ${term}`);
    assert.ok(!routeWindow.includes(term), `route line must not contain: ${term}`);
  }
});

test('C behavior: company payroll_setup.* grants (no payroll.view) do not grant schedule access', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({
      company_permissions: [
        'payroll_setup.view',
        'payroll_setup.manage',
        'payroll_setup.publish',
        'payroll_setup.assign',
      ],
    }),
  });
  assert.equal(canViewAnyBranchPayrollSchedule(user), false);
  assert.equal(canViewBranchPayrollSchedule(user, 7), false);
});

// ── D/E: Driver and OwnDriverDataOnly deny despite payroll.view ─────────────

test('D: Driver role user with payroll.view is denied both helpers', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 7, scope: 'SpecificBranch', role_code: 'DRIVER', role_name: 'Driver' })],
    authority: makeAuthority({ company_permissions: ['payroll.view'] }),
  });
  assert.equal(canViewAnyBranchPayrollSchedule(user), false);
  assert.equal(canViewBranchPayrollSchedule(user, 7), false);
});

test('E: OwnDriverDataOnly user with payroll.view is denied both helpers', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: 7, scope: 'OwnDriverDataOnly', role_code: 'DRIVER', role_name: 'Driver' })],
    authority: makeAuthority({ company_permissions: ['payroll.view'] }),
  });
  assert.equal(canViewAnyBranchPayrollSchedule(user), false);
  assert.equal(canViewBranchPayrollSchedule(user, 7), false);
});

test('E: mixed Driver + company-admin with company payroll.view is denied both helpers', () => {
  const branches: BranchAccess[] = [
    makeBranch({ branch_id: null, scope: 'AllCompanyBranches', role_code: 'COMPANY_ADMIN', role_name: 'Company Admin' }),
    makeBranch({ branch_id: 7, scope: 'SpecificBranch', role_code: 'DRIVER', role_name: 'Driver' }),
  ];
  const authority = makeAuthority({ company_permissions: ['payroll.view'] });
  const mixedUser = makeUser({ branches, authority });
  assert.equal(canViewAnyBranchPayrollSchedule(mixedUser), false);
  assert.equal(canViewBranchPayrollSchedule(mixedUser, 7), false);
});

// ── F: branch-scoped payroll.view is branch-exact; payroll.entry alone denies ─

test('F: branch-scoped payroll.view on branch 7 grants only branch 7, and discovery', () => {
  const user = makeUser({
    authority: makeAuthority({ branch_permissions: [{ branch_id: 7, permissions: ['payroll.view'] }] }),
  });
  assert.equal(canViewAnyBranchPayrollSchedule(user), true);
  assert.equal(canViewBranchPayrollSchedule(user, 7), true);
  assert.equal(canViewBranchPayrollSchedule(user, 8), false);
});

test('F: payroll.entry-only on branch 7 grants nothing', () => {
  const user = makeUser({
    authority: makeAuthority({ branch_permissions: [{ branch_id: 7, permissions: ['payroll.entry'] }] }),
  });
  assert.equal(canViewAnyBranchPayrollSchedule(user), false);
  assert.equal(canViewBranchPayrollSchedule(user, 7), false);
});

// ── G: readiness fields referenced ────────────────────────────────────────────

test('G: page references schedule_readiness_reason, schedule_readiness_date, and readinessView(', () => {
  assert.ok(pageSource.includes('schedule_readiness_reason'));
  assert.ok(pageSource.includes('schedule_readiness_date'));
  assert.ok(pageSource.includes('readinessView('));
});

// ── H/I/J: single, exact getBranchEffectivePayrollSetup call site ───────────

test('H: exactly one call to getBranchEffectivePayrollSetup( outside the import line', () => {
  const importLine = pageSource
    .split('\n')
    .find((l) => l.includes('import') && l.includes('getBranchEffectivePayrollSetup'));
  const withoutImportBlock = importLine
    ? pageSource.replace(/import\s*\{[^}]*getBranchEffectivePayrollSetup[^}]*\}\s*from\s*'[^']*';?/s, '')
    : pageSource;
  const occurrences = withoutImportBlock.match(/getBranchEffectivePayrollSetup\(/g) ?? [];
  assert.equal(occurrences.length, 1);
});

test('I: the call site matches getBranchEffectivePayrollSetup(branchId, effectiveDate)', () => {
  const callMatch = pageSource.match(/getBranchEffectivePayrollSetup\(\s*branchId\s*,\s*effectiveDate\s*\)/);
  assert.ok(callMatch, 'expected getBranchEffectivePayrollSetup(branchId, effectiveDate)');
});

test('J: page uses effectiveRequestDate( and never gates the /effective call by direct null comparison on schedule_readiness_date', () => {
  assert.ok(pageSource.includes('effectiveRequestDate('));
  assert.ok(!/schedule_readiness_date\s*(!==?|===?)\s*null/.test(pageSource));
});

// ── K: no Date usage ───────────────────────────────────────────────────────────

test('K: page contains no Date/Intl/toLocale usage', () => {
  for (const forbidden of ['new Date', 'Date.now', 'Date.parse', 'toISOString', 'Intl.', 'toLocale']) {
    assert.ok(!pageSource.includes(forbidden), `must not contain: ${forbidden}`);
  }
});

// ── L/M: effective fields rendered ────────────────────────────────────────────

test('L/M: page renders the effective-authority fields (Unit B: friendly primary view, raw fields moved into Technical details)', () => {
  const fields = [
    'setup_id',
    'setup_code',
    'setup_name',
    'assignment_id',
    'version_id',
    'version_number',
    'config_hash',
    'period_start_date',
    'period_end_date',
    'next_boundary_date',
    'next_boundary_kind',
  ];
  for (const field of fields) {
    assert.ok(pageSource.includes(field), `expected reference to ${field}`);
  }
});

// ── N: history call and its effect deps ───────────────────────────────────────

test('N: getBranchPayrollSetupHistory(branchId) call exists with a [branchId]-only effect dependency array', () => {
  assert.ok(pageSource.includes('getBranchPayrollSetupHistory(branchId)'));
  const historyEffectMatch = pageSource.match(
    /useEffect\(\(\) => \{[\s\S]*?getBranchPayrollSetupHistory\(branchId\)[\s\S]*?\}, \[branchId\]\);/,
  );
  assert.ok(historyEffectMatch, 'expected the history effect to depend on exactly [branchId]');
});

// ── O/P/Q: annotateHistory + withdrawn + version fields rendered ───────────

test('O/P/Q (Unit B): page uses annotateHistory( and renders withdrawal_reason, version_number, config_hash; the raw withdrawn_at_utc timestamp is dropped from the friendly primary view (row.withdrawn / assignmentHistoryLine already convey "cancelled")', () => {
  assert.ok(pageSource.includes('annotateHistory('));
  assert.ok(pageSource.includes('withdrawal_reason'));
  assert.ok(pageSource.includes('version_number'));
  assert.ok(pageSource.includes('config_hash'));
});

// ── R: payrollSetupApi import boundary + no mutation names/verbs ───────────

test('R: page imports only getBranchPayrollSetupHistory and getBranchEffectivePayrollSetup from payrollSetupApi', () => {
  const importMatch = pageSource.match(/import\s*\{([^}]*)\}\s*from\s*'\.\.\/\.\.\/\.\.\/lib\/payrollSetupApi'/);
  assert.ok(importMatch, 'expected an import from lib/payrollSetupApi');
  const names = importMatch![1]
    .split(',')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
  assert.deepEqual(new Set(names), new Set(['getBranchEffectivePayrollSetup', 'getBranchPayrollSetupHistory']));

  const mutationNames = [
    'createPayrollSetup',
    'updatePayrollSetup',
    'archivePayrollSetup',
    'createPayrollSetupDraft',
    'updatePayrollSetupDraft',
    'discardPayrollSetupDraft',
    'previewPublicationImpact',
    'publishPayrollSetupDraft',
    'setDefaultPayrollSetup',
    'clearDefaultPayrollSetup',
    'assignPayrollSetup',
    'reassignPayrollSetup',
    'previewReassignmentImpact',
    'withdrawPayrollSetupAssignment',
  ];
  for (const name of mutationNames) {
    assert.ok(!pageSource.includes(name), `must not reference mutation: ${name}`);
  }
  assert.ok(!pageSource.includes('<button'));
  assert.ok(!pageSource.includes('ConfirmDialog'));
  assert.ok(!pageSource.includes('.post('));
  assert.ok(!pageSource.includes('.put('));
  assert.ok(!pageSource.includes('.patch('));
  assert.ok(!pageSource.includes('.delete('));
});

// ── S: no raw legacy endpoints / bypass wrappers ────────────────────────────

test('S: page never bypasses the lib wrappers with raw payroll-setup URLs or legacy assignment listing', () => {
  assert.ok(!pageSource.includes('listBranchPayrollSetupAssignments'));
  assert.ok(!pageSource.includes('/assignments'));
  assert.ok(!pageSource.includes('/payroll-setup/branches/'));
  assert.ok(!pageSource.includes("'/settings/payroll'"));
  assert.ok(!pageSource.includes('/settings/payroll"'));
});

// ── T/U: friendly governing/scheduled tags, and no future/upcoming/current wording ─

test('T (Unit B): the governing/scheduled tags read "In effect" / "Scheduled" (friendly words), not "Governs evaluated period" / "Scheduled transition"', () => {
  assert.ok(pageSource.includes('In effect'));
  assert.ok(!pageSource.includes('Governs evaluated period'));
  assert.ok(!pageSource.includes('Scheduled transition'));
});

test('U: page never derives "future"/"upcoming"/"current" wording in JSX text', () => {
  assert.ok(!/>[^<{]*\b(future|upcoming|current)\b/i.test(pageSource));
});

// ── Nav: AppShell wiring ──────────────────────────────────────────────────────

test('Nav: AppShell gates the schedule nav item on canViewAnyBranchPayrollSchedule and pushes into payrollItems', () => {
  const navMatch = appShellSource.match(
    /if \(canViewAnyBranchPayrollSchedule\(user\)\)\s*\n\s*payrollItems\.push\(\{ to: '\/payroll\/schedule', label: 'Payroll Schedule'/,
  );
  assert.ok(navMatch, 'expected the schedule nav item pushed into payrollItems');
  assert.ok(!appShellSource.includes("settingsItems.push({ to: '/payroll/schedule'"));
});

test('Nav: usePageTitle maps /payroll/schedule before the generic /payroll check', () => {
  const titleFnMatch = appShellSource.match(/function usePageTitle\(\)[\s\S]*?\n\}/);
  assert.ok(titleFnMatch);
  const body = titleFnMatch![0];
  const scheduleIdx = body.indexOf("startsWith('/payroll/schedule')");
  const genericIdx = body.indexOf("startsWith('/payroll'))");
  assert.ok(scheduleIdx >= 0 && genericIdx >= 0);
  assert.ok(scheduleIdx < genericIdx, 'the /payroll/schedule title check must come before the generic /payroll check');
  assert.ok(body.includes("return 'Payroll Schedule';"));
});

test('Nav: usePageIcon maps /payroll/schedule before the generic /payroll check', () => {
  const iconFnMatch = appShellSource.match(/function usePageIcon\(\)[\s\S]*?\n\}/);
  assert.ok(iconFnMatch);
  const body = iconFnMatch![0];
  const scheduleIdx = body.indexOf("startsWith('/payroll/schedule')");
  const genericIdx = body.indexOf("startsWith('/payroll'))");
  assert.ok(scheduleIdx >= 0 && genericIdx >= 0);
  assert.ok(scheduleIdx < genericIdx, 'the /payroll/schedule icon check must come before the generic /payroll check');
  assert.ok(body.includes('return <CalendarIcon />;'));
});

// ── Fix round: stale cross-branch state guard (Defect 1) + no lint-disable (Defect 2) ──

test('no eslint-disable directives anywhere in the page', () => {
  assert.ok(!pageSource.includes('eslint-disable'));
});

test('readiness and history state are tagged by branchId, and render checks the tag against the current branchId', () => {
  assert.ok(pageSource.includes('readinessState.branchId === branchId'));
  assert.ok(pageSource.includes('historyState.branchId === branchId'));
});

test('effectiveDate is derived from readinessData only, never from the raw readinessState', () => {
  assert.ok(
    /const\s+effectiveDate\s*=\s*readinessData\s*\?\s*effectiveRequestDate\(readinessData\)\s*:\s*null;/.test(
      pageSource,
    ),
  );
  assert.ok(!pageSource.includes('effectiveRequestDate(readinessState'));
});

test('the effective-authority effect depends on exactly [branchId, effectiveDate]', () => {
  const effectiveEffectMatch = pageSource.match(
    /useEffect\(\(\) => \{[\s\S]*?getBranchEffectivePayrollSetup\(branchId, effectiveDate\)[\s\S]*?\}, \[branchId, effectiveDate\]\);/,
  );
  assert.ok(effectiveEffectMatch, 'expected the effective effect to depend on exactly [branchId, effectiveDate]');
});

test('annotateHistory is called with effectiveData (never a raw/stale effective value) as its second argument', () => {
  assert.ok(
    /annotateHistory\(\s*historyData\s*,\s*effectiveData\s*,/.test(pageSource),
  );
});

test('the NOT_REQUESTED action/type does not appear anywhere in the page', () => {
  assert.ok(!pageSource.includes('NOT_REQUESTED'));
});

// ── Unit B: friendly primary view — no type="date", no raw ids/mask/hash outside Technical details ──

test('Unit B: page never uses a native type="date" input', () => {
  assert.doesNotMatch(pageSource, /type="date"/);
});

test('Unit B: "mask", config_hash, and raw id/version fragments only ever appear inside a <details> Technical-details block', () => {
  const detailsSpans: [number, number][] = [];
  const detailsRe = /<details/g;
  let m: RegExpExecArray | null;
  while ((m = detailsRe.exec(pageSource)) !== null) {
    const close = pageSource.indexOf('</details>', m.index);
    if (close !== -1) detailsSpans.push([m.index, close + '</details>'.length]);
  }
  function withinDetails(index: number): boolean {
    return detailsSpans.some(([start, end]) => index >= start && index < end);
  }

  // "mask" as a rendered word (JSX text, not the source's own `normal_days_off_mask` identifier).
  const maskRe = /normal days off mask/gi;
  let maskMatch: RegExpExecArray | null;
  while ((maskMatch = maskRe.exec(pageSource)) !== null) {
    assert.ok(withinDetails(maskMatch.index), `"mask" wording at index ${maskMatch.index} is outside Technical details`);
  }

  // Raw "#${...}" id fragments (Setup #, Assignment #, Version #).
  const idRe = /\b(Setup|Assignment|Version) #\$\{/g;
  let idMatch: RegExpExecArray | null;
  while ((idMatch = idRe.exec(pageSource)) !== null) {
    assert.ok(withinDetails(idMatch.index), `raw id fragment "${idMatch[0]}" at index ${idMatch.index} is outside Technical details`);
  }
});

test('Unit B: "Config hash" label text only appears inside a <details> Technical-details block', () => {
  const detailsSpans: [number, number][] = [];
  const detailsRe = /<details/g;
  let m: RegExpExecArray | null;
  while ((m = detailsRe.exec(pageSource)) !== null) {
    const close = pageSource.indexOf('</details>', m.index);
    if (close !== -1) detailsSpans.push([m.index, close + '</details>'.length]);
  }
  const hashRe = /Config hash/g;
  let hashMatch: RegExpExecArray | null;
  let found = 0;
  while ((hashMatch = hashRe.exec(pageSource)) !== null) {
    found += 1;
    assert.ok(
      detailsSpans.some(([start, end]) => hashMatch!.index >= start && hashMatch!.index < end),
      `"Config hash" at index ${hashMatch.index} is outside Technical details`,
    );
  }
  assert.ok(found > 0, 'expected at least one "Config hash" label (inside Technical details)');
});

test('Unit B: page uses formatIsoRange/formatIsoLong (isoDate) rather than raw ISO string concatenation for dates shown in the primary view', () => {
  assert.match(pageSource, /formatIsoRange\(/);
  assert.match(pageSource, /formatIsoLong\(/);
});

test('Unit B: page renders friendly schedule wording (periodsStartLine / fullDaysOffLine / scheduleFrequencyNoun) and upcoming-change text', () => {
  for (const name of ['periodsStartLine', 'fullDaysOffLine', 'scheduleFrequencyNoun', 'upcomingChange', 'upcomingChangeText']) {
    assert.match(pageSource, new RegExp(`\\b${name}\\(`), `expected a call to ${name}(`);
  }
});
