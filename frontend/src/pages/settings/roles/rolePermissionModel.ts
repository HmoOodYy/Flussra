/**
 * Pure permission-grant data/logic for RolesPage — extracted so it can be
 * unit-tested with `node --test` (which cannot import .tsx). No React, no
 * apiClient, no CSS, no side effects.
 */

// ─── Permission grouping & metadata ──────────────────────────────────────────

export type BusinessGroupDef = {
  id: string;
  label: string;
  codes: readonly string[];
  note?: string;
};

// Business-domain groups — order and membership are explicit, independent of API module_code
export const BUSINESS_GROUP_DEFS: readonly BusinessGroupDef[] = [
  {
    id: 'org-admin',
    label: 'Organization Administration',
    codes: ['branches.create', 'users.view', 'users.create', 'users.edit', 'users.deactivate', 'roles.view', 'roles.create', 'roles.edit', 'roles.delete'],
  },
  {
    id: 'company-settings',
    label: 'Company Settings',
    codes: ['settings.manage'],
    note: 'Company profile, currency, branches and statuses. Does not include managing members, roles, pay items or pay rates.',
  },
  {
    id: 'payroll-ops',
    label: 'Payroll Operations',
    codes: ['payroll.view', 'payroll.period.create', 'payroll.entry', 'review.decide', 'payroll.finalize'],
  },
  {
    id: 'payroll-config',
    label: 'Payroll Configuration',
    codes: ['payitems.view', 'payitems.edit', 'payrates.view', 'payrates.edit'],
  },
  {
    id: 'payroll-setup',
    label: 'Payroll Setup Policy',
    codes: ['payroll_setup.view', 'payroll_setup.manage', 'payroll_setup.publish', 'payroll_setup.assign'],
    note: 'Effective only on All-Branches role assignments.',
  },
  {
    id: 'workforce',
    label: 'Workforce',
    codes: ['drivers.view', 'drivers.edit', 'employees.view', 'employees.manage'],
  },
  {
    id: 'reporting',
    label: 'Reporting',
    codes: ['reports.view', 'ledger.view', 'ledger.audit.view'],
  },
];

// Permissions excluded from the UI — preserved unchanged on every save via permsSt.codes.
// None of these is checked by any backend route or policy, so ticking them would grant nothing;
// the codes stay in the catalogue and any role that already holds one keeps it.
export const HIDDEN_PERM_CODES: ReadonlySet<string> = new Set([
  'company.view', 'company.edit',
  'branches.view', 'branches.edit',
  'dispatch.view', 'dispatch.edit',
  'drivers.create',
  'payroll.approve', 'payroll.edit',
  'settings.view',
]);

// Risk tiers — visual only, no enforcement
export type RiskTier = 'readonly' | 'write' | 'approval' | 'critical';

export function getRiskTier(code: string): RiskTier {
  if (code === 'payroll.finalize' || code === 'users.deactivate' || code === 'roles.delete' || code === 'payroll_setup.publish') return 'critical';
  if (code === 'payroll.approve' || code === 'review.decide' || code === 'roles.edit' || code === 'company.edit' || code === 'payroll_setup.assign') return 'approval';
  if (code.endsWith('.view')) return 'readonly';
  return 'write';
}

// Child → required parent permission (for dependency enforcement)
export const PERM_DEPS: Record<string, string> = {
  'company.edit':          'company.view',
  'branches.create':       'branches.view',
  'branches.edit':         'branches.view',
  'roles.create':          'roles.view',
  'roles.edit':            'roles.view',
  'roles.delete':          'roles.view',
  'users.create':          'users.view',
  'users.edit':            'users.view',
  'users.deactivate':      'users.view',
  'payroll.edit':          'payroll.view',
  'payroll.approve':       'payroll.view',
  'payroll.finalize':      'payroll.view',
  'payroll.entry':         'payroll.view',
  'payroll.period.create': 'payroll.view',
  'review.decide':         'payroll.view',
  'payitems.edit':         'payitems.view',
  'payrates.edit':         'payrates.view',
  'drivers.create':        'drivers.view',
  'drivers.edit':          'drivers.view',
  'dispatch.edit':         'dispatch.view',
  'settings.manage':       'settings.view',
  'employees.manage':      'employees.view',
  'ledger.audit.view':     'ledger.view',
  // UI administration dependency only: the Payroll Setup pages require payroll_setup.view to be usable.
  // The backend checks each endpoint's explicit code and does NOT imply view from manage/publish/assign.
  'payroll_setup.manage':  'payroll_setup.view',
  'payroll_setup.publish': 'payroll_setup.view',
  'payroll_setup.assign':  'payroll_setup.view',
};

// Parent → children that depend on it. Hidden children are included on purpose: the backend
// re-adds a missing parent on save, so unticking a visible parent must also drop the hidden
// child that would otherwise silently bring it back. Hidden codes with no visible parent
// toggle are never touched.
export const PERM_DEPENDENTS: Record<string, string[]> = {};
for (const [child, parent] of Object.entries(PERM_DEPS)) {
  if (!PERM_DEPENDENTS[parent]) PERM_DEPENDENTS[parent] = [];
  PERM_DEPENDENTS[parent].push(child);
}

// ─── State / reducers ─────────────────────────────────────────────────────────

export function setsEqual(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false;
  for (const v of a) if (!b.has(v)) return false;
  return true;
}

export type PermsState = { codes: Set<string>; savedCodes: Set<string>; loading: boolean; error: string; dirty: boolean };
export type PermsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; codes: string[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'TOGGLE'; code: string }
  | { type: 'SET_MANY'; codes: string[]; enable: boolean }
  | { type: 'RESET_DIRTY' };

export function permsReducer(s: PermsState, a: PermsAction): PermsState {
  switch (a.type) {
    case 'FETCH_START':   return { codes: new Set<string>(), savedCodes: new Set<string>(), loading: true, error: '', dirty: false };
    case 'FETCH_OK': {
      const codes = new Set<string>(a.codes);
      return { codes, savedCodes: codes, loading: false, error: '', dirty: false };
    }
    case 'FETCH_ERROR':   return { codes: new Set<string>(), savedCodes: new Set<string>(), loading: false, error: a.error, dirty: false };
    case 'TOGGLE': {
      const next = new Set<string>(s.codes);
      if (next.has(a.code)) {
        // Disabling: cascade-disable all dependents
        next.delete(a.code);
        for (const dep of PERM_DEPENDENTS[a.code] ?? []) {
          next.delete(dep);
        }
      } else {
        // Enabling: auto-enable required parent
        next.add(a.code);
        const parent = PERM_DEPS[a.code];
        if (parent) next.add(parent);
      }
      return { ...s, codes: next, dirty: !setsEqual(next, s.savedCodes) };
    }
    case 'SET_MANY': {
      const next = new Set<string>(s.codes);
      if (a.enable) {
        for (const code of a.codes) {
          next.add(code);
          const parent = PERM_DEPS[code];
          if (parent) next.add(parent);
        }
      } else {
        for (const code of a.codes) {
          next.delete(code);
          for (const dep of PERM_DEPENDENTS[code] ?? []) next.delete(dep);
        }
      }
      return { ...s, codes: next, dirty: !setsEqual(next, s.savedCodes) };
    }
    case 'RESET_DIRTY':   return { ...s, savedCodes: s.codes, dirty: false };
    default:              return s;
  }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

// A parent that is hidden cannot be ticked by the user, so a missing hidden parent is not a
// blocker to show ("requires …"); enabling the child adds it automatically.
export function isUserVisibleParentMissing(parent: string | undefined, codes: ReadonlySet<string>): boolean {
  return parent !== undefined && !HIDDEN_PERM_CODES.has(parent) && !codes.has(parent);
}

export function groupPermissionsByDomain<T extends { permission_code: string }>(perms: T[]): [BusinessGroupDef, T[]][] {
  const byCode = new Map(perms.map(p => [p.permission_code, p]));
  return BUSINESS_GROUP_DEFS
    .map(group => {
      const grouped = group.codes
        .filter(code => !HIDDEN_PERM_CODES.has(code))
        .map(code => byCode.get(code))
        .filter((p): p is T => p !== undefined);
      return [group, grouped] as [BusinessGroupDef, T[]];
    })
    .filter(([, grouped]) => grouped.length > 0);
}
