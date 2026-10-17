import type { AxiosPromise } from 'axios';
import apiClient from './apiClient';
import type {
  BranchAdmin,
  BranchCreate,
  BranchUpdate,
  CompanyProfile,
  CompanyUpdate,
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
