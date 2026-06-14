/**
 * API client functions for the Driver Transfer workflow.
 * All routes are under /driver-transfers (backend app/transfer/router.py).
 */
import apiClient from './apiClient';
import type {
  DriverTransferRequest,
  TransferListResponse,
  DriverTransferCreate,
  SourceApprovalRequest,
  TargetDecisionRequest,
  CancelRequest,
} from '../types/transfer';

export async function listTransfers(params?: {
  branch_id?: number;
  status?: string;
}): Promise<TransferListResponse> {
  const qs = new URLSearchParams();
  if (params?.branch_id != null) qs.set('branch_id', String(params.branch_id));
  if (params?.status)            qs.set('status', params.status);
  const url = `/driver-transfers${qs.toString() ? `?${qs}` : ''}`;
  const r = await apiClient.get<TransferListResponse>(url);
  return r.data;
}

export async function getTransfer(id: number): Promise<DriverTransferRequest> {
  const r = await apiClient.get<DriverTransferRequest>(`/driver-transfers/${id}`);
  return r.data;
}

export async function createTransfer(data: DriverTransferCreate): Promise<DriverTransferRequest> {
  const r = await apiClient.post<DriverTransferRequest>('/driver-transfers', data);
  return r.data;
}

export async function approveSource(
  id: number,
  data: SourceApprovalRequest = {},
): Promise<DriverTransferRequest> {
  const r = await apiClient.post<DriverTransferRequest>(
    `/driver-transfers/${id}/approve-source`,
    data,
  );
  return r.data;
}

export async function decideTarget(
  id: number,
  data: TargetDecisionRequest,
): Promise<DriverTransferRequest> {
  const r = await apiClient.post<DriverTransferRequest>(
    `/driver-transfers/${id}/decide-target`,
    data,
  );
  return r.data;
}

export async function completeTransfer(id: number): Promise<DriverTransferRequest> {
  const r = await apiClient.post<DriverTransferRequest>(
    `/driver-transfers/${id}/complete`,
    {},
  );
  return r.data;
}

export async function cancelTransfer(
  id: number,
  data: CancelRequest = {},
): Promise<DriverTransferRequest> {
  const r = await apiClient.post<DriverTransferRequest>(
    `/driver-transfers/${id}/cancel`,
    data,
  );
  return r.data;
}
