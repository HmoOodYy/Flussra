import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import type { DriverRateSummaryItem, ScheduleSummary, ScheduleValue } from '../src/types/compensation.ts';
import { buildHistoryRows } from '../src/pages/people/pay-rates/payRatesHistory.ts';
import {
  buildPendingScheduleRows,
  canApprovePendingRow,
  isPendingRowComplete,
} from '../src/pages/people/pay-rates/payRatesPending.ts';
import {
  hasCompleteValues,
  isTieredSchedule,
  scalarAmount,
  tierRangeLabel,
  tierValueViews,
} from '../src/pages/people/pay-rates/payRatesTiers.ts';

const TIERED = 7;
const SCALAR = 8;

function tier(sequenceNo: number, from: number, to: number | null, amount: string | null): ScheduleValue {
  return { rate_component_definition_id: 100 + sequenceNo, sequence_no: sequenceNo, ordinal_from: from, ordinal_to: to, amount };
}

const THREE_FOUR_FIVE = [tier(1, 1, 1, '3.0000'), tier(2, 2, 2, '4.0000'), tier(3, 3, null, '5.0000')];
const SCALAR_VALUE: ScheduleValue[] = [
  { rate_component_definition_id: 1, sequence_no: 1, ordinal_from: null, ordinal_to: null, amount: '25.0000' }];

function summary(id: number, overrides: Partial<DriverRateSummaryItem> = {}): DriverRateSummaryItem {
  return {
    rate_definition_id: id, pay_definition_id: id, definition_code: `C${id}`,
    definition_name: `Definition ${id}`, unit: 'stop', pay_definition_status: 'Active',
    approved_schedule_id: null, approved_effective_from: null, pending_schedule_id: null,
    ...overrides,
  };
}

function schedule(id: number, rateDefinitionId: number, status: ScheduleSummary['status'], values: ScheduleValue[]): ScheduleSummary {
  return {
    driver_rate_schedule_id: id, company_id: 1, driver_id: 9, pay_definition_id: rateDefinitionId,
    rate_definition_id: rateDefinitionId, effective_from: '2026-06-01', effective_to: null, status,
    created_by_user_id: null, created_at_utc: '2026-01-01T00:00:00Z', updated_by_user_id: null,
    updated_at_utc: null, approved_by_user_id: null, approved_at_utc: null, voided_by_user_id: null,
    voided_at_utc: null, void_reason: null, notes: null, values,
  };
}

// ---------------------------------------------------------------------------
// The tier model
// ---------------------------------------------------------------------------

test('a 3 / 4 / 5 schedule is tiered and is never projected as only its first value', () => {
  assert.equal(isTieredSchedule(THREE_FOUR_FIVE), true);
  assert.equal(scalarAmount(THREE_FOUR_FIVE), null);
  assert.deepEqual(tierValueViews(THREE_FOUR_FIVE), [
    { label: '1', amount: '3.0000' }, { label: '2', amount: '4.0000' }, { label: '3+', amount: '5.0000' }]);
});

test('a scalar schedule keeps its one amount and has no tier views', () => {
  assert.equal(isTieredSchedule(SCALAR_VALUE), false);
  assert.equal(scalarAmount(SCALAR_VALUE), '25.0000');
  assert.deepEqual(tierValueViews(SCALAR_VALUE), []);
  assert.equal(scalarAmount([]), null);
});

test('tier ranges read as positions, and tiers keep topology order whatever the response order', () => {
  assert.equal(tierRangeLabel(1, 1), '1');
  assert.equal(tierRangeLabel(2, 4), '2–4');
  assert.equal(tierRangeLabel(5, null), '5+');
  assert.deepEqual(
    tierValueViews([THREE_FOUR_FIVE[2], THREE_FOUR_FIVE[0], THREE_FOUR_FIVE[1]]).map((t) => t.label), ['1', '2', '3+']);
});

test('completeness needs every tier: a missing tier value is not zero and blocks approval', () => {
  assert.equal(hasCompleteValues(THREE_FOUR_FIVE), true);
  assert.equal(hasCompleteValues([tier(1, 1, 1, '3.0000'), tier(2, 2, null, null)]), false);
  assert.equal(hasCompleteValues([tier(1, 1, 1, '0.0000'), tier(2, 2, null, '0.0000')]), true);
  assert.equal(hasCompleteValues([]), false);
  assert.equal(hasCompleteValues(SCALAR_VALUE), true);
});

// ---------------------------------------------------------------------------
// Pending
// ---------------------------------------------------------------------------

test('a tiered Pending row carries every tier and no scalar amount', () => {
  const rows = buildPendingScheduleRows(
    [summary(TIERED, { pending_schedule_id: 50 })],
    { [TIERED]: [schedule(50, TIERED, 'Pending', THREE_FOUR_FIVE)] }, []);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].tiered, true);
  assert.equal(rows[0].amount, null);
  assert.deepEqual(tierValueViews(rows[0].values).map((t) => t.amount), ['3.0000', '4.0000', '5.0000']);
  assert.equal(isPendingRowComplete(rows[0]), true);
});

test('an incomplete tiered Pending row is never approvable from this screen', () => {
  const partial = [tier(1, 1, 1, '3.0000'), tier(2, 2, 2, null), tier(3, 3, null, '5.0000')];
  const [row] = buildPendingScheduleRows(
    [summary(TIERED, { pending_schedule_id: 51 })],
    { [TIERED]: [schedule(51, TIERED, 'Pending', partial)] }, []);
  assert.equal(row.tiered, true);
  assert.equal(isPendingRowComplete(row), false);
});

test('a scalar Pending row is unchanged: one amount, approvable when set', () => {
  const [row] = buildPendingScheduleRows(
    [summary(SCALAR, { pending_schedule_id: 60 })],
    { [SCALAR]: [schedule(60, SCALAR, 'Pending', SCALAR_VALUE)] }, []);
  assert.equal(row.tiered, false);
  assert.equal(row.amount, '25.0000');
  assert.equal(isPendingRowComplete(row), true);
  assert.equal(canApprovePendingRow(row, true), true);
  const [unset] = buildPendingScheduleRows(
    [summary(SCALAR, { pending_schedule_id: 61 })],
    { [SCALAR]: [schedule(61, SCALAR, 'Pending', [{ ...SCALAR_VALUE[0], amount: null }])] }, []);
  assert.equal(unset.amount, null);
  assert.equal(isPendingRowComplete(unset), false);
});

test('the Pending screen lists every tier and gates Approve on all of them', () => {
  const read = (name: string) => readFileSync(
    new URL(`../src/pages/people/pay-rates/${name}`, import.meta.url), 'utf8');
  const pending = read('PendingRateChanges.tsx');
  assert.match(pending, /row\.tiered/);
  assert.match(pending, /<TierValueList values=\{row\.values\}/);
  assert.match(pending, /!isPendingRowComplete\(row\)/);
  assert.doesNotMatch(pending, /row\.amount === null\}/);        // approval no longer keys off one amount
  assert.doesNotMatch(pending, /values\[0\]/);
  const model = read('payRatesPending.ts');
  assert.doesNotMatch(model, /values\[0\]/);
  const list = read('TierValueList.tsx');
  assert.match(list, /tierValueViews\(values\)\.map/);
});

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

test('history rows keep every tier value of a tiered schedule', () => {
  const rows = buildHistoryRows(
    [summary(TIERED), summary(SCALAR)],
    [[schedule(70, TIERED, 'Approved', THREE_FOUR_FIVE)], [schedule(71, SCALAR, 'Approved', SCALAR_VALUE)]]);
  const tiered = rows.find((r) => r.driver_rate_schedule_id === 70)!;
  const scalar = rows.find((r) => r.driver_rate_schedule_id === 71)!;
  assert.equal(isTieredSchedule(tiered.values), true);
  assert.equal(scalarAmount(tiered.values), null);
  assert.equal(tierValueViews(tiered.values).length, 3);
  assert.equal(scalarAmount(scalar.values), '25.0000');
});

test('the History tab never shows the first tier under a scalar Amount label', () => {
  const history = readFileSync(
    new URL('../src/pages/people/pay-rates/RateHistoryTab.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(history, /values\[0\]/);
  assert.match(history, /isTieredSchedule\(r\.values\)/);
  assert.match(history, /<TierValueList values=\{r\.values\}/);
  assert.match(history, /fmtAmount\(scalarAmount\(r\.values\)/);
});
