import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { DriverTransferRequest, TransferStatus } from '../src/types/transfer.ts';
import { FILTER_LABELS, apiError, matchesFilter, normalizeUtcDateTimeLocal, fmtEffectiveAt } from '../src/pages/people/transfers/transferRequestsView.ts';
import { buildTimeline } from '../src/pages/people/transfers/transferTimeline.ts';
import {
  canCancelRequest, canCompleteRequest, canRescheduleRequest, driverLabel, isFutureScheduled,
} from '../src/pages/people/transfers/transferActions.ts';
import { statusKind, statusLabel, TRANSFER_ACTIVE, TRANSFER_TERMINAL } from '../src/types/transfer.ts';

const statuses: TransferStatus[] = [
  'PendingSourceApproval', 'PendingTargetApproval', 'Returned', 'Rejected',
  'Approved', 'Scheduled', 'Completed', 'Cancelled',
];
const filters = ['all', 'active', 'pending_source', 'pending_target', 'returned', 'approved', 'scheduled', 'completed', 'terminal'] as const;

function request(status: TransferStatus, initiated_by: DriverTransferRequest['initiated_by'] = 'Driver', overrides: Partial<DriverTransferRequest> = {}): DriverTransferRequest {
  return {
    transfer_request_id: 41,
    company_id: 3,
    driver_id: 9,
    driver_number: 'D-0009',
    source_driver_assignment_id: 71,
    destination_driver_assignment_id: null,
    source_branch_id: 12,
    target_branch_id: 15,
    requested_by_user_id: 22,
    initiated_by,
    status,
    stored_status: status === 'Completed' ? 'Scheduled' : status,
    effective_at: null,
    reason: null,
    notes: null,
    source_approved_by_user_id: null,
    source_approved_at_utc: null,
    target_decided_by_user_id: null,
    target_decided_at_utc: null,
    target_decision_notes: null,
    scheduled_at_utc: null,
    scheduled_by_user_id: null,
    completed_at_utc: null,
    completed_by_user_id: null,
    cancelled_at_utc: null,
    cancelled_by_user_id: null,
    cancel_reason: null,
    created_at_utc: '2026-10-17T12:00:00Z',
    updated_at_utc: null,
    driver_name: 'Casey Driver',
    source_branch_name: 'North',
    target_branch_name: 'South',
    requested_by_name: null,
    ...overrides,
  };
}

test('transfer filter contract covers all nine filters over all eight statuses', () => {
  const expected: Record<(typeof filters)[number], TransferStatus[]> = {
    all: statuses,
    active: ['PendingSourceApproval', 'PendingTargetApproval', 'Returned', 'Approved', 'Scheduled'],
    pending_source: ['PendingSourceApproval'],
    pending_target: ['PendingTargetApproval'],
    returned: ['Returned'],
    approved: ['Approved'],
    scheduled: ['Scheduled'],
    completed: ['Completed'],
    terminal: ['Rejected', 'Completed', 'Cancelled'],
  };
  assert.deepEqual(Object.keys(FILTER_LABELS), filters);
  for (const filter of filters) {
    for (const status of statuses) {
      assert.equal(matchesFilter(request(status), filter), expected[filter].includes(status), `${filter}/${status}`);
    }
  }
});

test('transfer API error formatter preserves detail, validation-array, empty-array, and fallback behavior', () => {
  assert.equal(apiError({ response: { data: { detail: '  backend message  ' } } }), '  backend message  ');
  assert.equal(apiError({ response: { data: { detail: [{ msg: 'invalid date' }, { msg: 'bad branch' }, null, { other: 'skip' }] } } }), 'invalid date bad branch');
  assert.equal(apiError({ response: { data: { detail: [] } } }), '');
  assert.equal(apiError({ response: { data: { detail: '  ' } } }), 'An unexpected error occurred.');
  assert.equal(apiError(new Error('network')), 'An unexpected error occurred.');
});

test('transfer time input is optional, explicitly UTC, and rejects invalid calendar/time values', () => {
  assert.equal(request('PendingTargetApproval').effective_at, null);
  assert.equal(fmtEffectiveAt(null), 'On completion');
  const originalTz = process.env.TZ;
  try {
    for (const tz of ['America/New_York', 'America/Los_Angeles', 'Asia/Tokyo']) {
      process.env.TZ = tz;
      assert.equal(normalizeUtcDateTimeLocal('2026-11-01T09:30'), '2026-11-01T09:30:00Z');
    }
  } finally {
    if (originalTz === undefined) delete process.env.TZ;
    else process.env.TZ = originalTz;
  }
  assert.equal(normalizeUtcDateTimeLocal('2026-02-30T09:30'), null);
  assert.equal(normalizeUtcDateTimeLocal('2026-11-01T24:00'), null);
  assert.equal(normalizeUtcDateTimeLocal('2026-11-01T09:60'), null);
});

test('transfer timeline keeps step order, labels, and states for both initiators and every status', () => {
  const expectedCompletion: Record<TransferStatus, [string, string]> = {
    PendingSourceApproval: ['Complete Transfer', 'pending'],
    PendingTargetApproval: ['Complete Transfer', 'pending'],
    Returned: ['Complete Transfer', 'pending'],
    Rejected: ['Not Completed', 'skipped'],
    Approved: ['Ready to Complete', 'active'],
    Scheduled: ['Transfer Scheduled', 'active'],
    Completed: ['Transfer Completed', 'done'],
    Cancelled: ['Request Cancelled', 'done'],
  };
  const expectedTarget: Record<TransferStatus, [string, string]> = {
    PendingSourceApproval: ['Target Branch Decision', 'pending'],
    PendingTargetApproval: ['Awaiting Target Decision', 'active'],
    Returned: ['Target Returned Request', 'done'],
    Rejected: ['Rejected by Target', 'done'],
    Approved: ['Target Approved', 'done'],
    Scheduled: ['Target Approved', 'done'],
    Completed: ['Target Approved', 'done'],
    Cancelled: ['Target Approved', 'done'],
  };
  for (const status of statuses) {
    for (const initiated_by of ['Driver', 'SourceBranch'] as const) {
      const steps = buildTimeline(request(status, initiated_by));
      assert.deepEqual(steps.map(({ label, state }) => [label, state]), [
        ['Request Created', 'done'],
        initiated_by === 'SourceBranch'
          ? ['Source Branch Approved', 'done']
          : status === 'PendingSourceApproval'
            ? ['Awaiting Source Approval', 'active']
            : status === 'Returned'
              ? ['Source Approval — Returned', 'active']
              : ['Source Branch Approved', 'done'],
        expectedTarget[status],
        expectedCompletion[status],
      ], `${initiated_by}/${status}`);
      assert.equal(steps.length, 4);
    }
  }
});

test('timeline keeps implicit approval provenance and missing timestamp fallbacks', () => {
  const branchInitiated = buildTimeline(request('PendingTargetApproval', 'SourceBranch'));
  assert.equal(branchInitiated[1].sublabel, 'Branch-initiated (implicit approval)');
  const branchWithTimestamp = buildTimeline(request('Approved', 'SourceBranch', { source_approved_at_utc: '2026-10-17T13:00:00Z' }));
  assert.match(branchWithTimestamp[1].sublabel ?? '', /branch-initiated/);

  const driverCancelled = buildTimeline(request('Cancelled', 'Driver'));
  assert.equal(driverCancelled[1].state, 'done');
  assert.equal(driverCancelled[1].sublabel, null);
  assert.equal(driverCancelled[2].state, 'done');
  assert.equal(driverCancelled[2].sublabel, null);
  assert.equal(driverCancelled[3].sublabel, null);

  const createdBy = buildTimeline(request('PendingSourceApproval', 'Driver', { requested_by_name: 'Avery' }));
  assert.match(createdBy[0].sublabel ?? '', / by Avery$/);
});

const NOW = Date.parse('2026-10-18T12:00:00Z');
const FUTURE = '2026-12-01T09:00:00Z';
const PAST = '2026-10-01T09:00:00Z';

test('Scheduled is a distinct label and badge kind from Completed', () => {
  assert.equal(statusLabel('Scheduled'), 'Scheduled');
  assert.equal(statusLabel('Completed'), 'Completed');
  assert.equal(statusKind('Scheduled'), 'scheduled');
  assert.equal(statusKind('Completed'), 'done');
  assert.notEqual(statusKind('Scheduled'), statusKind('Completed'));
  // A Scheduled request still needs attention; a Completed one is closed.
  assert.ok(TRANSFER_ACTIVE.has('Scheduled'));
  assert.ok(!TRANSFER_TERMINAL.has('Scheduled'));
  assert.ok(TRANSFER_TERMINAL.has('Completed'));
});

test('Scheduled timeline shows when it takes effect; a Scheduled request the server reports Completed shows its EffectiveAt', () => {
  const scheduled = buildTimeline(request('Scheduled', 'SourceBranch', { effective_at: FUTURE, scheduled_at_utc: '2026-10-17T13:00:00Z' }));
  assert.equal(scheduled[3].label, 'Transfer Scheduled');
  assert.match(scheduled[3].sublabel ?? '', /^Takes effect /);
  const elapsed = buildTimeline(request('Completed', 'SourceBranch', { effective_at: PAST, completed_at_utc: null }));
  assert.equal(elapsed[3].label, 'Transfer Completed');
  assert.ok(elapsed[3].sublabel);
});

test('Cancel and Reschedule are offered only for a Scheduled request whose EffectiveAt is still ahead', () => {
  const future = request('Scheduled', 'SourceBranch', { effective_at: FUTURE });
  assert.equal(isFutureScheduled(future, NOW), true);
  assert.equal(canRescheduleRequest(future, NOW), true);
  assert.equal(canCancelRequest(future, NOW), true);

  // A page left open past the instant stops offering both, without promoting the status itself.
  const stale = request('Scheduled', 'SourceBranch', { effective_at: PAST });
  assert.equal(stale.status, 'Scheduled');
  assert.equal(canRescheduleRequest(stale, NOW), false);
  assert.equal(canCancelRequest(stale, NOW), false);

  // Once the server reports Completed, nothing is offered.
  const completed = request('Completed', 'SourceBranch', { effective_at: PAST });
  assert.equal(canRescheduleRequest(completed, NOW), false);
  assert.equal(canCancelRequest(completed, NOW), false);

  // A Scheduled request without an instant cannot be rescheduled.
  assert.equal(canRescheduleRequest(request('Scheduled', 'SourceBranch', { effective_at: null }), NOW), false);
});

test('open requests keep Cancel, never offer Reschedule, and only Approved offers Complete', () => {
  for (const status of statuses) {
    const req = request(status, 'SourceBranch', { effective_at: FUTURE });
    const terminal = status === 'Rejected' || status === 'Completed' || status === 'Cancelled';
    assert.equal(canCancelRequest(req, NOW), !terminal, `cancel/${status}`);
    assert.equal(canRescheduleRequest(req, NOW), status === 'Scheduled', `reschedule/${status}`);
    assert.equal(canCompleteRequest(req), status === 'Approved', `complete/${status}`);
  }
});

test('transfer identity shows the Driver number, not an assignment or profile id', () => {
  assert.equal(driverLabel(request('Approved')), 'Casey Driver (D-0009)');
  assert.equal(driverLabel(request('Approved', 'Driver', { driver_number: null })), 'Casey Driver');
  assert.equal(driverLabel(request('Approved', 'Driver', { driver_name: null })), 'D-0009');
  assert.equal(driverLabel(request('Approved', 'Driver', { driver_name: null, driver_number: null })), 'Driver');
});
