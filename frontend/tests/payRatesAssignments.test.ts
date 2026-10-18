import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { DriverAssignmentInfo } from '../src/types/core.ts';
import {
  assignmentLabel, defaultAssignmentId, isReadOnlyAssignment, viewedAssignmentId,
} from '../src/pages/people/pay-rates/payRatesAssignments.ts';

function assignment(id: number, state: DriverAssignmentInfo['state'], overrides: Partial<DriverAssignmentInfo> = {}): DriverAssignmentInfo {
  return {
    driver_assignment_id: id,
    driver_id: 9,
    branch_id: 3,
    branch_name: 'North',
    effective_from_at: '2026-01-02T00:00:00Z',
    effective_to_at: null,
    end_reason: null,
    state,
    ...overrides,
  };
}

const ended = assignment(10, 'ended', {
  effective_to_at: '2026-03-03T00:00:00Z', end_reason: 'Transferred',
});
const current = assignment(11, 'current', { branch_name: 'South', effective_from_at: '2026-03-03T00:00:00Z' });
const pending = assignment(12, 'pending', { branch_name: 'East', effective_from_at: '2027-01-01T00:00:00Z' });

test('assignment labels carry Branch, window and end reason; open windows read "present"', () => {
  assert.equal(assignmentLabel(ended), 'North · Jan 2, 2026 – Mar 3, 2026 (Transferred)');
  assert.equal(assignmentLabel(current), 'South · Mar 3, 2026 – present');
  assert.equal(assignmentLabel(pending), 'Upcoming · East · Jan 1, 2027 – present');
});

test('only ended assignments are read-only; current and pending stay editable', () => {
  assert.equal(isReadOnlyAssignment(ended), true);
  assert.equal(isReadOnlyAssignment(current), false);
  assert.equal(isReadOnlyAssignment(pending), false);
  assert.equal(isReadOnlyAssignment(null), false);
});

test('the Driver\'s visible assignment is opened first, else current, else pending, else the newest listed', () => {
  assert.equal(defaultAssignmentId([current, ended], 11), 11);
  // The visible assignment is not readable for this user: fall back to what is listed.
  assert.equal(defaultAssignmentId([ended, current], 99), 11);
  assert.equal(defaultAssignmentId([ended, pending], null), 12);
  assert.equal(defaultAssignmentId([ended], null), 10);
  assert.equal(defaultAssignmentId([], 7), 7);
});

test('an explicit pick wins only while it is still one of the listed assignments', () => {
  assert.equal(viewedAssignmentId([ended, current], 10, 11), 10);
  assert.equal(viewedAssignmentId([ended, current], 55, 11), 11);
  assert.equal(viewedAssignmentId([ended, current], null, 11), 11);
  assert.equal(viewedAssignmentId([], 10, 11), 11);
});

test('Pay Rates wires the assignment picker, the read-only gate and the scoped list endpoint', async () => {
  const { readFileSync } = await import('node:fs');
  const read = (name: string) => readFileSync(
    new URL(`../src/pages/people/pay-rates/${name}`, import.meta.url), 'utf8');
  assert.match(read('driverAssignmentsApi.ts'), /\/core\/drivers\/\$\{driverId\}\/assignments/);
  const context = read('usePayRateDriverContext.ts');
  assert.match(context, /getDriverAssignments\(selectedDriverId\)/);
  assert.match(context, /viewedAssignmentId\(assignments, pickedAssignmentId/);
  const page = read('PayRatesPage.tsx');
  assert.match(page, /!readOnlyAssignment/);
  assert.match(page, /driver\.chooseAssignment\(Number\(e\.target\.value\)\)/);
  // Ownership and authorization are unchanged: every read still goes through the
  // assignment-addressed payroll/compensation routes.
  assert.ok(!/driver_id/.test(read('usePayRateReads.ts')));
});
