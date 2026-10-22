import type { ScheduleValue } from '../../../types/compensation.ts';

/**
 * A rate schedule is either one scalar value or the complete value set of an OrdinalTier
 * definition (one value per ordinal tier). Wherever the Pay Rates screens show a schedule they
 * must not reduce a tiered one to its first value: the amount of a scalar schedule is its only
 * value, a tiered schedule has no single amount and is shown tier by tier.
 */

export interface TierValueView {
  /** The ordinal positions the tier covers, e.g. "1", "2–4" or "5+". */
  label: string;
  /** The tier's value; null while it has none (missing, never zero). */
  amount: string | null;
}

export function isTieredSchedule(values: ScheduleValue[]): boolean {
  return values.some((value) => value.ordinal_from != null);
}

export function tierRangeLabel(ordinalFrom: number, ordinalTo: number | null): string {
  if (ordinalTo === null) return `${ordinalFrom}+`;
  return ordinalTo === ordinalFrom ? `${ordinalFrom}` : `${ordinalFrom}–${ordinalTo}`;
}

/** Every tier of a tiered schedule in topology order; empty for a scalar schedule. */
export function tierValueViews(values: ScheduleValue[]): TierValueView[] {
  if (!isTieredSchedule(values)) return [];
  return [...values]
    .sort((a, b) => a.sequence_no - b.sequence_no)
    .map((value) => ({
      label: tierRangeLabel(value.ordinal_from as number, value.ordinal_to),
      amount: value.amount,
    }));
}

/** The one amount of a scalar schedule; null for a tiered schedule (it has no single amount). */
export function scalarAmount(values: ScheduleValue[]): string | null {
  if (isTieredSchedule(values)) return null;
  return values[0]?.amount ?? null;
}

/** A schedule can be approved only when every value it holds is set: all tiers, or the one amount. */
export function hasCompleteValues(values: ScheduleValue[]): boolean {
  return values.length > 0 && values.every((value) => value.amount !== null);
}
