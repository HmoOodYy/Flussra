/**
 * Typed wrappers for the company-owned Payroll Setup API.
 *
 * This module is the ONLY frontend boundary for /payroll-setup. All routes
 * are mounted at that prefix on the backend (see
 * backend/app/payroll_setup/router.py). Mutations are not retried and errors
 * are not swallowed here — callers handle failures (see payrollSetupErrors.ts).
 * Do not resolve authority/eligibility client-side; the backend is the only
 * source of truth for what is allowed.
 */
import apiClient from './apiClient';
import type {
  AssignmentCreateRequest,
  AssignmentResponse,
  BoundaryChoicesResponse,
  BranchHistoryResponse,
  BranchPolicySummaryResponse,
  DefaultSetupRequest,
  DefaultSetupResponse,
  DraftCreateRequest,
  DraftResponse,
  DraftUpdateRequest,
  EffectiveAuthorityResponse,
  InlinePublicationRequest,
  InlineScheduleRequest,
  PublicationImpactRequest,
  PublicationImpactResponse,
  PublishRequest,
  ReassignmentImpactRequest,
  ReassignmentImpactResponse,
  ReassignmentRequest,
  SetupCreateRequest,
  SetupResponse,
  SetupUpdateRequest,
  VersionResponse,
  WithdrawalRequest,
} from '../types/payrollSetup';
import type { OnboardingOptionsResponse } from '../types/settings';

// ── Setups ──────────────────────────────────────────────────────────────────

export async function listPayrollSetups(): Promise<SetupResponse[]> {
  const resp = await apiClient.get<SetupResponse[]>('/payroll-setup/setups');
  return resp.data;
}

export async function createPayrollSetup(body: SetupCreateRequest): Promise<SetupResponse> {
  const resp = await apiClient.post<SetupResponse>('/payroll-setup/setups', body);
  return resp.data;
}

export async function getPayrollSetup(setupId: number): Promise<SetupResponse> {
  const resp = await apiClient.get<SetupResponse>(`/payroll-setup/setups/${setupId}`);
  return resp.data;
}

export async function updatePayrollSetup(
  setupId: number,
  body: SetupUpdateRequest,
): Promise<SetupResponse> {
  const resp = await apiClient.put<SetupResponse>(`/payroll-setup/setups/${setupId}`, body);
  return resp.data;
}

export async function archivePayrollSetup(setupId: number): Promise<void> {
  await apiClient.post(`/payroll-setup/setups/${setupId}/archive`);
}

// ── Drafts ──────────────────────────────────────────────────────────────────

export async function listPayrollSetupDrafts(setupId: number): Promise<DraftResponse[]> {
  const resp = await apiClient.get<DraftResponse[]>(`/payroll-setup/setups/${setupId}/drafts`);
  return resp.data;
}

export async function createPayrollSetupDraft(
  setupId: number,
  body: DraftCreateRequest,
): Promise<DraftResponse> {
  const resp = await apiClient.post<DraftResponse>(
    `/payroll-setup/setups/${setupId}/drafts`,
    body,
  );
  return resp.data;
}

export async function updatePayrollSetupDraft(
  setupId: number,
  draftId: number,
  body: DraftUpdateRequest,
): Promise<DraftResponse> {
  const resp = await apiClient.put<DraftResponse>(
    `/payroll-setup/setups/${setupId}/drafts/${draftId}`,
    body,
  );
  return resp.data;
}

export async function discardPayrollSetupDraft(setupId: number, draftId: number): Promise<void> {
  await apiClient.delete(`/payroll-setup/setups/${setupId}/drafts/${draftId}`);
}

// ── Versions / publication ─────────────────────────────────────────────────

export async function listPayrollSetupVersions(setupId: number): Promise<VersionResponse[]> {
  const resp = await apiClient.get<VersionResponse[]>(`/payroll-setup/setups/${setupId}/versions`);
  return resp.data;
}

export async function previewPublicationImpact(
  setupId: number,
  draftId: number,
  body: PublicationImpactRequest,
): Promise<PublicationImpactResponse> {
  const resp = await apiClient.post<PublicationImpactResponse>(
    `/payroll-setup/setups/${setupId}/drafts/${draftId}/publication-impact`,
    body,
  );
  return resp.data;
}

export async function getInlinePublicationChoices(
  setupId: number,
  body: InlineScheduleRequest,
  around?: string,
): Promise<BoundaryChoicesResponse> {
  const resp = await apiClient.post<BoundaryChoicesResponse>(
    `/payroll-setup/setups/${setupId}/publication-choices`,
    body,
    { params: around ? { around } : undefined },
  );
  return resp.data;
}

export async function previewInlinePublicationImpact(
  setupId: number,
  body: InlinePublicationRequest,
): Promise<PublicationImpactResponse> {
  const resp = await apiClient.post<PublicationImpactResponse>(
    `/payroll-setup/setups/${setupId}/publication-impact`,
    body,
  );
  return resp.data;
}

export async function publishInlinePayrollSetup(
  setupId: number,
  body: InlinePublicationRequest,
): Promise<VersionResponse> {
  const resp = await apiClient.post<VersionResponse>(
    `/payroll-setup/setups/${setupId}/publish`,
    body,
  );
  return resp.data;
}

export async function publishPayrollSetupDraft(
  setupId: number,
  draftId: number,
  body: PublishRequest,
): Promise<VersionResponse> {
  const resp = await apiClient.post<VersionResponse>(
    `/payroll-setup/setups/${setupId}/drafts/${draftId}/publish`,
    body,
  );
  return resp.data;
}

// ── Company default ─────────────────────────────────────────────────────────

export async function getDefaultPayrollSetup(): Promise<DefaultSetupResponse> {
  const resp = await apiClient.get<DefaultSetupResponse>('/payroll-setup/default');
  return resp.data;
}

export async function setDefaultPayrollSetup(setupId: number): Promise<void> {
  const body: DefaultSetupRequest = { setup_id: setupId };
  await apiClient.put('/payroll-setup/default', body);
}

export async function clearDefaultPayrollSetup(): Promise<void> {
  const body: DefaultSetupRequest = { setup_id: null };
  await apiClient.put('/payroll-setup/default', body);
}

// ── Branch assignments ──────────────────────────────────────────────────────

export async function listBranchPayrollSetupAssignments(
  branchId: number,
): Promise<AssignmentResponse[]> {
  const resp = await apiClient.get<AssignmentResponse[]>(
    `/payroll-setup/branches/${branchId}/assignments`,
  );
  return resp.data;
}

export async function assignPayrollSetup(
  branchId: number,
  body: AssignmentCreateRequest,
): Promise<AssignmentResponse> {
  const resp = await apiClient.post<AssignmentResponse>(
    `/payroll-setup/branches/${branchId}/assignments`,
    body,
  );
  return resp.data;
}

export async function reassignPayrollSetup(
  branchId: number,
  body: ReassignmentRequest,
): Promise<AssignmentResponse> {
  const resp = await apiClient.post<AssignmentResponse>(
    `/payroll-setup/branches/${branchId}/reassignments`,
    body,
  );
  return resp.data;
}

export async function previewReassignmentImpact(
  branchId: number,
  body: ReassignmentImpactRequest,
): Promise<ReassignmentImpactResponse> {
  const resp = await apiClient.post<ReassignmentImpactResponse>(
    `/payroll-setup/branches/${branchId}/reassignment-impact`,
    body,
  );
  return resp.data;
}

export async function withdrawPayrollSetupAssignment(
  assignmentId: number,
  body: WithdrawalRequest = {},
): Promise<void> {
  await apiClient.post(`/payroll-setup/assignments/${assignmentId}/withdraw`, body);
}

export async function getBranchPayrollSetupHistory(branchId: number): Promise<BranchHistoryResponse> {
  const resp = await apiClient.get<BranchHistoryResponse>(
    `/payroll-setup/branches/${branchId}/history`,
  );
  return resp.data;
}

/**
 * The period start date must come from backend-supplied context (e.g.
 * BranchAdmin.schedule_readiness_date) — never wall-clock today, browser
 * timezone, or frontend cadence math.
 */
export async function getBranchEffectivePayrollSetup(
  branchId: number,
  periodStartDate: string,
): Promise<EffectiveAuthorityResponse> {
  const resp = await apiClient.get<EffectiveAuthorityResponse>(
    `/payroll-setup/branches/${branchId}/effective`,
    { params: { period_start_date: periodStartDate } },
  );
  return resp.data;
}

// ── Boundary choices ─────────────────────────────────────────────────────────
//
// `around` is omitted entirely (not sent as an empty param) when undefined or
// '' — an omitted `around` asks the backend for its own suggestion instead of
// validating a specific date.

export async function getPublicationChoices(
  setupId: number,
  draftId: number,
  around?: string,
): Promise<BoundaryChoicesResponse> {
  const resp = await apiClient.get<BoundaryChoicesResponse>(
    `/payroll-setup/setups/${setupId}/drafts/${draftId}/publication-choices`,
    { params: around ? { around } : undefined },
  );
  return resp.data;
}

export async function getAssignmentChoices(
  branchId: number,
  setupId: number,
  around?: string,
): Promise<BoundaryChoicesResponse> {
  const resp = await apiClient.get<BoundaryChoicesResponse>(
    `/payroll-setup/branches/${branchId}/assignment-choices`,
    { params: around ? { setup_id: setupId, around } : { setup_id: setupId } },
  );
  return resp.data;
}

export async function getReassignmentChoices(
  branchId: number,
  destinationSetupId: number,
  around?: string,
): Promise<BoundaryChoicesResponse> {
  const resp = await apiClient.get<BoundaryChoicesResponse>(
    `/payroll-setup/branches/${branchId}/reassignment-choices`,
    {
      params: around
        ? { destination_setup_id: destinationSetupId, around }
        : { destination_setup_id: destinationSetupId },
    },
  );
  return resp.data;
}

// ── Branch policy summaries ─────────────────────────────────────────────────

export async function listBranchPolicySummaries(): Promise<BranchPolicySummaryResponse[]> {
  const resp = await apiClient.get<BranchPolicySummaryResponse[]>('/payroll-setup/branch-summaries');
  return resp.data;
}

// ── Onboarding options (settings-owned) ─────────────────────────────────────

export async function getBranchOnboardingOptions(
  around?: string,
): Promise<OnboardingOptionsResponse> {
  const resp = await apiClient.get<OnboardingOptionsResponse>(
    '/settings/branches/onboarding-options',
    { params: around ? { around } : undefined },
  );
  return resp.data;
}
