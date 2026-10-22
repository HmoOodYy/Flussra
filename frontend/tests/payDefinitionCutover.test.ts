import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildPayDefinitionPayload, buildPayDefinitionRequestPayload, ordinalTopologyError, validatePayMethod } from '../src/pages/settings/pay-items/payItemsModel.ts';
import { buildCompleteScheduleValues, hasCompleteValues } from '../src/pages/people/pay-rates/payRatesTiers.ts';

const read = (path: string) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const payItemsFeatureFiles = [
  'src/pages/settings/pay-items/PayItemsPage.tsx',
  'src/pages/settings/pay-items/payItemsModel.ts',
  'src/pages/settings/pay-items/usePayItemBranchContext.ts',
  'src/pages/settings/pay-items/useCompanyPayDefinitions.ts',
  'src/pages/settings/pay-items/usePayItemApplicability.ts',
  'src/pages/settings/pay-items/usePayItemConfiguration.ts',
  'src/pages/settings/pay-items/usePayDefinitionRequests.ts',
  'src/pages/settings/pay-items/usePayItemCreation.ts',
  'src/pages/settings/pay-items/PayItemCatalog.tsx',
  'src/pages/settings/pay-items/PayDefinitionRequestsPanel.tsx',
  'src/pages/settings/pay-items/BranchConfigurationDialogs.tsx',
  'src/pages/settings/pay-items/PayItemCreationDialog.tsx',
  'src/pages/settings/pay-items/requestDraftWorkflow.ts',
];
const payItemsSources = payItemsFeatureFiles.map(read);
const payItemsFeatureSource = payItemsSources.join('\n');
const payItemsSource = (name: string) => read(`src/pages/settings/pay-items/${name}`);
const payRatesFeatureFiles = [
  'src/pages/people/pay-rates/PayRatesPage.tsx',
  'src/pages/people/pay-rates/payRatesView.ts',
  'src/pages/people/pay-rates/driverPayRulesApi.ts',
  'src/pages/people/pay-rates/usePayRateDriverContext.ts',
  'src/pages/people/pay-rates/usePayRateReads.ts',
  'src/pages/people/pay-rates/useTargetRateEditing.ts',
  'src/pages/people/pay-rates/useRateScheduleActions.ts',
  'src/pages/people/pay-rates/useDriverPayRules.ts',
  'src/pages/people/pay-rates/PayRateDriverList.tsx',
  'src/pages/people/pay-rates/TargetRateSection.tsx',
  'src/pages/people/pay-rates/PendingRateChanges.tsx',
  'src/pages/people/pay-rates/RateHistoryTab.tsx',
  'src/pages/people/pay-rates/DriverPayRulesTab.tsx',
  'src/pages/people/pay-rates/payRatesDrivers.ts',
  'src/pages/people/pay-rates/payRatesErrors.ts',
  'src/pages/people/pay-rates/payRulesView.ts',
];
const payRatesSources = payRatesFeatureFiles.map(read);
const payRatesFeatureSource = payRatesSources.join('\n');
const payRatesSource = (name: string) => read('src/pages/people/pay-rates/' + name);
const payRatesTargetHook = payRatesSource('useTargetRateEditing.ts');
const compensationApi = read('src/lib/compensationApi.ts');

test('Pay Items implementation keeps all definition, request, and branch APIs behind current lib owners', () => {
  assert.doesNotMatch(payItemsFeatureSource, /\/settings\/cdpi/);
  assert.doesNotMatch(payItemsFeatureSource, /\/settings\/pay-items/);
  assert.doesNotMatch(payItemsFeatureSource, /\/settings\/branches\/[^'"`]*pay-items/);
  assert.doesNotMatch(payItemsFeatureSource, /cdpiApi|Cdpi/);
  assert.doesNotMatch(payItemsFeatureSource, /\bapiClient\.(?:get|post|put|patch|delete)(?:<[^>]*>)?\s*\(/);

  assert.match(payItemsSource('usePayItemBranchContext.ts'), /getSettingsBranches\(\)/);
  assert.ok(payItemsSource('usePayItemBranchContext.ts').includes("from '../../../lib/settingsApi'"));
  for (const owner of ['useCompanyPayDefinitions.ts', 'usePayItemApplicability.ts', 'usePayItemConfiguration.ts', 'usePayDefinitionRequests.ts', 'usePayItemCreation.ts']) {
    assert.ok(payItemsSource(owner).includes("from '../../../lib/compensationApi'"), owner);
  }
  assert.match(payItemsSource('useCompanyPayDefinitions.ts'), /listPayDefinitions\(\)/);
  assert.match(payItemsSource('useCompanyPayDefinitions.ts'), /retirePayDefinition\(selectedId\)/);
  assert.match(payItemsSource('usePayItemApplicability.ts'), /listBranchPayDefinitions\(selectedBranchId\)/);
  assert.match(payItemsSource('usePayItemApplicability.ts'), /listBranchPayDefinitionHistory\(selectedBranchId, selectedId\)/);
  assert.match(payItemsSource('usePayItemConfiguration.ts'), /updateBranchPayDefinition\(selectedBranchId, selectedId, payload\)/);
  assert.match(payItemsSource('usePayItemConfiguration.ts'), /bulkUpdateBranchPayDefinition\(selectedId/);
  assert.match(payItemsSource('usePayItemCreation.ts'), /createPayDefinition\(fields\)/);
  assert.match(payItemsSource('usePayItemCreation.ts'), /createPayDefinitionRequest\(/);
  assert.match(payItemsSource('usePayDefinitionRequests.ts'), /listPayDefinitionRequests\(/);
  assert.match(payItemsSource('usePayDefinitionRequests.ts'), /decidePayDefinitionRequest\(/);
  assert.match(payItemsSource('usePayDefinitionRequests.ts'), /copyPayDefinitionRequest\(request\.request_id\)/);
  assert.match(payItemsSource('usePayDefinitionRequests.ts'), /listPayDefinitionRequestEvents\(request\.request_id\)/);
});

test('Pay Items feature modules expose no legacy display-order or PayItem identity', () => {
  assert.doesNotMatch(payItemsFeatureSource, /pay_item_id|sort_order|PayItemOrder|dragHandle/);
});

test('Pay Rates authors ordinary rates through Driver rate schedules and exposes no copy flow', () => {
  assert.match(payRatesTargetHook, /createSchedule/);
  assert.match(payRatesTargetHook, /replaceScheduleValues/);
  assert.ok(payRatesTargetHook.includes("from '../../../lib/compensationApi'"));
  assert.doesNotMatch(payRatesFeatureSource, /copy-from/);
  assert.doesNotMatch(payRatesFeatureSource, /pay_item_id/);
  assert.doesNotMatch(payRatesFeatureSource, /\/payroll\/driver-assignments\/\$\{[^}]+\}\/rates\/(pending|history|summary)/);
});

test('Status pay rates are Driver rate schedules of a Status rate column, with no Status-only rate path', () => {
  const page = payRatesSource('PayRatesPage.tsx');
  assert.match(page, /r\.kind === 'StatusRateColumn'/);
  assert.doesNotMatch(payRatesFeatureSource, /rate-matrix|rates\/batch|saveStatusRateBatch|approveStatusRate|voidStatusRate/);
  assert.doesNotMatch(payRatesFeatureSource, /rate_type_id|driver_rate_id/);
  // Scalar Status rows still produce a complete one-component schedule payload.
  assert.deepEqual(buildCompleteScheduleValues([
    { rate_component_definition_id: 31, sequence_no: 1, ordinal_from: null, ordinal_to: null },
  ], { 31: '25.00' }), [{ rate_component_definition_id: 31, amount: '25.00' }]);
});

test('compensationApi talks only to the /compensation namespace', () => {
  const paths = [...compensationApi.matchAll(/['"`](\/[a-z][^'"`]*)['"`]/g)].map((m) => m[1]);
  assert.ok(paths.length > 0);
  for (const path of paths) {
    assert.ok(path.startsWith('/compensation/'), `unexpected API path ${path}`);
  }
});

function functionBody(source: string, signature: RegExp): string {
  const start = source.search(signature);
  assert.ok(start >= 0, `missing ${signature}`);
  const open = source.indexOf('{', source.indexOf(')', start));
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(open, i + 1);
    }
  }
  throw new Error('unbalanced function body');
}

test('Pay Rates saves target rates one PayDefinition row at a time with no multi-row action', () => {
  assert.doesNotMatch(payRatesFeatureSource, /Save & Approve \$\{/);
  assert.doesNotMatch(payRatesFeatureSource, /dirtyCount|saveRates\(|enterEditMode|dirtyKeys/);
  const body = functionBody(payRatesTargetHook, /async function saveTargetRow\(/);
  assert.doesNotMatch(body, /\bfor\s*\(|\.forEach\(|\.map\(|Promise\.all/);
  assert.equal((body.match(/createSchedule\(/g) ?? []).length, 1);
  assert.doesNotMatch(body, /rates\/batch/);
  const targetUI = payRatesSource('TargetRateSection.tsx');
  assert.match(targetUI, /Save as Pending/);
  assert.match(targetUI, /Save & Approve/);
  assert.match(targetUI, /cancelTargetEdit/);
});

test('A failed approval keeps the saved Pending change visible and recoverable', () => {
  const body = functionBody(payRatesTargetHook, /async function saveTargetRow\(/);
  assert.match(body, /saved as Pending, but approval failed/);
  assert.match(body, /refreshTargetRows\(driverId\)/);
  assert.match(body, /Pending Changes/);
});

test('Status rates have no independent batch action and never touch assignments', () => {
  assert.throws(() => payRatesSource('useStatusRateEditing.ts'));
  assert.throws(() => payRatesSource('statusRatesApi.ts'));
  assert.ok(payRatesSource('usePayRateReads.ts').includes("import { getDriverPayRules"));
  assert.ok(payRatesSource('useDriverPayRules.ts').includes("from './driverPayRulesApi'"));
  assert.ok(payRatesSource('driverPayRulesApi.ts').includes("/payroll/drivers/${driverId}/pay-rules"));
  assert.ok(payRatesSource('driverPayRulesApi.ts').includes("'/payroll/driver-pay-rules'"));
});

const VALID_TIERS = [
  { ordinal_from: 1, ordinal_to: 2 },
  { ordinal_from: 3, ordinal_to: 5 },
  { ordinal_from: 6, ordinal_to: null },
];

test('Per Unit direct creation does not carry a tier topology', () => {
  assert.deepEqual(buildPayDefinitionPayload({
    definition_name: 'Mileage', input_type: 'Decimal', unit: 'mile',
    calculation_method: 'PerUnit', ordinal_tiers: VALID_TIERS,
  }), {
    definition_name: 'Mileage', input_type: 'Decimal', unit: 'mile', calculation_method: 'PerUnit',
  });
});

test('Ordinal direct creation carries WholeNumber input and ordered tier bounds', () => {
  assert.deepEqual(buildPayDefinitionPayload({
    definition_name: 'Loads', input_type: 'WholeNumber', unit: 'load',
    calculation_method: 'OrdinalTier', ordinal_tiers: VALID_TIERS,
  }), {
    definition_name: 'Loads', input_type: 'WholeNumber', unit: 'load',
    calculation_method: 'OrdinalTier', ordinal_tiers: VALID_TIERS,
  });
});

test('Ordinal branch request carries the branch, method, input, topology, and notes', () => {
  const definition = buildPayDefinitionPayload({
    definition_name: 'Loads', input_type: 'WholeNumber', unit: 'load',
    calculation_method: 'OrdinalTier', ordinal_tiers: VALID_TIERS,
  });
  assert.deepEqual(buildPayDefinitionRequestPayload(42, definition, 'Route loads'), {
    requesting_branch_id: 42, ...definition, notes: 'Route loads',
  });
});

test('Ordinal topology rejects decimal, gaps, overlaps, non-positive bounds, and missing final open tier', () => {
  assert.equal(validatePayMethod('OrdinalTier', 'WholeNumber', VALID_TIERS), null);
  assert.match(validatePayMethod('OrdinalTier', 'Decimal', VALID_TIERS) ?? '', /whole-number/);
  assert.match(ordinalTopologyError([{ ordinal_from: 1, ordinal_to: 2 }, { ordinal_from: 4, ordinal_to: null }]) ?? '', /continuous/);
  assert.match(ordinalTopologyError([{ ordinal_from: 1, ordinal_to: 3 }, { ordinal_from: 3, ordinal_to: null }]) ?? '', /continuous/);
  assert.match(ordinalTopologyError([{ ordinal_from: 0, ordinal_to: null }]) ?? '', /positive/);
  assert.match(ordinalTopologyError([{ ordinal_from: 1, ordinal_to: 2 }]) ?? '', /final tier/);
  assert.match(ordinalTopologyError([{ ordinal_from: 1, ordinal_to: null }, { ordinal_from: 2, ordinal_to: null }]) ?? '', /open-ended/);
});

test('Tiered Driver schedule writes every component atomically and treats zero as present', () => {
  const components = [
    { rate_component_definition_id: 20, sequence_no: 2, ordinal_from: 3, ordinal_to: null },
    { rate_component_definition_id: 10, sequence_no: 1, ordinal_from: 1, ordinal_to: 2 },
  ];
  assert.equal(buildCompleteScheduleValues(components, { 10: '0', 20: '' }), null);
  assert.equal(hasCompleteValues([
    { rate_component_definition_id: 10, sequence_no: 1, ordinal_from: 1, ordinal_to: 2, amount: '0' },
    { rate_component_definition_id: 20, sequence_no: 2, ordinal_from: 3, ordinal_to: null, amount: null },
  ]), false);
  assert.deepEqual(buildCompleteScheduleValues(components, { 10: '0', 20: '5.25' }), [
    { rate_component_definition_id: 10, amount: '0' },
    { rate_component_definition_id: 20, amount: '5.25' },
  ]);
});
