/**
 * Whole-period presentation model: the day strip's progress, the Week view's cells and totals,
 * and the driver panel's week bars, days worked, pay parts and rates.
 *
 * Presentation only, over saved data. Quantities and money are added as exact decimal strings,
 * and everything is keyed by the period definition id and the driver assignment id, never by a
 * code, a name or a kind of pay.
 */
import type { CalculationPreviewDriver } from '../../types/payroll/calculation';
import type { DayGridColumn, DayGridRow, PeriodEntryOverviewDay } from '../../types/payroll/entry';
import { dayPayByDriver, formatQuantity, isBlankOrZero, sumDecimalStrings } from './dayEntryPageModel.ts';

export type EntryView = 'day' | 'week';

// ---------------------------------------------------------------------------
// Saved row facts
// ---------------------------------------------------------------------------

/** The Status text of a saved row, or null when the day holds none. */
export function rowStatusLabel(row: Pick<DayGridRow, 'status_key_id' | 'status_key' | 'status_label'>): string | null {
  const label = (row.status_label ?? '').trim();
  if (label !== '') return label;
  const key = (row.status_key ?? '').trim();
  if (key !== '') return key;
  return row.status_key_id !== null ? 'Status' : null;
}

function savedQuantity(row: Pick<DayGridRow, 'values'> | undefined, key: string): string {
  return row?.values[key]?.quantity ?? '';
}

/** Whether any column of the saved row holds a quantity, blank not counting. */
function rowHasAnyQuantityText(row: Pick<DayGridRow, 'values'>): boolean {
  return Object.values(row.values).some((value) => (value.quantity ?? '').trim() !== '');
}

/** A Status, or any quantity text: the row has been entered for the day. */
export function rowIsEntered(row: Pick<DayGridRow, 'status_key_id' | 'status_key' | 'status_label' | 'values'>): boolean {
  return rowStatusLabel(row) !== null || rowHasAnyQuantityText(row);
}

// ---------------------------------------------------------------------------
// Day strip progress
// ---------------------------------------------------------------------------

export interface DayProgress {
  /** The one line under the day. */
  text: string;
  tone: 'none' | 'off' | 'notStarted' | 'partial' | 'complete' | 'check';
  barTone: 'blue' | 'green' | 'amber';
  /** The share of the day's drivers entered, 0 to 1. */
  ratio: number;
  /** A normal day off of the period's schedule, whatever has been entered on it. */
  configuredOff: boolean;
}

export function dayProgress(
  day: Pick<PeriodEntryOverviewDay, 'is_configured_off_day' | 'rows'> & { summary: { needs_attention: number } },
): DayProgress {
  const total = day.rows.length;
  const done = day.rows.filter(rowIsEntered).length;
  const attention = day.summary.needs_attention;
  const configuredOff = day.is_configured_off_day;
  const ratio = total === 0 ? 0 : done / total;
  const barTone = attention > 0 ? 'amber' : done === total && total > 0 ? 'green' : 'blue';
  if (total === 0) return { text: 'No drivers', tone: 'none', barTone, ratio, configuredOff };
  if (done === 0) {
    return configuredOff
      ? { text: 'Normal day off', tone: 'off', barTone, ratio, configuredOff }
      : { text: 'Not started', tone: 'notStarted', barTone, ratio, configuredOff };
  }
  if (done === total) {
    return attention > 0
      ? { text: `${attention} to check`, tone: 'check', barTone, ratio, configuredOff }
      : { text: 'Complete', tone: 'complete', barTone, ratio, configuredOff };
  }
  return { text: `${done} of ${total} entered`, tone: 'partial', barTone, ratio, configuredOff };
}

// ---------------------------------------------------------------------------
// Week view
// ---------------------------------------------------------------------------

/** The overview days of the given dates, in order; a date the overview lacks is skipped. */
export function daysOfWeek(
  days: readonly PeriodEntryOverviewDay[],
  weekDates: readonly string[],
): PeriodEntryOverviewDay[] {
  const byDate = new Map(days.map((day) => [day.work_date, day]));
  return weekDates.flatMap((date) => byDate.get(date) ?? []);
}

/** Every driver of the given days once, in the order first met. */
export function driversOfDays(
  days: readonly Pick<PeriodEntryOverviewDay, 'rows'>[],
): Array<Pick<DayGridRow, 'driver_assignment_id' | 'driver_name' | 'driver_number'>> {
  const seen = new Map<number, Pick<DayGridRow, 'driver_assignment_id' | 'driver_name' | 'driver_number'>>();
  for (const day of days) {
    for (const row of day.rows) {
      if (!seen.has(row.driver_assignment_id)) {
        seen.set(row.driver_assignment_id, {
          driver_assignment_id: row.driver_assignment_id,
          driver_name: row.driver_name,
          driver_number: row.driver_number,
        });
      }
    }
  }
  return [...seen.values()];
}

export function rowOfDriver(
  day: Pick<PeriodEntryOverviewDay, 'rows'>,
  driverAssignmentId: number,
): DayGridRow | undefined {
  return day.rows.find((row) => row.driver_assignment_id === driverAssignmentId);
}

export interface WeekCell {
  text: string;
  kind: 'status' | 'quantity' | 'pending' | 'blank';
  /** The server flagged this value for review (no approved rate). */
  needsReview: boolean;
}

/**
 * One Week view cell for a driver on a day: the Status label when the day holds one, else the
 * saved quantity of the shown column, else a dot for a working day with nothing yet (blank on a
 * normal day off).
 */
export function weekCell(
  row: Pick<DayGridRow, 'status_key_id' | 'status_key' | 'status_label' | 'values'> | undefined,
  columnKey: string,
  configuredOff: boolean,
): WeekCell {
  const needsReview = row?.values[columnKey]?.needs_manager_review ?? false;
  const status = row ? rowStatusLabel(row) : null;
  if (status !== null) return { text: status, kind: 'status', needsReview };
  const quantity = savedQuantity(row, columnKey);
  if (quantity.trim() !== '') return { text: formatQuantity(quantity), kind: 'quantity', needsReview };
  return configuredOff
    ? { text: '', kind: 'blank', needsReview }
    : { text: '·', kind: 'pending', needsReview };
}

/** The exact sum of one column's saved quantities for a driver over the given days. */
export function weekTotal(
  days: readonly Pick<PeriodEntryOverviewDay, 'rows'>[],
  driverAssignmentId: number,
  columnKey: string,
): string | null {
  return sumDecimalStrings(days.map((day) => {
    const quantity = savedQuantity(rowOfDriver(day, driverAssignmentId), columnKey);
    return quantity.trim() === '' ? null : quantity;
  }));
}

/** Each driver's pay over the given dates: the exact sum of their daily pay from the preview. */
export function weekPayByDriver(
  drivers: readonly Pick<CalculationPreviewDriver, 'lines'>[],
  dates: readonly string[],
): Map<number, string> {
  const amounts = new Map<number, string[]>();
  for (const date of dates) {
    for (const [driverAssignmentId, amount] of dayPayByDriver(drivers, date)) {
      const list = amounts.get(driverAssignmentId) ?? [];
      list.push(amount);
      amounts.set(driverAssignmentId, list);
    }
  }
  const result = new Map<number, string>();
  for (const [driverAssignmentId, list] of amounts) {
    const sum = sumDecimalStrings(list);
    if (sum !== null) result.set(driverAssignmentId, sum);
  }
  return result;
}

// ---------------------------------------------------------------------------
// Driver panel
// ---------------------------------------------------------------------------

export interface WeekBar {
  date: string;
  kind: 'quantity' | 'status' | 'notEntered' | 'dayOff';
  label: string;
  /** The bar length against the week's largest quantity, 0 to 1; 0 unless a quantity. */
  ratio: number;
}

/** A driver's week for one column: per day the quantity, the Status, or why there is none. */
export function driverWeekBars(
  days: readonly Pick<PeriodEntryOverviewDay, 'work_date' | 'is_configured_off_day' | 'rows'>[],
  driverAssignmentId: number,
  columnKey: string,
): WeekBar[] {
  const cells = days.map((day) => {
    const row = rowOfDriver(day, driverAssignmentId);
    const status = row ? rowStatusLabel(row) : null;
    const quantity = savedQuantity(row, columnKey).trim();
    return { day, status, quantity };
  });
  // Bar lengths are drawing only; the numbers shown stay the saved decimal text.
  const largest = Math.max(0, ...cells.map(({ status, quantity }) => (status === null && quantity !== '' ? Number(quantity) || 0 : 0)));
  return cells.map(({ day, status, quantity }): WeekBar => {
    if (status !== null) return { date: day.work_date, kind: 'status', label: status, ratio: 0 };
    if (quantity !== '') {
      const ratio = largest > 0 ? Math.min(1, Math.max(0, (Number(quantity) || 0) / largest)) : 0;
      return { date: day.work_date, kind: 'quantity', label: formatQuantity(quantity), ratio };
    }
    return day.is_configured_off_day
      ? { date: day.work_date, kind: 'dayOff', label: 'Day off', ratio: 0 }
      : { date: day.work_date, kind: 'notEntered', label: 'Not entered', ratio: 0 };
  });
}

/** Days with some quantity and no full-day-off Status, among the given days. */
export function daysWorked(
  days: readonly Pick<PeriodEntryOverviewDay, 'rows'>[],
  driverAssignmentId: number,
): number {
  return days.filter((day) => {
    const row = rowOfDriver(day, driverAssignmentId);
    if (!row || row.is_off) return false;
    return Object.values(row.values).some((value) => !isBlankOrZero(value.quantity ?? ''));
  }).length;
}

export interface PayPart {
  key: 'daily' | 'status' | 'bonus' | 'minimum' | 'maximum';
  label: string;
  amount: string;
}

/** The parts of a driver's preview pay that are not zero, in a fixed order. */
export function payParts(
  driver: Pick<CalculationPreviewDriver, 'daily_pay' | 'status_pay' | 'bonus_total' | 'minimum_adjustment' | 'maximum_adjustment'>,
): PayPart[] {
  const parts: PayPart[] = [
    { key: 'daily', label: 'Daily pay', amount: driver.daily_pay },
    { key: 'status', label: 'Status pay', amount: driver.status_pay },
    { key: 'bonus', label: 'Bonus', amount: driver.bonus_total },
    { key: 'minimum', label: 'Minimum adjustment', amount: driver.minimum_adjustment },
    { key: 'maximum', label: 'Maximum adjustment', amount: driver.maximum_adjustment },
  ];
  return parts.filter((part) => {
    const text = part.amount.trim();
    return /^[+-]?\d+(\.\d+)?$/.test(text) && /[1-9]/.test(text);
  });
}

export interface ColumnRates {
  key: string;
  label: string;
  unit: string | null;
  /** The distinct rates the driver's lines of this column resolved to, in the order met. */
  rates: string[];
  /** Some line of this column has no approved rate. */
  needsReview: boolean;
  /** The driver has no line of this column yet. */
  noEntries: boolean;
}

/** The rates a driver was paid at, per period column, from the preview lines of that column. */
export function ratesByColumn(
  driver: Pick<CalculationPreviewDriver, 'lines'> | undefined,
  columns: readonly Pick<DayGridColumn, 'payroll_period_definition_id' | 'label' | 'unit'>[],
): ColumnRates[] {
  return columns.map((column) => {
    const lines = (driver?.lines ?? []).filter(
      (line) => line.payroll_period_definition_id === column.payroll_period_definition_id,
    );
    const rates: string[] = [];
    for (const line of lines) {
      if (line.resolved_rate !== null && !rates.includes(line.resolved_rate)) rates.push(line.resolved_rate);
    }
    return {
      key: String(column.payroll_period_definition_id),
      label: column.label,
      unit: column.unit,
      rates,
      needsReview: lines.some((line) => line.needs_manager_review),
      noEntries: lines.length === 0,
    };
  });
}
