import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import type { DriverAssignmentRosterRow } from '../src/types/core.ts';
import {
  DEFAULT_ASSIGNMENT_FILTER,
  assignmentLabel,
  assignmentWindow,
  driverAssignmentsOf,
  filterRoster,
  isReadOnlyAssignment,
  rosterIdentity,
  stateBadgeLabel,
  type RosterFilters,
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

// A permanent transfer: the same Driver (D-0009) has an ended North tenure and a current South one.
const ended = row(10, 'ended', { effective_to_at: '2026-03-03T00:00:00Z', end_reason: 'Transferred' });
const current = row(11, 'current', {
  branch_id: 4, branch_name: 'South', effective_from_at: '2026-03-03T00:00:00Z',
});
const pending = row(12, 'pending', {
  driver_id: 20, driver_number: 'D-0020', full_name: 'Avery Hauler', branch_id: 5, branch_name: 'East',
  effective_from_at: '2027-01-01T00:00:00Z',
});
const otherEnded = row(13, 'ended', {
  driver_id: 21, driver_number: 'D-0021', full_name: 'Blake Rider', driver_status: 'Inactive',
  effective_to_at: '2026-02-01T00:00:00Z', end_reason: 'Terminated',
});
const roster = [ended, current, pending, otherEnded];

const baseFilters: RosterFilters = { search: '', branchId: '', driverStatus: '', assignment: 'open' };
const ids = (rows: DriverAssignmentRosterRow[]) => rows.map((r) => r.driver_assignment_id);

test('assignment labels carry Branch, window and end reason; open windows read "present"', () => {
  assert.equal(assignmentLabel(ended), 'North · Jan 2, 2026 – Mar 3, 2026 (Transferred)');
  assert.equal(assignmentLabel(current), 'South · Mar 3, 2026 – present');
  assert.equal(assignmentLabel(pending), 'Upcoming · East · Jan 1, 2027 – present');
  assert.equal(assignmentWindow(otherEnded), 'Jan 2, 2026 – Feb 1, 2026 (Terminated)');
});

test('only ended assignments are read-only; current and pending stay editable', () => {
  assert.equal(isReadOnlyAssignment(ended), true);
  assert.equal(isReadOnlyAssignment(current), false);
  assert.equal(isReadOnlyAssignment(pending), false);
  assert.equal(isReadOnlyAssignment(null), false);
});

test('the roster shows state badges Current, Pending and Ended', () => {
  assert.equal(stateBadgeLabel('current'), 'Current');
  assert.equal(stateBadgeLabel('pending'), 'Pending');
  assert.equal(stateBadgeLabel('ended'), 'Ended');
});

test('identity is the permanent Driver number with the Branch provenance, per assignment row', () => {
  assert.equal(rosterIdentity(ended), 'D-0009 · North');
  assert.equal(rosterIdentity(current), 'D-0009 · South');
  assert.equal(rosterIdentity(row(14, 'current', { driver_number: null })), 'No number · North');
});

test('the default roster view lists current and pending assignments, one row per assignment', () => {
  assert.equal(DEFAULT_ASSIGNMENT_FILTER, 'open');
  assert.deepEqual(ids(filterRoster(roster, baseFilters)), [11, 12]);
});

test('the Ended filter reaches a transferred Driver\'s old-Branch assignment; All shows everything', () => {
  assert.deepEqual(ids(filterRoster(roster, { ...baseFilters, assignment: 'ended' })), [10, 13]);
  assert.deepEqual(ids(filterRoster(roster, { ...baseFilters, assignment: 'all' })), [10, 11, 12, 13]);
});

test('search, Branch and Driver status filters apply to assignment rows', () => {
  const all = { ...baseFilters, assignment: 'all' as const };
  assert.deepEqual(ids(filterRoster(roster, { ...all, search: 'd-0009' })), [10, 11]);
  assert.deepEqual(ids(filterRoster(roster, { ...all, search: 'blake' })), [13]);
  assert.deepEqual(ids(filterRoster(roster, { ...all, branchId: '4' })), [11]);
  assert.deepEqual(ids(filterRoster(roster, { ...all, driverStatus: 'Inactive' })), [13]);
});

test('the picker lists the same Driver\'s other readable assignments', () => {
  assert.deepEqual(ids(driverAssignmentsOf(roster, 9)), [10, 11]);
  assert.deepEqual(ids(driverAssignmentsOf(roster, 20)), [12]);
  assert.deepEqual(driverAssignmentsOf(roster, null), []);
});

test('Pay Rates builds its roster from assignment rows and gates edits on the viewed assignment', () => {
  const read = (name: string) => readFileSync(
    new URL(`../src/pages/people/pay-rates/${name}`, import.meta.url), 'utf8');
  assert.match(read('driverAssignmentsApi.ts'), /\/core\/driver-assignments/);
  const context = read('usePayRateDriverContext.ts');
  assert.match(context, /listDriverAssignmentRoster\(\)/);
  assert.match(context, /filterRoster\(roster/);
  // Deep link and the user lookup keep addressing an assignment.
  assert.match(context, /searchParams\.get\('driverAssignmentId'\)/);
  assert.match(context, /has_driver: boolean; driver_assignment_id: number \| null/);
  // The viewed assignment is looked up in the whole roster, so an ended one is reachable
  // whatever the list filter shows.
  assert.match(context, /roster\.find\(\(row\) => row\.driver_assignment_id === selectedDriverAssignmentId\)/);
  const page = read('PayRatesPage.tsx');
  assert.match(page, /isReadOnlyAssignment\(driver\.viewedAssignment\)/);
  assert.match(page, /!readOnlyAssignment/);
  assert.match(page, /driver\.selectAssignment\(Number\(e\.target\.value\)\)/);
  const list = read('PayRateDriverList.tsx');
  assert.match(list, /ASSIGNMENT_FILTER_LABELS/);
  assert.match(list, /stateBadgeLabel\(row\.state\)/);
  // Ownership and authorization are unchanged: every read still goes through the
  // assignment-addressed payroll/compensation routes.
  assert.ok(!/driver_id/.test(read('usePayRateReads.ts')));
});
