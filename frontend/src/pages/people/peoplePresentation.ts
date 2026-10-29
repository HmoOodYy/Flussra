import { accessSafetyMessage } from '../../lib/accessSafetyErrors.ts';

// ─── Helpers ──────────────────────────────────────────────────────────────────

export function apiError(err: unknown): string {
  const safety = accessSafetyMessage(err);
  if (safety) return safety;
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d))
    return d.map((i: unknown) =>
      i && typeof i === 'object' && 'msg' in i ? String((i as { msg: unknown }).msg) : null
    ).filter(Boolean).join(' ');
  if (d && typeof d === 'object' && 'message' in d) return String((d as { message: unknown }).message);
  return 'An unexpected error occurred.';
}

export function fmtDate(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export function fmtDateTime(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

export function avatarInitials(name: string): string {
  return name.split(' ').map(w => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase();
}

export function scopeLabel(scope: string | null, branchName: string | null): string {
  if (!scope) return '—';
  if (scope === 'AllCompanyBranches') return 'All branches';
  if (scope === 'SpecificBranch') return branchName ? branchName : 'Specific branch';
  if (scope === 'Self') return 'Self';
  return scope;
}
