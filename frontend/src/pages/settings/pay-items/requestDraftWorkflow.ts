import type {
  PayDefinitionInputType,
  PayDefinitionRequestSubmitPayload,
  PayDefinitionRequestSummary,
  PayDefinitionRequestUpdatePayload,
} from '../../../types/compensation';

export interface RequestDraftWorkflowDeps {
  updatePayDefinitionRequest: (
    requestId: string, payload: PayDefinitionRequestUpdatePayload,
  ) => Promise<PayDefinitionRequestSummary>;
  submitPayDefinitionRequest: (
    requestId: string, payload: PayDefinitionRequestSubmitPayload,
  ) => Promise<PayDefinitionRequestSummary>;
}

export interface RequestDraftEditFields {
  definition_name: string;
  input_type: PayDefinitionInputType;
  unit: string;
  notes: string;
}

export type RequestDraftWorkflowResult =
  | { kind: 'submitted'; request: PayDefinitionRequestSummary }
  | { kind: 'draft-saved'; request: PayDefinitionRequestSummary; error: unknown };

export async function submitExistingDraft(
  deps: RequestDraftWorkflowDeps,
  draft: PayDefinitionRequestSummary,
): Promise<RequestDraftWorkflowResult> {
  try {
    const submitted = await deps.submitPayDefinitionRequest(draft.request_id, {
      expected_revision: draft.revision,
    });
    return { kind: 'submitted', request: submitted };
  } catch (error) {
    return { kind: 'draft-saved', request: draft, error };
  }
}

export async function saveAndSubmitDraft(
  deps: RequestDraftWorkflowDeps,
  draft: PayDefinitionRequestSummary,
  edits: RequestDraftEditFields,
): Promise<RequestDraftWorkflowResult> {
  const updated = await deps.updatePayDefinitionRequest(draft.request_id, {
    expected_revision: draft.revision,
    definition_name: edits.definition_name,
    input_type: edits.input_type,
    unit: edits.unit,
    notes: edits.notes,
  });
  return submitExistingDraft(deps, updated);
}
