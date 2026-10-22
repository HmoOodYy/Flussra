import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  apiError,
  buildCreatePayload,
  buildUpdatePayload,
  cleanAliases,
  EMPTY_KEY_FORM,
  keyToForm,
  keysLoadReducer,
  sortStatusKeys,
  statusColumnName,
  validateKeyForm,
  visibleStatusKeys,
  type KeyForm,
} from '../src/pages/settings/status-keys/statusKeysModel.ts';
import {
  activeStatusCount,
  canDeactivateColumn,
  selectableColumns,
  sortColumns,
  validateColumnName,
} from '../src/pages/settings/status-keys/statusColumnsModel.ts';
import type { StatusKey, StatusRateColumn } from '../src/types/settings.ts';

function key(id: number, overrides: Partial<StatusKey> = {}): StatusKey {
  return {
    status_key_id: id, company_id: 1, key_name: `Status ${id}`, aliases: [], status_code: `SK_${id}`,
    category: 'Vacation', hours_value: '8.00', is_off_reason: true, deducts_from_yearly_allowance: false,
    is_active: true, display_order: 0, status_rate_column_id: null, is_in_use: false,
    created_at_utc: '2026-01-01T00:00:00Z', updated_at_utc: null,
    ...overrides,
  };
}

function column(id: number, overrides: Partial<StatusRateColumn> = {}): StatusRateColumn {
  return {
    status_rate_column_id: id, company_id: 1, column_name: `Column ${id}`, is_default: false, is_active: true,
    rate_definition_id: 100 + id, rate_component_definition_id: 200 + id,
    created_at_utc: '2026-01-01T00:00:00Z', updated_at_utc: null,
    ...overrides,
  };
}

const form = (overrides: Partial<KeyForm> = {}): KeyForm => ({ ...EMPTY_KEY_FORM, key_name: 'Vacation', ...overrides });

// ── Aliases ────────────────────────────────────────────────────────────────

test('aliases are trimmed, blank rows dropped and a repeated normal form kept once', () => {
  assert.deepEqual(cleanAliases([' V ', '', '  ', 'vac', 'VAC', 'Ｖac']), ['V', 'vac']);
  assert.deepEqual(cleanAliases([]), []);
});

// ── Validation ─────────────────────────────────────────────────────────────

test('the editor refuses only what is certainly invalid before the request', () => {
  assert.equal(validateKeyForm(form()), '');
  assert.match(validateKeyForm(form({ key_name: '   ' })), /Name is required/);
  assert.match(validateKeyForm(form({ hours_value: '25' })), /between 0 and 24/);
  assert.match(validateKeyForm(form({ hours_value: '' })), /between 0 and 24/);
  assert.match(validateKeyForm(form({ hours_value: '0', status_rate_column_id: '3' })), /greater than 0/);
  assert.equal(validateKeyForm(form({ hours_value: '8', status_rate_column_id: '3' })), '');
  assert.match(validateKeyForm(form({ display_order: '1.5' })), /whole number/);
  assert.match(validateKeyForm(form({ aliases: ['  vacation '] })), /same as the Status name/);
  assert.match(validateKeyForm(form({ category: 'Weekend' as never })), /Category/);
});

// ── Payloads ───────────────────────────────────────────────────────────────

test('a create payload carries the Company Status fields and no Branch, code or limits', () => {
  const payload = buildCreatePayload(form({
    key_name: ' Sick Day ', category: 'Sick', hours_value: '8', is_off_reason: false,
    status_rate_column_id: '4', aliases: ['S', ' ', 'Sick'], display_order: '2',
  }));
  assert.deepEqual(payload, {
    key_name: 'Sick Day', category: 'Sick', aliases: ['S', 'Sick'], hours_value: 8, is_off_reason: false,
    is_active: true, display_order: 2, status_rate_column_id: 4,
  });
  assert.equal(buildCreatePayload(form()).status_rate_column_id, null);
  assert.deepEqual(Object.keys(payload).filter((k) => /branch|code|limit/.test(k)), []);
});

test('an update sends only what changed, and the alias set whole and only when it changed', () => {
  const original = key(1, { key_name: 'Vacation', aliases: ['V', 'Vac'], hours_value: '8.00', status_rate_column_id: 4 });
  const unchanged = keyToForm(original);
  assert.deepEqual(buildUpdatePayload(unchanged, original), {});
  assert.deepEqual(buildUpdatePayload({ ...unchanged, key_name: ' Holiday ' }, original), { key_name: 'Holiday' });
  assert.deepEqual(buildUpdatePayload({ ...unchanged, aliases: ['V', 'Vac', 'Hol'] }, original), { aliases: ['V', 'Vac', 'Hol'] });
  assert.deepEqual(buildUpdatePayload({ ...unchanged, aliases: [] }, original), { aliases: [] });
  assert.deepEqual(buildUpdatePayload({ ...unchanged, aliases: ['V', '', 'Vac'] }, original), {});
  assert.deepEqual(buildUpdatePayload({ ...unchanged, status_rate_column_id: '' }, original), { status_rate_column_id: null });
  assert.deepEqual(
    buildUpdatePayload({ ...unchanged, hours_value: '4', is_off_reason: false, category: 'Sick' }, original),
    { hours_value: 4, is_off_reason: false, category: 'Sick' });
});

test('a Status in use renames and re-aliases without sending its locked fields', () => {
  const original = key(2, { is_in_use: true, aliases: ['S'] });
  const edited = { ...keyToForm(original), key_name: 'Sick leave', aliases: ['S', 'Sick'] };
  assert.deepEqual(buildUpdatePayload(edited, original), { key_name: 'Sick leave', aliases: ['S', 'Sick'] });
});

// ── Catalog view ───────────────────────────────────────────────────────────

test('the catalog is ordered by display order then name and filtered by state', () => {
  const keys = [key(1, { key_name: 'Zeta' }), key(2, { key_name: 'Alpha', display_order: 1 }),
    key(3, { key_name: 'Beta', is_active: false }), key(4, { key_name: 'Alpha2' })];
  assert.deepEqual(sortStatusKeys(keys).map((k) => k.key_name), ['Alpha2', 'Beta', 'Zeta', 'Alpha']);
  assert.deepEqual(visibleStatusKeys(keys, 'Active').map((k) => k.status_key_id), [1, 2, 4]);
  assert.deepEqual(visibleStatusKeys(keys, 'Inactive').map((k) => k.status_key_id), [3]);
});

test('a Status shows the name of its rate column, or none when it is not paid', () => {
  const columns = [column(4, { column_name: 'Holiday pay' })];
  assert.equal(statusColumnName(columns, 4), 'Holiday pay');
  assert.equal(statusColumnName(columns, null), null);
  assert.equal(statusColumnName(columns, 9), 'Column #9');
});

test('the load reducer keeps the selection across a refresh of the same catalog', () => {
  let state = keysLoadReducer({ keys: [], loading: true, error: '', selectedKeyId: null }, { type: 'FETCH_OK', keys: [key(1)] });
  state = keysLoadReducer(state, { type: 'SELECT', id: 1 });
  state = keysLoadReducer(state, { type: 'FETCH_OK', keys: [key(1, { key_name: 'Renamed' })] });
  assert.equal(state.selectedKeyId, 1);
  assert.equal(state.keys[0].key_name, 'Renamed');
  assert.equal(keysLoadReducer(state, { type: 'FETCH_ERROR', error: 'x' }).selectedKeyId, null);
});

// ── Server errors ──────────────────────────────────────────────────────────

test('a coded server error shows its own message; lists and strings still work', () => {
  const failure = (detail: unknown) => ({ response: { data: { detail } } });
  assert.equal(
    apiError(failure({ code: 'STATUS_KEY_IN_USE', message: 'This Status is in use; its hours can no longer change.' })),
    'This Status is in use; its hours can no longer change.');
  assert.equal(apiError(failure({ code: 'STATUS_ALIAS_AMBIGUOUS', message: 'Alias "V" is ambiguous.' })), 'Alias "V" is ambiguous.');
  assert.equal(apiError(failure([{ msg: 'a' }, { msg: 'b' }])), 'a b');
  assert.equal(apiError(failure('plain')), 'plain');
  assert.equal(apiError(failure({ code: 'X' })), 'An unexpected error occurred.');
  assert.equal(apiError(new Error('offline')), 'An unexpected error occurred.');
});

// ── Status rate columns ────────────────────────────────────────────────────

test('columns list the default first, then active ones, then by name', () => {
  const columns = [column(1, { column_name: 'Zed' }), column(2, { column_name: 'Old', is_active: false }),
    column(3, { column_name: 'Base', is_default: true }), column(4, { column_name: 'Alpha' })];
  assert.deepEqual(sortColumns(columns).map((c) => c.status_rate_column_id), [3, 4, 1, 2]);
});

test('a column can be deactivated only when it is not the default and no active Status is paid through it', () => {
  const keys = [key(1, { status_rate_column_id: 5 }), key(2, { status_rate_column_id: 6, is_active: false })];
  assert.equal(activeStatusCount(column(5), keys), 1);
  assert.equal(activeStatusCount(column(6), keys), 0);
  assert.equal(canDeactivateColumn(column(5), keys), false);              // in use
  assert.equal(canDeactivateColumn(column(6), keys), true);               // only an inactive Status uses it
  assert.equal(canDeactivateColumn(column(7, { is_default: true }), keys), false);
  assert.equal(canDeactivateColumn(column(8, { is_active: false }), keys), false);
});

test('a Status can be bound to active columns, and keeps its current column even when inactive', () => {
  const columns = [column(1, { is_default: true }), column(2), column(3, { is_active: false })];
  assert.deepEqual(selectableColumns(columns, null).map((c) => c.status_rate_column_id), [1, 2]);
  assert.deepEqual(selectableColumns(columns, 3).map((c) => c.status_rate_column_id), [1, 2, 3]);
});

test('a column needs a name', () => {
  assert.match(validateColumnName('  '), /required/);
  assert.match(validateColumnName('x'.repeat(201)), /at most 200/);
  assert.equal(validateColumnName('Holiday pay'), '');
});
