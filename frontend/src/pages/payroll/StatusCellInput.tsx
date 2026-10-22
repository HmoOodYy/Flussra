import { useId, useState } from 'react';
import {
  resolveStatusInput,
  statusInputSuggestions,
  type StatusCatalogEntry,
} from '../../lib/statusInput';

/**
 * The Day Grid Status cell: the typed name or alias of a Status (the catalog suggests both),
 * shown as the canonical primary name once committed. The client resolves the text for
 * convenience only; the server decides when the day is saved.
 */
export function StatusCellInput({
  shownText, statusKeys, disabled, className, onCommit,
}: {
  /** The canonical text to show while the cell is not being edited. */
  shownText: string;
  statusKeys: readonly StatusCatalogEntry[];
  disabled: boolean;
  className?: string;
  /** Receives the text to save: null clears the Status. */
  onCommit: (text: string | null) => void;
}) {
  const listId = useId();
  const [draft, setDraft] = useState<string | null>(null);
  const text = draft ?? shownText;
  const unresolved = draft !== null && draft.trim() !== ''
    && resolveStatusInput(draft, statusKeys).kind !== 'match';

  function commit() {
    if (draft === null) return;
    const typed = draft;
    setDraft(null);
    const resolution = resolveStatusInput(typed, statusKeys);
    if (resolution.kind === 'empty') onCommit(null);
    else onCommit(resolution.kind === 'match' ? resolution.entry.label : typed.trim());
  }

  return (
    <>
      <input
        className={className}
        type="text"
        list={listId}
        value={text}
        disabled={disabled}
        placeholder="—"
        aria-invalid={unresolved || undefined}
        title={unresolved ? 'Not a known Status name or alias; the server will check it on save.' : undefined}
        style={{ width: '9.5rem', ...(unresolved ? { borderColor: '#dc2626' } : null) }}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === 'Enter') commit(); }}
      />
      <datalist id={listId}>
        {statusKeys.flatMap((entry) => statusInputSuggestions(entry).map((option) => (
          <option key={`${entry.status_key_id}:${option.value}`} value={option.value} label={option.label} />
        )))}
      </datalist>
    </>
  );
}
