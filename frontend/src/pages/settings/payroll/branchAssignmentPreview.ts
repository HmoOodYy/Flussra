/**
 * Pure branch assignment / reassignment / withdrawal model for the
 * company-owned Payroll Setups page (Phase 6 U5c).
 *
 * PURE MODULE: no React, no apiClient, `import type` only for API types.
 * Never constructs a JS Date object or reads the wall clock — ISO date
 * strings are handled as strings only. Never computes assignment legality,
 * continuity, or withdrawal eligibility — the backend is the only source of
 * truth for what is allowed; this module only shapes requests and formats
 * backend-supplied facts for display.
 *
 * A reassignment may only be submitted using a preview whose inputs still
 * match the current form state exactly (branch, destination Setup, and the
 * effective date). Any change to one of those three inputs makes a
 * previously-fetched preview stale and Reassign must be disabled until
 * "Preview impact" is run again — mirroring publishPreview.ts. The reason
 * text is deliberately NOT part of the staleness inputs.
 */
import type {
  AssignmentCreateRequest,
  AssignmentResponse,
  ReassignmentImpactRequest,
  ReassignmentImpactResponse,
  ReassignmentRequest,
  SetupResponse,
  WithdrawalRequest,
} from '../../../types/payrollSetup';

// ── Reason normalization ────────────────────────────────────────────────────

/** Empty/whitespace-only reason becomes null; otherwise sent verbatim (no trimming). */
export function normalizeReason(text: string): string | null {
  return text.trim() === '' ? null : text;
}

// ── Initial assignment ──────────────────────────────────────────────────────

export function buildAssignmentRequest(
  setupId: number,
  effectiveFromDate: string,
  reasonText: string,
): AssignmentCreateRequest {
  return {
    setup_id: setupId,
    effective_from_date: effectiveFromDate,
    reason: normalizeReason(reasonText),
  };
}

// ── Reassignment — inputs / stored preview ──────────────────────────────────

export interface ReassignInputs {
  branchId: number;
  destinationSetupId: number;
  effectiveFromDate: string;
}

export interface StoredReassignPreview {
  inputs: ReassignInputs;
  response: ReassignmentImpactResponse;
}

export function sameReassignInputs(a: ReassignInputs, b: ReassignInputs): boolean {
  return (
    a.branchId === b.branchId &&
    a.destinationSetupId === b.destinationSetupId &&
    a.effectiveFromDate === b.effectiveFromDate
  );
}

export function isReassignPreviewCurrent(
  current: ReassignInputs,
  stored: StoredReassignPreview | null,
): boolean {
  return stored != null && sameReassignInputs(current, stored.inputs);
}

export function canReassignFromPreview(
  current: ReassignInputs,
  stored: StoredReassignPreview | null,
): boolean {
  return isReassignPreviewCurrent(current, stored) && stored!.response.allowed === true;
}

// ── Reassignment — request builders ─────────────────────────────────────────

/** Exactly the two keys the backend schema allows (extras are rejected). */
export function buildReassignmentImpactRequest(inputs: ReassignInputs): ReassignmentImpactRequest {
  return {
    destination_setup_id: inputs.destinationSetupId,
    effective_from_date: inputs.effectiveFromDate,
  };
}

export function buildReassignmentRequest(
  inputs: ReassignInputs,
  reasonText: string,
): ReassignmentRequest {
  return {
    destination_setup_id: inputs.destinationSetupId,
    effective_from_date: inputs.effectiveFromDate,
    reason: normalizeReason(reasonText),
  };
}

// ── Withdrawal — request builder ────────────────────────────────────────────

export function buildWithdrawalRequest(reasonText: string): WithdrawalRequest {
  return { reason: normalizeReason(reasonText) };
}

// ── Display labels ──────────────────────────────────────────────────────────

export function setupLabel(setupId: number | null, setups: readonly SetupResponse[]): string {
  if (setupId === null) return 'None';
  const found = setups.find((s) => s.setup_id === setupId);
  return found ? `${found.setup_code} — ${found.setup_name}` : `Setup #${setupId}`;
}

export function branchLabel(
  branchId: number,
  branches: readonly { branch_id: number; branch_name: string; branch_code: string }[],
): string {
  const found = branches.find((b) => b.branch_id === branchId);
  return found ? `${found.branch_name} (${found.branch_code})` : `Branch #${branchId}`;
}

/** Active Setups only, in their original order — no client-side reordering. */
export function activeSetupOptions(setups: readonly SetupResponse[]): SetupResponse[] {
  return setups.filter((s) => s.status === 'Active');
}

export function assignmentIntervalLabel(a: {
  effective_from_date: string;
  effective_to_date: string | null;
}): string {
  return `${a.effective_from_date} → ${a.effective_to_date ?? 'open-ended'}`;
}

/** Presentation only — never used to infer legality or eligibility. */
export function isWithdrawn(a: AssignmentResponse): boolean {
  return a.withdrawn_at_utc !== null;
}

export const EFFECTIVE_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
