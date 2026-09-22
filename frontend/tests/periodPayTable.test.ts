import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildPeriodPayTable, driverLabel } from '../src/pages/payroll/periodPayTable.ts';
import type { PayItemAmount, ReportColumn, ReportDriver, ReportPaySection } from '../src/types/payroll.ts';

function makeColumn(overrides: Partial<ReportColumn> = {}): ReportColumn {
  return {
    pay_item_id: 1, code: 'HOURS', label: 'Hours', category: 'Work',
    data_type: 'Decimal', unit: 'Hour', scope: 'Daily', sort_order: 1,
    ...overrides,
  };
}

function makePay(overrides: Partial<ReportPaySection> = {}): ReportPaySection {
  return {
    daily_pay: '0.00', status_pay: '0.00', period_pay: '0.00', minimum_adjustment: '0.00',
    maximum_adjustment: '0.00', bonus_total: '0.00', total_pay: '0.00', gross_pay: '0.00',
    pay_item_amounts: [], driver_code: null, driver_name: null, financial_lines: [],
    ...overrides,
  };
}

function makeDriver(overrides: Partial<ReportDriver> = {}): ReportDriver {
  return {
    driver_id: 1, driver_code: null, driver_name: null,
    work: { daily_rows: [], status_entries: [], status_summaries: [] },
    pay: makePay(), bonus_events: [],
    ...overrides,
  };
}

// ── 1. Dynamic column order ─────────────────────────────────────────────────

test('buildPeriodPayTable: preserves backend pay_item_columns order exactly, no frontend alphabetical sorting', () => {
  const columns = [
    makeColumn({ pay_item_id: 30, code: 'ZEBRA', label: 'Zebra Item', sort_order: 1 }),
    makeColumn({ pay_item_id: 10, code: 'ALPHA', label: 'Alpha Item', sort_order: 2 }),
  ];
  const table = buildPeriodPayTable(columns, [], null, null);
  assert.deepEqual(table.columns.map((c) => c.pay_item_id), [30, 10]);
  assert.deepEqual(table.columns.map((c) => c.label), ['Zebra Item', 'Alpha Item']);
});

// ── 2. PayItemID mapping ────────────────────────────────────────────────────

test('buildPeriodPayTable: aligns amounts by pay_item_id, not by array position or code', () => {
  const columns = [makeColumn({ pay_item_id: 10, code: 'A' }), makeColumn({ pay_item_id: 20, code: 'B' })];
  const amounts: PayItemAmount[] = [
    { pay_item_id: 20, amount: '50.00' }, // deliberately supplied out of column order
    { pay_item_id: 10, amount: '15.00' },
  ];
  const driver = makeDriver({ pay: makePay({ pay_item_amounts: amounts }) });
  const [row] = buildPeriodPayTable(columns, [driver], null, null).rows;
  assert.deepEqual(row.items, [
    { pay_item_id: 10, amount: '15.00' },
    { pay_item_id: 20, amount: '50.00' },
  ]);
});

// ── 3. Custom Daily Pay Item ────────────────────────────────────────────────

test('buildPeriodPayTable: an arbitrary custom Daily Pay Item renders with no hard-coded name', () => {
  const columns = [makeColumn({ pay_item_id: 777, code: 'CDPI9F3A1B', label: 'Night Differential Bonus Pool' })];
  const driver = makeDriver({ pay: makePay({ pay_item_amounts: [{ pay_item_id: 777, amount: '42.00' }] }) });
  const table = buildPeriodPayTable(columns, [driver], null, null);
  assert.equal(table.columns[0].label, 'Night Differential Bonus Pool');
  assert.equal(table.columns[0].code, 'CDPI9F3A1B');
  assert.equal(table.rows[0].items[0].amount, '42.00');
});

// ── 4. Dense zero ────────────────────────────────────────────────────────────

test('buildPeriodPayTable: a real backend "0" amount is preserved, not treated as missing', () => {
  const columns = [makeColumn({ pay_item_id: 10 })];
  const driver = makeDriver({ pay: makePay({ pay_item_amounts: [{ pay_item_id: 10, amount: '0.00' }] }) });
  const [row] = buildPeriodPayTable(columns, [driver], null, null).rows;
  assert.equal(row.items[0].amount, '0.00');
  assert.notEqual(row.items[0].amount, null);
});

// ── 5. Missing amount defense ───────────────────────────────────────────────

test('buildPeriodPayTable: a column with no matching PayItemAmount maps to null, never a manufactured zero', () => {
  const columns = [makeColumn({ pay_item_id: 10 }), makeColumn({ pay_item_id: 20 })];
  const driver = makeDriver({ pay: makePay({ pay_item_amounts: [{ pay_item_id: 10, amount: '5.00' }] }) });
  const [row] = buildPeriodPayTable(columns, [driver], null, null).rows;
  assert.equal(row.items[0].amount, '5.00');
  assert.equal(row.items[1].amount, null);
});

// ── 6. Driver identity ───────────────────────────────────────────────────────

test('driverLabel: uses driver_name when present', () => {
  assert.equal(driverLabel({ driver_id: 9, driver_name: 'Ada Operator' }), 'Ada Operator');
});

test('driverLabel: falls back to "Driver #<id>" when driver_name is null', () => {
  assert.equal(driverLabel({ driver_id: 9, driver_name: null }), 'Driver #9');
});

test('buildPeriodPayTable: row carries driver_code separately from the display label', () => {
  const driver = makeDriver({ driver_id: 5, driver_name: 'Ada Operator', driver_code: 'D-005' });
  const [row] = buildPeriodPayTable([], [driver], null, null).rows;
  assert.equal(row.driver_label, 'Ada Operator');
  assert.equal(row.driver_code, 'D-005');
});

// ── 7. Footer ─────────────────────────────────────────────────────────────

test('buildPeriodPayTable: footer item totals come from pay_item_totals, aligned by id, never summed from rows', () => {
  const columns = [makeColumn({ pay_item_id: 10 }), makeColumn({ pay_item_id: 20 })];
  const drivers = [
    makeDriver({ driver_id: 1, pay: makePay({ pay_item_amounts: [{ pay_item_id: 10, amount: '5.00' }, { pay_item_id: 20, amount: '3.00' }] }) }),
    makeDriver({ driver_id: 2, pay: makePay({ pay_item_amounts: [{ pay_item_id: 10, amount: '7.00' }, { pay_item_id: 20, amount: '1.00' }] }) }),
  ];
  // Deliberately NOT the row sum (12.00 / 4.00) -- proves the footer reads
  // the backend total verbatim rather than deriving it from the rows above.
  const payItemTotals: PayItemAmount[] = [{ pay_item_id: 20, amount: '999.00' }, { pay_item_id: 10, amount: '888.00' }];
  const payTotals = { status_pay: '1.00', gross_pay: '2.00', minimum_adjustment: '3.00', maximum_adjustment: '-4.00', bonus_total: '5.00', total_pay: '6.00' };
  const table = buildPeriodPayTable(columns, drivers, payItemTotals, payTotals);
  assert.deepEqual(table.footer.items, [
    { pay_item_id: 10, amount: '888.00' },
    { pay_item_id: 20, amount: '999.00' },
  ]);
  assert.equal(table.footer.status_pay, '1.00');
  assert.equal(table.footer.gross_pay, '2.00');
  assert.equal(table.footer.minimum_adjustment, '3.00');
  assert.equal(table.footer.maximum_adjustment, '-4.00');
  assert.equal(table.footer.bonus_total, '5.00');
  assert.equal(table.footer.total_pay, '6.00');
});

test('buildPeriodPayTable: a footer column missing from pay_item_totals is null, never a manufactured zero', () => {
  const columns = [makeColumn({ pay_item_id: 10 })];
  const table = buildPeriodPayTable(columns, [], [], null);
  assert.equal(table.footer.items[0].amount, null);
});

test('buildPeriodPayTable: footer values are null when pay_item_totals/pay_totals are null (financials unavailable)', () => {
  const columns = [makeColumn({ pay_item_id: 10 })];
  const table = buildPeriodPayTable(columns, [], null, null);
  assert.equal(table.footer.items[0].amount, null);
  assert.equal(table.footer.total_pay, null);
});

// ── 8. Negative maximum adjustment ──────────────────────────────────────────

test('buildPeriodPayTable: a negative maximum_adjustment is preserved exactly from the backend', () => {
  const driver = makeDriver({ pay: makePay({ maximum_adjustment: '-125.50' }) });
  const [row] = buildPeriodPayTable([], [driver], null, null).rows;
  assert.equal(row.maximum_adjustment, '-125.50');
});

// ── 9. driver.pay == null ────────────────────────────────────────────────────

test('buildPeriodPayTable: driver.pay == null renders every fixed and item value as null, never a fabricated zero', () => {
  const columns = [makeColumn({ pay_item_id: 10 })];
  const driver = makeDriver({ pay: null });
  const [row] = buildPeriodPayTable(columns, [driver], null, null).rows;
  assert.deepEqual(row.items, [{ pay_item_id: 10, amount: null }]);
  assert.equal(row.status_pay, null);
  assert.equal(row.gross_pay, null);
  assert.equal(row.minimum_adjustment, null);
  assert.equal(row.maximum_adjustment, null);
  assert.equal(row.bonus_total, null);
  assert.equal(row.total_pay, null);
});

// ── 10. No dependency on fixed Pay Item names ───────────────────────────────

test('periodPayTable.ts source contains no hard-coded Daily Pay Item names', () => {
  const source = readFileSync(new URL('../src/pages/payroll/periodPayTable.ts', import.meta.url), 'utf8');
  for (const bannedName of ['Hours', 'Miles', 'Loads', 'Pallets', 'Wait Time']) {
    assert.ok(!source.includes(bannedName), `unexpected hard-coded Pay Item name "${bannedName}" found in periodPayTable.ts`);
  }
});

// ── Structural: Current and Finalized never build two interpretations ──────
// These guard the one drift risk pure input/output tests can't see: someone
// reintroducing a second, independent table for the other dialog.

test('both dialogs align money through the shared buildPeriodPayTable helper', () => {
  for (const file of ['CurrentPayrollReportsDialog.tsx', 'FinalizedPayrollLibraryDialog.tsx']) {
    const source = readFileSync(new URL(`../src/pages/payroll/${file}`, import.meta.url), 'utf8');
    assert.ok(source.includes("from './periodPayTable'"), `${file}: expected an import from ./periodPayTable`);
    assert.ok(source.includes('buildPeriodPayTable('), `${file}: expected a call to buildPeriodPayTable`);
  }
});

test('both dialogs render period-pay through the shared PeriodPayMatrix component, not separate markup', () => {
  for (const file of ['CurrentPayrollReportsDialog.tsx', 'FinalizedPayrollLibraryDialog.tsx']) {
    const source = readFileSync(new URL(`../src/pages/payroll/${file}`, import.meta.url), 'utf8');
    assert.ok(source.includes("from './PeriodPayMatrix'"), `${file}: expected an import from ./PeriodPayMatrix`);
    assert.ok(source.includes('<PeriodPayMatrix'), `${file}: expected a <PeriodPayMatrix> usage`);
  }
});

test('neither dialog performs its own financial summation (Array.reduce) for official money', () => {
  for (const file of ['CurrentPayrollReportsDialog.tsx', 'FinalizedPayrollLibraryDialog.tsx']) {
    const source = readFileSync(new URL(`../src/pages/payroll/${file}`, import.meta.url), 'utf8');
    assert.ok(!/\.reduce\(/.test(source), `${file} must not use Array.reduce for financial totals`);
  }
});

test('periodPayTable.ts performs no monetary arithmetic (no Array.reduce over amount fields)', () => {
  const source = readFileSync(new URL('../src/pages/payroll/periodPayTable.ts', import.meta.url), 'utf8');
  assert.ok(!/\.reduce\(/.test(source), 'periodPayTable.ts must not use Array.reduce');
});
