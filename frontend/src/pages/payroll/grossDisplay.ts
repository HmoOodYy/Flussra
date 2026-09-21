/**
 * Day Grid gross-total display — resolves the backend's authoritative
 * financials_available/gross_total pair (app.payroll.day_grid, CP-2F) into
 * the text shown next to "Gross:" in the summary bar.
 *
 * Draft (Prepared) periods have no authoritative financial truth yet — the
 * backend sends gross_total=null and financials_available=false for them.
 * This module never computes a gross figure itself; it only decides what
 * text to show for a value the backend already resolved.
 */

export const GROSS_UNAVAILABLE_MESSAGE = 'Not available';

/**
 * `grossTotal` is the backend's DayGridSummary.gross_total value. A null
 * value means the backend has no authoritative gross for this period's
 * current status (Draft) — show a neutral unavailable message instead of a
 * fabricated "$null" or a manufactured "$0.00".
 */
export function formatGrossTotal(grossTotal: string | null): string {
  return grossTotal == null ? GROSS_UNAVAILABLE_MESSAGE : `$${grossTotal}`;
}
