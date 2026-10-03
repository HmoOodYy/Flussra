import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { formatMoney, formatRate } from '../src/lib/money.ts';

test('formats standard, zero, three, and four minor-unit currencies', () => {
  assert.equal(formatMoney('12.34', 'USD', 2), '$12.34');
  assert.equal(formatMoney('12.3456', 'JPY', 0), '¥12.3456');
  assert.equal(formatMoney('12.3456', 'KWD', 3), 'KWD 12.3456');
  assert.equal(formatMoney('12.3456', 'CLF', 4), 'CLF 12.3456');
});

test('unconfigured values stay neutral and preserve stored four-decimal precision', () => {
  assert.equal(formatMoney('12.3456', null, null), '12.3456');
  assert.equal(formatMoney('12.3456', 'USD', 2), '$12.3456');
  assert.equal(formatMoney('123456789012345.6789', 'USD', 2), '$123,456,789,012,345.6789');
});

test('rate formatting retains precision and uses the frozen response currency', () => {
  const authCurrency = 'USD';
  const packet = { currency_code: 'KWD', currency_minor_unit_digits: 3, rate: '3.1234' };
  assert.equal(formatRate(packet.rate, packet.currency_code, packet.currency_minor_unit_digits), 'KWD 3.1234');
  assert.notEqual(formatRate(packet.rate, authCurrency, 2), formatRate(packet.rate, packet.currency_code, packet.currency_minor_unit_digits));
});

test('company currency settings use the server catalog and update auth currency after a successful save', () => {
  const source = readFileSync(resolve(import.meta.dirname, '../src/pages/settings/company-branches/CompanyBranchesPage.tsx'), 'utf8');
  assert.match(source, /get<SupportedCurrency\[]>\('\/settings\/currencies'\)/);
  assert.match(source, /currencies\.map\(\(currency\)/);
  assert.match(source, /value=\{coForm\.currency_code \?\? company\.currency_code \?\? ''\}/);
  assert.match(source, /!company\.currency_code/);
  assert.match(source, /currency_change_locked/);

  const saveBody = source.match(/async function saveCo\(\) \{([\s\S]*?)\r?\n[ \t]{2}\}\r?\n\r?\n[ \t]{2}async function setDefault/)?.[1];
  assert.ok(saveBody, 'company save handler should exist');
  assert.match(saveBody, /await apiClient\.patch<CompanyProfile>\('\/settings\/company', coForm\)/);
  assert.match(saveBody, /setUser\(\{[\s\S]*?\.\.\.user,[\s\S]*?currency_code:\s*data\.currency_code,[\s\S]*?currency_minor_unit_digits:\s*data\.currency_minor_unit_digits,[\s\S]*?\}\)/);
  assert.doesNotMatch(saveBody, /auth\/me/);
});

test('Ledger finalized gross denomination comes from frozen finalized currency', () => {
  const source = readFileSync(resolve(import.meta.dirname, '../src/pages/payroll/LedgerPage.tsx'), 'utf8');
  assert.match(source, /const displayCurrencyCode = p\.finalized \? p\.finalized\.currency_code : user\?\.currency_code/);
  assert.match(source, /const displayMinorUnitDigits = p\.finalized \? p\.finalized\.currency_minor_unit_digits : user\?\.currency_minor_unit_digits/);
  assert.match(source, /formatCurrencyMoney\(p\.operational\.final_gross, displayCurrencyCode, displayMinorUnitDigits\)/);
  assert.doesNotMatch(source, /formatCurrencyMoney\(p\.operational\.final_gross, user\?\.currency_code, user\?\.currency_minor_unit_digits\)/);
});

test('financial screens have no known USD-specific formatting assumptions', () => {
  const files = [
    '../src/pages/ReviewDetailDialog.tsx',
    '../src/pages/people/pay-rates/PayRatesPage.tsx',
    '../src/pages/payroll/BonusDialog.tsx',
    '../src/pages/payroll/CalculationPreviewDialog.tsx',
    '../src/pages/payroll/CurrentPayrollReportsDialog.tsx',
    '../src/pages/payroll/FinalizationPreviewDialog.tsx',
    '../src/pages/payroll/FinalSummaryDialog.tsx',
    '../src/pages/payroll/FinalizedPayrollLibraryDialog.tsx',
    '../src/pages/payroll/LedgerPage.tsx',
    '../src/pages/payroll/PeriodsListPage.tsx',
    '../src/pages/payroll/grossDisplay.ts',
    '../src/pages/payroll/PeriodDetailPage.tsx',
  ];
  for (const file of files) {
    const source = readFileSync(resolve(import.meta.dirname, file), 'utf8');
    assert.doesNotMatch(source, /currency\s*:\s*['"]USD['"]/i, file);
    assert.doesNotMatch(source, /toFixed\(2\)/, file);
    assert.doesNotMatch(source, /Amount\s*\(\$\)|\$\/period/, file);
  }
});
