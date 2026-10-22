import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { CompensationAccess } from '../src/types/compensation.ts';
import type { DriverAssignmentRosterRow } from '../src/types/core.ts';
import {
  DEFAULT_DRIVER_TENURE_FILTER,
  NOT_APPLICABLE_FLAG,
  PAST_BRANCH_BANNER,
  applicabilityFlag,
  canEditDriverCompensation,
  canDiscardPendingRates,
  driverBranchSummary,
  filterDrivers,
  groupRosterByDriver,
  isPastBranchView,
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
});

test('the roster flags a Driver with no current or pending tenure, for presentation only', () => {
  assert.equal(byId(21).noOpenTenure, true);
  assert.equal(byId(9).noOpenTenure, false);
  assert.equal(byId(20).noOpenTenure, false);
  assert.equal(PAST_BRANCH_BANNER, 'History from your Branch\'s tenure — read-only');
});

// What the backend decided for four callers of one Driver (GET .../compensation-access).
const oldBranchReader: CompensationAccess = {
  full_history: false, can_discard_pending: false, has_current_or_pending_tenure: true };
const companyWideReaderAfterAllEnded: CompensationAccess = {
  full_history: true, can_discard_pending: false, has_current_or_pending_tenure: false };
const companyWideEditorAfterAllEnded: CompensationAccess = {
  full_history: true, can_discard_pending: true, has_current_or_pending_tenure: false };
const currentBranchEditor: CompensationAccess = {
  full_history: true, can_discard_pending: true, has_current_or_pending_tenure: true };
const allow = () => true;

test('an old-Branch reader sees the read-only banner, no Pending tab and no edit or discard', () => {
  assert.equal(isPastBranchView(oldBranchReader), true);
  assert.deepEqual(visibleTabs(oldBranchReader), ['current', 'history', 'pay-rules']);
  assert.equal(canEditDriverCompensation(byId(9), oldBranchReader, allow), false);
  assert.equal(canDiscardPendingRates(oldBranchReader), false);
});

test('an unrelated-Branch reader gets no Driver at all: the roster is empty and the 403 reads as permission', () => {
  assert.deepEqual(groupRosterByDriver([]), []);
  assert.equal(canEditDriverCompensation(null, null, allow), false);
  assert.equal(isPastBranchView(null), false); // nothing loaded: no banner, no access
  assert.deepEqual(visibleTabs(null), ['current', 'history', 'pay-rules']);
  assert.equal(canDiscardPendingRates(null), false);
});

test('a company-wide reader after every tenure ended keeps Pending visibility, no banner, no discard', () => {
  const access = companyWideReaderAfterAllEnded;
  assert.equal(isPastBranchView(access), false);
  assert.deepEqual(visibleTabs(access), ['current', 'pending', 'history', 'pay-rules']);
  assert.equal(canDiscardPendingRates(access), false);
  assert.equal(canEditDriverCompensation(byId(21), access, allow), false);
});

test('a company-wide editor after every tenure ended may discard Pending rates but not approve or edit', () => {
  const access = companyWideEditorAfterAllEnded;
  assert.equal(isPastBranchView(access), false);
  assert.deepEqual(visibleTabs(access), ['current', 'pending', 'history', 'pay-rules']);
  assert.equal(canDiscardPendingRates(access), true);
  assert.equal(canEditDriverCompensation(byId(21), access, allow), false); // orphaned: approval is refused
});

test('the current authority Branch keeps the full set of controls', () => {
  assert.equal(canEditDriverCompensation(byId(9), currentBranchEditor, allow), true);
  assert.equal(canDiscardPendingRates(currentBranchEditor), true);
});

test('Driver-owned edit authority follows the current (else upcoming) Branch, not the old one', () => {
  const onlyNorth = (branchId: number) => branchId === 3;
  const onlySouth = (branchId: number) => branchId === 4;
  assert.equal(canEditDriverCompensation(byId(9), currentBranchEditor, onlyNorth), false); // North is the past Branch
  assert.equal(canEditDriverCompensation(byId(9), currentBranchEditor, onlySouth), true);
  assert.equal(canEditDriverCompensation(byId(20), currentBranchEditor, (branchId) => branchId === 5), true);
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
