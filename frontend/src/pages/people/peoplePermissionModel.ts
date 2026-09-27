/**
 * Pure permission-grant data/logic for PeoplePage — extracted so it can be
 * unit-tested with `node --test` (which cannot import .tsx). No React, no
 * apiClient, no CSS, no side effects.
 */

// ─── Permission metadata (mirrors RolesPage) ─────────────────────────────────

export const MODULE_LABELS: Record<string, string> = {
  company:       'Company & Branches',
  roles:         'Roles & Permissions',
  users:         'Members',
  payroll:       'Payroll',
  payroll_setup: 'Payroll Setup Policy',
  payitems:      'Pay Items',
  payrates:      'Pay Rates',
  drivers:       'Drivers',
  dispatch:      'Dispatch / Operations',
  reports:       'Reports',
  settings:      'Settings',
};
export const MODULE_ORDER = Object.keys(MODULE_LABELS);

// Per-module notes shown under a group's header in the permission picker.
// payroll_setup: per-user overrides only become company-wide authority when
// the member holds an All-Branches assignment — backend builds
// company_permissions only in that case.
export const MODULE_NOTES: Record<string, string> = {
  payroll_setup: 'Effective only for members with an All-Branches role assignment.',
};

export const PERM_DEPS: Record<string, string> = {
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
  // UI administration dependency only: the Payroll Setup pages require payroll_setup.view to be usable.
  // The backend checks each endpoint's explicit code and does NOT imply view from manage/publish/assign.
  'payroll_setup.manage':  'payroll_setup.view',
  'payroll_setup.publish': 'payroll_setup.view',
  'payroll_setup.assign':  'payroll_setup.view',
};
export const PERM_DEPENDENTS: Record<string, string[]> = {};
for (const [child, parent] of Object.entries(PERM_DEPS)) {
  (PERM_DEPENDENTS[parent] ??= []).push(child);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

// Group permissions by module, sorted by MODULE_ORDER
export function groupPerms<T extends { permission_code: string; module_code: string }>(
  perms: T[]
): Array<{ module: string; label: string; perms: T[]; note?: string }> {
  const map = new Map<string, T[]>();
  for (const p of perms) {
    if (!map.has(p.module_code)) map.set(p.module_code, []);
    map.get(p.module_code)!.push(p);
  }
  return MODULE_ORDER
    .filter(m => map.has(m))
    .map(m => ({ module: m, label: MODULE_LABELS[m] ?? m, perms: map.get(m)!, note: MODULE_NOTES[m] }));
}

// Apply PERM_DEPS: adding a code also adds its parent; removing a code removes its dependents
export function applyToggle(current: Set<string>, code: string, checked: boolean): Set<string> {
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
