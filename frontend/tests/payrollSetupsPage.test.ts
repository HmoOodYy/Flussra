import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  canViewDailyPayItems,
  canViewPayrollSetups,
  canViewSettings,
} from '../src/lib/permissions.ts';
import { toUserProfile } from '../src/store/authStore.ts';
import type {
  BranchAccess,
  PermissionAuthority,
  UserInfoResponse,
  UserProfile,
} from '../src/store/authStore.ts';
import {
  archiveBlockedReason,
  assignedPolicyBranchRows,
  draftCreatedLabel,
  normalizeDescription,
  policyNextSteps,
  validateSetupCreate,
  validateSetupUpdate,
  versionRelationship,
} from '../src/pages/settings/payroll/payrollSetupsView.ts';
import type { PolicyBranchRow } from '../src/pages/settings/payroll/payrollSetupsView.ts';
import { canPublishFromPreview } from '../src/pages/settings/payroll/publishPreview.ts';
import type { PreviewInputs, StoredPreview } from '../src/pages/settings/payroll/publishPreview.ts';
import { canReassignFromPreview } from '../src/pages/settings/payroll/branchAssignmentPreview.ts';
import type { ReassignInputs, StoredReassignPreview } from '../src/pages/settings/payroll/branchAssignmentPreview.ts';
import type {
  BranchPolicySummaryResponse,
  PolicyAssignmentSummaryResponse,
  PublicationImpactResponse,
  ReassignmentImpactResponse,
  VersionResponse,
} from '../src/types/payrollSetup.ts';

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
const dashboardSource = readFileSync(new URL('../src/pages/DashboardPage.tsx', import.meta.url), 'utf8');

const PAYROLL_DIR = '../src/pages/settings/payroll/';
const pageSource = readFileSync(new URL(`${PAYROLL_DIR}PayrollSetupsPage.tsx`, import.meta.url), 'utf8');
const pageCssSource = readFileSync(new URL(`${PAYROLL_DIR}PayrollSetupsPage.module.css`, import.meta.url), 'utf8');
const detailPanelSource = readFileSync(new URL(`${PAYROLL_DIR}PolicyDetailPanel.tsx`, import.meta.url), 'utf8');
const publishPanelSource = readFileSync(new URL(`${PAYROLL_DIR}PublishPanel.tsx`, import.meta.url), 'utf8');
const draftEditorModalSource = readFileSync(new URL(`${PAYROLL_DIR}DraftEditorModal.tsx`, import.meta.url), 'utf8');
const branchAssignmentsTabSource = readFileSync(new URL(`${PAYROLL_DIR}BranchAssignmentsTab.tsx`, import.meta.url), 'utf8');
const assignPolicyModalSource = readFileSync(new URL(`${PAYROLL_DIR}AssignPolicyModal.tsx`, import.meta.url), 'utf8');
const manageBranchDrawerSource = readFileSync(new URL(`${PAYROLL_DIR}ManageBranchDrawer.tsx`, import.meta.url), 'utf8');
const viewSource = readFileSync(new URL(`${PAYROLL_DIR}payrollSetupsView.ts`, import.meta.url), 'utf8');
const draftEditorSource = readFileSync(new URL(`${PAYROLL_DIR}draftEditor.ts`, import.meta.url), 'utf8');
const publishPreviewSource = readFileSync(new URL(`${PAYROLL_DIR}publishPreview.ts`, import.meta.url), 'utf8');
const branchAssignmentPreviewSource = readFileSync(new URL(`${PAYROLL_DIR}branchAssignmentPreview.ts`, import.meta.url), 'utf8');
const branchPolicyViewSource = readFileSync(new URL(`${PAYROLL_DIR}branchPolicyView.ts`, import.meta.url), 'utf8');

const ALL_PAYROLL_TSX = [
  ['PayrollSetupsPage.tsx', pageSource],
  ['PolicyDetailPanel.tsx', detailPanelSource],
  ['PublishPanel.tsx', publishPanelSource],
  ['DraftEditorModal.tsx', draftEditorModalSource],
  ['BranchAssignmentsTab.tsx', branchAssignmentsTabSource],
  ['AssignPolicyModal.tsx', assignPolicyModalSource],
  ['ManageBranchDrawer.tsx', manageBranchDrawerSource],
] as const;

// ── A/B. App.tsx payroll route wiring (unchanged file) ─────────────────────

test('App.tsx: payroll route renders PayrollSetupsPage inside Gate(canViewPayrollSetups) inside LegacyStatusKeysRedirect', () => {
  const routeRe =
    /<Route\s+path=["']payroll["']\s+element=\{\s*<LegacyStatusKeysRedirect>\s*<Gate\s+check=\{canViewPayrollSetups\}\s*>\s*<PayrollSetupsPage\s*\/>\s*<\/Gate>\s*<\/LegacyStatusKeysRedirect>\s*\}\s*\/>/;
  assert.match(appSource, routeRe);
});

test('App.tsx: the payroll route gate is canViewPayrollSetups, not canManageSettingsAdmin', () => {
  const idx = appSource.indexOf('path="payroll"');
  assert.notEqual(idx, -1, 'payroll route not found');
  const routeBlock = appSource.slice(idx, appSource.indexOf('/>', idx) + 2);
  assert.match(routeBlock, /canViewPayrollSetups/);
  assert.doesNotMatch(routeBlock, /canManageSettingsAdmin/);
});

// ── C. Authority ─────────────────────────────────────────────────────────────

test('canViewPayrollSetups: company-wide payroll_setup.view (AllCompanyBranches) grants view, but not canViewSettings/canViewDailyPayItems', () => {
  const user = makeUser({
    branches: [makeBranch({ branch_id: null, scope: 'AllCompanyBranches' })],
    authority: makeAuthority({ company_permissions: ['payroll_setup.view'] }),
  });
  assert.equal(canViewPayrollSetups(user), true);
  assert.equal(canViewSettings(user), false);
  assert.equal(canViewDailyPayItems(user), false);
});

test('App.tsx: outer /settings gate expression includes canViewPayrollSetups(u)', () => {
  const gateRe =
    /Gate\s+check=\{\(u\)\s*=>\s*canViewSettings\(u\)\s*\|\|\s*canViewDailyPayItems\(u\)\s*\|\|\s*canViewPayrollSetups\(u\)\s*\|\|\s*canCreateBranches\(u\)\}/;
  assert.match(appSource, gateRe);
});

test('App.tsx: SettingsDefaultRedirect has the canViewPayrollSetups -> /settings/payroll step after canViewSettings', () => {
  const idx = appSource.indexOf('function SettingsDefaultRedirect');
  assert.notEqual(idx, -1, 'SettingsDefaultRedirect not found');
  const end = appSource.indexOf('\n}', idx);
  const body = appSource.slice(idx, end);
  const settingsIdx = body.indexOf('canViewSettings(user)');
  const payrollIdx = body.indexOf('canViewPayrollSetups(user)');
  assert.notEqual(settingsIdx, -1);
  assert.notEqual(payrollIdx, -1);
  assert.ok(payrollIdx > settingsIdx, 'canViewPayrollSetups step must come after canViewSettings step');
  assert.match(body, /canViewPayrollSetups\(user\)\)\s*return\s*<Navigate to="\/settings\/payroll" replace \/>/);
});

// ── D. status-keys route unchanged ──────────────────────────────────────────

test('App.tsx: status-keys route is still gated by canManageSettingsAdmin and renders StatusKeysPage', () => {
  const routeRe = /<Route\s+path=["']status-keys["']\s+element=\{<Gate\s+check=\{canManageSettingsAdmin\}\s*>\s*<StatusKeysPage\s*\/>\s*<\/Gate>\}\s*\/>/;
  assert.match(appSource, routeRe);
});

// ── F. DashboardPage.tsx quick action (Unit A review: label text now aligned) ─

test('DashboardPage.tsx: /settings/payroll quick action is guarded by canViewPayrollSetups and labelled "Payroll Policies"', () => {
  const idx = dashboardSource.indexOf("to: '/settings/payroll'");
  assert.notEqual(idx, -1, '/settings/payroll quick action not found');
  const windowStart = Math.max(0, idx - 300);
  const block = dashboardSource.slice(windowStart, idx + 100);
  assert.match(block, /canViewPayrollSetups\(user\)/);
  assert.match(block, /label:\s*'Payroll Policies'/);
});

test('DashboardPage.tsx: no longer labels the payroll quick action "Payroll Setups"', () => {
  assert.doesNotMatch(dashboardSource, /label:\s*'Payroll Setups'/);
});

test('DashboardPage.tsx: no longer guards the payroll quick action with canManageSettingsAdmin', () => {
  assert.doesNotMatch(dashboardSource, /canManageSettingsAdmin/);
});

// ── I. Clear/Set Default confirm copy (Unit A terminology pass; moved to PolicyDetailPanel.tsx in the master-detail Step 1 split) ─

test('PolicyDetailPanel.tsx: calls clearDefaultPayrollSetup and has the exact Clear Default confirm copy', () => {
  assert.match(detailPanelSource, /\bclearDefaultPayrollSetup\(\)/);
  assert.match(detailPanelSource, /title="Clear company default\?"/);
  assert.match(
    detailPanelSource,
    /message="New branches will no longer have a default payroll policy available for automatic onboarding until another default is selected\. Existing branch assignments are unchanged\."/,
  );
});

test('PolicyDetailPanel.tsx: has the exact Set Company Default confirm copy (onboarding-only, non-retroactive)', () => {
  assert.match(detailPanelSource, /title="Set company default\?"/);
  assert.match(
    detailPanelSource,
    /message="Affects only branches created later; existing branch assignments never change\."/,
  );
});

test('PayrollSetupsPage.tsx (shell): no longer calls clearDefaultPayrollSetup or renders the default/archive confirm dialogs (moved to PolicyDetailPanel.tsx)', () => {
  assert.doesNotMatch(pageSource, /clearDefaultPayrollSetup/);
  assert.doesNotMatch(pageSource, /title="Clear company default\?"/);
  assert.doesNotMatch(pageSource, /title="Set company default\?"/);
  assert.doesNotMatch(pageSource, /title="Archive payroll policy\?"/);
});

// ── J. No wall-clock usage anywhere in the payroll feature ─────────────────

test('No new Date(/Date.now/Date.UTC/Date.parse usage anywhere in the payroll feature files', () => {
  const allSources: [string, string][] = [
    ...ALL_PAYROLL_TSX,
    ['payrollSetupsView.ts', viewSource],
    ['draftEditor.ts', draftEditorSource],
    ['publishPreview.ts', publishPreviewSource],
    ['branchAssignmentPreview.ts', branchAssignmentPreviewSource],
    ['branchPolicyView.ts', branchPolicyViewSource],
  ];
  for (const [name, source] of allSources) {
    assert.doesNotMatch(source, /\bnew Date\(/, `${name} must not use new Date(`);
    assert.doesNotMatch(source, /\bDate\.(now|UTC|parse)\(/, `${name} must not use Date.${name}`);
  }
});

test('PayrollSetupsPage.tsx: no invented "Effective today" / "Current version" style labels', () => {
  assert.doesNotMatch(pageSource, /Effective today/);
  assert.doesNotMatch(pageSource, /Current version/);
});

// ── K/P. U5c branch-assignment wrappers now live in the extracted modals ──

test('AssignPolicyModal.tsx references assignPayrollSetup; never previews before initial assignment', () => {
  assert.match(assignPolicyModalSource, /\bassignPayrollSetup\(/);
  assert.doesNotMatch(assignPolicyModalSource, /previewReassignmentImpact|previewPublicationImpact/);
});

test('ManageBranchDrawer.tsx references reassignPayrollSetup, previewReassignmentImpact, and withdrawPayrollSetupAssignment', () => {
  for (const name of ['reassignPayrollSetup', 'previewReassignmentImpact', 'withdrawPayrollSetupAssignment']) {
    assert.match(manageBranchDrawerSource, new RegExp(`\\b${name}\\b`), `must reference ${name}`);
  }
});

test('PayrollSetupsPage.tsx (shell): does not itself call any of the four U5c branch-assignment wrappers (moved out)', () => {
  for (const name of ['assignPayrollSetup', 'reassignPayrollSetup', 'previewReassignmentImpact', 'withdrawPayrollSetupAssignment']) {
    assert.doesNotMatch(pageSource, new RegExp(`\\b${name}\\b`), `shell must not reference ${name}`);
  }
});

test('PayrollSetupsPage.tsx: does not reach ahead into U6 branch-schedule wrappers or route', () => {
  const forbiddenU6 = ['getBranchPayrollSetupHistory', 'getBranchEffectivePayrollSetup'];
  for (const name of forbiddenU6) {
    assert.doesNotMatch(pageSource, new RegExp(`\\b${name}\\b`), `must not reference ${name}`);
  }
  assert.doesNotMatch(pageSource, /\/payroll\/schedule/);
});

test('PolicyDetailPanel.tsx references the U5b Draft/Publish wrappers (drafts moved out of the shell in the master-detail Step 1 split)', () => {
  const expected = ['listPayrollSetupDrafts', 'discardPayrollSetupDraft'];
  for (const name of expected) {
    assert.match(detailPanelSource, new RegExp(`\\b${name}\\b`), `must reference ${name}`);
  }
});

test('PayrollSetupsPage.tsx (shell): no longer references the drafts/versions wrappers (moved to PolicyDetailPanel.tsx)', () => {
  for (const name of ['listPayrollSetupDrafts', 'discardPayrollSetupDraft', 'listPayrollSetupVersions', 'createPayrollSetupDraft', 'updatePayrollSetupDraft']) {
    assert.doesNotMatch(pageSource, new RegExp(`\\b${name}\\b`), `shell must not reference ${name}`);
  }
});

test('PublishPanel.tsx references previewPublicationImpact and publishPayrollSetupDraft', () => {
  assert.match(publishPanelSource, /\bpreviewPublicationImpact\(/);
  assert.match(publishPanelSource, /\bpublishPayrollSetupDraft\(/);
});

test('DraftEditorModal.tsx references createPayrollSetupDraft and updatePayrollSetupDraft', () => {
  assert.match(draftEditorModalSource, /\bcreatePayrollSetupDraft\(/);
  assert.match(draftEditorModalSource, /\bupdatePayrollSetupDraft\(/);
});

// ── L. AppShell.tsx nav/title wiring (Unit A: "Payroll Setups" -> "Payroll Policies") ─

test('AppShell.tsx: Payroll Policies nav item is conditioned on canViewPayrollSetups(user)', () => {
  const idx = appShellSource.indexOf("'/settings/payroll'");
  assert.notEqual(idx, -1, 'Payroll Policies nav item not found');
  const windowStart = Math.max(0, idx - 200);
  const surrounding = appShellSource.slice(windowStart, idx + 200);
  assert.match(surrounding, /canViewPayrollSetups\(user\)/);
  assert.match(appShellSource, /label:\s*'Payroll Policies'/);
});

test('AppShell.tsx: no longer labels the payroll nav item "Payroll Setups"', () => {
  assert.doesNotMatch(appShellSource, /label:\s*'Payroll Setups'/);
});

test('AppShell.tsx: Status Keys nav item is still conditioned on canManageSettingsAdmin(user)', () => {
  const idx = appShellSource.indexOf("'/settings/status-keys'");
  assert.notEqual(idx, -1, 'Status Keys nav item not found');
  const windowStart = Math.max(0, idx - 200);
  const surrounding = appShellSource.slice(windowStart, idx + 200);
  assert.match(surrounding, /canManageSettingsAdmin\(user\)/);
});

test('AppShell.tsx: usePageTitle maps /settings/payroll to "Payroll Policies"', () => {
  const re = /pathname\.startsWith\(['"]\/settings\/payroll['"]\)\)\s*return\s*'Payroll Policies'/;
  assert.match(appShellSource, re);
});

// ── M. Archive error stays tied to the policy it happened on (moved to PolicyDetailPanel.tsx) ──

test('PolicyDetailPanel.tsx: renders the archive PayrollErrorNotice only when actionError.setupId matches the selected policy', () => {
  assert.match(
    detailPanelSource,
    /actionError\?\.kind === 'archive'\s*&&\s*actionError\.setupId === selectedPolicyId/,
  );
});

test('PolicyDetailPanel.tsx: the Policy updates table has no onClick / mutation calls (read-only)', () => {
  const anchorIdx = detailPanelSource.indexOf('title="Policy updates"');
  assert.notEqual(anchorIdx, -1, 'Policy updates SectionCard not found');
  const cardEnd = detailPanelSource.indexOf('</SectionCard>', anchorIdx);
  assert.notEqual(cardEnd, -1);
  const block = detailPanelSource.slice(anchorIdx, cardEnd);
  assert.doesNotMatch(block, /onClick/);
  assert.doesNotMatch(
    block,
    /updatePayrollSetupDraft|discardPayrollSetupDraft|publishPayrollSetupDraft|createPayrollSetupDraft/,
  );
});

test('PayrollSetupsPage.tsx (shell): does not render a "Policy updates" SectionCard nor reference actionError (moved to PolicyDetailPanel.tsx)', () => {
  assert.doesNotMatch(pageSource, /title="Policy updates"/);
  assert.doesNotMatch(pageSource, /actionError/);
});

// ── N. Draft/Publish action buttons only render for an Active policy (moved to PolicyDetailPanel.tsx) ──

test("PolicyDetailPanel.tsx: New Draft button is gated on selectedPolicy.status === 'Active' && canManage", () => {
  assert.match(detailPanelSource, /selectedPolicy\.status === 'Active' && canManage \?/);
});

test("PolicyDetailPanel.tsx: the Publish panel only renders for selectedPolicy.status === 'Active' && canPublish", () => {
  assert.match(
    detailPanelSource,
    /selectedPolicy && selectedPolicy\.status === 'Active' && canPublish && publishDraft && currentPublishDraft/,
  );
});

test('PayrollSetupsPage.tsx (shell): renders no "Publish…" draft button and no "Add draft schedule" action (moved to PolicyDetailPanel.tsx)', () => {
  assert.doesNotMatch(pageSource, /Publish…/);
  assert.doesNotMatch(pageSource, /Add draft schedule/);
});

// ── Q. Publish controls guarded by canPublish; manage never guards a publish button ──

test('PolicyDetailPanel.tsx: declares a canPublish prop (permission computation now lives wherever this panel is eventually mounted)', () => {
  assert.match(detailPanelSource, /canPublish:\s*boolean;/);
});

test('PolicyDetailPanel.tsx: the "Publish…" draft-row button is nearest-guarded by canPublish &&, never canManage &&', () => {
  const idx = detailPanelSource.indexOf('Publish…');
  assert.notEqual(idx, -1, 'Publish… button text not found');
  const preceding = detailPanelSource.slice(Math.max(0, idx - 500), idx);
  const lastCanPublishGuard = preceding.lastIndexOf('canPublish &&');
  const lastCanManageGuard = preceding.lastIndexOf('canManage &&');
  assert.ok(lastCanPublishGuard !== -1, 'no canPublish && guard found before the Publish… button');
  assert.ok(lastCanPublishGuard > lastCanManageGuard, 'Publish… button is nearer-guarded by canManage than canPublish');
});

test('PublishPanel.tsx: never itself references canManage/canManageSettingsAdmin (trusts the shell\'s canPublish gate)', () => {
  assert.doesNotMatch(publishPanelSource, /canManage|canManageSettingsAdmin/);
});

// ── R (U5c). Every branch-assignment mutation control is guarded by canAssign ──

test('BranchAssignmentsTab.tsx: the Action column and its buttons are guarded by canAssign', () => {
  assert.match(branchAssignmentsTabSource, /\{canAssign && <th>Action<\/th>\}/);
  assert.match(branchAssignmentsTabSource, /\{canAssign && \(/);
});

test('PayrollSetupsPage.tsx: policy creation keeps refresh/selection behavior without a layout-shifting success banner', () => {
  assert.doesNotMatch(pageSource, /<div className=\{styles\.successAlert\}>/);
  assert.doesNotMatch(pageSource, /const \[toast, setToast\]/);
  assert.match(pageSource, /await loadSetups\(\)/);
  assert.match(pageSource, /setSearchParams\(\{ setupId: String\(created\.setup_id\) \}/);
});

test('Policy detail header wraps its helper content instead of forcing horizontal overflow', () => {
  assert.match(pageCssSource, /\.detailPaneInner\s*\{[\s\S]*?min-width:\s*0;/);
  assert.match(pageCssSource, /\.policyHeaderTop\s*\{[\s\S]*?min-width:\s*0;/);
  assert.match(pageCssSource, /\.policyActionHint\s*\{[\s\S]*?white-space:\s*normal;/);
  assert.match(pageCssSource, /\.policyActionHint\s*\{[\s\S]*?overflow-wrap:\s*anywhere;/);
});

test('ManageBranchDrawer closes after a successful scheduled-assignment cancellation', () => {
  const mutationEnd = manageBranchDrawerSource.indexOf('afterMutation();', manageBranchDrawerSource.indexOf('withdrawPayrollSetupAssignment'));
  assert.notEqual(mutationEnd, -1);
  assert.match(manageBranchDrawerSource.slice(mutationEnd, mutationEnd + 80), /onClose\(\)/);
});

test('BranchAssignmentsTab.tsx: AssignPolicyModal and ManageBranchDrawer only render when canAssign', () => {
  assert.match(branchAssignmentsTabSource, /\{canAssign && assignTarget && \(/);
  assert.match(branchAssignmentsTabSource, /\{canAssign && manageTarget && \(/);
});

test('BranchAssignmentsTab.tsx: never references canManage/canPublish for its own gating', () => {
  assert.doesNotMatch(branchAssignmentsTabSource, /canManage|canManageSettingsAdmin|canPublish/);
});

test('PayrollSetupsPage.tsx: does not use the setup.manage literal or canManageSettingsAdmin', () => {
  assert.doesNotMatch(pageSource, /setup\.manage/);
  assert.doesNotMatch(pageSource, /canManageSettingsAdmin/);
});

test('PayrollSetupsPage.tsx: imports canAssignPayrollSetups from lib/permissions', () => {
  assert.match(pageSource, /canAssignPayrollSetups/);
});

// ── Branch Assignments: never shows "Reassign…" for an unassigned branch ──

// User-facing "Reassign" text (a button/dialog label reading exactly
// "Reassign", "Reassign…", "Reassign Branch", etc.) — not the substring
// "Reassign" inside prose like "Reassignment preview-first stale safety" or
// internal identifiers (reassignPayrollSetup, confirmReassign, ...), which
// legitimately keep mirroring the backend/API naming.
const USER_FACING_REASSIGN_TEXT = /(>\s*Reassign(?!ment)[^<]*<|title="Reassign(?!ment)|"Reassign(?!ment)[^"]*"\s*confirmLabel)/;

test('BranchAssignmentsTab.tsx: never renders user-facing "Reassign" text — action labels come from BRANCH_POLICY_ACTION_LABEL', () => {
  assert.doesNotMatch(branchAssignmentsTabSource, USER_FACING_REASSIGN_TEXT);
});

test('ManageBranchDrawer.tsx: the mutation is called "Change policy", never rendered as "Reassign"', () => {
  assert.doesNotMatch(manageBranchDrawerSource, USER_FACING_REASSIGN_TEXT);
  assert.match(manageBranchDrawerSource, /Change policy/);
});

// ── S (U5c). No withdrawal-eligibility logic reconstructed client-side ────

test('ManageBranchDrawer.tsx: does not reference PERIOD_HISTORY_CONFLICT (server-only eligibility)', () => {
  assert.doesNotMatch(manageBranchDrawerSource, /PERIOD_HISTORY_CONFLICT/);
});

test('ManageBranchDrawer.tsx: cancel-scheduled-change confirm copy never claims safety or that payroll is unaffected', () => {
  assert.doesNotMatch(manageBranchDrawerSource, /safe to (cancel|withdraw)/i);
  assert.doesNotMatch(manageBranchDrawerSource, /payroll is unaffected/i);
  assert.doesNotMatch(manageBranchDrawerSource, /not affected/i);
});

// ── T. PolicyErrorNotice is gone everywhere; PayrollErrorNotice used instead ──

test('The local PolicyErrorNotice component no longer exists anywhere in the payroll feature', () => {
  for (const [name, source] of ALL_PAYROLL_TSX) {
    assert.doesNotMatch(source, /function PolicyErrorNotice/, `${name} must not define PolicyErrorNotice`);
    assert.doesNotMatch(source, /<PolicyErrorNotice/, `${name} must not render PolicyErrorNotice`);
  }
});

test('Every payroll feature .tsx file that shows an error uses <PayrollErrorNotice>', () => {
  for (const [name, source] of ALL_PAYROLL_TSX) {
    if (/ApiErrorInfo/.test(source)) {
      assert.match(source, /<PayrollErrorNotice/, `${name} handles ApiErrorInfo but never renders <PayrollErrorNotice>`);
    }
  }
});

// ── Static: no type="date", no "Preview impact" button, no raw ids outside Technical details ──

test('No file under src/pages/settings/payroll uses a native type="date" input', () => {
  for (const [name, source] of ALL_PAYROLL_TSX) {
    assert.doesNotMatch(source, /type="date"/, `${name} must not use a native date input`);
  }
});

test('No file under src/pages/settings/payroll renders a "Preview impact" button (superseded by automatic preview)', () => {
  for (const [name, source] of ALL_PAYROLL_TSX) {
    assert.doesNotMatch(source, /Preview impact/, `${name} must not render a manual "Preview impact" button`);
  }
});

test('No file renders config_hash at all (dropped from primary UI; not useful enough to keep behind Technical details)', () => {
  for (const [name, source] of ALL_PAYROLL_TSX) {
    assert.doesNotMatch(source, /config_hash/, `${name} must not reference config_hash`);
  }
});

test('Raw version/setup/assignment/draft id fragments ("Version #", "Assignment #", "Policy #", "Draft #") only ever appear inside a <details> Technical-details block', () => {
  // Branch id fallbacks ("Branch #${id}") are a pre-existing, sanctioned
  // fallback idiom elsewhere in this codebase and are not in the
  // coordinator's restricted list (version/setup/assignment/draft ids) —
  // only those four are checked here.
  const restrictedIdLabel = /\b(Version|Setup|Assignment|Draft|Policy) #\$\{/g;
  for (const [name, source] of ALL_PAYROLL_TSX) {
    const detailsSpans: [number, number][] = [];
    const detailsRe = /<details/g;
    let m: RegExpExecArray | null;
    while ((m = detailsRe.exec(source)) !== null) {
      const close = source.indexOf('</details>', m.index);
      if (close !== -1) detailsSpans.push([m.index, close + '</details>'.length]);
    }
    let idMatch: RegExpExecArray | null;
    while ((idMatch = restrictedIdLabel.exec(source)) !== null) {
      const withinDetails = detailsSpans.some(([start, end]) => idMatch!.index >= start && idMatch!.index < end);
      assert.ok(withinDetails, `${name}: a raw id fragment "${idMatch[0]}" at index ${idMatch.index} is outside any <details> block`);
    }
  }
});

// ── U. Opus-review-style fixes, re-targeted at their new home (PublishPanel.tsx) ──

test('PublishPanel.tsx: changing the date always resets replacesVersionId to null', () => {
  const startIdx = publishPanelSource.indexOf('function handleDateChange');
  assert.notEqual(startIdx, -1, 'handleDateChange not found');
  const endIdx = publishPanelSource.indexOf('\n  }', startIdx);
  const body = publishPanelSource.slice(startIdx, endIdx);
  assert.match(body, /setEffectiveFromDate\(iso\)/);
  assert.match(body, /setReplacesVersionId\(null\)/);
});

test('PublishPanel.tsx: setReplacesVersionId is only ever called with null or a ternary between replaceCandidateVersionId and null', () => {
  const calls = [...publishPanelSource.matchAll(/setReplacesVersionId\(([^)]*)\)/g)].map((m) => m[1].trim());
  assert.ok(calls.length >= 2, `expected at least 2 setReplacesVersionId(...) calls, found ${calls.length}`);
  const allowed = ['null', 'e.target.checked ? replaceCandidateVersionId : null'];
  for (const arg of calls) {
    assert.ok(allowed.includes(arg), `setReplacesVersionId called with unexpected argument: "${arg}"`);
  }
});

test('PublishPanel.tsx: the "Replace Version N" checkbox is driven by the live boundary choice, not a separately-tracked stale candidate', () => {
  // Unit A supersedes the old "stuck correction checkbox" contract: the
  // checkbox's visibility now comes from requestedChoice (the boundary
  // choice for the CURRENTLY selected date), which is always in sync with
  // the date because changing the date clears replacesVersionId and
  // re-fetches choices for the new date. There is no separate
  // correctionCandidateId to go stale.
  assert.match(publishPanelSource, /replaceCandidateVersionNumber != null/);
  assert.match(publishPanelSource, /requestedChoice\?\.\s*replaces_version_number/);
});

test('PolicyDetailPanel.tsx: confirmDiscardDraft closes the publish panel when the discarded draft is the one open (moved from the shell)', () => {
  assert.match(detailPanelSource, /openPublishDraftId\s*=\s*publishDraft\?\.version_id\s*\?\?\s*null/);
  const startIdx = detailPanelSource.indexOf('async function confirmDiscardDraft');
  assert.notEqual(startIdx, -1, 'confirmDiscardDraft not found');
  const endIdx = detailPanelSource.indexOf('\n  }', startIdx);
  const body = detailPanelSource.slice(startIdx, endIdx);
  assert.match(body, /openPublishDraftId === targetDraftId/);
  assert.match(body, /if \(publishPanelOpenOnThisDraft\)\s*\{\s*setPublishDraft\(null\);/);
});

test('PayrollSetupsPage.tsx (shell): no longer defines confirmDiscardDraft (moved to PolicyDetailPanel.tsx)', () => {
  assert.doesNotMatch(pageSource, /confirmDiscardDraft/);
});

// ═══════════════════════════════════════════════════════════════════════════
// V. Unit A review: stale-preview safety (useAutoPreview snapshot fix)
// ═══════════════════════════════════════════════════════════════════════════
//
// useAutoPreview.stored.inputs must be an independent snapshot of the
// inputs at request time, not a re-stamp of the caller's current inputs —
// otherwise canPublishFromPreview(currentInputs, stored)'s comparison is
// tautological (current vs. itself) and can never detect staleness. These
// tests exercise exactly the bug scenario: a `stored` whose inputs differ
// from the live current inputs must never be publishable/reassignable, and
// the two extracted components must always execute stored.inputs, never
// the live current-inputs object.

function makePublishResponse(overrides: Partial<PublicationImpactResponse> = {}): PublicationImpactResponse {
  return {
    setup_id: 1,
    affected_branch_ids: [],
    effective_date: '2026-09-23',
    predecessor_version_id: null,
    predecessor_hash: null,
    current_same_date_version_id: null,
    successor_hash: 'hash',
    successor_schedule: {
      payroll_frequency: 'Week',
      anchor_start_date: '2026-09-23',
      custom_interval_days: null,
      normal_days_off_mask: 65,
    },
    next_version_boundary: null,
    conflicts: [],
    allowed: true,
    ...overrides,
  };
}

function makePreviewInputs(overrides: Partial<PreviewInputs> = {}): PreviewInputs {
  return {
    setupId: 1,
    draftId: 50,
    draftScheduleKey: '["Week","2026-09-23",null,65]',
    effectiveFromDate: '2026-09-23',
    replacesVersionId: null,
    ...overrides,
  };
}

test('V1. canPublishFromPreview: a stored preview snapshotted for an OLDER date is never publishable once the current date has moved on', () => {
  const stored: StoredPreview = {
    inputs: makePreviewInputs({ effectiveFromDate: '2026-09-23' }),
    response: makePublishResponse({ allowed: true }),
  };
  const currentInputs = makePreviewInputs({ effectiveFromDate: '2026-09-30' });
  assert.equal(canPublishFromPreview(currentInputs, stored), false);
});

function makeReassignResponse(overrides: Partial<ReassignmentImpactResponse> = {}): ReassignmentImpactResponse {
  return {
    branch_id: 10,
    source_setup_id: 1,
    destination_setup_id: 2,
    predecessor_version_id: 100,
    successor_version_id: 200,
    effective_date: '2026-02-01',
    conflicts: [],
    allowed: true,
    ...overrides,
  };
}

function makeReassignInputs(overrides: Partial<ReassignInputs> = {}): ReassignInputs {
  return { branchId: 10, destinationSetupId: 2, effectiveFromDate: '2026-02-01', ...overrides };
}

test('V2. canReassignFromPreview: a stored preview snapshotted for a DIFFERENT destination policy is never usable once the selection has moved on', () => {
  const stored: StoredReassignPreview = {
    inputs: makeReassignInputs({ destinationSetupId: 2 }),
    response: makeReassignResponse({ allowed: true }),
  };
  const currentInputs = makeReassignInputs({ destinationSetupId: 3 });
  assert.equal(canReassignFromPreview(currentInputs, stored), false);
});

test('PublishPanel.tsx: buildPublishRequest is always called with storedPreview.inputs, never currentPreviewInputs', () => {
  const calls = [...publishPanelSource.matchAll(/buildPublishRequest\(([^)]*)\)/g)].map((m) => m[1].trim());
  assert.ok(calls.length >= 1, 'expected at least one buildPublishRequest(...) call');
  for (const arg of calls) {
    assert.notEqual(arg, 'currentPreviewInputs', `buildPublishRequest must not be called with currentPreviewInputs (got "${arg}")`);
    assert.match(arg, /storedPreview\.inputs/, `buildPublishRequest argument should be storedPreview.inputs (got "${arg}")`);
  }
});

test('ManageBranchDrawer.tsx: buildReassignmentRequest is always called with storedReassignPreview.inputs, never currentReassignInputs', () => {
  const calls = [...manageBranchDrawerSource.matchAll(/buildReassignmentRequest\(([^,]*),/g)].map((m) => m[1].trim());
  assert.ok(calls.length >= 1, 'expected at least one buildReassignmentRequest(...) call');
  for (const arg of calls) {
    assert.notEqual(arg, 'currentReassignInputs', `buildReassignmentRequest must not be called with currentReassignInputs (got "${arg}")`);
    assert.match(arg, /storedReassignPreview\.inputs/, `buildReassignmentRequest first argument should be storedReassignPreview.inputs (got "${arg}")`);
  }
});

test('PublishPanel.tsx / ManageBranchDrawer.tsx: the auto-preview fetcher never closes over currentPreviewInputs/currentReassignInputs — it must take the snapshot the hook hands it', () => {
  // The fetcher passed to useAutoPreview must be parameterized (receives
  // its own inputs argument), not a zero-arg closure baked with the live
  // current-inputs object — otherwise the "snapshot" is fake.
  assert.doesNotMatch(publishPanelSource, /previewPublicationImpact\([^)]*currentPreviewInputs[^)]*\)/);
  assert.doesNotMatch(manageBranchDrawerSource, /previewReassignmentImpact\([^)]*currentReassignInputs[^)]*\)/);
});

// ═══════════════════════════════════════════════════════════════════════════
// W. Master-detail redesign, Step 1: layout shell + PolicyDetailPanel split
// ═══════════════════════════════════════════════════════════════════════════

test('W1. Both page tabs ("Policies", "Branch Assignments") are still rendered, unrenamed', () => {
  assert.match(pageSource, />\s*Policies\s*<\/button>/);
  assert.match(pageSource, />\s*Branch Assignments\s*<\/button>/);
});

test('W2 (Step 2). The master panel maps over `visible` (visiblePolicies output — filter+search derived), not a hardcoded Active-only filter', () => {
  const listIdx = pageSource.indexOf('className={styles.setupList}');
  assert.notEqual(listIdx, -1, 'setupList <ul> not found');
  const mapIdx = pageSource.indexOf('visible.map(', listIdx);
  assert.notEqual(mapIdx, -1, 'expected {visible.map(...)} directly inside the setupList <ul>');
  const between = pageSource.slice(listIdx, mapIdx);
  assert.doesNotMatch(between, /\.filter\(/, 'the master list must not additionally filter inline before mapping');
  assert.match(pageSource, /visiblePolicies\(setups, usage, filter, search\)/);
});

test('W3. Clicking a master row calls selectPolicy(s.setup_id), which updates the ?setupId-backed selection via setSearchParams', () => {
  assert.match(pageSource, /onClick=\{\(\)\s*=>\s*selectPolicy\(s\.setup_id\)\}/);
  const startIdx = pageSource.indexOf('function selectPolicy');
  assert.notEqual(startIdx, -1, 'selectPolicy not found');
  const endIdx = pageSource.indexOf('\n  }', startIdx);
  const body = pageSource.slice(startIdx, endIdx);
  assert.match(body, /setSearchParams\(\{\s*setupId:\s*String\(id\)\s*\}/);
});

test('W4. selectedPolicyId is the renamed derived selection value (no selectedSetupId remains in the shell)', () => {
  assert.match(pageSource, /selectedPolicyId/);
  assert.doesNotMatch(pageSource, /selectedSetupId/);
});

test('W5. The selected master row gets the active class and aria-current="true"; unselected rows get neither', () => {
  assert.match(pageSource, /isSelected \? `\$\{styles\.setupRow\} \$\{styles\.setupRowActive\}` : styles\.setupRow/);
  assert.match(pageSource, /aria-current=\{isSelected \? 'true' : undefined\}/);
});

test('W6. The master panel shows the derived usage badge (Active/Inactive/Archived, never raw setup.status) and a Default badge', () => {
  assert.match(pageSource, /styles\.badge\}\s*\$\{styles\.badgeGreen\}/);
  assert.match(pageSource, /styles\.badge\}\s*\$\{styles\.badgeGray\}/);
  assert.match(pageSource, /styles\.badge\}\s*\$\{styles\.badgeBlue\}/);
  assert.match(pageSource, /styles\.badge\}\s*\$\{styles\.badgeMuted\}/);
  assert.match(pageSource, />Default</);
  // The badge text/branch comes from the derived `state`, never `s.status`.
  assert.doesNotMatch(pageSource, /\{s\.status\}/);
});

test('W7 (Step 3A). The detail column is a <section> (data-testid="policy-detail-panel", styled as the detail pane) that mounts PolicyDetailPanel for a selected policy and otherwise shows a minimal empty state', () => {
  const sectionMatch = pageSource.match(
    /<section\s+className=\{styles\.setupsDetailPane\}\s+aria-label="Policy details"\s+data-testid="policy-detail-panel"\s*>([\s\S]*?)<\/section>/,
  );
  assert.ok(sectionMatch, 'expected the detail <section> to have children');
  const inner = sectionMatch[1];
  assert.match(inner, /selectedPolicy \?/);
  assert.match(inner, /<PolicyDetailPanel/);
  assert.match(inner, /Select a payroll policy/);
  assert.match(inner, /Choose a policy on the left to view its details\./);
});

test('W8. The detail <section> renders inside the same two-column .setupsBody grid as the master list', () => {
  const bodyIdx = pageSource.indexOf('styles.setupsBody');
  assert.notEqual(bodyIdx, -1);
  const sectionIdx = pageSource.indexOf('data-testid="policy-detail-panel"', bodyIdx);
  assert.notEqual(sectionIdx, -1, 'the blank detail section must be inside .setupsBody');
});

test('W7b (full-height split view). .setupsBody uses align-items: stretch; .setupsListCard is a flex column that itself hides overflow, and .setupListScroll (Step 2) is the actual scroll region', () => {
  const bodyRuleMatch = pageCssSource.match(/\.setupsBody\s*\{[^}]*\}/);
  assert.ok(bodyRuleMatch, '.setupsBody rule not found');
  assert.match(bodyRuleMatch![0], /align-items:\s*stretch/);

  const listCardRuleMatch = pageCssSource.match(/\.setupsListCard\s*\{[^}]*\}/);
  assert.ok(listCardRuleMatch, '.setupsListCard rule not found');
  assert.match(listCardRuleMatch![0], /display:\s*flex/);
  assert.match(listCardRuleMatch![0], /flex-direction:\s*column/);
  assert.match(listCardRuleMatch![0], /min-height:\s*0/);
  assert.match(listCardRuleMatch![0], /overflow:\s*hidden/);

  const listScrollRuleMatch = pageCssSource.match(/\.setupListScroll\s*\{[^}]*\}/);
  assert.ok(listScrollRuleMatch, '.setupListScroll rule not found');
  assert.match(listScrollRuleMatch![0], /flex:\s*1/);
  assert.match(listScrollRuleMatch![0], /min-height:\s*0/);
  assert.match(listScrollRuleMatch![0], /overflow-y:\s*auto/);
});

// ── W7c–e (RolesPage visual parity pass). Two separate cards + gap, not one
// bordered frame — mirrors RolesPage's .body / .rolesPanel / .permPanel ──

test('W7c. .setupsListCard and .setupsDetailPane are each card containers (border + border-radius), mirroring RolesPage\'s .rolesPanel / .permPanel', () => {
  const listCardRuleMatch = pageCssSource.match(/\.setupsListCard\s*\{[^}]*\}/);
  assert.ok(listCardRuleMatch, '.setupsListCard rule not found');
  assert.match(listCardRuleMatch![0], /border:\s*1px solid/);
  assert.match(listCardRuleMatch![0], /border-radius:\s*\d/);

  const detailPaneRuleMatch = pageCssSource.match(/\.setupsDetailPane\s*\{[^}]*\}/);
  assert.ok(detailPaneRuleMatch, '.setupsDetailPane rule not found');
  assert.match(detailPaneRuleMatch![0], /border:\s*1px solid/);
  assert.match(detailPaneRuleMatch![0], /border-radius:\s*\d/);
});

test('W7d. .setupsBody has a non-zero gap between the two cards and declares no border/background/box-shadow of its own', () => {
  const bodyRuleMatch = pageCssSource.match(/\.setupsBody\s*\{[^}]*\}/);
  assert.ok(bodyRuleMatch, '.setupsBody rule not found');
  const body = bodyRuleMatch![0];
  const gapMatch = body.match(/gap:\s*([^;]+);/);
  assert.ok(gapMatch, '.setupsBody must declare a gap');
  assert.notEqual(gapMatch![1].trim(), '0', '.setupsBody gap must be non-zero (two separate cards, not one frame)');
  assert.doesNotMatch(body, /\bborder:/, '.setupsBody must not draw its own border — each card draws its own');
  assert.doesNotMatch(body, /\bbackground:/, '.setupsBody must not draw its own background');
  assert.doesNotMatch(body, /box-shadow:/, '.setupsBody must not draw its own box-shadow');
});

test('W7e. .setupsListCard no longer has the old single-frame divider (border-right) or grey background', () => {
  const listCardRuleMatch = pageCssSource.match(/\.setupsListCard\s*\{[^}]*\}/);
  assert.ok(listCardRuleMatch, '.setupsListCard rule not found');
  assert.doesNotMatch(listCardRuleMatch![0], /border-right/, 'the master/detail divider must be gone now that they are separate cards');
  assert.doesNotMatch(listCardRuleMatch![0], /#f8fafc/, 'the master pane must be white like RolesPage\'s .rolesPanel, not grey');
});

test('W6e. The search input and filter row are wrapped in a single .setupsControls block, mirroring RolesPage\'s .rolesControls', () => {
  const controlsIdx = pageSource.indexOf('styles.setupsControls');
  assert.notEqual(controlsIdx, -1, '.setupsControls wrapper not found');
  const searchIdx = pageSource.indexOf('styles.policySearchInput', controlsIdx);
  const filterIdx = pageSource.indexOf('styles.policyFilterRow', controlsIdx);
  assert.ok(searchIdx > controlsIdx, 'search input must be inside .setupsControls');
  assert.ok(filterIdx > searchIdx, 'filter row must come after the search input, still inside .setupsControls');
});

test('W6b (Step 2). The master pane has a search input, a 4-way filter segmented control, and a pinned footer, in that structural order', () => {
  const searchIdx = pageSource.indexOf('styles.policySearchInput');
  const filterIdx = pageSource.indexOf('styles.policyFilterRow');
  const scrollIdx = pageSource.indexOf('styles.setupListScroll');
  const footerIdx = pageSource.indexOf('styles.setupsPaneFooter');
  assert.notEqual(searchIdx, -1, 'search input not found');
  assert.notEqual(filterIdx, -1, 'filter row not found');
  assert.notEqual(scrollIdx, -1, 'scroll region not found');
  assert.notEqual(footerIdx, -1, 'pane footer not found');
  assert.ok(searchIdx < filterIdx && filterIdx < scrollIdx && scrollIdx < footerIdx,
    'expected search -> filter -> scroll region -> footer, in that order');

  assert.match(pageSource, /type="search"/);
  assert.match(pageSource, /aria-label="Search policies"/);
  assert.match(pageSource, /placeholder="Search policies…"/);
  assert.match(pageSource, /\['All', 'Active', 'Inactive', 'Archived'\]/, 'expected the four filter values');
  assert.match(pageSource, /aria-pressed=\{filter === f\}/);
});

test('W6c (Step 2). An Inactive row with a futureStart shows the "Starts <date>" secondary line via futureStartLabel', () => {
  assert.match(pageSource, /futureStartLabel\(rowUsage\.futureStart\)/);
  assert.match(pageSource, /state === 'Inactive' && rowUsage\?\.futureStart != null/);
});

test('W6d (Step 2). An empty visible list shows a friendly "No policies match." line inside the scroll region, with an extra hint when search is non-empty', () => {
  const scrollIdx = pageSource.indexOf('styles.setupListScroll');
  const emptyIdx = pageSource.indexOf('No policies match.', scrollIdx);
  assert.notEqual(emptyIdx, -1, 'expected "No policies match." inside the scroll region');
  assert.match(pageSource, /search\.trim\(\) !== ''/);
});

test('W9. The welcome empty state (no policies) is still master-level — rendered as a sibling of, not inside, .setupsBody', () => {
  const welcomeIdx = pageSource.indexOf('Welcome to Payroll');
  assert.notEqual(welcomeIdx, -1);
  const bodyIdx = pageSource.indexOf('styles.setupsBody');
  // The welcome card and the master-detail grid are mutually exclusive
  // branches of the same ternary (setups.length === 0 ? welcome : grid) —
  // the welcome text must appear before .setupsBody's own branch, not
  // nested inside it.
  assert.ok(welcomeIdx < bodyIdx, 'welcome empty state must not be nested inside .setupsBody');
});

test('W10 (Step 2). The page-level PageHeader Create button is gone — PageHeader no longer receives an `actions` prop', () => {
  assert.doesNotMatch(pageSource, /<PageHeader[\s\S]*?actions=/);
  assert.doesNotMatch(pageSource, /\+ Create payroll policy/);
});

test('W10b (Step 2). "+ Add policy" lives inside .setupsListCard, in the pinned footer after the scroll region, gated on canManage, and calls the same openCreateModal (no duplicate create logic)', () => {
  const listCardIdx = pageSource.indexOf('styles.setupsListCard}');
  const footerIdx = pageSource.indexOf('styles.setupsPaneFooter', listCardIdx);
  const scrollIdx = pageSource.indexOf('styles.setupListScroll', listCardIdx);
  assert.notEqual(listCardIdx, -1);
  assert.notEqual(footerIdx, -1, '"+ Add policy" footer must be inside .setupsListCard');
  assert.ok(scrollIdx !== -1 && scrollIdx < footerIdx, 'the scroll region must precede the pinned footer');

  const footerBlock = pageSource.slice(footerIdx, footerIdx + 300);
  assert.match(footerBlock, /\+ Add policy/);
  assert.match(footerBlock, /onClick=\{openCreateModal\}/);

  assert.match(pageSource, /\{canManage && \(\s*<div className=\{styles\.setupsPaneFooter\}>/);

  // Exactly one "+ Add policy" button (outside doc-comment mentions), and
  // openCreateModal is only ever wired to a button (never duplicated create
  // logic elsewhere).
  const addPolicyMatches = stripBlockComments(pageSource).match(/\+ Add policy/g) ?? [];
  assert.equal(addPolicyMatches.length, 1, 'expected exactly one "+ Add policy" button');
  const submitCreateMatches = pageSource.match(/async function submitCreate/g) ?? [];
  assert.equal(submitCreateMatches.length, 1, 'submitCreate must be defined exactly once');
});

test('W11. The create-policy modal submit path (submitCreate -> createPayrollSetup, then select the new policy) is intact in the shell', () => {
  const startIdx = pageSource.indexOf('async function submitCreate');
  assert.notEqual(startIdx, -1, 'submitCreate not found');
  const endIdx = pageSource.indexOf('\n  }', startIdx);
  const body = pageSource.slice(startIdx, endIdx);
  assert.match(body, /\bcreatePayrollSetup\(/);
  assert.match(body, /setSearchParams\(\{\s*setupId:\s*String\(created\.setup_id\)\s*\}/);
});

test('W11b (Step 2). After a successful create, submitCreate also resets filter to \'All\' and search to \'\' so the new policy is visible', () => {
  const startIdx = pageSource.indexOf('async function submitCreate');
  assert.notEqual(startIdx, -1, 'submitCreate not found');
  const endIdx = pageSource.indexOf('\n  }', startIdx);
  const body = pageSource.slice(startIdx, endIdx);
  const selectIdx = body.indexOf('setSearchParams(');
  assert.notEqual(selectIdx, -1);
  // Reset BEFORE the URL selection so the new policy is never filtered out
  // (and re-resolved away) while the param updates.
  const before = body.slice(0, selectIdx);
  assert.match(before, /setFilter\('All'\)/);
  assert.match(before, /setSearch\(''\)/);
});

test('W12. The create modal is still wired to the "Create payroll policy" submit button', () => {
  assert.match(pageSource, /onClick=\{submitCreate\}/);
});

test('W13 (Step 3A). PolicyDetailPanel.tsx exports the component; the shell imports and mounts it with usageState from the derived usage map, and never passes showLowerSections', () => {
  assert.match(detailPanelSource, /export function PolicyDetailPanel/);
  assert.match(pageSource, /import\s*\{[^}]*PolicyDetailPanel[^}]*\}\s*from '\.\/PolicyDetailPanel'/);
  const mount = pageSource.match(/<PolicyDetailPanel[\s\S]*?\/>/);
  assert.ok(mount, 'expected a <PolicyDetailPanel ... /> mount');
  assert.match(mount[0], /usageState=\{usage\.get\(selectedPolicy\.setup_id\)\?\.state \?\? 'Inactive'\}/);
  assert.doesNotMatch(mount[0], /showLowerSections/);
});

test('W13c (Step 3A correction). The left master pane is untouched: rows still render the raw derived usage state, and detail-only helpers are not used by the shell', () => {
  assert.doesNotMatch(pageSource, /usageStateLabel|hasPublishedVersion|currentVersion|policyScheduleFields/);
  assert.match(pageSource, /styles\.setupRowBadges/);
  assert.match(pageSource, /\{state\}\s*<\/span>/);
  assert.match(pageSource, /placeholder="Search policies…"/);
  assert.match(pageSource, /\+ Add policy/);
});

test('W13b (Step 3A). Selection resolves against the visible list via resolveSelectedPolicyId; hidden/empty selection rewrites or clears ?setupId= with replace', () => {
  assert.match(pageSource, /const selectableList = summariesLoading \? setups : visible;/);
  assert.match(pageSource, /resolveSelectedPolicyId\(setupIdParam, selectableList\)/);
  assert.match(
    pageSource,
    /setSearchParams\(\s*selectedPolicyId != null \? \{ setupId: String\(selectedPolicyId\) \} : \{\},\s*\{ replace: true \},?\s*\)/,
  );
});

// Strips block comments so doc-comment mentions of moved content (e.g. this
// file's own header explaining what moved to PolicyDetailPanel.tsx) don't
// false-positive a "must not render" check.
function stripBlockComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '');
}

test('W14. Old detail content is NOT rendered by PayrollSetupsPage.tsx in the Policies tab, but IS present in PolicyDetailPanel.tsx', () => {
  const movedContentMarkers = [
    'Next steps',
    'Company default',
    'Draft schedules',
    'Policy updates',
    'Assigned branches',
    'Archive',
    "<PublishPanel",
  ];
  const pageSourceNoComments = stripBlockComments(pageSource);
  for (const marker of movedContentMarkers) {
    const escaped = marker.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    // "Archive" is the archive-policy action button/label moved to
    // PolicyDetailPanel.tsx — a word-boundary match so it never false-positives
    // on the Step 2 master pane's own unrelated "Archived" filter/status label.
    const re = marker === 'Archive' ? /\bArchive\b/ : new RegExp(escaped);
    assert.doesNotMatch(pageSourceNoComments, re, `shell must not render "${marker}"`);
    assert.match(detailPanelSource, re, `PolicyDetailPanel.tsx must still contain "${marker}"`);
  }
});

test('W15. PolicyDetailPanel.tsx still imports PublishPanel and DraftEditorModal (business logic preserved, not mounted by the shell)', () => {
  assert.match(detailPanelSource, /import \{ PublishPanel \} from '\.\/PublishPanel'/);
  assert.match(detailPanelSource, /import \{ DraftEditorModal \} from '\.\/DraftEditorModal'/);
});

test('W16. PolicyDetailPanel.tsx typechecks as a real component (has a props type and returns JSX) — sanity via source shape', () => {
  assert.match(detailPanelSource, /type PolicyDetailPanelProps = \{/);
  assert.match(detailPanelSource, /export function PolicyDetailPanel\(\{/);
});

test('W17. The Branch Assignments tab is unchanged: still mounts BranchAssignmentsTab with the same props', () => {
  assert.match(pageSource, /<BranchAssignmentsTab/);
  for (const prop of ['summaries', 'summariesLoading', 'summariesError', 'activeSetups', 'canAssign', 'mutating', 'setMutating', 'onReload', 'showToast']) {
    assert.match(pageSource, new RegExp(`${prop}=\\{`), `BranchAssignmentsTab must still receive ${prop}`);
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// Pure helpers (payrollSetupsView.ts)
// ═══════════════════════════════════════════════════════════════════════════

// ── validateSetupCreate / validateSetupUpdate (Unit A: code now optional) ──

test('validateSetupCreate: blank Integration code is fine — the server generates one', () => {
  assert.equal(validateSetupCreate({ setup_code: '', setup_name: 'Standard Weekly' }), null);
});

test('validateSetupCreate: code at 50 chars is valid; 51 chars is rejected', () => {
  const code50 = 'A'.repeat(50);
  const code51 = 'A'.repeat(51);
  assert.equal(validateSetupCreate({ setup_code: code50, setup_name: 'Name' }), null);
  assert.equal(
    validateSetupCreate({ setup_code: code51, setup_name: 'Name' }),
    'Setup code must be 50 characters or fewer.',
  );
});

test('validateSetupCreate: an intentionally-supplied code still follows the backend pattern', () => {
  assert.equal(
    validateSetupCreate({ setup_code: '_bad', setup_name: 'Name' }),
    "Integration code must start with a letter or digit and contain only letters, digits, '_', '.', or '-'.",
  );
  assert.equal(validateSetupCreate({ setup_code: 'A1_b.c-9', setup_name: 'Name' }), null);
});

test('validateSetupCreate: code pattern rejects invalid interior characters', () => {
  assert.equal(
    validateSetupCreate({ setup_code: 'code space', setup_name: 'Name' }),
    "Integration code must start with a letter or digit and contain only letters, digits, '_', '.', or '-'.",
  );
});

test('validateSetupCreate: empty name is required (given a valid/blank code)', () => {
  assert.equal(validateSetupCreate({ setup_code: '', setup_name: '' }), 'Policy name is required.');
  assert.equal(validateSetupCreate({ setup_code: 'CODE1', setup_name: '' }), 'Policy name is required.');
});

test('validateSetupCreate: name at 200 chars is valid; 201 chars is rejected', () => {
  const name200 = 'N'.repeat(200);
  const name201 = 'N'.repeat(201);
  assert.equal(validateSetupCreate({ setup_code: '', setup_name: name200 }), null);
  assert.equal(
    validateSetupCreate({ setup_code: '', setup_name: name201 }),
    'Policy name must be 200 characters or fewer.',
  );
});

test('validateSetupCreate: valid name alone returns null (no code)', () => {
  assert.equal(validateSetupCreate({ setup_code: '', setup_name: 'Standard Weekly' }), null);
});

test('validateSetupUpdate: empty name is required; boundaries mirror create', () => {
  assert.equal(validateSetupUpdate({ setup_name: '' }), 'Policy name is required.');
  assert.equal(validateSetupUpdate({ setup_name: 'N'.repeat(200) }), null);
  assert.equal(
    validateSetupUpdate({ setup_name: 'N'.repeat(201) }),
    'Policy name must be 200 characters or fewer.',
  );
  assert.equal(validateSetupUpdate({ setup_name: 'Valid Name' }), null);
});

// ── normalizeDescription ────────────────────────────────────────────────────

test('normalizeDescription: whitespace-only becomes null; non-empty passes through unchanged', () => {
  assert.equal(normalizeDescription(''), null);
  assert.equal(normalizeDescription('   '), null);
  assert.equal(normalizeDescription('  hello  '), '  hello  ');
  assert.equal(normalizeDescription('hello'), 'hello');
});

// ── archiveBlockedReason (Unit A wording) ───────────────────────────────────

test('archiveBlockedReason: blocks exactly the current default; otherwise null', () => {
  assert.equal(
    archiveBlockedReason(5, 5),
    'This policy is the company default. Clear or change the company default before archiving.',
  );
  assert.equal(archiveBlockedReason(5, 6), null);
  assert.equal(archiveBlockedReason(5, null), null);
});

// ── versionRelationship (Unit A: "Version N", never "vN") ───────────────────

function makeVersion(overrides: Partial<VersionResponse> = {}): VersionResponse {
  return {
    setup_id: 1,
    version_id: 100,
    lifecycle_state: 'Published',
    version_number: 1,
    effective_from_date: '2026-01-01',
    effective_to_date: null,
    schedule: {
      payroll_frequency: 'Week',
      anchor_start_date: '2026-01-01',
      custom_interval_days: null,
      normal_days_off_mask: 65,
    },
    config_hash: 'hash',
    replaces_version_id: null,
    replaced_by_version_id: null,
    is_terminal: true,
    is_current: false,
    ...overrides,
  };
}

test('versionRelationship: replaced_by resolves to "Replaced by Version N"', () => {
  const successor = makeVersion({ version_id: 101, version_number: 2 });
  const version = makeVersion({ version_id: 100, version_number: 1, replaced_by_version_id: 101 });
  assert.equal(versionRelationship(version, [version, successor]), 'Replaced by Version 2');
});

test('versionRelationship: replaces resolves to "Replaces Version N"', () => {
  const predecessor = makeVersion({ version_id: 99, version_number: 1 });
  const version = makeVersion({ version_id: 100, version_number: 2, replaces_version_id: 99 });
  assert.equal(versionRelationship(version, [predecessor, version]), 'Replaces Version 1');
});

test('versionRelationship: neither replaces nor replaced_by returns null', () => {
  const version = makeVersion();
  assert.equal(versionRelationship(version, [version]), null);
});

test('versionRelationship: unknown referenced id falls back to "Version #id"', () => {
  const version = makeVersion({ version_id: 100, version_number: 1, replaced_by_version_id: 999 });
  assert.equal(versionRelationship(version, [version]), 'Replaced by Version #999');
});

// ── draftCreatedLabel (Unit A: no raw draft ids in primary UI) ─────────────

test('draftCreatedLabel: "Draft created <formatted date>" from the ISO date part of created_at_utc', () => {
  assert.equal(draftCreatedLabel('2026-09-23T00:00:00Z'), 'Draft created Sep 23, 2026');
  assert.equal(draftCreatedLabel('2026-01-04T14:30:00.123Z'), 'Draft created Jan 4, 2026');
});

test('draftCreatedLabel: malformed timestamp falls back to plain "Draft created"', () => {
  assert.equal(draftCreatedLabel('not-a-timestamp'), 'Draft created');
  assert.equal(draftCreatedLabel(''), 'Draft created');
});

// ── assignedPolicyBranchRows (derived from branch-summaries) ────────────────

function makeAssignmentSummary(
  overrides: Partial<PolicyAssignmentSummaryResponse> = {},
): PolicyAssignmentSummaryResponse {
  return {
    assignment_id: 1,
    setup_id: 1,
    setup_code: 'STD',
    setup_name: 'Standard',
    effective_from_date: '2026-01-01',
    effective_to_date: null,
    payroll_frequency: 'Week',
    custom_interval_days: null,
    ...overrides,
  };
}

function makeSummary(overrides: Partial<BranchPolicySummaryResponse> = {}): BranchPolicySummaryResponse {
  return {
    branch_id: 10,
    branch_code: 'ALP',
    branch_name: 'Alpha',
    branch_status: 'Active',
    reference_date: '2026-01-01',
    payroll_set_up: true,
    current: makeAssignmentSummary(),
    scheduled_change: null,
    upcoming_assignments: [],
    readiness_reason: 'READY',
    readiness_date: null,
    ...overrides,
  };
}

test('assignedPolicyBranchRows: includes a branch whose current assignment matches the setup', () => {
  const rows = assignedPolicyBranchRows([makeSummary({ current: makeAssignmentSummary({ setup_id: 1 }) })], 1);
  assert.equal(rows.length, 1);
  assert.deepEqual(rows[0], {
    branch_id: 10,
    branch_name: 'Alpha',
    branch_code: 'ALP',
    relation: 'current',
    since_date: '2026-01-01',
  } satisfies PolicyBranchRow);
});

test('assignedPolicyBranchRows: includes a branch whose scheduled_change (not current) matches the setup', () => {
  const rows = assignedPolicyBranchRows(
    [makeSummary({ current: null, scheduled_change: makeAssignmentSummary({ setup_id: 1, effective_from_date: '2026-03-01' }) })],
    1,
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].relation, 'scheduled');
  assert.equal(rows[0].since_date, '2026-03-01');
});

test('assignedPolicyBranchRows: excludes branches following a different policy', () => {
  const rows = assignedPolicyBranchRows([makeSummary({ current: makeAssignmentSummary({ setup_id: 2 }) })], 1);
  assert.deepEqual(rows, []);
});

test('assignedPolicyBranchRows: current takes priority over scheduled_change when both happen to reference the same setup', () => {
  const rows = assignedPolicyBranchRows(
    [
      makeSummary({
        current: makeAssignmentSummary({ setup_id: 1, effective_from_date: '2026-01-01' }),
        scheduled_change: makeAssignmentSummary({ setup_id: 1, effective_from_date: '2027-01-01' }),
      }),
    ],
    1,
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].relation, 'current');
  assert.equal(rows[0].since_date, '2026-01-01');
});

test('assignedPolicyBranchRows: sorts by branch_name', () => {
  const rows = assignedPolicyBranchRows(
    [
      makeSummary({ branch_id: 1, branch_name: 'Bravo', current: makeAssignmentSummary({ setup_id: 1 }) }),
      makeSummary({ branch_id: 2, branch_name: 'Alpha', current: makeAssignmentSummary({ setup_id: 1 }) }),
    ],
    1,
  );
  assert.deepEqual(rows.map((r) => r.branch_name), ['Alpha', 'Bravo']);
});

// ── policyNextSteps ──────────────────────────────────────────────────────────

test('policyNextSteps: no drafts, no published versions -> "add-draft" only', () => {
  assert.deepEqual(
    policyNextSteps({
      hasDrafts: false,
      hasPublishedVersions: false,
      hasCompanyDefault: true,
      hasAnyBranchFollowing: true,
      canAssign: true,
    }),
    ['add-draft'],
  );
});

test('policyNextSteps: drafts exist, no published version -> "publish" only', () => {
  assert.deepEqual(
    policyNextSteps({
      hasDrafts: true,
      hasPublishedVersions: false,
      hasCompanyDefault: true,
      hasAnyBranchFollowing: true,
      canAssign: true,
    }),
    ['publish'],
  );
});

test('policyNextSteps: published, no company default, canAssign -> "set-default"', () => {
  assert.deepEqual(
    policyNextSteps({
      hasDrafts: true,
      hasPublishedVersions: true,
      hasCompanyDefault: false,
      hasAnyBranchFollowing: true,
      canAssign: true,
    }),
    ['set-default'],
  );
});

test('policyNextSteps: "set-default" is withheld when the user cannot assign, even with no company default', () => {
  assert.deepEqual(
    policyNextSteps({
      hasDrafts: true,
      hasPublishedVersions: true,
      hasCompanyDefault: false,
      hasAnyBranchFollowing: true,
      canAssign: false,
    }),
    [],
  );
});

test('policyNextSteps: published, no branch following -> "assign-branches"', () => {
  assert.deepEqual(
    policyNextSteps({
      hasDrafts: true,
      hasPublishedVersions: true,
      hasCompanyDefault: true,
      hasAnyBranchFollowing: false,
      canAssign: true,
    }),
    ['assign-branches'],
  );
});

test('policyNextSteps: fully complete -> empty (guide hidden)', () => {
  assert.deepEqual(
    policyNextSteps({
      hasDrafts: true,
      hasPublishedVersions: true,
      hasCompanyDefault: true,
      hasAnyBranchFollowing: true,
      canAssign: true,
    }),
    [],
  );
});

test('policyNextSteps: can report set-default and assign-branches together', () => {
  assert.deepEqual(
    policyNextSteps({
      hasDrafts: true,
      hasPublishedVersions: true,
      hasCompanyDefault: false,
      hasAnyBranchFollowing: false,
      canAssign: true,
    }),
    ['set-default', 'assign-branches'],
  );
});
