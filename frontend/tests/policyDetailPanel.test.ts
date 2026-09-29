import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const DIR = '../src/pages/settings/payroll/';
const source = readFileSync(new URL(`${DIR}PolicyDetailPanel.tsx`, import.meta.url), 'utf8');

function countOf(re: RegExp): number {
  return (source.match(re) ?? []).length;
}

const headerStart = source.indexOf('Header: name');
const lowerGateIdx = source.indexOf('{showLowerSections && (');
const confirmIdx = source.indexOf('Confirm dialogs (policy-level)');

test('the header renders the policy name and the friendly usage label from usageState (never raw status); no code and no ids', () => {
  const header = source.slice(headerStart, lowerGateIdx);
  assert.match(header, /styles\.policyTitle\}>\{selectedPolicy\.setup_name\}/);
  assert.match(header, /usageState === 'Active'/);
  assert.match(header, /\{usageStateLabel\(usageState\)\}/);
  assert.doesNotMatch(header, /\{selectedPolicy\.status\}/);
  assert.doesNotMatch(header, /\{selectedPolicy\.setup_id\}/);
  assert.doesNotMatch(header, /setup_code/);
  assert.doesNotMatch(header, /policyCodePill/);
});

test('the header shows "Default for new branches" only for the company default policy', () => {
  const header = source.slice(headerStart, lowerGateIdx);
  assert.match(header, /selectedPolicy\.setup_id === defaultSetupId && \(\s*<span[^>]*>Default for new branches<\/span>/);
});

test('Set as default keeps the old gates (canAssign + Active + not default), goes through openConfirm, and the old wording is gone', () => {
  const header = source.slice(headerStart, lowerGateIdx);
  assert.match(
    header,
    /canAssign && selectedPolicy\.status === 'Active' && selectedPolicy\.setup_id !== defaultSetupId && \(\s*<button[\s\S]*?openConfirm\('set-default'\)[\s\S]*?>\s*Set as default\s*<\/button>/,
  );
  assert.doesNotMatch(source, /Use for new branches|Use as default/);
});

test('Set as default is disabled without a Published Version and explains why (frontend prerequisite mirrors the backend rule)', () => {
  const header = source.slice(headerStart, lowerGateIdx);
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
  const header = source.slice(headerStart, lowerGateIdx);
  assert.match(
    header,
    /selectedPolicy\.setup_id === defaultSetupId && canAssign && \(\s*<button[\s\S]*?openConfirm\('clear-default'\)[\s\S]*?>\s*Clear company default/,
  );
});

test('Edit details keeps its gate (canManage + Active) and reuses openEditModal', () => {
  const header = source.slice(headerStart, lowerGateIdx);
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
  assert.match(source, /title="Set company default\?"/);
  assert.match(source, /message="Affects only branches created later; existing branch assignments never change\."/);
  assert.match(source, /title="Clear company default\?"/);
  assert.match(source, /Existing branch assignments are unchanged\./);
});

test('Payroll schedule card: renamed, fields come from the current version only, Custom interval is conditional', () => {
  assert.match(source, /policyScheduleFields\(\s*selectedPolicy \? currentVersion\(versions, selectedPolicy\.setup_id\) : null,?\s*\)/);
  assert.match(source, /<h3 className=\{styles\.policyDetailsTitle\}>Payroll schedule<\/h3>/);
  assert.doesNotMatch(source, />Policy details</);
  for (const label of ['Payroll frequency', 'First payroll period starts on', 'Custom interval', 'Regular days off']) {
    assert.match(source, new RegExp(`<dt>${label}</dt>`), `missing label ${label}`);
  }
  assert.doesNotMatch(source, /<dt>Interval<\/dt>/);
  assert.match(source, /scheduleFields\.interval !== null && \(\s*<div[^>]*>\s*<dt>Custom interval<\/dt>/);
  const card = source.slice(source.indexOf('Payroll schedule: the currently effective'), lowerGateIdx);
  assert.doesNotMatch(card, /Anchor/);
  assert.doesNotMatch(card, /[Ll]ocked/);
});

test('no current version: one intentional empty state, no placeholder dash values', () => {
  const planningStart = source.indexOf('<VersionPlanningSection');
  const card = source.slice(source.indexOf('Payroll schedule: the currently effective'), planningStart);
  assert.match(card, /scheduleFields === null \? \(/);
  assert.match(card, /<p className=\{styles\.scheduleEmptyTitle\}>No payroll schedule is active yet<\/p>/);
  assert.match(card, /Publish a schedule to start using this policy\./);
  assert.match(card, /The schedule currently in effect for this policy\./);
  assert.doesNotMatch(card, /'—'|"—"/);
  // No draft/publish actions in the empty state.
  assert.doesNotMatch(card, /openCreateDraftModal|setPublishDraft/);
});

test('lower sections render only behind showLowerSections (default false); dialogs and modals stay outside it', () => {
  assert.match(source, /showLowerSections = false,/);
  assert.notEqual(lowerGateIdx, -1);
  assert.ok(confirmIdx > lowerGateIdx);
  for (const marker of ['title="Draft schedules"', '<SectionCard title="Publish">', 'title="Policy updates"', 'title="Assigned branches"', '<p className={styles.label}>Next steps</p>']) {
    const idx = source.indexOf(marker);
    assert.ok(idx > lowerGateIdx && idx < confirmIdx, `${marker} must sit inside the showLowerSections gate`);
  }
  // Header + Policy details render before the gate.
  assert.ok(headerStart < lowerGateIdx);
});
