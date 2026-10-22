/**
 * Driver Transfer lifecycle operations and its Driver picker.
 * Lifecycle routes are under /driver-transfers (backend app/transfer/router.py).
 */
import apiClient from './apiClient';
import type { DriverSummary } from '../types/core';
import type {
  DriverTransferRequest,
  TransferListResponse,
  DriverTransferCreate,
  SourceApprovalRequest,
  TargetDecisionRequest,
  CancelRequest,
  RescheduleRequest,
} from '../types/transfer';


/**
 * The permanent Drivers a transfer can be requested for: the canonical Driver roster under the
 * caller's own drivers.view authority (no Workforce or Access permission is needed). Each item
 * carries the Branch of the Driver's current (else pending, else latest) assignment.
 */
export async function listTransferDriverPicker(): Promise<DriverSummary[]> {
  const response = await apiClient.get<DriverSummary[]>('/core/drivers', {
    params: { driver_status: 'Active' },
  });
  return response.data;
}

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

/** Move a request's EffectiveAt to another future instant (Scheduled requests move both boundaries). */
export async function rescheduleTransfer(
  id: number,
  data: RescheduleRequest,
): Promise<DriverTransferRequest> {
  const r = await apiClient.post<DriverTransferRequest>(
    `/driver-transfers/${id}/reschedule`,
    data,
  );
  return r.data;
}
