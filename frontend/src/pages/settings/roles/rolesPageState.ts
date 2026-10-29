import { accessSafetyMessage } from '../../../lib/accessSafetyErrors.ts';
import type { CompanyRole, CompanyRoleUser } from '../../../types/admin';

export type RolesState = { roles: CompanyRole[]; loading: boolean; error: string };

export type RolesAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; roles: CompanyRole[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'ROLE_ADDED'; role: CompanyRole }
  | { type: 'ROLE_DELETED'; id: number }
  | { type: 'ROLE_RENAMED'; id: number; name: string };

export function rolesReducer(s: RolesState, a: RolesAction): RolesState {
  switch (a.type) {
    case 'FETCH_START':  return { roles: [], loading: true,  error: '' };
    case 'FETCH_OK':     return { roles: a.roles, loading: false, error: '' };
    case 'FETCH_ERROR':  return { roles: [], loading: false, error: a.error };
    case 'ROLE_ADDED':   return { ...s, roles: [...s.roles, a.role] };
    case 'ROLE_DELETED': return { ...s, roles: s.roles.filter(r => r.company_role_id !== a.id) };
    case 'ROLE_RENAMED': return { ...s, roles: s.roles.map(r => r.company_role_id === a.id ? { ...r, role_name: a.name } : r) };
    default:             return s;
  }
}

export type UsersModalState = {
  roleId: number | null;
  roleName: string;
  loading: boolean;
  users: CompanyRoleUser[];
  error: string;
};

export type UsersModalAction =
  | { type: 'OPEN'; roleId: number; roleName: string }
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; users: CompanyRoleUser[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'CLOSE' };

export function usersModalReducer(s: UsersModalState, a: UsersModalAction): UsersModalState {
  switch (a.type) {
    case 'OPEN':        return { roleId: a.roleId, roleName: a.roleName, loading: true, users: [], error: '' };
    case 'FETCH_START': return { ...s, loading: true, error: '' };
    case 'FETCH_OK':    return { ...s, loading: false, users: a.users };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
    case 'CLOSE':       return { roleId: null, roleName: '', loading: false, users: [], error: '' };
    default:            return s;
  }
}

export function apiError(err: unknown): string {
  const safety = accessSafetyMessage(err);
  if (safety) return safety;
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d)) return d.map((i: unknown) => (i as { msg?: string })?.msg ?? '').filter(Boolean).join(' ');
  if (d && typeof d === 'object' && 'message' in d) return String((d as { message: unknown }).message);
  return 'An unexpected error occurred.';
}
