import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { AxiosError } from 'axios';
import { ledgerLoadErrorMessage } from '../src/pages/payroll/ledgerLoadError.ts';

function httpFailure(status: number): AxiosError {
  const error = new AxiosError('failed');
  error.response = { status, data: {}, statusText: '', headers: {}, config: {} as never };
  return error;
}

test('a 403 from the finalized authority is reported as no access, not as a load failure', () => {
  assert.match(ledgerLoadErrorMessage(httpFailure(403)), /do not have access/);
});

test('every other failure is a real load failure, never presented as an empty or forbidden ledger', () => {
  for (const failure of [httpFailure(500), httpFailure(401), httpFailure(404), new AxiosError('Network Error'), new Error('boom')]) {
    assert.equal(ledgerLoadErrorMessage(failure), 'Failed to load ledger periods.');
  }
});

// ── The frontend has one operational Day Grid and one finalized-payroll presentation ─────────

function sourceFiles(): string[] {
  const root = new URL('../src/', import.meta.url);
  const files: string[] = [];
  const walk = (dir: URL) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const next = new URL(`${entry.name}${entry.isDirectory() ? '/' : ''}`, dir);
      if (entry.isDirectory()) walk(next);
      else if (/\.(ts|tsx)$/.test(entry.name)) files.push(fileURLToPath(next));
    }
  };
  walk(root);
  return files;
}

test('no active frontend source uses the retired duplicate Day Grid page, the raw FinalLines surface or a generic money writer', () => {
  const retired = new RegExp([
    'PeriodDetailPage',
    'payroll/periods/:periodId',
    'FinalSummaryDialog',
    'canViewFinalLines',
    'getFinalLines',
    'FinalLineSummary',
    '/final-lines',
    'discoverLedgerPeriods',
    'rate-matrix',
    'rates/batch',
    'copy-rates',
  ].join('|'));
  const offenders = sourceFiles().filter((file) => retired.test(readFileSync(file, 'utf8')));
  assert.deepEqual(offenders, []);
});

test('the Day Grid summary carries no PTO figure the backend does not own', () => {
  const types = readFileSync(new URL('../src/types/payroll/entry.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(types, /\bpto\b/);
  const workbench = readFileSync(new URL('../src/pages/payroll/PayrollEntryDialog.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(workbench, /PTO|summary\.pto/);
});

test('the payroll entry workbench has no money authoring: no rate or amount input, only definition-keyed quantities', () => {
  const workbench = readFileSync(new URL('../src/pages/payroll/PayrollEntryDialog.tsx', import.meta.url), 'utf8');
  assert.match(workbench, /quantityInputProps\(col\)/);
  assert.doesNotMatch(workbench, /rate_|recalculate|<input[^>]*(rate|amount|money)/i);
});
