import type {
  CompanyRole,
  EmployeeLinkCandidate,
  OwnerTransferResult,
  Permission,
  UserAdmin,
} from '../../../types/admin';
import type { Branch } from '../../../types/core';
import { applyToggle } from '../peoplePermissionModel.ts';
import {
  ACCESS_FILTERS0,
  ACCOUNT_FORM0,
  PROVISION_FORM0,
  type AccessFilters,
  type AccountForm,
  type ProvisionForm,
} from './accessModel.ts';

// ─── Dialog sub-states ───────────────────────────────────────────────────────

interface Busy { open: boolean; loading: boolean; error: string | null }

export interface CreateAccountState extends Busy {
  form: AccountForm;
  search: string;
  candidates: EmployeeLinkCandidate[];
  candidatesLoading: boolean;
  candidatesMore: boolean;
  /** The chosen Employee, kept whole so it survives paging and searching of the candidate list. */
  selected: EmployeeLinkCandidate | null;
  /** An Employee preselected by id (from Workforce) that is still being looked up. */
  resolving: boolean;
}

export interface ProvisionState extends Busy { form: ProvisionForm }

export interface LinkState extends Busy {
  search: string;
  candidates: EmployeeLinkCandidate[];
  candidatesLoading: boolean;
  candidatesMore: boolean;
  employeeId: number | null;
}

export interface EditState extends Busy {
  name: string; email: string; phone: string; canLogin: boolean; mustChange: boolean;
}

export interface ResetState extends Busy { pw: string; mustChange: boolean }

export interface ChangeRoleState extends Busy {
  roleId: number | null;
  scope: 'AllCompanyBranches' | 'SpecificBranch' | 'Self';
  branchId: number | null;
}

export interface ExtraPermsState extends Busy { codes: Set<string> }

export interface OwnerTransferState extends Busy {
  targetId: number | null; confirmText: string; replacementRoleId: number | null;
}

export interface AccessPageState {
  users: UserAdmin[];
  roles: CompanyRole[];
  branches: Branch[];
  allPerms: Permission[];
  loading: boolean;
  error: string | null;
  selectedId: number | null;
  filters: AccessFilters;
  create: CreateAccountState;
  provision: ProvisionState;
  link: LinkState;
  edit: EditState;
  reset: ResetState;
  changeRole: ChangeRoleState;
  extraPerms: ExtraPermsState;
  confirmToggle: { open: boolean; userId: number | null; toActive: boolean };
  transfer: OwnerTransferState;
  toast: string | null;
}

export type AccessAction =
  | { type: 'LOADED'; users: UserAdmin[]; roles: CompanyRole[]; branches: Branch[]; allPerms: Permission[] }
  | { type: 'LOAD_ERR'; error: string }
  | { type: 'SELECT'; id: number | null }
  | { type: 'FILTERS'; filters: Partial<AccessFilters> }
  | { type: 'UPDATE_USER'; user: UserAdmin }
  | { type: 'ADD_USER'; user: UserAdmin }
  | { type: 'TOAST'; msg: string | null }
  // Create account (one atomic request)
  | { type: 'CREATE_OPEN' }
  | { type: 'CREATE_CLOSE' }
  | { type: 'CREATE_FORM'; form: Partial<AccountForm> }
  | { type: 'CREATE_SEARCH'; val: string }
  | { type: 'CREATE_CANDIDATES'; candidates: EmployeeLinkCandidate[]; more: boolean; append?: boolean; loading?: boolean }
  | { type: 'CREATE_PICK'; employee: EmployeeLinkCandidate | null }
  | { type: 'CREATE_PRESELECT'; employeeId: number }
  | { type: 'CREATE_RESOLVED'; employee: EmployeeLinkCandidate | null }
  | { type: 'CREATE_LOADING'; val: boolean }
  | { type: 'CREATE_ERROR'; err: string | null }
  // Provision a staged account
  | { type: 'PROVISION_OPEN' }
  | { type: 'PROVISION_CLOSE' }
  | { type: 'PROVISION_FORM'; form: Partial<ProvisionForm> }
  | { type: 'PROVISION_LOADING'; val: boolean }
  | { type: 'PROVISION_ERROR'; err: string | null }
  // Link an Employee
  | { type: 'LINK_OPEN' }
  | { type: 'LINK_CLOSE' }
  | { type: 'LINK_SEARCH'; val: string }
  | { type: 'LINK_CANDIDATES'; candidates: EmployeeLinkCandidate[]; more: boolean; append?: boolean; loading?: boolean }
  | { type: 'LINK_PICK'; employeeId: number | null }
  | { type: 'LINK_LOADING'; val: boolean }
  | { type: 'LINK_ERROR'; err: string | null }
  // Edit
  | { type: 'EDIT_OPEN'; user: UserAdmin }
  | { type: 'EDIT_CLOSE' }
  | { type: 'EDIT_LOADING'; val: boolean }
  | { type: 'EDIT_ERROR'; err: string | null }
  | { type: 'EDIT_FIELD'; field: keyof EditState; val: unknown }
  // Reset password
  | { type: 'RESET_OPEN' }
  | { type: 'RESET_CLOSE' }
  | { type: 'RESET_LOADING'; val: boolean }
  | { type: 'RESET_ERROR'; err: string | null }
  | { type: 'RESET_PW'; val: string }
  | { type: 'RESET_MUST'; val: boolean }
  // Change role
  | { type: 'CR_OPEN'; user: UserAdmin }
  | { type: 'CR_CLOSE' }
  | { type: 'CR_LOADING'; val: boolean }
  | { type: 'CR_ERROR'; err: string | null }
  | { type: 'CR_FIELD'; field: keyof ChangeRoleState; val: unknown }
  // Extra permissions
  | { type: 'EP_OPEN'; user: UserAdmin }
  | { type: 'EP_CLOSE' }
  | { type: 'EP_LOADING'; val: boolean }
  | { type: 'EP_ERROR'; err: string | null }
  | { type: 'EP_TOGGLE'; code: string; checked: boolean }
  // Activate / deactivate
  | { type: 'CONFIRM_TOGGLE'; userId: number; toActive: boolean }
  | { type: 'CONFIRM_TOGGLE_CLOSE' }
  // Owner transfer
  | { type: 'TRANSFER_OPEN'; targetId: number }
  | { type: 'TRANSFER_CLOSE' }
  | { type: 'TRANSFER_LOADING'; val: boolean }
  | { type: 'TRANSFER_ERROR'; err: string | null }
  | { type: 'TRANSFER_FIELD'; field: keyof OwnerTransferState; val: unknown }
  | { type: 'TRANSFER_DONE'; result: OwnerTransferResult };

const CLOSED: Busy = { open: false, loading: false, error: null };

export const ACCESS_INITIAL: AccessPageState = {
  users: [], roles: [], branches: [], allPerms: [],
  loading: true, error: null, selectedId: null, filters: ACCESS_FILTERS0,
  create: { ...CLOSED, form: ACCOUNT_FORM0, search: '', candidates: [], candidatesLoading: false, candidatesMore: false, selected: null, resolving: false },
  provision: { ...CLOSED, form: PROVISION_FORM0 },
  link: { ...CLOSED, search: '', candidates: [], candidatesLoading: false, candidatesMore: false, employeeId: null },
  edit: { ...CLOSED, name: '', email: '', phone: '', canLogin: true, mustChange: false },
  reset: { ...CLOSED, pw: '', mustChange: true },
  changeRole: { ...CLOSED, roleId: null, scope: 'AllCompanyBranches', branchId: null },
  extraPerms: { ...CLOSED, codes: new Set() },
  confirmToggle: { open: false, userId: null, toActive: false },
  transfer: { ...CLOSED, targetId: null, confirmText: '', replacementRoleId: null },
  toast: null,
};

export function accessReducer(st: AccessPageState, a: AccessAction): AccessPageState {
  switch (a.type) {
    case 'LOADED': return { ...st, loading: false, error: null, users: a.users, roles: a.roles, branches: a.branches, allPerms: a.allPerms };
    case 'LOAD_ERR': return { ...st, loading: false, error: a.error };
    case 'SELECT': return { ...st, selectedId: a.id };
    case 'FILTERS': return { ...st, filters: { ...st.filters, ...a.filters } };
    case 'UPDATE_USER': return { ...st, users: st.users.map(u => u.user_id === a.user.user_id ? a.user : u) };
    case 'ADD_USER': return { ...st, users: [...st.users, a.user] };
    case 'TOAST': return { ...st, toast: a.msg };
    // Create account
    case 'CREATE_OPEN': return { ...st, create: { ...ACCESS_INITIAL.create, open: true } };
    case 'CREATE_CLOSE': return { ...st, create: { ...st.create, open: false } };
    case 'CREATE_FORM': {
      const form = { ...st.create.form, ...a.form };
      // DRIVER access takes only a current Driver: a chosen Employee that is not one is dropped.
      const role = st.roles.find(r => r.company_role_id === form.roleId);
      const dropEmployee = a.form.roleId !== undefined && form.mode === 'provisioned'
        && role?.role_code === 'DRIVER' && st.create.selected !== null && st.create.selected.driver_state !== 'current';
      return { ...st, create: {
        ...st.create,
        form: dropEmployee ? { ...form, employeeId: null } : form,
        selected: dropEmployee ? null : st.create.selected,
        error: null,
      } };
    }
    case 'CREATE_SEARCH': return { ...st, create: { ...st.create, search: a.val } };
    case 'CREATE_CANDIDATES': return { ...st, create: {
      ...st.create,
      candidates: a.append ? [...st.create.candidates, ...a.candidates] : a.candidates,
      candidatesMore: a.more,
      candidatesLoading: a.loading ?? false,
    } };
    case 'CREATE_PICK': return { ...st, create: {
      ...st.create,
      selected: a.employee,
      form: {
        ...st.create.form,
        employeeId: a.employee?.employee_id ?? null,
        displayName: a.employee && st.create.form.displayName.trim() === '' ? a.employee.full_name : st.create.form.displayName,
      },
      error: null,
    } };
    case 'CREATE_PRESELECT': return { ...st, create: {
      ...st.create, resolving: true, form: { ...st.create.form, employeeId: a.employeeId },
    } };
    case 'CREATE_RESOLVED': return a.employee
      ? { ...st, create: {
          ...st.create, resolving: false, selected: a.employee,
          form: {
            ...st.create.form,
            employeeId: a.employee.employee_id,
            displayName: st.create.form.displayName.trim() === '' ? a.employee.full_name : st.create.form.displayName,
          },
        } }
      : { ...st, create: {
          ...st.create, resolving: false, selected: null,
          form: { ...st.create.form, employeeId: null },
          error: 'That Employee is not available for a new account (it may already have one).',
        } };
    case 'CREATE_LOADING': return { ...st, create: { ...st.create, loading: a.val } };
    case 'CREATE_ERROR': return { ...st, create: { ...st.create, error: a.err, loading: false } };
    // Provision
    case 'PROVISION_OPEN': return { ...st, provision: { ...ACCESS_INITIAL.provision, open: true } };
    case 'PROVISION_CLOSE': return { ...st, provision: { ...st.provision, open: false } };
    case 'PROVISION_FORM': return { ...st, provision: { ...st.provision, form: { ...st.provision.form, ...a.form }, error: null } };
    case 'PROVISION_LOADING': return { ...st, provision: { ...st.provision, loading: a.val } };
    case 'PROVISION_ERROR': return { ...st, provision: { ...st.provision, error: a.err, loading: false } };
    // Link
    case 'LINK_OPEN': return { ...st, link: { ...ACCESS_INITIAL.link, open: true, candidatesLoading: true } };
    case 'LINK_CLOSE': return { ...st, link: { ...st.link, open: false } };
    case 'LINK_SEARCH': return { ...st, link: { ...st.link, search: a.val } };
    case 'LINK_CANDIDATES': return { ...st, link: {
      ...st.link,
      candidates: a.append ? [...st.link.candidates, ...a.candidates] : a.candidates,
      candidatesMore: a.more,
      candidatesLoading: a.loading ?? false,
    } };
    case 'LINK_PICK': return { ...st, link: { ...st.link, employeeId: a.employeeId, error: null } };
    case 'LINK_LOADING': return { ...st, link: { ...st.link, loading: a.val } };
    case 'LINK_ERROR': return { ...st, link: { ...st.link, error: a.err, loading: false } };
    // Edit
    case 'EDIT_OPEN': return { ...st, edit: { open: true, loading: false, error: null, name: a.user.display_name, email: a.user.email ?? '', phone: a.user.phone ?? '', canLogin: a.user.can_login, mustChange: a.user.must_change_password } };
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
    case 'CR_OPEN': return { ...st, changeRole: { open: true, loading: false, error: null, roleId: a.user.company_role_id, scope: a.user.company_role_scope ?? 'AllCompanyBranches', branchId: a.user.company_role_branch_id } };
    case 'CR_CLOSE': return { ...st, changeRole: { ...st.changeRole, open: false } };
    case 'CR_LOADING': return { ...st, changeRole: { ...st.changeRole, loading: a.val } };
    case 'CR_ERROR': return { ...st, changeRole: { ...st.changeRole, error: a.err, loading: false } };
    case 'CR_FIELD': return { ...st, changeRole: { ...st.changeRole, [a.field]: a.val } };
    // Extra permissions
    case 'EP_OPEN': return { ...st, extraPerms: { open: true, loading: false, error: null, codes: new Set(a.user.extra_permission_codes) } };
    case 'EP_CLOSE': return { ...st, extraPerms: { ...st.extraPerms, open: false } };
    case 'EP_LOADING': return { ...st, extraPerms: { ...st.extraPerms, loading: a.val } };
    case 'EP_ERROR': return { ...st, extraPerms: { ...st.extraPerms, error: a.err, loading: false } };
    case 'EP_TOGGLE': return { ...st, extraPerms: { ...st.extraPerms, codes: applyToggle(st.extraPerms.codes, a.code, a.checked) } };
    // Activate / deactivate
    case 'CONFIRM_TOGGLE': return { ...st, confirmToggle: { open: true, userId: a.userId, toActive: a.toActive } };
    case 'CONFIRM_TOGGLE_CLOSE': return { ...st, confirmToggle: { open: false, userId: null, toActive: false } };
    // Owner transfer
    case 'TRANSFER_OPEN': return { ...st, transfer: { ...ACCESS_INITIAL.transfer, open: true, targetId: a.targetId } };
    case 'TRANSFER_CLOSE': return { ...st, transfer: { ...st.transfer, open: false } };
    case 'TRANSFER_LOADING': return { ...st, transfer: { ...st.transfer, loading: a.val } };
    case 'TRANSFER_ERROR': return { ...st, transfer: { ...st.transfer, error: a.err, loading: false } };
    case 'TRANSFER_FIELD': return { ...st, transfer: { ...st.transfer, [a.field]: a.val } };
    case 'TRANSFER_DONE': return { ...st, transfer: { ...st.transfer, open: false, loading: false } };
    default: return st;
  }
}
