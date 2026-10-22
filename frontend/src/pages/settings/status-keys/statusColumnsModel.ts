import type { StatusKey, StatusRateColumn } from '../../../types/settings';

/** Company Status rate columns: the default first, then by name. */
export function sortColumns(columns: readonly StatusRateColumn[]): StatusRateColumn[] {
  return [...columns].sort((a, b) =>
    Number(b.is_default) - Number(a.is_default)
    || Number(b.is_active) - Number(a.is_active)
    || a.column_name.localeCompare(b.column_name));
}

/** How many active Statuses are paid through a column. */
export function activeStatusCount(column: Pick<StatusRateColumn, 'status_rate_column_id'>, keys: readonly StatusKey[]): number {
  return keys.filter((k) => k.is_active && k.status_rate_column_id === column.status_rate_column_id).length;
}

/**
 * Whether the column can be deactivated: it is not the Company default (make another the
 * default first) and no active Status is paid through it. The server enforces both.
 */
export function canDeactivateColumn(column: StatusRateColumn, keys: readonly StatusKey[]): boolean {
  return column.is_active && !column.is_default && activeStatusCount(column, keys) === 0;
}

/** The columns a Status can be bound to: the active ones, plus its current one if it is not. */
export function selectableColumns(
  columns: readonly StatusRateColumn[],
  currentColumnId: number | null,
): StatusRateColumn[] {
  return sortColumns(columns.filter((c) => c.is_active || c.status_rate_column_id === currentColumnId));
}

export function validateColumnName(name: string): string {
  const trimmed = name.trim();
  if (trimmed === '') return 'Column name is required.';
  if (trimmed.length > 200) return 'Column name must be at most 200 characters.';
  return '';
}
