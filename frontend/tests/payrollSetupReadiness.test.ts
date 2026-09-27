import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  READINESS_REASONS,
  describeReadiness,
  WEEKDAYS,
  maskToDays,
  daysToMask,
  toggleDay,
  daysOffLabel,
  frequencyLabel,
  scheduleSummary,
  friendlyScheduleSummary,
  MAX_NORMAL_DAYS_OFF,
  canAddDayOff,
} from '../src/lib/payrollSetupReadiness.ts';
import type { ScheduleResponse } from '../src/types/payrollSetup.ts';

// ── G/H/I: readiness reasons ─────────────────────────────────────────────────

const EXPECTED_REASONS: Record<string, { label: string; description: string }> = {
  READY: { label: 'Ready', description: 'Schedule resolves for the next payroll period.' },
  NO_COMPANY_DEFAULT: {
    label: 'No company default',
    description: 'No default Setup, so nothing was assigned.',
  },
  NO_ASSIGNMENT: {
    label: 'Not assigned',
    description: 'No assignment covers the next payroll period.',
  },
  NO_PUBLISHED_VERSION: {
    label: 'No published version',
    description: 'The assigned Setup has no published Version for that date.',
  },
  AUTHORITY_BOUNDARY_CONFLICT: {
    label: 'Boundary conflict',
    description: 'The next period would cross a scheduled Setup/Version change.',
  },
  SETUP_NOT_ACTIVE: { label: 'Setup not active', description: 'The assigned Setup is archived.' },
  INVALID_SCHEDULE_BOUNDARY: {
    label: 'Invalid period start',
    description: 'The next start is not a period boundary under the schedule.',
  },
  BRANCH_NOT_OPERATIONAL: {
    label: 'Branch not operational',
    description: 'The Branch or Company is not active.',
  },
};

test('G. every one of the 8 known reason codes: known true, label/description match the plan table', () => {
  const codes = Object.keys(EXPECTED_REASONS);
  assert.equal(codes.length, 8);
  for (const code of codes) {
    assert.deepEqual(READINESS_REASONS[code], EXPECTED_REASONS[code], `map entry for ${code}`);
    const result = describeReadiness(code);
    assert.equal(result.code, code);
    assert.equal(result.known, true);
    assert.equal(result.label, EXPECTED_REASONS[code].label);
    assert.equal(result.description, EXPECTED_REASONS[code].description);
  }
});

test('H. unknown code -> label falls back to the code, known false, isReady false', () => {
  const result = describeReadiness('SOMETHING_NEW');
  assert.deepEqual(result, {
    code: 'SOMETHING_NEW',
    label: 'SOMETHING_NEW',
    description: null,
    known: false,
    isReady: false,
  });
});

test('H2. inherited Object.prototype keys are not treated as known reason codes', () => {
  for (const code of ['constructor', 'toString', '__proto__', 'hasOwnProperty']) {
    assert.deepEqual(describeReadiness(code), {
      code,
      label: code,
      description: null,
      known: false,
      isReady: false,
    });
  }
});

test('I. isReady is true only for READY, false for all 7 other known codes', () => {
  assert.equal(describeReadiness('READY').isReady, true);
  const others = Object.keys(EXPECTED_REASONS).filter((c) => c !== 'READY');
  assert.equal(others.length, 7);
  for (const code of others) {
    assert.equal(describeReadiness(code).isReady, false, `${code} must not be ready`);
  }
});

// ── J/K/L/M/N: weekday mask ─────────────────────────────────────────────────

test('WEEKDAYS is Sun(0)..Sat(6) in order, matching the backend bit convention', () => {
  assert.deepEqual(
    WEEKDAYS.map((d) => d.bit),
    [0, 1, 2, 3, 4, 5, 6],
  );
  assert.deepEqual(
    WEEKDAYS.map((d) => d.short),
    ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'],
  );
});

test('J. Sunday: daysToMask([0]) === 1, maskToDays(1) === [0]', () => {
  assert.equal(daysToMask([0]), 1);
  assert.deepEqual(maskToDays(1), [0]);
});

test('K. Saturday: daysToMask([6]) === 64, maskToDays(64) === [6]', () => {
  assert.equal(daysToMask([6]), 64);
  assert.deepEqual(maskToDays(64), [6]);
});

test('L. empty mask: maskToDays(0) === [], daysToMask([]) === 0, daysOffLabel(0) === "None"', () => {
  assert.deepEqual(maskToDays(0), []);
  assert.equal(daysToMask([]), 0);
  assert.equal(daysOffLabel(0), 'None');
});

test('M. multi-day round trip [0,3,6] <-> 73, and daysOffLabel(65) === "Sun, Sat"', () => {
  assert.equal(daysToMask([0, 3, 6]), 73);
  assert.deepEqual(maskToDays(73), [0, 3, 6]);
  assert.equal(daysOffLabel(65), 'Sun, Sat');
});

test('N. no maximum-days rule: all 7 days can be set, toggleDay round-trips, out-of-range throws RangeError', () => {
  assert.equal(daysToMask([0, 1, 2, 3, 4, 5, 6]), 127);
  assert.equal(maskToDays(127).length, 7);

  let mask = 0;
  mask = toggleDay(mask, 3);
  assert.equal(mask, 8);
  mask = toggleDay(mask, 3);
  assert.equal(mask, 0);

  assert.throws(() => daysToMask([7]), RangeError);
  assert.throws(() => daysToMask([-1]), RangeError);

  assert.throws(() => toggleDay(0, 7), RangeError);
  assert.throws(() => toggleDay(0, -1), RangeError);
  assert.throws(() => toggleDay(0, 1.5), RangeError);
});

// ── frequencyLabel ───────────────────────────────────────────────────────────

test('frequencyLabel: unknown value returns the raw value unchanged', () => {
  assert.equal(frequencyLabel('Fortnightly'), 'Fortnightly');
});

// ── O/P/Q/R: scheduleSummary ─────────────────────────────────────────────────

function makeSchedule(overrides: Partial<ScheduleResponse> = {}): ScheduleResponse {
  return {
    payroll_frequency: 'Week',
    anchor_start_date: '2026-01-04',
    custom_interval_days: null,
    normal_days_off_mask: 65, // Sun, Sat
    ...overrides,
  };
}

test('O. scheduleSummary: Week -> "Weekly · anchor ... · days off: ..."', () => {
  const schedule = makeSchedule({ payroll_frequency: 'Week' });
  assert.equal(scheduleSummary(schedule), 'Weekly · anchor 2026-01-04 · days off: Sun, Sat');
});

test('P. scheduleSummary: Biweek -> "Biweekly · anchor ... · days off: ..."', () => {
  const schedule = makeSchedule({ payroll_frequency: 'Biweek' });
  assert.equal(scheduleSummary(schedule), 'Biweekly · anchor 2026-01-04 · days off: Sun, Sat');
});

test('Q. scheduleSummary: Month -> "Monthly · anchor ... · days off: ..."', () => {
  const schedule = makeSchedule({ payroll_frequency: 'Month' });
  assert.equal(scheduleSummary(schedule), 'Monthly · anchor 2026-01-04 · days off: Sun, Sat');
});

test('R. scheduleSummary: Custom with interval 10 -> "Every 10 days · anchor ... · days off: ..."', () => {
  const schedule = makeSchedule({ payroll_frequency: 'Custom', custom_interval_days: 10 });
  assert.equal(scheduleSummary(schedule), 'Every 10 days · anchor 2026-01-04 · days off: Sun, Sat');
});

test('R2. scheduleSummary: Custom with null interval -> "Custom (interval missing) · anchor ... · days off: ..."', () => {
  const schedule = makeSchedule({ payroll_frequency: 'Custom', custom_interval_days: null });
  assert.equal(
    scheduleSummary(schedule),
    'Custom (interval missing) · anchor 2026-01-04 · days off: Sun, Sat',
  );
});

test('scheduleSummary: no days off -> "days off: None"', () => {
  const schedule = makeSchedule({ normal_days_off_mask: 0 });
  assert.equal(scheduleSummary(schedule), 'Weekly · anchor 2026-01-04 · days off: None');
});

// ── MAX_NORMAL_DAYS_OFF / canAddDayOff (frontend guidance only) ────────────

test('MAX_NORMAL_DAYS_OFF is 2', () => {
  assert.equal(MAX_NORMAL_DAYS_OFF, 2);
});

test('canAddDayOff: true below the max, false at or above it', () => {
  assert.equal(canAddDayOff(0), true);
  assert.equal(canAddDayOff(daysToMask([0])), true);
  assert.equal(canAddDayOff(daysToMask([0, 6])), false);
  assert.equal(canAddDayOff(127), false);
});

// ── friendlyScheduleSummary (Unit A) ────────────────────────────────────────

test('friendlyScheduleSummary: Weekly, names the anchor weekday, no raw anchor date', () => {
  const schedule = makeSchedule({
    payroll_frequency: 'Week',
    anchor_start_date: '2026-09-23', // Wednesday
    normal_days_off_mask: 65, // Sun, Sat
  });
  assert.equal(
    friendlyScheduleSummary(schedule),
    'Weekly · periods start Wednesday · days off: Sun, Sat',
  );
  assert.doesNotMatch(friendlyScheduleSummary(schedule), /2026-09-23/);
});

test('friendlyScheduleSummary: Custom shows "{n}-day" and never "(interval missing)"', () => {
  const schedule = makeSchedule({
    payroll_frequency: 'Custom',
    custom_interval_days: 10,
    anchor_start_date: '2026-09-23',
    normal_days_off_mask: 0,
  });
  assert.equal(
    friendlyScheduleSummary(schedule),
    '10-day · periods start Wednesday · days off: None',
  );

  const missingInterval = makeSchedule({ payroll_frequency: 'Custom', custom_interval_days: null });
  assert.doesNotMatch(friendlyScheduleSummary(missingInterval), /interval missing/);
});

test('friendlyScheduleSummary: an invalid/incomplete anchor date omits the weekday segment, not a raw fragment', () => {
  const schedule = makeSchedule({ payroll_frequency: 'Month', anchor_start_date: '' });
  assert.equal(friendlyScheduleSummary(schedule), 'Monthly · days off: Sun, Sat');
});
