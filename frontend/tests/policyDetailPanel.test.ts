import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const DIR = '../src/pages/settings/payroll/';
const source = readFileSync(new URL(`${DIR}PolicyDetailPanel.tsx`, import.meta.url), 'utf8');

const headerSource = readFileSync(new URL(DIR + 'PolicyDetailHeader.tsx', import.meta.url), 'utf8');
const lowerSource = readFileSync(new URL(DIR + 'PolicyDetailLowerSections.tsx', import.meta.url), 'utf8');
const dialogsSource = readFileSync(new URL(DIR + 'PolicyDetailDialogs.tsx', import.meta.url), 'utf8');
const dataSource = readFileSync(new URL(DIR + 'usePolicyDetailData.ts', import.meta.url), 'utf8');
const typesSource = readFileSync(new URL(DIR + 'policyDetailTypes.ts', import.meta.url), 'utf8');
const timelineSource = readFileSync(new URL(DIR + 'VersionTimelineSection.tsx', import.meta.url), 'utf8');
const featureSource = [source, headerSource, lowerSource, dialogsSource, dataSource, timelineSource].join('\n');

function countOf(re: RegExp): number {
  return (featureSource.match(re) ?? []).length;
}

const lowerGateIdx = source.indexOf('{showLowerSections && (');
const confirmIdx = source.indexOf('<PolicyDetailDialogs');

test('the header renders the policy name and the friendly usage label from usageState (never raw status); no code and no ids', () => {
  const header = headerSource;
  assert.match(header, /styles\.policyTitle\}>\{selectedPolicy\.setup_name\}/);
  assert.match(header, /usageState === 'Active'/);
  assert.match(header, /\{usageStateLabel\(usageState\)\}/);
  assert.doesNotMatch(header, /\{selectedPolicy\.status\}/);
  assert.doesNotMatch(header, /\{selectedPolicy\.setup_id\}/);
  assert.doesNotMatch(header, /setup_code/);
  assert.doesNotMatch(header, /policyCodePill/);
});

test('the header shows "Default for new branches" only for the company default policy', () => {
  const header = headerSource;
  assert.match(header, /selectedPolicy\.setup_id === defaultSetupId && \(\s*<span[^>]*>Default for new branches<\/span>/);
});

test('Set as default keeps the old gates (canAssign + Active + not default), goes through openConfirm, and the old wording is gone', () => {
  const header = headerSource;
  assert.match(
    header,
    /canAssign && selectedPolicy\.status === 'Active' && selectedPolicy\.setup_id !== defaultSetupId && \(\s*<button[\s\S]*?openConfirm\('set-default'\)[\s\S]*?>\s*Set as default\s*<\/button>/,
  );
  assert.doesNotMatch(featureSource, /Use for new branches|Use as default/);
});

test('Set as default is disabled without a Published Version and explains why (frontend prerequisite mirrors the backend rule)', () => {
  const header = headerSource;
  assert.match(source, /hasPublishedVersion\(versions, selectedPolicy\.setup_id\)/);
  assert.match(source, /const versionsLoaded = !versionsLoading && !versionsError;/);
  assert.match(header, /disabled=\{mutating \|\| !defaultPrerequisiteMet\}/);
  assert.match(header, /defaultBlockedByNoPublishedVersion &&/);
  assert.match(header, /Publish a payroll schedule before setting this policy as the default\./);
  assert.match(header, /aria-describedby=\{defaultBlockedByNoPublishedVersion \? 'policy-default-hint' : undefined\}/);
  // The hint is only for a policy that would otherwise be eligible (same permission/status/not-default gates).
  assert.match(
    header,
    /defaultBlockedByNoPublishedVersion &&\s*canAssign &&\s*selectedPolicy\.status === 'Active' &&\s*selectedPolicy\.setup_id !== defaultSetupId/,
  );
});

test('Clear company default requires the default policy + canAssign and goes through openConfirm', () => {
  const header = headerSource;
  assert.match(
    header,
    /selectedPolicy\.setup_id === defaultSetupId && canAssign && \(\s*<button[\s\S]*?openConfirm\('clear-default'\)[\s\S]*?>\s*Clear company default/,
  );
});

test('Edit details keeps its gate (canManage + Active) and reuses openEditModal', () => {
  const header = headerSource;
  assert.match(
    header,
    /canManage && selectedPolicy\.status === 'Active' && \(\s*<button[^>]*onClick=\{openEditModal\}[^>]*>\s*Edit details/,
  );
});

test('set/clear default requests exist exactly once each (no duplicate requests in the header)', () => {
  assert.equal(countOf(/setDefaultPayrollSetup\(/g), 1);
  assert.equal(countOf(/clearDefaultPayrollSetup\(/g), 1);
  assert.equal(countOf(/updatePayrollSetup\(/g), 1);
  assert.equal(countOf(/listPayrollSetupVersions\(/g), 1);
});

test('existing confirm-dialog copy is unchanged', () => {
  assert.match(dialogsSource, /title="Set company default\?"/);
  assert.match(dialogsSource, /message="Affects only branches created later; existing branch assignments never change\."/);
  assert.match(dialogsSource, /title="Clear company default\?"/);
  assert.match(dialogsSource, /Existing branch assignments are unchanged\./);
});

test('Payroll schedule card: renamed, fields come from the current version only, Custom interval is conditional', () => {
  assert.match(source, /policyScheduleFields\(\s*selectedPolicy \? currentVersion\(versions, selectedPolicy\.setup_id\) : null,?\s*\)/);
  assert.match(headerSource, /<h3 className=\{styles\.policyDetailsTitle\}>Payroll schedule<\/h3>/);
  assert.doesNotMatch(featureSource, />Policy details</);
  for (const label of ['Payroll frequency', 'First payroll period starts on', 'Custom interval', 'Regular days off']) {
    assert.match(headerSource, new RegExp(`<dt>${label}</dt>`), `missing label ${label}`);
  }
  assert.doesNotMatch(featureSource, /<dt>Interval<\/dt>/);
  assert.match(headerSource, /scheduleFields\.interval !== null && \(\s*<div[^>]*>\s*<dt>Custom interval<\/dt>/);
  const cardStart = headerSource.indexOf('Payroll schedule: the currently effective');
  const card = headerSource.slice(cardStart);
  assert.doesNotMatch(card, /Anchor/);
  assert.doesNotMatch(card, /[Ll]ocked/);
});

test('current-version display requires Branch context and keeps the contextual empty state', () => {
  const card = headerSource;
  assert.match(card, /scheduleFields === null \? \(/);
  assert.match(card, /Select a Branch to identify the current version/);
  assert.match(card, /No payroll schedule is active for this date/);
  assert.match(card, /Current version using \$\{referenceBranchName\}'s business date/);
  assert.match(card, /published schedules are shared configuration/i);
  assert.doesNotMatch(card, /'—'|"—"/);
  // No draft/publish actions in the empty state.
  assert.doesNotMatch(card, /openCreateDraftModal|setPublishDraft/);
});

test('version data is keyed to both Setup and Branch context before it can render', () => {
  assert.match(dataSource, /setupId: number \| null/);
  assert.match(dataSource, /branchId: number \| null/);
  assert.match(dataSource, /versionsState\.setupId === selectedPolicyId && versionsState\.branchId === branchId/);
  assert.match(dataSource, /!hasVersionContext \|\| versionsState\.loading/);
});

test('lower sections render only behind showLowerSections (default false); dialogs and modals stay outside it', () => {
  assert.match(typesSource, /showLowerSections\?: boolean/);
  assert.match(source, /showLowerSections = false,/);
  const lowerGate = source.match(/\{showLowerSections && \(\s*<PolicyDetailLowerSections[\s\S]*?\/>\s*\)\}/);
  assert.ok(lowerGate);
  assert.notEqual(lowerGateIdx, -1);
  assert.equal((source.match(/<PolicyDetailLowerSections\b/g) ?? []).length, 1);
  assert.ok(confirmIdx > lowerGateIdx + lowerGate[0].length, 'dialogs must remain after the lower-sections gate closes');
  for (const marker of ['title="Draft schedules"', '<SectionCard title="Publish">', 'title="Policy updates"', 'title="Assigned branches"', '<p className={styles.label}>Next steps</p>']) {
    assert.ok(lowerSource.includes(marker), marker + ' must remain in the lower owner');
  }
  assert.ok(source.indexOf('<PolicyDetailHeader') < lowerGateIdx);
  assert.ok(source.indexOf('<VersionTimelineSection') < lowerGateIdx);
});
