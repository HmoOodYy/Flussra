import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { DriverTransferRequest, TransferStatus } from '../src/types/transfer.ts';
import { FILTER_LABELS, apiError, matchesFilter, normalizeUtcDateTimeLocal, fmtEffectiveAt } from '../src/pages/people/transfers/transferRequestsView.ts';
import { buildTimeline } from '../src/pages/people/transfers/transferTimeline.ts';

const statuses: TransferStatus[] = [
  'PendingSourceApproval', 'PendingTargetApproval', 'Returned', 'Rejected',
  'Approved', 'Completed', 'Cancelled',
];
const filters = ['all', 'active', 'pending_source', 'pending_target', 'returned', 'approved', 'completed', 'terminal'] as const;

function request(status: TransferStatus, initiated_by: DriverTransferRequest['initiated_by'] = 'Driver', overrides: Partial<DriverTransferRequest> = {}): DriverTransferRequest {
  return {
    transfer_request_id: 41,
    company_id: 3,
    driver_id: 9,
    source_branch_id: 12,
    target_branch_id: 15,
    requested_by_user_id: 22,
    initiated_by,
    status,
    effective_at: null,
    reason: null,
    notes: null,
    source_approved_by_user_id: null,
    source_approved_at_utc: null,
    target_decided_by_user_id: null,
    target_decided_at_utc: null,
    target_decision_notes: null,
    new_driver_id: null,
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

test('transfer filter contract covers all eight filters over all seven statuses', () => {
  const expected: Record<(typeof filters)[number], TransferStatus[]> = {
    all: statuses,
    active: ['PendingSourceApproval', 'PendingTargetApproval', 'Returned', 'Approved'],
    pending_source: ['PendingSourceApproval'],
    pending_target: ['PendingTargetApproval'],
    returned: ['Returned'],
    approved: ['Approved'],
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
    Completed: ['Transfer Completed', 'done'],
    Cancelled: ['Request Cancelled', 'done'],
  };
  const expectedTarget: Record<TransferStatus, [string, string]> = {
    PendingSourceApproval: ['Target Branch Decision', 'pending'],
    PendingTargetApproval: ['Awaiting Target Decision', 'active'],
    Returned: ['Target Returned Request', 'done'],
    Rejected: ['Rejected by Target', 'done'],
    Approved: ['Target Approved', 'done'],
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
