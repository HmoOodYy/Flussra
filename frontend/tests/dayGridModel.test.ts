import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { buildSaveRowFromGrid, columnKey, quantityInputProps, quantityTotalViews } from '../src/pages/payroll/entry/dayGridModel.ts';
import type { DayGridColumn, DayGridResponse } from '../src/types/payroll/entry.ts';

const read = (path: string) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

// The entry page: its orchestration, tables, day selector, header and driver panel, plus its pure models and reads.
const entryPageComponents = [
  'src/pages/payroll/entry/PayrollEntryPage.tsx',
  'src/pages/payroll/entry/DayEntryTable.tsx',
  'src/pages/payroll/entry/DaySelector.tsx',
  'src/pages/payroll/entry/PeriodEntryHeader.tsx',
  'src/pages/payroll/entry/WeekTable.tsx',
  'src/pages/payroll/entry/DriverPanel.tsx',
  'src/pages/payroll/entry/DriverIdentity.tsx',
  'src/pages/payroll/entry/EntryToolbar.tsx',
];
const entryPageSources = [
  ...entryPageComponents,
  'src/pages/payroll/entry/dayEntryPageModel.ts',
  'src/pages/payroll/entry/usePeriodPreview.ts',
  'src/pages/payroll/entry/usePeriodOverview.ts',
  'src/pages/payroll/entry/periodOverviewModel.ts',
];

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
  assert.deepEqual(quantityInputProps(column(1, { input_type: 'WholeNumber' })), { inputMode: 'numeric' });
  assert.deepEqual(quantityInputProps(column(2, { input_type: 'Decimal' })), { inputMode: 'decimal' });
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
    'src/pages/payroll/entry/dayGridModel.ts',
    'src/pages/payroll/WorkTotals.tsx',
    ...entryPageSources,
    'src/pages/payroll/entry/useDailyEntryController.ts',
  ].map(read).join('\n');
  assert.doesNotMatch(sources, /pay_item_code|pay_item_id|is_time|total_hours|total_miles/);
  assert.doesNotMatch(sources, /['"`](HOURS|MILES)['"`]/);
  assert.doesNotMatch(sources, /line_type/);
});

test('Day Grid saves are keyed by the period definition id', () => {
  const entry = entryPageComponents.map(read).join('\n');
  assert.match(entry, /columnKey\(/);
  const feature = [entry, read('src/pages/payroll/entry/useDailyEntryController.ts'), read('src/pages/payroll/entry/dayGridModel.ts')].join('\n');
  assert.doesNotMatch(feature, /values\[[^\]]*definition_code/);
});

test('the payroll entry workbench carries no PayItem identity', () => {
  const types = read('src/types/payroll/entry.ts');
  assert.match(types, /payroll_period_definition_id/);
  const detail = [...entryPageSources, 'src/pages/payroll/entry/useDailyEntryController.ts'].map(read).join('\n');
  assert.doesNotMatch(detail, /pay_item_code|PayItem/);
});

test('dirty-row construction preserves driver identity, existing definition keys, string quantities, status, and notes', () => {
  const grid = {
    rows: [{
      driver_assignment_id: 42,
      values: {
        '7': { quantity: '0' },
        '8': { quantity: '1.234567890123' },
        '9': { quantity: '' },
      },
      status_key_id: 11,
      status_key: 'OFF',
      notes: 'existing note',
    }],
  } as unknown as DayGridResponse;

  assert.deepEqual(buildSaveRowFromGrid(grid, 42), {
    driver_assignment_id: 42,
    values: { '7': '0', '8': '1.234567890123', '9': '' },
    status_key_id: 11,
    status_key: 'OFF',
    notes: 'existing note',
  });
});

test('dirty-row construction converts nullish quantities to empty strings and preserves null status/notes', () => {
  const grid = {
    rows: [{ driver_assignment_id: 7, values: { '12': { quantity: null }, '15': { quantity: '3.50' } }, status_key: null, notes: null }],
  } as unknown as DayGridResponse;

  assert.deepEqual(buildSaveRowFromGrid(grid, 7), {
    driver_assignment_id: 7,
    values: { '12': '', '15': '3.50' },
    status_key_id: null,
    status_key: null,
    notes: null,
  });
});

test('dirty-row construction defaults a missing grid or driver row without losing the requested id', () => {
  assert.deepEqual(buildSaveRowFromGrid(null, 99), {
    driver_assignment_id: 99, values: {}, status_key_id: null, status_key: null, notes: null,
  });
  assert.deepEqual(buildSaveRowFromGrid({ rows: [] } as unknown as DayGridResponse, 100), {
    driver_assignment_id: 100, values: {}, status_key_id: null, status_key: null, notes: null,
  });
});

test('dirty-row construction preserves empty Status and notes and converts undefined quantities', () => {
  const grid = {
    rows: [{ driver_assignment_id: 8, values: { '20': { quantity: undefined } }, status_key: '', notes: '' }],
  } as unknown as DayGridResponse;

  assert.deepEqual(buildSaveRowFromGrid(grid, 8), {
    driver_assignment_id: 8,
    values: { '20': '' },
    status_key_id: null,
    status_key: '',
    notes: '',
  });
});
