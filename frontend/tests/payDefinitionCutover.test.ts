import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const read = (path: string) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

const payItemsPage = read('src/pages/settings/pay-items/PayItemsPage.tsx');
const payRatesPage = read('src/pages/people/pay-rates/PayRatesPage.tsx');
const compensationApi = read('src/lib/compensationApi.ts');

test('Pay Items settings use only target Compensation APIs for definitions and branch configuration', () => {
  assert.doesNotMatch(payItemsPage, /\/settings\/cdpi/);
  assert.doesNotMatch(payItemsPage, /\/settings\/pay-items/);
  assert.doesNotMatch(payItemsPage, /\/settings\/branches\/[^'"`]*pay-items/);
  assert.doesNotMatch(payItemsPage, /cdpiApi|Cdpi/);
  assert.match(payItemsPage, /compensationApi/);
});

test('Pay Items settings expose no legacy display-order or PayItem identity', () => {
  assert.doesNotMatch(payItemsPage, /pay_item_id|sort_order|PayItemOrder|dragHandle/);
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
