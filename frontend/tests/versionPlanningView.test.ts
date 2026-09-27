import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import type { DraftResponse, ScheduleResponse, VersionResponse } from '../src/types/payrollSetup.ts';
import {
  draftPlanningSummary,
  planningDateParts,
  planningDaysOffLabel,
  upcomingPublishedVersions,
} from '../src/pages/settings/payroll/versionPlanningView.ts';

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
    config_hash: 'hash',
    replaces_version_id: null,
    replaced_by_version_id: null,
    is_terminal: true,
    is_current: false,
    ...overrides,
  };
}

function makeDraft(overrides: Partial<DraftResponse> = {}): DraftResponse {
  return {
    setup_id: 1,
    version_id: 50,
    lifecycle_state: 'Draft',
    payroll_frequency: 'Biweek',
    anchor_start_date: '2026-02-01',
    custom_interval_days: null,
    normal_days_off_mask: 65,
    created_at_utc: '2026-01-01T00:00:00Z',
    discarded_at_utc: null,
    ...overrides,
  };
}

test('upcomingPublishedVersions excludes the current terminal version and historical terminal versions', () => {
  const historical = makeVersion({ version_id: 1, version_number: 1, effective_from_date: '2025-01-05' });
  const current = makeVersion({ version_id: 2, version_number: 2, effective_from_date: '2026-01-04', is_current: true });
  const future = makeVersion({ version_id: 3, version_number: 3, effective_from_date: '2026-10-04' });

  assert.deepEqual(upcomingPublishedVersions([historical, current, future]).map((v) => v.version_id), [3]);
});

test('upcomingPublishedVersions excludes non-terminal replaced versions from the main Upcoming list', () => {
  const current = makeVersion({ version_id: 2, is_current: true });
  const replaced = makeVersion({ version_id: 3, effective_from_date: '2026-10-04', is_terminal: false });
  const terminalReplacement = makeVersion({ version_id: 4, effective_from_date: '2026-10-04', version_number: 4 });

  assert.deepEqual(upcomingPublishedVersions([current, replaced, terminalReplacement]).map((v) => v.version_id), [4]);
});

test('upcomingPublishedVersions treats a no-current response as the backend future-only state', () => {
  const futureOne = makeVersion({ version_id: 3, effective_from_date: '2090-01-04' });
  const futureTwo = makeVersion({ version_id: 4, effective_from_date: '2090-10-04' });

  assert.deepEqual(upcomingPublishedVersions([futureOne, futureTwo]).map((v) => v.version_id), [3, 4]);
});

test('planningDateParts and planningDaysOffLabel provide the friendly Upcoming card display', () => {
  assert.deepEqual(planningDateParts('2026-10-21'), {
    month: 'Oct',
    day: '21',
    year: '2026',
    long: 'Oct 21, 2026',
  });
  assert.equal(planningDaysOffLabel(65), 'Sunday & Saturday off');
  assert.equal(planningDaysOffLabel(0), 'No regular days off');
});

test('draftPlanningSummary distinguishes complete and incomplete saved work', () => {
  assert.match(draftPlanningSummary(makeDraft()), /Biweekly/);
  assert.match(draftPlanningSummary(makeDraft({ payroll_frequency: null })), /^Incomplete schedule$/);
  assert.match(
    draftPlanningSummary(makeDraft({ payroll_frequency: 'Custom', custom_interval_days: null })),
    /^Incomplete schedule$/,
  );
});

test('Version Planning view helper never reads the browser wall clock', () => {
  const source = readFileSync(new URL('../src/pages/settings/payroll/versionPlanningView.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /new Date\(|Date\.now|Date\.UTC|Date\.parse/);
});
