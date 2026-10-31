import { makeAuthority, makeBranchAccess } from './authorityTestFixtures.ts';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
  canAccessCompanyBranches,
  canCreatePeople,
  canEditPayRates,
  canManageRoles,
  canManageSettingsAdmin,
  canViewAccess,
  canViewDailyPayItems,
  canViewPayRates,
  canViewPeopleHub,
  canViewSettings,
  canViewTransfers,
} from '../src/lib/permissions.ts';
import { toUserProfile } from '../src/store/authStore.ts';
import type { UserProfile } from '../src/store/authStore.ts';

function companyUser(...codes: string[]): UserProfile {
  return toUserProfile({
    user_id: 1,
    username: 'operator',
    display_name: 'Operator',
    company_id: 1,
    company_name: 'Demo Logistics',
    branches: [makeBranchAccess({ branch_id: null, scope: 'AllCompanyBranches' })],
    self_assignments: [],
    active_permissions: [],
    authority: makeAuthority({ company_permissions: codes }),
  });
}

const appShellSource = readFileSync(new URL('../src/components/AppShell.tsx', import.meta.url), 'utf8');

test('settings.manage alone manages company settings and reaches the Company & Branches page', () => {
  const user = companyUser('settings.manage');
  assert.equal(canManageSettingsAdmin(user), true);
  assert.equal(canAccessCompanyBranches(user), true);
  assert.equal(canViewSettings(user), true);
});

test('settings.manage alone opens no people, role, pay item, pay rate or transfer page', () => {
  const user = companyUser('settings.manage');
  assert.equal(canViewAccess(user), false);
  assert.equal(canViewPeopleHub(user), false);
  assert.equal(canCreatePeople(user), false);
  assert.equal(canManageRoles(user), false);
  assert.equal(canViewDailyPayItems(user), false);
  assert.equal(canViewPayRates(user), false);
  assert.equal(canEditPayRates(user, 10), false);
  assert.equal(canViewTransfers(user), false);
});

test('the retired setup.manage opens nothing', () => {
  const user = companyUser('setup.manage');
  assert.equal(canManageSettingsAdmin(user), false);
  assert.equal(canAccessCompanyBranches(user), false);
  assert.equal(canViewSettings(user), false);
  assert.equal(canViewAccess(user), false);
  assert.equal(canManageRoles(user), false);
  assert.equal(canViewDailyPayItems(user), false);
  assert.equal(canViewPayRates(user), false);
});

test('specific permissions open their own pages', () => {
  assert.equal(canViewAccess(companyUser('users.view')), true);
  assert.equal(canManageRoles(companyUser('roles.view')), true);
  assert.equal(canViewPayRates(companyUser('payrates.view')), true);
  assert.equal(canViewDailyPayItems(companyUser('payitems.view')), true);
});

test('AppShell: the Pay Items and Roles nav items follow their own route gates, not the settings gate', () => {
  assert.match(appShellSource, /if \(canViewDailyPayItems\(user\)\)\s*\n\s*settingsItems\.push\(\{ to: '\/settings\/pay-items'/);
  assert.match(appShellSource, /if \(canManageRoles\(user\)\)\s*\n\s*settingsItems\.push\(\{ to: '\/settings\/roles'/);
});
