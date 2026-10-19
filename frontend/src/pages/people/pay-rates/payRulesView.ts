import type { DriverPayRule } from './driverPayRulesApi.ts';

/** Presentation of a Min/Max rule's captured home pay calendar. It is read-only: the server records it. */

const FREQUENCY_LABELS: Record<string, string> = {
  Week: 'Weekly',
  Biweek: 'Every 2 weeks',
  Month: 'Monthly',
};

type CalendarFields = Pick<
  DriverPayRule,
  'calendar_frequency' | 'calendar_anchor_start_date' | 'calendar_custom_interval_days'
>;

/** "Weekly, periods anchored on 2026-01-05" / "Every 10 days, periods anchored on ...". */
export function describeCalendar(rule: CalendarFields): string {
  const frequency = rule.calendar_frequency === 'Custom' && rule.calendar_custom_interval_days != null
    ? `Every ${rule.calendar_custom_interval_days} days`
    : FREQUENCY_LABELS[rule.calendar_frequency] ?? rule.calendar_frequency;
  return `${frequency}, periods anchored on ${rule.calendar_anchor_start_date}`;
}

/** The D8 hint: a later tenure begins in a Branch whose pay calendar this rule does not fit. */
export function reconciliationWarning(
  rule: Pick<DriverPayRule, 'calendar_reconciliation_needed_from'>,
): string | null {
  const from = rule.calendar_reconciliation_needed_from;
  if (!from) return null;
  return `Min/Max needs reconciliation from ${from}: the home pay calendar changes. `
    + 'End this rule and create a new one.';
}
