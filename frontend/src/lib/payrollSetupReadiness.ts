/**
 * Pure formatting/presentation helpers for canonical Payroll Setup readiness,
 * schedules, and weekday masks.
 *
 * PURE MODULE: no imports of React, apiClient, or axios, and no browser
 * globals. Never uses JS `Date` for weekday math — NormalDaysOffMask bit
 * positions follow the backend convention exactly (see
 * backend/app/payroll/period_creation.py): bit 0 = Sunday … bit 6 = Saturday,
 * where day_of_week = (python_weekday + 1) % 7. `friendlyScheduleSummary`
 * below names the anchor date's weekday using isoDate's pure calendar
 * arithmetic (weekdayOfIso, Sakamoto's algorithm) — that is calendar math,
 * not payroll chronology, and is not the same convention as the mask above.
 */
import type { ScheduleResponse } from '../types/payrollSetup.ts';
import { WEEKDAY_NAMES, isValidIsoDate, weekdayOfIso } from './isoDate.ts';

// ── Readiness reasons ───────────────────────────────────────────────────────

export const READINESS_REASONS: Record<string, { label: string; description: string }> = {
  READY: {
    label: 'Ready',
    description: 'Schedule resolves for the next payroll period.',
  },
  NO_COMPANY_DEFAULT: {
    label: 'No company default',
    description: 'No default Setup, so nothing was assigned.',
  },
  NO_ASSIGNMENT: {
    label: 'Not assigned',
    description: 'No assignment covers the next payroll period.',
  },
  NO_PUBLISHED_VERSION: {
    label: 'No published version',
    description: 'The assigned Setup has no published Version for that date.',
  },
  AUTHORITY_BOUNDARY_CONFLICT: {
    label: 'Boundary conflict',
    description: 'The next period would cross a scheduled Setup/Version change.',
  },
  SETUP_NOT_ACTIVE: {
    label: 'Setup not active',
    description: 'The assigned Setup is archived.',
  },
  INVALID_SCHEDULE_BOUNDARY: {
    label: 'Invalid period start',
    description: 'The next start is not a period boundary under the schedule.',
  },
  BRANCH_NOT_OPERATIONAL: {
    label: 'Branch not operational',
    description: 'The Branch or Company is not active.',
  },
};

export interface ReadinessPresentation {
  code: string;
  label: string;
  description: string | null;
  known: boolean;
  isReady: boolean;
}

/** Callers handle a null reason themselves; this never accepts null. */
export function describeReadiness(code: string): ReadinessPresentation {
  if (!Object.prototype.hasOwnProperty.call(READINESS_REASONS, code)) {
    return { code, label: code, description: null, known: false, isReady: false };
  }
  const known = READINESS_REASONS[code];
  return {
    code,
    label: known.label,
    description: known.description,
    known: true,
    isReady: code === 'READY',
  };
}

// ── Weekday mask (backend convention: bit 0 = Sunday … bit 6 = Saturday) ────

export const WEEKDAYS: readonly { bit: number; short: string; full: string }[] = [
  { bit: 0, short: 'Sun', full: 'Sunday' },
  { bit: 1, short: 'Mon', full: 'Monday' },
  { bit: 2, short: 'Tue', full: 'Tuesday' },
  { bit: 3, short: 'Wed', full: 'Wednesday' },
  { bit: 4, short: 'Thu', full: 'Thursday' },
  { bit: 5, short: 'Fri', full: 'Friday' },
  { bit: 6, short: 'Sat', full: 'Saturday' },
];

/**
 * Ascending bit indices set in `mask`. Decodes without limiting — the 2-day
 * UI guidance lives in `canAddDayOff`/`MAX_NORMAL_DAYS_OFF` below and in
 * `validateDraftForm`; the backend (INVALID_NORMAL_DAYS_OFF) remains the
 * only real enforcement point.
 */
export function maskToDays(mask: number): number[] {
  const days: number[] = [];
  for (const { bit } of WEEKDAYS) {
    if ((mask & (1 << bit)) !== 0) days.push(bit);
  }
  return days;
}

/** Builds a mask from day-of-week bit indices. Throws RangeError for any value outside 0..6. */
export function daysToMask(days: readonly number[]): number {
  let mask = 0;
  for (const day of days) {
    if (!Number.isInteger(day) || day < 0 || day > 6) {
      throw new RangeError(`day out of range 0..6: ${day}`);
    }
    mask |= 1 << day;
  }
  return mask;
}

/** Toggles a single day bit. Throws RangeError for any bit outside 0..6, consistent with daysToMask. */
export function toggleDay(mask: number, bit: number): number {
  if (!Number.isInteger(bit) || bit < 0 || bit > 6) {
    throw new RangeError(`bit out of range 0..6: ${bit}`);
  }
  return mask ^ (1 << bit);
}

export function daysOffLabel(mask: number): string {
  const days = maskToDays(mask);
  if (days.length === 0) return 'None';
  return days.map((bit) => WEEKDAYS[bit].short).join(', ');
}

/**
 * Frontend guidance only (disables the "add a day off" control once two are
 * selected) — the backend is the enforcement point (INVALID_NORMAL_DAYS_OFF).
 */
export const MAX_NORMAL_DAYS_OFF = 2;

export function canAddDayOff(mask: number): boolean {
  return maskToDays(mask).length < MAX_NORMAL_DAYS_OFF;
}

// ── Frequency / schedule formatting ─────────────────────────────────────────

export function frequencyLabel(frequency: string): string {
  switch (frequency) {
    case 'Week':
      return 'Weekly';
    case 'Biweek':
      return 'Biweekly';
    case 'Month':
      return 'Monthly';
    case 'Custom':
      return 'Custom';
    default:
      return frequency;
  }
}

/**
 * Formats a schedule for display using the ISO anchor string as-is — no Date
 * parsing or timezone conversion.
 */
export function scheduleSummary(schedule: ScheduleResponse): string {
  const frequencyPart =
    schedule.payroll_frequency === 'Custom'
      ? schedule.custom_interval_days != null
        ? `Every ${schedule.custom_interval_days} days`
        : 'Custom (interval missing)'
      : frequencyLabel(schedule.payroll_frequency);

  return `${frequencyPart} · anchor ${schedule.anchor_start_date} · days off: ${daysOffLabel(schedule.normal_days_off_mask)}`;
}

/**
 * User-facing schedule summary, e.g. "Weekly · periods start Wednesday ·
 * days off: Sun, Sat". Names the anchor date's weekday using isoDate's pure
 * calendar arithmetic (never JS `Date`) instead of exposing the raw anchor
 * date or a "Custom (interval missing)"-style technical fragment; an
 * incomplete/invalid anchor date simply omits that segment. Kept alongside
 * (not instead of) `scheduleSummary`, which existing callers/tests keep
 * using verbatim.
 */
export function friendlyScheduleSummary(schedule: ScheduleResponse): string {
  const frequencyPart =
    schedule.payroll_frequency === 'Custom'
      ? schedule.custom_interval_days != null
        ? `${schedule.custom_interval_days}-day`
        : 'Custom'
      : frequencyLabel(schedule.payroll_frequency);

  const weekdayPart = isValidIsoDate(schedule.anchor_start_date)
    ? `periods start ${WEEKDAY_NAMES[weekdayOfIso(schedule.anchor_start_date)]}`
    : null;

  const daysOffPart = `days off: ${daysOffLabel(schedule.normal_days_off_mask)}`;

  return [frequencyPart, weekdayPart, daysOffPart].filter((part): part is string => part != null).join(' · ');
}
