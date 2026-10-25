/**
 * Day view presentation model: the period's dates and weeks, the lifecycle indicator, the
 * per-driver day pay read from the calculation preview, cell warnings, and row search/filters.
 *
 * Presentation only. Every amount shown comes from the backend; the day pay here is the exact
 * sum of preview line amounts for the selected date, never a recomputation from rates.
 */
import { resolveStatusInput } from '../../../lib/statusInput.ts';
import { addIsoDays, compareIso, isValidIsoDate, WEEKDAY_NAMES, weekdayOfIso } from '../../../lib/isoDate.ts';
import type { CalculationPreviewDriver } from '../../../types/payroll/calculation';
import type {
  DayGridColumn,
  DayGridRow,
  DayGridSaveRow,
  DayGridStatusKey,
} from '../../../types/payroll/entry';

// ---------------------------------------------------------------------------
// Period dates and weeks
// ---------------------------------------------------------------------------

const MAX_PERIOD_DAYS = 366;

/** Every date from start to end inclusive; empty when either is invalid or end precedes start. */
export function listPeriodDates(start: string, end: string): string[] {
  if (!isValidIsoDate(start) || !isValidIsoDate(end) || compareIso(end, start) < 0) return [];
  const dates = [start];
  while (dates[dates.length - 1] !== end && dates.length < MAX_PERIOD_DAYS) {
    dates.push(addIsoDays(dates[dates.length - 1], 1));
  }
  return dates;
}

/** Consecutive weeks of `size` dates, counted from the first date. */
export function groupIntoWeeks(dates: readonly string[], size = 7): string[][] {
  const weeks: string[][] = [];
  for (let i = 0; i < dates.length; i += size) weeks.push(dates.slice(i, i + size));
  return weeks;
}

/** The index of the week holding `date`, or 0 when it is in none. */
export function weekIndexOf(weeks: readonly (readonly string[])[], date: string | null): number {
  if (date === null) return 0;
  const index = weeks.findIndex((week) => week.includes(date));
  return index < 0 ? 0 : index;
}

interface DayChip {
  date: string;
  weekday: string;
  dayOfMonth: number;
}

/** '2026-09-23' -> Wed 23. Assumes a valid ISO date. */
export function dayChip(date: string): DayChip {
  return {
    date,
    weekday: WEEKDAY_NAMES[weekdayOfIso(date)].slice(0, 3),
    dayOfMonth: Number(date.slice(8, 10)),
  };
}

/** The period id of the route, or null when it is not a positive whole number. */
export function parsePeriodId(raw: string | undefined): number | null {
  if (raw === undefined || !/^[1-9]\d*$/.test(raw)) return null;
  const id = Number(raw);
  return Number.isSafeInteger(id) ? id : null;
}

// ---------------------------------------------------------------------------
// Lifecycle indicator
// ---------------------------------------------------------------------------

interface LifecycleStage {
  key: 'entry' | 'review' | 'approved' | 'finalized';
  label: string;
  state: 'done' | 'current' | 'upcoming';
}

const ENTRY_STAGE_LABEL: Record<string, string> = {
  Draft: 'Prepared',
  Open: 'Open',
  Returned: 'Returned for correction',
};

const STAGE_INDEX: Record<string, number> = {
  Draft: 0, Open: 0, Returned: 0, InReview: 1, Approved: 2, Locked: 3, Archived: 3,
};

/** The four lifecycle stages with the one the period is in; null for a status outside the flow. */
export function periodLifecycle(status: string): LifecycleStage[] | null {
  const current = STAGE_INDEX[status];
  if (current === undefined) return null;
  const labels: Array<[LifecycleStage['key'], string]> = [
    ['entry', ENTRY_STAGE_LABEL[status] ?? 'Open'],
    ['review', 'In review'],
    ['approved', 'Approved'],
    ['finalized', 'Finalized'],
  ];
  return labels.map(([key, label], index) => ({
    key,
    label,
    state: index < current ? 'done' : index === current ? 'current' : 'upcoming',
  }));
}

// ---------------------------------------------------------------------------
// Day pay from the calculation preview
// ---------------------------------------------------------------------------

const DECIMAL_STRING = /^([+-]?)(\d+)(?:\.(\d+))?$/;

function formatScaled(total: bigint, scale: number): string {
  const negative = total < 0n;
  const digits = (negative ? -total : total).toString().padStart(scale + 1, '0');
  const whole = digits.slice(0, digits.length - scale);
  const fraction = digits.slice(digits.length - scale);
  return `${negative ? '-' : ''}${whole}${scale > 0 ? `.${fraction}` : ''}`;
}

/**
 * The exact sum of decimal strings, kept to the longest fraction among them. Null and
 * malformed values are ignored; null when nothing is left to add.
 */
export function sumDecimalStrings(values: readonly (string | null | undefined)[]): string | null {
  const parsed: Array<{ negative: boolean; whole: string; fraction: string }> = [];
  let scale = 0;
  for (const value of values) {
    if (value == null) continue;
    const match = DECIMAL_STRING.exec(value.trim());
    if (!match) continue;
    const fraction = match[3] ?? '';
    parsed.push({ negative: match[1] === '-', whole: match[2], fraction });
    scale = Math.max(scale, fraction.length);
  }
  if (parsed.length === 0) return null;
  let total = 0n;
  for (const { negative, whole, fraction } of parsed) {
    const scaled = BigInt(whole + fraction.padEnd(scale, '0'));
    total += negative ? -scaled : scaled;
  }
  return formatScaled(total, scale);
}

/**
 * Each driver's pay for one date: the exact sum of the amounts of the preview lines dated that
 * day. Lines without a date (period-level adjustments) never count. A driver with no amount
 * for the date has no entry.
 */
export function dayPayByDriver(
  drivers: readonly Pick<CalculationPreviewDriver, 'lines'>[],
  workDate: string,
): Map<number, string> {
  const amounts = new Map<number, Array<string | null>>();
  for (const driver of drivers) {
    for (const line of driver.lines) {
      if (line.work_date !== workDate) continue;
      const list = amounts.get(line.driver_assignment_id) ?? [];
      list.push(line.calculated_amount);
      amounts.set(line.driver_assignment_id, list);
    }
  }
  const result = new Map<number, string>();
  for (const [driverAssignmentId, list] of amounts) {
    const sum = sumDecimalStrings(list);
    if (sum !== null) result.set(driverAssignmentId, sum);
  }
  return result;
}

/** The small line under the Day pay card: where the figure comes from, or why there is none. */
export function dayPayNote(state: {
  periodStatus: string;
  loading: boolean;
  failed: boolean;
  hasAmount: boolean;
}): string {
  if (state.hasAmount) return 'calculated by Flussra';
  if (state.periodStatus === 'Draft') return 'shown once the period is Open';
  if (state.periodStatus !== 'Open' && state.periodStatus !== 'Returned') return 'not available for this status';
  if (state.loading) return 'calculating…';
  if (state.failed) return 'not available right now';
  return 'nothing paid yet';
}

// ---------------------------------------------------------------------------
// Quantity display
// ---------------------------------------------------------------------------

const PLAIN_DECIMAL = /^([+-]?)(\d+)(?:\.(\d*))?$/;

/**
 * A server quantity without insignificant trailing zeros ("119.0000" -> "119", "12.5000" ->
 * "12.5", "0.0000" -> "0"). Pure string work, so no digit is ever rounded; text that is not a
 * plain decimal is returned as is.
 */
export function formatQuantity(value: string): string {
  const match = PLAIN_DECIMAL.exec(value.trim());
  if (!match) return value;
  const fraction = (match[3] ?? '').replace(/0+$/, '');
  const isZero = /^0*$/.test(match[2]) && fraction === '';
  const sign = match[1] === '-' && !isZero ? '-' : '';
  return `${sign}${match[2]}${fraction === '' ? '' : `.${fraction}`}`;
}

// ---------------------------------------------------------------------------
// Cell warnings
// ---------------------------------------------------------------------------

const WARNING_NO_APPROVED_RATE = 'No approved rate';
const WARNING_NUMBERS_ONLY = 'Numbers only';
const WARNING_NOT_NEGATIVE = "Can't be negative";
const WARNING_WHOLE_NUMBERS = 'Whole numbers only';

const NUMERIC_TEXT = /^[+-]?(\d+\.?\d*|\.\d+)$/;

/** A problem with the typed quantity itself; blank is fine. The server stays the authority. */
export function clientCellWarning(value: string, inputType: DayGridColumn['input_type']): string | null {
  const text = value.trim();
  if (text === '') return null;
  if (!NUMERIC_TEXT.test(text)) return WARNING_NUMBERS_ONLY;
  if (Number(text) < 0) return WARNING_NOT_NEGATIVE;
  if (inputType === 'WholeNumber' && !Number.isInteger(Number(text))) return WARNING_WHOLE_NUMBERS;
  return null;
}

/** The small text under a quantity cell: a problem with the typed value, else the server's flag. */
export function cellWarning(
  value: string,
  inputType: DayGridColumn['input_type'],
  needsManagerReview: boolean,
): string | null {
  return clientCellWarning(value, inputType) ?? (needsManagerReview ? WARNING_NO_APPROVED_RATE : null);
}

// ---------------------------------------------------------------------------
// Off-reason Status rows
// ---------------------------------------------------------------------------

/**
 * Whether the row's Status, as currently shown, is a full-day-off one. An untouched row (or one
 * whose held Status is unchanged) is what the server says; typed text is read through the catalog.
 */
export function rowIsOff(
  row: Pick<DayGridRow, 'is_off' | 'status_key'>,
  dirty: Pick<DayGridSaveRow, 'status_key_id' | 'status_key'> | undefined,
  statusKeys: readonly DayGridStatusKey[],
): boolean {
  if (dirty === undefined || dirty.status_key_id !== null || dirty.status_key === row.status_key) {
    return row.is_off;
  }
  const resolution = resolveStatusInput(dirty.status_key, statusKeys);
  if (resolution.kind !== 'match') return false;
  return statusKeys.find((key) => key.status_key_id === resolution.entry.status_key_id)?.is_off_reason ?? false;
}

export function isBlankOrZero(value: string): boolean {
  const text = value.trim();
  return text === '' || (NUMERIC_TEXT.test(text) && Number(text) === 0);
}

/**
 * The server refuses ordinary work on a full-day-off Status day (it must be cleared or the Status
 * removed), so those quantity inputs are closed while empty. Work already typed stays editable so
 * it can be cleared.
 */
export function quantitiesLocked(isOff: boolean, values: Readonly<Record<string, string>>): boolean {
  return isOff && Object.values(values).every(isBlankOrZero);
}

// ---------------------------------------------------------------------------
// Search and filters
// ---------------------------------------------------------------------------

export type RowFilter = 'all' | 'attention' | 'notEntered' | 'unsaved';

/** A driver row as currently shown, with its unsaved edits applied. */
export interface EntryRowState {
  row: Pick<DayGridRow, 'driver_name' | 'driver_number' | 'values'>;
  dirty: boolean;
  /** The Status text the cell shows. */
  statusText: string;
  /** The quantity text the cells show, by column key. */
  values: Readonly<Record<string, string>>;
}

export function matchesDriverSearch(
  row: Pick<DayGridRow, 'driver_name' | 'driver_number'>,
  query: string,
): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === '') return true;
  return row.driver_name.toLowerCase().includes(needle)
    || (row.driver_number ?? '').toLowerCase().includes(needle);
}

/** Any cell the server flagged for review, or holding a value that cannot be saved. */
function rowNeedsAttention(
  state: Pick<EntryRowState, 'row' | 'values'>,
  columns: readonly Pick<DayGridColumn, 'payroll_period_definition_id' | 'input_type'>[],
): boolean {
  return columns.some((column) => {
    const key = String(column.payroll_period_definition_id);
    return (state.row.values[key]?.needs_manager_review ?? false)
      || clientCellWarning(state.values[key] ?? '', column.input_type) !== null;
  });
}

/** No Status and no quantity entered. */
function rowNotEntered(state: Pick<EntryRowState, 'statusText' | 'values'>): boolean {
  return state.statusText.trim() === ''
    && Object.values(state.values).every((value) => value.trim() === '');
}

export function filterEntryRows<T extends EntryRowState>(
  states: readonly T[],
  columns: readonly Pick<DayGridColumn, 'payroll_period_definition_id' | 'input_type'>[],
  filter: RowFilter,
  query: string,
): T[] {
  return states.filter((state) => {
    if (!matchesDriverSearch(state.row, query)) return false;
    if (filter === 'attention') return rowNeedsAttention(state, columns);
    if (filter === 'notEntered') return rowNotEntered(state);
    if (filter === 'unsaved') return state.dirty;
    return true;
  });
}

// ---------------------------------------------------------------------------
// Small labels
// ---------------------------------------------------------------------------

/** Up to two capital letters from the first and last name parts. */
export function driverInitials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return '?';
  const letters = parts.length === 1 ? [parts[0]] : [parts[0], parts[parts.length - 1]];
  return letters.map((part) => Array.from(part)[0]).join('').toUpperCase();
}

export function unsavedChangesLabel(count: number): string {
  return `Unsaved changes for ${count} ${count === 1 ? 'driver' : 'drivers'}`;
}
