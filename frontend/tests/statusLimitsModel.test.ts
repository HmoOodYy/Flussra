import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  LIMIT_SCOPES,
  limitsChanged,
  limitsSummary,
  limitsToForm,
  limitsToPayload,
  validateLimits,
  type LimitsForm,
} from '../src/pages/settings/status-keys/statusLimitsModel.ts';
import type { StatusKeyBranchLimits } from '../src/types/settings.ts';

function limits(overrides: Partial<StatusKeyBranchLimits> = {}): StatusKeyBranchLimits {
  return {
    branch_id: 3, status_key_id: 7, key_name: 'Vacation',
    limit_uses_per_period_enabled: false, limit_uses_per_period: null,
    limit_uses_per_driver_enabled: false, limit_uses_per_driver: null,
    limit_uses_across_drivers_enabled: false, limit_uses_across_drivers: null,
    limit_uses_per_day_enabled: false, limit_uses_per_day: null,
    ...overrides,
  };
}

test('a Status without a row has every limit disabled', () => {
  const form = limitsToForm(limits());
  for (const scope of LIMIT_SCOPES) assert.deepEqual(form[scope], { enabled: false, value: '' });
  assert.equal(limitsSummary(limits()), null);
});

test('the four limits map to the form and back to the whole replacement the server stores', () => {
  const saved = limits({
    limit_uses_per_period_enabled: true, limit_uses_per_period: 5,
    limit_uses_per_day_enabled: true, limit_uses_per_day: 2,
  });
  const form = limitsToForm(saved);
  assert.deepEqual(form.per_period, { enabled: true, value: '5' });
  assert.deepEqual(form.per_driver, { enabled: false, value: '' });
  assert.deepEqual(limitsToPayload(form), {
    limit_uses_per_period_enabled: true, limit_uses_per_period: 5,
    limit_uses_per_driver_enabled: false, limit_uses_per_driver: null,
    limit_uses_across_drivers_enabled: false, limit_uses_across_drivers: null,
    limit_uses_per_day_enabled: true, limit_uses_per_day: 2,
  });
  assert.equal(limitsSummary(saved), 'Per period 5 · Per day 2');
});

test('an enabled limit needs a positive whole count; a disabled one carries none', () => {
  const base = limitsToForm(limits());
  const with_ = (scope: keyof LimitsForm, value: string): LimitsForm => ({ ...base, [scope]: { enabled: true, value } });
  assert.equal(validateLimits(base), '');
  assert.equal(validateLimits(with_('per_driver', '3')), '');
  for (const bad of ['', '0', '-1', '1.5', 'abc', ' ']) {
    assert.match(validateLimits(with_('per_driver', bad)), /Per driver: enter a whole number greater than 0/, bad);
  }
  // A stale count behind a disabled limit is never sent.
  const stale: LimitsForm = { ...base, per_day: { enabled: false, value: '9' } };
  assert.equal(limitsToPayload(stale).limit_uses_per_day, null);
});

test('only a real change makes the row savable', () => {
  const saved = limits({ limit_uses_across_drivers_enabled: true, limit_uses_across_drivers: 10 });
  const form = limitsToForm(saved);
  assert.equal(limitsChanged(form, saved), false);
  assert.equal(limitsChanged({ ...form, across_drivers: { enabled: true, value: ' 10 ' } }, saved), false);
  assert.equal(limitsChanged({ ...form, across_drivers: { enabled: true, value: '11' } }, saved), true);
  assert.equal(limitsChanged({ ...form, across_drivers: { enabled: false, value: '' } }, saved), true);
  assert.equal(limitsChanged({ ...form, per_day: { enabled: true, value: '' } }, saved), true);
});

test('the limits panel writes one Status in one Branch through the limits operation and is edit-gated', () => {
  const panel = readFileSync(new URL('../src/pages/settings/status-keys/StatusLimitsPanel.tsx', import.meta.url), 'utf8');
  assert.match(panel, /onSave=\{\(form\) => limits\.save\(row\.status_key_id, form\)\}/);
  assert.match(panel, /disabled=\{!canEdit \|\| saving\}/);
  assert.match(panel, /counts each\s+Branch assignment of a driver separately/);
});
