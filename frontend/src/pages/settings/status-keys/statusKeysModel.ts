import type { StatusKey } from '../../../types/settings';

export type ConfirmKind = 'save-key' | 'deactivate-key' | 'reactivate-key';

export function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })
    ?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d) && d.length > 0)
    return d
      .map((i: unknown) =>
        i && typeof i === 'object' && 'msg' in i
          ? String((i as { msg: unknown }).msg)
          : null,
      )
      .filter(Boolean)
      .join(' ');
  return 'An unexpected error occurred.';
}

export interface KeyForm {
  key_name: string;
  hours_value: string;
  is_off_reason: boolean;
  deducts_from_yearly_allowance: boolean;
  allowance_category: string;
  // Usage limits
  limit_uses_per_period_enabled: boolean;
  limit_uses_per_period: string;
  limit_uses_per_driver_enabled: boolean;
  limit_uses_per_driver: string;
  limit_uses_across_drivers_enabled: boolean;
  limit_uses_across_drivers: string;
  limit_uses_per_day_enabled: boolean;
  limit_uses_per_day: string;
}

export const EMPTY_KEY_FORM: KeyForm = {
  key_name:                          '',
  hours_value:                       '0',
  is_off_reason:                     true,
  deducts_from_yearly_allowance:     false,
  allowance_category:                '',
  limit_uses_per_period_enabled:     false,
  limit_uses_per_period:             '',
  limit_uses_per_driver_enabled:     false,
  limit_uses_per_driver:             '',
  limit_uses_across_drivers_enabled: false,
  limit_uses_across_drivers:         '',
  limit_uses_per_day_enabled:        false,
  limit_uses_per_day:                '',
};

export function keyToForm(k: StatusKey): KeyForm {
  return {
    key_name:                          k.key_name,
    hours_value:                       String(k.hours_value),
    is_off_reason:                     k.is_off_reason,
    deducts_from_yearly_allowance:     k.deducts_from_yearly_allowance,
    allowance_category:                k.allowance_category ?? '',
    limit_uses_per_period_enabled:     k.limit_uses_per_period_enabled,
    limit_uses_per_period:             k.limit_uses_per_period != null ? String(k.limit_uses_per_period) : '',
    limit_uses_per_driver_enabled:     k.limit_uses_per_driver_enabled,
    limit_uses_per_driver:             k.limit_uses_per_driver != null ? String(k.limit_uses_per_driver) : '',
    limit_uses_across_drivers_enabled: k.limit_uses_across_drivers_enabled,
    limit_uses_across_drivers:         k.limit_uses_across_drivers != null ? String(k.limit_uses_across_drivers) : '',
    limit_uses_per_day_enabled:        k.limit_uses_per_day_enabled,
    limit_uses_per_day:                k.limit_uses_per_day != null ? String(k.limit_uses_per_day) : '',
  };
}

export type KeysLoadState = { keys: StatusKey[]; loading: boolean; error: string; selectedKeyId: number | null };
export type KeysLoadAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    keys: StatusKey[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'SELECT';      id: number | null };

export function keysLoadReducer(s: KeysLoadState, a: KeysLoadAction): KeysLoadState {
  switch (a.type) {
    case 'FETCH_START': return { keys: [],      loading: true,  error: '', selectedKeyId: null };
    case 'FETCH_OK':    return { keys: a.keys,  loading: false, error: '', selectedKeyId: s.selectedKeyId };
    case 'FETCH_ERROR': return { keys: [],      loading: false, error: a.error, selectedKeyId: null };
    case 'SELECT':      return { ...s, selectedKeyId: a.id };
  }
}
