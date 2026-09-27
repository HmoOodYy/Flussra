/**
 * Pure preview-first publication model for the company-owned Payroll Setups
 * page (Phase 6 U5b).
 *
 * PURE MODULE: no React, no apiClient, `import type` only for API types.
 * Never constructs a JS Date object or reads the wall clock — ISO date
 * strings are handled as strings only.
 *
 * A Draft may only be published using a preview whose inputs still match the
 * current form state exactly (setup, draft, its schedule, the effective
 * date, and any same-date correction choice). Any change to one of those
 * five inputs makes a previously-fetched preview stale and Publish must be
 * disabled until "Preview impact" is run again.
 */
import type {
  InlinePublicationRequest,
  InlineScheduleRequest,
  PublicationImpactRequest,
  PublicationImpactResponse,
  PublishRequest,
} from '../../../types/payrollSetup';

// ── Inputs / stored preview ─────────────────────────────────────────────────

export interface PreviewInputs {
  setupId: number;
  draftId: number;
  draftScheduleKey: string;
  effectiveFromDate: string;
  replacesVersionId: number | null;
}

export interface StoredPreview {
  inputs: PreviewInputs;
  response: PublicationImpactResponse;
}

export interface InlinePreviewInputs {
  setupId: number;
  schedule: InlineScheduleRequest;
  scheduleKey: string;
  effectiveFromDate: string;
  replacesVersionId: number | null;
}

export function sameInlineInputs(a: InlinePreviewInputs, b: InlinePreviewInputs): boolean {
  return (
    a.setupId === b.setupId &&
    a.scheduleKey === b.scheduleKey &&
    a.effectiveFromDate === b.effectiveFromDate &&
    a.replacesVersionId === b.replacesVersionId &&
    JSON.stringify(a.schedule) === JSON.stringify(b.schedule)
  );
}

export function canPublishInlineFromPreview(
  current: InlinePreviewInputs,
  stored: { inputs: InlinePreviewInputs; response: PublicationImpactResponse } | null,
): boolean {
  return stored != null && sameInlineInputs(current, stored.inputs) && stored.response.allowed === true;
}

export function sameInputs(a: PreviewInputs, b: PreviewInputs): boolean {
  return (
    a.setupId === b.setupId &&
    a.draftId === b.draftId &&
    a.draftScheduleKey === b.draftScheduleKey &&
    a.effectiveFromDate === b.effectiveFromDate &&
    a.replacesVersionId === b.replacesVersionId
  );
}

export function isPreviewCurrent(current: PreviewInputs, stored: StoredPreview | null): boolean {
  return stored != null && sameInputs(current, stored.inputs);
}

export function canPublishFromPreview(
  current: PreviewInputs,
  stored: StoredPreview | null,
): boolean {
  return isPreviewCurrent(current, stored) && stored!.response.allowed === true;
}

/**
 * The ONLY source of a same-date replacement candidate: the backend's
 * current_same_date_version_id. Never derived from any other field.
 */
export function correctionCandidate(response: PublicationImpactResponse): number | null {
  return response.current_same_date_version_id;
}

// ── Request builders ────────────────────────────────────────────────────────

export function buildImpactRequest(inputs: PreviewInputs): PublicationImpactRequest {
  return {
    effective_from_date: inputs.effectiveFromDate,
    replaces_version_id: inputs.replacesVersionId,
  };
}

export function buildPublishRequest(inputs: PreviewInputs): PublishRequest {
  return {
    effective_from_date: inputs.effectiveFromDate,
    replaces_version_id: inputs.replacesVersionId,
  };
}

export function buildInlinePublicationRequest(inputs: InlinePreviewInputs): InlinePublicationRequest {
  return {
    ...inputs.schedule,
    effective_from_date: inputs.effectiveFromDate,
    replaces_version_id: inputs.replacesVersionId,
  };
}

// ── Affected branch labels ──────────────────────────────────────────────────

export function affectedBranchLabels(
  ids: readonly number[],
  branches: readonly { branch_id: number; branch_name: string }[],
): string[] {
  return ids.map((id) => {
    const found = branches.find((b) => b.branch_id === id);
    return found ? found.branch_name : `Branch #${id}`;
  });
}
