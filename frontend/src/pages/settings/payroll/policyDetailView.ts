/**
 * Pure derivation helpers for the Payroll Policy detail header (master-detail
 * redesign Step 3A): which published Version the header represents, how its
 * schedule fields are worded, the detail-panel usage wording, and which
 * policy is selected.
 *
 * PURE MODULE: no React, no apiClient, `import type` only for API types.
 * Never constructs a JS Date object or reads the wall clock — "which Version
 * is in effect today" is decided by the backend (`VersionResponse.is_current`),
 * never computed here.
 */
import { compareIso, formatIsoLong, isValidIsoDate } from '../../../lib/isoDate.ts';
import { WEEKDAYS, frequencyLabel, maskToDays, friendlyScheduleSummary } from '../../../lib/payrollSetupReadiness.ts';
import type { VersionResponse } from '../../../types/payrollSetup';
import type { PolicyUsageState } from './policyMasterView.ts';

/**
 * The published Version currently in effect for `setupId`, as flagged by the
 * backend. Null when none is in effect (including a policy whose first
 * published Version starts in the future — never falls forward to it).
 * The setup_id filter guards against stale versions of a previously
 * selected policy.
 */
export function currentVersion(
  versions: readonly VersionResponse[],
  setupId: number,
): VersionResponse | null {
  return versions.find((v) => v.setup_id === setupId && v.is_current) ?? null;
}

/**
 * Whether `setupId` has at least one Published Version (the backend list is
 * Published-only; the lifecycle check is defensive). A Published Version
 * that starts in the future counts — the Company Default prerequisite is
 * "has been published", not "is in effect today". Drafts never count.
 */
export function hasPublishedVersion(versions: readonly VersionResponse[], setupId: number): boolean {
  return versions.some((v) => v.setup_id === setupId && v.lifecycle_state === 'Published');
}

/**
 * Detail-panel wording for the Step 2 derived usage state (the master list
 * keeps Active/Inactive/Archived; this is presentation only, not a second
 * status model).
 */
export function usageStateLabel(state: PolicyUsageState): string {
  switch (state) {
    case 'Active':
      return 'In use';
    case 'Inactive':
      return 'Not in use';
    case 'Archived':
      return 'Archived';
  }
}

export interface PolicyScheduleFields {
  frequency: string;
  firstPeriodStart: string;
  daysOff: string;
  /** Present only for Custom cadence with an interval; null otherwise (field is not shown). */
  interval: string | null;
}

/**
 * "Payroll schedule" card values, all from the one current Version's
 * schedule. Null when there is no current Version (the card shows its
 * empty state instead of placeholder values).
 */
export function policyScheduleFields(version: VersionResponse | null): PolicyScheduleFields | null {
  if (version === null) return null;
  const { schedule } = version;
  const days = maskToDays(schedule.normal_days_off_mask);
  return {
    frequency: frequencyLabel(schedule.payroll_frequency),
    firstPeriodStart: isValidIsoDate(schedule.anchor_start_date)
      ? formatIsoLong(schedule.anchor_start_date)
      : '—',
    daysOff: days.length === 0 ? 'No regular days off' : days.map((bit) => WEEKDAYS[bit].full).join(', '),
    interval:
      schedule.payroll_frequency === 'Custom' && schedule.custom_interval_days != null
        ? `${schedule.custom_interval_days} ${schedule.custom_interval_days === 1 ? 'day' : 'days'}`
        : null,
  };
}

/**
 * The Version Timeline is the effective chronology, not the planning list.
 * The versions endpoint is chronological and the backend marks the effective
 * terminal Version with `is_current`; no client date or wall clock is used.
 */
export function timelinePublishedVersions(versions: readonly VersionResponse[]): VersionResponse[] {
  const terminalPublished = versions.filter(
    (version) => version.lifecycle_state === 'Published' && version.is_terminal,
  );
  const currentIndex = terminalPublished.findIndex((version) => version.is_current);
  return currentIndex === -1 ? [] : terminalPublished.slice(0, currentIndex + 1).reverse();
}

/**
 * User-facing chronology for effective Published Versions. The persisted
 * version_number is a publication/creation sequence and is intentionally not
 * used for labels. Only terminal Versions through the backend-marked current
 * Version have taken effect; future-only policies therefore have no display
 * numbers yet.
 */
export function effectiveDisplayVersionNumbers(
  versions: readonly VersionResponse[],
): ReadonlyMap<number, number> {
  const terminalPublished = versions.filter(
    (version) => version.lifecycle_state === 'Published' && version.is_terminal,
  );
  const currentIndex = terminalPublished.findIndex((version) => version.is_current);
  if (currentIndex === -1) return new Map();

  const effective = terminalPublished
    .slice(0, currentIndex + 1)
    .sort((left, right) => compareIso(left.effective_from_date, right.effective_from_date));
  return new Map(effective.map((version, index) => [version.version_id, index + 1]));
}

/** Friendly label for a Published Version in user-facing planning/editor UI. */
export function startingPointLabel(
  version: VersionResponse,
  versions: readonly VersionResponse[],
): string {
  const displayNumber = effectiveDisplayVersionNumbers(versions).get(version.version_id);
  if (displayNumber != null) {
    return version.is_current
      ? `Version ${displayNumber} — current schedule`
      : `Version ${displayNumber} — effective ${formatIsoLong(version.effective_from_date)}`;
  }
  return `Scheduled update — starts ${formatIsoLong(version.effective_from_date)}`;
}

export function timelineDateRange(version: VersionResponse): string {
  const starts = isValidIsoDate(version.effective_from_date)
    ? formatIsoLong(version.effective_from_date)
    : 'Date unavailable';
  if (version.is_current) return `${starts} — Present`;
  const ends = version.effective_to_date && isValidIsoDate(version.effective_to_date)
    ? formatIsoLong(version.effective_to_date)
    : 'End date unavailable';
  return `${starts} — ${ends}`;
}

export function timelineScheduleSummary(version: VersionResponse): string {
  return friendlyScheduleSummary(version.schedule);
}

/**
 * The selected policy id: the `?setupId=` param when it names a candidate,
 * else the first candidate, else null (nothing selectable).
 */
export function resolveSelectedPolicyId(
  setupIdParam: string | null,
  candidates: readonly { setup_id: number }[],
): number | null {
  const parsed = setupIdParam != null ? Number(setupIdParam) : NaN;
  if (Number.isFinite(parsed) && candidates.some((c) => c.setup_id === parsed)) return parsed;
  return candidates[0]?.setup_id ?? null;
}
