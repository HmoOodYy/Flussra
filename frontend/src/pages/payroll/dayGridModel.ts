/**
 * Day Grid presentation model.
 *
 * Ordinary columns are the period's frozen definitions. A column's identity is its
 * `payroll_period_definition_id`: value maps, dirty state and save payloads are keyed by
 * that id (as a string), never by a code or a name. Code and label are display metadata
 * and have no behavior here.
 */
import type {
  DayGridColumn,
  DayGridResponse,
  DayGridSaveRow,
  DayGridSummary,
} from '../../types/payroll/entry';

/** The stable key for a column in value maps and save payloads. */
export function columnKey(column: Pick<DayGridColumn, 'payroll_period_definition_id'>): string {
  return String(column.payroll_period_definition_id);
}

export interface QuantityInputProps {
  step: number | 'any';
  min: number;
}

/** Input constraints come from the frozen InputType. The server is authoritative. */
export function quantityInputProps(column: Pick<DayGridColumn, 'input_type'>): QuantityInputProps {
  return column.input_type === 'WholeNumber' ? { step: 1, min: 0 } : { step: 'any', min: 0 };
}

export interface QuantityTotalView {
  key: string;
  label: string;
  unit: string | null;
  quantity: string;
}

/**
 * Generic per-definition quantity totals, in column order. Which totals exist depends
 * only on the period's definitions, never on a code such as hours or miles.
 */
export function quantityTotalViews(
  columns: readonly DayGridColumn[],
  summary: Pick<DayGridSummary, 'quantity_totals'>,
): QuantityTotalView[] {
  const byId = new Map(
    summary.quantity_totals.map((total) => [total.payroll_period_definition_id, total.quantity]),
  );
  return columns.map((column) => ({
    key: columnKey(column),
    label: column.label,
    unit: column.unit,
    quantity: byId.get(column.payroll_period_definition_id) ?? '0',
  }));
}

/** Build a dirty row from the current grid, preserving its definition-keyed values. */
export function buildSaveRowFromGrid(grid: DayGridResponse | null, driverAssignmentId: number): DayGridSaveRow {
  const row = grid?.rows.find((r) => r.driver_assignment_id === driverAssignmentId);
  return {
    driver_assignment_id: driverAssignmentId,
    values: Object.fromEntries(
      Object.entries(row?.values ?? {}).map(([k, v]) => [k, v.quantity ?? '']),
    ),
    status_key: row?.status_key ?? null,
    notes: row?.notes ?? null,
  };
}