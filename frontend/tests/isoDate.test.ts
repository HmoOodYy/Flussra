import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  isLeapYear,
  daysInMonth,
  parseDateParts,
  isoToParts,
  isValidIsoDate,
  weekdayOfIso,
  MONTH_SHORT,
  WEEKDAY_NAMES,
  formatIsoShort,
  formatIsoLong,
  formatIsoRange,
  compareIso,
  addIsoDays,
  inclusiveIsoDays,
} from '../src/lib/isoDate.ts';

// ── isLeapYear ───────────────────────────────────────────────────────────────

test('isLeapYear: 1900 is not a leap year (divisible by 100, not 400)', () => {
  assert.equal(isLeapYear(1900), false);
});

test('isLeapYear: 2000 is a leap year (divisible by 400)', () => {
  assert.equal(isLeapYear(2000), true);
});

test('isLeapYear: 2024 is a leap year (divisible by 4, not 100)', () => {
  assert.equal(isLeapYear(2024), true);
});

test('isLeapYear: 2027 is not a leap year', () => {
  assert.equal(isLeapYear(2027), false);
});

// ── daysInMonth ──────────────────────────────────────────────────────────────

test('daysInMonth: February is 29 in a leap year, 28 otherwise', () => {
  assert.equal(daysInMonth(2024, 2), 29);
  assert.equal(daysInMonth(2027, 2), 28);
});

test('daysInMonth: April has 30 days, January has 31', () => {
  assert.equal(daysInMonth(2026, 4), 30);
  assert.equal(daysInMonth(2026, 1), 31);
});

// ── parseDateParts ───────────────────────────────────────────────────────────

test('parseDateParts: all blank -> empty', () => {
  assert.deepEqual(parseDateParts({ day: '', month: '', year: '' }), { kind: 'empty' });
});

test('parseDateParts: any blank field -> incomplete', () => {
  assert.deepEqual(parseDateParts({ day: '5', month: '', year: '2026' }), { kind: 'incomplete' });
  assert.deepEqual(parseDateParts({ day: '', month: '9', year: '2026' }), { kind: 'incomplete' });
  assert.deepEqual(parseDateParts({ day: '5', month: '9', year: '' }), { kind: 'incomplete' });
});

test('parseDateParts: year shorter than 4 digits -> incomplete', () => {
  assert.deepEqual(parseDateParts({ day: '5', month: '9', year: '202' }), { kind: 'incomplete' });
});

test('parseDateParts: non-digit characters -> invalid "Use numbers only."', () => {
  assert.deepEqual(parseDateParts({ day: '5a', month: '9', year: '2026' }), {
    kind: 'invalid',
    message: 'Use numbers only.',
  });
  assert.deepEqual(parseDateParts({ day: '5', month: '9', year: '20x6' }), {
    kind: 'invalid',
    message: 'Use numbers only.',
  });
});

test('parseDateParts: month 13 -> invalid "Month must be between 1 and 12."', () => {
  assert.deepEqual(parseDateParts({ day: '5', month: '13', year: '2026' }), {
    kind: 'invalid',
    message: 'Month must be between 1 and 12.',
  });
});

test('parseDateParts: month 0 -> invalid "Month must be between 1 and 12."', () => {
  assert.deepEqual(parseDateParts({ day: '5', month: '0', year: '2026' }), {
    kind: 'invalid',
    message: 'Month must be between 1 and 12.',
  });
});

test('parseDateParts: Feb 29 in a non-leap year -> invalid, message names the month and year', () => {
  assert.deepEqual(parseDateParts({ day: '29', month: '2', year: '2027' }), {
    kind: 'invalid',
    message: 'February 2027 has 28 days.',
  });
});

test('parseDateParts: Feb 29 in a leap year -> valid', () => {
  assert.deepEqual(parseDateParts({ day: '29', month: '2', year: '2024' }), {
    kind: 'valid',
    iso: '2024-02-29',
  });
});

test('parseDateParts: day 31 in April -> invalid, message names the month', () => {
  assert.deepEqual(parseDateParts({ day: '31', month: '4', year: '2026' }), {
    kind: 'invalid',
    message: 'April 2026 has 30 days.',
  });
});

test('parseDateParts: day 0 -> invalid "Day must be between 1 and 31."', () => {
  assert.deepEqual(parseDateParts({ day: '0', month: '5', year: '2026' }), {
    kind: 'invalid',
    message: 'Day must be between 1 and 31.',
  });
});

test('parseDateParts: day 32 -> invalid "Day must be between 1 and 31."', () => {
  assert.deepEqual(parseDateParts({ day: '32', month: '5', year: '2026' }), {
    kind: 'invalid',
    message: 'Day must be between 1 and 31.',
  });
});

test('parseDateParts: single-digit day and month are padded in the resulting ISO', () => {
  assert.deepEqual(parseDateParts({ day: '5', month: '9', year: '2026' }), {
    kind: 'valid',
    iso: '2026-09-05',
  });
});

test('parseDateParts: leading zeros accepted for day/month', () => {
  assert.deepEqual(parseDateParts({ day: '05', month: '09', year: '2026' }), {
    kind: 'valid',
    iso: '2026-09-05',
  });
});

test('parseDateParts: year below 1900 -> invalid year-range message', () => {
  assert.deepEqual(parseDateParts({ day: '5', month: '9', year: '1899' }), {
    kind: 'invalid',
    message: 'Enter a year between 1900 and 2999.',
  });
});

test('parseDateParts: year above 2999 -> invalid year-range message', () => {
  assert.deepEqual(parseDateParts({ day: '5', month: '9', year: '3000' }), {
    kind: 'invalid',
    message: 'Enter a year between 1900 and 2999.',
  });
});

test('parseDateParts: year boundaries 1900 and 2999 are accepted', () => {
  assert.equal(parseDateParts({ day: '1', month: '1', year: '1900' }).kind, 'valid');
  assert.equal(parseDateParts({ day: '1', month: '1', year: '2999' }).kind, 'valid');
});

// ── isoToParts ───────────────────────────────────────────────────────────────

test('isoToParts: valid ISO round-trips to zero-padded day/month/year', () => {
  assert.deepEqual(isoToParts('2026-09-05'), { day: '05', month: '09', year: '2026' });
});

test('isoToParts: invalid or malformed input -> empty parts', () => {
  assert.deepEqual(isoToParts(''), { day: '', month: '', year: '' });
  assert.deepEqual(isoToParts('not-a-date'), { day: '', month: '', year: '' });
  assert.deepEqual(isoToParts('2026-13-01'), { day: '', month: '', year: '' });
  assert.deepEqual(isoToParts('2026-02-30'), { day: '', month: '', year: '' });
});

// ── isValidIsoDate ───────────────────────────────────────────────────────────

test('isValidIsoDate: strict YYYY-MM-DD syntax and a real calendar date', () => {
  assert.equal(isValidIsoDate('2026-09-23'), true);
  assert.equal(isValidIsoDate('2024-02-29'), true);
  assert.equal(isValidIsoDate('2027-02-29'), false);
  assert.equal(isValidIsoDate('2026-9-23'), false);
  assert.equal(isValidIsoDate('09/23/2026'), false);
  assert.equal(isValidIsoDate(''), false);
});

// ── weekdayOfIso (Sakamoto's algorithm) ──────────────────────────────────────

test('weekdayOfIso: 2026-09-23 is a Wednesday', () => {
  assert.equal(weekdayOfIso('2026-09-23'), 3);
  assert.equal(WEEKDAY_NAMES[weekdayOfIso('2026-09-23')], 'Wednesday');
});

test('weekdayOfIso: 2000-01-01 is a Saturday', () => {
  assert.equal(weekdayOfIso('2000-01-01'), 6);
  assert.equal(WEEKDAY_NAMES[weekdayOfIso('2000-01-01')], 'Saturday');
});

test('weekdayOfIso: 2024-02-29 is a Thursday', () => {
  assert.equal(weekdayOfIso('2024-02-29'), 4);
  assert.equal(WEEKDAY_NAMES[weekdayOfIso('2024-02-29')], 'Thursday');
});

test('weekdayOfIso: throws RangeError for an invalid ISO date', () => {
  assert.throws(() => weekdayOfIso('2027-02-29'), RangeError);
  assert.throws(() => weekdayOfIso('not-a-date'), RangeError);
});

// ── formatIsoShort / formatIsoLong / formatIsoRange ─────────────────────────

test('formatIsoShort: "2026-09-23" -> "Sep 23"', () => {
  assert.equal(formatIsoShort('2026-09-23'), 'Sep 23');
});

test('formatIsoLong: "2026-09-23" -> "Sep 23, 2026"', () => {
  assert.equal(formatIsoLong('2026-09-23'), 'Sep 23, 2026');
});

test('formatIsoRange: same year -> "Sep 23 – Sep 29"', () => {
  assert.equal(formatIsoRange('2026-09-23', '2026-09-29'), 'Sep 23 – Sep 29');
});

test('formatIsoRange: crossing years -> long form on both ends', () => {
  assert.equal(formatIsoRange('2026-12-30', '2027-01-05'), 'Dec 30, 2026 – Jan 5, 2027');
});

test('MONTH_SHORT has 12 entries starting with Jan and ending with Dec', () => {
  assert.equal(MONTH_SHORT.length, 12);
  assert.equal(MONTH_SHORT[0], 'Jan');
  assert.equal(MONTH_SHORT[11], 'Dec');
});

// ── compareIso ───────────────────────────────────────────────────────────────

test('compareIso: -1/0/1 by string comparison', () => {
  assert.equal(compareIso('2026-01-01', '2026-02-01'), -1);
  assert.equal(compareIso('2026-02-01', '2026-01-01'), 1);
  assert.equal(compareIso('2026-01-01', '2026-01-01'), 0);
});

test('addIsoDays and inclusiveIsoDays use date-only calendar arithmetic', () => {
  assert.equal(addIsoDays('2026-01-31', 1), '2026-02-01');
  assert.equal(addIsoDays('2024-02-28', 1), '2024-02-29');
  assert.equal(addIsoDays('2026-01-04', 13), '2026-01-17');
  assert.equal(inclusiveIsoDays('2026-01-04', '2026-01-17'), 14);
  assert.equal(inclusiveIsoDays('2026-01-17', '2026-01-04'), null);
});

// ── Static check: no `Date` usage anywhere in this module ──────────────────

test('static: src/lib/isoDate.ts never uses the JS Date object', () => {
  const source = readFileSync(new URL('../src/lib/isoDate.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /\bnew Date\(/);
  assert.doesNotMatch(source, /\bDate\.(now|UTC|parse)\(/);
  assert.doesNotMatch(source, /\bDate\(/);
});
