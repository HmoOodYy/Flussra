import styles from './PayRatesPage.module.css';
import { formatRate } from '../../../lib/money';
import type { AssignmentSummary } from '../../../types/compensation';

export interface EditRow {
  amount: string;
  effective_from: string;
}

export type ActiveTab = 'current' | 'pending' | 'history' | 'pay-rules';

export interface HistoryRow extends AssignmentSummary {
  definition_name: string;
  unit: string | null;
}

export type RuleForm = {
  ruleType: 'MinimumPay' | 'MaximumPay';
  action: 'add' | 'end';
  amount: string;
  effectiveFrom: string;
  effectiveTo: string;
  notes: string;
};

export function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (d && typeof d === 'object' && typeof (d as { message?: unknown }).message === 'string') {
    return (d as { message: string }).message;
  }
  if (Array.isArray(d) && d.length > 0) {
    const first = d[0];
    if (first?.msg) return first.msg;
  }
  return 'An unexpected error occurred.';
}

export function fmtAmount(amount: string | null, unit: string | null, code: string | null, digits: number | null): string {
  if (amount === null) return '—';
  const text = formatRate(amount, code, digits);
  return unit ? `${text}/${unit}` : text;
}

export function today(): string {
  return new Date().toISOString().slice(0, 10);
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
