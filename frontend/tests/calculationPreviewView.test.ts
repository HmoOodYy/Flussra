import test from 'node:test';
import assert from 'node:assert/strict';
import { minMaxNote } from '../src/pages/payroll/calculationPreviewView.ts';
import type { CalculationPreviewMinMax } from '../src/types/payroll/calculation.ts';

function minmax(overrides: Partial<CalculationPreviewMinMax>): CalculationPreviewMinMax {
  return {
    status: 'APPLIED', proration_version: 'minmax-proration-v1', numerator: 10, denominator: 10,
    base_amount: '50.00', minimum_rule_id: 1, minimum_rule_amount: '100.0000', prorated_minimum: '100.0000',
    maximum_rule_id: null, maximum_rule_amount: null, prorated_maximum: null, ...overrides,
  };
}

test('a top-up for a full period is just a Minimum top-up', () => {
  assert.equal(minMaxNote({ minmax: minmax({}), minimum_adjustment: '50.00', maximum_adjustment: '0.00' }), 'Minimum top-up');
});

test('a top-up for a partial period shows the whole-day proration', () => {
  assert.equal(
    minMaxNote({ minmax: minmax({ numerator: 7 }), minimum_adjustment: '20.00', maximum_adjustment: '0' }),
    'Minimum top-up, prorated 7/10 days',
  );
});

test('a cap shows the same proration', () => {
  assert.equal(
    minMaxNote({
      minmax: minmax({ numerator: 3, denominator: 5, maximum_rule_id: 2, minimum_rule_id: null }),
      minimum_adjustment: '0.00', maximum_adjustment: '-12.50',
    }),
    'Maximum cap, prorated 3/5 days',
  );
});

test('rules that changed nothing show nothing', () => {
  assert.equal(minMaxNote({ minmax: minmax({}), minimum_adjustment: '0.00', maximum_adjustment: '0.00' }), null);
  assert.equal(minMaxNote({ minmax: minmax({ status: 'NO_RULE', minimum_rule_id: null }), minimum_adjustment: '0', maximum_adjustment: '0' }), null);
  assert.equal(minMaxNote({ minmax: null, minimum_adjustment: '0', maximum_adjustment: '0' }), null);
  assert.equal(minMaxNote({ minimum_adjustment: '5', maximum_adjustment: '0' }), null);
});

test('a driver with no eligible scheduled day is told the rules were not applied', () => {
  assert.equal(
    minMaxNote({ minmax: minmax({ status: 'NO_ELIGIBLE_SCHEDULED_DAYS', numerator: 0 }), minimum_adjustment: '0', maximum_adjustment: '0' }),
    'Minimum/Maximum not applied: no eligible scheduled days in this period',
  );
});
