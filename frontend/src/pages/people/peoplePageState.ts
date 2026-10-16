import type { CompanyRole, OwnerTransferResult, Permission, UserAdmin } from '../../types/admin';
import type { Branch, PersonSummary } from '../../types/core';
import { applyToggle } from './peoplePermissionModel';

// ─── Page state ───────────────────────────────────────────────────────────────

export type FilterStatus = 'all' | 'active' | 'inactive';

export type PageTab = 'people' | 'transfers';

export interface PageState {
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
export interface WizState {
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
  employeeId: number | null;
  employeeOptions: PersonSummary[];
  scope: 'AllCompanyBranches' | 'SpecificBranch' | 'Self';
  branchId: number | null;
  extraCodes: Set<string>;
}

export interface EditState {
  open: boolean; loading: boolean; error: string | null;
  name: string; email: string; phone: string; canLogin: boolean; mustChange: boolean;
}

export interface ResetState {
  open: boolean; loading: boolean; error: string | null; pw: string; mustChange: boolean;
}

export interface ChangeRoleState {
  open: boolean; loading: boolean; error: string | null;
  roleId: number | null;
  scope: 'AllCompanyBranches' | 'SpecificBranch' | 'Self';
  branchId: number | null;
}

export interface ExtraPermsState {
  open: boolean; loading: boolean; error: string | null;
  codes: Set<string>;
}

export interface TransferState {
  open: boolean; loading: boolean; error: string | null;
  targetId: number | null; confirmText: string; replacementRoleId: number | null;
}

export type Action =
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
  | { type: 'WIZ_EMPLOYEES'; employees: PersonSummary[] }
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

export const WIZ0: WizState = {
  open: false, step: 1, loading: false, error: null, createdUser: null, partialSuccess: false,
  name: '', username: '', password: '', email: '', phone: '', canLogin: true, mustChange: true,
  roleId: null, employeeId: null, employeeOptions: [], scope: 'AllCompanyBranches', branchId: null, extraCodes: new Set(),
};
export const EDIT0: EditState = { open: false, loading: false, error: null, name: '', email: '', phone: '', canLogin: true, mustChange: false };
export const RESET0: ResetState = { open: false, loading: false, error: null, pw: '', mustChange: true };
export const CR0: ChangeRoleState = { open: false, loading: false, error: null, roleId: null, scope: 'AllCompanyBranches', branchId: null };
export const EP0: ExtraPermsState = { open: false, loading: false, error: null, codes: new Set() };
export const TRANSFER0: TransferState = { open: false, loading: false, error: null, targetId: null, confirmText: '', replacementRoleId: null };

export const INITIAL: PageState = {
  tab: 'people',
  people: [], roles: [], branches: [], allPerms: [],
  loading: true, error: null,
  selectedId: null, search: '', filterStatus: 'active', filterRoleId: null,
  wiz: WIZ0, edit: EDIT0, reset: RESET0, changeRole: CR0, extraPerms: EP0,
  confirmToggle: { open: false, userId: null, toActive: false },
  transfer: TRANSFER0,
  toast: null,
};

export function reducer(st: PageState, a: Action): PageState {
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
    case 'WIZ_USER_CREATED': return { ...st, wiz: { ...st.wiz, createdUser: a.user, partialSuccess: false, loading: false, step: 2, error: null } };
    case 'WIZ_PARTIAL': return { ...st, wiz: { ...st.wiz, partialSuccess: true, createdUser: a.user, loading: false, step: 3 } };
    case 'WIZ_DONE': return { ...st, wiz: { ...st.wiz, open: false, loading: false }, selectedId: a.user.user_id };
    case 'WIZ_FIELD': return { ...st, wiz: { ...st.wiz, [a.field]: a.val } };
    case 'WIZ_EMPLOYEES': return { ...st, wiz: { ...st.wiz, employeeOptions: a.employees } };
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
