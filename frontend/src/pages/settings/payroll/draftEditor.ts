/**
 * Pure Draft-editor form model for the company-owned Payroll Policies page
 * (Phase 6 U5b + Unit A).
 *
 * PURE MODULE: no React, no apiClient, `import type` only for API types.
 * Never constructs a JS Date object or reads the wall clock — ISO date
 * strings from the backend are copied/compared as strings only.
 *
 * Mirrors the backend schedule contract exactly (see
 * backend/app/payroll_setup/schemas.py DraftUpdateRequest and
 * backend/app/payroll_setup/policy.py Schedule): frequency
 * Week|Biweek|Month|Custom, an ISO anchor date, a positive integer interval
 * required only for Custom, and a 0..127 day-off mask. `validateDraftForm`
 * additionally rejects more than MAX_NORMAL_DAYS_OFF (2) days off as UI
 * guidance only — the backend (INVALID_NORMAL_DAYS_OFF) is the only real
 * enforcement point, and a mask with more than 2 bits set (e.g. one copied
 * from an old draft/schedule) is still shown as-is, never silently changed.
 * No other client-invented constraint.
 */
import { addIsoDays, inclusiveIsoDays, isValidIsoDate } from '../../../lib/isoDate.ts';
import { MAX_NORMAL_DAYS_OFF, maskToDays } from '../../../lib/payrollSetupReadiness.ts';
import type {
  DraftResponse,
  DraftUpdateRequest,
  InlineScheduleRequest,
  PayrollFrequency,
  ScheduleResponse,
  VersionResponse,
} from '../../../types/payrollSetup';

const KNOWN_FREQUENCIES: readonly PayrollFrequency[] = ['Week', 'Biweek', 'Month', 'Custom'];

function isKnownFrequency(value: string | null): value is PayrollFrequency {
  return value != null && (KNOWN_FREQUENCIES as readonly string[]).includes(value);
}

/**
 * Leaving Custom discards the explicit first-period end date, so the editor
 * asks for confirmation only when that user-entered value would be lost.
 */
export function frequencyChangeNeedsConfirmation(
  currentFrequency: DraftForm['payroll_frequency'],
  nextFrequency: DraftForm['payroll_frequency'],
  customPeriodEndDate: string,
  customIntervalDays = '',
): boolean {
  return (
    currentFrequency === 'Custom' &&
    nextFrequency !== 'Custom' &&
    (customPeriodEndDate !== '' || customIntervalDays !== '')
  );
}

/** Stable schedule-only snapshot used to detect edits since a template was applied. */
export function draftFormScheduleKey(form: DraftForm): string {
  return JSON.stringify([
    form.payroll_frequency,
    form.anchor_start_date,
    form.custom_period_end_date,
    form.custom_interval_days,
    form.normal_days_off_mask,
  ]);
}

/** Apply a selected Published terminal Version as a schedule-only template. */
export function formFromStartingPoint(
  currentForm: DraftForm,
  startingPoint: string,
  versions: readonly VersionResponse[],
): DraftForm | null {
  if (startingPoint === 'blank') {
    return {
      ...emptyDraftForm(),
      planned_effective_from_date: currentForm.planned_effective_from_date,
    };
  }
  const source = versions.find(
    (version) =>
      version.version_id === Number(startingPoint) &&
      version.lifecycle_state === 'Published' &&
      version.is_terminal,
  );
  if (!source) return null;
  return {
    ...draftFormFromSchedule(source.schedule),
    planned_effective_from_date: currentForm.planned_effective_from_date,
  };
}

export function formAfterAnchorChange(
  form: DraftForm,
  anchorStartDate: string,
  effectiveDateOwnership: 'auto' | 'manual',
): DraftForm {
  return {
    ...form,
    anchor_start_date: anchorStartDate,
    planned_effective_from_date:
      effectiveDateOwnership === 'auto'
        ? isValidIsoDate(anchorStartDate)
          ? anchorStartDate
          : ''
        : form.planned_effective_from_date,
  };
}

export function formAfterFrequencyChange(form: DraftForm, nextFrequency: DraftForm['payroll_frequency']): DraftForm {
  const leavingCustom = form.payroll_frequency === 'Custom' && nextFrequency !== 'Custom';
  return {
    ...form,
    payroll_frequency: nextFrequency,
    custom_period_end_date: leavingCustom ? '' : form.custom_period_end_date,
    custom_interval_days: leavingCustom ? '' : form.custom_interval_days,
  };
}

// ── Form shape ───────────────────────────────────────────────────────────────

export interface DraftForm {
  payroll_frequency: PayrollFrequency | '';
  anchor_start_date: string;
  custom_period_end_date: string;
  custom_interval_days: string;
  normal_days_off_mask: number;
  planned_effective_from_date: string;
}

export function emptyDraftForm(): DraftForm {
  return {
    payroll_frequency: '',
    anchor_start_date: '',
    custom_period_end_date: '',
    custom_interval_days: '',
    normal_days_off_mask: 0,
    planned_effective_from_date: '',
  };
}

/**
 * Prefills the editor from a Published Version's schedule (a client copy of
 * backend data). Copies exactly the 4 schedule fields — never returns or
 * retains a version id, so saving unmistakably creates a new Draft rather
 * than mutating the Version it was copied from.
 */
export function draftFormFromSchedule(schedule: ScheduleResponse): DraftForm {
  const interval = schedule.custom_interval_days;
  let customEnd = '';
  if (schedule.payroll_frequency === 'Custom' && interval != null && isValidIsoDate(schedule.anchor_start_date)) {
    try {
      customEnd = addIsoDays(schedule.anchor_start_date, interval - 1);
    } catch {
      customEnd = '';
    }
  }
  return {
    payroll_frequency: isKnownFrequency(schedule.payroll_frequency) ? schedule.payroll_frequency : '',
    anchor_start_date: schedule.anchor_start_date,
    custom_period_end_date: customEnd,
    custom_interval_days: interval != null ? String(interval) : '',
    normal_days_off_mask: schedule.normal_days_off_mask,
    planned_effective_from_date: '',
  };
}

export function draftFormFromDraft(draft: DraftResponse): DraftForm {
  const interval = draft.custom_interval_days;
  let customEnd = '';
  if (draft.payroll_frequency === 'Custom' && interval != null && isValidIsoDate(draft.anchor_start_date ?? '')) {
    try {
      customEnd = addIsoDays(draft.anchor_start_date!, interval - 1);
    } catch {
      customEnd = '';
    }
  }
  return {
    payroll_frequency: isKnownFrequency(draft.payroll_frequency) ? draft.payroll_frequency : '',
    anchor_start_date: draft.anchor_start_date ?? '',
    custom_period_end_date: customEnd,
    custom_interval_days: interval != null ? String(interval) : '',
    normal_days_off_mask: draft.normal_days_off_mask ?? 0,
    planned_effective_from_date: draft.planned_effective_from_date ?? '',
  };
}

// ── Validation ──────────────────────────────────────────────────────────────

function dateFieldError(label: string, value: string | undefined): string | null {
  return value != null && value !== '' && !isValidIsoDate(value) ? `${label} must be a valid date.` : null;
}

function validateSharedForm(form: DraftForm): string | null {
  if (
    !Number.isInteger(form.normal_days_off_mask) ||
    form.normal_days_off_mask < 0 ||
    form.normal_days_off_mask > 127
  ) {
    return 'Invalid days-off selection.';
  }
  if (maskToDays(form.normal_days_off_mask).length > MAX_NORMAL_DAYS_OFF) {
    return 'Choose at most two normal days off.';
  }
  const anchorError = dateFieldError('First payroll period start', form.anchor_start_date);
  if (anchorError) return anchorError;
  const endError = dateFieldError('First payroll period end', form.custom_period_end_date ?? '');
  if (endError) return endError;
  const plannedError = dateFieldError('Change start date', form.planned_effective_from_date ?? '');
  if (plannedError) return plannedError;
  if (form.anchor_start_date !== '' && (form.custom_period_end_date ?? '') !== '') {
    const interval = inclusiveIsoDays(form.anchor_start_date, form.custom_period_end_date ?? '');
    if (interval == null || interval <= 0) return 'The Custom period end must be on or after its start.';
  }
  return null;
}

/** Save-for-later validation. Missing values are intentionally allowed. */
export function validateSaveForLater(form: DraftForm): string | null {
  return validateSharedForm(form);
}

/** Strict validation used before asking the backend for publication impact. */
export function validateReviewForm(form: DraftForm): string | null {
  const sharedError = validateSharedForm(form);
  if (sharedError) return sharedError;
  if (form.payroll_frequency === '') return 'Payroll frequency is required.';
  if (form.anchor_start_date === '') return 'First payroll period start is required.';
  if (form.payroll_frequency === 'Custom') {
    if ((form.custom_period_end_date ?? '') === '') return 'First payroll period end is required for Custom frequency.';
    const interval = inclusiveIsoDays(form.anchor_start_date, form.custom_period_end_date ?? '');
    if (interval == null || interval <= 0) return 'Custom frequency requires a valid first payroll period.';
  }
  if (form.planned_effective_from_date === '') return 'This version takes effect on is required.';
  return null;
}

export function isScheduleFormComplete(form: DraftForm): boolean {
  if (validateSharedForm(form) != null) return false;
  if (form.payroll_frequency === '' || form.anchor_start_date === '') return false;
  return form.payroll_frequency !== 'Custom' || form.custom_period_end_date !== '';
}

/** Backwards-compatible name for callers that still mean strict publication validation. */
export const validateDraftForm = validateReviewForm;

function derivedInterval(form: DraftForm): number | null {
  if (form.payroll_frequency !== 'Custom') return null;
  return inclusiveIsoDays(form.anchor_start_date, form.custom_period_end_date ?? '');
}

export function toDraftPayload(form: DraftForm): DraftUpdateRequest {
  const frequency = form.payroll_frequency === '' ? null : form.payroll_frequency;
  return {
    payroll_frequency: frequency,
    anchor_start_date: form.anchor_start_date || null,
    custom_interval_days: derivedInterval(form),
    normal_days_off_mask: form.normal_days_off_mask,
    planned_effective_from_date: form.planned_effective_from_date || null,
  };
}

export function toInlineSchedulePayload(form: DraftForm): InlineScheduleRequest {
  return {
    payroll_frequency: form.payroll_frequency as PayrollFrequency,
    anchor_start_date: form.anchor_start_date,
    custom_interval_days: derivedInterval(form),
    normal_days_off_mask: form.normal_days_off_mask,
  };
}

// ── Draft completeness (UI hint only — the backend decides) ────────────────

export function isDraftComplete(draft: DraftResponse): boolean {
  return (
    draft.payroll_frequency != null &&
    draft.anchor_start_date != null &&
    draft.normal_days_off_mask != null &&
    (draft.payroll_frequency !== 'Custom' || draft.custom_interval_days != null)
  );
}

/** Deterministic key over the 4 schedule fields — used to detect preview staleness. */
export function draftScheduleKey(draft: DraftResponse): string {
  return JSON.stringify([
    draft.payroll_frequency,
    draft.anchor_start_date,
    draft.custom_interval_days,
    draft.normal_days_off_mask,
  ]);
}
