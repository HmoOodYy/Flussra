/**
 * Typed-in monetary amounts (Bonus, Minimum and Maximum pay).
 *
 * The server rounds a typed-in amount to the Company currency's decimals when it is
 * saved and returns the normalized value; this module never rounds. It only builds the
 * neutral input hint and tells when the saved value differs from what was typed, so the
 * screen can show the result of the server's rounding.
 */

/** Input placeholder with exactly the currency's decimals (text only, no arithmetic). */
export function amountPlaceholder(minorUnitDigits: number | null | undefined): string {
  const digits = Number.isInteger(minorUnitDigits) ? Math.min(4, Math.max(0, minorUnitDigits as number)) : 0;
  return digits === 0 ? '0' : `0.${'0'.repeat(digits)}`;
}

/** The short, neutral rounding note shown beside an amount input. */
export function amountRoundingNote(
  currencyCode: string | null | undefined,
  minorUnitDigits: number | null | undefined,
): string | null {
  if (!currencyCode || !Number.isInteger(minorUnitDigits)) return null;
  const digits = minorUnitDigits as number;
  const places = digits === 0 ? 'whole units' : `${digits} decimal place${digits === 1 ? '' : 's'}`;
  return `Amounts are rounded to ${places} (${currencyCode}) when saved.`;
}

/** A decimal string in canonical form (no sign for zero, no insignificant zeros). */
function canonicalDecimal(value: string): string | null {
  const match = /^([+-]?)(\d*)(?:\.(\d*))?$/.exec(value.trim());
  if (!match || (match[2] === '' && (match[3] ?? '') === '')) return null;
  const integer = match[2].replace(/^0+(?=\d)/, '') || '0';
  const fraction = (match[3] ?? '').replace(/0+$/, '');
  const isZero = integer === '0' && fraction === '';
  return `${match[1] === '-' && !isZero ? '-' : ''}${integer}${fraction ? `.${fraction}` : ''}`;
}

/** True when two decimal strings are numerically equal (exact, no floating point). */
export function sameDecimal(a: string, b: string): boolean {
  const left = canonicalDecimal(a);
  const right = canonicalDecimal(b);
  return left !== null && left === right;
}

/**
 * For each typed amount whose saved (server-normalized) value differs, a line such as
 * "12.345 was saved as $12.34". Equal amounts produce nothing.
 */
export function normalizedAmountNotices(
  typed: readonly string[],
  saved: readonly string[],
  format: (value: string) => string,
): string[] {
  const notices: string[] = [];
  typed.forEach((value, index) => {
    const result = saved[index];
    if (result !== undefined && !sameDecimal(value, result)) {
      notices.push(`${value.trim()} was saved as ${format(result)}.`);
    }
  });
  return notices;
}
