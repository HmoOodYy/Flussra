import { normalizeStatusName } from '../../../lib/statusInput.ts';
import {
  STATUS_CATEGORIES,
  type StatusCategory,
  type StatusKey,
  type StatusKeyCreate,
  type StatusKeyUpdate,
} from '../../../types/settings.ts';

export type ConfirmKind = 'save-key' | 'deactivate-key' | 'reactivate-key';

/** The longest name or alias the server stores. */
export const STATUS_NAME_MAX_LENGTH = 200;

/**
 * The message of a failed Status settings request. The server's coded errors carry their own
 * message ({code, message}); validation errors are a list; older errors a plain string.
 */
export function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })
    ?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d) && d.length > 0) {
    const joined = d
      .map((i: unknown) =>
        i && typeof i === 'object' && 'msg' in i
          ? String((i as { msg: unknown }).msg)
          : null,
      )
      .filter(Boolean)
      .join(' ');
    if (joined) return joined;
  }
  if (d && typeof d === 'object' && 'message' in d) {
    const message = (d as { message: unknown }).message;
    if (typeof message === 'string' && message.trim()) return message;
  }
  return 'An unexpected error occurred.';
}

/** The editor's state. Numbers stay text while typing; `status_rate_column_id` '' means none. */
export interface KeyForm {
  key_name: string;
  category: StatusCategory;
  hours_value: string;
  /** "Counts as a full day off". */
  is_off_reason: boolean;
  status_rate_column_id: string;
  is_active: boolean;
  display_order: string;
  /** One text per row of the alias editor; blank rows are ignored. */
  aliases: string[];
}

export const EMPTY_KEY_FORM: KeyForm = {
  key_name: '',
  category: 'Other',
  hours_value: '0',
  is_off_reason: true,
  status_rate_column_id: '',
  is_active: true,
  display_order: '0',
  aliases: [],
};

export function keyToForm(k: StatusKey): KeyForm {
  return {
    key_name: k.key_name,
    category: k.category,
    hours_value: String(Number(k.hours_value)),
    is_off_reason: k.is_off_reason,
    status_rate_column_id: k.status_rate_column_id == null ? '' : String(k.status_rate_column_id),
    is_active: k.is_active,
    display_order: String(k.display_order),
    aliases: [...k.aliases],
  };
}

/**
 * The aliases to send: trimmed, blank rows dropped, and an alias repeated under the same
 * normal form kept once (the server stores one alias per normal form).
 */
export function cleanAliases(rows: readonly string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const row of rows) {
    const alias = row.trim();
    if (alias === '') continue;
    const normal = normalizeStatusName(alias);
    if (seen.has(normal)) continue;
    seen.add(normal);
    out.push(alias);
  }
  return out;
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

/**
 * What the form cannot send. The server stays the authority (name uniqueness, alias
 * ambiguity, the semantic lock); this only catches what is certain before the request.
 */
export function validateKeyForm(form: KeyForm): string {
  const name = form.key_name.trim();
  if (name === '') return 'Name is required.';
  if (name.length > STATUS_NAME_MAX_LENGTH) return `Name must be at most ${STATUS_NAME_MAX_LENGTH} characters.`;
  if (!STATUS_CATEGORIES.includes(form.category)) return 'Choose a Category.';
  const hours = Number(form.hours_value);
  if (form.hours_value.trim() === '' || Number.isNaN(hours) || hours < 0 || hours > 24) {
    return 'Hours must be between 0 and 24.';
  }
  if (form.status_rate_column_id !== '' && hours <= 0) {
    return 'Hours must be greater than 0 when a Status rate column is set.';
  }
  const order = Number(form.display_order);
  if (form.display_order.trim() === '' || !Number.isInteger(order)) {
    return 'Display order must be a whole number.';
  }
  const aliases = cleanAliases(form.aliases);
  if (aliases.some((alias) => alias.length > STATUS_NAME_MAX_LENGTH)) {
    return `An alias must be at most ${STATUS_NAME_MAX_LENGTH} characters.`;
  }
  const own = normalizeStatusName(name);
  if (aliases.some((alias) => normalizeStatusName(alias) === own)) {
    return 'An alias cannot be the same as the Status name.';
  }
  return '';
}

export function buildCreatePayload(form: KeyForm): StatusKeyCreate {
  return {
    key_name: form.key_name.trim(),
    category: form.category,
    aliases: cleanAliases(form.aliases),
    hours_value: Number(form.hours_value),
    is_off_reason: form.is_off_reason,
    is_active: true,
    display_order: Number(form.display_order),
    status_rate_column_id: form.status_rate_column_id === '' ? null : Number(form.status_rate_column_id),
  };
}

/**
 * Only the fields the user changed. A Status in use cannot change hours, the off-day
 * semantic, Category or rate column, so those are never sent unless they changed; the alias
 * set is sent whole, and only when it changed.
 */
export function buildUpdatePayload(form: KeyForm, original: StatusKey): StatusKeyUpdate {
  const payload: StatusKeyUpdate = {};
  const name = form.key_name.trim();
  if (name !== original.key_name) payload.key_name = name;
  if (form.category !== original.category) payload.category = form.category;
  const hours = Number(form.hours_value);
  if (hours !== Number(original.hours_value)) payload.hours_value = hours;
  if (form.is_off_reason !== original.is_off_reason) payload.is_off_reason = form.is_off_reason;
  const column = form.status_rate_column_id === '' ? null : Number(form.status_rate_column_id);
  if (column !== original.status_rate_column_id) payload.status_rate_column_id = column;
  if (form.is_active !== original.is_active) payload.is_active = form.is_active;
  const order = Number(form.display_order);
  if (order !== original.display_order) payload.display_order = order;
  const aliases = cleanAliases(form.aliases);
  if (!sameList(aliases, original.aliases)) payload.aliases = aliases;
  return payload;
}

/** The server's order: display order, then name. */
export function sortStatusKeys(keys: readonly StatusKey[]): StatusKey[] {
  return [...keys].sort((a, b) =>
    a.display_order - b.display_order || a.key_name.localeCompare(b.key_name));
}

export type KeyFilter = 'Active' | 'Inactive';

/** The Statuses a filter shows (the catalog is loaded whole, inactive ones included). */
export function visibleStatusKeys(keys: readonly StatusKey[], filter: KeyFilter): StatusKey[] {
  return keys.filter((k) => (filter === 'Active' ? k.is_active : !k.is_active));
}

/** The label of the Status rate column a Status is bound to, or null when it is not paid. */
export function statusColumnName(
  columns: readonly { status_rate_column_id: number; column_name: string }[],
  columnId: number | null,
): string | null {
  if (columnId == null) return null;
  return columns.find((c) => c.status_rate_column_id === columnId)?.column_name ?? `Column #${columnId}`;
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
