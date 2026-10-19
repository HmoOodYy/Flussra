import type { DriverTransferRequest } from '../../../types/transfer.ts';
import { TRANSFER_ACTIVE, TRANSFER_TERMINAL } from '../../../types/transfer.ts';
import { isValidIsoDate } from '../../../lib/isoDate.ts';

export function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d))
    return d
      .map((i: unknown) =>
        i && typeof i === 'object' && 'msg' in i ? String((i as { msg: unknown }).msg) : null,
      )
      .filter(Boolean)
      .join(' ');
  return 'An unexpected error occurred.';
}

export function fmtDate(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  });
}

export function fmtDateTime(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

export function fmtEffectiveAt(s: string | null | undefined): string {
  return s ? fmtDateTime(s) : 'On completion';
}

export function normalizeUtcDateTimeLocal(value: string): string | null {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if (!match || !isValidIsoDate(match[1])) return null;
  const hour = Number(match[2]);
  const minute = Number(match[3]);
  const second = Number(match[4] ?? '0');
  if (hour > 23 || minute > 59 || second > 59) return null;
  return `${match[1]}T${match[2]}:${match[3]}:${match[4] ?? '00'}Z`;
}

// ─── Filter types ─────────────────────────────────────────────────────────────

export type FilterView = 'all' | 'active' | 'pending_source' | 'pending_target' | 'returned' | 'approved' | 'scheduled' | 'completed' | 'terminal';

export const FILTER_LABELS: Record<FilterView, string> = {
  all:            'All',
  active:         'Active',
  pending_source: 'Awaiting Source',
  pending_target: 'Awaiting Target',
  returned:       'Returned',
  approved:       'Ready to Complete',
  scheduled:      'Scheduled',
  completed:      'Completed',
  terminal:       'Closed',
};

export function matchesFilter(req: DriverTransferRequest, filter: FilterView): boolean {
  switch (filter) {
    case 'all':            return true;
    case 'active':         return TRANSFER_ACTIVE.has(req.status);
    case 'pending_source': return req.status === 'PendingSourceApproval';
    case 'pending_target': return req.status === 'PendingTargetApproval';
    case 'returned':       return req.status === 'Returned';
    case 'approved':       return req.status === 'Approved';
    case 'scheduled':      return req.status === 'Scheduled';
    case 'completed':      return req.status === 'Completed';
    case 'terminal':       return TRANSFER_TERMINAL.has(req.status);
  }
}
