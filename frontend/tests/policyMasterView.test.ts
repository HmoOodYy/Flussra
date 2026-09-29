import assert from 'node:assert/strict';
import { test } from 'node:test';
import { futureStartLabel, policyUsage, visiblePolicies } from '../src/pages/settings/payroll/policyMasterView.ts';
import type { PolicyUsage } from '../src/pages/settings/payroll/policyMasterView.ts';
import type {
  BranchPolicySummaryResponse,
  PolicyAssignmentSummaryResponse,
  SetupResponse,
} from '../src/types/payrollSetup.ts';

// ── Fixtures ─────────────────────────────────────────────────────────────────

function makeSetup(overrides: Partial<SetupResponse> = {}): SetupResponse {
  return {
    setup_id: 1,
    setup_code: 'STD',
    setup_name: 'Standard',
    description: null,
    status: 'Active',
    ...overrides,
  };
}

function makeAssignment(overrides: Partial<PolicyAssignmentSummaryResponse> = {}): PolicyAssignmentSummaryResponse {
  return {
    assignment_id: 1,
    setup_id: 1,
    setup_code: 'STD',
    setup_name: 'Standard',
    effective_from_date: '2026-01-01',
    effective_to_date: null,
    payroll_frequency: 'Week',
    custom_interval_days: null,
    ...overrides,
  };
}

function makeSummary(overrides: Partial<BranchPolicySummaryResponse> = {}): BranchPolicySummaryResponse {
  return {
    branch_id: 10,
    branch_code: 'ALP',
    branch_name: 'Alpha',
    branch_status: 'Active',
    reference_date: '2026-01-01',
    payroll_set_up: true,
    current: null,
    scheduled_change: null,
    upcoming_assignments: [],
    readiness_reason: 'READY',
    readiness_date: null,
    ...overrides,
  };
}

// ── policyUsage ──────────────────────────────────────────────────────────────

test('policyUsage: a Setup with status Archived is Archived, regardless of any current usage', () => {
  const setups = [makeSetup({ setup_id: 1, status: 'Archived' })];
  const summaries = [makeSummary({ current: makeAssignment({ setup_id: 1 }) })];
  const usage = policyUsage(setups, summaries);
  assert.deepEqual(usage.get(1), { state: 'Archived', futureStart: null });
});

test('policyUsage: any summary whose current.setup_id matches -> Active, regardless of that branch_status', () => {
  const setups = [makeSetup({ setup_id: 1, status: 'Active' })];
  const summaries = [
    makeSummary({ branch_status: 'Archived', current: makeAssignment({ setup_id: 1 }) }),
  ];
  const usage = policyUsage(setups, summaries);
  assert.deepEqual(usage.get(1), { state: 'Active', futureStart: null });
});

test('policyUsage: a raw setup.status of \'Active\' with no current usage becomes Inactive, never Active', () => {
  const setups = [makeSetup({ setup_id: 1, status: 'Active' })];
  const summaries = [makeSummary({ current: null })];
  const usage = policyUsage(setups, summaries);
  assert.equal(usage.get(1)!.state, 'Inactive');
});

test('policyUsage: a policy that only appears in upcoming_assignments (never current) is Inactive', () => {
  const setups = [makeSetup({ setup_id: 1, status: 'Active' })];
  const summaries = [
    makeSummary({
      current: null,
      upcoming_assignments: [makeAssignment({ setup_id: 1, effective_from_date: '2026-05-01' })],
    }),
  ];
  const usage = policyUsage(setups, summaries);
  assert.equal(usage.get(1)!.state, 'Inactive');
  assert.equal(usage.get(1)!.futureStart, '2026-05-01');
});

test('policyUsage: futureStart is null for Active and Archived policies, even if they also appear in upcoming_assignments', () => {
  const setups = [
    makeSetup({ setup_id: 1, status: 'Active' }),
    makeSetup({ setup_id: 2, status: 'Archived' }),
  ];
  const summaries = [
    makeSummary({
      current: makeAssignment({ setup_id: 1 }),
      upcoming_assignments: [
        makeAssignment({ setup_id: 1, effective_from_date: '2026-05-01' }),
        makeAssignment({ setup_id: 2, effective_from_date: '2026-06-01' }),
      ],
    }),
  ];
  const usage = policyUsage(setups, summaries);
  assert.equal((usage.get(1) as PolicyUsage).futureStart, null);
  assert.equal((usage.get(2) as PolicyUsage).futureStart, null);
});

test('policyUsage: futureStart is the minimum effective_from_date across multiple branches', () => {
  const setups = [makeSetup({ setup_id: 1, status: 'Active' })];
  const summaries = [
    makeSummary({
      branch_id: 1,
      current: null,
      upcoming_assignments: [makeAssignment({ setup_id: 1, effective_from_date: '2026-08-01' })],
    }),
    makeSummary({
      branch_id: 2,
      current: null,
      upcoming_assignments: [makeAssignment({ setup_id: 1, effective_from_date: '2026-03-01' })],
    }),
  ];
  const usage = policyUsage(setups, summaries);
  assert.equal(usage.get(1)!.futureStart, '2026-03-01');
});

test('policyUsage: futureStart also takes the minimum across a SECOND future assignment on one Branch', () => {
  const setups = [makeSetup({ setup_id: 1, status: 'Active' })];
  const summaries = [
    makeSummary({
      branch_id: 1,
      current: null,
      upcoming_assignments: [
        makeAssignment({ setup_id: 1, effective_from_date: '2026-09-01' }),
        makeAssignment({ setup_id: 1, effective_from_date: '2026-04-01' }),
      ],
    }),
  ];
  const usage = policyUsage(setups, summaries);
  assert.equal(usage.get(1)!.futureStart, '2026-04-01');
});

test('policyUsage: a policy with no current usage and no upcoming_assignments anywhere is Inactive with futureStart null', () => {
  const setups = [makeSetup({ setup_id: 1, status: 'Active' })];
  const usage = policyUsage(setups, [makeSummary()]);
  assert.deepEqual(usage.get(1), { state: 'Inactive', futureStart: null });
});

// ── visiblePolicies ──────────────────────────────────────────────────────────

function usageMap(entries: Array<[number, PolicyUsage]>): Map<number, PolicyUsage> {
  return new Map(entries);
}

test('visiblePolicies: search matches setup_name case-insensitively, after trim()', () => {
  const setups = [
    makeSetup({ setup_id: 1, setup_name: 'Standard Weekly' }),
    makeSetup({ setup_id: 2, setup_name: 'Driver Biweekly' }),
  ];
  const usage = usageMap([
    [1, { state: 'Active', futureStart: null }],
    [2, { state: 'Active', futureStart: null }],
  ]);
  const result = visiblePolicies(setups, usage, 'All', '  standard  ');
  assert.deepEqual(result.map((s) => s.setup_id), [1]);
});

test('visiblePolicies: an empty search (or whitespace-only) matches everything', () => {
  const setups = [makeSetup({ setup_id: 1 }), makeSetup({ setup_id: 2 })];
  const usage = usageMap([
    [1, { state: 'Active', futureStart: null }],
    [2, { state: 'Active', futureStart: null }],
  ]);
  assert.equal(visiblePolicies(setups, usage, 'All', '').length, 2);
  assert.equal(visiblePolicies(setups, usage, 'All', '   ').length, 2);
});

test('visiblePolicies: search respects the selected filter — it never bypasses it', () => {
  const setups = [
    makeSetup({ setup_id: 1, setup_name: 'Standard Weekly', status: 'Active' }),
    makeSetup({ setup_id: 2, setup_name: 'Standard Archived', status: 'Archived' }),
  ];
  const usage = usageMap([
    [1, { state: 'Active', futureStart: null }],
    [2, { state: 'Archived', futureStart: null }],
  ]);
  // Both names match "standard", but the Active filter excludes the Archived one.
  const result = visiblePolicies(setups, usage, 'Active', 'standard');
  assert.deepEqual(result.map((s) => s.setup_id), [1]);
});

test('visiblePolicies: All orders Active -> Inactive -> Archived, stable within each rank', () => {
  const setups = [
    makeSetup({ setup_id: 1, setup_name: 'Archived One', status: 'Archived' }),
    makeSetup({ setup_id: 2, setup_name: 'Active One' }),
    makeSetup({ setup_id: 3, setup_name: 'Inactive One' }),
    makeSetup({ setup_id: 4, setup_name: 'Active Two' }),
    makeSetup({ setup_id: 5, setup_name: 'Archived Two', status: 'Archived' }),
    makeSetup({ setup_id: 6, setup_name: 'Inactive Two' }),
  ];
  const usage = usageMap([
    [1, { state: 'Archived', futureStart: null }],
    [2, { state: 'Active', futureStart: null }],
    [3, { state: 'Inactive', futureStart: null }],
    [4, { state: 'Active', futureStart: null }],
    [5, { state: 'Archived', futureStart: null }],
    [6, { state: 'Inactive', futureStart: null }],
  ]);
  const result = visiblePolicies(setups, usage, 'All', '');
  assert.deepEqual(result.map((s) => s.setup_id), [2, 4, 3, 6, 1, 5]);
});

test('visiblePolicies: Archived is last in the All ordering', () => {
  const setups = [
    makeSetup({ setup_id: 1, status: 'Archived' }),
    makeSetup({ setup_id: 2, status: 'Active' }),
  ];
  const usage = usageMap([
    [1, { state: 'Archived', futureStart: null }],
    [2, { state: 'Active', futureStart: null }],
  ]);
  const result = visiblePolicies(setups, usage, 'All', '');
  assert.equal(result[result.length - 1].setup_id, 1);
});

test('visiblePolicies: Active filter shows only currently-used policies', () => {
  const setups = [makeSetup({ setup_id: 1 }), makeSetup({ setup_id: 2 })];
  const usage = usageMap([
    [1, { state: 'Active', futureStart: null }],
    [2, { state: 'Inactive', futureStart: null }],
  ]);
  const result = visiblePolicies(setups, usage, 'Active', '');
  assert.deepEqual(result.map((s) => s.setup_id), [1]);
});

test('visiblePolicies: Inactive filter shows only non-archived policies with zero current usage', () => {
  const setups = [
    makeSetup({ setup_id: 1, status: 'Active' }),
    makeSetup({ setup_id: 2, status: 'Active' }),
    makeSetup({ setup_id: 3, status: 'Archived' }),
  ];
  const usage = usageMap([
    [1, { state: 'Active', futureStart: null }],
    [2, { state: 'Inactive', futureStart: null }],
    [3, { state: 'Archived', futureStart: null }],
  ]);
  const result = visiblePolicies(setups, usage, 'Inactive', '');
  assert.deepEqual(result.map((s) => s.setup_id), [2]);
});

test('visiblePolicies: Archived filter shows archived policies only', () => {
  const setups = [
    makeSetup({ setup_id: 1, status: 'Active' }),
    makeSetup({ setup_id: 2, status: 'Archived' }),
    makeSetup({ setup_id: 3, status: 'Archived' }),
  ];
  const usage = usageMap([
    [1, { state: 'Active', futureStart: null }],
    [2, { state: 'Archived', futureStart: null }],
    [3, { state: 'Archived', futureStart: null }],
  ]);
  const result = visiblePolicies(setups, usage, 'Archived', '');
  assert.deepEqual(result.map((s) => s.setup_id), [2, 3]);
});

test('visiblePolicies: single-filter results keep the incoming API order (no re-sort)', () => {
  const setups = [
    makeSetup({ setup_id: 3, setup_name: 'C' }),
    makeSetup({ setup_id: 1, setup_name: 'A' }),
    makeSetup({ setup_id: 2, setup_name: 'B' }),
  ];
  const usage = usageMap([
    [3, { state: 'Active', futureStart: null }],
    [1, { state: 'Active', futureStart: null }],
    [2, { state: 'Active', futureStart: null }],
  ]);
  const result = visiblePolicies(setups, usage, 'Active', '');
  assert.deepEqual(result.map((s) => s.setup_id), [3, 1, 2]);
});

// ── futureStartLabel ─────────────────────────────────────────────────────────

test('futureStartLabel: "Starts " + formatIsoLong(iso)', () => {
  assert.equal(futureStartLabel('2026-10-07'), 'Starts Oct 7, 2026');
});
