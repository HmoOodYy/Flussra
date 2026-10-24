import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  cellWarning,
  clientCellWarning,
  dayChip,
  dayPayByDriver,
  dayPayForDriver,
  dayPayNote,
  driverInitials,
  filterEntryRows,
  formatQuantity,
  groupIntoWeeks,
  listPeriodDates,
  matchesDriverSearch,
  parsePeriodId,
  periodLifecycle,
  quantitiesLocked,
  rowIsOff,
  sumDecimalStrings,
  unsavedChangesLabel,
  weekIndexOf,
  type EntryRowState,
} from '../src/pages/payroll/dayEntryPageModel.ts';
import {
  dayProgress,
  daysOfWeek,
  daysWorked,
  driversOfDays,
  driverWeekBars,
  payParts,
  ratesByColumn,
  weekCell,
  weekPayByDriver,
  weekTotal,
} from '../src/pages/payroll/periodOverviewModel.ts';
import type { DayGridColumn, DayGridStatusKey, PeriodEntryOverviewDay } from '../src/types/payroll/entry.ts';

// ── Period dates and weeks ──────────────────────────────────────────────────

test('a period lists every date from its start to its end, across month and year boundaries', () => {
  assert.deepEqual(listPeriodDates('2026-09-28', '2026-10-02'), [
    '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-01', '2026-10-02',
  ]);
  assert.deepEqual(listPeriodDates('2026-12-30', '2027-01-01'), ['2026-12-30', '2026-12-31', '2027-01-01']);
  assert.deepEqual(listPeriodDates('2026-09-23', '2026-09-23'), ['2026-09-23']);
});

test('an invalid or reversed period lists no dates', () => {
  assert.deepEqual(listPeriodDates('2026-09-30', '2026-09-23'), []);
  assert.deepEqual(listPeriodDates('not-a-date', '2026-09-23'), []);
});

test('a 7-day period is one week', () => {
  const weeks = groupIntoWeeks(listPeriodDates('2026-09-21', '2026-09-27'));
  assert.equal(weeks.length, 1);
  assert.equal(weeks[0].length, 7);
});

test('a 14-day period is two weeks of seven, counted from the start date', () => {
  const weeks = groupIntoWeeks(listPeriodDates('2026-09-21', '2026-10-04'));
  assert.equal(weeks.length, 2);
  assert.deepEqual(weeks.map((week) => [week[0], week[week.length - 1]]), [
    ['2026-09-21', '2026-09-27'],
    ['2026-09-28', '2026-10-04'],
  ]);
});

test('a period that is not a whole number of weeks ends with a shorter week', () => {
  const weeks = groupIntoWeeks(listPeriodDates('2026-09-21', '2026-10-01'));
  assert.deepEqual(weeks.map((week) => week.length), [7, 4]);
});

test('the selected date picks its week, defaulting to the first', () => {
  const weeks = groupIntoWeeks(listPeriodDates('2026-09-21', '2026-10-04'));
  assert.equal(weekIndexOf(weeks, '2026-09-30'), 1);
  assert.equal(weekIndexOf(weeks, '2026-09-21'), 0);
  assert.equal(weekIndexOf(weeks, null), 0);
  assert.equal(weekIndexOf(weeks, '2027-01-01'), 0);
});

test('a day button reads as the weekday and the day of the month', () => {
  assert.deepEqual(dayChip('2026-10-12'), { date: '2026-10-12', weekday: 'Mon', dayOfMonth: 12 });
  assert.deepEqual(dayChip('2026-10-04'), { date: '2026-10-04', weekday: 'Sun', dayOfMonth: 4 });
});

test('only a positive whole number is a period id', () => {
  assert.equal(parsePeriodId('42'), 42);
  for (const raw of [undefined, '', '0', '-3', '4.5', 'abc', '12abc', '007']) {
    assert.equal(parsePeriodId(raw), null, String(raw));
  }
});

// ── Lifecycle ───────────────────────────────────────────────────────────────

test('the lifecycle marks the stage the period is in', () => {
  const states = (status: string) => periodLifecycle(status)?.map((stage) => stage.state);
  assert.deepEqual(states('Draft'), ['current', 'upcoming', 'upcoming', 'upcoming']);
  assert.deepEqual(states('InReview'), ['done', 'current', 'upcoming', 'upcoming']);
  assert.deepEqual(states('Approved'), ['done', 'done', 'current', 'upcoming']);
  assert.deepEqual(states('Locked'), ['done', 'done', 'done', 'current']);
  assert.deepEqual(states('Archived'), ['done', 'done', 'done', 'current']);
});

test('the first lifecycle stage is named for the entry status of the period', () => {
  assert.equal(periodLifecycle('Draft')?.[0].label, 'Prepared');
  assert.equal(periodLifecycle('Open')?.[0].label, 'Open');
  assert.equal(periodLifecycle('Returned')?.[0].label, 'Returned for correction');
  assert.equal(periodLifecycle('Returned')?.[0].state, 'current');
});

test('a status outside the payroll flow has no lifecycle', () => {
  assert.equal(periodLifecycle('Cancelled'), null);
});

// ── Exact day pay ───────────────────────────────────────────────────────────

test('decimal amounts are added exactly, never through floating point', () => {
  assert.equal(sumDecimalStrings(['0.1000', '0.2000']), '0.3000');
  assert.equal(sumDecimalStrings(['1234567890123.4567', '0.0001']), '1234567890123.4568');
  assert.equal(sumDecimalStrings(['10', '2.5']), '12.5');
  assert.equal(sumDecimalStrings(['5.0000', '-5.0000']), '0.0000');
  assert.equal(sumDecimalStrings(['-0.5', '0.25']), '-0.25');
});

test('null amounts are ignored and nothing to add gives null', () => {
  assert.equal(sumDecimalStrings(['1.5000', null, undefined, '2.0000']), '3.5000');
  assert.equal(sumDecimalStrings([null, undefined]), null);
  assert.equal(sumDecimalStrings([]), null);
});

function line(driverAssignmentId: number, workDate: string | null, amount: string | null) {
  return { driver_assignment_id: driverAssignmentId, work_date: workDate, calculated_amount: amount };
}

test('a driver\'s day pay is the exact sum of that driver\'s lines dated that day', () => {
  const drivers = [{
    lines: [
      line(1, '2026-09-23', '0.1000'),
      line(1, '2026-09-23', '0.2000'),
      line(1, '2026-09-23', null),
      line(1, '2026-09-24', '100.0000'),
      line(1, null, '50.0000'),
    ],
  }];
  assert.equal(dayPayForDriver(drivers, 1, '2026-09-23'), '0.3000');
});

test('lines of other drivers and other dates never enter a driver\'s day pay', () => {
  const drivers = [
    { lines: [line(1, '2026-09-23', '10.0000'), line(1, '2026-09-24', '99.0000')] },
    { lines: [line(2, '2026-09-23', '20.5000')] },
  ];
  const pay = dayPayByDriver(drivers, '2026-09-23');
  assert.equal(pay.get(1), '10.0000');
  assert.equal(pay.get(2), '20.5000');
  assert.equal(dayPayForDriver(drivers, 1, '2026-09-24'), '99.0000');
});

test('a driver with no amount for the day has no day pay', () => {
  const drivers = [{ lines: [line(1, '2026-09-24', '10.0000'), line(2, '2026-09-23', null)] }];
  assert.equal(dayPayForDriver(drivers, 1, '2026-09-23'), null);
  assert.equal(dayPayForDriver(drivers, 2, '2026-09-23'), null);
  assert.equal(dayPayForDriver(drivers, 3, '2026-09-23'), null);
  assert.equal(dayPayForDriver([], 1, '2026-09-23'), null);
});

test('the day pay note says where the figure comes from or why there is none', () => {
  const note = (periodStatus: string, over: { loading?: boolean; failed?: boolean; hasAmount?: boolean } = {}) =>
    dayPayNote({ periodStatus, loading: false, failed: false, hasAmount: false, ...over });
  assert.equal(note('Open', { hasAmount: true }), 'calculated by Flussra');
  assert.equal(note('Draft'), 'shown once the period is Open');
  assert.equal(note('Open'), 'nothing paid yet');
  assert.equal(note('Returned'), 'nothing paid yet');
  assert.equal(note('Open', { failed: true }), 'not available right now');
  assert.equal(note('Open', { loading: true }), 'calculating…');
  assert.equal(note('InReview'), 'not available for this status');
});

// ── Quantity display ────────────────────────────────────────────────────────

test('a server quantity shows without insignificant trailing zeros', () => {
  assert.equal(formatQuantity('119.0000'), '119');
  assert.equal(formatQuantity('12.5000'), '12.5');
  assert.equal(formatQuantity('0.0000'), '0');
  assert.equal(formatQuantity('100'), '100');
  assert.equal(formatQuantity('100.0500'), '100.05');
  assert.equal(formatQuantity('0.1230'), '0.123');
  assert.equal(formatQuantity('-0.0000'), '0');
  assert.equal(formatQuantity('-2.5000'), '-2.5');
});

test('quantity trimming is exact for values a float cannot hold', () => {
  assert.equal(formatQuantity('12345678901234567890.1200'), '12345678901234567890.12');
  assert.equal(formatQuantity('0.1234567890123456789000'), '0.1234567890123456789');
});

test('text that is not a plain decimal is shown unchanged, and blank stays blank', () => {
  assert.equal(formatQuantity(''), '');
  assert.equal(formatQuantity('abc'), 'abc');
  assert.equal(formatQuantity('1e3'), '1e3');
});

test('quantity inputs are text fields that no wheel or arrow key can step', () => {
  const table = readFileSync(new URL('../src/pages/payroll/DayEntryTable.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(table, /type="number"/);
  assert.match(table, /inputMode=\{inputProps\.inputMode\}/);
  assert.doesNotMatch(table, /onWheel|onKeyDown/);
});

// ── Cell warnings ───────────────────────────────────────────────────────────

test('a blank or valid quantity has no client warning', () => {
  for (const value of ['', '  ', '0', '7', '7.5', '.5', '12.']) {
    assert.equal(clientCellWarning(value, 'Decimal'), null, value);
  }
  for (const value of ['', '0', '3', '3.0']) {
    assert.equal(clientCellWarning(value, 'WholeNumber'), null, value);
  }
});

test('text that is not a number is flagged as numbers only', () => {
  for (const value of ['abc', '1,5', '1e3', '--2', '5 6']) {
    assert.equal(clientCellWarning(value, 'Decimal'), 'Numbers only', value);
  }
});

test('a negative quantity is flagged', () => {
  assert.equal(clientCellWarning('-1', 'Decimal'), "Can't be negative");
  assert.equal(clientCellWarning('-0.5', 'WholeNumber'), "Can't be negative");
});

test('a decimal in a whole-number column is flagged', () => {
  assert.equal(clientCellWarning('2.5', 'WholeNumber'), 'Whole numbers only');
  assert.equal(clientCellWarning('2.5', 'Decimal'), null);
});

test('a server review flag shows as no approved rate unless the typed value is itself wrong', () => {
  assert.equal(cellWarning('4', 'Decimal', true), 'No approved rate');
  assert.equal(cellWarning('4', 'Decimal', false), null);
  assert.equal(cellWarning('', 'Decimal', false), null);
  assert.equal(cellWarning('-4', 'Decimal', true), "Can't be negative");
});

// ── Off-reason Status rows ──────────────────────────────────────────────────

const statusKeys: DayGridStatusKey[] = [
  { status_key_id: 1, key_code: 'SICK', label: 'Sick Day', is_off_reason: true, hours_value: null, aliases: ['sick'] },
  { status_key_id: 2, key_code: 'HALF', label: 'Half Day', is_off_reason: false, hours_value: 4, aliases: [] },
];

test('an untouched row is off exactly when the server says so', () => {
  assert.equal(rowIsOff({ is_off: true, status_key: 'SICK' }, undefined, statusKeys), true);
  assert.equal(rowIsOff({ is_off: false, status_key: null }, undefined, statusKeys), false);
});

test('typed Status text is read through the catalog by name, alias or code', () => {
  const row = { is_off: false, status_key: null };
  const typed = (text: string | null) => ({ status_key_id: null, status_key: text });
  assert.equal(rowIsOff(row, typed('Sick Day'), statusKeys), true);
  assert.equal(rowIsOff(row, typed('sick'), statusKeys), true);
  assert.equal(rowIsOff(row, typed('SICK'), statusKeys), true);
  assert.equal(rowIsOff(row, typed('Half Day'), statusKeys), false);
  assert.equal(rowIsOff(row, typed('unknown text'), statusKeys), false);
  assert.equal(rowIsOff({ is_off: true, status_key: 'SICK' }, typed(null), statusKeys), false);
});

test('an unrelated edit of an off row keeps it off', () => {
  const held = { status_key_id: 1, status_key: 'SICK' };
  assert.equal(rowIsOff({ is_off: true, status_key: 'SICK' }, held, statusKeys), true);
});

test('quantity inputs close on an off row only while it holds no work', () => {
  assert.equal(quantitiesLocked(true, { '1': '', '2': '0' }), true);
  assert.equal(quantitiesLocked(true, { '1': '', '2': '3' }), false);
  assert.equal(quantitiesLocked(false, { '1': '', '2': '' }), false);
});

// ── Search and filters ──────────────────────────────────────────────────────

const columns: Pick<DayGridColumn, 'payroll_period_definition_id' | 'input_type'>[] = [
  { payroll_period_definition_id: 7, input_type: 'Decimal' },
  { payroll_period_definition_id: 8, input_type: 'WholeNumber' },
];

function state(
  name: string,
  number: string | null,
  overrides: { dirty?: boolean; statusText?: string; values?: Record<string, string>; review?: boolean } = {},
): EntryRowState {
  return {
    row: {
      driver_name: name,
      driver_number: number,
      values: {
        '7': { line_id: null, quantity: null, calculated_amount: null, needs_manager_review: overrides.review ?? false, calculation_status: null },
        '8': { line_id: null, quantity: null, calculated_amount: null, needs_manager_review: false, calculation_status: null },
      },
    },
    dirty: overrides.dirty ?? false,
    statusText: overrides.statusText ?? '',
    values: overrides.values ?? { '7': '', '8': '' },
  };
}

const ana = state('Ana Lopez', 'D-101', { values: { '7': '8', '8': '1' } });
const ben = state('Ben Carter', 'D-202', { statusText: 'Sick Day' });
const cho = state('Cho Min', null, { dirty: true, values: { '7': '5', '8': '' } });
const dee = state('Dee Park', 'D-404', { review: true, values: { '7': '6', '8': '' } });
const eli = state('Eli Stone', 'D-505', { values: { '7': '2', '8': '1.5' } });
const all = [ana, ben, cho, dee, eli];
const names = (rows: readonly EntryRowState[]) => rows.map((r) => r.row.driver_name);

test('the search matches a driver name or number, ignoring case and surrounding space', () => {
  assert.equal(matchesDriverSearch({ driver_name: 'Ana Lopez', driver_number: 'D-101' }, 'ANA'), true);
  assert.equal(matchesDriverSearch({ driver_name: 'Ana Lopez', driver_number: 'D-101' }, ' d-101 '), true);
  assert.equal(matchesDriverSearch({ driver_name: 'Ana Lopez', driver_number: null }, 'd-101'), false);
  assert.equal(matchesDriverSearch({ driver_name: 'Ana Lopez', driver_number: null }, ''), true);
  assert.deepEqual(names(filterEntryRows(all, columns, 'all', 'a')), ['Ana Lopez', 'Ben Carter', 'Dee Park']);
  assert.deepEqual(names(filterEntryRows(all, columns, 'all', 'd-5')), ['Eli Stone']);
});

test('the all-drivers filter keeps every row', () => {
  assert.deepEqual(names(filterEntryRows(all, columns, 'all', '')), names(all));
});

test('the needs-attention filter keeps rows with a server flag or a value that cannot be saved', () => {
  assert.deepEqual(names(filterEntryRows(all, columns, 'attention', '')), ['Dee Park', 'Eli Stone']);
});

test('the not-entered filter keeps rows with no Status and no quantity', () => {
  const blank = state('Fay Gray', 'D-606');
  assert.deepEqual(names(filterEntryRows([...all, blank], columns, 'notEntered', '')), ['Fay Gray']);
});

test('the unsaved filter keeps rows with unsaved edits', () => {
  assert.deepEqual(names(filterEntryRows(all, columns, 'unsaved', '')), ['Cho Min']);
});

test('search and filter combine', () => {
  assert.deepEqual(names(filterEntryRows(all, columns, 'attention', 'dee')), ['Dee Park']);
  assert.deepEqual(names(filterEntryRows(all, columns, 'unsaved', 'ana')), []);
});

// ── Labels ──────────────────────────────────────────────────────────────────

test('the unsaved-changes text is singular for one driver and plural otherwise', () => {
  assert.equal(unsavedChangesLabel(1), 'Unsaved changes for 1 driver');
  assert.equal(unsavedChangesLabel(2), 'Unsaved changes for 2 drivers');
});

test('driver initials come from the first and last name parts', () => {
  assert.equal(driverInitials('Ana Lopez'), 'AL');
  assert.equal(driverInitials('ana maria de la Cruz'), 'AC');
  assert.equal(driverInitials('Cher'), 'C');
  assert.equal(driverInitials('   '), '?');
});

// ── Whole-period model ──────────────────────────────────────────────────────

type OverviewRow = PeriodEntryOverviewDay['rows'][number];

function savedRow(
  id: number,
  quantities: Record<string, string | null>,
  overrides: { status?: string | null; isOff?: boolean; review?: string[] } = {},
): OverviewRow {
  const status = overrides.status ?? null;
  return {
    driver_assignment_id: id,
    driver_id: id,
    driver_name: `Driver ${id}`,
    driver_number: `D-${id}`,
    status_key_id: status === null ? null : 9,
    status_key: status === null ? null : status.toUpperCase(),
    status_label: status,
    is_off: overrides.isOff ?? false,
    notes: null,
    values: Object.fromEntries(Object.entries(quantities).map(([key, quantity]) => [key, {
      line_id: null,
      quantity,
      calculated_amount: null,
      needs_manager_review: overrides.review?.includes(key) ?? false,
      calculation_status: null,
    }])),
  };
}

function overviewDay(date: string, rows: OverviewRow[], over: { off?: boolean; attention?: number } = {}): PeriodEntryOverviewDay {
  return {
    work_date: date,
    is_configured_off_day: over.off ?? false,
    rows,
    summary: { needs_attention: over.attention ?? 0 } as PeriodEntryOverviewDay['summary'],
  };
}

test('a day with no drivers says so', () => {
  const progress = dayProgress(overviewDay('2026-09-21', []));
  assert.equal(progress.text, 'No drivers');
  assert.equal(progress.tone, 'none');
  assert.equal(progress.ratio, 0);
});

test('a day nobody has entered is not started, or a normal day off when it is scheduled off', () => {
  const rows = [savedRow(1, { '7': null }), savedRow(2, { '7': '' })];
  const idle = dayProgress(overviewDay('2026-09-21', rows));
  assert.deepEqual([idle.text, idle.tone], ['Not started', 'notStarted']);
  const off = dayProgress(overviewDay('2026-09-26', rows, { off: true }));
  assert.equal(off.text, 'Normal day off');
  assert.equal(off.tone, 'off');
  assert.equal(off.configuredOff, true);
});

test('a Status or any quantity text counts a driver as entered', () => {
  const rows = [
    savedRow(1, { '7': '8.0000' }),
    savedRow(2, { '7': null }, { status: 'Sick Day' }),
    savedRow(3, { '7': null }),
    savedRow(4, { '7': '' }),
  ];
  const progress = dayProgress(overviewDay('2026-09-21', rows));
  assert.equal(progress.text, '2 of 4 entered');
  assert.equal(progress.tone, 'partial');
  assert.equal(progress.ratio, 0.5);
  assert.equal(progress.barTone, 'blue');
});

test('a fully entered day is complete, or counts its cells to check', () => {
  const rows = [savedRow(1, { '7': '8' }), savedRow(2, { '7': null }, { status: 'Sick Day' })];
  const complete = dayProgress(overviewDay('2026-09-21', rows));
  assert.deepEqual([complete.text, complete.tone, complete.barTone, complete.ratio], ['Complete', 'complete', 'green', 1]);
  const check = dayProgress(overviewDay('2026-09-21', rows, { attention: 3 }));
  assert.deepEqual([check.text, check.tone, check.barTone], ['3 to check', 'check', 'amber']);
});

test('a partly entered day with cells to check draws an amber bar', () => {
  const rows = [savedRow(1, { '7': '8' }), savedRow(2, { '7': null })];
  assert.equal(dayProgress(overviewDay('2026-09-21', rows, { attention: 1 })).barTone, 'amber');
});

test('a normal day off keeps its mark once entries exist', () => {
  const progress = dayProgress(overviewDay('2026-09-26', [savedRow(1, { '7': '8' })], { off: true }));
  assert.equal(progress.configuredOff, true);
  assert.equal(progress.text, 'Complete');
});

const week = [
  overviewDay('2026-09-21', [savedRow(1, { '7': '8.0000', '8': '2' }, { review: ['7'] }), savedRow(2, { '7': null, '8': null })]),
  overviewDay('2026-09-22', [savedRow(1, { '7': '4.5000', '8': null }), savedRow(2, { '7': null, '8': null }, { status: 'Sick Day', isOff: true })]),
  overviewDay('2026-09-23', [savedRow(1, { '7': null, '8': null }), savedRow(2, { '7': '0.1000', '8': null })], { off: true }),
];

test('a week cell shows the Status label, else the saved quantity, else a dot', () => {
  const cell = (day: number, driver: number, off = false) =>
    weekCell(week[day].rows.find((r) => r.driver_assignment_id === driver), '7', off);
  assert.deepEqual(cell(0, 1), { text: '8', kind: 'quantity', needsReview: true });
  assert.deepEqual(cell(1, 2), { text: 'Sick Day', kind: 'status', needsReview: false });
  assert.deepEqual(cell(0, 2), { text: '·', kind: 'pending', needsReview: false });
});

test('an empty week cell on a normal day off is blank, and a driver missing from the day is empty', () => {
  assert.deepEqual(weekCell(week[2].rows[0], '7', true), { text: '', kind: 'blank', needsReview: false });
  assert.deepEqual(weekCell(undefined, '7', false), { text: '·', kind: 'pending', needsReview: false });
  assert.equal(weekCell(week[2].rows[1], '7', true).text, '0.1');
});

test('a week total adds the saved quantities of one column exactly, ignoring blanks', () => {
  assert.equal(weekTotal(week, 1, '7'), '12.5000');
  assert.equal(weekTotal(week, 1, '8'), '2');
  assert.equal(weekTotal(week, 2, '7'), '0.1000');
  assert.equal(weekTotal(week, 2, '8'), null);
  assert.equal(weekTotal(week, 99, '7'), null);
});

test('a week pay adds a driver day pay over the dates of the week exactly', () => {
  const drivers = [{
    lines: [
      line(1, '2026-09-21', '0.1000'),
      line(1, '2026-09-22', '0.2000'),
      line(1, '2026-09-28', '50.0000'),
      line(1, null, '9.0000'),
      line(2, '2026-09-21', null),
    ],
  }];
  const pay = weekPayByDriver(drivers, ['2026-09-21', '2026-09-22', '2026-09-23']);
  assert.equal(pay.get(1), '0.3000');
  assert.equal(pay.has(2), false);
});

test('the week lists each driver once, in the order first met, and finds the visible days', () => {
  assert.deepEqual(driversOfDays(week).map((d) => d.driver_assignment_id), [1, 2]);
  assert.deepEqual(daysOfWeek(week, ['2026-09-22', '2026-09-30', '2026-09-23']).map((d) => d.work_date), ['2026-09-22', '2026-09-23']);
});

test('a driver week bars show quantity against the largest of the week, the Status, or why nothing', () => {
  const bars = driverWeekBars(week, 1, '7');
  assert.deepEqual(bars.map((b) => [b.date, b.kind, b.label]), [
    ['2026-09-21', 'quantity', '8'],
    ['2026-09-22', 'quantity', '4.5'],
    ['2026-09-23', 'dayOff', 'Day off'],
  ]);
  assert.equal(bars[0].ratio, 1);
  assert.equal(bars[1].ratio, 4.5 / 8);
  const second = driverWeekBars(week, 2, '7');
  assert.deepEqual(second.map((b) => [b.kind, b.label]), [
    ['notEntered', 'Not entered'],
    ['status', 'Sick Day'],
    ['quantity', '0.1'],
  ]);
});

test('days worked counts days with a quantity and no full-day-off Status', () => {
  assert.equal(daysWorked(week, 1), 2);
  assert.equal(daysWorked(week, 2), 1);
  const withOffStatus = [overviewDay('2026-09-24', [savedRow(3, { '7': '5' }, { status: 'Sick Day', isOff: true })])];
  assert.equal(daysWorked(withOffStatus, 3), 0);
  const partialStatus = [overviewDay('2026-09-24', [savedRow(3, { '7': '5' }, { status: 'Half Day' })])];
  assert.equal(daysWorked(partialStatus, 3), 1);
  assert.equal(daysWorked([overviewDay('2026-09-24', [savedRow(3, { '7': '0.0000' })])], 3), 0);
});

test('the pay parts list only the parts that are not zero', () => {
  const parts = payParts({
    daily_pay: '120.5000', status_pay: '0.0000', bonus_total: '10.0000',
    minimum_adjustment: '0.0000', maximum_adjustment: '-5.0000',
  });
  assert.deepEqual(parts.map((p) => [p.key, p.amount]), [
    ['daily', '120.5000'], ['bonus', '10.0000'], ['maximum', '-5.0000'],
  ]);
});

test('rates are listed per column from the lines of that column, with review and empty states', () => {
  const columnsForRates = [
    { payroll_period_definition_id: 7, label: 'Hours', unit: 'hr' },
    { payroll_period_definition_id: 8, label: 'Stops', unit: 'stop' },
    { payroll_period_definition_id: 9, label: 'Miles', unit: null },
  ];
  const ratedLine = (ppd: number | null, rate: string | null, review = false) => ({
    payroll_period_definition_id: ppd, resolved_rate: rate, needs_manager_review: review,
  });
  const driver = {
    lines: [
      ratedLine(7, '20.0000'), ratedLine(7, '20.0000'), ratedLine(7, '22.5000'),
      ratedLine(8, null, true), ratedLine(8, '1.0000'),
      ratedLine(null, '99.0000'),
    ],
  };
  const [hours, stops, miles] = ratesByColumn(driver as never, columnsForRates);
  assert.deepEqual(hours, { key: '7', label: 'Hours', unit: 'hr', rates: ['20.0000', '22.5000'], needsReview: false, noEntries: false });
  assert.equal(stops.needsReview, true);
  assert.deepEqual(miles.rates, []);
  assert.equal(miles.noEntries, true);
  assert.equal(ratesByColumn(undefined, columnsForRates)[0].noEntries, true);
});
