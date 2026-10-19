/**
 * Typed wrappers for the target Compensation API, mounted at /compensation:
 * Company PayDefinition governance, Branch applicability and Driver rate
 * schedules.
 */
import apiClient from './apiClient';
import type {
  ScheduleCreatePayload,
  ScheduleSummary,
  ScheduleValuePayload,
  BranchConfigUpdatePayload,
  BranchConfigVersion,
  BranchPayDefinitionState,
  BulkBranchConfigResult,
  BulkBranchConfigUpdatePayload,
  CompensationAccess,
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

// ── Driver rate schedules (addressed by the permanent Driver) ─────────────────

export async function listDriverPayRates(
  driverId: number,
  asOf?: string,
): Promise<DriverPayRateRow[]> {
  const resp = await apiClient.get<DriverPayRateRow[]>(
    `/compensation/drivers/${driverId}/pay-rates`,
    { params: asOf ? { as_of: asOf } : {} });
  return resp.data;
}

export async function getCompensationAccess(driverId: number): Promise<CompensationAccess> {
  const resp = await apiClient.get<CompensationAccess>(
    `/compensation/drivers/${driverId}/compensation-access`);
  return resp.data;
}

export async function listScheduleHistory(
  driverId: number,
  rateDefinitionId: number,
): Promise<ScheduleSummary[]> {
  const resp = await apiClient.get<ScheduleSummary[]>(
    `/compensation/drivers/${driverId}/rate-definitions/${rateDefinitionId}/rate-schedules`);
  return resp.data;
}

export async function createSchedule(
  payload: ScheduleCreatePayload,
): Promise<ScheduleSummary> {
  const resp = await apiClient.post<ScheduleSummary>(
    '/compensation/driver-rate-schedules', payload);
  return resp.data;
}

export async function replaceScheduleValues(
  scheduleId: number,
  values: ScheduleValuePayload[],
): Promise<ScheduleSummary> {
  const resp = await apiClient.put<ScheduleSummary>(
    `/compensation/driver-rate-schedules/${scheduleId}/values`, { values });
  return resp.data;
}

export async function approveSchedule(scheduleId: number): Promise<ScheduleSummary> {
  const resp = await apiClient.post<ScheduleSummary>(
    `/compensation/driver-rate-schedules/${scheduleId}/approve`);
  return resp.data;
}

export async function discardSchedule(scheduleId: number): Promise<void> {
  await apiClient.delete(`/compensation/driver-rate-schedules/${scheduleId}`);
}

export async function voidSchedule(
  scheduleId: number,
  reason: string,
): Promise<ScheduleSummary> {
  const resp = await apiClient.post<ScheduleSummary>(
    `/compensation/driver-rate-schedules/${scheduleId}/void`, { reason });
  return resp.data;
}

export async function updateSchedule(
  scheduleId: number,
  payload: { effective_from?: string; effective_to?: string | null; notes?: string | null },
): Promise<ScheduleSummary> {
  const resp = await apiClient.patch<ScheduleSummary>(
    `/compensation/driver-rate-schedules/${scheduleId}`, payload);
  return resp.data;
}
