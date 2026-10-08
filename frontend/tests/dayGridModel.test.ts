import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { columnKey, quantityInputProps, quantityTotalViews } from '../src/pages/payroll/dayGridModel.ts';
import type { DayGridColumn } from '../src/types/payroll.ts';

const read = (path: string) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

function column(id: number, overrides: Partial<DayGridColumn> = {}): DayGridColumn {
  return {
    payroll_period_definition_id: id,
    pay_definition_id: id + 100,
    definition_code: `CODE_${id}`,
    label: `Item ${id}`,
    input_type: 'Decimal',
    unit: 'unit',
    calculation_method: 'PerUnit',
    ...overrides,
  };
}

test('a column is keyed by its period definition id, never by code or label', () => {
  const a = column(7, { definition_code: 'SAME', label: 'Same' });
  const b = column(8, { definition_code: 'SAME', label: 'Same' });
  assert.equal(columnKey(a), '7');
  assert.notEqual(columnKey(a), columnKey(b));
});

test('input constraints follow the frozen InputType', () => {
  assert.deepEqual(quantityInputProps(column(1, { input_type: 'WholeNumber' })), { step: 1, min: 0 });
  assert.deepEqual(quantityInputProps(column(2, { input_type: 'Decimal' })), { step: 'any', min: 0 });
});

test('quantity totals are generic per definition, in column order, defaulting to zero', () => {
  const columns = [column(2, { label: 'Second', unit: null }), column(1, { label: 'First' })];
  const totals = quantityTotalViews(columns, {
    quantity_totals: [{ payroll_period_definition_id: 1, quantity: '12.5' }],
  } as never);
  assert.deepEqual(totals, [
    { key: '2', label: 'Second', unit: null, quantity: '0' },
    { key: '1', label: 'First', unit: 'unit', quantity: '12.5' },
  ]);
});

test('the Day Grid has no hours, miles or pay-item semantics', () => {
  const sources = [
    'src/pages/payroll/dayGridModel.ts',
    'src/pages/payroll/WorkTotals.tsx',
    'src/pages/payroll/PayrollEntryDialog.tsx',
  ].map(read).join('\n');
  assert.doesNotMatch(sources, /pay_item_code|pay_item_id|is_time|total_hours|total_miles/);
  assert.doesNotMatch(sources, /['"`](HOURS|MILES)['"`]/);
  assert.doesNotMatch(sources, /line_type/);
});

test('Day Grid saves are keyed by the period definition id', () => {
  const entry = read('src/pages/payroll/PayrollEntryDialog.tsx');
  assert.match(entry, /columnKey\(/);
  assert.doesNotMatch(entry, /values\[[^\]]*definition_code/);
});

test('period pages carry no PayItem identity', () => {
  const types = read('src/types/payroll.ts');
  assert.match(types, /payroll_period_definition_id/);
  const detail = read('src/pages/payroll/PeriodDetailPage.tsx');
  assert.doesNotMatch(detail, /pay_item_code|PayItem/);
});
