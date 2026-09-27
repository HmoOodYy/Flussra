import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const sectionSource = readFileSync(
  new URL('../src/pages/settings/payroll/VersionPlanningSection.tsx', import.meta.url),
  'utf8',
);
const panelSource = readFileSync(
  new URL('../src/pages/settings/payroll/PolicyDetailPanel.tsx', import.meta.url),
  'utf8',
);
const modalSource = readFileSync(
  new URL('../src/pages/settings/payroll/DraftEditorModal.tsx', import.meta.url),
  'utf8',
);
const timelineSource = readFileSync(
  new URL('../src/pages/settings/payroll/policyDetailView.ts', import.meta.url),
  'utf8',
);

test('empty Version Planning has one centered create path and no header duplicate', () => {
  assert.match(sectionSource, /const isResolvedEmpty =/);
  assert.match(sectionSource, /No versions planned yet/);
  assert.match(sectionSource, /Create a new version now\. You can save your work for later or publish it when you&apos;re ready\./);
  assert.match(sectionSource, /isResolvedEmpty \? \(/);
  assert.match(sectionSource, /!isResolvedEmpty && canCreate/);
  assert.match(sectionSource, /isResolvedEmpty \?[\s\S]*?Create new version[\s\S]*?: \(/);
});

test('content Version Planning keeps one cohesive surface without nested empty subsections', () => {
  assert.match(sectionSource, /Version Planning/);
  assert.match(sectionSource, /Prepare and schedule future payroll policy changes\./);
  assert.doesNotMatch(sectionSource, /<h4>Upcoming<\/h4>|<h4>Drafts<\/h4>/);
  assert.doesNotMatch(sectionSource, /No upcoming changes scheduled\.|No saved versions yet\./);
  assert.ok(sectionSource.indexOf('upcomingVersionRow') < sectionSource.indexOf('planningDraftRow'));
  assert.match(sectionSource, /className=\{styles\.versionPlanningHeader\}/);
  assert.match(sectionSource, /Create new version/);
  assert.match(sectionSource, /Scheduled update/);
  assert.doesNotMatch(sectionSource, /Version \{version\.version_number\}/);
});

test('Draft cards use quiet saved-work language, friendly summaries, Continue editing, and Discard', () => {
  assert.match(sectionSource, /draftPlanningSummary\(draft\)/);
  assert.match(sectionSource, /Saved for later/);
  assert.match(sectionSource, /Continue editing/);
  assert.match(sectionSource, />\s*Discard\s*</);
  assert.doesNotMatch(sectionSource, /Publish…|Review & publish/);
});

test('create mode uses Version language while edit mode remains an edit flow', () => {
  assert.match(modalSource, /mode === 'create' \? 'Create new version' : 'Continue version'/);
  assert.match(modalSource, /mode === 'create' \? 'Save for later' : 'Save changes'/);
  assert.match(modalSource, /Review & publish/);
  assert.match(modalSource, /Version saved for later\./);
  assert.doesNotMatch(modalSource, /Add draft schedule|Create draft/);
});

test('create modal guards the hidden confirmation message when the initial date is blank', () => {
  assert.match(modalSource, /const publishConfirmationDate = form\.planned_effective_from_date/);
  assert.match(modalSource, /message=\{`This version takes effect \$\{publishConfirmationDate\}/);
  assert.doesNotMatch(modalSource, /message=\{`This version takes effect \$\{formatIsoLong\(form\.planned_effective_from_date\)/);
});

test('create modal uses an accessible frequency selector and guards Custom frequency changes', () => {
  assert.match(modalSource, /role="radiogroup"/);
  assert.match(modalSource, /role="radio"/);
  assert.match(modalSource, /aria-checked={value === option\.value}/);
  assert.doesNotMatch(modalSource, /role="listbox"|aria-haspopup="listbox"|role="option"/);
  for (const label of ['Weekly', 'Biweekly', 'Monthly', 'Custom']) assert.match(modalSource, new RegExp(label));
  assert.match(modalSource, /frequencyChangeNeedsConfirmation/);
  assert.match(modalSource, /Change payroll frequency\?/);
  assert.match(modalSource, /Custom period details will be cleared and will need to be entered again\./);
});

test('Starting point is a real template selector with a guarded replacement path', () => {
  assert.match(modalSource, /<label className=\{styles\.label\}>Starting point<\/label>/);
  assert.match(modalSource, /<option value=\{BLANK_START_FROM\}>Start from scratch<\/option>/);
  assert.match(modalSource, /formFromStartingPoint\(form, value, versions\)/);
  assert.match(modalSource, /scheduleDirtySinceTemplate/);
  assert.match(modalSource, /Change starting point\?/);
  assert.match(modalSource, /Your current schedule edits will be replaced with the selected starting point\./);
  assert.match(modalSource, /Your current schedule edits will be cleared\./);
});

test('modal anchor changes auto-seed only an auto-managed effective date', () => {
  assert.match(modalSource, /formAfterAnchorChange\(current, iso, effectiveDateOwnership\)/);
  assert.match(modalSource, /setEffectiveDateOwnership\('manual'\)/);
  assert.match(modalSource, /planned_effective_from_date: initialForm\.planned_effective_from_date/);
});

test('boundary arrows keep accessible labels while using stronger local styling', () => {
  const boundarySource = readFileSync(
    new URL('../src/components/payroll/PayrollBoundaryDateInput.tsx', import.meta.url),
    'utf8',
  );
  const boundaryCss = readFileSync(
    new URL('../src/components/payroll/PayrollBoundaryDateInput.module.css', import.meta.url),
    'utf8',
  );
  assert.match(boundarySource, /aria-label="Next valid date"/);
  assert.match(boundarySource, /title="Previous valid date"/);
  assert.match(boundaryCss, /width: 2rem/);
  assert.match(boundaryCss, /font-weight: 700/);
  assert.match(boundaryCss, /:focus-visible/);
});

test('effective-date input uses friendly publication wording without technical details', () => {
  assert.match(modalSource, /label="This version takes effect on"/);
  assert.match(modalSource, /hint="Choose the payroll period when this version should begin\."/);
  assert.match(modalSource, /showTechnicalDetails=\{false\}/);
});

test('Version Timeline is built from published terminal versions only', () => {
  assert.match(timelineSource, /timelinePublishedVersions/);
  assert.match(timelineSource, /version\.lifecycle_state === 'Published' && version\.is_terminal/);
  assert.match(timelineSource, /version\.is_current/);
  assert.match(timelineSource, /effectiveDisplayVersionNumbers/);
  assert.doesNotMatch(panelSource, /<strong>Version \{version\.version_number\}<\/strong>/);
});

test('Payroll boundary and impact refreshes do not insert transient layout rows', () => {
  const boundarySource = readFileSync(
    new URL('../src/components/payroll/PayrollBoundaryDateInput.tsx', import.meta.url),
    'utf8',
  );
  assert.doesNotMatch(boundarySource, /Checking payroll dates/);
  assert.doesNotMatch(modalSource, /Checking impact/);
  assert.doesNotMatch(readFileSync(new URL('../src/pages/settings/payroll/PublishPanel.tsx', import.meta.url), 'utf8'), /Checking impact/);
});

test('Version Timeline is mounted below Version Planning and outside parked lower sections', () => {
  const timelineIndex = panelSource.indexOf('<VersionTimelineSection');
  const planningIndex = panelSource.indexOf('<VersionPlanningSection');
  const lowerGateIndex = panelSource.indexOf('{showLowerSections && (');
  assert.ok(timelineIndex > planningIndex);
  assert.ok(timelineIndex < lowerGateIndex);
});

test('Version Planning is mounted intentionally before the parked lower-section gate', () => {
  const planningIndex = panelSource.indexOf('<VersionPlanningSection');
  const lowerGateIndex = panelSource.indexOf('{showLowerSections && (');
  assert.ok(planningIndex > -1);
  assert.ok(lowerGateIndex > planningIndex);
  assert.match(panelSource, /const canCreateVersion = selectedPolicy != null && selectedPolicy\.status === 'Active' && canManage;/);
  assert.match(panelSource, /showLowerSections = false,/);
});

test('Version Planning keeps the old lower UI parked and does not add a second fetch path', () => {
  assert.equal((panelSource.match(/listPayrollSetupVersions\(/g) ?? []).length, 1);
  assert.equal((panelSource.match(/listPayrollSetupDrafts\(/g) ?? []).length, 1);
  for (const marker of ['title="Draft schedules"', 'title="Policy updates"', 'title="Assigned branches"', '<SectionCard title="Publish">']) {
    const markerIndex = panelSource.indexOf(marker);
    assert.ok(markerIndex > lowerGateIndex(panelSource), `${marker} must remain behind showLowerSections`);
  }
});

function lowerGateIndex(source: string): number {
  return source.indexOf('{showLowerSections && (');
}
