import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  amountPlaceholder,
  amountRoundingNote,
  normalizedAmountNotices,
  sameDecimal,
} from '../src/lib/amountEntry.ts';
import { formatMoney } from '../src/lib/money.ts';

test('placeholder shows exactly the currency decimals', () => {
  assert.equal(amountPlaceholder(2), '0.00');
  assert.equal(amountPlaceholder(0), '0');
  assert.equal(amountPlaceholder(3), '0.000');
  assert.equal(amountPlaceholder(null), '0');
});

test('rounding note is neutral and names the currency decimals', () => {
  assert.equal(amountRoundingNote('USD', 2), 'Amounts are rounded to 2 decimal places (USD) when saved.');
  assert.equal(amountRoundingNote('JPY', 0), 'Amounts are rounded to whole units (JPY) when saved.');
  assert.equal(amountRoundingNote('XXX', 1), 'Amounts are rounded to 1 decimal place (XXX) when saved.');
  assert.equal(amountRoundingNote(null, 2), null);
  assert.equal(amountRoundingNote('USD', null), null);
});

test('decimal equality is exact and ignores insignificant zeros', () => {
  assert.equal(sameDecimal('12.3400', '12.34'), true);
  assert.equal(sameDecimal('012.340', '12.34'), true);
  assert.equal(sameDecimal('12.345', '12.34'), false);
  assert.equal(sameDecimal('0.10', '.1'), true);
  assert.equal(sameDecimal('1000', '1000.0000'), true);
  assert.equal(sameDecimal('123456789012345.6789', '123456789012345.6788'), false);
  assert.equal(sameDecimal('abc', 'abc'), false);
});

test('only amounts the server changed are reported, with the saved value formatted', () => {
  const format = (value: string) => formatMoney(value, 'USD', 2);
  assert.deepEqual(
    normalizedAmountNotices(['12.345', '10', '7.5'], ['12.3400', '10.0000', '7.5000'], format),
    ['12.345 was saved as $12.34.'],
  );
  assert.deepEqual(normalizedAmountNotices(['10.00'], ['10.0000'], format), []);
  assert.deepEqual(normalizedAmountNotices(['10.00', '5'], ['10.0000'], format), []);
});

test('amount inputs accept extra precision and show the currency decimals', () => {
  const bonusSource = readFileSync(resolve(import.meta.dirname, '../src/pages/payroll/BonusDialog.tsx'), 'utf8');
  const bonusAmountInput = bonusSource.match(/Amount\s*<input([\s\S]*?)value=\{row\.amount\}/)?.[1];
  assert.ok(bonusAmountInput, 'Bonus amount input should exist');
  assert.match(bonusAmountInput, /step="any"/);
  assert.doesNotMatch(bonusAmountInput, /step="0\.0001"|step="0\.01"/);
  assert.match(bonusAmountInput, /placeholder=\{amountPlaceholder\(user\?\.currency_minor_unit_digits\)\}/);
  assert.match(bonusSource, /amountRoundingNote\(user\?\.currency_code, user\?\.currency_minor_unit_digits\)/);
  assert.match(bonusSource, /normalizedAmountNotices\(/);

  const rulesSource = readFileSync(resolve(import.meta.dirname, '../src/pages/people/pay-rates/DriverPayRulesTab.tsx'), 'utf8');
  const ruleAmountInput = rulesSource.match(/type="number"([\s\S]*?)value=\{ruleForm\.amount\}/)?.[1];
  assert.ok(ruleAmountInput, 'DriverPayRule amount input should exist');
  assert.match(ruleAmountInput, /step="any"/);
  assert.doesNotMatch(ruleAmountInput, /step="0\.0001"|step="0\.01"/);
  assert.match(ruleAmountInput, /placeholder=\{`e\.g\. \$\{amountPlaceholder\(currencyDigits\)\}`\}/);
  assert.match(rulesSource, /amountRoundingNote\(currencyCode, currencyDigits\)/);
});

test('the money formatter never rounds below the stored precision', () => {
  const moneySource = readFileSync(resolve(import.meta.dirname, '../src/lib/money.ts'), 'utf8');
  assert.doesNotMatch(moneySource, /toFixed|Math\.round|Math\.floor|Math\.ceil|Math\.trunc/);
  assert.equal(formatMoney('12.34', 'USD', 2), '$12.34');
  assert.equal(formatMoney('12.3000', 'USD', 2), '$12.30');
  assert.equal(formatMoney('1500', 'JPY', 0), '¥1,500');
  assert.equal(formatMoney('12.345', 'KWD', 3).replace(/\s/g, ' '), 'KWD 12.345');
});

test('no money screen rounds or truncates an amount itself', () => {
  const files = [
    'lib/money.ts',
    'pages/payroll/BonusDialog.tsx',
    'pages/payroll/CalculationPreviewDialog.tsx',
    'pages/payroll/CurrentPayrollReportsDialog.tsx',
    'pages/payroll/FinalizationPreviewDialog.tsx',
    'pages/payroll/LedgerPage.tsx',
    'pages/payroll/PeriodsListPage.tsx',
    'pages/people/pay-rates/DriverPayRulesTab.tsx',
    'pages/people/pay-rates/payRatesView.ts',
    'pages/ReviewDetailDialog.tsx',
    'pages/payroll/finalizedLibrary/formatting.ts',
    'pages/payroll/finalizedLibrary/OverviewBody.tsx',
    'pages/payroll/finalizedLibrary/ReportBody.tsx',
    'pages/payroll/finalizedLibrary/RatesUsedBody.tsx',
  ];
  for (const file of files) {
    const source = readFileSync(resolve(import.meta.dirname, '../src', file), 'utf8');
    assert.doesNotMatch(source, /\.toFixed\(|Math\.(round|floor|ceil|trunc)\(/, file);
  }
});
