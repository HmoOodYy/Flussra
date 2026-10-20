import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { DriverRateSummaryItem, ScheduleSummary } from '../src/types/compensation.ts';
import {
  RETIRED_LABEL,
  buildHistoryRows,
  canVoidHistoryRow,
  historyDefinitionName,
  isRetiredDefinition,
} from '../src/pages/people/pay-rates/payRatesHistory.ts';

function item(id: number, overrides: Partial<DriverRateSummaryItem> = {}): DriverRateSummaryItem {
  return {
    rate_definition_id: id, pay_definition_id: id, definition_code: `C${id}`,
    definition_name: `Definition ${id}`, unit: 'hr', pay_definition_status: 'Active',
    approved_schedule_id: null, approved_effective_from: null, pending_schedule_id: null,
    ...overrides,
  };
}

function schedule(id: number, from: string, status: ScheduleSummary['status'] = 'Approved'): ScheduleSummary {
  return {
    driver_rate_schedule_id: id, company_id: 1, driver_id: 9, pay_definition_id: 1,
    rate_definition_id: 1, effective_from: from, effective_to: null, status,
    created_by_user_id: null, created_at_utc: '2026-01-01T00:00:00Z', updated_by_user_id: null,
    updated_at_utc: null, approved_by_user_id: null, approved_at_utc: null, voided_by_user_id: null,
    voided_at_utc: null, void_reason: null, notes: null, values: [],
  };
}

test('a retired definition is recognised from the summary, not from the authoring matrix', () => {
  assert.equal(isRetiredDefinition(item(1)), false);
  assert.equal(isRetiredDefinition(item(2, { pay_definition_status: 'Retired' })), true);
  assert.equal(isRetiredDefinition(item(3, { pay_definition_status: null })), false);
  assert.equal(RETIRED_LABEL, 'Retired');
});

test('history rows come from every summarized definition, retired included, newest first', () => {
  const items = [item(1), item(2, { pay_definition_status: 'Retired', definition_name: 'Old bonus' })];
  const rows = buildHistoryRows(items, [[schedule(10, '2026-03-01')], [schedule(20, '2026-05-01'), schedule(21, '2026-01-01', 'Superseded')]]);
  assert.deepEqual(rows.map((r) => r.driver_rate_schedule_id), [20, 10, 21]);
  assert.deepEqual(rows.map((r) => r.definition_name), ['Old bonus', 'Definition 1', 'Old bonus']);
  assert.deepEqual(rows.map((r) => r.retired), [true, false, true]);
  assert.deepEqual(rows.map((r) => r.unit), ['hr', 'hr', 'hr']);
});

test('a retired definition\'s rows are read-only; others can be voided only by an editor', () => {
  const [retiredRow, activeRow] = buildHistoryRows(
    [item(2, { pay_definition_status: 'Retired' }), item(1)],
    [[schedule(20, '2026-05-01')], [schedule(10, '2026-03-01')]]);
  assert.equal(canVoidHistoryRow(retiredRow, true), false);
  assert.equal(canVoidHistoryRow(activeRow, true), true);
  assert.equal(canVoidHistoryRow(activeRow, false), false);
  const [pendingRow, voidedRow] = buildHistoryRows(
    [item(1)], [[schedule(30, '2026-06-01', 'Pending'), schedule(31, '2026-02-01', 'Voided')]]);
  assert.equal(canVoidHistoryRow(pendingRow, true), false);
  assert.equal(canVoidHistoryRow(voidedRow, true), false);
});

test('the name falls back to the code, then the rate definition id', () => {
  assert.equal(historyDefinitionName(item(1)), 'Definition 1');
  assert.equal(historyDefinitionName(item(1, { definition_name: null })), 'C1');
  assert.equal(historyDefinitionName(item(7, { definition_name: null, definition_code: null })), 'Rate 7');
});
