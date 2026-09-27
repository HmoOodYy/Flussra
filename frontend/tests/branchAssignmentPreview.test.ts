import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  activeSetupOptions,
  assignmentIntervalLabel,
  branchLabel,
  buildAssignmentRequest,
  buildReassignmentImpactRequest,
  buildReassignmentRequest,
  buildWithdrawalRequest,
  canReassignFromPreview,
  isReassignPreviewCurrent,
  isWithdrawn,
  normalizeReason,
  sameReassignInputs,
  setupLabel,
} from '../src/pages/settings/payroll/branchAssignmentPreview.ts';
import type {
  ReassignInputs,
  StoredReassignPreview,
} from '../src/pages/settings/payroll/branchAssignmentPreview.ts';
import type {
  AssignmentResponse,
  ReassignmentImpactResponse,
  SetupResponse,
} from '../src/types/payrollSetup.ts';

// ── Fixtures ─────────────────────────────────────────────────────────────────

function makeSetup(overrides: Partial<SetupResponse> = {}): SetupResponse {
  return {
    setup_id: 1,
    setup_code: 'STD',
    setup_name: 'Standard Weekly',
    description: null,
    status: 'Active',
    ...overrides,
  };
}

function makeInputs(overrides: Partial<ReassignInputs> = {}): ReassignInputs {
  return {
    branchId: 10,
    destinationSetupId: 2,
    effectiveFromDate: '2026-02-01',
    ...overrides,
  };
}

function makeImpactResponse(
  overrides: Partial<ReassignmentImpactResponse> = {},
): ReassignmentImpactResponse {
  return {
    branch_id: 10,
    source_setup_id: 1,
    destination_setup_id: 2,
    predecessor_version_id: 100,
    successor_version_id: 200,
    effective_date: '2026-02-01',
    conflicts: [],
    allowed: true,
    ...overrides,
  };
}

function makeStored(overrides: {
  inputs?: Partial<ReassignInputs>;
  response?: Partial<ReassignmentImpactResponse>;
} = {}): StoredReassignPreview {
  return {
    inputs: makeInputs(overrides.inputs),
    response: makeImpactResponse(overrides.response),
  };
}

function makeAssignment(overrides: Partial<AssignmentResponse> = {}): AssignmentResponse {
  return {
    assignment_id: 1,
    branch_id: 10,
    setup_id: 1,
    setup_code: 'STD',
    setup_name: 'Standard Weekly',
    effective_from_date: '2026-01-01',
    effective_to_date: null,
    reason: null,
    created_at_utc: '2026-01-01T00:00:00Z',
    withdrawn_at_utc: null,
    withdrawal_reason: null,
    ...overrides,
  };
}

// ── A. buildAssignmentRequest ───────────────────────────────────────────────

test('A. buildAssignmentRequest: exact object, reason null for blank/whitespace', () => {
  assert.deepEqual(buildAssignmentRequest(5, '2026-03-01', ''), {
    setup_id: 5,
    effective_from_date: '2026-03-01',
    reason: null,
  });
  assert.deepEqual(buildAssignmentRequest(5, '2026-03-01', '   '), {
    setup_id: 5,
    effective_from_date: '2026-03-01',
    reason: null,
  });
});

test('A. buildAssignmentRequest: reason carried verbatim otherwise', () => {
  assert.deepEqual(buildAssignmentRequest(5, '2026-03-01', 'Branch opened'), {
    setup_id: 5,
    effective_from_date: '2026-03-01',
    reason: 'Branch opened',
  });
  assert.deepEqual(buildAssignmentRequest(5, '2026-03-01', '  padded  '), {
    setup_id: 5,
    effective_from_date: '2026-03-01',
    reason: '  padded  ',
  });
});

// ── normalizeReason ──────────────────────────────────────────────────────────

test('normalizeReason: blank/whitespace -> null; otherwise verbatim', () => {
  assert.equal(normalizeReason(''), null);
  assert.equal(normalizeReason('   '), null);
  assert.equal(normalizeReason('reason'), 'reason');
  assert.equal(normalizeReason('  padded  '), '  padded  ');
});

// ── D. buildReassignmentImpactRequest ───────────────────────────────────────

test('D. buildReassignmentImpactRequest: exactly destination_setup_id + effective_from_date', () => {
  const req = buildReassignmentImpactRequest(makeInputs());
  assert.deepEqual(req, { destination_setup_id: 2, effective_from_date: '2026-02-01' });
  assert.deepEqual(Object.keys(req).sort(), ['destination_setup_id', 'effective_from_date']);
});

// ── E. buildReassignmentRequest ─────────────────────────────────────────────

test('E. buildReassignmentRequest: exactly destination_setup_id, effective_from_date, reason', () => {
  const req = buildReassignmentRequest(makeInputs(), 'Route change');
  assert.deepEqual(req, {
    destination_setup_id: 2,
    effective_from_date: '2026-02-01',
    reason: 'Route change',
  });
  assert.deepEqual(
    Object.keys(req).sort(),
    ['destination_setup_id', 'effective_from_date', 'reason'],
  );
});

test('E. buildReassignmentRequest: blank reason -> null', () => {
  assert.deepEqual(buildReassignmentRequest(makeInputs(), ''), {
    destination_setup_id: 2,
    effective_from_date: '2026-02-01',
    reason: null,
  });
});

// ── F. No stored preview -> cannot reassign ─────────────────────────────────

test('F. canReassignFromPreview: no stored preview -> false', () => {
  assert.equal(canReassignFromPreview(makeInputs(), null), false);
});

test('F. isReassignPreviewCurrent: no stored preview -> false', () => {
  assert.equal(isReassignPreviewCurrent(makeInputs(), null), false);
});

test('isReassignPreviewCurrent: current when all three inputs match', () => {
  assert.equal(isReassignPreviewCurrent(makeInputs(), makeStored()), true);
});

// ── G/H/I. Any single-field change makes the preview stale ─────────────────

test('G. branch change -> stale', () => {
  const stored = makeStored();
  const current = makeInputs({ branchId: 99 });
  assert.equal(isReassignPreviewCurrent(current, stored), false);
  assert.equal(canReassignFromPreview(current, stored), false);
});

test('H. destination change -> stale', () => {
  const stored = makeStored();
  const current = makeInputs({ destinationSetupId: 99 });
  assert.equal(isReassignPreviewCurrent(current, stored), false);
  assert.equal(canReassignFromPreview(current, stored), false);
});

test('I. date change -> stale', () => {
  const stored = makeStored();
  const current = makeInputs({ effectiveFromDate: '2026-05-01' });
  assert.equal(isReassignPreviewCurrent(current, stored), false);
  assert.equal(canReassignFromPreview(current, stored), false);
});

test('I. reason is not an input — sameReassignInputs ignores it (inputs carry no reason field)', () => {
  // ReassignInputs has no `reason` key at all; constructing two otherwise-identical
  // inputs objects (as if reason text changed elsewhere in the form) stays equal.
  const a = makeInputs();
  const b = makeInputs();
  assert.equal(sameReassignInputs(a, b), true);
  assert.deepEqual(Object.keys(a).sort(), ['branchId', 'destinationSetupId', 'effectiveFromDate']);
});

// ── J. allowed=false never enables reassign, even when current ─────────────

test('J. canReassignFromPreview: allowed=false never enables reassign even when current', () => {
  const stored = makeStored({ response: { allowed: false } });
  assert.equal(canReassignFromPreview(makeInputs(), stored), false);
});

test('J. canReassignFromPreview: allowed=true and current -> true', () => {
  const stored = makeStored({ response: { allowed: true } });
  assert.equal(canReassignFromPreview(makeInputs(), stored), true);
});

test('J. canReassignFromPreview: allowed=true but stale inputs -> false', () => {
  const stored = makeStored({ response: { allowed: true } });
  const current = makeInputs({ effectiveFromDate: '2026-09-01' });
  assert.equal(canReassignFromPreview(current, stored), false);
});

// ── K. setupLabel ────────────────────────────────────────────────────────────

test('K. setupLabel: resolves code — name when found', () => {
  const setups = [makeSetup({ setup_id: 1, setup_code: 'STD', setup_name: 'Standard Weekly' })];
  assert.equal(setupLabel(1, setups), 'STD — Standard Weekly');
});

test('K. setupLabel: unknown id -> Setup #id', () => {
  const setups = [makeSetup({ setup_id: 1 })];
  assert.equal(setupLabel(42, setups), 'Setup #42');
});

test('K. setupLabel: null -> None', () => {
  const setups = [makeSetup({ setup_id: 1 })];
  assert.equal(setupLabel(null, setups), 'None');
});

// ── L. buildWithdrawalRequest ────────────────────────────────────────────────

test('L. buildWithdrawalRequest: reason carried through normalizeReason', () => {
  assert.deepEqual(buildWithdrawalRequest('Branch closed'), { reason: 'Branch closed' });
  assert.deepEqual(buildWithdrawalRequest(''), { reason: null });
  assert.deepEqual(buildWithdrawalRequest('   '), { reason: null });
});

// ── activeSetupOptions ───────────────────────────────────────────────────────

test('activeSetupOptions: excludes Archived and keeps original order', () => {
  const setups = [
    makeSetup({ setup_id: 1, status: 'Active' }),
    makeSetup({ setup_id: 2, status: 'Archived' }),
    makeSetup({ setup_id: 3, status: 'Active' }),
  ];
  const result = activeSetupOptions(setups);
  assert.deepEqual(result.map((s) => s.setup_id), [1, 3]);
});

test('activeSetupOptions: empty list -> empty', () => {
  assert.deepEqual(activeSetupOptions([]), []);
});

// ── branchLabel ──────────────────────────────────────────────────────────────

test('branchLabel: resolves name (code) when found', () => {
  const branches = [{ branch_id: 10, branch_name: 'Downtown', branch_code: 'DTN' }];
  assert.equal(branchLabel(10, branches), 'Downtown (DTN)');
});

test('branchLabel: unknown id falls back to Branch #id', () => {
  const branches = [{ branch_id: 10, branch_name: 'Downtown', branch_code: 'DTN' }];
  assert.equal(branchLabel(99, branches), 'Branch #99');
});

// ── assignmentIntervalLabel ──────────────────────────────────────────────────

test('assignmentIntervalLabel: open-ended when effective_to_date is null', () => {
  assert.equal(
    assignmentIntervalLabel({ effective_from_date: '2026-01-01', effective_to_date: null }),
    '2026-01-01 → open-ended',
  );
});

test('assignmentIntervalLabel: bounded interval', () => {
  assert.equal(
    assignmentIntervalLabel({ effective_from_date: '2026-01-01', effective_to_date: '2026-06-01' }),
    '2026-01-01 → 2026-06-01',
  );
});

// ── isWithdrawn ──────────────────────────────────────────────────────────────

test('isWithdrawn: true when withdrawn_at_utc is non-null', () => {
  assert.equal(isWithdrawn(makeAssignment({ withdrawn_at_utc: '2026-02-01T00:00:00Z' })), true);
});

test('isWithdrawn: false when withdrawn_at_utc is null', () => {
  assert.equal(isWithdrawn(makeAssignment({ withdrawn_at_utc: null })), false);
});
