import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { DriverAssignmentSummary, EmployeeDetail } from '../src/types/workforce.ts';
import {
  accessAccountPath,
  accountFacts,
  assignmentRows,
  assignmentState,
  buildCreatePayload,
  buildEmployeeUpdate,
  driverStateLabel,
  editFormFrom,
  employeeActions,
  EMPLOYEE_FORM0,
  payRatesPath,
  toListParams,
  transferRequestPath,
  FILTERS0,
  type WorkforceActionContext,
} from '../src/pages/people/workforce/workforceModel.ts';

const NOW = Date.parse('2026-06-01T12:00:00Z');

function assignment(id: number, from: string, to: string | null, branch = 10): DriverAssignmentSummary {
  return {
    driver_assignment_id: id, driver_id: 7, branch_id: branch, branch_name: `Branch ${branch}`,
    effective_from_at: from, effective_to_at: to, end_reason: to ? 'Transfer' : null,
    transferred_from_assignment_id: null, transferred_to_assignment_id: null,
  };
}

function employee(overrides: Partial<EmployeeDetail> = {}): EmployeeDetail {
  return {
    employee_id: 1, company_id: 1, branch_id: 10, branch_name: 'Branch 10', employee_key: 'E-1',
    full_name: 'Ada Driver', preferred_name: null, email: null, primary_phone: null, hire_date: null,
    employment_status: 'Active', termination_date: null,
    driver_state: 'none', driver: null, current_or_pending_assignment: null,
    driver_assignments: [], linked_user: null,
    ...overrides,
  };
}

function driverEmployee(overrides: Partial<EmployeeDetail> = {}): EmployeeDetail {
  const current = assignment(5, '2026-01-01T00:00:00Z', null);
  return employee({
    driver_state: 'current',
    driver: { driver_id: 7, driver_number: 'D-7', cdl_number: null, driver_status: 'Active' },
    current_or_pending_assignment: current,
    driver_assignments: [current],
    ...overrides,
  });
}

const FULL: WorkforceActionContext = {
  canManageEmployee: true, canViewPayRates: true, canRequestTransfer: true,
  access: { canView: true, canCreate: true, canEdit: true },
};
const WORKFORCE_ONLY: WorkforceActionContext = {
  canManageEmployee: true, canViewPayRates: false, canRequestTransfer: false,
  access: { canView: false, canCreate: false, canEdit: false },
};

test('Add Driver carries the driver block; Add Employee never does; neither carries a login', () => {
  const form = { ...EMPLOYEE_FORM0, branchId: 10, fullName: ' Ada ', driverNumber: 'D-1' };
  const asDriver = buildCreatePayload('driver', form);
  assert.ok(typeof asDriver !== 'string');
  assert.deepEqual(asDriver.driver, { driver_number: 'D-1', cdl_number: null });
  const asEmployee = buildCreatePayload('employee', form);
  assert.ok(typeof asEmployee !== 'string');
  assert.equal('driver' in asEmployee, false);
  for (const payload of [asDriver, asEmployee]) {
    for (const key of ['username', 'password', 'can_login', 'role_assignment', 'user']) {
      assert.equal(key in payload, false, key);
    }
  }
  assert.equal(asDriver.full_name, 'Ada');
});

test('creation requires a Branch and a name before any request is built', () => {
  assert.equal(buildCreatePayload('driver', { ...EMPLOYEE_FORM0, fullName: 'X' }), 'Select a Branch.');
  assert.equal(buildCreatePayload('employee', { ...EMPLOYEE_FORM0, branchId: 1 }), 'Full name is required.');
});

test('a Driver is read from the durable identity, never from an account or role code', () => {
  const driver = driverEmployee();
  assert.equal(driverStateLabel(driver), 'Driver — current assignment');
  assert.equal(driverStateLabel(employee()), 'Not a Driver');
  assert.equal(driverStateLabel(driverEmployee({ driver_state: 'pending' })), 'Driver — assignment starts later');
  assert.equal(driverStateLabel(driverEmployee({ driver_state: 'none' })), 'Driver — no current assignment');
});

test('assignment state and history are derived from tenures, newest first', () => {
  assert.equal(assignmentState(assignment(1, '2026-01-01T00:00:00Z', null), NOW), 'current');
  assert.equal(assignmentState(assignment(2, '2027-01-01T00:00:00Z', null), NOW), 'pending');
  assert.equal(assignmentState(assignment(3, '2025-01-01T00:00:00Z', '2026-01-01T00:00:00Z'), NOW), 'ended');
  const rows = assignmentRows({
    driver_assignments: [
      assignment(1, '2025-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 11),
      assignment(2, '2026-01-01T00:00:00Z', '2027-02-01T00:00:00Z', 12),
      assignment(3, '2027-02-01T00:00:00Z', null, 13),
    ],
  }, NOW);
  assert.deepEqual(rows.map((r) => [r.driver_assignment_id, r.state]), [[3, 'pending'], [2, 'current'], [1, 'ended']]);
});

test('Pay Rates and transfer links use the durable driverId, not a User id', () => {
  const driver = driverEmployee();
  assert.equal(payRatesPath(driver), '/people/pay-rates?driverId=7');
  assert.equal(transferRequestPath(driver), '/people/transfers?newFor=7');
  assert.equal(payRatesPath(employee()), null);
  assert.equal(transferRequestPath(employee()), null);
  assert.equal(accessAccountPath({ user_id: 42 }), '/people/access?user=42');
});

test('a Workforce-only operator sees no Access card and no Access actions', () => {
  const actions = employeeActions(driverEmployee(), WORKFORCE_ONLY);
  assert.equal(actions.showAccess, false);
  assert.equal(actions.openAccount, false);
  assert.equal(actions.createAccount, false);
  assert.equal(actions.linkAccount, false);
  assert.equal(actions.payRates, false);
  assert.equal(actions.requestTransfer, false);
  assert.equal(actions.edit, true);
  assert.equal(actions.terminate, true);
});

test('a fully authorized operator gets the contextual Access, transfer and Pay Rates actions', () => {
  const unlinked = employeeActions(driverEmployee(), FULL);
  assert.deepEqual(
    [unlinked.showAccess, unlinked.createAccount, unlinked.linkAccount, unlinked.openAccount, unlinked.payRates, unlinked.requestTransfer],
    [true, true, true, false, true, true],
  );
  const linked = employeeActions(driverEmployee({
    linked_user: { user_id: 9, username: 'ada', display_name: 'Ada', is_active: true, can_login: true, is_staged: false },
  }), FULL);
  assert.deepEqual([linked.openAccount, linked.createAccount, linked.linkAccount], [true, false, false]);
});

test('a transfer is only offered for a current Driver; a terminated Employee gets no lifecycle actions', () => {
  assert.equal(employeeActions(driverEmployee({ driver_state: 'pending' }), FULL).requestTransfer, false);
  assert.equal(employeeActions(employee(), FULL).requestTransfer, false);
  const terminated = employeeActions(driverEmployee({ employment_status: 'Terminated' }), FULL);
  assert.deepEqual([terminated.terminate, terminated.requestTransfer, terminated.createAccount], [false, false, false]);
  assert.equal(employeeActions(employee(), FULL).addDriver, true);
  assert.equal(employeeActions(driverEmployee(), FULL).addDriver, false);
});

test('account facts keep staged, active and login separate and handle no account', () => {
  assert.deepEqual(accountFacts(null), { linked: false, lifecycle: null, active: null, login: null });
  assert.deepEqual(
    accountFacts({ user_id: 1, username: 'u', display_name: 'U', is_active: false, can_login: true, is_staged: true }),
    { linked: true, lifecycle: 'Staged', active: 'Inactive', login: 'Login enabled' },
  );
});

test('filters become the canonical query and unset filters are not sent', () => {
  assert.deepEqual(toListParams(FILTERS0), { employment_status: 'Active' });
  assert.deepEqual(
    toListParams({ search: ' ada ', branchId: 3, employment: 'all', driver: 'pending' }),
    { q: 'ada', branch_id: 3, driver_state: 'pending' },
  );
});

test('editing a Driver never sends employment status, termination or Branch', () => {
  const driver = driverEmployee();
  const form = { ...editFormFrom(driver), fullName: 'Ada B', employmentStatus: 'Inactive', terminationDate: '2026-01-01', branchId: 99 };
  assert.deepEqual(buildEmployeeUpdate(driver, form), { full_name: 'Ada B' });
  const plain = employee();
  const plainForm = { ...editFormFrom(plain), employmentStatus: 'Inactive', branchId: 11 };
  assert.deepEqual(buildEmployeeUpdate(plain, plainForm), { employment_status: 'Inactive', branch_id: 11 });
});
