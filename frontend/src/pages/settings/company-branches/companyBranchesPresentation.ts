export interface SuccessInfo {
  title: string;
  sub: string;
  detail?: string;
  code?: string | null;
  links?: { to: string; label: string }[];
}

export type ConfirmKind = 'save-company' | 'save-branch' | 'set-default';

export function fmtDate(dt: string | null): string {
  if (!dt) return '—';
  return new Date(dt).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d) && d.length > 0)
    return d.map((i: unknown) => i && typeof i === 'object' && 'msg' in i ? String((i as { msg: unknown }).msg) : null).filter(Boolean).join(' ');
  return 'An unexpected error occurred.';
}
