import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  MAX_VISIBLE_PAY_RULES,
  buildPayRuleImpactView,
  payRuleTypeLabel,
} from '../src/pages/settings/payroll/payRuleImpactView.ts';
import type { AffectedPayRuleResponse } from '../src/types/payrollSetup.ts';

function rule(overrides: Partial<AffectedPayRuleResponse> = {}): AffectedPayRuleResponse {
  return {
    driver_id: 1,
    driver_name: 'Ada Weekly',
    driver_number: 'D-100',
    branch_id: 10,
    branch_name: 'North',
    rule_id: 1,
    rule_type: 'MinimumPay',
    calendar_frequency: 'Week',
    calendar_custom_interval_days: null,
    calendar_anchor_start_date: '2026-01-05',
    effective_from: '2026-01-05',
    effective_to: null,
    ...overrides,
  };
}

test('no affected rules means no warning', () => {
  assert.equal(buildPayRuleImpactView([]), null);
  assert.equal(buildPayRuleImpactView(undefined), null);
  assert.equal(buildPayRuleImpactView(null), null);
});

test('the warning counts drivers, not rules, and says what blocks payroll', () => {
  const view = buildPayRuleImpactView([
    rule({ rule_id: 1, rule_type: 'MinimumPay' }),
    rule({ rule_id: 2, rule_type: 'MaximumPay' }),
    rule({ rule_id: 3, driver_id: 2, driver_name: 'Bo Monthly', driver_number: null }),
  ]);
  assert.ok(view);
  assert.equal(view.driverCount, 2);
  assert.equal(
    view.message,
    "2 drivers have minimum or maximum pay set for the old calendar. Their payroll can't be " +
      'submitted until those rules are ended and set again for the new calendar.',
  );
  assert.equal(view.rows.length, 3);
});

test('a single driver reads naturally', () => {
  const view = buildPayRuleImpactView([rule()]);
  assert.ok(view);
  assert.match(view.message, /^1 driver has minimum or maximum pay set for the old calendar\./);
});

test('rows name the driver, branch, rule type and the calendar the rule was set for', () => {
  const view = buildPayRuleImpactView([
    rule(),
    rule({
      rule_id: 2, driver_id: 2, driver_name: 'Cy Custom', driver_number: null,
      rule_type: 'MaximumPay', calendar_frequency: 'Custom', calendar_custom_interval_days: 10,
    }),
  ]);
  assert.ok(view);
  assert.deepEqual(view.rows.map(({ driver, branch, ruleType, calendar }) => ({ driver, branch, ruleType, calendar })), [
    { driver: 'Ada Weekly (D-100)', branch: 'North', ruleType: 'Minimum pay', calendar: 'weekly calendar' },
    { driver: 'Cy Custom', branch: 'North', ruleType: 'Maximum pay', calendar: '10-day calendar' },
  ]);
});

test('the list is compact and says how many more there are', () => {
  const many = Array.from({ length: MAX_VISIBLE_PAY_RULES + 3 }, (_, i) =>
    rule({ rule_id: i + 1, driver_id: i + 1, driver_name: `Driver ${i + 1}` }));
  const view = buildPayRuleImpactView(many);
  assert.ok(view);
  assert.equal(view.rows.length, MAX_VISIBLE_PAY_RULES);
  assert.equal(view.hiddenCount, 3);
  assert.equal(view.driverCount, MAX_VISIBLE_PAY_RULES + 3);
  assert.equal(new Set(view.rows.map((row) => row.key)).size, MAX_VISIBLE_PAY_RULES);
});

test('rule types have plain labels and unknown types pass through', () => {
  assert.equal(payRuleTypeLabel('MinimumPay'), 'Minimum pay');
  assert.equal(payRuleTypeLabel('MaximumPay'), 'Maximum pay');
  assert.equal(payRuleTypeLabel('Other'), 'Other');
});
