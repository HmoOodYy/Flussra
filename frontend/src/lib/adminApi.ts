import type { AxiosPromise } from 'axios';
import apiClient from './apiClient';
import type {
  CompanyRole,
  CompanyRoleAssignmentCreate,
  OwnerTransferRequest,
  OwnerTransferResult,
  Permission,
  UserAdmin,
  UserCreate,
  UserPasswordReset,
  UserPermissionOverridesUpdate,
  UserProvision,
  UserUpdate,
} from '../types/admin';

export function getCompanyRoles(): AxiosPromise<CompanyRole[]> {
  return apiClient.get<CompanyRole[]>('/admin/company-roles');
}

export function getAdminUsersIncludingInactive(): AxiosPromise<UserAdmin[]> {
  return apiClient.get<UserAdmin[]>('/admin/users?include_inactive=true');
}

export function getAdminPermissions(uiOnly?: boolean): AxiosPromise<Permission[]> {
  return apiClient.get<Permission[]>(uiOnly === undefined ? '/admin/permissions' : `/admin/permissions?ui_only=${uiOnly}`);
}

export function getAdminUser(userId: number): AxiosPromise<UserAdmin> {
  return apiClient.get<UserAdmin>(`/admin/users/${userId}`);
}

export function createAdminUser(payload: UserCreate): AxiosPromise<UserAdmin> {
  return apiClient.post<UserAdmin>('/admin/users', payload);
}

export function linkAdminUserEmployee(userId: number, employeeId: number | null): AxiosPromise {
  return apiClient.put(`/admin/users/${userId}/employee-link`, { employee_id: employeeId });
}

export function provisionAdminUser(userId: number, payload: UserProvision): AxiosPromise<UserAdmin> {
  return apiClient.post<UserAdmin>(`/admin/users/${userId}/provision`, payload);
}

export function updateAdminUserPermissionOverrides(userId: number, payload: UserPermissionOverridesUpdate): AxiosPromise {
  return apiClient.put(`/admin/users/${userId}/permission-overrides`, payload);
}

export function updateAdminUser(userId: number, payload: UserUpdate): AxiosPromise<UserAdmin> {
  return apiClient.patch<UserAdmin>(`/admin/users/${userId}`, payload);
}

export function resetAdminUserPassword(userId: number, payload: UserPasswordReset): AxiosPromise {
  return apiClient.post(`/admin/users/${userId}/reset-password`, payload);
}

export function assignAdminUserCompanyRole(userId: number, payload: CompanyRoleAssignmentCreate): AxiosPromise {
  return apiClient.post(`/admin/users/${userId}/company-role-assignments`, payload);
}

export function transferCompanyOwner(payload: OwnerTransferRequest): AxiosPromise<OwnerTransferResult> {
  return apiClient.post<OwnerTransferResult>('/admin/company-owner/transfer', payload);
}


