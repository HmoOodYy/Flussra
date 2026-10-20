import type { CalculationPreviewDriver } from '../../types/payroll/calculation';
import { sameDecimal } from '../../lib/amountEntry.ts';

/**
 * The business meaning of a driver's Minimum/Maximum evaluation, or null when there is
 * nothing worth showing. Only a top-up or cap that changed the pay is shown, with the
 * whole-day proration when the driver covered part of the period; the rounding itself
 * (pre-rounding values, the proration version) is never surfaced here.
 */
export function minMaxNote(driver: Pick<CalculationPreviewDriver, 'minmax' | 'minimum_adjustment' | 'maximum_adjustment'>): string | null {
  const minmax = driver.minmax;
  if (!minmax) return null;
  const hasRule = minmax.minimum_rule_id != null || minmax.maximum_rule_id != null;
  if (minmax.status === 'NO_ELIGIBLE_SCHEDULED_DAYS' && hasRule) {
    return 'Minimum/Maximum not applied: no eligible scheduled days in this period';
  }
  if (minmax.status !== 'APPLIED') return null;
  const partial = minmax.numerator != null && minmax.denominator != null && minmax.numerator < minmax.denominator;
  const suffix = partial ? `, prorated ${minmax.numerator}/${minmax.denominator} days` : '';
  if (!sameDecimal(driver.minimum_adjustment, '0')) return `Minimum top-up${suffix}`;
  if (!sameDecimal(driver.maximum_adjustment, '0')) return `Maximum cap${suffix}`;
  return null;
}
