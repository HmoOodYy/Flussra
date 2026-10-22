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

test('Bonus and DriverPayRule amount inputs accept extra precision that the server rounds on entry', () => {
  const bonusSource = readFileSync(resolve(import.meta.dirname, '../src/pages/payroll/BonusDialog.tsx'), 'utf8');
  const bonusAmountInput = bonusSource.match(/Amount\s*<input([\s\S]*?)value=\{row\.amount\}/)?.[1];
  assert.ok(bonusAmountInput, 'Bonus amount input should exist');
  assert.match(bonusAmountInput, /step="any"/);
  assert.doesNotMatch(bonusAmountInput, /step="0\.0001"|step="0\.01"|min="0\.01"/);
  assert.match(bonusSource, /formatMoney\(v, user\?\.currency_code, user\?\.currency_minor_unit_digits\)/);

  const payRatesSource = readFileSync(resolve(import.meta.dirname, '../src/pages/people/pay-rates/DriverPayRulesTab.tsx'), 'utf8');
  const driverPayRuleAmountInput = payRatesSource.match(/type="number"([\s\S]*?)value=\{ruleForm\.amount\}/)?.[1];
  assert.ok(driverPayRuleAmountInput, 'DriverPayRule amount input should exist');
  assert.match(driverPayRuleAmountInput, /step="any"/);
  assert.doesNotMatch(driverPayRuleAmountInput, /step="0\.0001"|step="0\.01"|min="0\.01"/);
});

test('company currency settings use the server catalog and update auth currency after a successful save', () => {
  const section = readFileSync(resolve(import.meta.dirname, '../src/pages/settings/company-branches/CompanyProfileSection.tsx'), 'utf8');
  const hook = readFileSync(resolve(import.meta.dirname, '../src/pages/settings/company-branches/useCompanyProfile.ts'), 'utf8');
  const api = readFileSync(resolve(import.meta.dirname, '../src/lib/settingsApi.ts'), 'utf8');
  assert.match(api, /apiClient\.get<SupportedCurrency\[]>\('\/settings\/currencies'\)/);
  assert.match(hook, /getSupportedCurrencies\(\)/);
  assert.match(hook, /getSupportedCurrencies\(\)[\s\S]*?\.then\(\(\{ data \}\) => setCurrencies\(data\)\)/);
  assert.match(readFileSync(resolve(import.meta.dirname, '../src/pages/settings/company-branches/CompanyBranchesPage.tsx'), 'utf8'), /useCompanyProfile\(\{ user, setUser, setSuccessInfo, setConfirmKind \}\)/);
  assert.match(section, /currencies\.map\(\(currency\)/);
  assert.match(section, /value=\{coForm\.currency_code \?\? company\.currency_code \?\? ''\}/);
  assert.match(section, /!company\.currency_code/);
  assert.match(section, /currency_change_locked/);

  const saveBody = hook.match(/async function saveCo\(\) \{([\s\S]*?)\n\x20{2}\}/)?.[1];
  assert.ok(saveBody, 'company save handler should exist');
  assert.match(saveBody, /await updateCompanyProfile\(coForm\)/);
  assert.match(api, /apiClient\.patch<CompanyProfile>\('\/settings\/company', payload\)/);
  assert.match(saveBody, /setUser\(\{[\s\S]*?\.\.\.user,[\s\S]*?currency_code:\s*data\.currency_code,[\s\S]*?currency_minor_unit_digits:\s*data\.currency_minor_unit_digits,[\s\S]*?\}\)/);
  assert.match(saveBody, /catch \(e\) \{ setCoSaveErr\(apiError\(e\)\); \}/);
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
    '../src/pages/people/pay-rates/payRatesView.ts',
    '../src/pages/people/pay-rates/driverPayRulesApi.ts',
    '../src/pages/people/pay-rates/usePayRateDriverContext.ts',
    '../src/pages/people/pay-rates/usePayRateReads.ts',
    '../src/pages/people/pay-rates/useTargetRateEditing.ts',
    '../src/pages/people/pay-rates/useRateScheduleActions.ts',
    '../src/pages/people/pay-rates/useDriverPayRules.ts',
    '../src/pages/people/pay-rates/PayRateDriverList.tsx',
    '../src/pages/people/pay-rates/TargetRateSection.tsx',
    '../src/pages/people/pay-rates/PendingRateChanges.tsx',
    '../src/pages/people/pay-rates/RateHistoryTab.tsx',
    '../src/pages/people/pay-rates/DriverPayRulesTab.tsx',
    '../src/pages/payroll/BonusDialog.tsx',
    '../src/pages/payroll/CalculationPreviewDialog.tsx',
    '../src/pages/payroll/CurrentPayrollReportsDialog.tsx',
    '../src/pages/payroll/FinalizationPreviewDialog.tsx',
    '../src/pages/payroll/FinalSummaryDialog.tsx',
    '../src/pages/payroll/FinalizedPayrollLibraryDialog.tsx',
    '../src/pages/payroll/finalizedLibrary/OverviewBody.tsx',
    '../src/pages/payroll/finalizedLibrary/ReportBody.tsx',
    '../src/pages/payroll/finalizedLibrary/OffStatusBody.tsx',
    '../src/pages/payroll/finalizedLibrary/RatesUsedBody.tsx',
    '../src/pages/payroll/finalizedLibrary/AuditBody.tsx',
    '../src/pages/payroll/finalizedLibrary/formatting.ts',
    '../src/pages/payroll/finalizedLibrary/EvidencePresentation.tsx',
    '../src/pages/payroll/LedgerPage.tsx',
    '../src/pages/payroll/PeriodsListPage.tsx',
    '../src/pages/payroll/grossDisplay.ts',
    '../src/pages/payroll/PeriodDetailPage.tsx',
    '../src/pages/payroll/useDailyEntryController.ts',
  ];
  const payRatesFeatureFiles = [
    '../src/pages/people/pay-rates/PayRatesPage.tsx',
    '../src/pages/people/pay-rates/payRatesView.ts',
    '../src/pages/people/pay-rates/driverPayRulesApi.ts',
    '../src/pages/people/pay-rates/usePayRateDriverContext.ts',
    '../src/pages/people/pay-rates/usePayRateReads.ts',
    '../src/pages/people/pay-rates/useTargetRateEditing.ts',
    '../src/pages/people/pay-rates/useRateScheduleActions.ts',
    '../src/pages/people/pay-rates/useDriverPayRules.ts',
    '../src/pages/people/pay-rates/PayRateDriverList.tsx',
    '../src/pages/people/pay-rates/TargetRateSection.tsx',
    '../src/pages/people/pay-rates/PendingRateChanges.tsx',
    '../src/pages/people/pay-rates/RateHistoryTab.tsx',
    '../src/pages/people/pay-rates/DriverPayRulesTab.tsx',
  ];
  for (const file of [...files.filter((file) => !file.includes('/people/pay-rates/')), ...payRatesFeatureFiles]) {
    const source = readFileSync(resolve(import.meta.dirname, file), 'utf8');
    assert.doesNotMatch(source, /currency\s*:\s*['"]USD['"]/i, file);
    assert.doesNotMatch(source, /toFixed\(2\)/, file);
    assert.doesNotMatch(source, /Amount\s*\(\$\)|\$\/period/, file);
  }
});
