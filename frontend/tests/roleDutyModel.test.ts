import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  applyDutyLevel,
  customDutyCodes,
  DUTY_DEFS,
  dutyLevelChangeEffects,
  dutyLevelLabel,
  dutyLevelOf,
  dutyLevels,
  dutyVisibleCodes,
  getDuty,
  summarizeDuties,
} from '../src/pages/settings/roles/roleDutyModel.ts';
import type { DutyDef } from '../src/pages/settings/roles/roleDutyModel.ts';
import {
  BUSINESS_GROUP_DEFS,
  HIDDEN_PERM_CODES,
  permsReducer,
} from '../src/pages/settings/roles/rolePermissionModel.ts';
import type { PermsState } from '../src/pages/settings/roles/rolePermissionModel.ts';

const set = (...codes: string[]) => new Set(codes);
const sorted = (codes: Iterable<string>) => [...codes].sort();
const duty = (id: string): DutyDef => getDuty(id);

const VISIBLE_CODES = BUSINESS_GROUP_DEFS.flatMap(g => g.codes).filter(c => !HIDDEN_PERM_CODES.has(c));

// ─── Coverage ────────────────────────────────────────────────────────────────

test('every visible permission code belongs to exactly one duty', () => {
  const owners = new Map<string, string[]>();
  for (const d of DUTY_DEFS) {
    for (const code of dutyVisibleCodes(d)) owners.set(code, [...(owners.get(code) ?? []), d.id]);
  }
  for (const code of VISIBLE_CODES) {
    assert.equal(owners.get(code)?.length, 1, `${code} should belong to exactly one duty, got ${owners.get(code)}`);
  }
});

test('duties cover no code the editor does not show', () => {
  const visible = new Set(VISIBLE_CODES);
  for (const d of DUTY_DEFS) {
    for (const level of d.levels) {
      for (const code of level.codes) {
        assert.ok(visible.has(code), `${d.id} lists ${code}, which is not a visible editor code`);
        assert.ok(!HIDDEN_PERM_CODES.has(code), `${d.id} lists hidden code ${code}`);
      }
    }
  }
});

test('duties are well formed: unique ids, off first, cumulative levels, plain descriptions', () => {
  assert.equal(new Set(DUTY_DEFS.map(d => d.id)).size, DUTY_DEFS.length);
  for (const d of DUTY_DEFS) {
    assert.equal(d.levels[0].id, 'off');
    assert.deepEqual(d.levels[0].codes, []);
    assert.ok(d.levels.length >= 2, `${d.id} needs a level above off`);
    assert.equal(new Set(d.levels.map(l => l.id)).size, d.levels.length);
    for (let i = 2; i < d.levels.length; i++) {
      for (const code of d.levels[i - 1].codes) {
        assert.ok(d.levels[i].codes.includes(code), `${d.id}: ${d.levels[i].id} must include ${code}`);
      }
    }
    assert.ok(d.description.length > 0);
    assert.doesNotMatch(d.description, /\b[a-z_]+\.[a-z_.]+\b/, `${d.id} description must not show codes`);
  }
});

test('duties that only work company-wide are flagged and the others are not', () => {
  const companyWide = DUTY_DEFS.filter(d => d.companyWideOnly).map(d => d.id);
  assert.deepEqual(sorted(companyWide), sorted(['payCalendars', 'signIn', 'roles', 'companySettings', 'newBranches']));
  assert.ok(duty('payItems').scopeNote);
});

// ─── Level detection ─────────────────────────────────────────────────────────

test('a duty with none of its codes is off', () => {
  for (const d of DUTY_DEFS) assert.equal(dutyLevelOf(d, set()), 'off');
});

test('each level is detected from exactly its code set', () => {
  for (const d of DUTY_DEFS) {
    for (const level of d.levels) {
      assert.equal(dutyLevelOf(d, new Set(level.codes)), level.id, `${d.id} ${level.id}`);
    }
  }
});

test('a partial or mixed set of a duty is custom', () => {
  assert.equal(dutyLevelOf(duty('payCalendars'), set('payroll_setup.view', 'payroll_setup.publish')), 'custom');
  assert.equal(dutyLevelOf(duty('payroll'), set('payroll.view', 'payroll.entry')), 'custom');
  assert.equal(dutyLevelOf(duty('people'), set('employees.view')), 'custom');
  assert.equal(dutyLevelOf(duty('reports'), set('reports.view')), 'custom');
  assert.equal(dutyLevelOf(duty('signIn'), set('users.view', 'users.edit')), 'custom');
  // a child without its parent is not a defined level either
  assert.equal(dutyLevelOf(duty('payRates'), set('payrates.edit')), 'custom');
});

test('hidden codes never change a duty level', () => {
  assert.equal(dutyLevelOf(duty('people'), set('drivers.create')), 'off');
  assert.equal(dutyLevelOf(duty('people'), set('employees.view', 'drivers.view', 'drivers.create')), 'see');
  assert.equal(dutyLevelOf(duty('payroll'), set('payroll.view', 'payroll.edit', 'payroll.approve')), 'see');
  assert.equal(dutyLevelOf(duty('companySettings'), set('settings.view')), 'off');
});

test('codes of other duties do not affect a duty level', () => {
  const codes = set('payroll.view', 'review.decide', 'payrates.view');
  assert.equal(dutyLevelOf(duty('payroll'), codes), 'see');
  assert.equal(dutyLevelOf(duty('review'), codes), 'manage');
  assert.equal(dutyLevelOf(duty('payRates'), codes), 'see');
  assert.equal(dutyLevelOf(duty('finalize'), codes), 'off');
});

test('dutyLevels reports every duty', () => {
  const levels = dutyLevels(set('payroll.view', 'payroll.finalize', 'payroll_setup.manage'));
  assert.deepEqual(Object.keys(levels), DUTY_DEFS.map(d => d.id));
  assert.equal(levels.payroll, 'see');
  assert.equal(levels.finalize, 'manage');
  assert.equal(levels.payCalendars, 'custom');
  assert.equal(levels.roles, 'off');
});

test('custom duty codes list the visible codes currently held', () => {
  const codes = set('payroll_setup.view', 'payroll_setup.publish', 'payroll.view', 'settings.view');
  assert.deepEqual(customDutyCodes(codes, 'payCalendars'), ['payroll_setup.view', 'payroll_setup.publish']);
  assert.deepEqual(customDutyCodes(codes, 'companySettings'), []);
  assert.deepEqual(customDutyCodes(set('drivers.create'), 'people'), []);
});

// ─── Applying a level ────────────────────────────────────────────────────────

test('applying any level to any duty round-trips to that level', () => {
  const starts = [set(), new Set(VISIBLE_CODES)];
  for (const start of starts) {
    for (const d of DUTY_DEFS) {
      for (const level of d.levels) {
        const next = applyDutyLevel(start, d.id, level.id);
        assert.equal(dutyLevelOf(d, next), level.id, `${d.id} -> ${level.id}`);
      }
    }
  }
});

test('applying a level replaces a custom set with exactly the level codes', () => {
  const next = applyDutyLevel(set('payroll_setup.view', 'payroll_setup.publish'), 'payCalendars', 'see');
  assert.deepEqual(sorted(next), ['payroll_setup.view']);
});

test('applying the level a duty already has changes nothing', () => {
  const start = set('payroll.view', 'payroll.entry', 'payroll.period.create', 'drivers.create', 'employees.view', 'drivers.view');
  assert.deepEqual(sorted(applyDutyLevel(start, 'payroll', 'manage')), sorted(start));
  assert.deepEqual(sorted(applyDutyLevel(start, 'people', 'see')), sorted(start));
});

test('hidden codes with no affected relation are preserved', () => {
  const start = set('company.view', 'dispatch.view', 'dispatch.edit', 'branches.edit', 'payroll.view');
  const next = applyDutyLevel(start, 'payRates', 'manage');
  for (const code of ['company.view', 'dispatch.view', 'dispatch.edit', 'branches.edit', 'payroll.view']) {
    assert.ok(next.has(code), `${code} must be kept`);
  }
});

test('hidden dependents of a kept code are kept when the level moves around it', () => {
  const start = set('employees.view', 'drivers.view', 'drivers.create');
  assert.ok(applyDutyLevel(start, 'people', 'manage').has('drivers.create'));
  assert.ok(applyDutyLevel(start, 'people', 'see').has('drivers.create'));
});

test('switching a duty off drops the hidden children of its removed codes', () => {
  const start = set('drivers.view', 'drivers.create', 'payroll.view', 'payroll.edit', 'payroll.approve');
  const people = applyDutyLevel(start, 'people', 'off');
  assert.ok(!people.has('drivers.create'));
  const payroll = applyDutyLevel(start, 'payroll', 'off');
  assert.ok(!payroll.has('payroll.edit'));
  assert.ok(!payroll.has('payroll.approve'));
});

test('required hidden parents are added', () => {
  const settings = applyDutyLevel(set(), 'companySettings', 'manage');
  assert.deepEqual(sorted(settings), ['settings.manage', 'settings.view']);
  const branches = applyDutyLevel(set(), 'newBranches', 'manage');
  assert.deepEqual(sorted(branches), ['branches.create', 'branches.view']);
});

test('turning company settings off removes the settings code and its hidden parent stays', () => {
  const next = applyDutyLevel(set('settings.manage', 'settings.view'), 'companySettings', 'off');
  assert.ok(!next.has('settings.manage'));
  assert.ok(next.has('settings.view'));
});

test('lowering manage to see removes only the management codes', () => {
  const start = set('users.view', 'users.create', 'users.edit', 'users.deactivate', 'roles.view');
  assert.deepEqual(sorted(applyDutyLevel(start, 'signIn', 'see')), ['roles.view', 'users.view']);
});

test('applying a level matches the code-by-code editor result', () => {
  const state = (codes: Set<string>): PermsState => ({ codes, savedCodes: new Set(), loading: false, error: '', dirty: false });
  const start = set('payroll.view', 'payroll.entry', 'review.decide', 'payroll.finalize', 'payroll.edit');
  const viaEditor = permsReducer(
    state(start),
    { type: 'SET_MANY', codes: ['payroll.view', 'payroll.entry', 'payroll.period.create'], enable: false },
  ).codes;
  assert.deepEqual(sorted(applyDutyLevel(start, 'payroll', 'off')), sorted(viaEditor));
});

test('the input set is never mutated', () => {
  const start = set('payroll.view', 'review.decide', 'payroll.finalize');
  const snapshot = sorted(start);
  const next = applyDutyLevel(start, 'payroll', 'off');
  assert.notEqual(next, start);
  assert.deepEqual(sorted(start), snapshot);
  dutyLevelChangeEffects(start, 'payroll', 'off');
  assert.deepEqual(sorted(start), snapshot);
});

test('applying a level the duty does not have, or an unknown duty, is rejected', () => {
  assert.throws(() => applyDutyLevel(set(), 'review', 'see'), /no level/);
  assert.throws(() => applyDutyLevel(set(), 'nope', 'off'), /Unknown duty/);
});

// ─── Cascade effects ─────────────────────────────────────────────────────────

test('turning payroll work off also turns review and finalize off', () => {
  const start = set('payroll.view', 'payroll.entry', 'payroll.period.create', 'review.decide', 'payroll.finalize');
  const effects = dutyLevelChangeEffects(start, 'payroll', 'off');
  assert.deepEqual(effects, [
    { dutyId: 'review', label: 'Review payrolls', before: 'manage', after: 'off' },
    { dutyId: 'finalize', label: 'Finalize payrolls', before: 'manage', after: 'off' },
  ]);
});

test('lowering payroll work to see keeps review and finalize', () => {
  const start = set('payroll.view', 'payroll.entry', 'payroll.period.create', 'review.decide', 'payroll.finalize');
  assert.deepEqual(dutyLevelChangeEffects(start, 'payroll', 'see'), []);
});

test('allowing review while payroll work is off lifts payroll work to see', () => {
  const effects = dutyLevelChangeEffects(set(), 'review', 'manage');
  assert.deepEqual(effects, [{ dutyId: 'payroll', label: 'Payroll work', before: 'off', after: 'see' }]);
});

test('allowing finalize lifts payroll work to see, and leaves it alone if already higher', () => {
  assert.deepEqual(
    dutyLevelChangeEffects(set(), 'finalize', 'manage').map(e => [e.dutyId, e.after]),
    [['payroll', 'see']],
  );
  const managing = set('payroll.view', 'payroll.entry', 'payroll.period.create');
  assert.deepEqual(dutyLevelChangeEffects(managing, 'finalize', 'manage'), []);
});

test('choosing people or reports management repairs a missing parent inside the duty only', () => {
  // employees.manage without its parent is a custom People duty; Manage fills in the parent, no other duty moves
  const people = applyDutyLevel(set('employees.manage'), 'people', 'manage');
  assert.equal(dutyLevelOf(duty('people'), people), 'manage');
  assert.deepEqual(dutyLevelChangeEffects(set('employees.manage'), 'people', 'manage'), []);
  const reports = applyDutyLevel(set('ledger.audit.view'), 'reports', 'manage');
  assert.equal(dutyLevelOf(duty('reports'), reports), 'manage');
  assert.deepEqual(dutyLevelChangeEffects(set('ledger.audit.view'), 'reports', 'manage'), []);
});

test('turning reports or people off removes only that duty', () => {
  const start = set('reports.view', 'ledger.view', 'ledger.audit.view', 'employees.view', 'drivers.view');
  assert.deepEqual(dutyLevelChangeEffects(start, 'reports', 'off'), []);
  assert.deepEqual(dutyLevelChangeEffects(start, 'people', 'off'), []);
});

test('effects are found even when the changed duty was custom', () => {
  // payroll work is custom here (entry without period.create); Off still drops review.decide with payroll.view
  const start = set('payroll.view', 'payroll.entry', 'review.decide');
  const effects = dutyLevelChangeEffects(start, 'payroll', 'off');
  assert.deepEqual(effects.map(e => [e.dutyId, e.before, e.after]), [['review', 'manage', 'off']]);
});

test('company settings and new branches do not disturb other duties', () => {
  assert.deepEqual(dutyLevelChangeEffects(set(), 'companySettings', 'manage'), []);
  assert.deepEqual(dutyLevelChangeEffects(set(), 'newBranches', 'manage'), []);
});

// ─── Summary ─────────────────────────────────────────────────────────────────

test('summary lists only duties that are not off, in duty order, with their level label', () => {
  const codes = set(
    'roles.view',
    'reports.view', 'ledger.view', 'ledger.audit.view',
    'payroll.view', 'payroll.entry', 'payroll.period.create',
    'review.decide',
    'settings.view',
  );
  assert.deepEqual(summarizeDuties(codes).map(l => [l.dutyId, l.label, l.levelLabel]), [
    ['payroll', 'Payroll work', 'Enter and prepare'],
    ['review', 'Review payrolls', 'Approve or return'],
    ['reports', 'Reports and finalized payrolls', 'See with change history'],
    ['roles', 'Roles', 'See'],
  ]);
});

test('summary marks a partially held duty as custom', () => {
  const lines = summarizeDuties(set('payroll_setup.view', 'payroll_setup.publish'));
  assert.deepEqual(lines, [
    { dutyId: 'payCalendars', label: 'Pay calendars', level: 'custom', levelLabel: 'Custom' },
  ]);
});

test('summary is empty when no duty is held', () => {
  assert.deepEqual(summarizeDuties(set()), []);
  assert.deepEqual(summarizeDuties(set('company.view', 'dispatch.view')), []);
});

test('level labels come from the duty, with a shared label for custom', () => {
  assert.equal(dutyLevelLabel(duty('payroll'), 'off'), 'Off');
  assert.equal(dutyLevelLabel(duty('payroll'), 'see'), 'See');
  assert.equal(dutyLevelLabel(duty('finalize'), 'manage'), 'Finalize');
  assert.equal(dutyLevelLabel(duty('people'), 'custom'), 'Custom');
});
