import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import type {
  CompensationAccess,
  DriverPayRateRow,
  DriverRateSummaryItem,
  ScheduleSummary,
} from '../src/types/compensation.ts';
import type { DriverAssignmentRosterRow } from '../src/types/core.ts';
import {
  canDiscardPendingRates,
  canEditDriverCompensation,
  groupRosterByDriver,
  isPastBranchView,
  visibleTabs,
} from '../src/pages/people/pay-rates/payRatesDrivers.ts';
import { buildHistoryRows } from '../src/pages/people/pay-rates/payRatesHistory.ts';
import {
  buildPendingScheduleRows,
  canApprovePendingRow,
  canDiscardPendingRow,
} from '../src/pages/people/pay-rates/payRatesPending.ts';

const ACTIVE = 1;
const RETIRED = 2;

function summary(id: number, overrides: Partial<DriverRateSummaryItem> = {}): DriverRateSummaryItem {
  return {
    rate_definition_id: id, pay_definition_id: id, definition_code: `C${id}`,
    definition_name: `Definition ${id}`, unit: 'hr', pay_definition_status: 'Active',
    approved_schedule_id: null, approved_effective_from: null, pending_schedule_id: null,
    ...overrides,
  };
}

function schedule(id: number, rateDefinitionId: number, status: ScheduleSummary['status'], from: string, amount: string | null = '12'): ScheduleSummary {
  return {
    driver_rate_schedule_id: id, company_id: 1, driver_id: 9, pay_definition_id: rateDefinitionId,
    rate_definition_id: rateDefinitionId, effective_from: from, effective_to: null, status,
    created_by_user_id: null, created_at_utc: '2026-01-01T00:00:00Z', updated_by_user_id: null,
    updated_at_utc: null, approved_by_user_id: null, approved_at_utc: null, voided_by_user_id: null,
    voided_at_utc: null, void_reason: null, notes: null,
    values: [{ rate_component_definition_id: rateDefinitionId, sequence_no: 1, ordinal_from: null, ordinal_to: null, amount }],
  };
}

function matrixRow(id: number, current: DriverPayRateRow['current'] = null): DriverPayRateRow {
  return {
    pay_definition_id: id, definition_code: `C${id}`, definition_name: `Definition ${id}`,
    input_type: 'Decimal', unit: 'hr', rate_definition_id: id, rate_component_definition_id: id,
    applicable_in_context_branch: true, current, future: null, pending: null,
  };
}

function rosterRow(id: number, state: DriverAssignmentRosterRow['state']): DriverAssignmentRosterRow {
  return {
    driver_assignment_id: id, driver_id: 9, branch_id: 3, branch_name: 'North',
    effective_from_at: '2026-01-02T00:00:00Z', effective_to_at: state === 'ended' ? '2026-03-03T00:00:00Z' : null,
    end_reason: state === 'ended' ? 'Terminated' : null, state, driver_number: 'D-0009',
    driver_status: 'Active', full_name: 'Casey Driver', preferred_name: null,
  };
}

const allow = () => true;
const endedDriver = groupRosterByDriver([rosterRow(10, 'ended')])[0]; // no current or pending tenure
const liveDriver = groupRosterByDriver([rosterRow(11, 'current')])[0];

const companyEditorAfterAllEnded: CompensationAccess = {
  full_history: true, can_discard_pending: true, has_current_or_pending_tenure: false };
const companyReaderAfterAllEnded: CompensationAccess = {
  full_history: true, can_discard_pending: false, has_current_or_pending_tenure: false };
const editorWithTenure: CompensationAccess = {
  full_history: true, can_discard_pending: true, has_current_or_pending_tenure: true };
const oldBranchReader: CompensationAccess = {
  full_history: false, can_discard_pending: false, has_current_or_pending_tenure: true };

// The matrix lists Active definitions only; the retired definition (2) is absent from it.
const matrix = [matrixRow(ACTIVE)];
const retiredPendingSummary = [
  summary(ACTIVE),
  summary(RETIRED, { pay_definition_status: 'Retired', pending_schedule_id: 20 }),
];
const retiredPendingHistory = { [RETIRED]: [schedule(20, RETIRED, 'Pending', '2026-05-01', '15')] };

test('the exact case: a retired definition\'s Pending schedule is discovered, discardable, not approvable', () => {
  const access = companyEditorAfterAllEnded;
  const rows = buildPendingScheduleRows(retiredPendingSummary, retiredPendingHistory, matrix);

  assert.equal(rows.length, 1);
  const [row] = rows;
  assert.equal(row.scheduleId, 20);
  assert.equal(row.definitionName, 'Definition 2');
  assert.equal(row.definitionCode, 'C2');
  assert.equal(row.retired, true);
  assert.equal(row.inMatrix, false);
  assert.equal(row.amount, '15');
  assert.equal(row.effectiveFrom, '2026-05-01');
  // Discard is the backend's call; approval needs the edit gate, which an orphaned Driver fails.
  assert.equal(canDiscardPendingRow(canDiscardPendingRates(access)), true);
  const canEdit = canEditDriverCompensation(endedDriver, access, allow);
  assert.equal(canEdit, false);
  assert.equal(canApprovePendingRow(row, canEdit), false);
  // Even if the edit gate allowed, a retired definition is never approved.
  assert.equal(canApprovePendingRow(row, true), false);
  // The Pending tab is offered even though the retired definition is absent from the matrix.
  assert.ok(visibleTabs(access).includes('pending'));
});

test('an Active Pending orphan (no tenure) can be discarded but not approved or edited', () => {
  const rows = buildPendingScheduleRows(
    [summary(ACTIVE, { pending_schedule_id: 30 })],
    { [ACTIVE]: [schedule(30, ACTIVE, 'Pending', '2026-05-01')] },
    [matrixRow(ACTIVE, { driver_rate_schedule_id: 5, status: 'Approved', effective_from: '2026-01-01', effective_to: null, amount: '10' })]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].retired, false);
  assert.equal(rows[0].inMatrix, true);
  assert.equal(rows[0].current?.amount, '10');
  const canEdit = canEditDriverCompensation(endedDriver, companyEditorAfterAllEnded, allow);
  assert.equal(canEdit, false);
  assert.equal(canApprovePendingRow(rows[0], canEdit), false);
  assert.equal(canDiscardPendingRow(canDiscardPendingRates(companyEditorAfterAllEnded)), true);
});

test('a retired definition\'s Approved historical schedule is History only, never Pending', () => {
  const items = [summary(RETIRED, { pay_definition_status: 'Retired', approved_schedule_id: 21 })];
  const history = { [RETIRED]: [schedule(21, RETIRED, 'Approved', '2026-02-01')] };
  assert.deepEqual(buildPendingScheduleRows(items, history, matrix), []);
  const rows = buildHistoryRows(items, [history[RETIRED]]);
  assert.deepEqual(rows.map((r) => [r.driver_rate_schedule_id, r.retired]), [[21, true]]);
});

test('an old-Branch reader gets no Pending tab, no Pending rows and no actions', () => {
  assert.equal(isPastBranchView(oldBranchReader), true);
  assert.ok(!visibleTabs(oldBranchReader).includes('pending'));
  // The backend hides Pending from a past Branch, so the summary carries no pending schedule.
  assert.deepEqual(buildPendingScheduleRows([summary(ACTIVE), summary(RETIRED, { pay_definition_status: 'Retired' })], {}, matrix), []);
  assert.equal(canDiscardPendingRow(canDiscardPendingRates(oldBranchReader)), false);
  assert.equal(canEditDriverCompensation(liveDriver, oldBranchReader, allow), false);
});

test('an unrelated-Branch caller has no Driver, no access and no rows', () => {
  assert.deepEqual(groupRosterByDriver([]), []);
  assert.deepEqual(buildPendingScheduleRows([], {}, []), []);
  assert.equal(canDiscardPendingRow(canDiscardPendingRates(null)), false);
  assert.equal(canEditDriverCompensation(null, null, allow), false);
});

test('a company-wide read-only caller sees Pending rows (retired included) but has no actions', () => {
  const access = companyReaderAfterAllEnded;
  const rows = buildPendingScheduleRows(retiredPendingSummary, retiredPendingHistory, matrix);
  assert.equal(rows.length, 1);
  assert.ok(visibleTabs(access).includes('pending'));
  assert.equal(isPastBranchView(access), false);
  assert.equal(canDiscardPendingRow(canDiscardPendingRates(access)), false);
  assert.equal(canApprovePendingRow(rows[0], canEditDriverCompensation(endedDriver, access, allow)), false);
});

test('a company-wide editor with a current tenure keeps approve, edit and discard for an Active Pending', () => {
  const rows = buildPendingScheduleRows(
    [summary(ACTIVE, { pending_schedule_id: 30 }), summary(RETIRED, { pay_definition_status: 'Retired', pending_schedule_id: 20 })],
    { [ACTIVE]: [schedule(30, ACTIVE, 'Pending', '2026-05-01')], [RETIRED]: retiredPendingHistory[RETIRED] },
    matrix);
  const canEdit = canEditDriverCompensation(liveDriver, editorWithTenure, allow);
  assert.equal(canEdit, true);
  const byId = Object.fromEntries(rows.map((r) => [r.scheduleId, r]));
  assert.equal(canApprovePendingRow(byId[30], canEdit), true);
  assert.equal(canDiscardPendingRow(canDiscardPendingRates(editorWithTenure)), true);
  // The retired one still cannot be approved, even with a live tenure.
  assert.equal(canApprovePendingRow(byId[20], canEdit), false);
});

test('rows are sorted by definition then start, and a definition with no history yields no row', () => {
  const rows = buildPendingScheduleRows(
    [summary(ACTIVE, { pending_schedule_id: 31 }), summary(RETIRED, { pay_definition_status: 'Retired', pending_schedule_id: 20 }), summary(3, { pending_schedule_id: 40 })],
    { [ACTIVE]: [schedule(31, ACTIVE, 'Pending', '2026-06-01'), schedule(30, ACTIVE, 'Pending', '2026-05-01')],
      [RETIRED]: retiredPendingHistory[RETIRED] },
    matrix);
  assert.deepEqual(rows.map((r) => r.scheduleId), [30, 31, 20]);
});

test('the page builds its Pending list from the schedule summary, loaded with the rates', () => {
  const read = (name: string) => readFileSync(
    new URL(`../src/pages/people/pay-rates/${name}`, import.meta.url), 'utf8');
  const page = read('PayRatesPage.tsx');
  assert.match(page, /buildPendingScheduleRows\(\s*reads\.pendingSource\.summary, reads\.pendingSource\.historyByDefinition, reads\.rows\)/);
  assert.doesNotMatch(page, /reads\.rows\.filter\(\(r\) => r\.pending/);
  const reads = read('usePayRateReads.ts');
  assert.match(reads, /loadPendingSource\(driverId\)\]\)/); // loaded with the rates, not only for History
  assert.match(reads, /listDriverRateSummary\(driverId\)/);
  const pending = read('PendingRateChanges.tsx');
  assert.match(pending, /canApprovePendingRow\(row, canEditDriver\)/);
  assert.match(pending, /canDiscardPendingRow\(canDiscardDriver\)/);
});
