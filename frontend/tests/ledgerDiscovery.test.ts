import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  discoverLedgerPeriods,
  isDiscoveryForbidden,
} from '../src/pages/payroll/ledgerDiscovery.ts';
import type { LedgerDiscoveryDeps } from '../src/pages/payroll/ledgerDiscovery.ts';
import type { FinalizedPeriodListItem, PeriodSummary } from '../src/types/payroll.ts';

function makeFinalized(overrides: Partial<FinalizedPeriodListItem> = {}): FinalizedPeriodListItem {
  return {
    period_id: 1,
    period_code: 'P-1',
    period_name: 'Period 1',
    period_status: 'Locked',
    period_type: 'BiWeekly',
    branch_id: 10,
    branch_name: 'Branch A',
    start_date: '2026-01-01',
    end_date: '2026-01-14',
    pay_date: '2026-01-16',
    finalized_at_utc: '2026-01-15T00:00:00Z',
    ...overrides,
  };
}

function makeOperational(overrides: Partial<PeriodSummary> = {}): PeriodSummary {
  return {
    payroll_period_id: 1,
    branch_id: 10,
    branch_name: 'Branch A',
    parent_period_id: null,
    period_code: 'P-1',
    period_name: 'Period 1',
    period_type: 'BiWeekly',
    start_date: '2026-01-01',
    end_date: '2026-01-14',
    pay_date: '2026-01-16',
    status: 'Locked',
    notes: null,
    created_by_user_id: null,
    created_at_utc: '2026-01-01T00:00:00Z',
    draft_drivers: 0,
    draft_lines: 0,
    draft_lines_needing_attention: 0,
    final_lines: 5,
    final_gross: '1000.00',
    final_driver_count: 3,
    ...overrides,
  };
}

function forbiddenError(): Error {
  return Object.assign(new Error('Forbidden'), {
    isAxiosError: true,
    response: { status: 403 },
  });
}

function axiosErrorWithStatus(status: number): Error {
  return Object.assign(new Error(`HTTP ${status}`), {
    isAxiosError: true,
    response: { status },
  });
}

function networkError(): Error {
  return new Error('Network Error');
}

test('isDiscoveryForbidden: true only for an axios 403 response', () => {
  assert.equal(isDiscoveryForbidden(forbiddenError()), true);
  assert.equal(isDiscoveryForbidden(axiosErrorWithStatus(401)), false);
  assert.equal(isDiscoveryForbidden(axiosErrorWithStatus(404)), false);
  assert.equal(isDiscoveryForbidden(axiosErrorWithStatus(422)), false);
  assert.equal(isDiscoveryForbidden(axiosErrorWithStatus(500)), false);
  assert.equal(isDiscoveryForbidden(networkError()), false);
  assert.equal(isDiscoveryForbidden('not an error'), false);
});

test('discoverLedgerPeriods: merges the same payroll period identity from both authorities', async () => {
  const deps: LedgerDiscoveryDeps = {
    fetchFinalized: async () => [makeFinalized({ period_id: 5 })],
    fetchOperational: async () => [makeOperational({ payroll_period_id: 5 })],
  };

  const result = await discoverLedgerPeriods(deps, 'Locked');

  assert.equal(result.length, 1);
  assert.equal(result[0].identity, 5);
  assert.notEqual(result[0].finalized, null);
  assert.notEqual(result[0].operational, null);
});

test('discoverLedgerPeriods: a 403 from the finalized authority contributes no periods but operational periods remain', async () => {
  const deps: LedgerDiscoveryDeps = {
    fetchFinalized: async () => { throw forbiddenError(); },
    fetchOperational: async () => [makeOperational({ payroll_period_id: 7 })],
  };

  const result = await discoverLedgerPeriods(deps, 'Locked');

  assert.equal(result.length, 1);
  assert.equal(result[0].identity, 7);
  assert.equal(result[0].finalized, null);
  assert.notEqual(result[0].operational, null);
});

test('discoverLedgerPeriods: a 403 from the operational authority contributes no periods but finalized periods remain', async () => {
  const deps: LedgerDiscoveryDeps = {
    fetchFinalized: async () => [makeFinalized({ period_id: 9 })],
    fetchOperational: async () => { throw forbiddenError(); },
  };

  const result = await discoverLedgerPeriods(deps, 'Locked');

  assert.equal(result.length, 1);
  assert.equal(result[0].identity, 9);
  assert.notEqual(result[0].finalized, null);
  assert.equal(result[0].operational, null);
});

test('discoverLedgerPeriods: a non-403 failure from the finalized authority propagates as a real load failure', async () => {
  const thrown = axiosErrorWithStatus(500);
  const deps: LedgerDiscoveryDeps = {
    fetchFinalized: async () => { throw thrown; },
    fetchOperational: async () => [],
  };

  await assert.rejects(() => discoverLedgerPeriods(deps, 'Locked'), (err: unknown) => err === thrown);
});

test('discoverLedgerPeriods: a non-403 failure from the operational authority propagates as a real load failure', async () => {
  const thrown = axiosErrorWithStatus(401);
  const deps: LedgerDiscoveryDeps = {
    fetchFinalized: async () => [],
    fetchOperational: async () => { throw thrown; },
  };

  await assert.rejects(() => discoverLedgerPeriods(deps, 'Locked'), (err: unknown) => err === thrown);
});

test('discoverLedgerPeriods: a plain network failure (no response) propagates rather than being suppressed', async () => {
  const thrown = networkError();
  const deps: LedgerDiscoveryDeps = {
    fetchFinalized: async () => [],
    fetchOperational: async () => { throw thrown; },
  };

  await assert.rejects(() => discoverLedgerPeriods(deps, 'Archived'), (err: unknown) => err === thrown);
});

test('discoverLedgerPeriods: forwards status and branchId unchanged to both authorities', async () => {
  const calls: { finalized?: [string, number | undefined]; operational?: [string, number | undefined] } = {};
  const deps: LedgerDiscoveryDeps = {
    fetchFinalized: async (status, branchId) => { calls.finalized = [status, branchId]; return []; },
    fetchOperational: async (status, branchId) => { calls.operational = [status, branchId]; return []; },
  };

  await discoverLedgerPeriods(deps, 'Archived', 42);

  assert.deepEqual(calls.finalized, ['Archived', 42]);
  assert.deepEqual(calls.operational, ['Archived', 42]);

  await discoverLedgerPeriods(deps, 'Locked');

  assert.deepEqual(calls.finalized, ['Locked', undefined]);
  assert.deepEqual(calls.operational, ['Locked', undefined]);
});

test('discoverLedgerPeriods: mixed-branch sources — branch-A merges both capabilities, branch-B operational-only keeps only Final Lines', async () => {
  const deps: LedgerDiscoveryDeps = {
    // Backend only grants ledger.view for Branch A, so finalized discovery never sees Branch B.
    fetchFinalized: async () => [makeFinalized({ period_id: 1, branch_id: 10, branch_name: 'Branch A' })],
    // Backend grants payroll.view for both Branch A and Branch B.
    fetchOperational: async () => [
      makeOperational({ payroll_period_id: 1, branch_id: 10, branch_name: 'Branch A' }),
      makeOperational({ payroll_period_id: 2, branch_id: 20, branch_name: 'Branch B' }),
    ],
  };

  const result = await discoverLedgerPeriods(deps, 'Locked');

  assert.equal(result.length, 2);

  const branchA = result.find((p) => p.identity === 1);
  assert.ok(branchA);
  assert.notEqual(branchA.finalized, null);
  assert.notEqual(branchA.operational, null);

  const branchB = result.find((p) => p.identity === 2);
  assert.ok(branchB);
  assert.equal(branchB.finalized, null, 'Branch B must not gain library capability from Branch A ledger.view');
  assert.notEqual(branchB.operational, null);
});

test('discoverLedgerPeriods: merged results follow canonical ledger ordering (start_date DESC, then branch_name), not source grouping', async () => {
  const deps: LedgerDiscoveryDeps = {
    fetchFinalized: async () => [
      makeFinalized({ period_id: 1, start_date: '2026-01-05', branch_name: 'Zeta' }),
      makeFinalized({ period_id: 2, start_date: '2026-01-10', branch_name: 'Alpha' }),
    ],
    fetchOperational: async () => [
      makeOperational({ payroll_period_id: 3, start_date: '2026-01-10', branch_name: 'Beta' }),
      makeOperational({ payroll_period_id: 4, start_date: '2026-01-01', branch_name: 'Alpha' }),
    ],
  };

  const result = await discoverLedgerPeriods(deps, 'Locked');

  assert.deepEqual(result.map((p) => p.identity), [2, 3, 1, 4]);
});

test('discoverLedgerPeriods: ties on start_date and branch_name break by identity descending', async () => {
  const deps: LedgerDiscoveryDeps = {
    fetchFinalized: async () => [
      makeFinalized({ period_id: 5, start_date: '2026-02-01', branch_name: 'Alpha' }),
      makeFinalized({ period_id: 6, start_date: '2026-02-01', branch_name: 'Alpha' }),
    ],
    fetchOperational: async () => [],
  };

  const result = await discoverLedgerPeriods(deps, 'Locked');

  assert.deepEqual(result.map((p) => p.identity), [6, 5]);
});
