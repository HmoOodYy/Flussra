import { useId } from 'react';
import type { DayGridResponse, DayGridRow, DayGridSaveRow } from '../../types/payroll/entry';
import { columnKey, quantityInputProps, quantityTotalViews } from './dayGridModel';
import { cellWarning, formatQuantity, quantitiesLocked, rowIsOff } from './dayEntryPageModel';
import { DriverIdentity } from './DriverIdentity';
import { StatusCellInput } from './StatusCellInput';
import styles from './PayrollEntryPage.module.css';

interface DayEntryTableProps {
  grid: DayGridResponse;
  /** The rows to show: the grid's rows after search and filter. */
  rows: readonly DayGridRow[];
  dirtyRows: ReadonlyMap<number, DayGridSaveRow>;
  canEdit: boolean;
  /** The driver the side panel shows, if any. */
  panelDriverId: number | null;
  onOpenDriver: (driverAssignmentId: number) => void;
  /** Each driver's pay for the day; null when the period has no calculation preview. */
  dayPay: ReadonlyMap<number, string> | null;
  formatDayPay: (amount: string) => string;
  /** The day pay of the whole day, already formatted. */
  dayPayTotalText: string;
  getCellValue: (row: DayGridRow, key: string) => string;
  getStatusText: (row: DayGridRow) => string;
  getNotesValue: (row: DayGridRow) => string;
  onCellChange: (driverAssignmentId: number, key: string, value: string) => void;
  onStatusChange: (driverAssignmentId: number, statusText: string | null) => void;
  onNotesChange: (driverAssignmentId: number, notes: string) => void;
}

const DRIVER_WIDTH = 240;
const STATUS_WIDTH = 170;
const QUANTITY_WIDTH = 136;
const NOTE_MIN_WIDTH = 200;
const PAY_WIDTH = 140;

export function DayEntryTable({
  grid, rows, dirtyRows, canEdit, panelDriverId, onOpenDriver, dayPay, formatDayPay, dayPayTotalText,
  getCellValue, getStatusText, getNotesValue, onCellChange, onStatusChange, onNotesChange,
}: DayEntryTableProps) {
  const warningPrefix = useId();
  const columnCount = grid.columns.length + 4;
  // Fixed widths for every column but Note, which takes what is left.
  const minTableWidth = DRIVER_WIDTH + STATUS_WIDTH + grid.columns.length * QUANTITY_WIDTH + NOTE_MIN_WIDTH + PAY_WIDTH;

  return (
    <div className={styles.tableWrap}>
      <table className={styles.table} style={{ minWidth: minTableWidth }}>
        <colgroup>
          <col style={{ width: DRIVER_WIDTH }} />
          <col style={{ width: STATUS_WIDTH }} />
          {grid.columns.map((col) => <col key={columnKey(col)} style={{ width: QUANTITY_WIDTH }} />)}
          <col />
          <col style={{ width: PAY_WIDTH }} />
        </colgroup>
        <thead>
          <tr>
            <th scope="col" className={styles.thDriver}>Driver</th>
            <th scope="col" className={styles.thStatus}>Status</th>
            {grid.columns.map((col) => (
              <th scope="col" key={columnKey(col)} className={styles.thNumber}>
                {col.unit ? `${col.label} · ${col.unit}` : col.label}
              </th>
            ))}
            <th scope="col" className={styles.thNote}>Note</th>
            <th scope="col" className={styles.thNumber}>
              Day pay
              <span className={styles.thSub}>calculated</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td colSpan={columnCount} className={styles.emptyRow}>No drivers match.</td>
            </tr>
          )}
          {rows.map((row) => {
            const id = row.driver_assignment_id;
            const dirty = dirtyRows.get(id);
            const currentValues = Object.fromEntries(
              grid.columns.map((col) => [columnKey(col), getCellValue(row, columnKey(col))]),
            );
            const off = rowIsOff(row, dirty, grid.status_keys);
            const locked = quantitiesLocked(off, currentValues);
            const savedPay = dayPay?.get(id);
            return (
              <tr key={id} className={off ? styles.offRow : undefined}>
                <td className={styles.tdDriver}>
                  <DriverIdentity
                    name={row.driver_name}
                    number={row.driver_number}
                    active={panelDriverId === id}
                    onOpen={() => onOpenDriver(id)}
                  />
                </td>
                <td className={styles.tdStatus}>
                  <div className={`${styles.statusField} ${off ? styles.statusFieldOff : ''}`}>
                    <StatusCellInput
                      className={styles.statusInput}
                      shownText={getStatusText(row)}
                      statusKeys={grid.status_keys}
                      disabled={!canEdit}
                      ariaLabel={`Status for ${row.driver_name}`}
                      onCommit={(text) => onStatusChange(id, text)}
                    />
                  </div>
                </td>
                {grid.columns.map((col) => {
                  const key = columnKey(col);
                  const value = currentValues[key];
                  const saved = row.values[key]?.quantity ?? '';
                  // An untouched cell shows the saved quantity without trailing zeros; a typed one shows as typed.
                  const shown = value === saved ? formatQuantity(value) : value;
                  const edited = formatQuantity(value) !== formatQuantity(saved);
                  const warning = cellWarning(value, col.input_type, row.values[key]?.needs_manager_review ?? false);
                  const inputProps = quantityInputProps(col);
                  const warningId = `${warningPrefix}-${id}-${key}`;
                  const cellClass = warning ? styles.cellWarn : edited ? styles.cellEdited : undefined;
                  return (
                    <td key={key} className={styles.tdNumber}>
                      <input
                        className={`${styles.qtyInput} ${cellClass ?? ''}`}
                        type="text"
                        inputMode={inputProps.inputMode}
                        autoComplete="off"
                        value={shown}
                        disabled={!canEdit || locked}
                        title={locked ? 'Not available on a full-day-off Status' : undefined}
                        aria-label={`${col.label} for ${row.driver_name}`}
                        aria-invalid={warning ? true : undefined}
                        aria-describedby={warning ? warningId : undefined}
                        onChange={(e) => onCellChange(id, key, e.target.value)}
                      />
                      {warning && <span id={warningId} className={styles.cellWarningText}>{warning}</span>}
                    </td>
                  );
                })}
                <td className={styles.tdNote}>
                  <input
                    className={styles.noteInput}
                    type="text"
                    value={getNotesValue(row)}
                    placeholder={canEdit ? 'Add note' : undefined}
                    disabled={!canEdit}
                    aria-label={`Note for ${row.driver_name}`}
                    onChange={(e) => onNotesChange(id, e.target.value)}
                  />
                </td>
                <td className={styles.tdNumber}>
                  <span className={`${styles.dayPay} ${dirty ? styles.dayPayStale : ''}`}>
                    {savedPay === undefined ? '—' : formatDayPay(savedPay)}
                  </span>
                  {dirty && dayPay !== null && <span className={styles.dayPayNote}>updates after save</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row" className={styles.tfDriver}>Day total</th>
            <td />
            {quantityTotalViews(grid.columns, grid.summary).map((total) => (
              <td key={total.key} className={styles.tfNumber}>{formatQuantity(total.quantity)}</td>
            ))}
            <td />
            <td className={styles.tfNumber}>{dayPayTotalText}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
