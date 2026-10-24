import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  committedStatusText,
  displayedStatusText,
  normalizeStatusName,
  resolveStatusInput,
  statusInputSuggestions,
  type StatusCatalogEntry,
} from '../src/lib/statusInput.ts';
import { daySaveErrorMessage, statusCellText } from '../src/pages/payroll/dayGridModel.ts';

const read = (path: string) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

function entry(id: number, label: string, aliases: string[] = [], code = `SK_${id}`): StatusCatalogEntry {
  return { status_key_id: id, key_code: code, label, aliases };
}

const catalog = [
  entry(1, 'Vacation', ['V', 'Vac'], 'SK_AAAA1111'),
  entry(2, 'Sick Day', ['S'], 'SK_BBBB2222'),
  entry(3, 'Jury Duty', [], 'SK_CCCC3333'),
];

test('names and aliases share one normal form: NFKC, trimmed, collapsed, case-folded', () => {
  assert.equal(normalizeStatusName('  Sick   Day '), 'sick day');
  assert.equal(normalizeStatusName('Ｖａｃａｔｉｏｎ'), 'vacation');   // full-width letters
  assert.equal(normalizeStatusName('Straße'), 'strasse');
  assert.equal(normalizeStatusName('   '), '');
  assert.equal(normalizeStatusName(normalizeStatusName(' Jury\tDuty ')), 'jury duty');
});

test('typed text resolves by name, then alias, then technical code', () => {
  assert.deepEqual(resolveStatusInput('vacation', catalog), { kind: 'match', entry: catalog[0], via: 'name' });
  assert.deepEqual(resolveStatusInput('  SICK  day', catalog), { kind: 'match', entry: catalog[1], via: 'name' });
  assert.deepEqual(resolveStatusInput('vac', catalog), { kind: 'match', entry: catalog[0], via: 'alias' });
  assert.deepEqual(resolveStatusInput('s', catalog), { kind: 'match', entry: catalog[1], via: 'alias' });
  assert.deepEqual(resolveStatusInput('sk_cccc3333', catalog), { kind: 'match', entry: catalog[2], via: 'code' });
});

test('a name beats an alias, and an alias beats a code', () => {
  const tricky = [entry(1, 'Off', [], 'SK_ONE'), entry(2, 'Leave', ['off', 'SK_ONE'], 'SK_TWO')];
  assert.deepEqual(resolveStatusInput('off', tricky), { kind: 'match', entry: tricky[0], via: 'name' });
  assert.deepEqual(resolveStatusInput('sk_one', tricky), { kind: 'match', entry: tricky[1], via: 'alias' });
});

test('blank, unknown and ambiguous text never resolve to a Status', () => {
  assert.deepEqual(resolveStatusInput('', catalog), { kind: 'empty' });
  assert.deepEqual(resolveStatusInput('   ', catalog), { kind: 'empty' });
  assert.deepEqual(resolveStatusInput(null, catalog), { kind: 'empty' });
  assert.deepEqual(resolveStatusInput('PTO_STATUS', catalog), { kind: 'unknown' });
  const shared = [entry(1, 'A', ['x']), entry(2, 'B', ['X'])];
  assert.deepEqual(resolveStatusInput('x', shared), { kind: 'ambiguous' });
});

test('committed text is the canonical primary name, never the alias that was typed', () => {
  assert.equal(committedStatusText('vac', catalog), 'Vacation');
  assert.equal(committedStatusText('SK_BBBB2222', catalog), 'Sick Day');
  assert.equal(committedStatusText('  ', catalog), null);          // blank clears the day
  assert.equal(committedStatusText(' nonsense ', catalog), 'nonsense'); // the server rejects it
});

test('a cell shows the canonical name for saved or typed text, else the text as typed', () => {
  assert.equal(displayedStatusText('v', catalog), 'Vacation');
  assert.equal(displayedStatusText('Vacation', catalog), 'Vacation');
  assert.equal(displayedStatusText(null, catalog), '');
  assert.equal(displayedStatusText('nonsense', catalog), 'nonsense');
});

test('suggestions offer the name and every alias, all labelled with the canonical name', () => {
  assert.deepEqual(statusInputSuggestions(catalog[0]), [
    { value: 'Vacation', label: 'Vacation' },
    { value: 'V', label: 'Vacation' },
    { value: 'Vac', label: 'Vacation' },
  ]);
  assert.deepEqual(statusInputSuggestions(catalog[2]), [{ value: 'Jury Duty', label: 'Jury Duty' }]);
});

test('an untouched row shows the server canonical name; an edited row shows what its text resolves to', () => {
  const row = { status_key: 'SK_AAAA1111', status_label: 'Vacation' };
  assert.equal(statusCellText(row, undefined, catalog), 'Vacation');
  // A dirty row rebuilt from the grid still carries the saved code: unchanged, so the saved label.
  assert.equal(statusCellText(row, { status_key: 'SK_AAAA1111' }, catalog), 'Vacation');
  assert.equal(statusCellText(row, { status_key: 'S' }, catalog), 'Sick Day');
  assert.equal(statusCellText(row, { status_key: null }, catalog), '');
  assert.equal(statusCellText({ status_key: null, status_label: null }, { status_key: 'zzz' }, catalog), 'zzz');
  // A Status deactivated since it was chosen is not in the catalog: the server label is kept.
  assert.equal(statusCellText({ status_key: 'SK_OLD', status_label: 'Retired leave' }, { status_key: 'SK_OLD' }, catalog), 'Retired leave');
});

test('a failed save shows the server message whatever the shape of the error', () => {
  const failure = (detail: unknown) => ({ response: { data: { detail } } });
  assert.equal(daySaveErrorMessage(failure('Period is locked.')), 'Period is locked.');
  assert.equal(
    daySaveErrorMessage(failure({ code: 'STATUS_NOT_FOUND', message: "'zzz' is not a valid active Status in this Company." })),
    "'zzz' is not a valid active Status in this Company.");
  assert.equal(daySaveErrorMessage(failure([{ msg: 'Field required' }, { msg: 'Too long' }])), 'Field required Too long');
  assert.equal(daySaveErrorMessage(failure({ code: 'X' })), 'Save failed.');
  assert.equal(daySaveErrorMessage(new Error('network')), 'Save failed.');
});

test('the payroll entry workbench takes typed Status text through the shared cell and shows canonical names', () => {
  const source = ['PayrollEntryPage.tsx', 'DayEntryTable.tsx']
    .map((name) => read(`src/pages/payroll/${name}`)).join('\n');
  assert.match(source, /<StatusCellInput/);
  assert.match(source, /shownText=\{getStatusText\(row\)\}/);
  assert.match(source, /daySaveErrorMessage/);
  assert.doesNotMatch(source, /getStatusValue|<option key=\{sk\.key_code\}/);
  const cell = read('src/pages/payroll/StatusCellInput.tsx');
  assert.match(cell, /<datalist/);
  assert.match(cell, /statusInputSuggestions\(entry\)/);
  assert.match(cell, /resolution\.entry\.label/);
  const controller = read('src/pages/payroll/useDailyEntryController.ts');
  assert.match(controller, /statusCellText\(row, dirtyRows\.get\(row\.driver_assignment_id\), grid\?\.status_keys \?\? \[\]\)/);
});
