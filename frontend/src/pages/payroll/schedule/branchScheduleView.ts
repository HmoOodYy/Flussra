/**
 * Pure presentation helpers for the Branch read-only Payroll Schedule page.
 *
 * PURE MODULE: no React import, no HTTP client of any kind, no browser
 * globals, no `Date` usage of any kind. This module never computes
 * readiness, the effective Setup/Version, or period boundaries — the
 * backend's BranchAdmin readiness fields and the /effective response are
 * the sole authority. ISO string comparisons here are only ever performed
 * against the backend-supplied schedule_readiness_date (never the wall
 * clock).
 */
import type {
  AssignmentHistoryResponse,
  BranchHistoryResponse,
  EffectiveAuthorityResponse,
  ScheduleResponse,
  VersionSegmentResponse,
} from '../../../types/payrollSetup';
import type { BranchAdmin } from '../../../types/settings';
import { WEEKDAYS, describeReadiness, maskToDays } from '../../../lib/payrollSetupReadiness.ts';
import { frequencyNoun } from '../../../lib/payrollBoundaryView.ts';
import { formatIsoLong, isValidIsoDate, weekdayOfIso } from '../../../lib/isoDate.ts';

// ── Branch id param / selection ──────────────────────────────────────────────

export const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

const POSITIVE_INTEGER_PATTERN = /^[1-9]\d*$/;

export type BranchIdParam =
  | { kind: 'absent' }
  | { kind: 'invalid'; raw: string }
  | { kind: 'id'; id: number };

/**
 * Parses a `?branchId=` query param.
 *   - null / '' -> absent.
 *   - A plain positive-integer string (no leading zero, no sign, no
 *     decimal/exponent) whose numeric value is a safe integer -> id.
 *   - Anything else -> invalid, carrying the raw string for display.
 */
export function parseBranchIdParam(raw: string | null): BranchIdParam {
  if (raw === null || raw === '') return { kind: 'absent' };
  if (POSITIVE_INTEGER_PATTERN.test(raw)) {
    const id = Number(raw);
    if (Number.isSafeInteger(id)) return { kind: 'id', id };
  }
  return { kind: 'invalid', raw };
}

export type BranchSelection =
  | { kind: 'none' }
  | { kind: 'redirect'; branchId: number }
  | { kind: 'invalid'; raw: string }
  | { kind: 'unavailable'; branchId: number }
  | { kind: 'selected'; branchId: number };

/**
 * Resolves the branch selection for the page from the parsed param and the
 * list of branches the caller may view. An id that is not among the
 * viewable branches is `unavailable` — it is never substituted with another
 * branch.
 */
export function resolveBranchSelection(
  param: BranchIdParam,
  viewableBranchIds: readonly number[],
): BranchSelection {
  if (param.kind === 'invalid') return { kind: 'invalid', raw: param.raw };
  if (param.kind === 'absent') {
    if (viewableBranchIds.length === 0) return { kind: 'none' };
    return { kind: 'redirect', branchId: viewableBranchIds[0] };
  }
  if (viewableBranchIds.includes(param.id)) return { kind: 'selected', branchId: param.id };
  return { kind: 'unavailable', branchId: param.id };
}

// ── Effective-authority request gating ───────────────────────────────────────

/**
 * True only when `reason` is exactly the string 'READY' (case-sensitive) and
 * `readinessDate` is a string matching ISO_DATE_PATTERN. Every other
 * combination — a non-READY code with a date, READY with a null/undefined
 * date, an unknown code, or a differently-cased code — is false.
 */
export function canRequestEffective(
  reason: string | null | undefined,
  readinessDate: string | null | undefined,
): boolean {
  return (
    reason === 'READY' &&
    typeof readinessDate === 'string' &&
    ISO_DATE_PATTERN.test(readinessDate)
  );
}

/**
 * Returns the branch's schedule_readiness_date unchanged when
 * canRequestEffective(...) is true for that branch, else null. Callers must
 * use this single function both to decide whether to call the /effective
 * endpoint and as the period_start_date argument passed to it.
 */
export function effectiveRequestDate(
  branch: Pick<BranchAdmin, 'schedule_readiness_reason' | 'schedule_readiness_date'>,
): string | null {
  return canRequestEffective(branch.schedule_readiness_reason, branch.schedule_readiness_date)
    ? branch.schedule_readiness_date
    : null;
}

// ── Readiness presentation ────────────────────────────────────────────────────

export const NOT_READY_EXPLANATIONS: Record<string, string> = {
  NO_ASSIGNMENT: 'No Payroll Setup assignment applies at the evaluated date.',
  NO_PUBLISHED_VERSION: 'The assigned Payroll Setup has no Published Version covering the evaluated date.',
  AUTHORITY_BOUNDARY_CONFLICT: 'The persisted schedule authority has a boundary conflict.',
};

/**
 * Explains a non-READY readiness code:
 *   1. An own-property lookup in NOT_READY_EXPLANATIONS.
 *   2. Else, if describeReadiness(code) reports a known code, its
 *      description (always non-null for known codes).
 *   3. Else (an unknown code, including prototype-pollution-shaped strings
 *      like 'constructor' or '__proto__'), a generic message naming the raw
 *      code.
 */
export function notReadyExplanation(code: string): string {
  if (Object.prototype.hasOwnProperty.call(NOT_READY_EXPLANATIONS, code)) {
    return NOT_READY_EXPLANATIONS[code];
  }
  const described = describeReadiness(code);
  if (described.known && described.description !== null) {
    return described.description;
  }
  return `The server reported readiness code ${code}, which this page does not recognize.`;
}

export type ReadinessView =
  | { kind: 'unavailable' }
  | { kind: 'ready'; code: string; label: string; description: string | null }
  | { kind: 'not-ready'; code: string; label: string; known: boolean; explanation: string };

/**
 * Presents a BranchAdmin.schedule_readiness_reason value. A null reason
 * means readiness is not visible/supplied and is never treated as Ready.
 */
export function readinessView(reason: string | null): ReadinessView {
  if (reason === null) return { kind: 'unavailable' };
  const described = describeReadiness(reason);
  if (reason === 'READY') {
    return { kind: 'ready', code: reason, label: described.label, description: described.description };
  }
  return {
    kind: 'not-ready',
    code: reason,
    label: described.label,
    known: described.known,
    explanation: notReadyExplanation(reason),
  };
}

// ── Date-ordering (ISO string comparison only) ────────────────────────────────

/**
 * True when `dateIso` falls strictly after `evaluationDate`, comparing the
 * two ISO YYYY-MM-DD strings lexicographically (never via `Date`). False
 * when `evaluationDate` is null, when either string fails ISO_DATE_PATTERN,
 * or when the two dates are equal.
 */
export function isAfterEvaluationDate(dateIso: string, evaluationDate: string | null): boolean {
  if (evaluationDate === null) return false;
  if (!ISO_DATE_PATTERN.test(dateIso) || !ISO_DATE_PATTERN.test(evaluationDate)) return false;
  return dateIso > evaluationDate;
}

// ── History annotation ────────────────────────────────────────────────────────

export interface AnnotatedVersion {
  segment: VersionSegmentResponse;
  governing: boolean;
  scheduled: boolean;
}

export interface AnnotatedAssignment {
  assignment: AssignmentHistoryResponse;
  withdrawn: boolean;
  governing: boolean;
  scheduled: boolean;
  versions: AnnotatedVersion[];
}

/**
 * Annotates branch assignment history with display-only tags, in the exact
 * order the backend returned them. Never adds, drops, merges, or re-sorts
 * rows, never mutates `history`/`effective`, and reuses the same assignment
 * and version-segment object references rather than copying them.
 *
 * `governing` is decided by an id match against `effective` only — never by
 * comparing dates — and requires `effective.branch_id` to match
 * `history.branch_id`. `scheduled` requires the row not be withdrawn and its
 * effective_from_date to fall strictly after `anchor`. A null `anchor`
 * suppresses every `scheduled` tag; a null `effective` suppresses every
 * `governing` tag.
 */
export function annotateHistory(
  history: BranchHistoryResponse,
  effective: EffectiveAuthorityResponse | null,
  anchor: string | null,
): AnnotatedAssignment[] {
  return history.assignments.map((assignment): AnnotatedAssignment => {
    const withdrawn = assignment.withdrawn_at_utc !== null;
    const governing =
      effective !== null &&
      effective.branch_id === history.branch_id &&
      assignment.assignment_id === effective.assignment_id;
    const scheduled = !withdrawn && isAfterEvaluationDate(assignment.effective_from_date, anchor);

    const versions: AnnotatedVersion[] = assignment.versions.map((segment): AnnotatedVersion => ({
      segment,
      governing: governing && effective !== null && segment.version_id === effective.version_id,
      scheduled: !withdrawn && isAfterEvaluationDate(segment.effective_from_date, anchor),
    }));

    return { assignment, withdrawn, governing, scheduled, versions };
  });
}

// ── Small display formatters ──────────────────────────────────────────────────

export function intervalLabel(from: string, to: string | null): string {
  return `${from} → ${to ?? 'open-ended'}`;
}

const BOUNDARY_KIND_LABELS: Record<string, string> = {
  Assignment: 'Assignment change',
  Version: 'Version change',
  AssignmentAndVersion: 'Assignment and Version change',
};

export function boundaryKindLabel(kind: string | null): string {
  if (kind === null) return 'None reported';
  if (Object.prototype.hasOwnProperty.call(BOUNDARY_KIND_LABELS, kind)) {
    return BOUNDARY_KIND_LABELS[kind];
  }
  return kind;
}

export function setupDisplay(code: string, name: string): string {
  return `${code} — ${name}`;
}

export function branchDisplay(branch: Pick<BranchAdmin, 'branch_name' | 'branch_code'>): string {
  return `${branch.branch_name} (${branch.branch_code})`;
}

// ── Human-readable schedule summary (Unit B) ────────────────────────────────
//
// Uses isoDate's pure calendar arithmetic (weekdayOfIso — never JS `Date`)
// to name a weekday; the day-of-month for Monthly comes from plain string
// slicing of the ISO period_start_date, not calendar math. Never computes
// payroll chronology itself — `schedule` and `periodStartDate` are both
// backend-supplied.

/** "Periods start Wednesday" (Weekly/Biweekly), "Periods start on day 4 of the month" (Monthly), "Every 10 days" (Custom). */
export function periodsStartLine(schedule: ScheduleResponse, periodStartDate: string): string {
  if (schedule.payroll_frequency === 'Week' || schedule.payroll_frequency === 'Biweek') {
    if (!isValidIsoDate(periodStartDate)) return '';
    return `Periods start ${WEEKDAYS[weekdayOfIso(periodStartDate)].full}`;
  }
  if (schedule.payroll_frequency === 'Month') {
    if (!isValidIsoDate(periodStartDate)) return '';
    const day = Number(periodStartDate.slice(8, 10));
    return `Periods start on day ${day} of the month`;
  }
  if (schedule.payroll_frequency === 'Custom') {
    return schedule.custom_interval_days != null ? `Every ${schedule.custom_interval_days} days` : 'Custom interval';
  }
  return '';
}

/** "Days off: Sunday, Saturday" (full names), or "No normal days off". */
export function fullDaysOffLine(mask: number): string {
  const days = maskToDays(mask);
  if (days.length === 0) return 'No normal days off';
  return `Days off: ${days.map((bit) => WEEKDAYS[bit].full).join(', ')}`;
}

/** The frequency noun line, e.g. "Weekly", "Biweekly", "10-day". */
export function scheduleFrequencyNoun(schedule: ScheduleResponse): string {
  return frequencyNoun(schedule.payroll_frequency, schedule.custom_interval_days);
}

// ── Upcoming change (Unit B) ────────────────────────────────────────────────

export type UpcomingChangeView =
  | { kind: 'none' }
  | { kind: 'assignment'; setupName: string; date: string }
  | { kind: 'version'; date: string }
  | { kind: 'unknown'; date: string };

/**
 * Explains `effective.next_boundary_date` in plain language: matches it
 * against the non-withdrawn assignment (or, failing that, any version
 * segment) in `history` that starts exactly then. Never computes the next
 * boundary itself — that is `effective.next_boundary_date`, verbatim.
 */
export function upcomingChange(
  effective: Pick<EffectiveAuthorityResponse, 'next_boundary_date'> | null,
  history: BranchHistoryResponse | null,
): UpcomingChangeView {
  const nextBoundary = effective?.next_boundary_date ?? null;
  if (nextBoundary === null) return { kind: 'none' };

  if (history) {
    const matchingAssignment = history.assignments.find(
      (a) => a.withdrawn_at_utc === null && a.effective_from_date === nextBoundary,
    );
    if (matchingAssignment) {
      return { kind: 'assignment', setupName: matchingAssignment.setup_name, date: nextBoundary };
    }
    const matchingVersion = history.assignments
      .flatMap((a) => a.versions)
      .find((v) => v.effective_from_date === nextBoundary);
    if (matchingVersion) {
      return { kind: 'version', date: nextBoundary };
    }
  }
  return { kind: 'unknown', date: nextBoundary };
}

/** Renders an UpcomingChangeView using isoDate's formatIsoLong — never a raw ISO string, never JS `Date`. */
export function upcomingChangeText(view: UpcomingChangeView): string {
  if (view.kind === 'none') return 'No scheduled policy change';
  if (view.kind === 'assignment') return `Changes to ${view.setupName} on ${formatIsoLong(view.date)}`;
  if (view.kind === 'version') return `Policy update on ${formatIsoLong(view.date)}`;
  return `Scheduled change on ${formatIsoLong(view.date)}`;
}

// ── Policy history lines (Unit B) ───────────────────────────────────────────

/** "<name> — <Mon D, YYYY> onward" / "<from> – <to>", suffixed " (cancelled)" when withdrawn. */
export function assignmentHistoryLine(row: Pick<AnnotatedAssignment, 'assignment' | 'withdrawn'>): string {
  const { assignment, withdrawn } = row;
  const span =
    assignment.effective_to_date != null
      ? `${formatIsoLong(assignment.effective_from_date)} – ${formatIsoLong(assignment.effective_to_date)}`
      : `${formatIsoLong(assignment.effective_from_date)} onward`;
  return `${assignment.setup_name} — ${span}${withdrawn ? ' (cancelled)' : ''}`;
}

/** "Version N from <Mon D, YYYY>" — never the raw version_id. */
export function versionHistoryLine(segment: Pick<VersionSegmentResponse, 'version_number' | 'effective_from_date'>): string {
  return `Version ${segment.version_number} from ${formatIsoLong(segment.effective_from_date)}`;
}
