import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { ImportLayout, ImportTarget } from '../src/types/payrollImport.ts';
import {
  buildHeaderGroups, buildInitialMappingState, bytesLabel, classifyImportFailureStatus, combineMappedInputs, ignoredOccurrenceSelectors, matchesPreviewPage,
  dateCandidatesForAuthority, dateInterpretationLabel, selectorFromChoices, selectorKey, selectorsOverlap,
} from '../src/lib/payrollImportModel.ts';

test('request failures distinguish definitive rejection from refresh and uncertain outcomes', () => {
  for (const status of [400, 413, 415, 422]) assert.equal(classifyImportFailureStatus(status), 'rejected');
  assert.equal(classifyImportFailureStatus(409), 'conflict');
  assert.equal(classifyImportFailureStatus(429), 'busy');
  for (const status of [401, 403, 404, 410]) assert.equal(classifyImportFailureStatus(status), 'unavailable');
  for (const status of [undefined, 408, 425, 500, 503]) assert.equal(classifyImportFailureStatus(status), 'uncertain');
});

test('date review uses the backend explanation and resolved date', () => {
  assert.equal(dateInterpretationLabel({ status: 'resolved', resolved_date: '2026-10-16', explanation: 'Written month and day in the selected period.' }),
    '2026-10-16 — Written month and day in the selected period.');
  assert.equal(dateInterpretationLabel({ status: 'outside_period', explanation: 'The explicit year is outside this payroll period.' }),
    'outside period: The explicit year is outside this payroll period.');
  assert.equal(dateInterpretationLabel(null), null);
});

test('date candidate suggestions follow only the selected authority', () => {
  const inspection = { filename_date_candidates: ['2026-10-01'], unit_date_candidates: ['2026-10-16'] };
  assert.deepEqual(dateCandidatesForAuthority('filename', inspection), ['2026-10-01']);
  assert.deepEqual(dateCandidatesForAuthority('sheet', inspection), ['2026-10-16']);
  assert.deepEqual(dateCandidatesForAuthority('column', inspection), []);
});

const header = (source_column: number, label: string, normalized_label: string, signature?: string) => ({
  source_column, label, normalized_label,
  ...(signature ? { duplicate_signature: signature, occurrence_ordinal: source_column === 3 ? 1 : 2,
    occurrence_count: 2, occurrence_id: `occ-${source_column}` } : {}),
});

test('header selectors keep server normalization and resolve each duplicate arrangement independently', () => {
  const groups = buildHeaderGroups([
    { layoutId: 1, label: 'daily-a.csv · Sheet1', headers: [header(1, 'Straße', 'strasse'), header(3, 'Hours', 'hours', 'arrangement-a'), header(5, 'Hours', 'hours', 'arrangement-a')] },
    { layoutId: 2, label: 'daily-b.csv · Sheet1', headers: [header(2, 'STRASSE', 'strasse'), header(4, 'Hours', 'hours', 'arrangement-b'), header(8, 'Hours', 'hours', 'arrangement-b')] },
  ]);
  const hours = groups.find((item) => item.normalizedLabel === 'hours');
  assert.ok(hours);
  assert.equal(hours.displayLabel, 'Hours');
  assert.deepEqual(hours.arrangements.map((item) => item.signature), ['arrangement-a', 'arrangement-b']);
  const selector = selectorFromChoices(hours, { 'arrangement-a': 1, 'arrangement-b': 2 });
  assert.deepEqual(selector, { header: 'hours', occurrences: [
    { duplicate_signature: 'arrangement-a', occurrence_ordinal: 1 },
    { duplicate_signature: 'arrangement-b', occurrence_ordinal: 2 },
  ] });
  assert.deepEqual(selectorFromChoices(hours, { 'arrangement-a': 1, 'arrangement-b': null }), {
    header: 'hours', occurrences: [
      { duplicate_signature: 'arrangement-a', occurrence_ordinal: 1 },
      { duplicate_signature: 'arrangement-b', occurrence_ordinal: null },
    ],
  });
  const normalized = groups.find((item) => item.normalizedLabel === 'strasse');
  assert.ok(normalized);
  assert.equal(selectorFromChoices(normalized, { '': 1 })?.header, 'strasse');
});

test('selector identity is order stable and physical overlap respects arrangement and absence', () => {
  const first = { header: 'hours', occurrences: [
    { duplicate_signature: 'a', occurrence_ordinal: 1 },
    { duplicate_signature: 'b', occurrence_ordinal: 2 },
  ] };
  const reordered = { header: 'hours', occurrences: [...first.occurrences].reverse() };
  assert.equal(selectorKey(first), selectorKey(reordered));
  assert.equal(selectorsOverlap(first, { header: 'hours', occurrences: [{ duplicate_signature: 'a', occurrence_ordinal: 1 }] }), true);
  assert.equal(selectorsOverlap(first, { header: 'hours', occurrences: [{ duplicate_signature: 'a', occurrence_ordinal: 2 }] }), false);
  assert.equal(selectorsOverlap(first, { header: 'hours', occurrences: [{ duplicate_signature: 'b', occurrence_ordinal: null }] }), false);
  assert.equal(selectorsOverlap({ header: 'hours' }, { header: 'hours', occurrences: [{ duplicate_signature: null, occurrence_ordinal: 1 }] }), true);
});

test('saved duplicate ignores stay within their own physical arrangement and absent Sum inputs are retained', () => {
  const layouts = [
    { layoutId: 1, label: 'first.csv', headers: [header(3, 'Hours', 'hours', 'arrangement-a'), header(5, 'Hours', 'hours', 'arrangement-a')] },
    { layoutId: 2, label: 'second.csv', headers: [header(4, 'Hours', 'hours', 'arrangement-b'), header(8, 'Hours', 'hours', 'arrangement-b')] },
  ];
  const group = buildHeaderGroups(layouts)[0];
  const sourceLayouts = [{ layout_id: 1, mapping: { '5': 'Ignore' } },
    { layout_id: 2, mapping: { '8': 'Definition:42' } }] as unknown as ImportLayout[];
  const ignored = ignoredOccurrenceSelectors(group, layouts, sourceLayouts);
  assert.deepEqual(ignored, [{ header: 'hours', occurrences: [
    { duplicate_signature: 'arrangement-a', occurrence_ordinal: 2 },
    { duplicate_signature: 'arrangement-b', occurrence_ordinal: null },
  ] }]);
  const savedSecondOperand = { header: 'overtime', occurrences: [{ duplicate_signature: null, occurrence_ordinal: 1 }] };
  assert.deepEqual(combineMappedInputs([{ header: 'hours' }], [savedSecondOperand], true), [
    { header: 'hours' }, savedSecondOperand,
  ]);
  assert.deepEqual(combineMappedInputs([{ header: 'hours' }], [savedSecondOperand], false), [{ header: 'hours' }]);
});

test('retained absent Sum inputs keep their saved operand positions', () => {
  const first = { header: 'first' };
  const present = { header: 'present' };
  const middle = { header: 'middle' };
  const last = { header: 'last' };
  assert.deepEqual(combineMappedInputs([present, last], [first, middle], true, [first, present, middle, last]),
    [first, present, middle, last]);
  assert.deepEqual(combineMappedInputs([present, last], [middle], true, [first, present, middle, last]),
    [present, middle, last]);
});

test('mapping restoration retains off-page rules and missing Sum, identity, and date selectors', () => {
  const layout = { layoutId: 1, label: 'daily.csv', headers: [header(1, 'Hours', 'hours')] };
  const currentRule = {
    payload_version: 1 as const, transform: 'Sum' as const,
    inputs: [{ header: 'hours' }, { header: 'overtime' }],
    shared_columns: { DriverNumber: { header: 'code' }, WorkDate: { header: 'workdate' } },
  };
  const target = {
    target: 'Definition:42', label: 'Regular time', code: 'REG',
    rule_state: { state: 'pending', current_head_revision: 0, current_rule_version_id: null,
      pending_rule_version_id: 8, pending_revision: 2, pending_expected_head_revision: 0,
      pending_rule: currentRule, current_rule: null, reusable: false, reason: 'pending' },
  } as unknown as ImportTarget;
  const initial = buildInitialMappingState(buildHeaderGroups([layout]), [layout], [], [target]);
  assert.deepEqual(initial.drafts.map((draft) => [draft.group, draft.assignment]), [['hours', 'Definition:42']]);
  assert.deepEqual(initial.missingInputs['Definition:42'], [{ header: 'overtime' }]);
  assert.deepEqual(initial.missingShared, { DriverNumber: { header: 'code' }, WorkDate: { header: 'workdate' } });
  assert.deepEqual(combineMappedInputs([{ header: 'hours' }], initial.missingInputs['Definition:42'], true), [
    { header: 'hours' }, { header: 'overtime' },
  ]);
});

test('preview page response must match current import, revision, plan, and generation', () => {
  const request = { importId: 12, revision: 4, plan: 'plan-a', generation: 8 };
  const current = { importId: 12, revision: 4, plan: 'plan-a', generation: 8 };
  assert.equal(matchesPreviewPage(request, current, { revision: 4, plan_fingerprint: 'plan-a' }), true);
  assert.equal(matchesPreviewPage(request, { ...current, revision: 5 }, { revision: 4, plan_fingerprint: 'plan-a' }), false);
  assert.equal(matchesPreviewPage(request, current, { revision: 4, plan_fingerprint: 'plan-b' }), false);
  assert.equal(matchesPreviewPage(request, { ...current, importId: 13 }, { revision: 4, plan_fingerprint: 'plan-a' }), false);
  assert.equal(matchesPreviewPage(request, { ...current, generation: 9 }, { revision: 4, plan_fingerprint: 'plan-a' }), false);
});

test('policy byte labels use decimal upload units', () => {
  assert.equal(bytesLabel(999), '999 bytes');
  assert.equal(bytesLabel(1_000), '1.0 kB');
  assert.equal(bytesLabel(1_000_000), '1.0 MB');
});
