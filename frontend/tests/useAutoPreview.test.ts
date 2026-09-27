import assert from 'node:assert/strict';
import { test } from 'node:test';
import { buildAutoPreviewKey } from '../src/lib/useAutoPreview.ts';

// Only buildAutoPreviewKey is pure/testable in node:test — the hook itself
// needs a React runtime and is exercised by PublishPanel/ManageBranchDrawer.

test('buildAutoPreviewKey: same tuple -> same key', () => {
  const a = buildAutoPreviewKey([1, 50, 'sched', '2026-09-23', null]);
  const b = buildAutoPreviewKey([1, 50, 'sched', '2026-09-23', null]);
  assert.equal(a, b);
});

test('buildAutoPreviewKey: any single part changing -> a different key', () => {
  const base = buildAutoPreviewKey([1, 50, 'sched', '2026-09-23', null]);
  assert.notEqual(buildAutoPreviewKey([2, 50, 'sched', '2026-09-23', null]), base);
  assert.notEqual(buildAutoPreviewKey([1, 51, 'sched', '2026-09-23', null]), base);
  assert.notEqual(buildAutoPreviewKey([1, 50, 'other', '2026-09-23', null]), base);
  assert.notEqual(buildAutoPreviewKey([1, 50, 'sched', '2026-09-24', null]), base);
  assert.notEqual(buildAutoPreviewKey([1, 50, 'sched', '2026-09-23', 7]), base);
});

test('buildAutoPreviewKey: null vs the number 0 vs the string "0" all differ', () => {
  const withNull = buildAutoPreviewKey([1, null]);
  const withZero = buildAutoPreviewKey([1, 0]);
  const withStringZero = buildAutoPreviewKey([1, '0']);
  assert.notEqual(withNull, withZero);
  assert.notEqual(withZero, withStringZero);
  assert.notEqual(withNull, withStringZero);
});

test('buildAutoPreviewKey: works for the reassignment tuple shape (3 parts) as well as publish (5 parts)', () => {
  const reassign = buildAutoPreviewKey([10, 2, '2026-02-01']);
  const reassign2 = buildAutoPreviewKey([10, 2, '2026-02-01']);
  assert.equal(reassign, reassign2);
  assert.notEqual(reassign, buildAutoPreviewKey([10, 3, '2026-02-01']));
});
