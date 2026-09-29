import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

function readSource(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

// ── DateInput.tsx ────────────────────────────────────────────────────────────

const dateInputSource = readSource('../src/components/ui/DateInput.tsx');

test('DateInput.tsx: renders Day, Month, Year labels in that order', () => {
  const dayMatch = /<label[^>]*>\s*Day\s*<\/label>/.exec(dateInputSource);
  const monthMatch = /<label[^>]*>\s*Month\s*<\/label>/.exec(dateInputSource);
  const yearMatch = /<label[^>]*>\s*Year\s*<\/label>/.exec(dateInputSource);
  assert.ok(dayMatch, 'Day label not found');
  assert.ok(monthMatch, 'Month label not found');
  assert.ok(yearMatch, 'Year label not found');
  assert.ok(dayMatch!.index < monthMatch!.index, 'Day must come before Month');
  assert.ok(monthMatch!.index < yearMatch!.index, 'Month must come before Year');
});

test('DateInput.tsx: uses inputMode="numeric" and never type="date"', () => {
  assert.match(dateInputSource, /inputMode="numeric"/);
  assert.doesNotMatch(dateInputSource, /type="date"/);
});

test('DateInput.tsx: never constructs a JS Date object', () => {
  assert.doesNotMatch(dateInputSource, /\bnew Date\(/);
  assert.doesNotMatch(dateInputSource, /\bDate\.(now|UTC|parse)\(/);
  assert.doesNotMatch(dateInputSource, /\bDate\(/);
});

// ── PayrollBoundaryDateInput.tsx ─────────────────────────────────────────────

const boundaryInputSource = readSource('../src/components/payroll/PayrollBoundaryDateInput.tsx');

test('PayrollBoundaryDateInput.tsx: stepper buttons carry the required aria-labels', () => {
  assert.match(boundaryInputSource, /aria-label="Next valid date"/);
  assert.match(boundaryInputSource, /aria-label="Previous valid date"/);
});

test('PayrollBoundaryDateInput.tsx: raw conflict codes are tucked behind "Technical details"', () => {
  assert.match(boundaryInputSource, /Technical details/);
});

test('PayrollBoundaryDateInput.tsx: never renders `.code` outside the collapsed details block', () => {
  const detailsStart = boundaryInputSource.indexOf('<details');
  const detailsEnd = boundaryInputSource.indexOf('</details>');
  assert.ok(detailsStart >= 0 && detailsEnd > detailsStart, 'expected a single <details>...</details> block');

  const before = boundaryInputSource.slice(0, detailsStart);
  const after = boundaryInputSource.slice(detailsEnd + '</details>'.length);
  assert.doesNotMatch(before, /\.code\b/);
  assert.doesNotMatch(after, /\.code\b/);
});

// ── ui/index.ts ──────────────────────────────────────────────────────────────

test('ui/index.ts: exports DateInput', () => {
  const indexSource = readSource('../src/components/ui/index.ts');
  assert.match(indexSource, /export\s*\{\s*DateInput\s*\}\s*from\s*'\.\/DateInput'/);
});
