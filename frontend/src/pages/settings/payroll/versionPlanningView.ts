/**
 * Pure presentation helpers for the Step 3B-1 Version Planning surface.
 *
 * The versions endpoint is already ordered by effective date and marks the
 * one terminal Published Version effective on company-local today with
 * `is_current`. This module deliberately never reads the browser clock or
 * reconstructs chronology from client-local dates.
 */
import { friendlyScheduleSummary, maskToDays, WEEKDAYS } from '../../../lib/payrollSetupReadiness.ts';
import { MONTH_SHORT, formatIsoLong, isValidIsoDate, isoToParts } from '../../../lib/isoDate.ts';
import type { DraftResponse, VersionResponse } from '../../../types/payrollSetup.ts';

/**
 * Returns only future terminal Published Versions for the planning surface.
 *
 * The backend read model is chronological. Replaced same-date Versions are
 * removed by `is_terminal`, historical Versions are before the current
 * terminal Version, and a no-current response is the backend's future-only
 * state (there is no terminal version effective on company-local today).
 */
export function upcomingPublishedVersions(versions: readonly VersionResponse[]): VersionResponse[] {
  const terminalPublished = versions.filter(
    (version) => version.lifecycle_state === 'Published' && version.is_terminal,
  );
  const currentIndex = terminalPublished.findIndex((version) => version.is_current);
  return currentIndex === -1 ? terminalPublished : terminalPublished.slice(currentIndex + 1);
}

export interface PlanningDateParts {
  month: string;
  day: string;
  year: string;
  long: string;
}

export function planningDateParts(isoDate: string): PlanningDateParts | null {
  if (!isValidIsoDate(isoDate)) return null;
  const parts = isoToParts(isoDate);
  return {
    month: MONTH_SHORT[Number(parts.month) - 1],
    day: String(Number(parts.day)),
    year: parts.year,
    long: formatIsoLong(isoDate),
  };
}

export function planningDaysOffLabel(mask: number): string {
  const names = maskToDays(mask).map((bit) => WEEKDAYS[bit].full);
  if (names.length === 0) return 'No regular days off';
  if (names.length === 1) return `${names[0]} off`;
  return `${names.slice(0, -1).join(', ')} & ${names[names.length - 1]} off`;
}

export function draftPlanningSummary(draft: DraftResponse): string {
  if (
    draft.payroll_frequency == null ||
    draft.anchor_start_date == null ||
    draft.normal_days_off_mask == null ||
    (draft.payroll_frequency === 'Custom' && draft.custom_interval_days == null)
  ) {
    return 'Incomplete schedule';
  }

  return friendlyScheduleSummary({
    payroll_frequency: draft.payroll_frequency,
    anchor_start_date: draft.anchor_start_date,
    custom_interval_days: draft.custom_interval_days,
    normal_days_off_mask: draft.normal_days_off_mask,
  });
}
