import type { AxiosPromise } from 'axios';
import apiClient from './apiClient';
import type {
  BranchAdmin,
  BranchCreate,
  BranchUpdate,
  CompanyProfile,
  CompanyUpdate,
  StatusKey,
  StatusKeyBranchLimits,
  StatusKeyBranchLimitsUpdate,
  StatusKeyCreate,
  StatusKeyUpdate,
  StatusRateColumn,
  StatusRateColumnCreate,
  StatusRateColumnUpdate,
  SupportedCurrency,
} from '../types/settings';

export function getCompanyProfile(): AxiosPromise<CompanyProfile> {
  return apiClient.get<CompanyProfile>('/settings/company');
}

export function getSupportedCurrencies(): AxiosPromise<SupportedCurrency[]> {
  return apiClient.get<SupportedCurrency[]>('/settings/currencies');
}

export function getSupportedTimeZones(): AxiosPromise<string[]> {
  return apiClient.get<string[]>('/settings/time-zones');
}

export function getSettingsBranches(): AxiosPromise<BranchAdmin[]> {
  return apiClient.get<BranchAdmin[]>('/settings/branches');
}

export function updateCompanyProfile(payload: CompanyUpdate): AxiosPromise<CompanyProfile> {
  return apiClient.patch<CompanyProfile>('/settings/company', payload);
}

export function createSettingsBranch(payload: BranchCreate): AxiosPromise<BranchAdmin> {
  return apiClient.post<BranchAdmin>('/settings/branches', payload);
}

export function updateSettingsBranch(branchId: number, payload: BranchUpdate): AxiosPromise<BranchAdmin> {
  return apiClient.patch<BranchAdmin>(`/settings/branches/${branchId}`, payload);
}

export function setDefaultSettingsBranch(branchId: number): AxiosPromise<BranchAdmin> {
  return apiClient.post<BranchAdmin>(`/settings/branches/${branchId}/set-default`);
}

// ─── Company Status catalog ───────────────────────────────────────────────────

export function getStatusKeys(includeInactive: boolean): AxiosPromise<StatusKey[]> {
  return apiClient.get<StatusKey[]>('/settings/status-keys', {
    params: { include_inactive: includeInactive },
  });
}

export function createStatusKey(payload: StatusKeyCreate): AxiosPromise<StatusKey> {
  return apiClient.post<StatusKey>('/settings/status-keys', payload);
}

export function updateStatusKey(statusKeyId: number, payload: StatusKeyUpdate): AxiosPromise<StatusKey> {
  return apiClient.patch<StatusKey>(`/settings/status-keys/${statusKeyId}`, payload);
}

export function deactivateStatusKey(statusKeyId: number): AxiosPromise<StatusKey> {
  return apiClient.delete<StatusKey>(`/settings/status-keys/${statusKeyId}`);
}

// ─── Status rate columns (Company-owned) ──────────────────────────────────────

export function getStatusRateColumns(includeInactive: boolean): AxiosPromise<StatusRateColumn[]> {
  return apiClient.get<StatusRateColumn[]>('/settings/status-rate-columns', {
    params: { include_inactive: includeInactive },
  });
}

export function createStatusRateColumn(payload: StatusRateColumnCreate): AxiosPromise<StatusRateColumn> {
  return apiClient.post<StatusRateColumn>('/settings/status-rate-columns', payload);
}

export function updateStatusRateColumn(
  columnId: number,
  payload: StatusRateColumnUpdate,
): AxiosPromise<StatusRateColumn> {
  return apiClient.patch<StatusRateColumn>(`/settings/status-rate-columns/${columnId}`, payload);
}

// ─── Branch usage limits of the Company Statuses ──────────────────────────────

export function getBranchStatusKeyLimits(branchId: number): AxiosPromise<StatusKeyBranchLimits[]> {
  return apiClient.get<StatusKeyBranchLimits[]>(`/settings/branches/${branchId}/status-key-limits`);
}

export function putBranchStatusKeyLimits(
  branchId: number,
  statusKeyId: number,
  payload: StatusKeyBranchLimitsUpdate,
): AxiosPromise<StatusKeyBranchLimits> {
  return apiClient.put<StatusKeyBranchLimits>(
    `/settings/branches/${branchId}/status-keys/${statusKeyId}/limits`, payload);
}
