import type { DriverTransferRequest } from '../../../types/transfer.ts';
import { TRANSFER_ACTIVE, TRANSFER_TERMINAL } from '../../../types/transfer.ts';

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

// ─── Filter types ─────────────────────────────────────────────────────────────

export type FilterView = 'all' | 'active' | 'pending_source' | 'pending_target' | 'returned' | 'approved' | 'completed' | 'terminal';

export const FILTER_LABELS: Record<FilterView, string> = {
  all:            'All',
  active:         'Active',
  pending_source: 'Awaiting Source',
  pending_target: 'Awaiting Target',
  returned:       'Returned',
  approved:       'Ready to Complete',
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
    case 'completed':      return req.status === 'Completed';
    case 'terminal':       return TRANSFER_TERMINAL.has(req.status);
  }
}
