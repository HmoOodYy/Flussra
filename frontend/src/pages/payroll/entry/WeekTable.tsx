import type { DayGridColumn, DayGridRow, PeriodEntryOverviewDay } from '../../../types/payroll/entry';
import { dayChip, formatQuantity } from './dayEntryPageModel';
import { DriverIdentity } from './DriverIdentity';
import { rowOfDriver, weekCell, weekTotal } from './periodOverviewModel';
import styles from './PayrollEntryPage.module.css';

const DRIVER_WIDTH = 200;
const DAY_MIN_WIDTH = 72;
const TOTAL_WIDTH = 110;
const PAY_WIDTH = 110;

interface WeekTableProps {
  /** The dates of the week shown, in order. */
  weekDates: readonly string[];
  /** The overview days of those dates. */
  days: readonly PeriodEntryOverviewDay[];
  /** The drivers to list, already narrowed by the search. */
  drivers: ReadonlyArray<Pick<DayGridRow, 'driver_assignment_id' | 'driver_name' | 'driver_number'>>;
  /** The period column whose quantities the cells show. */
  column: DayGridColumn;
  /** Each driver's pay over the week; null when the period has no calculation preview. */
  weekPay: ReadonlyMap<number, string> | null;
  formatPay: (amount: string) => string;
  panelDriverId: number | null;
  onSelectDay: (date: string) => void;
  onOpenDriver: (driverAssignmentId: number) => void;
}

/** The read-only Week view: one row per driver, one column per day of the week. */
export function WeekTable({
  weekDates, days, drivers, column, weekPay, formatPay, panelDriverId, onSelectDay, onOpenDriver,
}: WeekTableProps) {
  const key = String(column.payroll_period_definition_id);
  const offDates = new Set(days.filter((day) => day.is_configured_off_day).map((day) => day.work_date));

  return (
    <div className={styles.tableWrap}>
      <table
        className={`${styles.table} ${styles.weekTable}`}
        style={{ minWidth: DRIVER_WIDTH + weekDates.length * DAY_MIN_WIDTH + TOTAL_WIDTH + PAY_WIDTH }}
      >
        <colgroup>
          <col style={{ width: DRIVER_WIDTH }} />
          {weekDates.map((date) => <col key={date} />)}
          <col style={{ width: TOTAL_WIDTH }} />
          <col style={{ width: PAY_WIDTH }} />
        </colgroup>
        <thead>
          <tr>
            <th scope="col" className={styles.thDriver}>Driver</th>
            {weekDates.map((date) => {
              const chip = dayChip(date);
              return (
                <th scope="col" key={date} className={`${styles.thNumber} ${offDates.has(date) ? styles.weekOff : ''}`}>
                  <button type="button" className={styles.weekDayBtn} onClick={() => onSelectDay(date)}>
                    {chip.weekday} {chip.dayOfMonth}
                  </button>
                </th>
              );
            })}
            <th scope="col" className={styles.thNumber}>
              Total
              {column.unit && <span className={styles.thSub}>{column.unit}</span>}
            </th>
            <th scope="col" className={styles.thNumber}>Week pay</th>
          </tr>
        </thead>
        <tbody>
          {drivers.length === 0 && (
            <tr>
              <td colSpan={weekDates.length + 3} className={styles.emptyRow}>No drivers match.</td>
            </tr>
          )}
          {drivers.map((driver) => {
            const id = driver.driver_assignment_id;
            const total = weekTotal(days, id, key);
            const pay = weekPay?.get(id);
            return (
              <tr key={id}>
                <td className={styles.tdDriver}>
                  <DriverIdentity
                    name={driver.driver_name}
                    number={driver.driver_number}
                    active={panelDriverId === id}
                    onOpen={() => onOpenDriver(id)}
                  />
                </td>
                {weekDates.map((date) => {
                  const day = days.find((d) => d.work_date === date);
                  const off = offDates.has(date);
                  const cell = weekCell(day ? rowOfDriver(day, id) : undefined, key, off);
                  const kindClass = cell.kind === 'status' ? styles.weekStatus : cell.kind === 'pending' ? styles.weekPending : '';
                  return (
                    <td
                      key={date}
                      className={`${styles.tdNumber} ${styles.weekCell} ${off ? styles.weekOff : ''} ${cell.needsReview ? styles.weekReview : ''}`}
                    >
                      <span className={kindClass}>{cell.text}</span>
                    </td>
                  );
                })}
                <td className={`${styles.tdNumber} ${styles.weekTotal}`}>{total === null ? '—' : formatQuantity(total)}</td>
                <td className={`${styles.tdNumber} ${styles.weekTotal}`}>{pay === undefined ? '—' : formatPay(pay)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
