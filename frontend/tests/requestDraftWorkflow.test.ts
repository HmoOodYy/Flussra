import { test } from 'node:test';
import assert from 'node:assert/strict';
import { submitExistingDraft, saveAndSubmitDraft } from '../src/pages/settings/pay-items/requestDraftWorkflow.ts';
import type {
  RequestDraftWorkflowDeps,
  RequestDraftEditFields,
} from '../src/pages/settings/pay-items/requestDraftWorkflow.ts';
import type { PayDefinitionRequestSummary } from '../src/types/compensation.ts';

function makeRequest(overrides: Partial<PayDefinitionRequestSummary> = {}): PayDefinitionRequestSummary {
  return {
    request_id: 'req-1',
    company_id: 1,
    requesting_branch_id: 10,
    definition_name: 'Original Name',
    input_type: 'Decimal',
    unit: null,
    calculation_method: 'PerUnit',
    notes: null,
    status: 'Draft',
    revision: 1,
    approved_pay_definition_id: null,
    copied_from_request_id: null,
    submitted_by_user_id: null,
    submitted_at_utc: null,
    created_by_user_id: 1,
    created_at_utc: '2026-01-01T00:00:00Z',
    updated_by_user_id: null,
    updated_at_utc: null,
    ...overrides,
  };
}

const EDITS: RequestDraftEditFields = {
  definition_name: 'Edited Name',
  input_type: 'Decimal',
  unit: '',
  notes: '',
};

test('submitExistingDraft: submit failure returns saved Draft identity/revision', async () => {
  const draft = makeRequest({ request_id: 'req-1', revision: 2 });
  const submitError = new Error('network down');
  let submitCalledWith: [string, unknown] | null = null;

  const deps: RequestDraftWorkflowDeps = {
    updatePayDefinitionRequest: async () => { throw new Error('update must not be called'); },
    submitPayDefinitionRequest: async (requestId, payload) => {
      submitCalledWith = [requestId, payload];
      throw submitError;
    },
  };

  const result = await submitExistingDraft(deps, draft);

  assert.equal(result.kind, 'draft-saved');
  assert.equal(result.request.request_id, 'req-1');
  assert.equal(result.request.revision, 2);
  assert.equal(result.error, submitError);
  assert.deepEqual(submitCalledWith, ['req-1', { expected_revision: 2 }]);
});

test('saveAndSubmitDraft: submit uses the revision returned by PATCH, not a local increment', async () => {
  const draft = makeRequest({ request_id: 'req-2', revision: 3 });
  const patched = makeRequest({ request_id: 'req-2', revision: 5, definition_name: 'Edited Name' });
  const submitted = makeRequest({
    request_id: 'req-2', revision: 6, status: 'PendingCompanyApproval', definition_name: 'Edited Name',
  });
  let submitCalledWith: [string, unknown] | null = null;

  const deps: RequestDraftWorkflowDeps = {
    updatePayDefinitionRequest: async (requestId, payload) => {
      assert.equal(requestId, 'req-2');
      assert.equal(payload.expected_revision, 3);
      return patched;
    },
    submitPayDefinitionRequest: async (requestId, payload) => {
      submitCalledWith = [requestId, payload];
      return submitted;
    },
  };

  const result = await saveAndSubmitDraft(deps, draft, EDITS);

  assert.equal(result.kind, 'submitted');
  assert.deepEqual(submitCalledWith, ['req-2', { expected_revision: 5 }]);
});

test('saveAndSubmitDraft: submit failure after a successful PATCH returns the updated Draft', async () => {
  const draft = makeRequest({ request_id: 'req-3', revision: 1 });
  const patched = makeRequest({ request_id: 'req-3', revision: 2, definition_name: 'Edited Name' });
  const submitError = new Error('submit boom');

  const deps: RequestDraftWorkflowDeps = {
    updatePayDefinitionRequest: async () => patched,
    submitPayDefinitionRequest: async () => { throw submitError; },
  };

  const result = await saveAndSubmitDraft(deps, draft, EDITS);

  assert.equal(result.kind, 'draft-saved');
  assert.equal(result.request.revision, 2);
  assert.equal(result.request.definition_name, 'Edited Name');
  assert.equal(result.error, submitError);
});

test('saveAndSubmitDraft: PATCH failure propagates and never calls submit', async () => {
  const draft = makeRequest({ request_id: 'req-4', revision: 1 });
  const patchError = new Error('patch boom');
  let submitCalled = false;

  const deps: RequestDraftWorkflowDeps = {
    updatePayDefinitionRequest: async () => { throw patchError; },
    submitPayDefinitionRequest: async () => { submitCalled = true; return draft; },
  };

  await assert.rejects(
    () => saveAndSubmitDraft(deps, draft, EDITS),
    (err: unknown) => err === patchError,
  );
  assert.equal(submitCalled, false);
});
