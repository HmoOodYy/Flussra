import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { DriverAssignmentRosterRow } from '../src/types/core.ts';
import {
  DEFAULT_DRIVER_TENURE_FILTER,
  NOT_APPLICABLE_FLAG,
  PAST_BRANCH_BANNER,
  applicabilityFlag,
  canEditDriverCompensation,
  canEditStatusRates,
  defaultStatusAssignment,
  driverBranchSummary,
  filterDrivers,
  groupRosterByDriver,
  visibleTabs,
  type DriverFilters,
} from '../src/pages/people/pay-rates/payRatesDrivers.ts';

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
const current = row(11, 'current', { branch_id: 4, branch_name: 'South', effective_from_at: '2026-03-03T00:00:00Z' });
const pending = row(12, 'pending', {
  driver_id: 20, driver_number: 'D-0020', full_name: 'Avery Hauler', branch_id: 5, branch_name: 'East',
  effective_from_at: '2027-01-01T00:00:00Z',
});
// A past Branch's manager: only the ended tenure of this Driver is readable.
const pastOnlyRow = row(13, 'ended', {
  driver_id: 21, driver_number: 'D-0021', full_name: 'Blake Rider', driver_status: 'Inactive',
  effective_to_at: '2026-02-01T00:00:00Z', end_reason: 'Transferred',
});
const roster = [ended, current, pending, pastOnlyRow];
const drivers = groupRosterByDriver(roster);
const byId = (id: number) => drivers.find((d) => d.driverId === id)!;

const baseFilters: DriverFilters = { search: '', branchId: '', driverStatus: '', tenure: 'all' };
const names = (list: typeof drivers) => list.map((d) => d.fullName);

test('the roster has one row per Driver, grouped by driver_id and ordered by name', () => {
  assert.deepEqual(names(drivers), ['Avery Hauler', 'Blake Rider', 'Casey Driver']);
  const casey = byId(9);
  assert.deepEqual(casey.tenures.map((t) => t.driver_assignment_id), [11, 10]); // newest first
  assert.equal(casey.current?.driver_assignment_id, 11);
  assert.equal(casey.home.driver_assignment_id, 11);
  assert.equal(casey.driverNumber, 'D-0009');
});

test('the home tenure is current, else upcoming, else the latest ended one', () => {
  assert.equal(byId(9).home.driver_assignment_id, 11);
  assert.equal(byId(20).home.driver_assignment_id, 12);
  assert.equal(byId(21).home.driver_assignment_id, 13);
  assert.equal(defaultStatusAssignment(byId(9))?.driver_assignment_id, 11);
  assert.equal(defaultStatusAssignment(null), null);
});

test('a Driver whose readable tenures all ended is shown in past-Branch mode', () => {
  assert.equal(byId(21).pastOnly, true);
  assert.equal(byId(9).pastOnly, false);
  assert.equal(byId(20).pastOnly, false);
  assert.equal(PAST_BRANCH_BANNER, 'History from your Branch\'s tenure — read-only');
});

test('past-Branch mode offers no edit controls and no Pending tab; authority mode offers both', () => {
  const allow = () => true;
  assert.equal(canEditDriverCompensation(byId(21), allow), false);
  assert.equal(canEditDriverCompensation(null, allow), false);
  assert.equal(canEditDriverCompensation(byId(9), allow), true);
  assert.deepEqual(visibleTabs(byId(21)), ['current', 'history', 'pay-rules']);
  assert.deepEqual(visibleTabs(byId(9)), ['current', 'pending', 'history', 'pay-rules']);
});

test('Driver-owned edit authority follows the current (else upcoming) Branch, not the old one', () => {
  const onlyNorth = (branchId: number) => branchId === 3;
  const onlySouth = (branchId: number) => branchId === 4;
  assert.equal(canEditDriverCompensation(byId(9), onlyNorth), false); // North is the past Branch
  assert.equal(canEditDriverCompensation(byId(9), onlySouth), true);
  assert.equal(canEditDriverCompensation(byId(20), (branchId) => branchId === 5), true);
});

test('Status rates stay assignment-addressed: only a current or upcoming assignment is writable', () => {
  const allow = () => true;
  assert.equal(canEditStatusRates(ended, allow), false);
  assert.equal(canEditStatusRates(current, allow), true);
  assert.equal(canEditStatusRates(pending, allow), true);
  assert.equal(canEditStatusRates(current, (branchId) => branchId === 3), false);
  assert.equal(canEditStatusRates(null, allow), false);
});

test('the default list view hides Drivers who are readable only through a past Branch', () => {
  assert.equal(DEFAULT_DRIVER_TENURE_FILTER, 'open');
  assert.deepEqual(names(filterDrivers(drivers, { ...baseFilters, tenure: 'open' })), ['Avery Hauler', 'Casey Driver']);
  assert.deepEqual(names(filterDrivers(drivers, { ...baseFilters, tenure: 'ended' })), ['Blake Rider', 'Casey Driver']);
  assert.deepEqual(names(filterDrivers(drivers, baseFilters)), ['Avery Hauler', 'Blake Rider', 'Casey Driver']);
});

test('the Branch filter matches any readable tenure of the Driver', () => {
  assert.deepEqual(names(filterDrivers(drivers, { ...baseFilters, branchId: '3' })), ['Blake Rider', 'Casey Driver']); // past tenures
  assert.deepEqual(names(filterDrivers(drivers, { ...baseFilters, branchId: '4' })), ['Casey Driver']); // current tenure
  assert.deepEqual(names(filterDrivers(drivers, { ...baseFilters, branchId: '5' })), ['Avery Hauler']);
});

test('search and Driver status filters apply to the Driver', () => {
  assert.deepEqual(names(filterDrivers(drivers, { ...baseFilters, search: 'd-0009' })), ['Casey Driver']);
  assert.deepEqual(names(filterDrivers(drivers, { ...baseFilters, search: 'blake' })), ['Blake Rider']);
  assert.deepEqual(names(filterDrivers(drivers, { ...baseFilters, driverStatus: 'Inactive' })), ['Blake Rider']);
});

test('the list shows the current Branch, the previous one, and marks past-Branch visibility', () => {
  assert.equal(driverBranchSummary(byId(9)), 'South (was North)');
  assert.equal(driverBranchSummary(byId(20)), 'Upcoming: East');
  assert.equal(driverBranchSummary(byId(21)), 'North (past Branch)');
});

test('a definition the context Branch does not use is flagged; unknown or applicable is not', () => {
  assert.equal(applicabilityFlag({ applicable_in_context_branch: false }), NOT_APPLICABLE_FLAG);
  assert.equal(NOT_APPLICABLE_FLAG, 'Not used in this Branch on that date');
  assert.equal(applicabilityFlag({ applicable_in_context_branch: true }), null);
  assert.equal(applicabilityFlag({ applicable_in_context_branch: null }), null);
});
