/**
 * PeoplePage — /people
 *
 * Management-page layout matching Company & Branches / Pay Items / Roles quality.
 *
 * Left panel  : filterable, searchable people list
 * Right panel : person detail with Profile, Access, Role & Scope,
 *               Extra Permissions, Driver sections
 *
 * Modals: Add Person wizard (5 steps), Edit Person, Reset Password,
 *         Change Role, Edit Extra Permissions, Transfer Ownership (high-friction)
 */
import { useEffect, useReducer, useRef, useCallback } from 'react';
import type { FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import apiClient from '../../lib/apiClient';
import { friendlyPermLabel } from '../../lib/permissionLabels';
import { useAuth } from '../../store/authStore';
import {
  canCreatePeople,
  canEditPeople,
  canTogglePeopleActive,
  canAssignPeopleRole,
  canManageRoles,
  canViewTransfers,
} from '../../lib/permissions';
import { TransferRequestsTab } from './TransferRequestsTab';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import type {
  UserAdmin,
  UserCreate,
  UserUpdate,
  UserPasswordReset,
  CompanyRole,
  CompanyRoleAssignmentCreate,
  UserPermissionOverridesUpdate,
  OwnerTransferRequest,
  OwnerTransferResult,
  Permission,
} from '../../types/admin';
import type { Branch } from '../../types/core';
import styles from './PeoplePage.module.css';

// ─── Permission metadata (mirrors RolesPage) ─────────────────────────────────

const MODULE_LABELS: Record<string, string> = {
  company:  'Company & Branches',
  roles:    'Roles & Permissions',
  users:    'Members',
  payroll:  'Payroll',
  payitems: 'Pay Items',
  payrates: 'Pay Rates',
  drivers:  'Drivers',
  dispatch: 'Dispatch / Operations',
  reports:  'Reports',
  settings: 'Settings',
};
const MODULE_ORDER = Object.keys(MODULE_LABELS);

const PERM_DEPS: Record<string, string> = {
  'company.edit':     'company.view',
  'branches.create':  'branches.view',
  'branches.edit':    'branches.view',
  'roles.create':     'roles.view',
  'roles.edit':       'roles.view',
  'roles.delete':     'roles.view',
  'users.create':     'users.view',
  'users.edit':       'users.view',
  'users.deactivate': 'users.view',
  'payroll.edit':     'payroll.view',
  'payroll.approve':  'payroll.view',
  'payroll.finalize': 'payroll.view',
  'payitems.edit':    'payitems.view',
  'payrates.edit':    'payrates.view',
  'drivers.create':   'drivers.view',
  'drivers.edit':     'drivers.view',
  'dispatch.edit':    'dispatch.view',
  'settings.manage':  'settings.view',
};
const PERM_DEPENDENTS: Record<string, string[]> = {};
for (const [child, parent] of Object.entries(PERM_DEPS)) {
  (PERM_DEPENDENTS[parent] ??= []).push(child);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

async function fetchCompanyRoles(canList: boolean): Promise<CompanyRole[]> {
  if (!canList) return [];
  const r = await apiClient.get<CompanyRole[]>('/admin/company-roles');
  return r.data;
}

function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d))
    return d.map((i: unknown) =>
      i && typeof i === 'object' && 'msg' in i ? String((i as { msg: unknown }).msg) : null
    ).filter(Boolean).join(' ');
  return 'An unexpected error occurred.';
}

function fmtDate(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function fmtDateTime(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function avatarInitials(name: string): string {
  return name.split(' ').map(w => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase();
}

function scopeLabel(scope: string | null, branchName: string | null): string {
  if (!scope) return '—';
  if (scope === 'AllCompanyBranches') return 'All branches';
  if (scope === 'SpecificBranch') return branchName ? branchName : 'Specific branch';
  if (scope === 'OwnDriverDataOnly') return 'Own data only';
  return scope;
}

type AccessStatus = { label: string; kind: 'ok' | 'warn' | 'err' };
function accessStatus(p: UserAdmin): AccessStatus {
  if (!p.is_active)       return { label: 'Inactive',         kind: 'err'  };
  if (!p.can_login)       return { label: 'Login disabled',   kind: 'warn' };
  if (!p.company_role_id) return { label: 'No role assigned', kind: 'warn' };
  return { label: 'Complete', kind: 'ok' };
}

// Group permissions by module, sorted by MODULE_ORDER
function groupPerms(perms: Permission[]): Array<{ module: string; label: string; perms: Permission[] }> {
  const map = new Map<string, Permission[]>();
  for (const p of perms) {
    if (!map.has(p.module_code)) map.set(p.module_code, []);
    map.get(p.module_code)!.push(p);
  }
  return MODULE_ORDER
    .filter(m => map.has(m))
    .map(m => ({ module: m, label: MODULE_LABELS[m] ?? m, perms: map.get(m)! }));
}

// Apply PERM_DEPS: adding a code also adds its parent; removing a code removes its dependents
function applyToggle(current: Set<string>, code: string, checked: boolean): Set<string> {
  const next = new Set(current);
  if (checked) {
    next.add(code);
    const parent = PERM_DEPS[code];
    if (parent) next.add(parent);
  } else {
    next.delete(code);
    const children = PERM_DEPENDENTS[code] ?? [];
    for (const child of children) next.delete(child);
  }
  return next;
}

// ─── Page state ───────────────────────────────────────────────────────────────

type FilterStatus = 'all' | 'active' | 'inactive';

type PageTab = 'people' | 'transfers';

interface PageState {
  tab: PageTab;
  people: UserAdmin[];
  roles: CompanyRole[];
  branches: Branch[];
  allPerms: Permission[];
  loading: boolean;
  error: string | null;
  selectedId: number | null;
  search: string;
  filterStatus: FilterStatus;
  filterRoleId: number | null;
  // Add Person wizard
  wiz: WizState;
  // Edit person modal
  edit: EditState;
  // Reset password modal
  reset: ResetState;
  // Change role modal
  changeRole: ChangeRoleState;
  // Extra permissions modal
  extraPerms: ExtraPermsState;
  // Activate/deactivate confirm
  confirmToggle: { open: boolean; userId: number | null; toActive: boolean };
  // Transfer ownership modal
  transfer: TransferState;
  // Toast
  toast: string | null;
}

// Wizard sub-state
interface WizState {
  open: boolean;
  step: 1 | 2 | 3 | 4 | 5;
  loading: boolean;
  error: string | null;
  createdUser: UserAdmin | null;
  partialSuccess: boolean;
  // Form fields
  name: string; username: string; password: string;
  email: string; phone: string; canLogin: boolean; mustChange: boolean;
  roleId: number | null;
  scope: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly';
  branchId: number | null;
  extraCodes: Set<string>;
}

interface EditState {
  open: boolean; loading: boolean; error: string | null;
  name: string; email: string; phone: string; canLogin: boolean; mustChange: boolean;
}

interface ResetState {
  open: boolean; loading: boolean; error: string | null; pw: string; mustChange: boolean;
}

interface ChangeRoleState {
  open: boolean; loading: boolean; error: string | null;
  roleId: number | null;
  scope: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly';
  branchId: number | null;
}

interface ExtraPermsState {
  open: boolean; loading: boolean; error: string | null;
  codes: Set<string>;
}

interface TransferState {
  open: boolean; loading: boolean; error: string | null;
  targetId: number | null; confirmText: string; replacementRoleId: number | null;
}

type Action =
  | { type: 'SET_TAB'; tab: PageTab }
  | { type: 'LOADED'; people: UserAdmin[]; roles: CompanyRole[]; branches: Branch[]; allPerms: Permission[] }
  | { type: 'LOAD_ERR'; error: string }
  | { type: 'SELECT'; id: number | null }
  | { type: 'SET_SEARCH'; val: string }
  | { type: 'SET_FILTER_STATUS'; val: FilterStatus }
  | { type: 'SET_FILTER_ROLE'; val: number | null }
  | { type: 'UPDATE_PERSON'; person: UserAdmin }
  | { type: 'ADD_PERSON'; person: UserAdmin }
  | { type: 'TOAST'; msg: string | null }
  // Wizard
  | { type: 'WIZ_OPEN' }
  | { type: 'WIZ_CLOSE' }
  | { type: 'WIZ_STEP'; step: WizState['step'] }
  | { type: 'WIZ_LOADING'; val: boolean }
  | { type: 'WIZ_ERROR'; err: string | null }
  | { type: 'WIZ_USER_CREATED'; user: UserAdmin }
  | { type: 'WIZ_PARTIAL'; user: UserAdmin }
  | { type: 'WIZ_DONE'; user: UserAdmin }
  | { type: 'WIZ_FIELD'; field: keyof WizState; val: unknown }
  | { type: 'WIZ_EXTRA_TOGGLE'; code: string; checked: boolean }
  // Edit
  | { type: 'EDIT_OPEN'; person: UserAdmin }
  | { type: 'EDIT_CLOSE' }
  | { type: 'EDIT_LOADING'; val: boolean }
  | { type: 'EDIT_ERROR'; err: string | null }
  | { type: 'EDIT_FIELD'; field: keyof EditState; val: unknown }
  // Reset
  | { type: 'RESET_OPEN' }
  | { type: 'RESET_CLOSE' }
  | { type: 'RESET_LOADING'; val: boolean }
  | { type: 'RESET_ERROR'; err: string | null }
  | { type: 'RESET_PW'; val: string }
  | { type: 'RESET_MUST'; val: boolean }
  // Change role
  | { type: 'CR_OPEN'; person: UserAdmin }
  | { type: 'CR_CLOSE' }
  | { type: 'CR_LOADING'; val: boolean }
  | { type: 'CR_ERROR'; err: string | null }
  | { type: 'CR_FIELD'; field: keyof ChangeRoleState; val: unknown }
  // Extra perms
  | { type: 'EP_OPEN'; person: UserAdmin }
  | { type: 'EP_CLOSE' }
  | { type: 'EP_LOADING'; val: boolean }
  | { type: 'EP_ERROR'; err: string | null }
  | { type: 'EP_TOGGLE'; code: string; checked: boolean }
  // Activate toggle
  | { type: 'CONFIRM_TOGGLE'; userId: number; toActive: boolean }
  | { type: 'CONFIRM_TOGGLE_CLOSE' }
  // Transfer
  | { type: 'TRANSFER_OPEN'; targetId: number }
  | { type: 'TRANSFER_CLOSE' }
  | { type: 'TRANSFER_LOADING'; val: boolean }
  | { type: 'TRANSFER_ERROR'; err: string | null }
  | { type: 'TRANSFER_FIELD'; field: keyof TransferState; val: unknown }
  | { type: 'TRANSFER_DONE'; result: OwnerTransferResult };

const WIZ0: WizState = {
  open: false, step: 1, loading: false, error: null, createdUser: null, partialSuccess: false,
  name: '', username: '', password: '', email: '', phone: '', canLogin: true, mustChange: true,
  roleId: null, scope: 'AllCompanyBranches', branchId: null, extraCodes: new Set(),
};
const EDIT0: EditState = { open: false, loading: false, error: null, name: '', email: '', phone: '', canLogin: true, mustChange: false };
const RESET0: ResetState = { open: false, loading: false, error: null, pw: '', mustChange: true };
const CR0: ChangeRoleState = { open: false, loading: false, error: null, roleId: null, scope: 'AllCompanyBranches', branchId: null };
const EP0: ExtraPermsState = { open: false, loading: false, error: null, codes: new Set() };
const TRANSFER0: TransferState = { open: false, loading: false, error: null, targetId: null, confirmText: '', replacementRoleId: null };

const INITIAL: PageState = {
  tab: 'people',
  people: [], roles: [], branches: [], allPerms: [],
  loading: true, error: null,
  selectedId: null, search: '', filterStatus: 'active', filterRoleId: null,
  wiz: WIZ0, edit: EDIT0, reset: RESET0, changeRole: CR0, extraPerms: EP0,
  confirmToggle: { open: false, userId: null, toActive: false },
  transfer: TRANSFER0,
  toast: null,
};

function reducer(st: PageState, a: Action): PageState {
  switch (a.type) {
    case 'SET_TAB': return { ...st, tab: a.tab };
    case 'LOADED': return { ...st, loading: false, error: null, people: a.people, roles: a.roles, branches: a.branches, allPerms: a.allPerms };
    case 'LOAD_ERR': return { ...st, loading: false, error: a.error };
    case 'SELECT': return { ...st, selectedId: a.id };
    case 'SET_SEARCH': return { ...st, search: a.val };
    case 'SET_FILTER_STATUS': return { ...st, filterStatus: a.val };
    case 'SET_FILTER_ROLE': return { ...st, filterRoleId: a.val };
    case 'UPDATE_PERSON': return { ...st, people: st.people.map(p => p.user_id === a.person.user_id ? a.person : p) };
    case 'ADD_PERSON': return { ...st, people: [...st.people, a.person] };
    case 'TOAST': return { ...st, toast: a.msg };
    // Wizard
    case 'WIZ_OPEN': return { ...st, wiz: { ...WIZ0, open: true } };
    case 'WIZ_CLOSE': return { ...st, wiz: { ...st.wiz, open: false } };
    case 'WIZ_STEP': return { ...st, wiz: { ...st.wiz, step: a.step, error: null } };
    case 'WIZ_LOADING': return { ...st, wiz: { ...st.wiz, loading: a.val } };
    case 'WIZ_ERROR': return { ...st, wiz: { ...st.wiz, error: a.err, loading: false } };
    case 'WIZ_USER_CREATED': return { ...st, wiz: { ...st.wiz, createdUser: a.user, loading: false, step: 2, error: null } };
    case 'WIZ_PARTIAL': return { ...st, wiz: { ...st.wiz, partialSuccess: true, createdUser: a.user, loading: false, step: 5 } };
    case 'WIZ_DONE': return { ...st, wiz: { ...st.wiz, open: false, loading: false }, selectedId: a.user.user_id };
    case 'WIZ_FIELD': return { ...st, wiz: { ...st.wiz, [a.field]: a.val } };
    case 'WIZ_EXTRA_TOGGLE': return { ...st, wiz: { ...st.wiz, extraCodes: applyToggle(st.wiz.extraCodes, a.code, a.checked) } };
    // Edit
    case 'EDIT_OPEN': return { ...st, edit: { open: true, loading: false, error: null, name: a.person.display_name, email: a.person.email ?? '', phone: a.person.phone ?? '', canLogin: a.person.can_login, mustChange: a.person.must_change_password } };
    case 'EDIT_CLOSE': return { ...st, edit: { ...st.edit, open: false } };
    case 'EDIT_LOADING': return { ...st, edit: { ...st.edit, loading: a.val } };
    case 'EDIT_ERROR': return { ...st, edit: { ...st.edit, error: a.err, loading: false } };
    case 'EDIT_FIELD': return { ...st, edit: { ...st.edit, [a.field]: a.val } };
    // Reset
    case 'RESET_OPEN': return { ...st, reset: { open: true, loading: false, error: null, pw: '', mustChange: true } };
    case 'RESET_CLOSE': return { ...st, reset: { ...st.reset, open: false } };
    case 'RESET_LOADING': return { ...st, reset: { ...st.reset, loading: a.val } };
    case 'RESET_ERROR': return { ...st, reset: { ...st.reset, error: a.err, loading: false } };
    case 'RESET_PW': return { ...st, reset: { ...st.reset, pw: a.val } };
    case 'RESET_MUST': return { ...st, reset: { ...st.reset, mustChange: a.val } };
    // Change role
    case 'CR_OPEN': return { ...st, changeRole: { open: true, loading: false, error: null, roleId: a.person.company_role_id, scope: a.person.company_role_scope ?? 'AllCompanyBranches', branchId: a.person.company_role_branch_id } };
    case 'CR_CLOSE': return { ...st, changeRole: { ...st.changeRole, open: false } };
    case 'CR_LOADING': return { ...st, changeRole: { ...st.changeRole, loading: a.val } };
    case 'CR_ERROR': return { ...st, changeRole: { ...st.changeRole, error: a.err, loading: false } };
    case 'CR_FIELD': return { ...st, changeRole: { ...st.changeRole, [a.field]: a.val } };
    // Extra perms
    case 'EP_OPEN': return { ...st, extraPerms: { open: true, loading: false, error: null, codes: new Set(a.person.extra_permission_codes) } };
    case 'EP_CLOSE': return { ...st, extraPerms: { ...st.extraPerms, open: false } };
    case 'EP_LOADING': return { ...st, extraPerms: { ...st.extraPerms, loading: a.val } };
    case 'EP_ERROR': return { ...st, extraPerms: { ...st.extraPerms, error: a.err, loading: false } };
    case 'EP_TOGGLE': return { ...st, extraPerms: { ...st.extraPerms, codes: applyToggle(st.extraPerms.codes, a.code, a.checked) } };
    // Activate toggle
    case 'CONFIRM_TOGGLE': return { ...st, confirmToggle: { open: true, userId: a.userId, toActive: a.toActive } };
    case 'CONFIRM_TOGGLE_CLOSE': return { ...st, confirmToggle: { open: false, userId: null, toActive: false } };
    // Transfer
    case 'TRANSFER_OPEN': return { ...st, transfer: { ...TRANSFER0, open: true, targetId: a.targetId } };
    case 'TRANSFER_CLOSE': return { ...st, transfer: { ...st.transfer, open: false } };
    case 'TRANSFER_LOADING': return { ...st, transfer: { ...st.transfer, loading: a.val } };
    case 'TRANSFER_ERROR': return { ...st, transfer: { ...st.transfer, error: a.err, loading: false } };
    case 'TRANSFER_FIELD': return { ...st, transfer: { ...st.transfer, [a.field]: a.val } };
    case 'TRANSFER_DONE': return { ...st, transfer: { ...st.transfer, open: false, loading: false } };
    default: return st;
  }
}

// ─── Main component ───────────────────────────────────────────────────────────

export function PeoplePage() {
  const { user: authUser, logout } = useAuth();
  const navigate = useNavigate();
  const [st, dispatch] = useReducer(reducer, INITIAL);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load. /admin/company-roles requires roles.view/fallback (canManageRoles) —
  // a users.view-only person must still be able to load the rest of the page.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const wantRoles = !!authUser && canManageRoles(authUser);
        const [pR, bR, permR, roles] = await Promise.all([
          apiClient.get<UserAdmin[]>('/admin/users?include_inactive=true'),
          apiClient.get<Branch[]>('/core/branches'),
          apiClient.get<Permission[]>('/admin/permissions?ui_only=true'),
          fetchCompanyRoles(wantRoles),
        ]);
        if (!cancelled) dispatch({ type: 'LOADED', people: pR.data, roles, branches: bR.data, allPerms: permR.data });
      } catch (e) {
        if (!cancelled) dispatch({ type: 'LOAD_ERR', error: apiError(e) });
      }
    }
    load();
    return () => { cancelled = true; };
  }, [authUser]);

  function showToast(msg: string) {
    dispatch({ type: 'TOAST', msg });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => dispatch({ type: 'TOAST', msg: null }), 3500);
  }

  async function refreshUser(userId: number) {
    try {
      const r = await apiClient.get<UserAdmin>(`/admin/users/${userId}`);
      dispatch({ type: 'UPDATE_PERSON', person: r.data });
      return r.data;
    } catch { return null; }
  }

  // Derived
  const filtered = st.people.filter(p => {
    const q = st.search.toLowerCase();
    if (q && !p.display_name.toLowerCase().includes(q) && !p.username.toLowerCase().includes(q) && !(p.email ?? '').toLowerCase().includes(q)) return false;
    if (st.filterStatus === 'active' && !p.is_active) return false;
    if (st.filterStatus === 'inactive' && p.is_active) return false;
    if (st.filterRoleId !== null && p.company_role_id !== st.filterRoleId) return false;
    return true;
  }).sort((a, b) => a.display_name.localeCompare(b.display_name));

  const selected = st.selectedId !== null ? st.people.find(p => p.user_id === st.selectedId) ?? null : null;
  const amOwner = !!(authUser && st.people.find(p => p.user_id === authUser.user_id)?.company_role_code === 'COMPANY_OWNER');
  const userCanCreate     = authUser ? canCreatePeople(authUser)       : false;
  const userCanEditPeople = authUser ? canEditPeople(authUser)         : false;
  const userCanToggle     = authUser ? canTogglePeopleActive(authUser) : false;
  const userCanAssignRole = authUser ? canAssignPeopleRole(authUser)   : false;
  const userCanManageRoles = authUser ? canManageRoles(authUser)       : false;
  const userCanChangeRole = userCanAssignRole && userCanManageRoles;
  const userCanTransfers  = authUser ? canViewTransfers(authUser)      : false;
  const assignableRoles = st.roles.filter(r => r.role_code !== 'COMPANY_OWNER' && r.is_active);

  // ── Wizard: step 1 — create user ──
  const wizStep1 = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    const { wiz } = st;
    if (!wiz.name.trim()) { dispatch({ type: 'WIZ_ERROR', err: 'Display name is required.' }); return; }
    if (!wiz.username.trim()) { dispatch({ type: 'WIZ_ERROR', err: 'Username is required.' }); return; }
    if (wiz.password.length < 8) { dispatch({ type: 'WIZ_ERROR', err: 'Password must be at least 8 characters.' }); return; }
    dispatch({ type: 'WIZ_LOADING', val: true });
    try {
      const payload: UserCreate = { display_name: wiz.name.trim(), username: wiz.username.trim().toLowerCase(), password: wiz.password, email: wiz.email.trim() || null, phone: wiz.phone.trim() || null, can_login: wiz.canLogin, must_change_password: wiz.mustChange, is_active: true };
      const r = await apiClient.post<UserAdmin>('/admin/users', payload);
      dispatch({ type: 'ADD_PERSON', person: r.data });
      dispatch({ type: 'WIZ_USER_CREATED', user: r.data });
      if (!userCanChangeRole) {
        dispatch({ type: 'WIZ_STEP', step: 5 });
      }
    } catch (e) { dispatch({ type: 'WIZ_ERROR', err: apiError(e) }); }
  }, [st, userCanChangeRole]);

  // ── Wizard: step 3 — assign role+scope ──
  const wizStep3 = useCallback(async () => {
    const { wiz } = st;
    if (!wiz.roleId) { dispatch({ type: 'WIZ_ERROR', err: 'Please select a role.' }); return; }
    if (!wiz.createdUser) return;
    if (wiz.scope !== 'AllCompanyBranches' && !wiz.branchId) { dispatch({ type: 'WIZ_ERROR', err: 'Please select a branch.' }); return; }
    dispatch({ type: 'WIZ_LOADING', val: true });
    try {
      const body: CompanyRoleAssignmentCreate = { company_role_id: wiz.roleId, scope_type: wiz.scope, branch_id: wiz.scope === 'AllCompanyBranches' ? null : wiz.branchId };
      await apiClient.post(`/admin/users/${wiz.createdUser.user_id}/company-role-assignments`, body);
      // Fix 6: Refresh user so role_permission_codes is populated before step 4 renders
      const updated = await apiClient.get<UserAdmin>(`/admin/users/${wiz.createdUser.user_id}`);
      dispatch({ type: 'UPDATE_PERSON', person: updated.data });
      dispatch({ type: 'WIZ_USER_CREATED', user: updated.data });
      dispatch({ type: 'WIZ_LOADING', val: false });
      dispatch({ type: 'WIZ_STEP', step: userCanEditPeople ? 4 : 5 });
    } catch (e) {
      // Partial success: user created, role assignment failed
      dispatch({ type: 'WIZ_PARTIAL', user: wiz.createdUser });
      dispatch({ type: 'WIZ_ERROR', err: `Person created but role assignment failed: ${apiError(e)}` });
    }
  }, [st, userCanEditPeople]);

  // ── Wizard: step 4 — save extra perms then go to review ──
  const wizStep4 = useCallback(async () => {
    const { wiz } = st;
    if (!wiz.createdUser) return;
    dispatch({ type: 'WIZ_LOADING', val: true });
    try {
      // Fix 6: Filter out codes already in role to avoid sending redundant overrides
      const rolePermsSet = new Set(wiz.createdUser.role_permission_codes);
      const extraOnly = Array.from(wiz.extraCodes).filter(c => !rolePermsSet.has(c));
      if (extraOnly.length > 0) {
        const body: UserPermissionOverridesUpdate = { permission_codes: extraOnly };
        await apiClient.put(`/admin/users/${wiz.createdUser.user_id}/permission-overrides`, body);
      }
      const updated = await apiClient.get<UserAdmin>(`/admin/users/${wiz.createdUser.user_id}`);
      dispatch({ type: 'UPDATE_PERSON', person: updated.data });
      dispatch({ type: 'WIZ_USER_CREATED', user: updated.data }); // update createdUser with fresh data
      dispatch({ type: 'WIZ_LOADING', val: false });
      dispatch({ type: 'WIZ_STEP', step: 5 });
    } catch (e) { dispatch({ type: 'WIZ_ERROR', err: apiError(e) }); }
  }, [st]);

  // ── Wizard: step 5 — finish ──
  const wizFinish = useCallback(() => {
    const { wiz } = st;
    if (!wiz.createdUser) { dispatch({ type: 'WIZ_CLOSE' }); return; }
    const roleName = st.roles.find(r => r.company_role_id === wiz.roleId)?.role_name ?? '';
    const isDriver = st.roles.find(r => r.company_role_id === wiz.roleId)?.role_code === 'DRIVER';
    dispatch({ type: 'WIZ_DONE', user: wiz.createdUser });
    showToast(isDriver ? `${wiz.createdUser.display_name} created as Driver. Set up pay rates next.` : `${wiz.createdUser.display_name} added${roleName ? ` as ${roleName}` : ''}.`);
  }, [st]);

  // ── Edit submit ──
  const submitEdit = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    dispatch({ type: 'EDIT_LOADING', val: true });
    try {
      const payload: UserUpdate = { display_name: st.edit.name.trim() || null, email: st.edit.email.trim() || null, phone: st.edit.phone.trim() || null, can_login: st.edit.canLogin, must_change_password: st.edit.mustChange };
      const r = await apiClient.patch<UserAdmin>(`/admin/users/${selected.user_id}`, payload);
      dispatch({ type: 'UPDATE_PERSON', person: r.data });
      dispatch({ type: 'EDIT_CLOSE' });
      showToast(`${r.data.display_name} updated.`);
    } catch (e) { dispatch({ type: 'EDIT_ERROR', err: apiError(e) }); }
  }, [st, selected]);

  // ── Reset password submit ──
  const submitReset = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    if (st.reset.pw.length < 8) { dispatch({ type: 'RESET_ERROR', err: 'Password must be at least 8 characters.' }); return; }
    dispatch({ type: 'RESET_LOADING', val: true });
    try {
      const payload: UserPasswordReset = { new_password: st.reset.pw, must_change_password: st.reset.mustChange };
      await apiClient.post(`/admin/users/${selected.user_id}/reset-password`, payload);
      dispatch({ type: 'RESET_CLOSE' });
      showToast('Password reset successfully.');
    } catch (e) { dispatch({ type: 'RESET_ERROR', err: apiError(e) }); }
  }, [st, selected]);

  // ── Activate/deactivate ──
  const confirmToggleActive = useCallback(async () => {
    const { userId, toActive } = st.confirmToggle;
    if (!userId) return;
    dispatch({ type: 'CONFIRM_TOGGLE_CLOSE' });
    try {
      const r = await apiClient.patch<UserAdmin>(`/admin/users/${userId}`, { is_active: toActive });
      dispatch({ type: 'UPDATE_PERSON', person: r.data });
      showToast(`${r.data.display_name} ${toActive ? 'activated' : 'deactivated'}.`);
    } catch (e) { showToast(apiError(e)); }
  }, [st.confirmToggle]);

  // ── Change role submit ──
  const submitChangeRole = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected || !st.changeRole.roleId) { dispatch({ type: 'CR_ERROR', err: 'Please select a role.' }); return; }
    if (st.changeRole.scope !== 'AllCompanyBranches' && !st.changeRole.branchId) { dispatch({ type: 'CR_ERROR', err: 'Please select a branch.' }); return; }
    dispatch({ type: 'CR_LOADING', val: true });
    try {
      const body: CompanyRoleAssignmentCreate = { company_role_id: st.changeRole.roleId, scope_type: st.changeRole.scope, branch_id: st.changeRole.scope === 'AllCompanyBranches' ? null : st.changeRole.branchId };
      await apiClient.post(`/admin/users/${selected.user_id}/company-role-assignments`, body);
      await refreshUser(selected.user_id);
      dispatch({ type: 'CR_CLOSE' });
      showToast('Role updated.');
    } catch (e) { dispatch({ type: 'CR_ERROR', err: apiError(e) }); }
  }, [st, selected]);

  // ── Extra perms submit ──
  const submitExtraPerms = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    dispatch({ type: 'EP_LOADING', val: true });
    try {
      const body: UserPermissionOverridesUpdate = { permission_codes: Array.from(st.extraPerms.codes) };
      await apiClient.put(`/admin/users/${selected.user_id}/permission-overrides`, body);
      await refreshUser(selected.user_id);
      dispatch({ type: 'EP_CLOSE' });
      showToast('Extra permissions updated.');
    } catch (e) { dispatch({ type: 'EP_ERROR', err: apiError(e) }); }
  }, [st, selected]);

  // ── Transfer submit ──
  const submitTransfer = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!st.transfer.targetId) return;
    if (st.transfer.confirmText.trim().toUpperCase() !== 'TRANSFER') { dispatch({ type: 'TRANSFER_ERROR', err: 'You must type TRANSFER to confirm.' }); return; }
    dispatch({ type: 'TRANSFER_LOADING', val: true });
    try {
      const body: OwnerTransferRequest = { target_user_id: st.transfer.targetId, replacement_company_role_id: st.transfer.replacementRoleId, confirmation: 'TRANSFER' };
      const r = await apiClient.post<OwnerTransferResult>('/admin/company-owner/transfer', body);
      dispatch({ type: 'TRANSFER_DONE', result: r.data });
      // Fix 3: The caller is always the outgoing owner. Force logout so they
      // re-authenticate with their updated (reduced) access level.
      showToast('Ownership transferred. Please sign in again with your updated access.');
      setTimeout(() => logout(), 2500);
    } catch (e) { dispatch({ type: 'TRANSFER_ERROR', err: apiError(e) }); }
  }, [st.transfer, logout]);

  if (st.loading) return <div className={styles.splash}>Loading people…</div>;
  if (st.error)   return <div className={styles.splash} style={{ color: '#dc2626' }}>{st.error}</div>;

  const targetPerson = st.transfer.targetId ? st.people.find(p => p.user_id === st.transfer.targetId) : null;

  return (
    <div className={styles.page}>
      {st.toast && <div className={styles.toast}>{st.toast}</div>}

      {/* ── Header ── */}
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>People</h1>
          <p className={styles.sub}>Add and manage everyone who can be assigned roles, access, or pay setup.</p>
        </div>
        {st.tab === 'people' && userCanCreate && (
          <button className={styles.addBtn} onClick={() => dispatch({ type: 'WIZ_OPEN' })}>+ Add Person</button>
        )}
      </div>

      {/* ── Tab bar ── */}
      <div className={styles.tabBar}>
        <button
          className={`${styles.tabBtn}${st.tab === 'people' ? ` ${styles.tabBtnActive}` : ''}`}
          onClick={() => dispatch({ type: 'SET_TAB', tab: 'people' })}
        >
          People
        </button>
        {userCanTransfers && (
          <button
            className={`${styles.tabBtn}${st.tab === 'transfers' ? ` ${styles.tabBtnActive}` : ''}`}
            onClick={() => dispatch({ type: 'SET_TAB', tab: 'transfers' })}
          >
            Transfer Requests
          </button>
        )}
      </div>

      {/* ── Transfer Requests tab ── */}
      {st.tab === 'transfers' && userCanTransfers && (
        <div className={styles.body}>
          <TransferRequestsTab branches={st.branches} />
        </div>
      )}

      {/* ── People tab body ── */}
      {st.tab === 'people' && <div className={styles.body}>

        {/* ── Left panel ── */}
        <aside className={styles.left}>
          {/* Filters */}
          <div className={styles.filters}>
            <input className={styles.search} placeholder="Search name, username, email…" value={st.search} onChange={e => dispatch({ type: 'SET_SEARCH', val: e.target.value })} />
            <div className={styles.filterRow}>
              <select className={styles.sel} value={st.filterStatus} onChange={e => dispatch({ type: 'SET_FILTER_STATUS', val: e.target.value as FilterStatus })}>
                <option value="active">Active</option>
                <option value="inactive">Inactive</option>
                <option value="all">All statuses</option>
              </select>
              <select className={styles.sel} value={st.filterRoleId ?? ''} onChange={e => dispatch({ type: 'SET_FILTER_ROLE', val: e.target.value ? parseInt(e.target.value) : null })}>
                <option value="">All roles</option>
                {st.roles.map(r => <option key={r.company_role_id} value={r.company_role_id}>{r.role_name}</option>)}
              </select>
            </div>
          </div>

          {/* List */}
          <div className={styles.list}>
            {filtered.length === 0 && <div className={styles.emptyList}>No people match your filters.</div>}
            {filtered.map(p => {
              const as = accessStatus(p);
              const isOwner = p.company_role_code === 'COMPANY_OWNER';
              return (
                <button key={p.user_id} className={`${styles.item}${st.selectedId === p.user_id ? ` ${styles.itemActive}` : ''}`} onClick={() => dispatch({ type: 'SELECT', id: p.user_id })}>
                  <div className={`${styles.av}${isOwner ? ` ${styles.avOwner}` : ''}`}>{avatarInitials(p.display_name)}</div>
                  <div className={styles.itemBody}>
                    <div className={styles.itemName}>
                      {p.display_name}
                      {isOwner && <span className={styles.ownerPill}>Owner</span>}
                      {!p.is_active && <span className={styles.pill} style={{ background: '#fee2e2', color: '#991b1b' }}>Inactive</span>}
                    </div>
                    <div className={styles.itemMeta}>{p.username}{p.email ? ` · ${p.email}` : ''}</div>
                    <div className={styles.itemRow}>
                      {p.company_role_name
                        ? <span className={styles.roleChip}>{p.company_role_name}</span>
                        : <span className={styles.noRole}>No role</span>
                      }
                      {p.company_role_scope && (
                        <span className={styles.scopeChip}>{scopeLabel(p.company_role_scope, p.company_role_branch_name)}</span>
                      )}
                    </div>
                  </div>
                  <div className={styles.itemRight}>
                    <span className={`${styles.asBadge} ${styles[`as_${as.kind}`]}`}>{as.label}</span>
                    {p.can_login && p.is_active && <span className={styles.loginDot} title="Can login" />}
                  </div>
                </button>
              );
            })}
          </div>
          <div className={styles.listFoot}>{filtered.length} / {st.people.length} people</div>
        </aside>

        {/* ── Right panel ── */}
        <main className={styles.right}>
          {!selected ? (
            <div className={styles.emptyRight}>
              <div className={styles.emptyRightCard}>
                <div className={styles.emptyRightIcon}>
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>
                    <path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
                  </svg>
                </div>
                <p className={styles.emptyRightTitle}>No person selected</p>
                <p className={styles.emptyRightMsg}>Choose a person from the list to view their access, role, and permissions.</p>
              </div>
            </div>
          ) : (
            <PersonDetail
              person={selected}
              roles={st.roles}
              branches={st.branches}
              allPerms={st.allPerms}
              amOwner={amOwner}
              canEditProfile={userCanEditPeople}
              canToggleActive={userCanToggle}
              canResetPassword={userCanEditPeople}
              canChangeRole={userCanChangeRole}
              canEditExtraPermissions={userCanEditPeople}
              onEdit={() => dispatch({ type: 'EDIT_OPEN', person: selected })}
              onToggleActive={() => dispatch({ type: 'CONFIRM_TOGGLE', userId: selected.user_id, toActive: !selected.is_active })}
              onResetPw={() => dispatch({ type: 'RESET_OPEN' })}
              onChangeRole={() => dispatch({ type: 'CR_OPEN', person: selected })}
              onEditExtraPerms={() => dispatch({ type: 'EP_OPEN', person: selected })}
              onTransfer={() => dispatch({ type: 'TRANSFER_OPEN', targetId: selected.user_id })}
              onGoPayRates={() => navigate(
                selected.driver_id
                  ? `/people/pay-rates?driverId=${selected.driver_id}`
                  : `/people/pay-rates?driverUserId=${selected.user_id}`
              )}
            />
          )}
        </main>
      </div>}

      {/* ── Modals ── */}

      {st.wiz.open && (
        <WizardModal
          wiz={st.wiz} roles={assignableRoles} branches={st.branches} allPerms={st.allPerms}
          dispatch={dispatch} onStep1={wizStep1} onStep3={wizStep3} onStep4={wizStep4} onFinish={wizFinish}
        />
      )}

      {st.edit.open && selected && (
        <Modal title="Edit Person" onClose={() => dispatch({ type: 'EDIT_CLOSE' })}>
          <form onSubmit={submitEdit} className={styles.form}>
            <Field label="Display Name *"><input className={styles.inp} value={st.edit.name} onChange={e => dispatch({ type: 'EDIT_FIELD', field: 'name', val: e.target.value })} required /></Field>
            <Field label="Email"><input className={styles.inp} type="email" value={st.edit.email} onChange={e => dispatch({ type: 'EDIT_FIELD', field: 'email', val: e.target.value })} /></Field>
            <Field label="Phone"><input className={styles.inp} value={st.edit.phone} onChange={e => dispatch({ type: 'EDIT_FIELD', field: 'phone', val: e.target.value })} /></Field>
            <div className={styles.checks}>
              <label><input type="checkbox" checked={st.edit.canLogin} onChange={e => dispatch({ type: 'EDIT_FIELD', field: 'canLogin', val: e.target.checked })} /> Can login</label>
              <label><input type="checkbox" checked={st.edit.mustChange} onChange={e => dispatch({ type: 'EDIT_FIELD', field: 'mustChange', val: e.target.checked })} /> Must change password on next login</label>
            </div>
            {st.edit.error && <ErrMsg msg={st.edit.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'EDIT_CLOSE' })} loading={st.edit.loading} label="Save Changes" />
          </form>
        </Modal>
      )}

      {st.reset.open && selected && (
        <Modal title="Reset Password" onClose={() => dispatch({ type: 'RESET_CLOSE' })}>
          <form onSubmit={submitReset} className={styles.form}>
            <p className={styles.helpTxt}>Setting a new password for <strong>{selected.display_name}</strong>.</p>
            <Field label="New Password *"><input className={styles.inp} type="password" value={st.reset.pw} onChange={e => dispatch({ type: 'RESET_PW', val: e.target.value })} minLength={8} required /></Field>
            <div className={styles.checks}>
              <label><input type="checkbox" checked={st.reset.mustChange} onChange={e => dispatch({ type: 'RESET_MUST', val: e.target.checked })} /> Require password change on next login</label>
            </div>
            {st.reset.error && <ErrMsg msg={st.reset.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'RESET_CLOSE' })} loading={st.reset.loading} label="Reset Password" />
          </form>
        </Modal>
      )}

      {st.changeRole.open && selected && (
        <Modal title="Change Role & Scope" onClose={() => dispatch({ type: 'CR_CLOSE' })}>
          <form onSubmit={submitChangeRole} className={styles.form}>
            <p className={styles.helpTxt}>Changing role for <strong>{selected.display_name}</strong>. The current role assignment will be revoked.</p>
            <ScopeFields roles={assignableRoles} branches={st.branches} roleId={st.changeRole.roleId} scope={st.changeRole.scope} branchId={st.changeRole.branchId} showRole
              onRole={id => dispatch({ type: 'CR_FIELD', field: 'roleId', val: id })}
              onScope={s => { dispatch({ type: 'CR_FIELD', field: 'scope', val: s }); if (s === 'AllCompanyBranches') dispatch({ type: 'CR_FIELD', field: 'branchId', val: null }); }}
              onBranch={b => dispatch({ type: 'CR_FIELD', field: 'branchId', val: b })}
            />
            {st.changeRole.error && <ErrMsg msg={st.changeRole.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'CR_CLOSE' })} loading={st.changeRole.loading} label="Save Role" />
          </form>
        </Modal>
      )}

      {st.extraPerms.open && selected && (
        <Modal title="Edit Extra Permissions" onClose={() => dispatch({ type: 'EP_CLOSE' })} wide>
          <form onSubmit={submitExtraPerms} className={styles.form}>
            <p className={styles.helpTxt}>Extra permissions for <strong>{selected.display_name}</strong>. These are added on top of the role's permissions. Use only for exceptions.</p>
            <PermissionPicker
              allPerms={st.allPerms}
              rolePerms={new Set(selected.role_permission_codes)}
              extraPerms={st.extraPerms.codes}
              onToggle={(code, checked) => dispatch({ type: 'EP_TOGGLE', code, checked })}
            />
            {st.extraPerms.error && <ErrMsg msg={st.extraPerms.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'EP_CLOSE' })} loading={st.extraPerms.loading} label="Save Extra Permissions" />
          </form>
        </Modal>
      )}

      {st.confirmToggle.open && (
        <ConfirmDialog
          open={st.confirmToggle.open}
          title={st.confirmToggle.toActive ? 'Activate Person' : 'Deactivate Person'}
          message={st.confirmToggle.toActive ? 'This will restore access for this person.' : 'This will prevent this person from logging in.'}
          confirmLabel={st.confirmToggle.toActive ? 'Activate' : 'Deactivate'}
          variant={st.confirmToggle.toActive ? 'primary' : 'danger'}
          onConfirm={confirmToggleActive}
          onCancel={() => dispatch({ type: 'CONFIRM_TOGGLE_CLOSE' })}
        />
      )}

      {st.transfer.open && targetPerson && (
        <Modal title="Transfer Company Ownership" onClose={() => dispatch({ type: 'TRANSFER_CLOSE' })} wide>
          <form onSubmit={submitTransfer} className={styles.form}>
            <div className={styles.xferWarn}>
              <span>⚠️</span>
              <div>
                <p><strong>You are about to transfer Company Owner authority to {targetPerson.display_name}.</strong></p>
                <p>You will no longer be Company Owner after this transfer. This action is audited and cannot be undone without the new owner's cooperation.</p>
              </div>
            </div>
            <div className={styles.xferSummary}>
              <span className={styles.fl}>New owner</span>
              <strong>{targetPerson.display_name}</strong> ({targetPerson.username})
            </div>
            <Field label="Replacement role for you after transfer (optional)">
              <select className={styles.inp} value={st.transfer.replacementRoleId ?? ''} onChange={e => dispatch({ type: 'TRANSFER_FIELD', field: 'replacementRoleId', val: e.target.value ? parseInt(e.target.value) : null })}>
                <option value="">— No replacement role —</option>
                {assignableRoles.map(r => <option key={r.company_role_id} value={r.company_role_id}>{r.role_name}</option>)}
              </select>
            </Field>
            <Field label={<>Type <strong>TRANSFER</strong> to confirm *</>}>
              <input className={`${styles.inp} ${styles.monoInput}`} value={st.transfer.confirmText} onChange={e => dispatch({ type: 'TRANSFER_FIELD', field: 'confirmText', val: e.target.value })} placeholder="TRANSFER" autoComplete="off" />
            </Field>
            {st.transfer.error && <ErrMsg msg={st.transfer.error} />}
            <div className={styles.mf}>
              <button type="button" className={styles.cancelBtn} onClick={() => dispatch({ type: 'TRANSFER_CLOSE' })}>Cancel</button>
              <button type="submit" className={styles.dangerBtn} disabled={st.transfer.loading || st.transfer.confirmText.trim().toUpperCase() !== 'TRANSFER'}>
                {st.transfer.loading ? 'Transferring…' : 'Transfer Ownership'}
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}

// ─── PersonDetail ─────────────────────────────────────────────────────────────

interface PersonDetailProps {
  person: UserAdmin;
  roles: CompanyRole[];
  branches: Branch[];
  allPerms: Permission[];
  amOwner: boolean;
  canEditProfile: boolean;
  canToggleActive: boolean;
  canResetPassword: boolean;
  canChangeRole: boolean;
  canEditExtraPermissions: boolean;
  onEdit(): void; onToggleActive(): void; onResetPw(): void;
  onChangeRole(): void; onEditExtraPerms(): void;
  onTransfer(): void; onGoPayRates(): void;
}

function PersonDetail({ person, allPerms, amOwner, canEditProfile, canToggleActive, canResetPassword, canChangeRole, canEditExtraPermissions, onEdit, onToggleActive, onResetPw, onChangeRole, onEditExtraPerms, onTransfer, onGoPayRates }: PersonDetailProps) {
  const permByCode = new Map(allPerms.map(p => [p.permission_code, p]));
  const as = accessStatus(person);
  const isOwner = person.company_role_code === 'COMPANY_OWNER';
  const isDriver = person.company_role_code === 'DRIVER';
  const canTransfer = amOwner && !isOwner && person.is_active && person.can_login;

  return (
    <div className={styles.detail}>
      {/* Person header card */}
      <div className={styles.dHeaderCard}>
        <div className={`${styles.dAv}${isOwner ? ` ${styles.dAvOwner}` : ''}`}>{avatarInitials(person.display_name)}</div>
        <div className={styles.dHeaderInfo}>
          <div className={styles.dName}>
            {person.display_name}
            {isOwner && <span className={styles.ownerPill}>Company Owner</span>}
            {isDriver && <span className={styles.driverPill}>Driver</span>}
          </div>
          <div className={styles.dSub}>@{person.username}</div>
          <div className={styles.dBadges}>
            <span className={person.is_active ? styles.badgeOk : styles.badgeErr}>{person.is_active ? 'Active' : 'Inactive'}</span>
            <span className={`${styles.asBadge} ${styles[`as_${as.kind}`]}`}>{as.label}</span>
            {person.can_login && person.is_active && <span className={styles.badgeLogin}>Login enabled</span>}
          </div>
          {/* Action buttons — horizontal row */}
          <div className={styles.dActions}>
            {canEditProfile && <button className={styles.actBtn} onClick={onEdit}>Edit</button>}
            {canToggleActive && (
              <button className={`${styles.actBtn}${person.is_active ? ` ${styles.actBtnDanger}` : ''}`} onClick={onToggleActive}>
                {person.is_active ? 'Deactivate' : 'Activate'}
              </button>
            )}
            {canResetPassword && <button className={styles.actBtn} onClick={onResetPw}>Reset Password</button>}
            {canChangeRole && !isOwner && <button className={styles.actBtn} onClick={onChangeRole}>Change Role</button>}
            {canTransfer && <button className={`${styles.actBtn} ${styles.actBtnOwner}`} onClick={onTransfer}>Transfer Ownership</button>}
            {isDriver && <button className={`${styles.actBtn} ${styles.actBtnDriver}`} onClick={onGoPayRates}>Pay Rates →</button>}
          </div>
        </div>
      </div>

      {/* Sections */}
      <div className={styles.sections}>

        {/* Profile */}
        <section className={styles.sec}>
          <h3 className={styles.secTitle}>Profile</h3>
          <div className={styles.grid2}>
            <span className={styles.fl}>Email</span><span>{person.email ?? '—'}</span>
            <span className={styles.fl}>Phone</span><span>{person.phone ?? '—'}</span>
            <span className={styles.fl}>Last login</span><span>{fmtDateTime(person.last_login_at_utc)}</span>
            <span className={styles.fl}>Member since</span><span>{fmtDate(person.created_at_utc)}</span>
          </div>
        </section>

        {/* Access */}
        <section className={styles.sec}>
          <h3 className={styles.secTitle}>Account Access</h3>
          <div className={styles.grid2}>
            <span className={styles.fl}>Can login</span><span>{person.can_login ? '✓ Yes' : '✗ No'}</span>
            <span className={styles.fl}>Must change password</span><span>{person.must_change_password ? 'Yes — on next login' : 'No'}</span>
            <span className={styles.fl}>Access status</span><span className={`${styles.asBadge} ${styles[`as_${as.kind}`]}`}>{as.label}</span>
          </div>
        </section>

        {/* Role & Scope */}
        <section className={styles.sec}>
          <h3 className={styles.secTitle}>Role & Scope</h3>
          {isOwner && (
            <div className={styles.ownerNote}>
              Company Owner has full access to all permissions. Role, scope, and extra permissions cannot be changed here. Use Transfer Ownership to hand over authority.
            </div>
          )}
          <div className={styles.grid2}>
            <span className={styles.fl}>Role</span>
            <span>{person.company_role_name ?? <span className={styles.noRole}>No role assigned</span>}</span>
            <span className={styles.fl}>Scope</span>
            <span>{scopeLabel(person.company_role_scope, person.company_role_branch_name)}</span>
            {person.role_permission_codes.length > 0 && (
              <>
                <span className={styles.fl}>Role permissions</span>
                <span className={styles.permCount}>{person.role_permission_codes.length} permissions from role</span>
              </>
            )}
          </div>
        </section>

        {/* Extra Permissions */}
        {!isOwner && (
          <section className={styles.sec}>
            <div className={styles.secTitleRow}>
              <h3 className={styles.secTitle}>Extra Permissions</h3>
              {canEditExtraPermissions && <button className={styles.secEditBtn} onClick={onEditExtraPerms}>Edit</button>}
            </div>
            {person.extra_permission_codes.length === 0 ? (
              <p className={styles.emptySecMsg}>No extra permissions. Role permissions apply.</p>
            ) : (
              <div className={styles.extraPermList}>
                {person.extra_permission_codes.map(code => (
                  <span key={code} className={styles.extraPermChip}>
                    {permByCode.get(code)?.permission_name ?? friendlyPermLabel(code)}
                  </span>
                ))}
              </div>
            )}
            <p className={styles.helpTxt}>Extra permissions are additions on top of this person's role. Use only for exceptions.</p>
          </section>
        )}

        {/* Driver section */}
        {isDriver && (
          <section className={styles.sec}>
            <h3 className={styles.secTitle}>Driver</h3>
            {person.driver_id ? (
              <div className={styles.grid2}>
                <span className={styles.fl}>Driver profile</span><span>Linked (ID {person.driver_id})</span>
                <span className={styles.fl}>Pay rates</span><span><button className={styles.linkBtn} onClick={onGoPayRates}>Go to Pay Rates →</button></span>
              </div>
            ) : (
              <div className={styles.driverGapBox}>
                <p>Driver profile not linked yet. Pay rates require a driver profile to be created separately via the Drivers module.</p>
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  );
}

// ─── WizardModal ──────────────────────────────────────────────────────────────

interface WizardModalProps {
  wiz: WizState;
  roles: CompanyRole[];
  branches: Branch[];
  allPerms: Permission[];
  dispatch(a: Action): void;
  onStep1(e: FormEvent): void;
  onStep3(): void;
  onStep4(): void;
  onFinish(): void;
}

const WIZ_STEPS = ['Person Info', 'Role', 'Scope', 'Extra Permissions', 'Review'] as const;

function WizardModal({ wiz, roles, branches, allPerms, dispatch, onStep1, onStep3, onStep4, onFinish }: WizardModalProps) {
  const selectedRole = roles.find(r => r.company_role_id === wiz.roleId);
  const isDriver = selectedRole?.role_code === 'DRIVER';
  // Role permissions for the selected role (for the extra perms step)
  // We don't have role perms in wiz state, but for the wizard we'll show all perms selectable
  // (role perms will be shown after creation in the detail panel)

  return (
    <div className={styles.overlay}>
      <div className={`${styles.modal} ${styles.wizModal}`}>
        <div className={styles.mHeader}>
          <h2>Add Person</h2>
          <button className={styles.closeBtn} onClick={() => dispatch({ type: 'WIZ_CLOSE' })}>✕</button>
        </div>

        {/* Step indicators */}
        <div className={styles.steps}>
          {WIZ_STEPS.map((label, i) => {
            const n = i + 1;
            const done = wiz.step > n;
            const active = wiz.step === n;
            return (
              <div key={label} className={`${styles.step}${active ? ` ${styles.stepActive}` : ''}${done ? ` ${styles.stepDone}` : ''}`}>
                <div className={styles.stepNum}>{done ? '✓' : n}</div>
                <div className={styles.stepLabel}>{label}</div>
              </div>
            );
          })}
        </div>

        <div className={styles.mBody}>

          {/* Step 1: Person info */}
          {wiz.step === 1 && (
            <form onSubmit={onStep1} className={styles.form}>
              <Field label="Display Name *"><input className={styles.inp} value={wiz.name} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'name', val: e.target.value })} placeholder="Full name" required /></Field>
              <Field label="Username *"><input className={styles.inp} value={wiz.username} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'username', val: e.target.value })} placeholder="lowercase username" required /></Field>
              <Field label="Password *"><input className={styles.inp} type="password" value={wiz.password} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'password', val: e.target.value })} minLength={8} required /></Field>
              <Field label="Email"><input className={styles.inp} type="email" value={wiz.email} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'email', val: e.target.value })} /></Field>
              <Field label="Phone"><input className={styles.inp} value={wiz.phone} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'phone', val: e.target.value })} /></Field>
              <div className={styles.checks}>
                <label><input type="checkbox" checked={wiz.canLogin} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'canLogin', val: e.target.checked })} /> Can login</label>
                <label><input type="checkbox" checked={wiz.mustChange} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'mustChange', val: e.target.checked })} /> Must change password on first login</label>
              </div>
              {wiz.error && <ErrMsg msg={wiz.error} />}
              <div className={styles.mf}>
                <button type="button" className={styles.cancelBtn} onClick={() => dispatch({ type: 'WIZ_CLOSE' })}>Cancel</button>
                <button type="submit" className={styles.primaryBtn} disabled={wiz.loading}>{wiz.loading ? 'Creating…' : 'Next: Role →'}</button>
              </div>
            </form>
          )}

          {/* Step 2: Role */}
          {wiz.step === 2 && (
            <div className={styles.form}>
              <p className={styles.helpTxt}><strong>Role</strong> determines what this person can do in the system. Company Owner is only transferable, not assignable here.</p>
              <div className={styles.roleGrid}>
                {roles.map(r => (
                  <button key={r.company_role_id} type="button"
                    className={`${styles.roleCard}${wiz.roleId === r.company_role_id ? ` ${styles.roleCardSel}` : ''}`}
                    onClick={() => dispatch({ type: 'WIZ_FIELD', field: 'roleId', val: r.company_role_id })}
                  >
                    <div className={styles.rcName}>{r.role_name}</div>
                    {r.role_code === 'DRIVER' && <div className={styles.rcHint}>Pay rates setup required after creation</div>}
                  </button>
                ))}
              </div>
              {wiz.error && <ErrMsg msg={wiz.error} />}
              <div className={styles.mf}>
                <button type="button" className={styles.cancelBtn} onClick={() => dispatch({ type: 'WIZ_STEP', step: 1 })}>← Back</button>
                <button type="button" className={styles.primaryBtn} disabled={!wiz.roleId} onClick={() => dispatch({ type: 'WIZ_STEP', step: 3 })}>Next: Scope →</button>
              </div>
            </div>
          )}

          {/* Step 3: Scope */}
          {wiz.step === 3 && (
            <div className={styles.form}>
              <p className={styles.helpTxt}><strong>Scope</strong> defines where this person's access applies — all branches or a specific one.</p>
              <ScopeFields roles={roles} branches={branches} roleId={wiz.roleId} scope={wiz.scope} branchId={wiz.branchId} showRole={false}
                onRole={() => {}}
                onScope={s => { dispatch({ type: 'WIZ_FIELD', field: 'scope', val: s }); if (s === 'AllCompanyBranches') dispatch({ type: 'WIZ_FIELD', field: 'branchId', val: null }); }}
                onBranch={b => dispatch({ type: 'WIZ_FIELD', field: 'branchId', val: b })}
              />
              {wiz.error && <ErrMsg msg={wiz.error} />}
              <div className={styles.mf}>
                <button type="button" className={styles.cancelBtn} onClick={() => dispatch({ type: 'WIZ_STEP', step: 2 })}>← Back</button>
                <button type="button" className={styles.primaryBtn} disabled={wiz.loading} onClick={onStep3}>{wiz.loading ? 'Saving…' : 'Next: Extra Permissions →'}</button>
              </div>
            </div>
          )}

          {/* Step 4: Extra permissions */}
          {wiz.step === 4 && (
            <div className={styles.form}>
              <p className={styles.helpTxt}><strong>Extra permissions</strong> are optional additions on top of the role. Permissions included by the role are shown as read-only.</p>
              <PermissionPicker
                allPerms={allPerms}
                rolePerms={new Set(wiz.createdUser?.role_permission_codes ?? [])}
                extraPerms={wiz.extraCodes}
                onToggle={(code, checked) => dispatch({ type: 'WIZ_EXTRA_TOGGLE', code, checked })}
              />
              {wiz.error && <ErrMsg msg={wiz.error} />}
              <div className={styles.mf}>
                <button type="button" className={styles.cancelBtn} onClick={() => dispatch({ type: 'WIZ_STEP', step: 3 })}>← Back</button>
                <button type="button" className={styles.primaryBtn} disabled={wiz.loading} onClick={onStep4}>{wiz.loading ? 'Saving…' : 'Next: Review →'}</button>
              </div>
            </div>
          )}

          {/* Step 5: Review */}
          {wiz.step === 5 && wiz.createdUser && (
            <div className={styles.form}>
              {wiz.partialSuccess && (
                <div className={styles.partialWarn}>⚠️ Person was created but role assignment failed. You can assign a role from the detail panel.</div>
              )}
              <div className={styles.reviewGrid}>
                <span className={styles.fl}>Name</span><span>{wiz.createdUser.display_name}</span>
                <span className={styles.fl}>Username</span><span>@{wiz.createdUser.username}</span>
                <span className={styles.fl}>Can login</span><span>{wiz.createdUser.can_login ? 'Yes' : 'No'}</span>
                <span className={styles.fl}>Role</span><span>{wiz.createdUser.company_role_name ?? '— not assigned —'}</span>
                <span className={styles.fl}>Scope</span><span>{scopeLabel(wiz.createdUser.company_role_scope, wiz.createdUser.company_role_branch_name)}</span>
                <span className={styles.fl}>Extra permissions</span>
                <span>{wiz.createdUser.extra_permission_codes.length > 0 ? `${wiz.createdUser.extra_permission_codes.length} permission(s)` : 'None'}</span>
              </div>
              {isDriver && (
                <div className={styles.driverNext}>🚛 <strong>Driver created.</strong> Set up pay rates next from the person's detail panel.</div>
              )}
              <div className={styles.mf}>
                <button type="button" className={styles.primaryBtn} onClick={onFinish}>Done</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── PermissionPicker ─────────────────────────────────────────────────────────

interface PermissionPickerProps {
  allPerms: Permission[];
  rolePerms: Set<string>;          // permissions already included by the role
  extraPerms: Set<string>;         // currently selected extra permissions
  onToggle(code: string, checked: boolean): void;
}

function PermissionPicker({ allPerms, rolePerms, extraPerms, onToggle }: PermissionPickerProps) {
  const groups = groupPerms(allPerms);
  return (
    <div className={styles.permPicker}>
      {groups.map(g => (
        <div key={g.module} className={styles.permGroup}>
          <div className={styles.permGroupTitle}>{g.label}</div>
          {g.perms.map(perm => {
            const code = perm.permission_code;
            const inRole = rolePerms.has(code);
            const inExtra = extraPerms.has(code);
            const parentCode = PERM_DEPS[code];
            const parentOff = parentCode && !rolePerms.has(parentCode) && !extraPerms.has(parentCode);
            return (
              <label key={code} className={`${styles.permRow}${inRole ? ` ${styles.permRowRole}` : ''}${parentOff && !inRole ? ` ${styles.permRowDim}` : ''}`}>
                <input
                  type="checkbox"
                  checked={inRole || inExtra}
                  disabled={inRole}
                  onChange={e => { if (!inRole) onToggle(code, e.target.checked); }}
                />
                <div>
                  <span className={styles.permName}>{perm.permission_name}</span>
                  {inRole && <span className={styles.permFromRole}> · included by role</span>}
                  {parentCode && !inRole && !inExtra && (
                  <span className={styles.permDep}> · requires {allPerms.find(p => p.permission_code === parentCode)?.permission_name ?? friendlyPermLabel(parentCode)}</span>
                )}
                </div>
              </label>
            );
          })}
        </div>
      ))}
    </div>
  );
}

// ─── ScopeFields ─────────────────────────────────────────────────────────────

interface ScopeFieldsProps {
  roles: CompanyRole[]; branches: Branch[];
  roleId: number | null;
  scope: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly';
  branchId: number | null;
  showRole?: boolean;
  onRole(id: number | null): void;
  onScope(s: 'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly'): void;
  onBranch(b: number | null): void;
}

function ScopeFields({ roles, branches, roleId, scope, branchId, showRole = true, onRole, onScope, onBranch }: ScopeFieldsProps) {
  const isDriverRole = roles.find(r => r.company_role_id === roleId)?.role_code === 'DRIVER';

  // If current scope is AllCompanyBranches but driver role selected, reset to OwnDriverDataOnly.
  useEffect(() => {
    if (isDriverRole && scope === 'AllCompanyBranches') {
      onScope('OwnDriverDataOnly');
    }
  }, [isDriverRole, scope, onScope]);

  return (
    <>
      {showRole && (
        <Field label="Role *">
          <select className={styles.inp} value={roleId ?? ''} onChange={e => onRole(e.target.value ? parseInt(e.target.value) : null)} required>
            <option value="">— Select a role —</option>
            {roles.map(r => <option key={r.company_role_id} value={r.company_role_id}>{r.role_name}</option>)}
          </select>
        </Field>
      )}
      <Field label="Scope *">
        <select className={styles.inp} value={scope} onChange={e => onScope(e.target.value as typeof scope)}>
          {!isDriverRole && (
            <option value="AllCompanyBranches">All company branches — full company access</option>
          )}
          <option value="SpecificBranch">Specific branch — limited to one branch</option>
          <option value="OwnDriverDataOnly">Own driver data only — driver's own records</option>
        </select>
      </Field>
      {isDriverRole && (
        <p className={styles.helpTxt}>
          🚛 Drivers need a home branch for payroll and pay rates. AllCompanyBranches scope is not available for driver roles.
        </p>
      )}
      {(scope === 'SpecificBranch' || scope === 'OwnDriverDataOnly') && (
        <Field label={scope === 'OwnDriverDataOnly' ? 'Home Branch (required) *' : 'Branch *'}>
          <select className={styles.inp} value={branchId ?? ''} onChange={e => onBranch(e.target.value ? parseInt(e.target.value) : null)} required>
            <option value="">— Select a branch —</option>
            {branches.map(b => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
          </select>
        </Field>
      )}
      {scope === 'OwnDriverDataOnly' && !isDriverRole && (
        <p className={styles.helpTxt}>This scope restricts the person to seeing only their own driver records. A home branch is required for system routing.</p>
      )}
    </>
  );
}

// ─── Small shared primitives ─────────────────────────────────────────────────

function Modal({ title, onClose, children, wide }: { title: string; onClose(): void; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className={styles.overlay}>
      <div className={`${styles.modal}${wide ? ` ${styles.modalWide}` : ''}`}>
        <div className={styles.mHeader}>
          <h2>{title}</h2>
          <button className={styles.closeBtn} onClick={onClose}>✕</button>
        </div>
        <div className={styles.mBody}>{children}</div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className={styles.field}>
      <label className={styles.lbl}>{label}</label>
      {children}
    </div>
  );
}

function ErrMsg({ msg }: { msg: string }) {
  return <div className={styles.errMsg}>{msg}</div>;
}

function ModalFoot({ onCancel, loading, label }: { onCancel(): void; loading: boolean; label: string }) {
  return (
    <div className={styles.mf}>
      <button type="button" className={styles.cancelBtn} onClick={onCancel}>Cancel</button>
      <button type="submit" className={styles.primaryBtn} disabled={loading}>{loading ? 'Saving…' : label}</button>
    </div>
  );
}
