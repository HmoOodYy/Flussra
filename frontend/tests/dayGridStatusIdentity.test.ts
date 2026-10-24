import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildSaveRowFromGrid, withTypedStatus } from '../src/pages/payroll/entry/dayGridModel.ts';
import type { DayGridResponse } from '../src/types/payroll/entry';

function gridWithHeldStatus(): DayGridResponse {
  return {
    rows: [
      {
        driver_assignment_id: 7,
        driver_id: 70,
        driver_name: 'Dana',
        driver_number: 'D-7',
        status_key_id: 41,
        status_key: 'SK_AAAA1111',
        status_label: 'Vacation',
        is_off: false,
        notes: 'first note',
        values: { '5': { line_id: 9, quantity: '2', calculated_amount: null, needs_manager_review: false, calculation_status: null } },
      },
      {
        driver_assignment_id: 8,
        driver_id: 80,
        driver_name: 'Eli',
        driver_number: null,
        status_key_id: null,
        status_key: null,
        status_label: null,
        is_off: false,
        notes: null,
        values: {},
      },
    ],
  } as unknown as DayGridResponse;
}

test('a row built from the grid carries the held Status as its identity, with the quantities and note', () => {
  const row = buildSaveRowFromGrid(gridWithHeldStatus(), 7);

  assert.equal(row.status_key_id, 41);
  assert.equal(row.status_key, 'SK_AAAA1111');
  assert.deepEqual(row.values, { '5': '2' });
  assert.equal(row.notes, 'first note');
});

test('a day without a Status carries no Status identity and no text', () => {
  const row = buildSaveRowFromGrid(gridWithHeldStatus(), 8);

  assert.equal(row.status_key_id, null);
  assert.equal(row.status_key, null);
});

test('an unrelated edit keeps the held Status identity', () => {
  const row = buildSaveRowFromGrid(gridWithHeldStatus(), 7);

  const edited = { ...row, notes: 'a new note', values: { ...row.values, '5': '3' } };

  assert.equal(edited.status_key_id, 41);
});

test('typing Status text drops the held identity so the server resolves the text', () => {
  const row = buildSaveRowFromGrid(gridWithHeldStatus(), 7);

  const typed = withTypedStatus(row, 'Sick Day');

  assert.equal(typed.status_key_id, null);
  assert.equal(typed.status_key, 'Sick Day');
  assert.equal(typed.notes, 'first note');
  assert.deepEqual(typed.values, { '5': '2' });
});

test('clearing the Status drops both the identity and the text', () => {
  const row = buildSaveRowFromGrid(gridWithHeldStatus(), 7);

  const cleared = withTypedStatus(row, null);

  assert.equal(cleared.status_key_id, null);
  assert.equal(cleared.status_key, null);
});
