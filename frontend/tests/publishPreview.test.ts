import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  affectedBranchLabels,
  buildImpactRequest,
  buildInlinePublicationRequest,
  buildPublishRequest,
  canPublishFromPreview,
  canPublishInlineFromPreview,
  correctionCandidate,
  isPreviewCurrent,
  sameInlineInputs,
  sameInputs,
} from '../src/pages/settings/payroll/publishPreview.ts';
import type {
  InlinePreviewInputs,
  PreviewInputs,
  StoredPreview,
} from '../src/pages/settings/payroll/publishPreview.ts';
import type { PublicationImpactResponse } from '../src/types/payrollSetup.ts';

function makeInputs(overrides: Partial<PreviewInputs> = {}): PreviewInputs {
  return {
    setupId: 1,
    draftId: 50,
    draftScheduleKey: '["Week","2026-01-04",null,65]',
    effectiveFromDate: '2026-01-04',
    replacesVersionId: null,
    ...overrides,
  };
}

function makeResponse(overrides: Partial<PublicationImpactResponse> = {}): PublicationImpactResponse {
  return {
    setup_id: 1,
    affected_branch_ids: [10, 20],
    effective_date: '2026-01-04',
    predecessor_version_id: null,
    predecessor_hash: null,
    current_same_date_version_id: null,
    successor_hash: 'hash123',
    successor_schedule: {
      payroll_frequency: 'Week',
      anchor_start_date: '2026-01-04',
      custom_interval_days: null,
      normal_days_off_mask: 65,
    },
    next_version_boundary: null,
    conflicts: [],
    allowed: true,
    ...overrides,
  };
}

function makeStored(overrides: {
  inputs?: Partial<PreviewInputs>;
  response?: Partial<PublicationImpactResponse>;
} = {}): StoredPreview {
  return {
    inputs: makeInputs(overrides.inputs),
    response: makeResponse(overrides.response),
  };
}

function makeInlineInputs(overrides: Partial<InlinePreviewInputs> = {}): InlinePreviewInputs {
  return {
    setupId: 1,
    schedule: {
      payroll_frequency: 'Week',
      anchor_start_date: '2026-01-04',
      custom_interval_days: null,
      normal_days_off_mask: 65,
    },
    scheduleKey: '["Week","2026-01-04",null,65]',
    effectiveFromDate: '2026-01-04',
    replacesVersionId: null,
    ...overrides,
  };
}

// ── sameInputs / F. isPreviewCurrent when all five inputs match ────────────

test('sameInputs: true when all five fields match', () => {
  assert.equal(sameInputs(makeInputs(), makeInputs()), true);
});

test('F. isPreviewCurrent: current when all five inputs match', () => {
  const stored = makeStored();
  assert.equal(isPreviewCurrent(makeInputs(), stored), true);
});

test('isPreviewCurrent: stored null -> false', () => {
  assert.equal(isPreviewCurrent(makeInputs(), null), false);
});

test('sameInlineInputs and canPublishInlineFromPreview protect the create-mode snapshot', () => {
  const inputs = makeInlineInputs();
  const stored = { inputs, response: makeResponse({ allowed: true }) };
  assert.equal(sameInlineInputs(inputs, makeInlineInputs()), true);
  assert.equal(canPublishInlineFromPreview(inputs, stored), true);
  assert.equal(
    canPublishInlineFromPreview(
      makeInlineInputs({ effectiveFromDate: '2026-02-01' }),
      stored,
    ),
    false,
  );
  assert.equal(
    canPublishInlineFromPreview(
      makeInlineInputs({ scheduleKey: '["Biweek","2026-01-04",null,65]' }),
      stored,
    ),
    false,
  );
  assert.equal(
    canPublishInlineFromPreview(inputs, { inputs, response: makeResponse({ allowed: false }) }),
    false,
  );
});

// ── G/H/I. Any single-field change makes the preview stale ─────────────────

test('G. effective date change -> stale', () => {
  const stored = makeStored();
  const current = makeInputs({ effectiveFromDate: '2026-01-11' });
  assert.equal(isPreviewCurrent(current, stored), false);
});

test('H. draftId change -> stale', () => {
  const stored = makeStored();
  const current = makeInputs({ draftId: 99 });
  assert.equal(isPreviewCurrent(current, stored), false);
});

test('H. draftScheduleKey change -> stale', () => {
  const stored = makeStored();
  const current = makeInputs({ draftScheduleKey: '["Biweek","2026-01-04",null,65]' });
  assert.equal(isPreviewCurrent(current, stored), false);
});

test('I. replacesVersionId change (null <-> id) -> stale', () => {
  const stored = makeStored({ inputs: { replacesVersionId: null } });
  const current = makeInputs({ replacesVersionId: 7 });
  assert.equal(isPreviewCurrent(current, stored), false);

  const stored2 = makeStored({ inputs: { replacesVersionId: 7 } });
  const current2 = makeInputs({ replacesVersionId: null });
  assert.equal(isPreviewCurrent(current2, stored2), false);
});

test('setupId change -> stale', () => {
  const stored = makeStored();
  const current = makeInputs({ setupId: 2 });
  assert.equal(isPreviewCurrent(current, stored), false);
});

// ── J. allowed=false never enables publish, even when current ──────────────

test('J. canPublishFromPreview: allowed=false never enables publish even when current', () => {
  const stored = makeStored({ response: { allowed: false } });
  assert.equal(canPublishFromPreview(makeInputs(), stored), false);
});

test('J. canPublishFromPreview: allowed=true and current -> true', () => {
  const stored = makeStored({ response: { allowed: true } });
  assert.equal(canPublishFromPreview(makeInputs(), stored), true);
});

test('J. canPublishFromPreview: stored null -> false', () => {
  assert.equal(canPublishFromPreview(makeInputs(), null), false);
});

test('J. canPublishFromPreview: allowed=true but stale inputs -> false', () => {
  const stored = makeStored({ response: { allowed: true } });
  const current = makeInputs({ effectiveFromDate: '2026-02-01' });
  assert.equal(canPublishFromPreview(current, stored), false);
});

// ── K/L. correctionCandidate ────────────────────────────────────────────────

test('K. correctionCandidate: returns exactly response.current_same_date_version_id', () => {
  const response = makeResponse({ current_same_date_version_id: 42 });
  assert.equal(correctionCandidate(response), 42);
});

test('L. correctionCandidate: null when absent', () => {
  const response = makeResponse({ current_same_date_version_id: null });
  assert.equal(correctionCandidate(response), null);
});

// ── buildImpactRequest / buildPublishRequest ────────────────────────────────

test('buildImpactRequest: carries effective_from_date and replaces_version_id from inputs', () => {
  const inputs = makeInputs({ effectiveFromDate: '2026-05-01', replacesVersionId: 12 });
  assert.deepEqual(buildImpactRequest(inputs), {
    effective_from_date: '2026-05-01',
    replaces_version_id: 12,
  });
});

test('buildImpactRequest: replaces_version_id null when not correcting', () => {
  const inputs = makeInputs({ effectiveFromDate: '2026-05-01', replacesVersionId: null });
  assert.deepEqual(buildImpactRequest(inputs), {
    effective_from_date: '2026-05-01',
    replaces_version_id: null,
  });
});

test('buildPublishRequest: carries effective_from_date and replaces_version_id from inputs', () => {
  const inputs = makeInputs({ effectiveFromDate: '2026-05-01', replacesVersionId: 12 });
  assert.deepEqual(buildPublishRequest(inputs), {
    effective_from_date: '2026-05-01',
    replaces_version_id: 12,
  });
});

test('buildPublishRequest: replaces_version_id null when not correcting', () => {
  const inputs = makeInputs({ effectiveFromDate: '2026-05-01', replacesVersionId: null });
  assert.deepEqual(buildPublishRequest(inputs), {
    effective_from_date: '2026-05-01',
    replaces_version_id: null,
  });
});

test('buildInlinePublicationRequest publishes exactly the stored schedule snapshot', () => {
  const inputs = makeInlineInputs({
    schedule: {
      payroll_frequency: 'Custom',
      anchor_start_date: '2026-01-04',
      custom_interval_days: 14,
      normal_days_off_mask: 65,
    },
    effectiveFromDate: '2026-02-01',
    replacesVersionId: 12,
  });
  assert.deepEqual(buildInlinePublicationRequest(inputs), {
    payroll_frequency: 'Custom',
    anchor_start_date: '2026-01-04',
    custom_interval_days: 14,
    normal_days_off_mask: 65,
    effective_from_date: '2026-02-01',
    replaces_version_id: 12,
  });
});

// ── affectedBranchLabels ─────────────────────────────────────────────────────

test('affectedBranchLabels: maps known branch names, falls back to Branch #id, preserves order and length', () => {
  const branches = [
    { branch_id: 10, branch_name: 'Alpha' },
    { branch_id: 20, branch_name: 'Bravo' },
  ];
  const labels = affectedBranchLabels([20, 30, 10], branches);
  assert.deepEqual(labels, ['Bravo', 'Branch #30', 'Alpha']);
  assert.equal(labels.length, 3);
});

test('affectedBranchLabels: empty ids -> empty array', () => {
  assert.deepEqual(affectedBranchLabels([], []), []);
});

test('affectedBranchLabels: never drops an id even if branches list is empty', () => {
  assert.deepEqual(affectedBranchLabels([1, 2, 3], []), ['Branch #1', 'Branch #2', 'Branch #3']);
});
