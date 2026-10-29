import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  ACCESS_SAFETY_ERROR_MESSAGES,
  accessSafetyMessage,
} from '../src/lib/accessSafetyErrors.ts';
import { apiError as peopleApiError } from '../src/pages/people/peoplePresentation.ts';
import { apiError as rolesApiError } from '../src/pages/settings/roles/rolesPageState.ts';

function refusal(detail: unknown) {
  return { response: { status: 403, data: { detail } } };
}

test('each access-safety code maps to its own friendly message', () => {
  for (const [code, message] of Object.entries(ACCESS_SAFETY_ERROR_MESSAGES)) {
    assert.equal(accessSafetyMessage(refusal({ code, message: 'technical text' })), message);
  }
});

test('a grant-ceiling refusal lists the permissions the person cannot grant by their friendly names', () => {
  const message = accessSafetyMessage(refusal({
    code: 'GRANT_CEILING_EXCEEDED',
    message: 'technical text',
    permission_codes: ['payroll.approve', 'users.edit'],
  }));
  assert.ok(message?.startsWith('You can only give access that you hold yourself.'));
  assert.ok(message?.includes('Approve Payroll'));
  assert.ok(message?.includes('Edit Members'));
  assert.ok(!message?.includes('payroll.approve'));
});

test('errors without a known access-safety code are left to the caller', () => {
  assert.equal(accessSafetyMessage(refusal('Plain string detail.')), null);
  assert.equal(accessSafetyMessage(refusal({ code: 'SOMETHING_ELSE', message: 'x' })), null);
  assert.equal(accessSafetyMessage(refusal([{ msg: 'bad' }])), null);
  assert.equal(accessSafetyMessage(new Error('network')), null);
});

test('both admin error readers show the friendly access-safety wording', () => {
  const err = refusal({ code: 'SELF_ACCESS_CHANGE_FORBIDDEN', message: 'technical text' });
  const friendly = ACCESS_SAFETY_ERROR_MESSAGES.SELF_ACCESS_CHANGE_FORBIDDEN;
  assert.equal(peopleApiError(err), friendly);
  assert.equal(rolesApiError(err), friendly);
});

test('both admin error readers still show plain string details unchanged', () => {
  assert.equal(peopleApiError(refusal('Employee not found.')), 'Employee not found.');
  assert.equal(rolesApiError(refusal('Employee not found.')), 'Employee not found.');
});
