import apiClient from '../../../lib/apiClient';
import type { AxiosPromise } from 'axios';

export interface StatusRate {
  driver_rate_id: number;
  amount: string;
  effective_from: string;
  effective_to: string | null;
  status: string;
}

export interface StatusRateGroup {
  group_key: string;
  status_rate_column_id: number;
  rate_type_id: number;
  rate_name: string;
  unit_name: string;
  current_rate: StatusRate | null;
  pending_rate: StatusRate | null;
  is_missing: boolean;
}

export interface StatusRateMatrix {
  driver_assignment_id: number;
  driver_id: number;
  driver_number: string | null;
  as_of: string;
  groups: StatusRateGroup[];
}

export interface StatusRateBatchPayload {
  effective_from: string | undefined;
  changes: Array<{
    status_rate_column_id: number;
    rate_type_id: number;
    amount: string;
  }>;
}

export function getStatusRateMatrix(driverAssignmentId: number): AxiosPromise<StatusRateMatrix> {
  return apiClient.get<StatusRateMatrix>(`/payroll/driver-assignments/${driverAssignmentId}/rate-matrix`);
}

export function saveStatusRateBatch(driverAssignmentId: number, payload: StatusRateBatchPayload): AxiosPromise<void> {
  return apiClient.post<void>(`/payroll/driver-assignments/${driverAssignmentId}/rates/batch`, payload);
}

export function approveStatusRate(rateId: number): AxiosPromise<void> {
  return apiClient.post<void>(`/payroll/rates/${rateId}/approve`);
}

export function voidStatusRate(rateId: number): AxiosPromise<void> {
  return apiClient.delete<void>(`/payroll/rates/${rateId}`);
}
