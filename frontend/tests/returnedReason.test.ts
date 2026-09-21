import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  RETURN_REASON_NOT_RECORDED_MESSAGE,
  RETURN_REASON_UNAVAILABLE_MESSAGE,
  resolveReturnReasonDisplay,
  shouldFetchReturnReason,
} from '../src/pages/payroll/returnedReason.ts';
import type { ReviewItemDetail } from '../src/types/review.ts';

function makeDetail(overrides: Partial<ReviewItemDetail> = {}): ReviewItemDetail {
  return {
    review_item_id: 501,
    company_id: 1,
    branch_id: 10,
    branch_name: 'Branch A',
    requested_by_user_id: 7,
    requested_by: 'Ada Operator',
    request_type: 'PeriodApproval',
    entity_schema: 'payroll',
    entity_name: 'PayrollPeriods',
    entity_id: '900',
    title: 'Payroll approval — Branch A',
    description: null,
    status: 'EditRequested',
    priority: 'Normal',
    created_at_utc: '2026-01-10T00:00:00Z',
    due_at_utc: null,
    final_decision_by_user_id: 3,
    final_decision_by: 'Manager Mo',
    final_decision_at_utc: '2026-01-11T00:00:00Z',
    final_decision_reason: 'Mileage for driver 42 looks wrong on 2026-01-08.',
    old_value_json: null,
    new_value_json: null,
    decisions: [],
    ...overrides,
  };
}

// ── shouldFetchReturnReason ──────────────────────────────────────────────────
// Note: this only decides whether a NETWORK FETCH is attempted. It is
// deliberately narrower than "should something be displayed" —
// resolveReturnReasonDisplay shows a visible message for Returned periods
// even when shouldFetchReturnReason is false (no review item id to fetch).

test('shouldFetchReturnReason: true only when Returned and a review item id is present', () => {
  assert.equal(shouldFetchReturnReason(true, 501), true);
  assert.equal(shouldFetchReturnReason(true, null), false);
  assert.equal(shouldFetchReturnReason(false, 501), false);
  assert.equal(shouldFetchReturnReason(false, null), false);
});

// ── resolveReturnReasonDisplay ───────────────────────────────────────────────
// Scenario 1: Returned + valid fetched reason -> reason displayed.

test('1. Returned + valid review-item ID, fetch succeeded -> reason displayed verbatim', () => {
  const detail = makeDetail({ final_decision_reason: 'Driver 42 mileage looks wrong on 2026-01-08.' });
  const result = resolveReturnReasonDisplay(true, 501, false, false, detail);
  assert.deepEqual(result, {
    kind: 'ready',
    message: 'Driver 42 mileage looks wrong on 2026-01-08.',
  });
});

// Scenario 2: Returned + blank canonical reason -> neutral "not recorded" fallback.

test('2. Returned + valid review-item ID, fetch succeeded but reason text is blank -> "No reason was recorded", not fabricated', () => {
  const detail = makeDetail({ final_decision_reason: null });
  const result = resolveReturnReasonDisplay(true, 501, false, false, detail);
  assert.deepEqual(result, { kind: 'ready', message: RETURN_REASON_NOT_RECORDED_MESSAGE });
});

test('2b. Returned + blank reason is whitespace-only -> still treated as not recorded, not shown as blank text', () => {
  const detail = makeDetail({ final_decision_reason: '   ' });
  const result = resolveReturnReasonDisplay(true, 501, false, false, detail);
  assert.deepEqual(result, { kind: 'ready', message: RETURN_REASON_NOT_RECORDED_MESSAGE });
});

test('Returned + valid review-item ID, fetch in flight -> loading state, no crash', () => {
  const result = resolveReturnReasonDisplay(true, 501, true, false, null);
  assert.deepEqual(result, { kind: 'loading' });
});

// Scenario 3: Returned + expected review item but fetch failure -> visible
// "Return reason unavailable." — must NOT silently render nothing.

test('3. Returned + valid review-item ID, fetch failed -> visible "Return reason unavailable.", never silent', () => {
  const result = resolveReturnReasonDisplay(true, 501, false, true, null);
  assert.deepEqual(result, { kind: 'unavailable' });
  assert.notEqual(result.kind, 'none', 'a fetch failure on a Returned period must never resolve to the silent "none" state');
});

// Scenario 4: Returned + malformed/unavailable review context (no linked
// review item id at all) -> same safe neutral unavailable fallback, still
// visible — this used to silently render nothing and no longer does.

test('4. Returned + missing review-item ID -> visible "Return reason unavailable.", not silent, not fabricated', () => {
  const result = resolveReturnReasonDisplay(true, null, false, false, null);
  assert.deepEqual(result, { kind: 'unavailable' });
});

test('Returned + missing review-item ID takes priority over loading/errored/detail flags (they are meaningless without an id)', () => {
  assert.deepEqual(resolveReturnReasonDisplay(true, null, true, true, makeDetail()), { kind: 'unavailable' });
});

// Scenario 5: non-Returned -> render nothing. This is the ONLY case that
// resolves to 'none'.

test('5. Not Returned -> none (the only silent case), regardless of review item id or fetch state', () => {
  assert.deepEqual(resolveReturnReasonDisplay(false, 501, false, false, makeDetail()), { kind: 'none' });
  assert.deepEqual(resolveReturnReasonDisplay(false, null, false, false, null), { kind: 'none' });
  assert.deepEqual(resolveReturnReasonDisplay(false, 501, false, true, null), { kind: 'none' });
});

// ── Message distinctness ─────────────────────────────────────────────────────

test('RETURN_REASON_UNAVAILABLE_MESSAGE and RETURN_REASON_NOT_RECORDED_MESSAGE are distinct, non-fabricated, neutral strings', () => {
  assert.notEqual(RETURN_REASON_UNAVAILABLE_MESSAGE, RETURN_REASON_NOT_RECORDED_MESSAGE);
  assert.equal(RETURN_REASON_UNAVAILABLE_MESSAGE, 'Return reason unavailable.');
});
