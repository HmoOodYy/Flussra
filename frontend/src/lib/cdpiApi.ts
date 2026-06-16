/**
 * Typed wrappers for CDPI (Custom Daily Pay Item) API calls.
 *
 * All routes are mounted at /settings/cdpi on the backend.
 * Do not mix legacy /settings/pay-items calls into this module.
 */
import apiClient from './apiClient';
import type {
  CdpiRequestSummary,
  CdpiRequestCreatePayload,
  CdpiRequestUpdatePayload,
  CdpiRequestListParams,
  CdpiSubmitPayload,
  CdpiDecidePayload,
  CdpiDirectCreatePayload,
  CdpiDirectCreateSummary,
  CdpiBranchItem,
  CdpiBranchItemUpdatePayload,
} from '../types/settings';

// ── Request workflow ──────────────────────────────────────────────────────────

export async function createCdpiRequest(
  payload: CdpiRequestCreatePayload,
): Promise<CdpiRequestSummary> {
  const resp = await apiClient.post<CdpiRequestSummary>(
    '/settings/cdpi/requests',
    payload,
  );
  return resp.data;
}

export async function listCdpiRequests(
  params?: CdpiRequestListParams,
): Promise<CdpiRequestSummary[]> {
  const query: Record<string, string> = {};
  if (params?.status) query.status = params.status;
  if (params?.branch_id != null) query.branch_id = String(params.branch_id);
  const resp = await apiClient.get<CdpiRequestSummary[]>(
    '/settings/cdpi/requests',
    { params: query },
  );
  return resp.data;
}

export async function getCdpiRequest(
  requestId: string,
): Promise<CdpiRequestSummary> {
  const resp = await apiClient.get<CdpiRequestSummary>(
    `/settings/cdpi/requests/${requestId}`,
  );
  return resp.data;
}

export async function updateCdpiRequest(
  requestId: string,
  payload: CdpiRequestUpdatePayload,
): Promise<CdpiRequestSummary> {
  const resp = await apiClient.patch<CdpiRequestSummary>(
    `/settings/cdpi/requests/${requestId}`,
    payload,
  );
  return resp.data;
}

export async function submitCdpiRequest(
  requestId: string,
  payload: CdpiSubmitPayload,
): Promise<CdpiRequestSummary> {
  const resp = await apiClient.post<CdpiRequestSummary>(
    `/settings/cdpi/requests/${requestId}/submit`,
    payload,
  );
  return resp.data;
}

export async function decideCdpiRequest(
  requestId: string,
  payload: CdpiDecidePayload,
): Promise<CdpiRequestSummary> {
  const resp = await apiClient.post<CdpiRequestSummary>(
    `/settings/cdpi/requests/${requestId}/decide`,
    payload,
  );
  return resp.data;
}

export async function copyCdpiRequest(
  requestId: string,
): Promise<CdpiRequestSummary> {
  const resp = await apiClient.post<CdpiRequestSummary>(
    `/settings/cdpi/requests/${requestId}/copy`,
  );
  return resp.data;
}

// ── Direct company creation ───────────────────────────────────────────────────

export async function createDirectCdpiCompanyItem(
  payload: CdpiDirectCreatePayload,
): Promise<CdpiDirectCreateSummary> {
  const resp = await apiClient.post<CdpiDirectCreateSummary>(
    '/settings/cdpi/direct-company-items',
    payload,
  );
  return resp.data;
}

// ── Branch controls ───────────────────────────────────────────────────────────

export async function listCdpiBranchItems(
  branchId: number,
): Promise<CdpiBranchItem[]> {
  const resp = await apiClient.get<CdpiBranchItem[]>(
    `/settings/cdpi/branches/${branchId}/items`,
  );
  return resp.data;
}

export async function updateCdpiBranchItem(
  branchId: number,
  payItemId: number,
  payload: CdpiBranchItemUpdatePayload,
): Promise<CdpiBranchItem> {
  const resp = await apiClient.patch<CdpiBranchItem>(
    `/settings/cdpi/branches/${branchId}/items/${payItemId}`,
    payload,
  );
  return resp.data;
}
