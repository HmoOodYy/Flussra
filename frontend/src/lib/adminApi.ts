import type { AxiosPromise } from 'axios';
import apiClient from './apiClient';
import type {
  CompanyRole,
  CompanyRoleAssignmentCreate,
  CompanyRoleCreate,
  CompanyRolePermissions,
  CompanyRoleUser,
  EmployeeLinkCandidate,
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

export function linkAdminUserEmployee(userId: number, employeeId: number): AxiosPromise<UserAdmin> {
  return apiClient.put<UserAdmin>(`/admin/users/${userId}/employee-link`, { employee_id: employeeId });
}

export function unlinkAdminUserEmployee(userId: number): AxiosPromise<UserAdmin> {
  return apiClient.delete<UserAdmin>(`/admin/users/${userId}/employee-link`);
}

/** Employees no User is linked to: an Access read that needs no Workforce permission. */
export function getEmployeeLinkCandidates(params: {
  q?: string;
  driver_state?: 'current' | 'pending' | 'none';
  /** Resolve one specific Employee, wherever it sits in the paged order. */
  employee_id?: number;
  limit?: number;
  offset?: number;
} = {}): AxiosPromise<EmployeeLinkCandidate[]> {
  return apiClient.get<EmployeeLinkCandidate[]>('/admin/employee-link-candidates', { params });
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



export function getCompanyRolePermissions(roleId: number): AxiosPromise<CompanyRolePermissions> {
  return apiClient.get<CompanyRolePermissions>(`/admin/company-roles/${roleId}/permissions`);
}

export function updateCompanyRolePermissions(roleId: number, payload: { permission_codes: string[] }): AxiosPromise {
  return apiClient.put(`/admin/company-roles/${roleId}/permissions`, payload);
}

export function getCompanyRoleUsers(roleId: number): AxiosPromise<CompanyRoleUser[]> {
  return apiClient.get<CompanyRoleUser[]>(`/admin/company-roles/${roleId}/users`);
}

export function createCompanyRole(payload: CompanyRoleCreate): AxiosPromise<CompanyRole> {
  return apiClient.post<CompanyRole>('/admin/company-roles', payload);
}

export function renameCompanyRole(roleId: number, payload: { role_name: string }): AxiosPromise<CompanyRole> {
  return apiClient.patch<CompanyRole>(`/admin/company-roles/${roleId}`, payload);
}

export function deleteCompanyRole(roleId: number): AxiosPromise {
  return apiClient.delete(`/admin/company-roles/${roleId}`);
}

