import type { FinalizedSectionAvailability } from '../../../types/payroll';
import { formatMoney as formatCurrencyMoney } from '../../../lib/money';

export function formatMoney(value: unknown, code?: string | null, digits?: number | null): string {
  if (value == null) return 'Unavailable';
  return formatCurrencyMoney(typeof value === 'number' || typeof value === 'string' ? value : String(value), code, digits);
}

export function formatValue(value: unknown): string {
  if (value == null) return '—';
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return value.toLocaleString();
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function formatHash(value: string | null): string {
  if (!value) return 'Not available';
  return value.length > 18 ? `${value.slice(0, 18)}…` : value;
}

export function availabilityMessage(
  availability: FinalizedSectionAvailability | undefined,
  emptyText: string,
): string {
  if (!availability || availability.state === 'UNAVAILABLE') {
    return availability?.reason_code
      ? `Unavailable: ${availability.reason_code}`
      : 'Unavailable from finalized history.';
  }
  if (availability.state === 'EMPTY') return emptyText;
  if (availability.state === 'PARTIAL') {
    return availability.reason_code
      ? `Partially available: ${availability.reason_code}`
      : 'Partially available from finalized history.';
  }
  return emptyText;
}
