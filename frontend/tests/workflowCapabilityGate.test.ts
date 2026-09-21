import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  getPeriodWorkflowCapabilities,
  isHubActiveWorkflowStatus,
  resolveCapabilityGate,
  WORKFLOW_CONTEXT_UNAVAILABLE_MESSAGE,
} from '../src/pages/payroll/workflowCapabilityGate.ts';
import type {
  CurrentPayrollHubBranch,
  PeriodWorkflowCapabilities,
  WorkflowCapability,
} from '../src/types/payroll.ts';

function makeCapability(overrides: Partial<WorkflowCapability> = {}): WorkflowCapability {
  return { allowed: true, reason_code: null, reason_message: null, ...overrides };
}

function makePeriodCapabilities(
  overrides: Partial<PeriodWorkflowCapabilities> = {},
): PeriodWorkflowCapabilities {
  return {
    can_enter_source: makeCapability(),
    can_submit_for_review: makeCapability(),
    can_resubmit_returned: makeCapability(),
    can_view_review: makeCapability(),
    can_cancel: makeCapability(),
    can_open_day_grid: makeCapability(),
    ...overrides,
  };
}

function makeBranch(overrides: Partial<CurrentPayrollHubBranch> = {}): CurrentPayrollHubBranch {
  return {
    branch_id: 10,
    branch_name: 'Branch A',
    setup_status: 'complete',
    slots: { open: null, prepared: null, in_review: null, returned: null },
    capabilities: {
      can_view_current_workflow: makeCapability(),
      can_create_open_candidate: makeCapability(),
      can_create_prepared_candidate: makeCapability(),
      can_view_candidates: makeCapability(),
      periods: {},
    },
    alerts: [],
    ...overrides,
  };
}

function branchWithPeriods(
  periods: Record<string, PeriodWorkflowCapabilities>,
  overrides: Partial<CurrentPayrollHubBranch> = {},
): CurrentPayrollHubBranch {
  return makeBranch({
    capabilities: {
      can_view_current_workflow: makeCapability(),
      can_create_open_candidate: makeCapability(),
      can_create_prepared_candidate: makeCapability(),
      can_view_candidates: makeCapability(),
      periods,
    },
    ...overrides,
  });
}

// ── getPeriodWorkflowCapabilities ───────────────────────────────────────────

test('getPeriodWorkflowCapabilities: returns the matching period capability entry', () => {
  const periodCaps = makePeriodCapabilities();
  const branch = branchWithPeriods({ '501': periodCaps });
  const result = getPeriodWorkflowCapabilities([branch], 10, 501);
  assert.equal(result, periodCaps);
});

test('getPeriodWorkflowCapabilities: null when the branch is not present in the Hub response', () => {
  const branch = makeBranch({ branch_id: 10 });
  const result = getPeriodWorkflowCapabilities([branch], 999, 501);
  assert.equal(result, null);
});

test('getPeriodWorkflowCapabilities: null when the period has no entry in the branch capability map (e.g. Approved)', () => {
  const branch = branchWithPeriods({ '111': makePeriodCapabilities() });
  const result = getPeriodWorkflowCapabilities([branch], 10, 501);
  assert.equal(result, null);
});

test('getPeriodWorkflowCapabilities: matches by exact branch_id across multiple branches', () => {
  const capsA = makePeriodCapabilities({ can_submit_for_review: makeCapability({ allowed: false }) });
  const capsB = makePeriodCapabilities({ can_submit_for_review: makeCapability({ allowed: true }) });
  const branchA = branchWithPeriods({ '501': capsA }, { branch_id: 10 });
  const branchB = branchWithPeriods({ '501': capsB }, { branch_id: 20 });
  assert.equal(getPeriodWorkflowCapabilities([branchA, branchB], 20, 501), capsB);
});

// ── isHubActiveWorkflowStatus ────────────────────────────────────────────────

test('isHubActiveWorkflowStatus: true for Draft, Open, InReview, Returned', () => {
  assert.equal(isHubActiveWorkflowStatus('Draft'), true);
  assert.equal(isHubActiveWorkflowStatus('Open'), true);
  assert.equal(isHubActiveWorkflowStatus('InReview'), true);
  assert.equal(isHubActiveWorkflowStatus('Returned'), true);
});

test('isHubActiveWorkflowStatus: false for Approved, Locked, Cancelled, Archived', () => {
  assert.equal(isHubActiveWorkflowStatus('Approved'), false);
  assert.equal(isHubActiveWorkflowStatus('Locked'), false);
  assert.equal(isHubActiveWorkflowStatus('Cancelled'), false);
  assert.equal(isHubActiveWorkflowStatus('Archived'), false);
});

// ── resolveCapabilityGate — Open / Submit ───────────────────────────────────

test('1. Open + can_submit_for_review allowed -> enabled', () => {
  const result = resolveCapabilityGate(makeCapability({ allowed: true }), isHubActiveWorkflowStatus('Open'));
  assert.deepEqual(result, { disabled: false, reasonMessage: null });
});

test('2. Open + denied capability -> disabled with backend reason', () => {
  const result = resolveCapabilityGate(
    makeCapability({
      allowed: false,
      reason_code: 'RETURNED_BACKLOG_BLOCKS_SUBMIT',
      reason_message: 'Returned backlog (ending 2026-01-14) must be resolved first.',
    }),
    isHubActiveWorkflowStatus('Open'),
  );
  assert.deepEqual(result, {
    disabled: true,
    reasonMessage: 'Returned backlog (ending 2026-01-14) must be resolved first.',
  });
});

test('3. Open + missing active capability -> disabled as context unavailable (fail closed, not a fabricated business reason)', () => {
  const result = resolveCapabilityGate(null, isHubActiveWorkflowStatus('Open'));
  assert.deepEqual(result, { disabled: true, reasonMessage: WORKFLOW_CONTEXT_UNAVAILABLE_MESSAGE });
});

// ── resolveCapabilityGate — Returned / Resubmit ─────────────────────────────

test('4. Returned + can_resubmit_returned allowed -> enabled', () => {
  const result = resolveCapabilityGate(makeCapability({ allowed: true }), isHubActiveWorkflowStatus('Returned'));
  assert.deepEqual(result, { disabled: false, reasonMessage: null });
});

test('5. Returned + denied -> disabled with backend reason', () => {
  const result = resolveCapabilityGate(
    makeCapability({
      allowed: false,
      reason_code: 'PERMISSION_DENIED',
      reason_message: 'payroll.entry required to resubmit.',
    }),
    isHubActiveWorkflowStatus('Returned'),
  );
  assert.deepEqual(result, { disabled: true, reasonMessage: 'payroll.entry required to resubmit.' });
});

test('6. Returned + missing active capability -> disabled as context unavailable', () => {
  const result = resolveCapabilityGate(null, isHubActiveWorkflowStatus('Returned'));
  assert.deepEqual(result, { disabled: true, reasonMessage: WORKFLOW_CONTEXT_UNAVAILABLE_MESSAGE });
});

// ── resolveCapabilityGate — Draft/Open/Returned / Day Grid ──────────────────

test('7. Draft/Open/Returned + can_open_day_grid denied -> disabled and backend reason exposed structurally', () => {
  for (const status of ['Draft', 'Open', 'Returned'] as const) {
    const result = resolveCapabilityGate(
      makeCapability({
        allowed: false,
        reason_code: 'PERMISSION_DENIED',
        reason_message: 'payroll.view or payroll.entry required.',
      }),
      isHubActiveWorkflowStatus(status),
    );
    assert.equal(result.disabled, true, `${status}: Day Grid must be disabled`);
    assert.equal(
      result.reasonMessage,
      'payroll.view or payroll.entry required.',
      `${status}: backend reason must be exposed verbatim`,
    );
  }
});

test('7b. Draft/Open/Returned + missing active Day Grid capability -> disabled as context unavailable, not silently enabled', () => {
  for (const status of ['Draft', 'Open', 'Returned'] as const) {
    const result = resolveCapabilityGate(null, isHubActiveWorkflowStatus(status));
    assert.deepEqual(result, { disabled: true, reasonMessage: WORKFLOW_CONTEXT_UNAVAILABLE_MESSAGE }, status);
  }
});

// ── resolveCapabilityGate — Approved (expected absence) ─────────────────────

test('8. Approved + no Hub capability -> existing View Payroll behavior remains unchanged (not disabled, no reason)', () => {
  const result = resolveCapabilityGate(null, isHubActiveWorkflowStatus('Approved'));
  assert.deepEqual(result, { disabled: false, reasonMessage: null });
});

test('8b. Approved + capability somehow present and allowed -> still enabled', () => {
  const result = resolveCapabilityGate(makeCapability({ allowed: true }), isHubActiveWorkflowStatus('Approved'));
  assert.deepEqual(result, { disabled: false, reasonMessage: null });
});

// ── resolveCapabilityGate — general shape guarantees ─────────────────────────

test('resolveCapabilityGate: allowed=false with a null reason_message still disables (no reason fabricated)', () => {
  const result = resolveCapabilityGate(makeCapability({ allowed: false, reason_message: null }), true);
  assert.deepEqual(result, { disabled: true, reasonMessage: null });
});

test('resolveCapabilityGate: a present, allowed capability always wins over isActiveWorkflowStatus', () => {
  const result = resolveCapabilityGate(makeCapability({ allowed: true }), false);
  assert.deepEqual(result, { disabled: false, reasonMessage: null });
});
