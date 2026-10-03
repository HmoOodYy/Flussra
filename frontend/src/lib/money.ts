export type MoneyValue = number | string | null | undefined;

function numericValue(value: MoneyValue): number | string | null {
  if (value == null || value === '') return null;
  const numeric = typeof value === 'number' ? value : value.trim();
  return numeric !== '' && Number.isFinite(Number(numeric)) ? numeric : null;
}

function precision(minorUnitDigits: number | null | undefined): { minimumFractionDigits: number; maximumFractionDigits: number } {
  const minimumFractionDigits = Number.isInteger(minorUnitDigits)
    ? Math.min(4, Math.max(0, minorUnitDigits as number))
    : 0;
  return { minimumFractionDigits, maximumFractionDigits: Math.max(minimumFractionDigits, 4) };
}

/** Format a displayed amount without changing or rounding the stored value. */
export function formatMoney(
  value: MoneyValue,
  currencyCode?: string | null,
  minorUnitDigits?: number | null,
): string {
  const numeric = numericValue(value);
  if (numeric == null) return value == null || value === '' ? '—' : String(value);
  const digits = precision(minorUnitDigits);
  // Intl accepts a decimal string directly. Keep backend NUMERIC(18,4) values
  // as strings so large amounts do not lose their last fractional digits.
  const format = (options: Intl.NumberFormatOptions) =>
    new Intl.NumberFormat(undefined, options).format(numeric as number);
  if (!currencyCode) {
    return format({ maximumFractionDigits: 4 });
  }
  return format({ style: 'currency', currency: currencyCode, ...digits });
}

/** Rates retain up to four stored decimal places; units should be appended separately. */
export function formatRate(
  value: MoneyValue,
  currencyCode?: string | null,
  minorUnitDigits?: number | null,
): string {
  return formatMoney(value, currencyCode, minorUnitDigits);
}
