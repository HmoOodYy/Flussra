/**
 * Pure view model of the Workforce area: Employee → optional permanent Driver →
 * DriverAssignments. Everything here derives from the canonical Workforce reads; nothing infers a
 * Driver from an Access role code, and the Access summary is only produced for an operator who
 * holds Access authority. No React, no I/O.
 */
import type {
  DriverAssignmentSummary,
  DriverUpdatePayload,
  DriverWorkStatus,
  EmployeeCreatePayload,
  EmployeeDetail,
  EmployeeListParams,
  EmployeeSummary,
  EmployeeUpdatePayload,
  EmploymentStatus,
  LinkedUserSummary,
  WorkforceDriver,
} from '../../../types/workforce.ts';

// ─── Labels: employment, Driver and account state are different facts ─────────

export function employmentLabel(status: string): string {
  return `Employment: ${status}`;
}

export function driverStateLabel(employee: Pick<EmployeeSummary, 'driver_state' | 'driver'>): string {
  switch (employee.driver_state) {
    case 'current': return 'Driver — current assignment';
    case 'pending': return 'Driver — assignment starts later';
    default: return employee.driver ? 'Driver — no current assignment' : 'Not a Driver';
  }
}

export interface AccountFacts {
  /** Whether an account exists for the Employee at all. */
  linked: boolean;
  /** Staged (not provisioned yet) or Provisioned. */
  lifecycle: 'Staged' | 'Provisioned' | null;
  /** The User's active state. */
  active: 'Active' | 'Inactive' | null;
  /** The User's login state. */
  login: 'Login enabled' | 'Login disabled' | null;
}

/** The three account facts Workforce and Access must never blur: lifecycle, active state, login. */
export function accountFacts(user: LinkedUserSummary | null): AccountFacts {
  if (user === null) return { linked: false, lifecycle: null, active: null, login: null };
  return {
    linked: true,
    lifecycle: user.is_staged ? 'Staged' : 'Provisioned',
    active: user.is_active ? 'Active' : 'Inactive',
    login: user.can_login ? 'Login enabled' : 'Login disabled',
  };
}

// ─── Driver assignments ──────────────────────────────────────────────────────

export type AssignmentState = 'current' | 'pending' | 'ended';

export function assignmentState(
  assignment: Pick<DriverAssignmentSummary, 'effective_from_at' | 'effective_to_at'>,
  nowMs: number,
): AssignmentState {
  const from = Date.parse(assignment.effective_from_at);
  const to = assignment.effective_to_at === null ? null : Date.parse(assignment.effective_to_at);
  if (to !== null && to <= from) return 'ended'; // never effective
  if (from > nowMs) return 'pending';
  if (to !== null && to <= nowMs) return 'ended';
  return 'current';
}

export interface AssignmentRow extends DriverAssignmentSummary {
  state: AssignmentState;
}

/** Newest tenure first, each with its state at `nowMs`. */
export function assignmentRows(detail: Pick<EmployeeDetail, 'driver_assignments'>, nowMs: number): AssignmentRow[] {
  return detail.driver_assignments
    .map((assignment) => ({ ...assignment, state: assignmentState(assignment, nowMs) }))
    .sort((a, b) => Date.parse(b.effective_from_at) - Date.parse(a.effective_from_at)
      || b.driver_assignment_id - a.driver_assignment_id);
}

// ─── Links to other areas (durable Driver identity, never a User id) ─────────

/** Pay Rates opens a Driver by its permanent id. */
export function payRatesPath(employee: Pick<EmployeeSummary, 'driver'>): string | null {
  return employee.driver ? `/people/pay-rates?driverId=${employee.driver.driver_id}` : null;
}

/** The Transfer Requests area, with the Driver preselected for a new request. */
export function transferRequestPath(employee: Pick<EmployeeSummary, 'driver'>): string | null {
  return employee.driver ? `/people/transfers?newFor=${employee.driver.driver_id}` : null;
}

/** The Access area, opened on the Employee's account. */
export function accessAccountPath(user: Pick<LinkedUserSummary, 'user_id'>): string {
  return `/people/access?user=${user.user_id}`;
}

// ─── Action availability ─────────────────────────────────────────────────────

export interface WorkforceActionContext {
  /** employees.manage for the Employee's Branch. */
  canManageEmployee: boolean;
  canViewPayRates: boolean;
  /** drivers.edit for the Driver's current Branch (a transfer starts from it). */
  canRequestTransfer: boolean;
  /** Access authority of the operator, kept apart from Workforce authority. */
  access: { canView: boolean; canCreate: boolean; canEdit: boolean };
}

export interface EmployeeActions {
  edit: boolean;
  addDriver: boolean;
  editDriver: boolean;
  requestTransfer: boolean;
  terminate: boolean;
  payRates: boolean;
  /** The Access card at all: only for an operator with Access authority. */
  showAccess: boolean;
  openAccount: boolean;
  createAccount: boolean;
  linkAccount: boolean;
}

export function employeeActions(detail: EmployeeDetail, context: WorkforceActionContext): EmployeeActions {
  const active = detail.employment_status !== 'Terminated';
  const hasDriver = detail.driver !== null;
  const hasAccount = detail.linked_user !== null;
  return {
    edit: context.canManageEmployee,
    addDriver: context.canManageEmployee && !hasDriver && active,
    editDriver: context.canManageEmployee && hasDriver,
    // A transfer moves a Driver whose home assignment is current, between Branches.
    requestTransfer: context.canRequestTransfer && detail.driver_state === 'current' && active,
    terminate: context.canManageEmployee && hasDriver && active,
    payRates: context.canViewPayRates && hasDriver,
    showAccess: context.access.canView,
    openAccount: context.access.canView && hasAccount,
    createAccount: context.access.canCreate && context.access.canEdit && !hasAccount && active,
    linkAccount: context.access.canEdit && !hasAccount && active,
  };
}

// ─── List filters ────────────────────────────────────────────────────────────

export type EmploymentFilter = 'all' | 'Active' | 'Inactive' | 'Terminated';
export type DriverFilter = 'all' | 'current' | 'pending' | 'none';

export interface WorkforceFilters {
  search: string;
  branchId: number | null;
  employment: EmploymentFilter;
  driver: DriverFilter;
}

export const FILTERS0: WorkforceFilters = { search: '', branchId: null, employment: 'Active', driver: 'all' };

/** The canonical Workforce query for a filter set; unset filters are not sent. */
export function toListParams(filters: WorkforceFilters): EmployeeListParams {
  const params: EmployeeListParams = {};
  if (filters.search.trim()) params.q = filters.search.trim();
  if (filters.branchId !== null) params.branch_id = filters.branchId;
  if (filters.employment !== 'all') params.employment_status = filters.employment;
  if (filters.driver !== 'all') params.driver_state = filters.driver;
  return params;
}

// ─── Creation ────────────────────────────────────────────────────────────────

export interface EmployeeForm {
  branchId: number | null;
  fullName: string;
  preferredName: string;
  employeeKey: string;
  email: string;
  phone: string;
  hireDate: string;
  driverNumber: string;
  cdlNumber: string;
}

export const EMPLOYEE_FORM0: EmployeeForm = {
  branchId: null, fullName: '', preferredName: '', employeeKey: '', email: '', phone: '',
  hireDate: '', driverNumber: '', cdlNumber: '',
};

export type CreateKind = 'driver' | 'employee';

function blankToNull(value: string): string | null {
  return value.trim() === '' ? null : value.trim();
}

/**
 * The one atomic request of Add Driver / Add Employee. Add Driver carries the `driver` block so
 * the Employee, permanent Driver and first assignment are created together; Add Employee never
 * does. Neither carries any login, username or password: an account is an Access matter.
 */
export function buildCreatePayload(kind: CreateKind, form: EmployeeForm): EmployeeCreatePayload | string {
  if (form.branchId === null) return 'Select a Branch.';
  if (!form.fullName.trim()) return 'Full name is required.';
  const payload: EmployeeCreatePayload = {
    branch_id: form.branchId,
    full_name: form.fullName.trim(),
    preferred_name: blankToNull(form.preferredName),
    employee_key: blankToNull(form.employeeKey),
    email: blankToNull(form.email),
    primary_phone: blankToNull(form.phone),
    hire_date: blankToNull(form.hireDate),
  };
  if (kind === 'driver') {
    payload.driver = {
      driver_number: blankToNull(form.driverNumber),
      cdl_number: blankToNull(form.cdlNumber),
    };
  }
  return payload;
}

// ─── Editing ─────────────────────────────────────────────────────────────────

export interface EmployeeEditForm {
  fullName: string;
  preferredName: string;
  email: string;
  phone: string;
  hireDate: string;
  employmentStatus: string;
  terminationDate: string;
  branchId: number | null;
}

export function editFormFrom(detail: EmployeeDetail): EmployeeEditForm {
  return {
    fullName: detail.full_name,
    preferredName: detail.preferred_name ?? '',
    email: detail.email ?? '',
    phone: detail.primary_phone ?? '',
    hireDate: detail.hire_date ?? '',
    employmentStatus: detail.employment_status,
    terminationDate: detail.termination_date ?? '',
    branchId: detail.branch_id,
  };
}

/**
 * Employment lifecycle and Branch are not ordinary fields of a Driver: they move through the
 * termination and transfer workflows. For a Driver the edit form omits them entirely, and for any
 * Employee only changed fields are sent.
 */
export function employmentFieldsEditable(detail: Pick<EmployeeDetail, 'driver' | 'driver_state'>): boolean {
  return detail.driver === null;
}

export function buildEmployeeUpdate(
  detail: EmployeeDetail, form: EmployeeEditForm,
): EmployeeUpdatePayload | string {
  if (!form.fullName.trim()) return 'Full name is required.';
  const update: EmployeeUpdatePayload = {};
  const text = (value: string) => (value.trim() === '' ? null : value.trim());
  if (form.fullName.trim() !== detail.full_name) update.full_name = form.fullName.trim();
  if (text(form.preferredName) !== detail.preferred_name) update.preferred_name = text(form.preferredName);
  if (text(form.email) !== detail.email) update.email = text(form.email);
  if (text(form.phone) !== detail.primary_phone) update.primary_phone = text(form.phone);
  if (text(form.hireDate) !== detail.hire_date) update.hire_date = text(form.hireDate);
  if (employmentFieldsEditable(detail)) {
    if (form.employmentStatus !== detail.employment_status) {
      update.employment_status = form.employmentStatus as EmploymentStatus;
    }
    if (text(form.terminationDate) !== detail.termination_date) {
      update.termination_date = text(form.terminationDate);
    }
    if (form.branchId !== null && form.branchId !== detail.branch_id) update.branch_id = form.branchId;
  }
  return update;
}

export interface DriverForm { driverNumber: string; cdlNumber: string; status: string }

export function driverFormFrom(driver: WorkforceDriver | null): DriverForm {
  return {
    driverNumber: driver?.driver_number ?? '',
    cdlNumber: driver?.cdl_number ?? '',
    status: driver?.driver_status ?? 'Active',
  };
}

export function buildDriverUpdate(driver: WorkforceDriver, form: DriverForm): DriverUpdatePayload {
  const text = (value: string) => (value.trim() === '' ? null : value.trim());
  const update: DriverUpdatePayload = {};
  if (text(form.driverNumber) !== driver.driver_number) update.driver_number = text(form.driverNumber);
  if (text(form.cdlNumber) !== driver.cdl_number) update.cdl_number = text(form.cdlNumber);
  if (form.status !== driver.driver_status) update.driver_status = form.status as DriverWorkStatus;
  return update;
}
