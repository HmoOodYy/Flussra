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
  driver_id: number;
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

export function getStatusRateMatrix(driverId: number): AxiosPromise<StatusRateMatrix> {
  return apiClient.get<StatusRateMatrix>(`/payroll/drivers/${driverId}/rate-matrix`);
}

export function saveStatusRateBatch(driverId: number, payload: StatusRateBatchPayload): AxiosPromise<void> {
  return apiClient.post<void>(`/payroll/drivers/${driverId}/rates/batch`, payload);
}

export function approveStatusRate(rateId: number): AxiosPromise<void> {
  return apiClient.post<void>(`/payroll/rates/${rateId}/approve`);
}

export function voidStatusRate(rateId: number): AxiosPromise<void> {
  return apiClient.delete<void>(`/payroll/rates/${rateId}`);
}
