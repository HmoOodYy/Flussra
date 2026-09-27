import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  ISO_DATE_PATTERN,
  annotateHistory,
  assignmentHistoryLine,
  boundaryKindLabel,
  branchDisplay,
  canRequestEffective,
  effectiveRequestDate,
  fullDaysOffLine,
  intervalLabel,
  isAfterEvaluationDate,
  notReadyExplanation,
  parseBranchIdParam,
  periodsStartLine,
  readinessView,
  resolveBranchSelection,
  scheduleFrequencyNoun,
  setupDisplay,
  upcomingChange,
  upcomingChangeText,
  versionHistoryLine,
} from '../src/pages/payroll/schedule/branchScheduleView.ts';
import type {
  AssignmentHistoryResponse,
  BranchHistoryResponse,
  EffectiveAuthorityResponse,
  ScheduleResponse,
  VersionSegmentResponse,
} from '../src/types/payrollSetup.ts';

// ── Fixtures ─────────────────────────────────────────────────────────────────

function makeSegment(overrides: Partial<VersionSegmentResponse> = {}): VersionSegmentResponse {
  return {
    version_id: 100,
    version_number: 1,
    effective_from_date: '2026-01-05',
    effective_to_date: null,
    schedule: {
      payroll_frequency: 'Week',
      anchor_start_date: '2026-01-05',
      custom_interval_days: null,
      normal_days_off_mask: 65,
    },
    config_hash: 'hash-100',
    ...overrides,
  };
}

function makeAssignment(overrides: Partial<AssignmentHistoryResponse> = {}): AssignmentHistoryResponse {
  return {
    assignment_id: 1,
    branch_id: 7,
    setup_id: 1,
    setup_code: 'SETUP1',
    setup_name: 'Setup One',
    effective_from_date: '2026-01-05',
    effective_to_date: null,
    reason: null,
    created_at_utc: '2026-01-01T00:00:00Z',
    withdrawn_at_utc: null,
    withdrawal_reason: null,
    versions: [],
    ...overrides,
  };
}

// A1: 2026-01-05 -> 2026-03-02, not withdrawn, two version segments.
const A1 = makeAssignment({
  assignment_id: 1,
  effective_from_date: '2026-01-05',
  effective_to_date: '2026-03-02',
  versions: [
    makeSegment({ version_id: 101, version_number: 1, effective_from_date: '2026-01-05', effective_to_date: '2026-02-01' }),
    makeSegment({ version_id: 102, version_number: 2, effective_from_date: '2026-02-01', effective_to_date: '2026-03-02' }),
  ],
});

// A2: 2026-03-02 -> null, not withdrawn (after anchor), one version segment.
const A2 = makeAssignment({
  assignment_id: 2,
  effective_from_date: '2026-03-02',
  effective_to_date: null,
  versions: [
    makeSegment({ version_id: 201, version_number: 1, effective_from_date: '2026-03-02', effective_to_date: null }),
  ],
});

// A3: 2026-04-06 -> null, WITHDRAWN, one version segment.
const A3 = makeAssignment({
  assignment_id: 3,
  effective_from_date: '2026-04-06',
  effective_to_date: null,
  withdrawn_at_utc: '2026-04-10T12:00:00Z',
  withdrawal_reason: 'Reassigned to another Setup',
  versions: [
    makeSegment({ version_id: 301, version_number: 1, effective_from_date: '2026-04-06', effective_to_date: null }),
  ],
});

const ANCHOR = '2026-02-02';

function makeHistory(assignments: AssignmentHistoryResponse[] = [A1, A2, A3], branchId = 7): BranchHistoryResponse {
  return { branch_id: branchId, assignments };
}

function makeEffective(overrides: Partial<EffectiveAuthorityResponse> = {}): EffectiveAuthorityResponse {
  return {
    company_id: 1,
    branch_id: 7,
    assignment_id: 1,
    setup_id: 1,
    setup_code: 'SETUP1',
    setup_name: 'Setup One',
    version_id: 101,
    version_number: 1,
    schedule: {
      payroll_frequency: 'Week',
      anchor_start_date: '2026-01-05',
      custom_interval_days: null,
      normal_days_off_mask: 65,
    },
    config_hash: 'hash-101',
    period_start_date: '2026-01-05',
    period_end_date: '2026-01-11',
    next_boundary_date: null,
    next_boundary_kind: null,
    ...overrides,
  };
}

// ── parseBranchIdParam ────────────────────────────────────────────────────────

test('parseBranchIdParam: absent for null and empty string', () => {
  assert.deepEqual(parseBranchIdParam(null), { kind: 'absent' });
  assert.deepEqual(parseBranchIdParam(''), { kind: 'absent' });
});

test('parseBranchIdParam: valid ids', () => {
  assert.deepEqual(parseBranchIdParam('1'), { kind: 'id', id: 1 });
  assert.deepEqual(parseBranchIdParam('42'), { kind: 'id', id: 42 });
});

test('parseBranchIdParam: invalid strings', () => {
  for (const raw of ['0', '-1', '1.5', 'abc', ' 7', '07', '1e3', '9007199254740993']) {
    assert.deepEqual(parseBranchIdParam(raw), { kind: 'invalid', raw }, raw);
  }
});

// ── resolveBranchSelection ────────────────────────────────────────────────────

test('resolveBranchSelection: none when absent and no viewable branches', () => {
  assert.deepEqual(resolveBranchSelection({ kind: 'absent' }, []), { kind: 'none' });
});

test('resolveBranchSelection: redirect to first viewable branch when absent', () => {
  assert.deepEqual(resolveBranchSelection({ kind: 'absent' }, [5, 6, 7]), { kind: 'redirect', branchId: 5 });
});

test('resolveBranchSelection: invalid passes through raw', () => {
  assert.deepEqual(resolveBranchSelection({ kind: 'invalid', raw: 'abc' }, [5, 6]), { kind: 'invalid', raw: 'abc' });
});

test('resolveBranchSelection: selected when id is viewable', () => {
  assert.deepEqual(resolveBranchSelection({ kind: 'id', id: 6 }, [5, 6, 7]), { kind: 'selected', branchId: 6 });
});

test('resolveBranchSelection: unavailable when requested id is not viewable — never substitutes', () => {
  assert.deepEqual(resolveBranchSelection({ kind: 'id', id: 9 }, [3, 4]), { kind: 'unavailable', branchId: 9 });
});

// ── canRequestEffective ───────────────────────────────────────────────────────

test('canRequestEffective: truth table', () => {
  assert.equal(canRequestEffective('READY', '2026-02-02'), true);
  assert.equal(canRequestEffective('READY', null), false);
  assert.equal(canRequestEffective('READY', undefined), false);
  assert.equal(canRequestEffective('READY', '2026-2-2'), false);
  // I: non-READY code with a valid date is still false.
  assert.equal(canRequestEffective('NO_ASSIGNMENT', '2026-02-02'), false);
  assert.equal(canRequestEffective('AUTHORITY_BOUNDARY_CONFLICT', '2026-02-02'), false);
  assert.equal(canRequestEffective(null, '2026-02-02'), false);
  assert.equal(canRequestEffective('ready', '2026-02-02'), false);
  assert.equal(canRequestEffective('SOMETHING_NEW', '2026-02-02'), false);
});

// ── effectiveRequestDate ──────────────────────────────────────────────────────

test('effectiveRequestDate: returns the exact same date string for READY (J)', () => {
  const date = '2026-02-02';
  const branch = { schedule_readiness_reason: 'READY', schedule_readiness_date: date };
  const result = effectiveRequestDate(branch);
  assert.equal(result, date);
  assert.ok(result === date);
});

test('effectiveRequestDate: null for non-READY with a date', () => {
  const branch = { schedule_readiness_reason: 'NO_ASSIGNMENT', schedule_readiness_date: '2026-02-02' };
  assert.equal(effectiveRequestDate(branch), null);
});

// ── readinessView ─────────────────────────────────────────────────────────────

test('readinessView: null reason -> unavailable, never ready', () => {
  const view = readinessView(null);
  assert.deepEqual(view, { kind: 'unavailable' });
});

test('readinessView: READY -> ready', () => {
  const view = readinessView('READY');
  assert.equal(view.kind, 'ready');
});

test('readinessView: each of the 7 known non-READY codes -> not-ready, known, non-empty explanation', () => {
  const knownNonReadyCodes = [
    'NO_COMPANY_DEFAULT',
    'NO_ASSIGNMENT',
    'NO_PUBLISHED_VERSION',
    'AUTHORITY_BOUNDARY_CONFLICT',
    'SETUP_NOT_ACTIVE',
    'INVALID_SCHEDULE_BOUNDARY',
    'BRANCH_NOT_OPERATIONAL',
  ];
  assert.equal(knownNonReadyCodes.length, 7);
  for (const code of knownNonReadyCodes) {
    const view = readinessView(code);
    assert.equal(view.kind, 'not-ready', code);
    if (view.kind === 'not-ready') {
      assert.equal(view.known, true, code);
      assert.ok(view.explanation.length > 0, code);
    }
  }
});

test('readinessView: the three spec explanations verbatim', () => {
  assert.equal(
    notReadyExplanation('NO_ASSIGNMENT'),
    'No Payroll Setup assignment applies at the evaluated date.',
  );
  assert.equal(
    notReadyExplanation('NO_PUBLISHED_VERSION'),
    'The assigned Payroll Setup has no Published Version covering the evaluated date.',
  );
  assert.equal(
    notReadyExplanation('AUTHORITY_BOUNDARY_CONFLICT'),
    'The persisted schedule authority has a boundary conflict.',
  );
});

test('readinessView: unknown code FUTURE_CODE -> not-ready, known false, label is the raw code, explanation names it (T)', () => {
  const view = readinessView('FUTURE_CODE');
  assert.equal(view.kind, 'not-ready');
  if (view.kind === 'not-ready') {
    assert.equal(view.known, false);
    assert.equal(view.label, 'FUTURE_CODE');
    assert.ok(view.explanation.includes('FUTURE_CODE'));
  }
});

test('readinessView: prototype-pollution-shaped codes are treated as unknown', () => {
  for (const code of ['constructor', '__proto__']) {
    const view = readinessView(code);
    assert.equal(view.kind, 'not-ready', code);
    if (view.kind === 'not-ready') {
      assert.equal(view.known, false, code);
    }
  }
});

// ── isAfterEvaluationDate ─────────────────────────────────────────────────────

test('isAfterEvaluationDate: after/equal/before/null-anchor/malformed (U)', () => {
  assert.equal(isAfterEvaluationDate('2026-03-01', '2026-02-01'), true);
  assert.equal(isAfterEvaluationDate('2026-02-01', '2026-02-01'), false);
  assert.equal(isAfterEvaluationDate('2026-01-01', '2026-02-01'), false);
  assert.equal(isAfterEvaluationDate('2026-03-01', null), false);
  assert.equal(isAfterEvaluationDate('not-a-date', '2026-02-01'), false);
  assert.equal(isAfterEvaluationDate('2026-03-01', 'not-a-date'), false);
});

// ── annotateHistory ────────────────────────────────────────────────────────────

test('annotateHistory: governing decided by assignment_id match, not by date bracketing', () => {
  const history = makeHistory();
  const effective = makeEffective({ assignment_id: 1, version_id: 101 });
  const rows = annotateHistory(history, effective, ANCHOR);
  const rowA1 = rows.find((r) => r.assignment.assignment_id === 1);
  const rowA2 = rows.find((r) => r.assignment.assignment_id === 2);
  assert.equal(rowA1?.governing, true);
  assert.equal(rowA2?.governing, false);
});

test('annotateHistory: governing follows the id even when it points at a different assignment (A2)', () => {
  const history = makeHistory();
  const effective = makeEffective({ assignment_id: 2, version_id: 201 });
  const rows = annotateHistory(history, effective, ANCHOR);
  const rowA1 = rows.find((r) => r.assignment.assignment_id === 1);
  const rowA2 = rows.find((r) => r.assignment.assignment_id === 2);
  assert.equal(rowA2?.governing, true);
  assert.equal(rowA1?.governing, false);
});

test('annotateHistory: effective.branch_id mismatch -> no governing anywhere', () => {
  const history = makeHistory();
  const effective = makeEffective({ assignment_id: 1, version_id: 101, branch_id: 999 });
  const rows = annotateHistory(history, effective, ANCHOR);
  assert.ok(rows.every((r) => r.governing === false));
  assert.ok(rows.every((r) => r.versions.every((v) => v.governing === false)));
});

test('annotateHistory: version governing only for the matching version_id inside the governing assignment', () => {
  const history = makeHistory();
  const effective = makeEffective({ assignment_id: 1, version_id: 102 });
  const rows = annotateHistory(history, effective, ANCHOR);
  const rowA1 = rows.find((r) => r.assignment.assignment_id === 1)!;
  assert.equal(rowA1.governing, true);
  const v101 = rowA1.versions.find((v) => v.segment.version_id === 101);
  const v102 = rowA1.versions.find((v) => v.segment.version_id === 102);
  assert.equal(v101?.governing, false);
  assert.equal(v102?.governing, true);
});

test('annotateHistory: scheduled — A2 true (after anchor), A1 false, A3 false because withdrawn (P)', () => {
  const history = makeHistory();
  const rows = annotateHistory(history, null, ANCHOR);
  const rowA1 = rows.find((r) => r.assignment.assignment_id === 1);
  const rowA2 = rows.find((r) => r.assignment.assignment_id === 2);
  const rowA3 = rows.find((r) => r.assignment.assignment_id === 3);
  assert.equal(rowA1?.scheduled, false);
  assert.equal(rowA2?.scheduled, true);
  assert.equal(rowA3?.scheduled, false);
  // A3 is still present with withdrawn true and its withdrawal fields intact.
  assert.equal(rowA3?.withdrawn, true);
  assert.equal(rowA3?.assignment.withdrawn_at_utc, '2026-04-10T12:00:00Z');
  assert.equal(rowA3?.assignment.withdrawal_reason, 'Reassigned to another Setup');
});

test('annotateHistory: version segments preserved in order and by reference (Q)', () => {
  const history = makeHistory();
  const rows = annotateHistory(history, null, ANCHOR);
  const rowA1 = rows.find((r) => r.assignment.assignment_id === 1)!;
  assert.equal(rowA1.versions.length, A1.versions.length);
  rowA1.versions.forEach((v, i) => {
    assert.equal(v.segment, A1.versions[i]);
  });
  assert.equal(rowA1.assignment, A1);
});

test('annotateHistory: null anchor -> no scheduled tags anywhere', () => {
  const history = makeHistory();
  const effective = makeEffective({ assignment_id: 1, version_id: 101 });
  const rows = annotateHistory(history, effective, null);
  assert.ok(rows.every((r) => r.scheduled === false));
  assert.ok(rows.every((r) => r.versions.every((v) => v.scheduled === false)));
});

test('annotateHistory: null effective -> no governing tags anywhere', () => {
  const history = makeHistory();
  const rows = annotateHistory(history, null, ANCHOR);
  assert.ok(rows.every((r) => r.governing === false));
  assert.ok(rows.every((r) => r.versions.every((v) => v.governing === false)));
});

test('annotateHistory: output length/order equals input, and never adds/drops/reorders rows', () => {
  const history = makeHistory();
  const rows = annotateHistory(history, null, ANCHOR);
  assert.equal(rows.length, history.assignments.length);
  rows.forEach((r, i) => {
    assert.equal(r.assignment, history.assignments[i]);
  });
});

test('annotateHistory: never mutates inputs (deep-equal against a pre-call snapshot)', () => {
  const history = makeHistory();
  const effective = makeEffective({ assignment_id: 1, version_id: 101 });
  const historySnapshot = structuredClone(history);
  const effectiveSnapshot = structuredClone(effective);
  annotateHistory(history, effective, ANCHOR);
  assert.deepEqual(history, historySnapshot);
  assert.deepEqual(effective, effectiveSnapshot);
});

test('annotateHistory: works on frozen inputs (proves no mutation, even deeply)', () => {
  const frozenSegment = Object.freeze(makeSegment({ version_id: 101 }));
  const frozenAssignment = Object.freeze(
    makeAssignment({ assignment_id: 1, versions: Object.freeze([frozenSegment]) as unknown as VersionSegmentResponse[] }),
  );
  const frozenHistory = Object.freeze({
    branch_id: 7,
    assignments: Object.freeze([frozenAssignment]) as unknown as AssignmentHistoryResponse[],
  });
  const effective = Object.freeze(makeEffective({ assignment_id: 1, version_id: 101 }));
  assert.doesNotThrow(() => annotateHistory(frozenHistory, effective, ANCHOR));
});

// ── intervalLabel / boundaryKindLabel / setupDisplay / branchDisplay ─────────

test('intervalLabel: open-ended and closed intervals', () => {
  assert.equal(intervalLabel('2026-01-05', '2026-03-02'), '2026-01-05 → 2026-03-02');
  assert.equal(intervalLabel('2026-01-05', null), '2026-01-05 → open-ended');
});

test('boundaryKindLabel: all three known kinds, null, unknown passthrough, prototype-shaped passthrough', () => {
  assert.equal(boundaryKindLabel('Assignment'), 'Assignment change');
  assert.equal(boundaryKindLabel('Version'), 'Version change');
  assert.equal(boundaryKindLabel('AssignmentAndVersion'), 'Assignment and Version change');
  assert.equal(boundaryKindLabel(null), 'None reported');
  assert.equal(boundaryKindLabel('SomethingElse'), 'SomethingElse');
  assert.equal(boundaryKindLabel('constructor'), 'constructor');
});

test('setupDisplay: code — name', () => {
  assert.equal(setupDisplay('SETUP1', 'Setup One'), 'SETUP1 — Setup One');
});

test('branchDisplay: name (code)', () => {
  assert.equal(branchDisplay({ branch_name: 'Downtown', branch_code: 'DT01' }), 'Downtown (DT01)');
});

// ── periodsStartLine / fullDaysOffLine / scheduleFrequencyNoun (Unit B) ─────

function makeSchedule(overrides: Partial<ScheduleResponse> = {}): ScheduleResponse {
  return {
    payroll_frequency: 'Week',
    anchor_start_date: '2026-09-23',
    custom_interval_days: null,
    normal_days_off_mask: 65, // Sun, Sat
    ...overrides,
  };
}

test('periodsStartLine: Weekly/Biweekly names the weekday of period_start_date', () => {
  // 2026-09-23 is a Wednesday.
  assert.equal(periodsStartLine(makeSchedule({ payroll_frequency: 'Week' }), '2026-09-23'), 'Periods start Wednesday');
  assert.equal(
    periodsStartLine(makeSchedule({ payroll_frequency: 'Biweek' }), '2026-09-23'),
    'Periods start Wednesday',
  );
});

test('periodsStartLine: Monthly names the day-of-month via string slicing, not calendar math', () => {
  assert.equal(
    periodsStartLine(makeSchedule({ payroll_frequency: 'Month' }), '2026-09-04'),
    'Periods start on day 4 of the month',
  );
  assert.equal(
    periodsStartLine(makeSchedule({ payroll_frequency: 'Month' }), '2026-09-23'),
    'Periods start on day 23 of the month',
  );
});

test('periodsStartLine: Custom shows "Every N days"', () => {
  assert.equal(
    periodsStartLine(makeSchedule({ payroll_frequency: 'Custom', custom_interval_days: 10 }), '2026-09-23'),
    'Every 10 days',
  );
});

test('periodsStartLine: an invalid period_start_date yields an empty string rather than throwing', () => {
  assert.equal(periodsStartLine(makeSchedule({ payroll_frequency: 'Week' }), 'not-a-date'), '');
  assert.equal(periodsStartLine(makeSchedule({ payroll_frequency: 'Month' }), 'not-a-date'), '');
});

test('fullDaysOffLine: full weekday names, or "No normal days off"', () => {
  assert.equal(fullDaysOffLine(65), 'Days off: Sunday, Saturday');
  assert.equal(fullDaysOffLine(0), 'No normal days off');
});

test('scheduleFrequencyNoun: Weekly/Biweekly/Monthly/Custom', () => {
  assert.equal(scheduleFrequencyNoun(makeSchedule({ payroll_frequency: 'Week' })), 'Weekly');
  assert.equal(scheduleFrequencyNoun(makeSchedule({ payroll_frequency: 'Biweek' })), 'Biweekly');
  assert.equal(scheduleFrequencyNoun(makeSchedule({ payroll_frequency: 'Month' })), 'Monthly');
  assert.equal(
    scheduleFrequencyNoun(makeSchedule({ payroll_frequency: 'Custom', custom_interval_days: 14 })),
    '14-day',
  );
});

// ── upcomingChange / upcomingChangeText (Unit B) ────────────────────────────

test('upcomingChange: no next_boundary_date -> none', () => {
  assert.deepEqual(upcomingChange({ next_boundary_date: null }, null), { kind: 'none' });
  assert.equal(upcomingChangeText({ kind: 'none' }), 'No scheduled policy change');
});

test('upcomingChange: a matching non-withdrawn assignment -> assignment kind', () => {
  const history = makeHistory([
    makeAssignment({ assignment_id: 9, setup_name: 'New Policy', effective_from_date: '2026-05-01', withdrawn_at_utc: null }),
  ]);
  const view = upcomingChange({ next_boundary_date: '2026-05-01' }, history);
  assert.deepEqual(view, { kind: 'assignment', setupName: 'New Policy', date: '2026-05-01' });
  assert.equal(upcomingChangeText(view), 'Changes to New Policy on May 1, 2026');
});

test('upcomingChange: a WITHDRAWN assignment at that date does not count -> falls through to version/unknown', () => {
  const history = makeHistory([
    makeAssignment({
      assignment_id: 9,
      setup_name: 'Withdrawn Policy',
      effective_from_date: '2026-05-01',
      withdrawn_at_utc: '2026-05-02T00:00:00Z',
    }),
  ]);
  const view = upcomingChange({ next_boundary_date: '2026-05-01' }, history);
  assert.notEqual(view.kind, 'assignment');
});

test('upcomingChange: a matching version segment (no matching assignment) -> version kind', () => {
  const history = makeHistory([
    makeAssignment({
      assignment_id: 9,
      effective_from_date: '2026-01-01',
      versions: [makeSegment({ version_id: 55, effective_from_date: '2026-05-01' })],
    }),
  ]);
  const view = upcomingChange({ next_boundary_date: '2026-05-01' }, history);
  assert.deepEqual(view, { kind: 'version', date: '2026-05-01' });
  assert.equal(upcomingChangeText(view), 'Policy update on May 1, 2026');
});

test('upcomingChange: neither an assignment nor a version segment matches -> unknown kind', () => {
  const history = makeHistory([]);
  const view = upcomingChange({ next_boundary_date: '2026-05-01' }, history);
  assert.deepEqual(view, { kind: 'unknown', date: '2026-05-01' });
  assert.equal(upcomingChangeText(view), 'Scheduled change on May 1, 2026');
});

test('upcomingChange: null history and a next boundary date -> unknown kind (never throws)', () => {
  const view = upcomingChange({ next_boundary_date: '2026-05-01' }, null);
  assert.deepEqual(view, { kind: 'unknown', date: '2026-05-01' });
});

// ── assignmentHistoryLine / versionHistoryLine (Unit B) ─────────────────────

test('assignmentHistoryLine: open-ended -> "<name> — <Mon D, YYYY> onward"', () => {
  const line = assignmentHistoryLine({
    assignment: makeAssignment({ setup_name: 'Standard Weekly', effective_from_date: '2026-01-05', effective_to_date: null }),
    withdrawn: false,
  });
  assert.equal(line, 'Standard Weekly — Jan 5, 2026 onward');
});

test('assignmentHistoryLine: bounded interval -> "<name> — <from> – <to>"', () => {
  const line = assignmentHistoryLine({
    assignment: makeAssignment({ setup_name: 'Standard Weekly', effective_from_date: '2026-01-05', effective_to_date: '2026-03-02' }),
    withdrawn: false,
  });
  assert.equal(line, 'Standard Weekly — Jan 5, 2026 – Mar 2, 2026');
});

test('assignmentHistoryLine: withdrawn suffixes " (cancelled)"', () => {
  const line = assignmentHistoryLine({
    assignment: makeAssignment({ setup_name: 'Old Policy', effective_from_date: '2026-01-05', effective_to_date: null }),
    withdrawn: true,
  });
  assert.equal(line, 'Old Policy — Jan 5, 2026 onward (cancelled)');
});

test('versionHistoryLine: "Version N from <Mon D, YYYY>" — never the raw version_id', () => {
  const line = versionHistoryLine(makeSegment({ version_number: 3, effective_from_date: '2026-02-01' }));
  assert.equal(line, 'Version 3 from Feb 1, 2026');
  assert.doesNotMatch(line, /#/);
});

// ── Source guard (K) ──────────────────────────────────────────────────────────

test('source guard: branchScheduleView.ts stays pure — no Date/apiClient/axios/React', () => {
  const source = readFileSync(
    new URL('../src/pages/payroll/schedule/branchScheduleView.ts', import.meta.url),
    'utf8',
  );
  for (const forbidden of [
    'new Date',
    'Date.now',
    'Date.parse',
    'toISOString',
    'Intl.',
    'toLocale',
    'apiClient',
    'axios',
    "from 'react'",
  ]) {
    assert.ok(!source.includes(forbidden), `must not contain: ${forbidden}`);
  }
});

// Sanity: ISO_DATE_PATTERN is exported and behaves as documented.
test('ISO_DATE_PATTERN: matches YYYY-MM-DD only', () => {
  assert.equal(ISO_DATE_PATTERN.test('2026-02-02'), true);
  assert.equal(ISO_DATE_PATTERN.test('2026-2-2'), false);
  assert.equal(ISO_DATE_PATTERN.test(''), false);
});
