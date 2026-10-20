import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { describeCalendar, reconciliationWarning } from '../src/pages/people/pay-rates/payRulesView.ts';
import {
  COMPENSATION_ERROR_MESSAGES,
  compensationErrorMessage,
} from '../src/pages/people/pay-rates/payRatesErrors.ts';

const read = (name: string) => readFileSync(
  new URL(`../src/pages/people/pay-rates/${name}`, import.meta.url), 'utf8');

const weekly = {
  calendar_frequency: 'Week', calendar_anchor_start_date: '2026-01-05', calendar_custom_interval_days: null,
};

test('the captured calendar is described from frequency, anchor and custom interval', () => {
  assert.equal(describeCalendar(weekly), 'Weekly, periods anchored on 2026-01-05');
  assert.equal(describeCalendar({ ...weekly, calendar_frequency: 'Biweek' }),
    'Every 2 weeks, periods anchored on 2026-01-05');
  assert.equal(describeCalendar({ ...weekly, calendar_frequency: 'Month', calendar_anchor_start_date: '2026-01-01' }),
    'Monthly, periods anchored on 2026-01-01');
  assert.equal(describeCalendar({ ...weekly, calendar_frequency: 'Custom', calendar_custom_interval_days: 10 }),
    'Every 10 days, periods anchored on 2026-01-05');
});

test('the reconciliation warning appears only when the server reports a date', () => {
  assert.equal(
    reconciliationWarning({ calendar_reconciliation_needed_from: '2026-11-02' }),
    'Min/Max needs reconciliation from 2026-11-02: the home pay calendar changes. '
      + 'End this rule and create a new one.',
  );
  assert.equal(reconciliationWarning({ calendar_reconciliation_needed_from: null }), null);
});

test('Min/Max creation sends no calendar and the calendar is shown read-only', () => {
  const api = read('driverPayRulesApi.ts');
  const payload = api.slice(api.indexOf('export interface DriverPayRuleCreatePayload'),
    api.indexOf('export interface DriverPayRuleEndPayload'));
  assert.match(payload, /driver_id: number/);
  assert.doesNotMatch(payload, /calendar|driver_assignment_id|branch_id/);
  assert.match(api, /\/payroll\/drivers\/\$\{driverId\}\/pay-rules/);
  const tab = read('DriverPayRulesTab.tsx');
  assert.match(tab, /describeCalendar\(active\)/);
  assert.match(tab, /reconciliationWarning\(active\)/);
  assert.doesNotMatch(tab, /calendar_frequency|calendarFrequency/);
  assert.doesNotMatch(read('useDriverPayRules.ts'), /calendar/);
});

function apiFailure(status: number, detail: unknown) {
  return { response: { status, data: { detail } } };
}

test('the historical-tenure refusal maps to a clear message for 403 and 409 alike', () => {
  for (const status of [403, 409]) {
    assert.equal(
      compensationErrorMessage(apiFailure(status, { code: 'DRIVER_COMPENSATION_HISTORICAL', message: 'x' })),
      COMPENSATION_ERROR_MESSAGES.DRIVER_COMPENSATION_HISTORICAL);
  }
  assert.match(COMPENSATION_ERROR_MESSAGES.DRIVER_COMPENSATION_HISTORICAL, /already ended/);
});

test('the finalized-payroll refusal names how many periods it touches', () => {
  const one = compensationErrorMessage(apiFailure(422, {
    code: 'DRIVER_COMPENSATION_IN_FINALIZED_PERIOD', message: 'x', payroll_period_ids: [7],
  }));
  assert.match(one, /finalized payroll \(1 payroll period\)/);
  const two = compensationErrorMessage(apiFailure(422, {
    code: 'DRIVER_COMPENSATION_IN_FINALIZED_PERIOD', message: 'x', payroll_period_ids: [7, 8],
  }));
  assert.match(two, /\(2 payroll periods\)/);
  assert.match(compensationErrorMessage(apiFailure(422, {
    code: 'DRIVER_COMPENSATION_IN_FINALIZED_PERIOD', message: 'x',
  })), /^This change touches finalized payroll and cannot be made/);
});

test('every schedule code and the calendar-unavailable code have a message', () => {
  for (const code of [
    'SCHEDULE_CHANGED', 'SCHEDULE_WINDOW_OVERLAP', 'SCHEDULE_NOT_PENDING', 'SCHEDULE_INCOMPLETE',
    'SCHEDULE_IMMUTABLE', 'SCHEDULE_VALUES_IMMUTABLE', 'SCHEDULE_INVALID_TRANSITION', 'SCHEDULE_NOT_FOUND',
    'PAY_RULE_CALENDAR_UNAVAILABLE',
  ]) {
    assert.ok(COMPENSATION_ERROR_MESSAGES[code], code);
    assert.equal(compensationErrorMessage(apiFailure(409, { code, message: 'raw' })),
      COMPENSATION_ERROR_MESSAGES[code]);
  }
});

test('unknown codes keep the server message; plain 403 and validation errors stay readable', () => {
  assert.equal(compensationErrorMessage(apiFailure(409, { code: 'SOMETHING_NEW', message: 'Server says no.' })),
    'Server says no.');
  assert.equal(compensationErrorMessage(apiFailure(422, 'Overlapping rule.')), 'Overlapping rule.');
  assert.equal(compensationErrorMessage(apiFailure(403, undefined)),
    "You do not have permission to change this Driver's pay rates.");
  assert.equal(compensationErrorMessage(apiFailure(422, [{ msg: 'Field required' }])), 'Field required');
  assert.equal(compensationErrorMessage(new Error('network')), 'An unexpected error occurred.');
});
