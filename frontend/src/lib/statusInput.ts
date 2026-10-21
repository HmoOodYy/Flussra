/**
 * Status input resolution, for convenience only.
 *
 * A day's Status is typed as text: the primary name, an alias or the technical code. The
 * server resolves that text Company-wide to exactly one Status and decides; this module only
 * mirrors the order of the server's lookup so the grid can show the canonical primary name
 * as soon as the text is committed and flag text that cannot resolve. It stores nothing:
 * a selection is always the Status, never the alias.
 */

/** The fields of a catalog entry the resolution needs (the Day Grid's active Statuses). */
export interface StatusCatalogEntry {
  status_key_id: number;
  key_code: string;
  label: string;
  aliases: string[];
}

/**
 * The normal form names and aliases are compared in: Unicode NFKC, trimmed, inner whitespace
 * collapsed, case-folded. The server's own normalization is the authority.
 */
export function normalizeStatusName(value: string): string {
  return value
    .normalize('NFKC')
    .trim()
    .replace(/\s+/g, ' ')
    .toLowerCase()
    .replace(/ß/g, 'ss')
    .replace(/ς/g, 'σ');
}

export type StatusInputVia = 'name' | 'alias' | 'code';

export type StatusInputResolution =
  | { kind: 'empty' }
  | { kind: 'match'; entry: StatusCatalogEntry; via: StatusInputVia }
  | { kind: 'ambiguous' }
  | { kind: 'unknown' };

/**
 * Resolve typed text: the normalized text is looked up as a primary name, then as an alias,
 * then as the technical code; the first lookup that matches decides, and it must match exactly
 * one Status.
 */
export function resolveStatusInput(
  text: string | null | undefined,
  catalog: readonly StatusCatalogEntry[],
): StatusInputResolution {
  if (text == null || text.trim() === '') return { kind: 'empty' };
  const needle = normalizeStatusName(text);
  const code = text.trim().toUpperCase();
  const lookups: Array<[StatusInputVia, (entry: StatusCatalogEntry) => boolean]> = [
    ['name', (entry) => normalizeStatusName(entry.label) === needle],
    ['alias', (entry) => entry.aliases.some((alias) => normalizeStatusName(alias) === needle)],
    ['code', (entry) => entry.key_code.toUpperCase() === code],
  ];
  for (const [via, matches] of lookups) {
    const found = catalog.filter(matches);
    if (found.length === 1) return { kind: 'match', entry: found[0], via };
    if (found.length > 1) return { kind: 'ambiguous' };
  }
  return { kind: 'unknown' };
}

/**
 * The text to save for what was typed: blank clears the Status (null); text that resolves is
 * saved as the canonical primary name (a name always resolves to its own Status first, so the
 * save can never be redirected by an alias or a code); anything else is sent as typed and the
 * server rejects it with its own message.
 */
export function committedStatusText(
  typed: string,
  catalog: readonly StatusCatalogEntry[],
): string | null {
  const resolution = resolveStatusInput(typed, catalog);
  if (resolution.kind === 'empty') return null;
  if (resolution.kind === 'match') return resolution.entry.label;
  return typed.trim();
}

/** What a cell shows for the saved or committed text: the canonical name when it resolves. */
export function displayedStatusText(
  saved: string | null | undefined,
  catalog: readonly StatusCatalogEntry[],
): string {
  const resolution = resolveStatusInput(saved, catalog);
  if (resolution.kind === 'empty') return '';
  if (resolution.kind === 'match') return resolution.entry.label;
  return (saved ?? '').trim();
}

/** Every text that selects the Status: the name first, then its aliases. */
export function statusInputSuggestions(
  entry: Pick<StatusCatalogEntry, 'label' | 'aliases'>,
): Array<{ value: string; label: string }> {
  return [
    { value: entry.label, label: entry.label },
    ...entry.aliases.map((alias) => ({ value: alias, label: entry.label })),
  ];
}
