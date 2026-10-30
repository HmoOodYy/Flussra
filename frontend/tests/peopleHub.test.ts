import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { makeAuthority, makeBranchAccess } from './authorityTestFixtures.ts';
import { toUserProfile } from '../src/store/authStore.ts';
import type { UserProfile } from '../src/store/authStore.ts';
import {
  canViewAccess,
  canViewPeopleHub,
  canViewTransfers,
  canViewWorkforce,
} from '../src/lib/permissions.ts';
import {
  defaultPeoplePath,
  peopleCapabilities,
  peopleChildForPath,
  visiblePeopleChildren,
} from '../src/pages/people/peopleHubModel.ts';

const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
const shellSource = readFileSync(new URL('../src/components/AppShell.tsx', import.meta.url), 'utf8');

function operator(opts: { company?: string[]; branch?: string[]; role?: string }): UserProfile {
  const branchPermissions = opts.branch && opts.branch.length > 0
    ? [{ branch_id: 10, permissions: opts.branch }] : [];
  return toUserProfile({
    user_id: 1,
    username: 'op',
    display_name: 'Operator',
    company_id: 1,
    company_name: 'Demo Logistics',
    branches: [makeBranchAccess({
      branch_id: opts.company ? null : 10,
      scope: opts.company ? 'AllCompanyBranches' : 'SpecificBranch',
      role_code: opts.role ?? 'BRANCH_MANAGER',
    })],
    self_assignments: [],
    active_permissions: [],
    authority: makeAuthority({ company_permissions: opts.company ?? [], branch_permissions: branchPermissions }),
  });
}

const workforceOnly = () => operator({ branch: ['employees.view'] });
const accessOnly = () => operator({ company: ['users.view'] });
const transfersOnly = () => operator({ branch: ['drivers.view'] });

test('each child area is authorized by its own permission family only', () => {
  const w = workforceOnly();
  assert.deepEqual([canViewWorkforce(w), canViewAccess(w), canViewTransfers(w)], [true, false, false]);
  const a = accessOnly();
  assert.deepEqual([canViewWorkforce(a), canViewAccess(a), canViewTransfers(a)], [false, true, false]);
  const t = transfersOnly();
  assert.deepEqual([canViewWorkforce(t), canViewAccess(t), canViewTransfers(t)], [false, false, true]);
});

test('Access is company-scoped: a branch-only users.view grant does not open it', () => {
  assert.equal(canViewAccess(operator({ branch: ['users.view'] })), false);
});

test('the hub is reachable when at least one child is reachable, and not otherwise', () => {
  assert.equal(canViewPeopleHub(workforceOnly()), true);
  assert.equal(canViewPeopleHub(accessOnly()), true);
  assert.equal(canViewPeopleHub(transfersOnly()), true);
  assert.equal(canViewPeopleHub(operator({ branch: ['payroll.view'] })), false);
});

test('tabs show only the authorized children, in Workforce, Access, Transfers order', () => {
  const labels = (user: UserProfile) => visiblePeopleChildren(peopleCapabilities(user)).map((c) => c.key);
  assert.deepEqual(labels(workforceOnly()), ['workforce']);
  assert.deepEqual(labels(accessOnly()), ['access']);
  assert.deepEqual(labels(transfersOnly()), ['transfers']);
  assert.deepEqual(labels(operator({ company: ['users.view', 'employees.view', 'drivers.view'] })), ['workforce', 'access', 'transfers']);
  assert.deepEqual(labels(operator({ company: ['users.view', 'drivers.view'] })), ['access', 'transfers']);
});

test('/people redirects to the first authorized child: Workforce, then Access, then Transfers', () => {
  const target = (user: UserProfile) => defaultPeoplePath(peopleCapabilities(user));
  assert.equal(target(operator({ company: ['users.view', 'employees.view', 'drivers.view'] })), '/people/workforce');
  assert.equal(target(operator({ company: ['users.view', 'drivers.view'] })), '/people/access');
  assert.equal(target(transfersOnly()), '/people/transfers');
  assert.equal(target(operator({ branch: ['payroll.view'] })), null);
});

test('company settings authority opens no child: settings.manage is not people authority', () => {
  const settingsManager = operator({ company: ['settings.manage'] });
  assert.deepEqual(visiblePeopleChildren(peopleCapabilities(settingsManager)).map((c) => c.key), []);
  assert.equal(canViewPeopleHub(settingsManager), false);
});

test('a Driver (Self) user never enters the hub', () => {
  const driver = toUserProfile({
    user_id: 2, username: 'd', display_name: 'D', company_id: 1, company_name: 'Demo',
    branches: [makeBranchAccess({ role_code: 'DRIVER', scope: 'Self' })],
    self_assignments: [], active_permissions: [],
    authority: makeAuthority({ branch_permissions: [{ branch_id: 10, permissions: ['employees.view', 'drivers.view'] }] }),
  });
  assert.equal(canViewPeopleHub(driver), false);
});

test('child paths resolve to their area, so a nested URL keeps the right tab active', () => {
  assert.equal(peopleChildForPath('/people/workforce'), 'workforce');
  assert.equal(peopleChildForPath('/people/access'), 'access');
  assert.equal(peopleChildForPath('/people/transfers'), 'transfers');
  assert.equal(peopleChildForPath('/people/pay-rates'), null);
});

test('each child route keeps its own gate and /people has an index redirect', () => {
  assert.match(appSource, /path="\/people" element=\{<Gate check=\{canViewPeopleHub\}>/);
  assert.match(appSource, /<Route index element=\{<PeopleDefaultRedirect \/>\} \/>/);
  assert.match(appSource, /path="workforce" element=\{<Gate check=\{canViewWorkforce\}>/);
  assert.match(appSource, /path="access" element=\{<Gate check=\{canViewAccess\}>/);
  assert.match(appSource, /path="transfers" element=\{<Gate check=\{canViewTransfers\}>/);
  assert.match(appSource, /path="\/people\/pay-rates"/);
});

test('the sidebar has one People & Access entry and Pay Rates stays a separate standalone route', () => {
  assert.equal((shellSource.match(/label: 'People & Access'/g) ?? []).length, 1);
  assert.match(shellSource, /excludePrefix: '\/people\/pay-rates'/);
  assert.doesNotMatch(shellSource, /canViewPeople\b/);
});
