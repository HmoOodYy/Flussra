/**
 * Pure "duty" presentation layer over the role permission codes — extracted so it can be
 * unit-tested with `node --test`. A role is shown as a short list of plain duties, each set to a
 * level (Off / See / Manage). Duties add no permission codes and change nothing on the backend:
 * every level is just a fixed set of the existing visible permission codes.
 * No React, no apiClient, no CSS, no side effects.
 */

import { HIDDEN_PERM_CODES, PERM_DEPENDENTS, PERM_DEPS } from './rolePermissionModel.ts';

// ─── Types ───────────────────────────────────────────────────────────────────

export type DutyLevelId = 'off' | 'see' | 'manage';
/** Result of level detection: the held codes of a duty match no defined level exactly. */
export type CustomLevel = 'custom';
export type DutyLevelState = DutyLevelId | CustomLevel;

export type DutyLevel = {
  id: DutyLevelId;
  label: string;
  /** The FULL set of visible codes held at this level (cumulative, not a delta). */
  codes: readonly string[];
};

export type DutyDef = {
  id: string;
  label: string;
  description: string;
  /** Ordered; always starts with `off`. */
  levels: readonly DutyLevel[];
  /** Effective only for company-wide (all-branch) role assignments. */
  companyWideOnly: boolean;
  scopeNote?: string;
};

export type DutyLevelChange = {
  dutyId: string;
  label: string;
  before: DutyLevelState;
  after: DutyLevelState;
};

export type DutySummaryLine = {
  dutyId: string;
  label: string;
  level: DutyLevelState;
  levelLabel: string;
};

export const CUSTOM_LEVEL_LABEL = 'Custom';

// ─── Duty definitions ────────────────────────────────────────────────────────

const OFF: DutyLevel = { id: 'off', label: 'Off', codes: [] };

function seeManage(see: readonly string[], manage: readonly string[], manageLabel = 'Manage'): readonly DutyLevel[] {
  return [OFF, { id: 'see', label: 'See', codes: see }, { id: 'manage', label: manageLabel, codes: manage }];
}

function manageOnly(manage: readonly string[], manageLabel: string): readonly DutyLevel[] {
  return [OFF, { id: 'manage', label: manageLabel, codes: manage }];
}

export const DUTY_DEFS: readonly DutyDef[] = [
  {
    id: 'payroll',
    label: 'Payroll work',
    description: 'Look at payrolls, or start pay periods and enter the days and amounts.',
    levels: seeManage(
      ['payroll.view'],
      ['payroll.view', 'payroll.period.create', 'payroll.entry'],
      'Enter and prepare',
    ),
    companyWideOnly: false,
  },
  {
    id: 'review',
    label: 'Review payrolls',
    description: 'Check payrolls that were sent in, then approve them or send them back.',
    levels: manageOnly(['review.decide'], 'Approve or return'),
    companyWideOnly: false,
  },
  {
    id: 'finalize',
    label: 'Finalize payrolls',
    description: 'Close an approved payroll so it becomes final.',
    levels: manageOnly(['payroll.finalize'], 'Finalize'),
    companyWideOnly: false,
  },
  {
    id: 'people',
    label: 'People',
    description: 'Look at employees and drivers, or add them and keep their details up to date.',
    levels: seeManage(
      ['employees.view', 'drivers.view'],
      ['employees.view', 'employees.manage', 'drivers.view', 'drivers.edit'],
    ),
    companyWideOnly: false,
  },
  {
    id: 'payRates',
    label: 'Pay rates',
    description: 'Look at what people are paid, or change it.',
    levels: seeManage(['payrates.view'], ['payrates.view', 'payrates.edit']),
    companyWideOnly: false,
  },
  {
    id: 'payItems',
    label: 'Pay items',
    description: 'Look at the kinds of pay a payroll can include, or manage them.',
    levels: seeManage(['payitems.view'], ['payitems.view', 'payitems.edit']),
    companyWideOnly: false,
    scopeNote:
      'On one branch, managing means requesting pay items; company-wide it means creating them and deciding requests.',
  },
  {
    id: 'payCalendars',
    label: 'Pay calendars',
    description: 'Look at how and when each branch is paid, or set up, publish and assign pay calendars.',
    levels: seeManage(
      ['payroll_setup.view'],
      ['payroll_setup.view', 'payroll_setup.manage', 'payroll_setup.publish', 'payroll_setup.assign'],
    ),
    companyWideOnly: true,
  },
  {
    id: 'reports',
    label: 'Reports and finalized payrolls',
    description: 'Open reports and finalized payrolls, with or without the history of changes.',
    levels: seeManage(
      ['reports.view', 'ledger.view'],
      ['reports.view', 'ledger.view', 'ledger.audit.view'],
      'See with change history',
    ),
    companyWideOnly: false,
  },
  {
    id: 'signIn',
    label: 'Sign-in accounts',
    description: 'Look at who can sign in, or add, change and switch off sign-in accounts.',
    levels: seeManage(
      ['users.view'],
      ['users.view', 'users.create', 'users.edit', 'users.deactivate'],
    ),
    companyWideOnly: true,
  },
  {
    id: 'roles',
    label: 'Roles',
    description: 'Look at the roles people hold, or create, change and delete them.',
    levels: seeManage(
      ['roles.view'],
      ['roles.view', 'roles.create', 'roles.edit', 'roles.delete'],
    ),
    companyWideOnly: true,
  },
  {
    id: 'companySettings',
    label: 'Company settings',
    description: 'Change the company profile, currency, branches and statuses.',
    levels: manageOnly(['settings.manage'], 'Manage'),
    companyWideOnly: true,
  },
  {
    id: 'newBranches',
    label: 'Open new branches',
    description: 'Add a new branch to the company.',
    levels: manageOnly(['branches.create'], 'Allowed'),
    companyWideOnly: true,
  },
];

// ─── Lookups ─────────────────────────────────────────────────────────────────

export function getDuty(dutyId: string): DutyDef {
  const duty = DUTY_DEFS.find(d => d.id === dutyId);
  if (!duty) throw new Error(`Unknown duty: ${dutyId}`);
  return duty;
}

/** Every visible code the duty controls (the union of its level sets). */
export function dutyVisibleCodes(duty: DutyDef): readonly string[] {
  const all = new Set<string>();
  for (const level of duty.levels) for (const code of level.codes) all.add(code);
  return [...all].filter(code => !HIDDEN_PERM_CODES.has(code));
}

function getLevel(duty: DutyDef, level: DutyLevelId): DutyLevel {
  const found = duty.levels.find(l => l.id === level);
  if (!found) throw new Error(`Duty ${duty.id} has no level: ${level}`);
  return found;
}

export function dutyLevelLabel(duty: DutyDef, level: DutyLevelState): string {
  return level === 'custom' ? CUSTOM_LEVEL_LABEL : getLevel(duty, level).label;
}

// ─── Level detection ─────────────────────────────────────────────────────────

/** The duty's visible codes currently held — for an "advanced" view of a Custom duty. */
export function customDutyCodes(codes: ReadonlySet<string>, dutyId: string): string[] {
  return dutyVisibleCodes(getDuty(dutyId)).filter(code => codes.has(code));
}

function sameMembers(held: readonly string[], levelCodes: readonly string[]): boolean {
  return held.length === levelCodes.length && levelCodes.every(code => held.includes(code));
}

/** Hidden codes are ignored; held visible codes that equal no level's set exactly are 'custom'. */
export function dutyLevelOf(duty: DutyDef, codes: ReadonlySet<string>): DutyLevelState {
  const held = dutyVisibleCodes(duty).filter(code => codes.has(code));
  if (held.length === 0) return 'off';
  for (const level of duty.levels) {
    if (sameMembers(held, level.codes)) return level.id;
  }
  return 'custom';
}

export function dutyLevels(codes: ReadonlySet<string>): Record<string, DutyLevelState> {
  const result: Record<string, DutyLevelState> = {};
  for (const duty of DUTY_DEFS) result[duty.id] = dutyLevelOf(duty, codes);
  return result;
}

// ─── Changing a level ────────────────────────────────────────────────────────

/**
 * Sets one duty to a level and returns a new code set (the input is never mutated).
 * Dependency rules are the same ones the code-by-code editor uses: a code that is dropped also
 * drops its dependents (possibly belonging to other duties, or hidden); a code that is added also
 * adds its required parent (possibly hidden, e.g. settings.view). Hidden codes with no affected
 * relation are preserved. Codes the level keeps are not "removed", so their hidden dependents stay.
 */
export function applyDutyLevel(codes: ReadonlySet<string>, dutyId: string, level: DutyLevelId): Set<string> {
  const duty = getDuty(dutyId);
  const target = new Set(getLevel(duty, level).codes);
  const next = new Set<string>(codes);

  for (const code of dutyVisibleCodes(duty)) {
    if (target.has(code) || !next.has(code)) continue;
    next.delete(code);
    for (const dependent of PERM_DEPENDENTS[code] ?? []) next.delete(dependent);
  }
  for (const code of target) {
    next.add(code);
    const parent = PERM_DEPS[code];
    if (parent) next.add(parent);
  }
  return next;
}

/** The OTHER duties whose level changes if `dutyId` were set to `level`, so the UI can warn. */
export function dutyLevelChangeEffects(
  codes: ReadonlySet<string>,
  dutyId: string,
  level: DutyLevelId,
): DutyLevelChange[] {
  const after = applyDutyLevel(codes, dutyId, level);
  const changes: DutyLevelChange[] = [];
  for (const duty of DUTY_DEFS) {
    if (duty.id === dutyId) continue;
    const beforeLevel = dutyLevelOf(duty, codes);
    const afterLevel = dutyLevelOf(duty, after);
    if (beforeLevel !== afterLevel) {
      changes.push({ dutyId: duty.id, label: duty.label, before: beforeLevel, after: afterLevel });
    }
  }
  return changes;
}

// ─── Summary ─────────────────────────────────────────────────────────────────

/** Duties that are not Off, in display order — for a one-line role summary. */
export function summarizeDuties(codes: ReadonlySet<string>): DutySummaryLine[] {
  const lines: DutySummaryLine[] = [];
  for (const duty of DUTY_DEFS) {
    const level = dutyLevelOf(duty, codes);
    if (level === 'off') continue;
    lines.push({ dutyId: duty.id, label: duty.label, level, levelLabel: dutyLevelLabel(duty, level) });
  }
  return lines;
}
