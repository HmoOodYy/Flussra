import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import type { DriverAssignmentRosterRow } from '../src/types/core.ts';
import {
  assignmentLabel,
  assignmentWindow,
  isReadOnlyAssignment,
} from '../src/pages/people/pay-rates/payRatesAssignments.ts';

function row(id: number, state: DriverAssignmentRosterRow['state'], overrides: Partial<DriverAssignmentRosterRow> = {}): DriverAssignmentRosterRow {
  return {
    driver_assignment_id: id,
    driver_id: 9,
    branch_id: 3,
    branch_name: 'North',
    effective_from_at: '2026-01-02T00:00:00Z',
    effective_to_at: null,
    end_reason: null,
    state,
    driver_number: 'D-0009',
    driver_status: 'Active',
    full_name: 'Casey Driver',
    preferred_name: null,
    ...overrides,
  };
}

const ended = row(10, 'ended', { effective_to_at: '2026-03-03T00:00:00Z', end_reason: 'Transferred' });
const current = row(11, 'current', { branch_id: 4, branch_name: 'South', effective_from_at: '2026-03-03T00:00:00Z' });
const pending = row(12, 'pending', { branch_id: 5, branch_name: 'East', effective_from_at: '2027-01-01T00:00:00Z' });

test('assignment labels carry Branch, window and end reason; open windows read "present"', () => {
  assert.equal(assignmentLabel(ended), 'North · Jan 2, 2026 – Mar 3, 2026 (Transferred)');
  assert.equal(assignmentLabel(current), 'South · Mar 3, 2026 – present');
  assert.equal(assignmentLabel(pending), 'Upcoming · East · Jan 1, 2027 – present');
  assert.equal(assignmentWindow(ended), 'Jan 2, 2026 – Mar 3, 2026 (Transferred)');
});

test('only ended assignments are read-only; current and pending stay editable', () => {
  assert.equal(isReadOnlyAssignment(ended), true);
  assert.equal(isReadOnlyAssignment(current), false);
  assert.equal(isReadOnlyAssignment(pending), false);
  assert.equal(isReadOnlyAssignment(null), false);
});

const read = (name: string) => readFileSync(
  new URL(`../src/pages/people/pay-rates/${name}`, import.meta.url), 'utf8');

test('the Pay Rates roster is built from the assignment roster grouped by Driver', () => {
  assert.match(read('driverAssignmentsApi.ts'), /\/core\/driver-assignments/);
  const context = read('usePayRateDriverContext.ts');
  assert.match(context, /listDriverAssignmentRoster\(\)/);
  assert.match(context, /groupRosterByDriver\(roster\)/);
  assert.match(context, /filterDrivers\(drivers/);
  // Deep links: ?driverId=, ?driverAssignmentId= resolved to its Driver, and ?driverUserId=.
  assert.match(context, /idParam\('driverId'\)/);
  assert.match(context, /idParam\('driverAssignmentId'\)/);
  assert.match(context, /roster\.find\(\(row\) => row\.driver_assignment_id === linkedAssignmentId\)/);
  assert.match(context, /linkedAssignment\?\.driver_id/);
  assert.match(context, /tenureById\(linkedAssignmentId\)/); // the linked assignment is preselected for Status rates
  assert.match(context, /has_driver: boolean; driver_id: number \| null/);
});

test('ordinary rates and pay rules are read and written by driver_id; Status rates by assignment', () => {
  const reads = read('usePayRateReads.ts');
  assert.match(reads, /listDriverPayRates\(driverId\)/);
  assert.match(reads, /listScheduleHistory\(driverId, row\.rate_definition_id\)/);
  assert.match(reads, /getDriverPayRules\(driverId\)/);
  assert.match(reads, /getStatusRateMatrix\(driverAssignmentId\)/);
  assert.match(read('useTargetRateEditing.ts'), /createSchedule\(\{\s*driver_id: driverId/);
  assert.match(read('useDriverPayRules.ts'), /driver_id: selectedDriverId/);
  // The Status pane keeps its assignment picker and its own save path.
  const section = read('StatusRateSection.tsx');
  assert.match(section, /Branch Status Pay Rates/);
  assert.match(section, /selectAssignment\(Number\(e\.target\.value\)\)/);
  assert.match(read('statusRatesApi.ts'), /\/payroll\/driver-assignments\/\$\{driverAssignmentId\}\/rate-matrix/);
  assert.match(read('statusRatesApi.ts'), /\/payroll\/driver-assignments\/\$\{driverAssignmentId\}\/rates\/batch/);
});

test('the page gates edits on Driver authority and shows the read-only banner for a past Branch', () => {
  const page = read('PayRatesPage.tsx');
  assert.match(page, /canEditDriverCompensation\(selectedDriver, canEditAtBranch\)/);
  assert.match(page, /canEditStatusRates\(statusAssignment, canEditAtBranch\)/);
  assert.match(page, /pastOnly && <p className=\{styles\.readOnlyBanner\}>\{PAST_BRANCH_BANNER\}/);
  assert.match(page, /visibleTabs\(selectedDriver\)/);
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
