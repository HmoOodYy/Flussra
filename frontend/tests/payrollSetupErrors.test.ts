import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  readApiError,
  friendlyPolicyMessage,
  friendlyBoundaryConflictMessage,
  friendlyError,
  FRIENDLY_POLICY_MESSAGES,
} from '../src/lib/payrollSetupErrors.ts';

const FALLBACK = 'Something went wrong. Please try again.';

function makeError(status: number, detail: unknown): unknown {
  return { response: { status, data: { detail } } };
}

// ── A: PolicyError envelope ──────────────────────────────────────────────────

test('A. PolicyError envelope {code, message} is preserved verbatim', () => {
  const error = makeError(409, { code: 'SETUP_ASSIGNED', message: 'Setup is assigned to one or more branches.' });
  const result = readApiError(error, FALLBACK);
  assert.deepEqual(result, {
    status: 409,
    code: 'SETUP_ASSIGNED',
    message: 'Setup is assigned to one or more branches.',
  });
});

test('A2. PolicyError envelope with empty message -> fallback message, code kept', () => {
  const error = makeError(409, { code: 'SETUP_ASSIGNED', message: '' });
  const result = readApiError(error, FALLBACK);
  assert.deepEqual(result, { status: 409, code: 'SETUP_ASSIGNED', message: FALLBACK });
});

// ── B: string detail ─────────────────────────────────────────────────────────

test('B. string detail (403) -> code null, message verbatim', () => {
  const error = makeError(403, "Branch is outside the user's access scope.");
  const result = readApiError(error, FALLBACK);
  assert.deepEqual(result, {
    status: 403,
    code: null,
    message: "Branch is outside the user's access scope.",
  });
});

// ── C: FastAPI 422 validation arrays ────────────────────────────────────────

test('C. 422 array with loc [body, field] -> "field: msg", multiple entries joined with "; "', () => {
  const error = makeError(422, [
    { loc: ['body', 'setup_code'], msg: 'String should match pattern', type: 'string_pattern_mismatch' },
    { loc: ['body', 'setup_name'], msg: 'Field required', type: 'missing' },
  ]);
  const result = readApiError(error, FALLBACK);
  assert.deepEqual(result, {
    status: 422,
    code: null,
    message: 'setup_code: String should match pattern; setup_name: Field required',
  });
});

test('C2. 422 array with loc [body] only -> msg without a field prefix', () => {
  const error = makeError(422, [{ loc: ['body'], msg: 'Extra fields not permitted', type: 'extra_forbidden' }]);
  const result = readApiError(error, FALLBACK);
  assert.deepEqual(result, { status: 422, code: null, message: 'Extra fields not permitted' });
});

test('C3. empty 422 array -> fallback', () => {
  const error = makeError(422, []);
  const result = readApiError(error, FALLBACK);
  assert.deepEqual(result, { status: 422, code: null, message: FALLBACK });
});

// ── D: no response at all ───────────────────────────────────────────────────

test('D. network error with no response -> status null, code null, fallback (never reads error.message)', () => {
  const result = readApiError(new Error('Network Error'), FALLBACK);
  assert.deepEqual(result, { status: null, code: null, message: FALLBACK });
});

test('D2. non-object thrown values -> fallback', () => {
  assert.deepEqual(readApiError(undefined, FALLBACK), { status: null, code: null, message: FALLBACK });
  assert.deepEqual(readApiError('x', FALLBACK), { status: null, code: null, message: FALLBACK });
  assert.deepEqual(readApiError(42, FALLBACK), { status: null, code: null, message: FALLBACK });
});

// ── E: unexpected detail shapes ─────────────────────────────────────────────

test('E. detail of unexpected shape (number) -> fallback, status kept', () => {
  const result = readApiError(makeError(500, 12345), FALLBACK);
  assert.deepEqual(result, { status: 500, code: null, message: FALLBACK });
});

test('E2. detail of unexpected shape (object without code) -> fallback, status kept', () => {
  const result = readApiError(makeError(500, { foo: 1 }), FALLBACK);
  assert.deepEqual(result, { status: 500, code: null, message: FALLBACK });
});

// ── F: status extraction ────────────────────────────────────────────────────

test('F. status extracted when present', () => {
  const result = readApiError(makeError(401, 'Unauthorized'), FALLBACK);
  assert.equal(result.status, 401);
});

test('F2. status null when response is absent', () => {
  const result = readApiError({ notResponse: true }, FALLBACK);
  assert.equal(result.status, null);
});

test('F3. status null when response.status is not a number', () => {
  const result = readApiError({ response: { status: 'oops', data: {} } }, FALLBACK);
  assert.equal(result.status, null);
});

// ── G: friendlyPolicyMessage ─────────────────────────────────────────────────

test('G. friendlyPolicyMessage: known code returns the exact table copy', () => {
  assert.equal(
    friendlyPolicyMessage('SETUP_ASSIGNED', 'fallback'),
    "Branches still follow this policy, so it can't be archived yet.",
  );
  assert.equal(
    friendlyPolicyMessage('ONBOARDING_START_TOO_EARLY', 'fallback'),
    'Payroll can start in the current payroll period or up to two periods earlier, not before.',
  );
});

test('G2. friendlyPolicyMessage: unknown code falls back to the caller-supplied text', () => {
  assert.equal(friendlyPolicyMessage('SOMETHING_NEW', 'fallback text'), 'fallback text');
  assert.equal(friendlyPolicyMessage(null, 'fallback text'), 'fallback text');
});

test('G2a. successor chronology conflict uses the server-supplied scheduled date', () => {
  assert.equal(
    friendlyBoundaryConflictMessage(
      'SUCCESSOR_BOUNDARY_INVALID',
      'Existing scheduled update on 2026-10-10 would not start on a valid boundary under this schedule',
      'fallback',
    ),
    'An existing scheduled update on Oct 10, 2026 would no longer start on a valid payroll-period boundary under this schedule.',
  );
});

test('G2b. predecessor chronology conflict remains friendly and date-specific', () => {
  assert.equal(
    friendlyBoundaryConflictMessage(
      'PREDECESSOR_BOUNDARY_INVALID',
      'Effective date 2026-09-27 splits the predecessor payroll period',
      'fallback',
    ),
    'The proposed change on Sep 27, 2026 would split the previous payroll period.',
  );
});

test("G3. friendlyPolicyMessage: '__proto__' and other inherited keys are never treated as known codes", () => {
  for (const code of ['__proto__', 'constructor', 'toString', 'hasOwnProperty']) {
    assert.equal(friendlyPolicyMessage(code, 'fallback'), 'fallback');
  }
});

test('G4. FRIENDLY_POLICY_MESSAGES has exactly the 29 documented codes', () => {
  assert.equal(Object.keys(FRIENDLY_POLICY_MESSAGES).length, 29);
  assert.equal(
    FRIENDLY_POLICY_MESSAGES.DEFAULT_SETUP_NOT_PUBLISHED,
    'Publish a payroll schedule before setting this policy as the default.',
  );
  assert.equal(FRIENDLY_POLICY_MESSAGES.INVALID_NORMAL_DAYS_OFF, 'Choose at most two normal days off.');
});

test('G5. FRIENDLY_POLICY_MESSAGES.INVALID_SETUP_CODE (Unit A: optional Integration code)', () => {
  assert.equal(
    friendlyPolicyMessage('INVALID_SETUP_CODE', 'fallback'),
    'Codes starting with PPOL- are reserved for system-generated references.',
  );
});

// ── H: friendlyError ─────────────────────────────────────────────────────────

test('H. friendlyError: known code -> friendly message, detail carries the distinct server message', () => {
  const info = { status: 409, code: 'SETUP_ASSIGNED', message: 'Setup is assigned to 3 branches.' };
  assert.deepEqual(friendlyError(info), {
    message: "Branches still follow this policy, so it can't be archived yet.",
    code: 'SETUP_ASSIGNED',
    detail: 'Setup is assigned to 3 branches.',
  });
});

test('H2. friendlyError: unknown code -> message falls back to info.message verbatim, no detail duplication', () => {
  const info = { status: 409, code: 'SOME_UNKNOWN_CODE', message: 'A server-authored message.' };
  assert.deepEqual(friendlyError(info), {
    message: 'A server-authored message.',
    code: 'SOME_UNKNOWN_CODE',
    detail: null,
  });
});

test('H3. friendlyError: no code (e.g. plain string detail or network error) -> message verbatim, no detail', () => {
  const info = { status: null, code: null, message: 'Network Error fallback' };
  assert.deepEqual(friendlyError(info), {
    message: 'Network Error fallback',
    code: null,
    detail: null,
  });
});

test('H4. friendlyError: review errors preserve the server-supplied successor date in friendly copy', () => {
  const info = {
    status: null,
    code: 'SUCCESSOR_BOUNDARY_INVALID',
    message: 'Existing scheduled update on 2026-10-10 would not start on a valid boundary under this schedule',
  };
  assert.deepEqual(friendlyError(info), {
    message: 'An existing scheduled update on Oct 10, 2026 would no longer start on a valid payroll-period boundary under this schedule.',
    code: 'SUCCESSOR_BOUNDARY_INVALID',
    detail: info.message,
  });
});
