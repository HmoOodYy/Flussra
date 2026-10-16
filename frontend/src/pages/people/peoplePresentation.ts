import type { UserAdmin } from '../../types/admin';

// ─── Helpers ──────────────────────────────────────────────────────────────────

export function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d))
    return d.map((i: unknown) =>
      i && typeof i === 'object' && 'msg' in i ? String((i as { msg: unknown }).msg) : null
    ).filter(Boolean).join(' ');
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

export type AccessStatus = { label: string; kind: 'ok' | 'warn' | 'err' };
export function accessStatus(p: UserAdmin): AccessStatus {
  if (p.is_staged)       return { label: 'Staged',           kind: 'warn' };
  if (!p.is_active)       return { label: 'Inactive',         kind: 'err'  };
  if (!p.can_login)       return { label: 'Login disabled',   kind: 'warn' };
  if (!p.company_role_id) return { label: 'No role assigned', kind: 'warn' };
  return { label: 'Complete', kind: 'ok' };
}
