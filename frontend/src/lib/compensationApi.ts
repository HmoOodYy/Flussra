/**
 * Typed wrappers for the target Compensation API, mounted at /compensation:
 * Company PayDefinition governance, Branch applicability and Driver rate
 * assignments.
 */
import apiClient from './apiClient';
import type {
  AssignmentCreatePayload,
  AssignmentSummary,
  AssignmentValuePayload,
  BranchConfigUpdatePayload,
  BranchConfigVersion,
  BranchPayDefinitionState,
  BulkBranchConfigResult,
  BulkBranchConfigUpdatePayload,
  DriverPayRateRow,
  PayDefinitionDirectCreatePayload,
  PayDefinitionRequestCreatePayload,
  PayDefinitionRequestDecisionPayload,
  PayDefinitionRequestEvent,
  PayDefinitionRequestListParams,
  PayDefinitionRequestSubmitPayload,
  PayDefinitionRequestSummary,
  PayDefinitionRequestUpdatePayload,
  PayDefinitionSummary,
} from '../types/compensation';

// ── Request workflow ──────────────────────────────────────────────────────────

export async function createPayDefinitionRequest(
  payload: PayDefinitionRequestCreatePayload,
): Promise<PayDefinitionRequestSummary> {
  const resp = await apiClient.post<PayDefinitionRequestSummary>(
    '/compensation/pay-definition-requests', payload);
  return resp.data;
}

export async function listPayDefinitionRequests(
  params?: PayDefinitionRequestListParams,
): Promise<PayDefinitionRequestSummary[]> {
  const query: Record<string, string> = {};
  if (params?.status) query.status = params.status;
  if (params?.branch_id != null) query.branch_id = String(params.branch_id);
  const resp = await apiClient.get<PayDefinitionRequestSummary[]>(
    '/compensation/pay-definition-requests', { params: query });
  return resp.data;
}

export async function updatePayDefinitionRequest(
  requestId: string,
  payload: PayDefinitionRequestUpdatePayload,
): Promise<PayDefinitionRequestSummary> {
  const resp = await apiClient.patch<PayDefinitionRequestSummary>(
    `/compensation/pay-definition-requests/${requestId}`, payload);
  return resp.data;
}

export async function submitPayDefinitionRequest(
  requestId: string,
  payload: PayDefinitionRequestSubmitPayload,
): Promise<PayDefinitionRequestSummary> {
  const resp = await apiClient.post<PayDefinitionRequestSummary>(
    `/compensation/pay-definition-requests/${requestId}/submit`, payload);
  return resp.data;
}

export async function decidePayDefinitionRequest(
  requestId: string,
  payload: PayDefinitionRequestDecisionPayload,
): Promise<PayDefinitionRequestSummary> {
  const resp = await apiClient.post<PayDefinitionRequestSummary>(
    `/compensation/pay-definition-requests/${requestId}/decide`, payload);
  return resp.data;
}

export async function copyPayDefinitionRequest(
  requestId: string,
): Promise<PayDefinitionRequestSummary> {
  const resp = await apiClient.post<PayDefinitionRequestSummary>(
    `/compensation/pay-definition-requests/${requestId}/copy`);
  return resp.data;
}

export async function listPayDefinitionRequestEvents(
  requestId: string,
): Promise<PayDefinitionRequestEvent[]> {
  const resp = await apiClient.get<PayDefinitionRequestEvent[]>(
    `/compensation/pay-definition-requests/${requestId}/events`);
  return resp.data;
}

// ── Company PayDefinitions ────────────────────────────────────────────────────

export async function createPayDefinition(
  payload: PayDefinitionDirectCreatePayload,
): Promise<PayDefinitionSummary> {
  const resp = await apiClient.post<PayDefinitionSummary>('/compensation/pay-definitions', payload);
  return resp.data;
}

export async function listPayDefinitions(): Promise<PayDefinitionSummary[]> {
  const resp = await apiClient.get<PayDefinitionSummary[]>('/compensation/pay-definitions');
  return resp.data;
}

export async function retirePayDefinition(payDefinitionId: number): Promise<PayDefinitionSummary> {
  const resp = await apiClient.post<PayDefinitionSummary>(
    `/compensation/pay-definitions/${payDefinitionId}/retire`);
  return resp.data;
}

// ── Branch applicability ──────────────────────────────────────────────────────

export async function listBranchPayDefinitions(
  branchId: number,
): Promise<BranchPayDefinitionState[]> {
  const resp = await apiClient.get<BranchPayDefinitionState[]>(
    `/compensation/branches/${branchId}/pay-definitions`);
  return resp.data;
}

export async function updateBranchPayDefinition(
  branchId: number,
  payDefinitionId: number,
  payload: BranchConfigUpdatePayload,
): Promise<BranchPayDefinitionState> {
  const resp = await apiClient.patch<BranchPayDefinitionState>(
    `/compensation/branches/${branchId}/pay-definitions/${payDefinitionId}`, payload);
  return resp.data;
}

export async function bulkUpdateBranchPayDefinition(
  payDefinitionId: number,
  payload: BulkBranchConfigUpdatePayload,
): Promise<BulkBranchConfigResult> {
  const resp = await apiClient.patch<BulkBranchConfigResult>(
    `/compensation/pay-definitions/${payDefinitionId}/branch-config`, payload);
  return resp.data;
}

export async function listBranchPayDefinitionHistory(
  branchId: number,
  payDefinitionId: number,
): Promise<BranchConfigVersion[]> {
  const resp = await apiClient.get<BranchConfigVersion[]>(
    `/compensation/branches/${branchId}/pay-definitions/${payDefinitionId}/history`);
  return resp.data;
}

// ── Driver rate assignments ───────────────────────────────────────────────────

export async function listDriverPayRates(
  driverId: number,
  asOf?: string,
): Promise<DriverPayRateRow[]> {
  const resp = await apiClient.get<DriverPayRateRow[]>(
    `/compensation/drivers/${driverId}/pay-rates`,
    { params: asOf ? { as_of: asOf } : {} });
  return resp.data;
}

export async function listAssignmentHistory(
  driverId: number,
  rateDefinitionId: number,
): Promise<AssignmentSummary[]> {
  const resp = await apiClient.get<AssignmentSummary[]>(
    `/compensation/drivers/${driverId}/rate-definitions/${rateDefinitionId}/assignments`);
  return resp.data;
}

export async function createAssignment(
  payload: AssignmentCreatePayload,
): Promise<AssignmentSummary> {
  const resp = await apiClient.post<AssignmentSummary>(
    '/compensation/driver-rate-assignments', payload);
  return resp.data;
}

export async function replaceAssignmentValues(
  assignmentId: number,
  values: AssignmentValuePayload[],
): Promise<AssignmentSummary> {
  const resp = await apiClient.put<AssignmentSummary>(
    `/compensation/driver-rate-assignments/${assignmentId}/values`, { values });
  return resp.data;
}

export async function approveAssignment(assignmentId: number): Promise<AssignmentSummary> {
  const resp = await apiClient.post<AssignmentSummary>(
    `/compensation/driver-rate-assignments/${assignmentId}/approve`);
  return resp.data;
}

export async function discardAssignment(assignmentId: number): Promise<void> {
  await apiClient.delete(`/compensation/driver-rate-assignments/${assignmentId}`);
}

export async function voidAssignment(
  assignmentId: number,
  reason: string,
): Promise<AssignmentSummary> {
  const resp = await apiClient.post<AssignmentSummary>(
    `/compensation/driver-rate-assignments/${assignmentId}/void`, { reason });
  return resp.data;
}

export async function updateAssignment(
  assignmentId: number,
  payload: { effective_from?: string; effective_to?: string | null; notes?: string | null },
): Promise<AssignmentSummary> {
  const resp = await apiClient.patch<AssignmentSummary>(
    `/compensation/driver-rate-assignments/${assignmentId}`, payload);
  return resp.data;
}
