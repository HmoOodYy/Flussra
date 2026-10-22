import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
const read = (name: string) => readFileSync(
  new URL(`../src/pages/people/pay-rates/${name}`, import.meta.url), 'utf8');

test('the Pay Rates roster is built from the assignment roster and uses permanent Driver links', () => {
  assert.match(read('driverAssignmentsApi.ts'), /\/core\/driver-assignments/);
  const context = read('usePayRateDriverContext.ts');
  assert.match(context, /listDriverAssignmentRoster\(\)/);
  assert.match(context, /groupRosterByDriver\(roster\)/);
  assert.match(context, /filterDrivers\(drivers/);
  // External navigation addresses the permanent Driver; no User or assignment translation is needed.
  assert.match(context, /idParam\('driverId'\)/);
  assert.doesNotMatch(context, /driverUserId|driverAssignmentId|linkedAssignment/);
  assert.doesNotMatch(context, /tenureById|statusAssignment|selectStatusAssignment/); // no rate is tied to an assignment
});

test('every rate and the pay rules are read and written by driver_id; Status rates are rate column schedules', () => {
  const reads = read('usePayRateReads.ts');
  assert.match(reads, /listDriverPayRates\(driverId\)/);
  // History is discovered from the Driver's schedule summary, never from the Active-only matrix.
  assert.match(reads, /listDriverRateSummary\(driverId\)/);
  assert.match(reads, /listScheduleHistory\(driverId, item\.rate_definition_id\)/);
  assert.match(reads, /buildHistoryRows\(items, lists\)/);
  assert.doesNotMatch(reads.slice(reads.indexOf('const loadHistory')), /listDriverPayRates|current\.map/);
  assert.match(reads, /getDriverPayRules\(driverId\)/);
  assert.match(reads, /getCompensationAccess\(driverId\)/);
  assert.doesNotMatch(reads, /getStatusRateMatrix|statusAssignmentId|statusMatrix/);
  assert.match(read('useTargetRateEditing.ts'), /createSchedule\(\{\s*driver_id: driverId/);
  assert.match(read('useDriverPayRules.ts'), /driver_id: selectedDriverId/);
  // Status rates are rows of the same listing (kind StatusRateColumn), edited by the same row
  // editor and the same compensation schedule routes as Pay Item rates.
  const page = read('PayRatesPage.tsx');
  assert.match(page, /r\.kind === 'StatusRateColumn'/);
  assert.match(page, /title="Status Pay Rates"/);
  assert.match(page, /title="Pay Item Rates"/);
  assert.equal((page.match(/<TargetRateSection /g) ?? []).length, 2);
  assert.doesNotMatch(page, /StatusRateSection|useStatusRateEditing|statusRatesApi/);
});

test('the page gates edits on Driver authority and shows the read-only banner for a past Branch', () => {
  const page = read('PayRatesPage.tsx');
  assert.match(page, /canEditDriverCompensation\(selectedDriver, reads\.access, canEditAtBranch\)/);
  assert.doesNotMatch(page, /canEditStatusRates/);
  assert.match(page, /pastBranchView && <p className=\{styles\.readOnlyBanner\}>\{PAST_BRANCH_BANNER\}/);
  assert.match(page, /isPastBranchView\(reads\.access\)/);
  assert.match(page, /canDiscardPendingRates\(reads\.access\)/);
  assert.match(page, /visibleTabs\(reads\.access\)/);
  assert.match(read('TargetRateSection.tsx'), /applicabilityFlag\(row\)/);
});

test('no stale assignment-owned compensation route, field or type remains in the frontend', () => {
  const files = [
    '../src/lib/compensationApi.ts', '../src/types/compensation.ts',
    '../src/pages/people/pay-rates/driverPayRulesApi.ts',
    '../src/pages/people/pay-rates/usePayRateReads.ts',
    '../src/pages/people/pay-rates/useTargetRateEditing.ts',
    '../src/pages/people/pay-rates/useRateScheduleActions.ts',
    '../src/pages/people/pay-rates/useDriverPayRules.ts',
    '../src/pages/people/pay-rates/PendingRateChanges.tsx',
    '../src/pages/people/pay-rates/RateHistoryTab.tsx',
  ].map((path) => readFileSync(new URL(path, import.meta.url), 'utf8')).join('\n');
  assert.doesNotMatch(files, /driver-rate-assignments|driver_rate_assignment_id/);
  assert.doesNotMatch(files, /\/driver-assignments\/\$\{[^}]+\}\/(pay-rates|pay-rules|rate-definitions)/);
  assert.doesNotMatch(files, /copy-from|copyRates|copy_driver_rates/);
  assert.doesNotMatch(files, /AssignmentSummary|AssignmentBrief|AssignmentCreatePayload/);
});

test('access flags come from the backend endpoint and gate Pending actions separately', () => {
  assert.match(readFileSync(new URL('../src/lib/compensationApi.ts', import.meta.url), 'utf8'),
    /\/compensation\/drivers\/\$\{driverId\}\/compensation-access/);
  const pending = read('PendingRateChanges.tsx');
  assert.match(pending, /\{approve && <button className=\{styles\.btnApprove\}[^]*?Approve/);
  assert.match(pending, /\{discard && <button className=\{styles\.btnVoid\}[^]*?Discard/);
  assert.match(pending, /canApprovePendingRow\(row, canEditDriver\)/);
  assert.match(pending, /canDiscardPendingRow\(canDiscardDriver\)/);
  // The client never re-derives scope from the roster.
  assert.doesNotMatch(read('PayRatesPage.tsx'), /pastOnly/);
  assert.doesNotMatch(read('payRatesDrivers.ts'), /pastOnly/);
});
