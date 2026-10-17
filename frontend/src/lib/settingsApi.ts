import type { AxiosPromise } from 'axios';
import apiClient from './apiClient';
import type {
  BranchAdmin,
  BranchCreate,
  BranchUpdate,
  CompanyProfile,
  CompanyUpdate,
  StatusKey,
  StatusKeyCreate,
  SupportedCurrency,
} from '../types/settings';

export function getCompanyProfile(): AxiosPromise<CompanyProfile> {
  return apiClient.get<CompanyProfile>('/settings/company');
}

export function getSupportedCurrencies(): AxiosPromise<SupportedCurrency[]> {
  return apiClient.get<SupportedCurrency[]>('/settings/currencies');
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

export function getBranchStatusKeys(branchId: number, includeInactiveKeys: boolean): AxiosPromise<StatusKey[]> {
  return apiClient.get<StatusKey[]>(`/settings/branches/${branchId}/status-keys`, {
    params: { include_inactive: includeInactiveKeys },
  });
}

export function createBranchStatusKey(branchId: number, payload: StatusKeyCreate): AxiosPromise<StatusKey> {
  return apiClient.post<StatusKey>(`/settings/branches/${branchId}/status-keys`, payload);
}

export function updateBranchStatusKey(
  branchId: number,
  statusKeyId: number,
  payload: Record<string, unknown>,
): AxiosPromise<StatusKey> {
  return apiClient.patch<StatusKey>(`/settings/branches/${branchId}/status-keys/${statusKeyId}`, payload);
}

export function deactivateBranchStatusKey(branchId: number, statusKeyId: number): AxiosPromise<StatusKey> {
  return apiClient.delete<StatusKey>(`/settings/branches/${branchId}/status-keys/${statusKeyId}`);
}
