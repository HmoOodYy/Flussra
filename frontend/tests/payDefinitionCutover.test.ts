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
