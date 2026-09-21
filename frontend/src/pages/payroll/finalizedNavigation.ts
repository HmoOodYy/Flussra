/**
 * Post-finalization navigation — the smallest link between "a period was
 * just finalized" and the Ledger page (/payroll/ledger), the existing
 * canonical destination for finalized (Locked/Archived) payroll.
 *
 * This does not invent a new destination: the Ledger page already lists
 * finalized periods and opens the existing FinalizedPayrollLibraryDialog /
 * FinalSummaryDialog for one. It previously had no way to preselect a period
 * from a link, so this module adds the smallest query-param contract needed
 * for that — nothing about Ledger's own display or discovery logic changes.
 */

const PERIOD_ID_PARAM = 'period_id';

/** Path to the Ledger page with `periodId` preselected via query param. */
export function finalizedLedgerPath(periodId: number): string {
  return `/payroll/ledger?${PERIOD_ID_PARAM}=${periodId}`;
}

/**
 * Parse the preselected period id out of a Ledger URLSearchParams, or null
 * if absent/invalid. Never throws on malformed input.
 */
export function resolvePreselectedPeriodId(searchParams: URLSearchParams): number | null {
  const raw = searchParams.get(PERIOD_ID_PARAM);
  if (raw == null) return null;
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 ? n : null;
}
