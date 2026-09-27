import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  frequencyNoun,
  describeBoundaryChoice,
  isChoicesCurrent,
  stepTarget,
  boundaryStatus,
} from '../src/lib/payrollBoundaryView.ts';
import type { BoundaryChoiceResponse, BoundaryChoicesResponse } from '../src/types/payrollSetup.ts';

function makeChoice(overrides: Partial<BoundaryChoiceResponse> = {}): BoundaryChoiceResponse {
  return {
    date: '2026-09-23',
    period_end_date: '2026-09-29',
    payroll_frequency: 'Week',
    custom_interval_days: null,
    relation: 'future',
    predecessor_payroll_frequency: null,
    predecessor_custom_interval_days: null,
    predecessor_period_end_date: null,
    replaces_version_id: null,
    replaces_version_number: null,
    ...overrides,
  };
}

function makeChoices(overrides: Partial<BoundaryChoicesResponse> = {}): BoundaryChoicesResponse {
  return {
    reference_date: '2026-09-01',
    requested_date: '2026-09-23',
    requested_valid: true,
    requested: makeChoice(),
    conflicts: [],
    previous: null,
    next: null,
    suggested: null,
    earliest_allowed_date: null,
    ...overrides,
  };
}

// ── frequencyNoun ────────────────────────────────────────────────────────────

test('frequencyNoun: Week/Biweek/Month map to Weekly/Biweekly/Monthly', () => {
  assert.equal(frequencyNoun('Week', null), 'Weekly');
  assert.equal(frequencyNoun('Biweek', null), 'Biweekly');
  assert.equal(frequencyNoun('Month', null), 'Monthly');
});

test('frequencyNoun: Custom with an interval -> "{n}-day"; without -> "Custom"', () => {
  assert.equal(frequencyNoun('Custom', 10), '10-day');
  assert.equal(frequencyNoun('Custom', null), 'Custom');
});

test('frequencyNoun: unknown frequency returns the raw value', () => {
  assert.equal(frequencyNoun('Fortnightly', null), 'Fortnightly');
});

// ── describeBoundaryChoice ───────────────────────────────────────────────────

test('describeBoundaryChoice: weekly current period', () => {
  const choice = makeChoice({
    date: '2026-09-23',
    period_end_date: '2026-09-29',
    payroll_frequency: 'Week',
    relation: 'current',
  });
  assert.equal(
    describeBoundaryChoice(choice, 'assignment'),
    'Weekly payroll period: Sep 23 – Sep 29 (current period)',
  );
});

test('describeBoundaryChoice: biweekly future period, no "(current period)" suffix', () => {
  const choice = makeChoice({
    date: '2026-10-07',
    period_end_date: '2026-10-20',
    payroll_frequency: 'Biweek',
    relation: 'future',
  });
  assert.equal(describeBoundaryChoice(choice, 'onboarding'), 'Biweekly payroll period: Oct 7 – Oct 20');
});

test('describeBoundaryChoice: custom interval frequency', () => {
  const choice = makeChoice({
    date: '2026-10-01',
    period_end_date: '2026-10-10',
    payroll_frequency: 'Custom',
    custom_interval_days: 10,
    relation: 'future',
  });
  assert.equal(describeBoundaryChoice(choice, 'assignment'), '10-day payroll period: Oct 1 – Oct 10');
});

test('describeBoundaryChoice: reassignment predecessor sentence, exact text', () => {
  const choice = makeChoice({
    date: '2026-10-07',
    period_end_date: '2026-10-20',
    payroll_frequency: 'Biweek',
    relation: 'future',
    predecessor_payroll_frequency: 'Week',
    predecessor_custom_interval_days: null,
    predecessor_period_end_date: '2026-10-06',
  });
  assert.equal(
    describeBoundaryChoice(choice, 'reassignment'),
    'Valid change point. Weekly period ends Oct 6. Biweekly policy begins Oct 7. ' +
      'Biweekly payroll period: Oct 7 – Oct 20',
  );
});

test('describeBoundaryChoice: reassignment context but no predecessor fields -> plain base sentence', () => {
  const choice = makeChoice({ payroll_frequency: 'Week', relation: 'future' });
  assert.equal(describeBoundaryChoice(choice, 'reassignment'), 'Weekly payroll period: Sep 23 – Sep 29');
});

test('describeBoundaryChoice: publication replacement sentence', () => {
  const choice = makeChoice({
    date: '2026-09-23',
    period_end_date: '2026-09-29',
    payroll_frequency: 'Week',
    relation: 'future',
    replaces_version_id: 501,
    replaces_version_number: 3,
  });
  assert.equal(
    describeBoundaryChoice(choice, 'publication'),
    'Weekly payroll period: Sep 23 – Sep 29 A change already starts on this date (Version 3); publishing here replaces it.',
  );
});

test('describeBoundaryChoice: replaces_version_number ignored outside publication context', () => {
  const choice = makeChoice({ payroll_frequency: 'Week', relation: 'future', replaces_version_number: 3 });
  assert.equal(describeBoundaryChoice(choice, 'assignment'), 'Weekly payroll period: Sep 23 – Sep 29');
});

// ── isChoicesCurrent / stepTarget ────────────────────────────────────────────

test('isChoicesCurrent: true only when requested_date matches value', () => {
  const choices = makeChoices({ requested_date: '2026-09-23' });
  assert.equal(isChoicesCurrent(choices, '2026-09-23'), true);
  assert.equal(isChoicesCurrent(choices, '2026-09-24'), false);
  assert.equal(isChoicesCurrent(null, '2026-09-23'), false);
});

test('stepTarget: returns the next/previous date when choices are current', () => {
  const choices = makeChoices({
    requested_date: '2026-09-23',
    previous: makeChoice({ date: '2026-09-16' }),
    next: makeChoice({ date: '2026-09-30' }),
  });
  assert.equal(stepTarget(choices, '2026-09-23', 'next'), '2026-09-30');
  assert.equal(stepTarget(choices, '2026-09-23', 'previous'), '2026-09-16');
});

test('stepTarget: null when choices are stale (requested_date does not match value)', () => {
  const choices = makeChoices({
    requested_date: '2026-09-23',
    next: makeChoice({ date: '2026-09-30' }),
  });
  assert.equal(stepTarget(choices, '2026-09-24', 'next'), null);
});

test('stepTarget: null when the neighbour itself is null', () => {
  const choices = makeChoices({ requested_date: '2026-09-23', next: null, previous: null });
  assert.equal(stepTarget(choices, '2026-09-23', 'next'), null);
  assert.equal(stepTarget(choices, '2026-09-23', 'previous'), null);
});

test('stepTarget: null when choices is null', () => {
  assert.equal(stepTarget(null, '2026-09-23', 'next'), null);
});

// ── boundaryStatus ───────────────────────────────────────────────────────────

test('boundaryStatus: loading takes priority over everything else', () => {
  assert.deepEqual(boundaryStatus(null, '2026-09-23', true), { kind: 'loading' });
  assert.deepEqual(boundaryStatus(makeChoices(), '2026-09-23', true), { kind: 'loading' });
});

test('boundaryStatus: idle when choices is null or stale', () => {
  assert.deepEqual(boundaryStatus(null, '2026-09-23', false), { kind: 'idle' });
  const stale = makeChoices({ requested_date: '2026-09-01' });
  assert.deepEqual(boundaryStatus(stale, '2026-09-23', false), { kind: 'idle' });
});

test('boundaryStatus: valid when requested_valid and requested are present', () => {
  const choices = makeChoices({
    requested_date: '2026-09-23',
    requested_valid: true,
    requested: makeChoice({ date: '2026-09-23', period_end_date: '2026-09-29', relation: 'current' }),
  });
  assert.deepEqual(boundaryStatus(choices, '2026-09-23', false), {
    kind: 'valid',
    text: 'Weekly payroll period: Sep 23 – Sep 29 (current period)',
  });
});

test('boundaryStatus: invalid with neighbours and codes, friendly text from the first conflict code', () => {
  const choices = makeChoices({
    requested_date: '2026-09-24',
    requested_valid: false,
    requested: null,
    previous: makeChoice({ date: '2026-09-23' }),
    next: makeChoice({ date: '2026-09-30' }),
    conflicts: [
      { branch_id: null, code: 'INVALID_SCHEDULE_BOUNDARY', reason: 'not a period boundary' },
      { branch_id: null, code: 'OTHER_CODE', reason: 'secondary' },
    ],
  });
  assert.deepEqual(boundaryStatus(choices, '2026-09-24', false), {
    kind: 'invalid',
    text: 'That date is not the start of a payroll period.',
    previous: '2026-09-23',
    next: '2026-09-30',
    codes: ['INVALID_SCHEDULE_BOUNDARY', 'OTHER_CODE'],
  });
});

test('boundaryStatus: invalid text never falls back to the raw code for an unknown code', () => {
  const choices = makeChoices({
    requested_date: '2026-09-24',
    requested_valid: false,
    requested: null,
    next: makeChoice({ date: '2026-09-30' }),
    conflicts: [{ branch_id: null, code: 'SOME_UNKNOWN_CODE', reason: 'n/a' }],
  });
  const status = boundaryStatus(choices, '2026-09-24', false);
  assert.equal(status.kind, 'invalid');
  assert.equal((status as { text: string }).text, "This date can't be used.");
});

test('boundaryStatus: successor conflict explains the existing scheduled update', () => {
  const choices = makeChoices({
    requested_date: '2026-09-27',
    requested_valid: false,
    requested: null,
    next: makeChoice({ date: '2026-10-04' }),
    conflicts: [{
      branch_id: null,
      code: 'SUCCESSOR_BOUNDARY_INVALID',
      reason: 'Existing scheduled update on 2026-10-10 would not start on a valid boundary under this schedule',
    }],
  });
  const status = boundaryStatus(choices, '2026-09-27', false);
  assert.equal(status.kind, 'invalid');
  assert.equal(
    (status as { text: string }).text,
    'An existing scheduled update on Oct 10, 2026 would no longer start on a valid payroll-period boundary under this schedule.',
  );
});

test('boundaryStatus: a conflict remains useful even when no nearby date exists', () => {
  const choices = makeChoices({
    requested_date: '2026-09-27',
    requested_valid: false,
    requested: null,
    previous: null,
    next: null,
    conflicts: [{
      branch_id: 10,
      code: 'PREDECESSOR_BOUNDARY_INVALID',
      reason: 'Effective date 2026-09-27 splits the predecessor payroll period',
    }],
  });
  assert.deepEqual(boundaryStatus(choices, '2026-09-27', false), {
    kind: 'none-found',
    text: 'The proposed change on Sep 27, 2026 would split the previous payroll period.',
  });
});

test('boundaryStatus: none-found when requested is invalid and there is no previous/next', () => {
  const choices = makeChoices({
    requested_date: '2026-09-24',
    requested_valid: false,
    requested: null,
    previous: null,
    next: null,
  });
  assert.deepEqual(boundaryStatus(choices, '2026-09-24', false), {
    kind: 'none-found',
    text: 'No valid date was found nearby.',
  });
});
