import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

// ── Walk frontend/src ───────────────────────────────────────────────────────

const srcDir = fileURLToPath(new URL('../src', import.meta.url));

function listSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      out.push(...listSourceFiles(full));
    } else if (/\.(ts|tsx)$/.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

const sourceFiles = listSourceFiles(srcDir);

// ── Forbidden patterns (legacy branch-owned Payroll Setup authority) ───────
//
// tab=status-keys handling is allowed and must NOT be flagged — it is the
// legacy redirect target, implemented via legacyPayrollSettingsRedirect.

const FORBIDDEN_PATTERNS: { name: string; re: RegExp }[] = [
  { name: 'legacy branch payroll-setup route', re: /\/settings\/branches\/[^\s'"`]*\/payroll-setup/ },
  { name: 'PayrollSetupUpsert identifier', re: /PayrollSetupUpsert/ },
  { name: 'PayrollSetup identifier (exact)', re: /\bPayrollSetup\b/ },
  { name: 'computeUpcomingPeriods', re: /computeUpcomingPeriods/ },
  { name: 'first_custom_end_date', re: /first_custom_end_date/ },
  { name: 'tab=pay-schedule', re: /tab=pay-schedule/ },
  { name: 'MAX_DAYS_OFF', re: /MAX_DAYS_OFF/ },
  { name: 'PayrollSetupPage identifier', re: /PayrollSetupPage\b/ },
];

for (const { name, re } of FORBIDDEN_PATTERNS) {
  test(`no frontend source file matches forbidden pattern: ${name}`, () => {
    const offenders: string[] = [];
    for (const file of sourceFiles) {
      const content = readFileSync(file, 'utf8');
      if (re.test(content)) offenders.push(path.relative(srcDir, file));
    }
    assert.deepEqual(offenders, [], `Pattern "${name}" (${re}) found in: ${offenders.join(', ')}`);
  });
}

// ── Legacy page files must be gone ──────────────────────────────────────────

test('legacy PayrollSetupPage.tsx no longer exists', () => {
  const p = path.join(srcDir, 'pages', 'settings', 'payroll', 'PayrollSetupPage.tsx');
  assert.equal(existsSync(p), false, `${p} still exists`);
});

test('legacy PayrollSetupPage.module.css no longer exists', () => {
  const p = path.join(srcDir, 'pages', 'settings', 'payroll', 'PayrollSetupPage.module.css');
  assert.equal(existsSync(p), false, `${p} still exists`);
});

// ── Sanity: the allowed tab=status-keys redirect target string is still
// reachable from source (proves the sweep above isn't vacuous) ─────────────

test('sanity: the status-keys redirect target string is still present somewhere (not flagged by the sweep above)', () => {
  const found = sourceFiles.some((file) => /'status-keys'/.test(readFileSync(file, 'utf8')));
  assert.equal(found, true, "expected legacyPayrollSettingsRedirect to still reference 'status-keys'");
});
