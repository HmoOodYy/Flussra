/**
 * Pure view model of the Access area. A User is its own record: it may or may not be linked to an
 * Employee, and it may be staged, provisioned, active/inactive and login-enabled/disabled. Those
 * are separate facts and are never folded into Workforce's employment status. No React, no I/O.
 */
import type {
  CompanyRole,
  CompanyRoleAssignmentCreate,
  EmployeeLinkCandidate,
  UserAdmin,
  UserCreate,
  UserProvision,
} from '../../../types/admin.ts';

// ─── Status labels ───────────────────────────────────────────────────────────

export type AccessStatus = { label: string; kind: 'ok' | 'warn' | 'err' };

/** The one badge a list row shows; the detail shows each underlying fact (see accessFacts). */
export function accessStatus(user: UserAdmin): AccessStatus {
  if (user.is_staged) return { label: 'Staged', kind: 'warn' };
  if (!user.is_active) return { label: 'Inactive', kind: 'err' };
  if (!user.can_login) return { label: 'Login disabled', kind: 'warn' };
  if (!user.company_role_id) return { label: 'No role assigned', kind: 'warn' };
  return { label: 'Provisioned', kind: 'ok' };
}

export interface AccessFacts {
  /** Staged (created, not yet given a role) or Provisioned. */
  lifecycle: 'Staged' | 'Provisioned';
  /** The User's active state. */
  active: 'Active' | 'Inactive';
  /** The User's login state. */
  login: 'Login enabled' | 'Login disabled';
}

export function accessFacts(user: UserAdmin): AccessFacts {
  return {
    lifecycle: user.is_staged ? 'Staged' : 'Provisioned',
    active: user.is_active ? 'Active' : 'Inactive',
    login: user.can_login ? 'Login enabled' : 'Login disabled',
  };
}

// ─── Employee link ───────────────────────────────────────────────────────────

/** Candidates are read a page at a time; a full page means there may be more. */
export const CANDIDATE_PAGE_SIZE = 50;

export function hasMoreCandidates(received: number): boolean {
  return received >= CANDIDATE_PAGE_SIZE;
}

/** The select's options: the loaded page plus the chosen Employee, which may sit on another page. */
export function withSelected(
  candidates: EmployeeLinkCandidate[],
  selected: EmployeeLinkCandidate | null,
): EmployeeLinkCandidate[] {
  if (selected === null || candidates.some((c) => c.employee_id === selected.employee_id)) return candidates;
  return [selected, ...candidates];
}

export interface LinkSummary {
  linked: boolean;
  employeeId: number | null;
  employeeName: string | null;
  /** The linked Employee is a permanent Driver. */
  isDriver: boolean;
}

export function linkSummary(user: UserAdmin): LinkSummary {
  return {
    linked: user.employee_id !== null,
    employeeId: user.employee_id,
    employeeName: user.employee_name,
    isDriver: user.driver_id !== null,
  };
}

/** Why a link cannot be removed right now, else null. Mirrors the backend precondition. */
export function unlinkBlockedReason(user: UserAdmin): string | null {
  if (user.employee_id === null) return 'This account is not linked to an Employee.';
  if (user.company_role_code === 'DRIVER') {
    return 'Remove the DRIVER role before unlinking the Employee.';
  }
  return null;
}

/** Linking is for an account with no Employee yet; an existing link is unlinked first. */
export function canLinkUser(user: UserAdmin): boolean {
  return user.employee_id === null;
}

/** DRIVER access needs an Employee with a current Driver assignment; any other role takes any Employee. */
export function eligibleCandidates(
  role: Pick<CompanyRole, 'role_code'> | null | undefined,
  candidates: EmployeeLinkCandidate[],
): EmployeeLinkCandidate[] {
  if (role?.role_code === 'DRIVER') return candidates.filter((c) => c.driver_state === 'current');
  return candidates;
}

// ─── List ────────────────────────────────────────────────────────────────────

export type StatusFilter = 'all' | 'active' | 'inactive';
export type LinkFilter = 'all' | 'linked' | 'unlinked' | 'staged';

export interface AccessFilters {
  search: string;
  status: StatusFilter;
  roleId: number | null;
  link: LinkFilter;
}

export const ACCESS_FILTERS0: AccessFilters = { search: '', status: 'active', roleId: null, link: 'all' };

/** Users with no Employee are first-class here: no filter ever requires a link. */
export function filterUsers(users: UserAdmin[], filters: AccessFilters): UserAdmin[] {
  const q = filters.search.trim().toLowerCase();
  return users
    .filter((user) => {
      if (q && !user.display_name.toLowerCase().includes(q)
        && !user.username.toLowerCase().includes(q)
        && !(user.email ?? '').toLowerCase().includes(q)
        && !(user.employee_name ?? '').toLowerCase().includes(q)) return false;
      if (filters.status === 'active' && !user.is_active) return false;
      if (filters.status === 'inactive' && user.is_active) return false;
      if (filters.roleId !== null && user.company_role_id !== filters.roleId) return false;
      if (filters.link === 'linked' && user.employee_id === null) return false;
      if (filters.link === 'unlinked' && user.employee_id !== null) return false;
      if (filters.link === 'staged' && !user.is_staged) return false;
      return true;
    })
    .sort((a, b) => a.display_name.localeCompare(b.display_name));
}

// ─── Account creation and provisioning: one atomic request each ──────────────

export type Scope = 'AllCompanyBranches' | 'SpecificBranch' | 'Self';

export interface AccountForm {
  /** Staged: created now, given a role later. Provisioned: created with its role in one step. */
  mode: 'staged' | 'provisioned';
  displayName: string;
  username: string;
  password: string;
  email: string;
  phone: string;
  mustChangePassword: boolean;
  canLogin: boolean;
  roleId: number | null;
  scope: Scope;
  branchId: number | null;
  employeeId: number | null;
}

export const ACCOUNT_FORM0: AccountForm = {
  mode: 'provisioned', displayName: '', username: '', password: '', email: '', phone: '',
  mustChangePassword: true, canLogin: true, roleId: null, scope: 'AllCompanyBranches',
  branchId: null, employeeId: null,
};

function blankToNull(value: string): string | null {
  return value.trim() === '' ? null : value.trim();
}

function roleAssignment(
  form: Pick<AccountForm, 'roleId' | 'scope' | 'branchId'>,
  roles: CompanyRole[],
  employeeId: number | null,
): CompanyRoleAssignmentCreate | string {
  if (form.roleId === null) return 'Select a role.';
  const role = roles.find((r) => r.company_role_id === form.roleId);
  if (!role) return 'Select a role.';
  if (role.role_code === 'DRIVER') {
    if (employeeId === null) return 'Select the current Driver Employee this account belongs to.';
    return { company_role_id: form.roleId, scope_type: 'Self', branch_id: null };
  }
  if (form.scope === 'Self') return 'Self scope is only for the DRIVER role.';
  if (form.scope === 'SpecificBranch' && form.branchId === null) return 'Select a Branch.';
  return {
    company_role_id: form.roleId,
    scope_type: form.scope,
    branch_id: form.scope === 'SpecificBranch' ? form.branchId : null,
  };
}

/**
 * The single request that creates an account. A provisioned account carries its Employee link
 * and role assignment in the same atomic call, so a retry or going back can never leave a partial
 * User; a staged account carries no role and cannot log in. Extra permissions are a separate,
 * explicit action on an existing account.
 */
export function buildCreateAccountPayload(form: AccountForm, roles: CompanyRole[]): UserCreate | string {
  if (!form.displayName.trim()) return 'Display name is required.';
  if (!form.username.trim()) return 'Username is required.';
  if (form.password.length < 8) return 'Password must be at least 8 characters.';
  const base = {
    username: form.username.trim().toLowerCase(),
    display_name: form.displayName.trim(),
    password: form.password,
    email: blankToNull(form.email),
    phone: blankToNull(form.phone),
    must_change_password: form.mustChangePassword,
    is_active: true,
    employee_id: form.employeeId,
  };
  if (form.mode === 'staged') {
    return { ...base, is_staged: true, can_login: false, role_assignment: null };
  }
  const assignment = roleAssignment(form, roles, form.employeeId);
  if (typeof assignment === 'string') return assignment;
  return { ...base, is_staged: false, can_login: form.canLogin, role_assignment: assignment };
}

export interface ProvisionForm {
  roleId: number | null;
  scope: Scope;
  branchId: number | null;
  canLogin: boolean;
}

export const PROVISION_FORM0: ProvisionForm = {
  roleId: null, scope: 'AllCompanyBranches', branchId: null, canLogin: true,
};

/** Only a staged account is provisioned; DRIVER access also needs the Employee already linked. */
export function buildProvisionPayload(
  user: Pick<UserAdmin, 'is_staged' | 'employee_id'>,
  form: ProvisionForm,
  roles: CompanyRole[],
): UserProvision | string {
  if (!user.is_staged) return 'Only a staged account can be provisioned.';
  const assignment = roleAssignment(form, roles, user.employee_id);
  if (typeof assignment === 'string') {
    return assignment.startsWith('Select the current Driver')
      ? 'Link the current Driver Employee to this account before assigning DRIVER access.'
      : assignment;
  }
  return { role_assignment: assignment, can_login: form.canLogin };
}
