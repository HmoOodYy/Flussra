/**
 * Generic, reusable calendar/ISO-date arithmetic (day/month/year strings <->
 * ISO YYYY-MM-DD, weekday-of-date, formatting).
 *
 * PURE MODULE: no React, no apiClient, no browser globals, and — this is
 * load-bearing — NO use of the JS `Date` object anywhere in this file (no
 * `new Date`, `Date.now`, `Date.UTC`, `Date.parse`). The browser clock must
 * never be read here. All arithmetic is pure string/integer calendar math
 * (days-in-month, leap years, Sakamoto's algorithm for weekday).
 *
 * This module computes CALENDAR facts only (is this a real date, what
 * weekday does it fall on, how many days are in this month). It never
 * computes payroll chronology — period boundaries and legal payroll dates
 * always come from the backend.
 */

// ── Calendar primitives ─────────────────────────────────────────────────────

export function isLeapYear(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

const DAYS_IN_MONTH: readonly number[] = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

/** month1to12 is 1-based (1 = January .. 12 = December). */
export function daysInMonth(year: number, month1to12: number): number {
  if (month1to12 === 2 && isLeapYear(year)) return 29;
  return DAYS_IN_MONTH[month1to12 - 1];
}

export const MONTH_SHORT: readonly string[] = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
];

/** Full month names, used only for the day-out-of-range message below (parseDateParts). */
const MONTH_FULL: readonly string[] = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
];

export const WEEKDAY_NAMES: readonly string[] = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
];

const MIN_YEAR = 1900;
const MAX_YEAR = 2999;

// ── Typed day/month/year parts ──────────────────────────────────────────────

export interface DateParts {
  day: string;
  month: string;
  year: string;
}

export type DatePartsResult =
  | { kind: 'empty' }
  | { kind: 'incomplete' }
  | { kind: 'invalid'; message: string }
  | { kind: 'valid'; iso: string };

const DIGITS_ONLY = /^\d+$/;

function pad2(value: number): string {
  return value < 10 ? `0${value}` : String(value);
}

/**
 * Parses raw typed day/month/year strings into a DatePartsResult. Day/month
 * may be typed with or without a leading zero. Never rewrites or reorders
 * what the caller passed — this is a pure classification.
 */
export function parseDateParts(parts: DateParts): DatePartsResult {
  const day = parts.day.trim();
  const month = parts.month.trim();
  const year = parts.year.trim();

  if (day === '' && month === '' && year === '') {
    return { kind: 'empty' };
  }

  for (const field of [day, month, year]) {
    if (field !== '' && !DIGITS_ONLY.test(field)) {
      return { kind: 'invalid', message: 'Use numbers only.' };
    }
  }

  if (day === '' || month === '' || year === '' || year.length < 4) {
    return { kind: 'incomplete' };
  }

  const monthNum = Number(month);
  if (monthNum < 1 || monthNum > 12) {
    return { kind: 'invalid', message: 'Month must be between 1 and 12.' };
  }

  const yearNum = Number(year);
  if (yearNum < MIN_YEAR || yearNum > MAX_YEAR) {
    return { kind: 'invalid', message: `Enter a year between ${MIN_YEAR} and ${MAX_YEAR}.` };
  }

  const dayNum = Number(day);
  const maxDay = daysInMonth(yearNum, monthNum);
  if (dayNum < 1 || dayNum > maxDay) {
    if (dayNum === 0 || dayNum > 31) {
      return { kind: 'invalid', message: 'Day must be between 1 and 31.' };
    }
    return {
      kind: 'invalid',
      message: `${MONTH_FULL[monthNum - 1]} ${yearNum} has ${maxDay} days.`,
    };
  }

  return { kind: 'valid', iso: `${String(yearNum).padStart(4, '0')}-${pad2(monthNum)}-${pad2(dayNum)}` };
}

const ISO_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

/** Valid ISO -> { day, month, year } (no leading zeros stripped from the ISO input itself). Anything else -> empty parts. */
export function isoToParts(iso: string): DateParts {
  if (!isValidIsoDate(iso)) return { day: '', month: '', year: '' };
  const match = ISO_PATTERN.exec(iso)!;
  return { year: match[1], month: match[2], day: match[3] };
}

/** Strict YYYY-MM-DD syntax AND a real calendar date. */
export function isValidIsoDate(iso: string): boolean {
  const match = ISO_PATTERN.exec(iso);
  if (!match) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < MIN_YEAR || year > MAX_YEAR) return false;
  if (month < 1 || month > 12) return false;
  if (day < 1 || day > daysInMonth(year, month)) return false;
  return true;
}

// ── Weekday (Sakamoto's algorithm — pure integer arithmetic) ────────────────

const SAKAMOTO_TABLE: readonly number[] = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];

/** 0 = Sunday … 6 = Saturday. Throws RangeError for an invalid ISO date. */
export function weekdayOfIso(iso: string): number {
  if (!isValidIsoDate(iso)) {
    throw new RangeError(`weekdayOfIso: not a valid ISO date: ${iso}`);
  }
  const match = ISO_PATTERN.exec(iso)!;
  let year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (month < 3) year -= 1;
  return (year + Math.floor(year / 4) - Math.floor(year / 100) + Math.floor(year / 400) + SAKAMOTO_TABLE[month - 1] + day) % 7;
}

// ── Formatting ───────────────────────────────────────────────────────────────

function partsOf(iso: string): { year: number; month: number; day: number } {
  const match = ISO_PATTERN.exec(iso)!;
  return { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) };
}

/** '2026-09-23' -> 'Sep 23'. Assumes a valid ISO date. */
export function formatIsoShort(iso: string): string {
  const { month, day } = partsOf(iso);
  return `${MONTH_SHORT[month - 1]} ${day}`;
}

/** '2026-09-23' -> 'Sep 23, 2026'. Assumes a valid ISO date. */
export function formatIsoLong(iso: string): string {
  const { year, month, day } = partsOf(iso);
  return `${MONTH_SHORT[month - 1]} ${day}, ${year}`;
}

/**
 * 'Sep 23 – Sep 29' when start/end share a year, otherwise
 * 'Dec 30, 2026 – Jan 5, 2027'. Uses an en dash surrounded by spaces.
 * Assumes valid ISO dates.
 */
export function formatIsoRange(start: string, end: string): string {
  const a = partsOf(start);
  const b = partsOf(end);
  if (a.year === b.year) {
    return `${formatIsoShort(start)} – ${formatIsoShort(end)}`;
  }
  return `${formatIsoLong(start)} – ${formatIsoLong(end)}`;
}

/** -1/0/1 by plain string comparison. Only meaningful for valid ISO strings. */
export function compareIso(a: string, b: string): -1 | 0 | 1 {
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

/** Deterministic date-only arithmetic for form translation. Never uses Date or the wall clock. */
export function addIsoDays(iso: string, amount: number): string {
  if (!isValidIsoDate(iso) || !Number.isInteger(amount)) {
    throw new RangeError(`addIsoDays: invalid input ${iso} / ${amount}`);
  }
  const parts = partsOf(iso);
  let year = parts.year;
  let month = parts.month;
  let day = parts.day;
  const direction = amount < 0 ? -1 : 1;
  let remaining = Math.abs(amount);
  while (remaining > 0) {
    day += direction;
    if (direction > 0 && day > daysInMonth(year, month)) {
      day = 1;
      month += 1;
      if (month > 12) {
        month = 1;
        year += 1;
      }
    } else if (direction < 0 && day < 1) {
      month -= 1;
      if (month < 1) {
        month = 12;
        year -= 1;
      }
      day = daysInMonth(year, month);
    }
    remaining -= 1;
  }
  const result = `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  if (!isValidIsoDate(result)) throw new RangeError(`addIsoDays: result out of range ${result}`);
  return result;
}

/** Inclusive calendar-day count used only to translate Custom form dates into an interval. */
export function inclusiveIsoDays(start: string, end: string): number | null {
  if (!isValidIsoDate(start) || !isValidIsoDate(end) || compareIso(end, start) < 0) return null;
  let cursor = start;
  let count = 1;
  while (cursor !== end) {
    cursor = addIsoDays(cursor, 1);
    count += 1;
  }
  return count;
}
