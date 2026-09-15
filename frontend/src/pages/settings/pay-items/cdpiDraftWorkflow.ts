import type {
  CdpiRequestSummary,
  CdpiRequestUpdatePayload,
  CdpiSubmitPayload,
  CdpiInputType,
} from '../../../types/settings';

export interface CdpiDraftWorkflowDeps {
  updateCdpiRequest: (requestId: string, payload: CdpiRequestUpdatePayload) => Promise<CdpiRequestSummary>;
  submitCdpiRequest: (requestId: string, payload: CdpiSubmitPayload) => Promise<CdpiRequestSummary>;
}

export interface CdpiDraftEditFields {
  item_name: string;
  input_type: CdpiInputType;
  unit: string;
  notes: string;
}

export type CdpiDraftWorkflowResult =
  | { kind: 'submitted'; request: CdpiRequestSummary }
  | { kind: 'draft-saved'; request: CdpiRequestSummary; error: unknown };

export async function submitExistingDraft(
  deps: CdpiDraftWorkflowDeps,
  draft: CdpiRequestSummary,
): Promise<CdpiDraftWorkflowResult> {
  try {
    const submitted = await deps.submitCdpiRequest(draft.request_id, {
      expected_revision: draft.revision,
    });
    return { kind: 'submitted', request: submitted };
  } catch (error) {
    return { kind: 'draft-saved', request: draft, error };
  }
}

export async function saveAndSubmitDraft(
  deps: CdpiDraftWorkflowDeps,
  draft: CdpiRequestSummary,
  edits: CdpiDraftEditFields,
): Promise<CdpiDraftWorkflowResult> {
  const updated = await deps.updateCdpiRequest(draft.request_id, {
    expected_revision: draft.revision,
    item_name: edits.item_name,
    input_type: edits.input_type,
    unit: edits.unit,
    notes: edits.notes,
  });
  return submitExistingDraft(deps, updated);
}
