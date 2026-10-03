import { test } from 'node:test';
import assert from 'node:assert/strict';
import { formatGrossTotal, GROSS_UNAVAILABLE_MESSAGE } from '../src/pages/payroll/grossDisplay.ts';

test('formatGrossTotal: null gross_total (Draft/Prepared, CP-2F) -> neutral unavailable text, not "$null" or a manufactured $0', () => {
  const result = formatGrossTotal(null);
  assert.equal(result, GROSS_UNAVAILABLE_MESSAGE);
  assert.ok(!result.includes('$'), 'unavailable text must not contain a dollar sign');
  assert.ok(!result.includes('null'));
  assert.ok(!result.includes('0.00'));
});

test('formatGrossTotal: authoritative gross_total uses the configured company currency', () => {
  assert.equal(formatGrossTotal('1234.56', 'USD', 2), '$1,234.56');
});

test('formatGrossTotal: authoritative zero gross_total is a real backend value, not manufactured, and still renders', () => {
  assert.equal(formatGrossTotal('0.00', 'USD', 2), '$0.00');
});
