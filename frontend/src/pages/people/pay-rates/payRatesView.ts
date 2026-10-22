import styles from './PayRatesPage.module.css';
import { formatRate } from '../../../lib/money';
import type { PayRateTab } from './payRatesDrivers';
import { compensationErrorMessage } from './payRatesErrors';

export interface EditRow {
  values: Record<number, string>;
  effective_from: string;
}

export type ActiveTab = PayRateTab;

export type { HistoryRow } from './payRatesHistory';

export type RuleForm = {
  ruleType: 'MinimumPay' | 'MaximumPay';
  action: 'add' | 'end';
  amount: string;
  effectiveFrom: string;
  effectiveTo: string;
  notes: string;
};

/** Every write on this page reports through the compensation error mapping. */
export const apiError = compensationErrorMessage;

export function fmtAmount(amount: string | null, unit: string | null, code: string | null, digits: number | null): string {
  if (amount === null) return '—';
  const text = formatRate(amount, code, digits);
  return unit ? `${text}/${unit}` : text;
}

export function statusBadgeClass(status: string): string {
  switch (status) {
    case 'Approved':   return styles.badgeApproved;
    case 'Superseded': return styles.badgeSuperseded;
    case 'Pending':    return styles.badgePending;
    case 'Voided':     return styles.badgeVoided;
    default:           return styles.statusBadge;
  }
}
