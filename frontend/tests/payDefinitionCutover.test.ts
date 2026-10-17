import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

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
const payRatesPage = read('src/pages/people/pay-rates/PayRatesPage.tsx');
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

test('Pay Rates authors ordinary rates through target assignments and exposes no copy flow', () => {
  assert.match(payRatesPage, /createAssignment/);
  assert.match(payRatesPage, /replaceAssignmentValues/);
  assert.doesNotMatch(payRatesPage, /copy-from/);
  assert.doesNotMatch(payRatesPage, /pay_item_id/);
  assert.doesNotMatch(payRatesPage, /\/payroll\/drivers\/\$\{[^}]+\}\/rates\/(pending|history|summary)/);
});

test('Status pay rates keep the temporary Status-only rate path and always send the Status column identity', () => {
  assert.match(payRatesPage, /status_rate_column_id/);
  assert.match(payRatesPage, /rate-matrix/);
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
  assert.doesNotMatch(payRatesPage, /Save & Approve \$\{/);
  assert.doesNotMatch(payRatesPage, /dirtyCount|saveRates\(|enterEditMode|dirtyKeys/);
  const body = functionBody(payRatesPage, /async function saveTargetRow\(/);
  assert.doesNotMatch(body, /\bfor\s*\(|\.forEach\(|\.map\(|Promise\.all/);
  assert.equal((body.match(/createAssignment\(/g) ?? []).length, 1);
  assert.doesNotMatch(body, /rates\/batch/);
  assert.match(payRatesPage, /Save as Pending/);
  assert.match(payRatesPage, /Save & Approve/);
  assert.match(payRatesPage, /cancelTargetEdit/);
});

test('A failed approval keeps the saved Pending change visible and recoverable', () => {
  const body = functionBody(payRatesPage, /async function saveTargetRow\(/);
  assert.match(body, /saved as Pending, but approval failed/);
  assert.match(body, /refreshTargetRows\(driverId\)/);
  assert.match(body, /Pending Changes/);
});

test('Status rates keep an independent temporary batch action and never touch target assignments', () => {
  const body = functionBody(payRatesPage, /async function saveStatusRates\(/);
  assert.match(body, /rates\/batch/);
  assert.doesNotMatch(body, /createAssignment|replaceAssignmentValues|approveAssignment/);
  assert.match(payRatesPage, /Edit Status Rates/);
});
