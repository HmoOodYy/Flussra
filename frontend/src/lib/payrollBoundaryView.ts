/**
 * Pure presentation helpers over BoundaryChoicesResponse (publication,
 * assignment, reassignment, and branch onboarding all share this shape — see
 * backend/app/payroll_setup/boundaries.py).
 *
 * PURE MODULE: no React, no apiClient. Never resolves which dates are valid
 * chronology boundaries itself — that judgment (requested_valid, previous,
 * next, suggested, conflicts) always comes from the backend response; this
 * module only formats it for display.
 */
import { formatIsoRange, formatIsoShort } from './isoDate.ts';
import { friendlyBoundaryConflictMessage } from './payrollSetupErrors.ts';
import type { BoundaryChoiceResponse, BoundaryChoicesResponse } from '../types/payrollSetup.ts';

export type BoundaryContext = 'publication' | 'assignment' | 'reassignment' | 'onboarding';

// ── Frequency wording ───────────────────────────────────────────────────────

export function frequencyNoun(frequency: string, customIntervalDays: number | null): string {
  switch (frequency) {
    case 'Week':
      return 'Weekly';
    case 'Biweek':
      return 'Biweekly';
    case 'Month':
      return 'Monthly';
    case 'Custom':
      return customIntervalDays != null ? `${customIntervalDays}-day` : 'Custom';
    default:
      return frequency;
  }
}

function baseDescription(choice: BoundaryChoiceResponse): string {
  const noun = frequencyNoun(choice.payroll_frequency, choice.custom_interval_days);
  let base = `${noun} payroll period: ${formatIsoRange(choice.date, choice.period_end_date)}`;
  if (choice.relation === 'current') {
    base += ' (current period)';
  }
  return base;
}

/**
 * The friendly, full explanation of a single boundary choice, decorated per
 * `context`:
 *   - reassignment: when the choice carries predecessor schedule fields,
 *     prefixed with a "Valid change point." sentence naming both schedules.
 *   - publication: when the choice replaces an existing same-date Version,
 *     suffixed with a note that publishing here replaces it.
 * Other contexts (assignment, onboarding) render the plain base sentence.
 */
export function describeBoundaryChoice(choice: BoundaryChoiceResponse, context: BoundaryContext): string {
  const base = baseDescription(choice);

  if (
    context === 'reassignment' &&
    choice.predecessor_payroll_frequency != null &&
    choice.predecessor_period_end_date != null
  ) {
    const successorNoun = frequencyNoun(choice.payroll_frequency, choice.custom_interval_days);
    const predecessorNoun = frequencyNoun(
      choice.predecessor_payroll_frequency,
      choice.predecessor_custom_interval_days,
    );
    const prefix =
      `Valid change point. ${predecessorNoun} period ends ${formatIsoShort(choice.predecessor_period_end_date)}. ` +
      `${successorNoun} policy begins ${formatIsoShort(choice.date)}.`;
    return `${prefix} ${base}`;
  }

  if (context === 'publication' && choice.replaces_version_number != null) {
    return `${base} A change already starts on this date (Version ${choice.replaces_version_number}); publishing here replaces it.`;
  }

  return base;
}

// ── Staleness / stepping ────────────────────────────────────────────────────

/** True only when `choices` was fetched for exactly this `value` (not stale). */
export function isChoicesCurrent(choices: BoundaryChoicesResponse | null, value: string): boolean {
  return choices != null && choices.requested_date === value;
}

/** The whole-date target to move to, or null when `choices` is stale or has no such neighbour. */
export function stepTarget(
  choices: BoundaryChoicesResponse | null,
  value: string,
  direction: 'next' | 'previous',
): string | null {
  if (!isChoicesCurrent(choices, value)) return null;
  const target = direction === 'next' ? choices!.next : choices!.previous;
  return target ? target.date : null;
}

// ── Status line ──────────────────────────────────────────────────────────────

export type BoundaryStatus =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'valid'; text: string }
  | { kind: 'invalid'; text: string; previous: string | null; next: string | null; codes: string[] }
  | { kind: 'none-found'; text: string };

const NO_DATE_FOUND_TEXT = 'No valid date was found nearby.';
const CANNOT_USE_DATE_TEXT = "This date can't be used.";

/**
 * Reduces a BoundaryChoicesResponse (plus loading/staleness) to the single
 * status the UI renders. The 'valid' text is the plain base description
 * (see baseDescription); a context-decorated explanation is available
 * separately via describeBoundaryChoice for callers that have a context.
 */
export function boundaryStatus(
  choices: BoundaryChoicesResponse | null,
  value: string,
  loading: boolean,
): BoundaryStatus {
  if (loading) return { kind: 'loading' };
  if (!isChoicesCurrent(choices, value)) return { kind: 'idle' };

  const current = choices!;
  if (current.requested_valid && current.requested) {
    return { kind: 'valid', text: baseDescription(current.requested) };
  }

  const previous = current.previous ? current.previous.date : null;
  const next = current.next ? current.next.date : null;
  if (previous == null && next == null) {
    const conflict = current.conflicts[0];
    return {
      kind: 'none-found',
      text: conflict
        ? friendlyBoundaryConflictMessage(conflict.code, conflict.reason, NO_DATE_FOUND_TEXT)
        : NO_DATE_FOUND_TEXT,
    };
  }

  const codes = current.conflicts.map((conflict) => conflict.code);
  const firstConflict = current.conflicts[0];
  const text = firstConflict
    ? friendlyBoundaryConflictMessage(firstConflict.code, firstConflict.reason, CANNOT_USE_DATE_TEXT)
    : CANNOT_USE_DATE_TEXT;
  return { kind: 'invalid', text, previous, next, codes };
}
