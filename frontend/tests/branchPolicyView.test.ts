import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  currentPolicyLabel,
  scheduledChangeLabel,
  branchPolicyActionKind,
  BRANCH_POLICY_ACTION_LABEL,
} from '../src/pages/settings/payroll/branchPolicyView.ts';
import type { BranchPolicySummaryResponse, PolicyAssignmentSummaryResponse } from '../src/types/payrollSetup.ts';

function makeAssignment(overrides: Partial<PolicyAssignmentSummaryResponse> = {}): PolicyAssignmentSummaryResponse {
  return {
    assignment_id: 1,
    setup_id: 5,
    setup_code: 'STD',
    setup_name: 'Standard Weekly',
    effective_from_date: '2026-09-23',
    effective_to_date: null,
    payroll_frequency: 'Week',
    custom_interval_days: null,
    ...overrides,
  };
}

function makeSummary(overrides: Partial<BranchPolicySummaryResponse> = {}): BranchPolicySummaryResponse {
  return {
    branch_id: 10,
    branch_code: 'DTN',
    branch_name: 'Downtown',
    branch_status: 'Active',
    reference_date: '2026-09-01',
    payroll_set_up: true,
    current: makeAssignment(),
    scheduled_change: null,
    upcoming_assignments: [],
    readiness_reason: 'READY',
    readiness_date: null,
    ...overrides,
  };
}

// ── currentPolicyLabel ──────────────────────────────────────────────────────

test('currentPolicyLabel: not set up at all -> "Payroll not set up"', () => {
  const summary = makeSummary({ payroll_set_up: false, current: null, scheduled_change: null });
  assert.equal(currentPolicyLabel(summary), 'Payroll not set up');
});

test('currentPolicyLabel: current present -> "<name> — <frequency>"', () => {
  const summary = makeSummary({ current: makeAssignment({ setup_name: 'Standard Weekly', payroll_frequency: 'Week' }) });
  assert.equal(currentPolicyLabel(summary), 'Standard Weekly — Weekly');
});

test('currentPolicyLabel: Biweekly and Custom interval frequencies', () => {
  const biweekly = makeSummary({ current: makeAssignment({ setup_name: 'Fortnightly', payroll_frequency: 'Biweek' }) });
  assert.equal(currentPolicyLabel(biweekly), 'Fortnightly — Biweekly');

  const custom = makeSummary({
    current: makeAssignment({ setup_name: 'Custom10', payroll_frequency: 'Custom', custom_interval_days: 10 }),
  });
  assert.equal(currentPolicyLabel(custom), 'Custom10 — 10-day');
});

test('currentPolicyLabel: payroll_set_up but current null (future-only onboarding) -> "Starts <date>"', () => {
  const summary = makeSummary({
    payroll_set_up: true,
    current: null,
    scheduled_change: makeAssignment({ effective_from_date: '2026-10-07' }),
  });
  assert.equal(currentPolicyLabel(summary), 'Starts Oct 7, 2026');
});

// ── scheduledChangeLabel ─────────────────────────────────────────────────────

test('scheduledChangeLabel: no scheduled change -> "None"', () => {
  assert.equal(scheduledChangeLabel(makeSummary({ scheduled_change: null })), 'None');
});

test('scheduledChangeLabel: "Changes <Mon D> → <name> — <frequency>"', () => {
  const summary = makeSummary({
    scheduled_change: makeAssignment({
      effective_from_date: '2026-10-07',
      setup_name: 'New Policy',
      payroll_frequency: 'Biweek',
    }),
  });
  assert.equal(scheduledChangeLabel(summary), 'Changes Oct 7 → New Policy — Biweekly');
});

// ── branchPolicyActionKind / BRANCH_POLICY_ACTION_LABEL ─────────────────────

test('branchPolicyActionKind: unassigned branch -> "assign", never "manage"', () => {
  const summary = makeSummary({ payroll_set_up: false, current: null });
  assert.equal(branchPolicyActionKind(summary), 'assign');
  assert.equal(BRANCH_POLICY_ACTION_LABEL[branchPolicyActionKind(summary)], 'Assign policy');
});

test('branchPolicyActionKind: assigned (or future-onboarded) branch -> "manage"', () => {
  assert.equal(branchPolicyActionKind(makeSummary({ payroll_set_up: true, current: makeAssignment() })), 'manage');
  assert.equal(
    branchPolicyActionKind(makeSummary({ payroll_set_up: true, current: null, scheduled_change: makeAssignment() })),
    'manage',
  );
  assert.equal(BRANCH_POLICY_ACTION_LABEL.manage, 'Manage');
});

test('BRANCH_POLICY_ACTION_LABEL never contains the word "Reassign"', () => {
  for (const label of Object.values(BRANCH_POLICY_ACTION_LABEL)) {
    assert.doesNotMatch(label, /Reassign/);
  }
});

test('Assign modal guards the initial empty effective date before formatting the confirmation message', () => {
  const source = readFileSync(
    new URL('../src/pages/settings/payroll/AssignPolicyModal.tsx', import.meta.url),
    'utf8',
  );
  assert.match(source, /selectedSetup != null && isValidIsoDate\(effectiveFromDate\)/);
  assert.match(source, /const canAssign = setupId != null && choicesReady/);
});

test('Branch assignment action states cover unassigned, current, and future-only summaries', () => {
  assert.equal(branchPolicyActionKind(makeSummary({ payroll_set_up: false, current: null })), 'assign');
  assert.equal(branchPolicyActionKind(makeSummary({ payroll_set_up: true, current: makeAssignment() })), 'manage');
  assert.equal(
    branchPolicyActionKind(makeSummary({ payroll_set_up: true, current: null, scheduled_change: makeAssignment() })),
    'manage',
  );
});
