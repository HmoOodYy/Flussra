import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  draftFormFromDraft,
  draftFormFromSchedule,
  draftScheduleKey,
  draftFormScheduleKey,
  emptyDraftForm,
  frequencyChangeNeedsConfirmation,
  formAfterAnchorChange,
  formAfterFrequencyChange,
  formFromStartingPoint,
  isScheduleFormComplete,
  isDraftComplete,
  toDraftPayload,
  toInlineSchedulePayload,
  validateReviewForm,
  validateSaveForLater,
  validateDraftForm,
} from '../src/pages/settings/payroll/draftEditor.ts';
import type { DraftForm } from '../src/pages/settings/payroll/draftEditor.ts';
import { daysToMask, toggleDay } from '../src/lib/payrollSetupReadiness.ts';
import type { DraftResponse, ScheduleResponse, VersionResponse } from '../src/types/payrollSetup.ts';

function makeForm(overrides: Partial<DraftForm> = {}): DraftForm {
  return {
    payroll_frequency: 'Week',
    anchor_start_date: '2026-01-04',
    custom_period_end_date: '',
    custom_interval_days: '',
    normal_days_off_mask: 0,
    planned_effective_from_date: '2026-02-01',
    ...overrides,
  };
}

function makeSchedule(overrides: Partial<ScheduleResponse> = {}): ScheduleResponse {
  return {
    payroll_frequency: 'Week',
    anchor_start_date: '2026-01-04',
    custom_interval_days: null,
    normal_days_off_mask: 65,
    ...overrides,
  };
}

function makeDraft(overrides: Partial<DraftResponse> = {}): DraftResponse {
  return {
    setup_id: 1,
    version_id: 50,
    lifecycle_state: 'Draft',
    payroll_frequency: 'Week',
    anchor_start_date: '2026-01-04',
    custom_interval_days: null,
    normal_days_off_mask: 65,
    planned_effective_from_date: null,
    created_at_utc: '2026-01-01T00:00:00Z',
    discarded_at_utc: null,
    ...overrides,
  };
}

function makeVersion(overrides: Partial<VersionResponse> = {}, schedule: Partial<ScheduleResponse> = {}): VersionResponse {
  return {
    setup_id: 1,
    version_id: 10,
    lifecycle_state: 'Published',
    version_number: 1,
    effective_from_date: '2025-01-01',
    effective_to_date: null,
    schedule: {
      payroll_frequency: 'Week',
      anchor_start_date: '2026-10-10',
      custom_interval_days: null,
      normal_days_off_mask: 96,
      ...schedule,
    },
    config_hash: 'source-hash',
    replaces_version_id: null,
    replaced_by_version_id: null,
    is_terminal: true,
    is_current: true,
    ...overrides,
  };
}

test('frequencyChangeNeedsConfirmation only guards loss of a populated Custom end date', () => {
  assert.equal(frequencyChangeNeedsConfirmation('Week', 'Biweek', ''), false);
  assert.equal(frequencyChangeNeedsConfirmation('Biweek', 'Month', ''), false);
  assert.equal(frequencyChangeNeedsConfirmation('Custom', 'Custom', '2026-01-14'), false);
  assert.equal(frequencyChangeNeedsConfirmation('Custom', 'Week', ''), false);
  assert.equal(frequencyChangeNeedsConfirmation('Custom', 'Week', '2026-01-14'), true);
  assert.equal(frequencyChangeNeedsConfirmation('Custom', '', '2026-01-14'), true);
  assert.equal(frequencyChangeNeedsConfirmation('Custom', 'Week', '', '14'), true);
});

test('formFromStartingPoint copies a terminal Published schedule without copying source identity or effective date', () => {
  const source = makeVersion({ version_id: 41, effective_from_date: '2025-01-01' }, {
    payroll_frequency: 'Custom',
    anchor_start_date: '2026-10-10',
    custom_interval_days: 10,
    normal_days_off_mask: 96,
  });
  const current = makeForm({ planned_effective_from_date: '2026-11-01' });
  const copied = formFromStartingPoint(current, '41', [source]);

  assert.deepEqual(copied, {
    payroll_frequency: 'Custom',
    anchor_start_date: '2026-10-10',
    custom_period_end_date: '2026-10-19',
    custom_interval_days: '10',
    normal_days_off_mask: 96,
    planned_effective_from_date: '2026-11-01',
  });
  assert.equal(source.version_id, 41);
  assert.equal(source.effective_from_date, '2025-01-01');
  assert.equal(source.schedule.anchor_start_date, '2026-10-10');
});

test('formFromStartingPoint excludes non-terminal templates and Start from scratch preserves only the effective date', () => {
  const replaced = makeVersion({ version_id: 42, is_terminal: false });
  const current = makeForm({ planned_effective_from_date: '2026-11-01' });
  assert.equal(formFromStartingPoint(current, '42', [replaced]), null);
  assert.deepEqual(formFromStartingPoint(current, 'blank', [replaced]), {
    ...emptyDraftForm(),
    planned_effective_from_date: '2026-11-01',
  });
});

test('schedule baseline distinguishes untouched template data from meaningful edits', () => {
  const form = makeForm();
  assert.equal(draftFormScheduleKey(form), draftFormScheduleKey({ ...form }));
  assert.notEqual(draftFormScheduleKey(form), draftFormScheduleKey({ ...form, anchor_start_date: '2026-04-01' }));
  assert.notEqual(draftFormScheduleKey(form), draftFormScheduleKey({ ...form, normal_days_off_mask: 3 }));
});

test('frequency confirmation preserves compatible fields and clears only Custom data after confirmation', () => {
  const custom = makeForm({
    payroll_frequency: 'Custom',
    custom_period_end_date: '2026-01-14',
    custom_interval_days: '11',
    anchor_start_date: '2026-01-04',
    normal_days_off_mask: 65,
    planned_effective_from_date: '2026-02-01',
  });
  assert.deepEqual(formAfterFrequencyChange(custom, 'Week'), {
    ...custom,
    payroll_frequency: 'Week',
    custom_period_end_date: '',
    custom_interval_days: '',
  });
  assert.equal(formAfterFrequencyChange(custom, 'Week').anchor_start_date, custom.anchor_start_date);
  assert.equal(formAfterFrequencyChange(custom, 'Week').normal_days_off_mask, custom.normal_days_off_mask);
  assert.equal(formAfterFrequencyChange(custom, 'Week').planned_effective_from_date, custom.planned_effective_from_date);
});

test('auto-seeded effective date follows a valid anchor only while it remains auto-managed', () => {
  const form = makeForm({ planned_effective_from_date: '' });
  assert.equal(formAfterAnchorChange(form, '2026-10-10', 'auto').planned_effective_from_date, '2026-10-10');
  assert.equal(formAfterAnchorChange(form, '2026-10', 'auto').planned_effective_from_date, '');
  assert.equal(
    formAfterAnchorChange({ ...form, planned_effective_from_date: '2026-11-01' }, '2026-10-10', 'manual').planned_effective_from_date,
    '2026-11-01',
  );
});

// ── emptyDraftForm ───────────────────────────────────────────────────────────

test('emptyDraftForm: blank strings and zero mask', () => {
  assert.deepEqual(emptyDraftForm(), {
    payroll_frequency: '',
    anchor_start_date: '',
    custom_period_end_date: '',
    custom_interval_days: '',
    normal_days_off_mask: 0,
    planned_effective_from_date: '',
  });
});

// ── A. Week payload ──────────────────────────────────────────────────────────

test('A. Week payload: interval null, mask kept', () => {
  const form = makeForm({ payroll_frequency: 'Week', normal_days_off_mask: 65 });
  assert.equal(validateReviewForm(form), null);
  assert.deepEqual(toDraftPayload(form), {
    payroll_frequency: 'Week',
    anchor_start_date: '2026-01-04',
    custom_interval_days: null,
    normal_days_off_mask: 65,
    planned_effective_from_date: '2026-02-01',
  });
});

// ── B. Custom uses a derived inclusive end date ─────────────────────────────

test('B. Custom requires a first period end during Review', () => {
  const form = makeForm({ payroll_frequency: 'Custom', custom_period_end_date: '' });
  assert.equal(validateReviewForm(form), 'First payroll period end is required for Custom frequency.');
});

test('B. Custom derives an inclusive interval from start and end', () => {
  const form = makeForm({
    payroll_frequency: 'Custom',
    anchor_start_date: '2026-01-04',
    custom_period_end_date: '2026-01-17',
  });
  assert.equal(validateReviewForm(form), null);
  assert.equal(toDraftPayload(form).custom_interval_days, 14);
  assert.equal(toInlineSchedulePayload(form).custom_interval_days, 14);
  assert.equal(isScheduleFormComplete(form), true);
});

test('B. Custom Save for later permits an incomplete end date', () => {
  const form = makeForm({ payroll_frequency: 'Custom', custom_period_end_date: '', custom_interval_days: '14' });
  assert.equal(validateSaveForLater(form), null);
  assert.equal(toDraftPayload(form).custom_interval_days, null);
  assert.equal(isScheduleFormComplete(form), false);
});

// ── C. Non-Custom always sends interval null ────────────────────────────────

test('C. non-Custom frequencies always send custom_interval_days: null, even with a leftover form string', () => {
  for (const frequency of ['Week', 'Biweek', 'Month'] as const) {
    const form = makeForm({ payroll_frequency: frequency, custom_interval_days: '30' });
    assert.equal(validateReviewForm(form), null);
    assert.equal(toDraftPayload(form).custom_interval_days, null);
  }
});

// ── D. Unit A: at most 2 days off is now guidance-enforced by validateDraftForm ──
//
// daysToMask/toggleDay themselves still build/toggle masks with any number
// of days (a prefilled source with >2 days, which should not exist, is
// still represented as-is, never silently changed) — only
// validateDraftForm's own guidance check now rejects saving with more than
// MAX_NORMAL_DAYS_OFF (2) days off, mirroring the backend's
// INVALID_NORMAL_DAYS_OFF, which remains the real enforcement point.

test('D. daysToMask/toggleDay still build/toggle a mask with more than two days (no mask-construction limit)', () => {
  const mask = daysToMask([0, 1, 2, 3, 6]);
  let toggled = 0;
  for (const day of [0, 1, 2, 3, 6]) {
    toggled = toggleDay(toggled, day);
  }
  assert.equal(mask, toggled);
  // validateDraftForm now flags this as guidance (more than 2 days off) —
  // the mask itself is unchanged, just rejected for saving.
  const form = makeForm({ normal_days_off_mask: mask });
  assert.equal(validateDraftForm(form), 'Choose at most two normal days off.');
});

test('D. validateDraftForm: 0/1/2 days off pass; 3+ days off are guidance-rejected; 128/-1 stay structurally invalid', () => {
  assert.equal(validateDraftForm(makeForm({ normal_days_off_mask: 0 })), null);
  assert.equal(validateDraftForm(makeForm({ normal_days_off_mask: daysToMask([0]) })), null);
  assert.equal(validateDraftForm(makeForm({ normal_days_off_mask: daysToMask([0, 6]) })), null);
  assert.equal(
    validateDraftForm(makeForm({ normal_days_off_mask: daysToMask([0, 1, 6]) })),
    'Choose at most two normal days off.',
  );
  assert.equal(
    validateDraftForm(makeForm({ normal_days_off_mask: 127 })),
    'Choose at most two normal days off.',
  );
  assert.equal(validateDraftForm(makeForm({ normal_days_off_mask: 128 })), 'Invalid days-off selection.');
  assert.equal(validateDraftForm(makeForm({ normal_days_off_mask: -1 })), 'Invalid days-off selection.');
});

// ── Other validateDraftForm rules ───────────────────────────────────────────

test('validateDraftForm: frequency required', () => {
  assert.equal(
    validateDraftForm(makeForm({ payroll_frequency: '' })),
    'Payroll frequency is required.',
  );
});

test('validateDraftForm: anchor date required', () => {
  assert.equal(
    validateDraftForm(makeForm({ anchor_start_date: '' })),
    'First payroll period start is required.',
  );
});

test('validateDraftForm: anchor date must match YYYY-MM-DD', () => {
  assert.equal(
    validateDraftForm(makeForm({ anchor_start_date: '01/04/2026' })),
    'First payroll period start must be a valid date.',
  );
  assert.equal(
    validateDraftForm(makeForm({ anchor_start_date: '2026-1-4' })),
    'First payroll period start must be a valid date.',
  );
});

// ── E. draftFormFromSchedule / draftFormFromDraft / isDraftComplete / draftScheduleKey ──

test('E. draftFormFromSchedule copies schedule fields and reconstructs Custom end date', () => {
  const schedule = makeSchedule({
    payroll_frequency: 'Custom',
    anchor_start_date: '2026-02-01',
    custom_interval_days: 10,
    normal_days_off_mask: 5,
  });
  const form = draftFormFromSchedule(schedule);
  assert.deepEqual(form, {
    payroll_frequency: 'Custom',
    anchor_start_date: '2026-02-01',
    custom_period_end_date: '2026-02-10',
    custom_interval_days: '10',
    normal_days_off_mask: 5,
    planned_effective_from_date: '',
  });
});

test('E. draftFormFromSchedule: null interval -> empty string; unknown frequency -> blank', () => {
  const schedule = makeSchedule({
    payroll_frequency: 'SomeUnknownFrequency',
    custom_interval_days: null,
  });
  const form = draftFormFromSchedule(schedule);
  assert.equal(form.payroll_frequency, '');
  assert.equal(form.custom_interval_days, '');
});

test('E. draftFormFromDraft: null fields map to blank/zero', () => {
  const draft = makeDraft({
    payroll_frequency: null,
    anchor_start_date: null,
    custom_interval_days: null,
    normal_days_off_mask: null,
  });
  assert.deepEqual(draftFormFromDraft(draft), {
    payroll_frequency: '',
    anchor_start_date: '',
    custom_period_end_date: '',
    custom_interval_days: '',
    normal_days_off_mask: 0,
    planned_effective_from_date: '',
  });
});

test('E. draftFormFromDraft: populated fields carry through, including planned date', () => {
  const draft = makeDraft({
    payroll_frequency: 'Custom',
    anchor_start_date: '2026-03-01',
    custom_interval_days: 21,
    normal_days_off_mask: 9,
    planned_effective_from_date: '2026-04-01',
  });
  assert.deepEqual(draftFormFromDraft(draft), {
    payroll_frequency: 'Custom',
    anchor_start_date: '2026-03-01',
    custom_period_end_date: '2026-03-21',
    custom_interval_days: '21',
    normal_days_off_mask: 9,
    planned_effective_from_date: '2026-04-01',
  });
});

test('E. draftFormFromDraft: unknown frequency string maps to blank', () => {
  const draft = makeDraft({ payroll_frequency: 'NotARealFrequency' });
  assert.equal(draftFormFromDraft(draft).payroll_frequency, '');
});

test('isDraftComplete: complete non-Custom draft is complete', () => {
  assert.equal(isDraftComplete(makeDraft()), true);
});

test('isDraftComplete: Custom draft requires custom_interval_days non-null', () => {
  assert.equal(
    isDraftComplete(makeDraft({ payroll_frequency: 'Custom', custom_interval_days: null })),
    false,
  );
  assert.equal(
    isDraftComplete(makeDraft({ payroll_frequency: 'Custom', custom_interval_days: 7 })),
    true,
  );
});

test('isDraftComplete: any null schedule field makes it incomplete', () => {
  assert.equal(isDraftComplete(makeDraft({ payroll_frequency: null })), false);
  assert.equal(isDraftComplete(makeDraft({ anchor_start_date: null })), false);
  assert.equal(isDraftComplete(makeDraft({ normal_days_off_mask: null })), false);
});

test('draftScheduleKey: changes when any of the 4 schedule fields changes', () => {
  const base = makeDraft();
  const baseKey = draftScheduleKey(base);
  assert.equal(draftScheduleKey(makeDraft()), baseKey);
  assert.notEqual(draftScheduleKey({ ...base, payroll_frequency: 'Biweek' }), baseKey);
  assert.notEqual(draftScheduleKey({ ...base, anchor_start_date: '2026-02-01' }), baseKey);
  assert.notEqual(
    draftScheduleKey({ ...base, payroll_frequency: 'Custom', custom_interval_days: 10 }),
    baseKey,
  );
  assert.notEqual(draftScheduleKey({ ...base, normal_days_off_mask: 1 }), baseKey);
});
