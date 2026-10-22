import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { CompanyRole, EmployeeLinkCandidate, UserAdmin } from '../src/types/admin.ts';
import {
  ACCESS_FILTERS0,
  ACCOUNT_FORM0,
  PROVISION_FORM0,
  accessFacts,
  accessStatus,
  buildCreateAccountPayload,
  buildProvisionPayload,
  CANDIDATE_PAGE_SIZE,
  canLinkUser,
  eligibleCandidates,
  filterUsers,
  hasMoreCandidates,
  linkSummary,
  unlinkBlockedReason,
  withSelected,
} from '../src/pages/people/access/accessModel.ts';
import { accessReducer, ACCESS_INITIAL } from '../src/pages/people/access/accessState.ts';

function user(overrides: Partial<UserAdmin> = {}): UserAdmin {
  return {
    user_id: 1, company_id: 1, employee_id: null, employee_name: null, is_staged: false,
    username: 'u1', display_name: 'User One', email: null, phone: null, is_active: true,
    can_login: true, must_change_password: false, last_login_at_utc: null,
    created_at_utc: '2026-01-01T00:00:00Z', updated_at_utc: null, role_assignments: [],
    company_role_assignment_id: 1, company_role_id: 2, company_role_code: 'PAYROLL_ADMIN',
    company_role_name: 'Payroll admin', company_role_scope: 'AllCompanyBranches',
    company_role_branch_id: null, company_role_branch_name: null, driver_id: null,
    role_permission_codes: [], extra_permission_codes: [],
    ...overrides,
  };
}

const ROLES = [
  { company_role_id: 2, role_code: 'PAYROLL_ADMIN', is_active: true },
  { company_role_id: 3, role_code: 'DRIVER', is_active: true },
] as unknown as CompanyRole[];

function candidate(id: number, name: string, driverState: EmployeeLinkCandidate['driver_state']): EmployeeLinkCandidate {
  return {
    employee_id: id, employee_key: null, full_name: name, branch_id: 10, branch_name: 'Branch 10',
    employment_status: 'Active', driver_state: driverState,
  };
}

test('employment, active, login and staged are distinct account facts', () => {
  assert.deepEqual(accessFacts(user({ is_staged: true, is_active: false, can_login: false })),
    { lifecycle: 'Staged', active: 'Inactive', login: 'Login disabled' });
  assert.equal(accessStatus(user({ is_staged: true })).label, 'Staged');
  assert.equal(accessStatus(user({ is_active: false })).label, 'Inactive');
  assert.equal(accessStatus(user({ can_login: false })).label, 'Login disabled');
  assert.equal(accessStatus(user({ company_role_id: null })).label, 'No role assigned');
  assert.equal(accessStatus(user()).label, 'Provisioned');
});

test('Users with no Employee are first-class and appear in every non-link filter', () => {
  const users = [
    user({ user_id: 1, display_name: 'External Admin' }),
    user({ user_id: 2, display_name: 'Linked Driver', employee_id: 5, employee_name: 'Ada Driver', driver_id: 7 }),
    user({ user_id: 3, display_name: 'Staged One', is_staged: true, company_role_id: null }),
  ];
  assert.equal(filterUsers(users, ACCESS_FILTERS0).length, 3);
  assert.deepEqual(filterUsers(users, { ...ACCESS_FILTERS0, link: 'unlinked' }).map((u) => u.user_id), [1, 3]);
  assert.deepEqual(filterUsers(users, { ...ACCESS_FILTERS0, link: 'linked' }).map((u) => u.user_id), [2]);
  assert.deepEqual(filterUsers(users, { ...ACCESS_FILTERS0, link: 'staged' }).map((u) => u.user_id), [3]);
  assert.deepEqual(filterUsers(users, { ...ACCESS_FILTERS0, search: 'ada' }).map((u) => u.user_id), [2]);
});

test('link summary and unlink rules: a DRIVER role blocks unlinking, a stand-alone User cannot be unlinked', () => {
  const linked = user({ employee_id: 5, employee_name: 'Ada', driver_id: 7, company_role_code: 'DRIVER' });
  assert.deepEqual(linkSummary(linked), { linked: true, employeeId: 5, employeeName: 'Ada', isDriver: true });
  assert.match(unlinkBlockedReason(linked) ?? '', /Remove the DRIVER role/);
  assert.equal(unlinkBlockedReason(user({ employee_id: 5 })), null);
  assert.match(unlinkBlockedReason(user()) ?? '', /not linked/);
  assert.equal(canLinkUser(user()), true);
  assert.equal(canLinkUser(linked), false);
});

test('DRIVER access is offered only current Drivers; other roles take any unlinked Employee', () => {
  const all = [candidate(1, 'A', 'current'), candidate(2, 'B', 'none'), candidate(3, 'C', 'pending')];
  assert.deepEqual(eligibleCandidates({ role_code: 'DRIVER' }, all).map((c) => c.employee_id), [1]);
  assert.equal(eligibleCandidates({ role_code: 'PAYROLL_ADMIN' }, all).length, 3);
  assert.equal(eligibleCandidates(null, all).length, 3);
});

test('a provisioned account is one atomic request carrying its link and role', () => {
  const payload = buildCreateAccountPayload({
    ...ACCOUNT_FORM0, displayName: 'Ada', username: ' ADA ', password: 'longenough', roleId: 2, employeeId: 5,
  }, ROLES);
  assert.ok(typeof payload !== 'string');
  assert.equal(payload.username, 'ada');
  assert.equal(payload.employee_id, 5);
  assert.deepEqual(payload.role_assignment, { company_role_id: 2, scope_type: 'AllCompanyBranches', branch_id: null });
  assert.equal(payload.is_staged, false);
});

test('a staged account carries no role and cannot log in; a stand-alone account has no employee', () => {
  const payload = buildCreateAccountPayload({
    ...ACCOUNT_FORM0, mode: 'staged', displayName: 'Ext', username: 'ext', password: 'longenough',
  }, ROLES);
  assert.ok(typeof payload !== 'string');
  assert.deepEqual([payload.is_staged, payload.can_login, payload.role_assignment, payload.employee_id], [true, false, null, null]);
});

test('DRIVER access forces Self scope and requires the Employee; other roles reject Self', () => {
  const base = { ...ACCOUNT_FORM0, displayName: 'D', username: 'd', password: 'longenough', roleId: 3 };
  assert.match(String(buildCreateAccountPayload(base, ROLES)), /current Driver/);
  const ok = buildCreateAccountPayload({ ...base, employeeId: 5, scope: 'AllCompanyBranches' }, ROLES);
  assert.ok(typeof ok !== 'string');
  assert.deepEqual(ok.role_assignment, { company_role_id: 3, scope_type: 'Self', branch_id: null });
  assert.match(String(buildCreateAccountPayload({ ...base, roleId: 2, scope: 'Self' }, ROLES)), /DRIVER/);
});

test('provisioning applies only to a staged account and needs the Employee for DRIVER', () => {
  assert.match(String(buildProvisionPayload({ is_staged: false, employee_id: null }, { ...PROVISION_FORM0, roleId: 2 }, ROLES)), /staged/);
  assert.match(String(buildProvisionPayload({ is_staged: true, employee_id: null }, { ...PROVISION_FORM0, roleId: 3 }, ROLES)), /Link the current Driver/);
  const ok = buildProvisionPayload({ is_staged: true, employee_id: 5 }, { ...PROVISION_FORM0, roleId: 3 }, ROLES);
  assert.ok(typeof ok !== 'string');
  assert.equal(ok.role_assignment.scope_type, 'Self');
});

test('a chosen Employee survives paging and searching of the candidate list', () => {
  let st = accessReducer(ACCESS_INITIAL, { type: 'CREATE_OPEN' });
  const far = candidate(900, 'Zed Far Away', 'none');
  st = accessReducer(st, { type: 'CREATE_PICK', employee: far });
  assert.equal(st.create.form.employeeId, 900);
  assert.equal(st.create.form.displayName, 'Zed Far Away');
  st = accessReducer(st, { type: 'CREATE_CANDIDATES', candidates: [candidate(1, 'A', 'none')], more: false });
  assert.equal(st.create.form.employeeId, 900);
  assert.deepEqual(withSelected(st.create.candidates, st.create.selected).map((c) => c.employee_id), [900, 1]);
  assert.deepEqual(withSelected([far], far).map((c) => c.employee_id), [900]);
});

test('an Employee preselected by id resolves independently of the first candidate page', () => {
  let st = accessReducer(ACCESS_INITIAL, { type: 'CREATE_OPEN' });
  st = accessReducer(st, { type: 'CREATE_PRESELECT', employeeId: 900 });
  assert.equal(st.create.resolving, true);
  assert.equal(st.create.form.employeeId, 900);
  // The first page does not contain it: the preselection is not discarded.
  st = accessReducer(st, { type: 'CREATE_CANDIDATES', candidates: [candidate(1, 'A', 'none')], more: true });
  assert.equal(st.create.form.employeeId, 900);
  st = accessReducer(st, { type: 'CREATE_RESOLVED', employee: candidate(900, 'Zed Far Away', 'current') });
  assert.deepEqual([st.create.resolving, st.create.form.employeeId, st.create.form.displayName], [false, 900, 'Zed Far Away']);
  assert.equal(st.create.selected?.employee_id, 900);
});

test('a preselected Employee that cannot be found is cleared with an explanation', () => {
  let st = accessReducer(ACCESS_INITIAL, { type: 'CREATE_OPEN' });
  st = accessReducer(st, { type: 'CREATE_PRESELECT', employeeId: 5 });
  st = accessReducer(st, { type: 'CREATE_RESOLVED', employee: null });
  assert.deepEqual([st.create.resolving, st.create.form.employeeId, st.create.selected], [false, null, null]);
  assert.match(st.create.error ?? '', /not available/);
});

test('a typed display name is never overwritten by the chosen Employee name', () => {
  let st = accessReducer(ACCESS_INITIAL, { type: 'CREATE_OPEN' });
  st = accessReducer(st, { type: 'CREATE_FORM', form: { displayName: 'Custom Name' } });
  st = accessReducer(st, { type: 'CREATE_PICK', employee: candidate(5, 'Ada Driver', 'current') });
  assert.equal(st.create.form.displayName, 'Custom Name');
});

test('choosing the DRIVER role drops a chosen Employee that is not a current Driver, and keeps one that is', () => {
  const roles = [{ company_role_id: 3, role_code: 'DRIVER', is_active: true }] as unknown as CompanyRole[];
  const base = { ...accessReducer(ACCESS_INITIAL, { type: 'CREATE_OPEN' }), roles };
  const notDriver = accessReducer(base, { type: 'CREATE_PICK', employee: candidate(1, 'Plain', 'none') });
  const dropped = accessReducer(notDriver, { type: 'CREATE_FORM', form: { roleId: 3 } });
  assert.deepEqual([dropped.create.form.employeeId, dropped.create.selected], [null, null]);
  const driver = accessReducer(base, { type: 'CREATE_PICK', employee: candidate(2, 'Dana', 'current') });
  const kept = accessReducer(driver, { type: 'CREATE_FORM', form: { roleId: 3 } });
  assert.equal(kept.create.form.employeeId, 2);
});

test('candidate pages append, and a full page means there may be more', () => {
  assert.equal(hasMoreCandidates(CANDIDATE_PAGE_SIZE), true);
  assert.equal(hasMoreCandidates(CANDIDATE_PAGE_SIZE - 1), false);
  let st = accessReducer(ACCESS_INITIAL, { type: 'CREATE_OPEN' });
  st = accessReducer(st, { type: 'CREATE_CANDIDATES', candidates: [candidate(1, 'A', 'none')], more: true });
  st = accessReducer(st, { type: 'CREATE_CANDIDATES', candidates: [candidate(2, 'B', 'none')], more: false, append: true });
  assert.deepEqual(st.create.candidates.map((c) => c.employee_id), [1, 2]);
  assert.equal(st.create.candidatesMore, false);
  st = accessReducer(st, { type: 'LINK_OPEN' });
  st = accessReducer(st, { type: 'LINK_CANDIDATES', candidates: [candidate(3, 'C', 'none')], more: true });
  st = accessReducer(st, { type: 'LINK_CANDIDATES', candidates: [candidate(4, 'D', 'none')], more: false, append: true });
  assert.deepEqual(st.link.candidates.map((c) => c.employee_id), [3, 4]);
});

// ── The legacy User-as-Person People UI is gone ─────────────────────────────

test('no source file still depends on the retired People wizard or the /core/people picker', () => {
  const root = new URL('../src/', import.meta.url);
  const files: string[] = [];
  const walk = (dir: URL) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const next = new URL(`${entry.name}${entry.isDirectory() ? '/' : ''}`, dir);
      if (entry.isDirectory()) walk(next);
      else if (/\.(ts|tsx)$/.test(entry.name)) files.push(fileURLToPath(next));
    }
  };
  walk(root);
  const retired = /\/core\/people|PersonSummary|PeopleWizard|usePeopleController|peoplePageState|canViewPeople\b|listTransferDriverPickerPeople|TransferRequestsTab/;
  const offenders = files.filter((file) => retired.test(readFileSync(file, 'utf8')));
  assert.deepEqual(offenders, []);
});

test('the Transfer Requests driver picker reads the transfer-authorized candidate read', () => {
  const api = readFileSync(new URL('../src/lib/transferApi.ts', import.meta.url), 'utf8');
  assert.match(api, /\/driver-transfers\/driver-candidates/);
  assert.doesNotMatch(api, /\/core\/drivers/);
  const hook = readFileSync(new URL('../src/pages/people/transfers/useTransferRequests.ts', import.meta.url), 'utf8');
  assert.match(hook, /listTransferDriverCandidates\(\)/);
  // Eligibility is the backend's: no Driver work status or Branch filtering is re-derived here.
  assert.doesNotMatch(hook, /driver_status/);
  assert.doesNotMatch(hook, /canEditTransfers\(user, d\./);
  // A failed load is shown, never turned into a silently empty picker.
  assert.match(hook, /Could not load Drivers/);
});

test('account creation resolves a preselected Employee by id instead of searching the first page', () => {
  const actions = readFileSync(new URL('../src/pages/people/access/useAccessActions.ts', import.meta.url), 'utf8');
  assert.match(actions, /getEmployeeLinkCandidates\(\{ employee_id: preselectedId, limit: 1 \}\)/);
  assert.match(actions, /limit: CANDIDATE_PAGE_SIZE, offset/);
});
