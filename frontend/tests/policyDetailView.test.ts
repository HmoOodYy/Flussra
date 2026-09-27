import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  currentVersion,
  effectiveDisplayVersionNumbers,
  hasPublishedVersion,
  policyScheduleFields,
  resolveSelectedPolicyId,
  timelineDateRange,
  timelinePublishedVersions,
  timelineScheduleSummary,
  startingPointLabel,
  usageStateLabel,
} from '../src/pages/settings/payroll/policyDetailView.ts';
import type { ScheduleResponse, VersionResponse } from '../src/types/payrollSetup';

function makeVersion(overrides: Partial<VersionResponse> = {}, schedule: Partial<ScheduleResponse> = {}): VersionResponse {
  return {
    setup_id: 1,
    version_id: 10,
    lifecycle_state: 'Published',
    version_number: 1,
    effective_from_date: '2026-01-04',
    effective_to_date: null,
    schedule: {
      payroll_frequency: 'Week',
      anchor_start_date: '2026-01-04',
      custom_interval_days: null,
      normal_days_off_mask: 0,
      ...schedule,
    },
    config_hash: 'h',
    replaces_version_id: null,
    replaced_by_version_id: null,
    is_terminal: true,
    is_current: true,
    ...overrides,
  };
}

// ── currentVersion ───────────────────────────────────────────────────────────

test('currentVersion picks the version the backend flags is_current, not the last one', () => {
  const past = makeVersion({ version_id: 1, is_current: true });
  const future = makeVersion({ version_id: 2, is_current: false, effective_from_date: '2099-01-04' });
  assert.equal(currentVersion([past, future], 1)?.version_id, 1);
});

test('currentVersion is null when nothing is current (first version starts in the future) — never falls forward', () => {
  const future = makeVersion({ version_id: 2, is_current: false, effective_from_date: '2099-01-04' });
  assert.equal(currentVersion([future], 1), null);
  assert.equal(currentVersion([], 1), null);
});

test('currentVersion ignores versions belonging to another setup (stale data from a previous selection)', () => {
  const other = makeVersion({ setup_id: 2, version_id: 5, is_current: true });
  assert.equal(currentVersion([other], 1), null);
});

// ── hasPublishedVersion (Company Default prerequisite) ───────────────────────

test('hasPublishedVersion: no versions -> false (default unavailable)', () => {
  assert.equal(hasPublishedVersion([], 1), false);
});

test('hasPublishedVersion: one or more Published versions -> true', () => {
  assert.equal(hasPublishedVersion([makeVersion()], 1), true);
  assert.equal(hasPublishedVersion([makeVersion({ version_id: 1 }), makeVersion({ version_id: 2 })], 1), true);
});

test('hasPublishedVersion: a future Published version (not current) still counts', () => {
  const future = makeVersion({ is_current: false, effective_from_date: '2099-01-04' });
  assert.equal(hasPublishedVersion([future], 1), true);
});

test('hasPublishedVersion: Draft versions never count, and other setups are ignored', () => {
  assert.equal(hasPublishedVersion([makeVersion({ lifecycle_state: 'Draft' })], 1), false);
  assert.equal(hasPublishedVersion([makeVersion({ setup_id: 2 })], 1), false);
});

// ── usageStateLabel ─────────────────────────────────────────────────────────

test('usageStateLabel maps the Step 2 derived state to friendly detail wording', () => {
  assert.equal(usageStateLabel('Active'), 'In use');
  assert.equal(usageStateLabel('Inactive'), 'Not in use');
  assert.equal(usageStateLabel('Archived'), 'Archived');
});

// ── policyScheduleFields ─────────────────────────────────────────────────────

test('policyScheduleFields: no current version gives null (empty state, not placeholder values)', () => {
  assert.equal(policyScheduleFields(null), null);
});

test('policyScheduleFields: cadence labels', () => {
  assert.equal(policyScheduleFields(makeVersion({}, { payroll_frequency: 'Week' }))?.frequency, 'Weekly');
  assert.equal(policyScheduleFields(makeVersion({}, { payroll_frequency: 'Biweek' }))?.frequency, 'Biweekly');
  assert.equal(policyScheduleFields(makeVersion({}, { payroll_frequency: 'Month' }))?.frequency, 'Monthly');
  assert.equal(policyScheduleFields(makeVersion({}, { payroll_frequency: 'Custom', custom_interval_days: 10 }))?.frequency, 'Custom');
});

test('policyScheduleFields: first payroll period start is the friendly long date of the same version anchor', () => {
  assert.equal(policyScheduleFields(makeVersion({}, { anchor_start_date: '2026-10-07' }))?.firstPeriodStart, 'Oct 7, 2026');
});

test('policyScheduleFields: invalid anchor renders a dash rather than garbage', () => {
  assert.equal(policyScheduleFields(makeVersion({}, { anchor_start_date: 'not-a-date' }))?.firstPeriodStart, '—');
});

test('policyScheduleFields: interval exists only for Custom cadence', () => {
  assert.equal(policyScheduleFields(makeVersion({}, { payroll_frequency: 'Custom', custom_interval_days: 14 }))?.interval, '14 days');
  assert.equal(policyScheduleFields(makeVersion({}, { payroll_frequency: 'Custom', custom_interval_days: 1 }))?.interval, '1 day');
  assert.equal(policyScheduleFields(makeVersion({}, { payroll_frequency: 'Week', custom_interval_days: 7 }))?.interval, null);
  assert.equal(policyScheduleFields(makeVersion({}, { payroll_frequency: 'Biweek' }))?.interval, null);
  assert.equal(policyScheduleFields(makeVersion({}, { payroll_frequency: 'Month' }))?.interval, null);
  assert.equal(policyScheduleFields(makeVersion({}, { payroll_frequency: 'Custom', custom_interval_days: null }))?.interval, null);
});

test('policyScheduleFields: regular days off uses full weekday names, or "No regular days off"', () => {
  assert.equal(policyScheduleFields(makeVersion({}, { normal_days_off_mask: 0b1000001 }))?.daysOff, 'Sunday, Saturday');
  assert.equal(policyScheduleFields(makeVersion({}, { normal_days_off_mask: 0b0100000 }))?.daysOff, 'Friday');
  assert.equal(policyScheduleFields(makeVersion({}, { normal_days_off_mask: 0 }))?.daysOff, 'No regular days off');
});

test('timelinePublishedVersions keeps current and historical terminal Published versions, newest first', () => {
  const historical = makeVersion({
    version_id: 1,
    version_number: 1,
    effective_from_date: '2025-01-04',
    effective_to_date: '2026-01-03',
    is_current: false,
  });
  const current = makeVersion({ version_id: 2, version_number: 2, effective_from_date: '2026-01-04', is_current: true });
  const future = makeVersion({ version_id: 3, version_number: 3, effective_from_date: '2026-10-04', is_current: false });
  const replaced = makeVersion({ version_id: 4, version_number: 4, effective_from_date: '2026-10-04', is_current: false, is_terminal: false });

  assert.deepEqual(
    timelinePublishedVersions([historical, current, future, replaced]).map((version) => version.version_id),
    [2, 1],
  );
  assert.equal(timelineDateRange(current), 'Jan 4, 2026 — Present');
  assert.equal(timelineDateRange(historical), 'Jan 4, 2025 — Jan 3, 2026');
  assert.match(timelineScheduleSummary(current), /Weekly/);
});

test('timelinePublishedVersions is empty for a future-only response with no backend current version', () => {
  const future = makeVersion({ version_id: 3, effective_from_date: '2090-01-04', is_current: false });
  assert.deepEqual(timelinePublishedVersions([future]), []);
});

test('effective display numbering follows effective chronology, not internal publication order', () => {
  const earliest = makeVersion({
    version_id: 11,
    version_number: 8,
    effective_from_date: '2026-01-04',
    effective_to_date: '2026-06-30',
    is_current: false,
  });
  const current = makeVersion({
    version_id: 12,
    version_number: 3,
    effective_from_date: '2026-07-01',
    is_current: true,
  });
  const future = makeVersion({
    version_id: 13,
    version_number: 9,
    effective_from_date: '2026-10-01',
    is_current: false,
  });
  const replacedSameDate = makeVersion({
    version_id: 14,
    version_number: 10,
    effective_from_date: '2026-10-01',
    is_current: false,
    is_terminal: false,
  });
  const numbers = effectiveDisplayVersionNumbers([earliest, current, future, replacedSameDate]);

  assert.equal(numbers.get(11), 1);
  assert.equal(numbers.get(12), 2);
  assert.equal(numbers.has(13), false);
  assert.equal(numbers.has(14), false);
  assert.equal(startingPointLabel(earliest, [earliest, current, future]), 'Version 1 — effective Jan 4, 2026');
  assert.equal(startingPointLabel(current, [earliest, current, future]), 'Version 2 — current schedule');
  assert.equal(startingPointLabel(future, [earliest, current, future]), 'Scheduled update — starts Oct 1, 2026');
});

test('future-only policies have no display Version number and use the scheduled label', () => {
  const firstFuture = makeVersion({ version_id: 20, version_number: 4, is_current: false, effective_from_date: '2026-10-01' });
  const secondFuture = makeVersion({ version_id: 21, version_number: 5, is_current: false, effective_from_date: '2026-11-01' });
  const numbers = effectiveDisplayVersionNumbers([firstFuture, secondFuture]);
  assert.equal(numbers.size, 0);
  assert.equal(startingPointLabel(firstFuture, [firstFuture, secondFuture]), 'Scheduled update — starts Oct 1, 2026');
  assert.equal(startingPointLabel(secondFuture, [firstFuture, secondFuture]), 'Scheduled update — starts Nov 1, 2026');
});

test('timelinePublishedVersions never reads the browser wall clock', async () => {
  const source = readFileSync(new URL('../src/pages/settings/payroll/policyDetailView.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /new Date\(|Date\.now|Date\.UTC|Date\.parse/);
});

// ── resolveSelectedPolicyId ──────────────────────────────────────────────────

const candidates = [{ setup_id: 3 }, { setup_id: 7 }, { setup_id: 9 }];

test('resolveSelectedPolicyId keeps the param when it names a visible candidate', () => {
  assert.equal(resolveSelectedPolicyId('7', candidates), 7);
});

test('resolveSelectedPolicyId falls back to the first candidate when the param is hidden, missing or garbage', () => {
  assert.equal(resolveSelectedPolicyId('99', candidates), 3);
  assert.equal(resolveSelectedPolicyId(null, candidates), 3);
  assert.equal(resolveSelectedPolicyId('abc', candidates), 3);
});

test('resolveSelectedPolicyId is null when nothing is visible', () => {
  assert.equal(resolveSelectedPolicyId('7', []), null);
  assert.equal(resolveSelectedPolicyId(null, []), null);
});
