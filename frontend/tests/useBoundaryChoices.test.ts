import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { boundaryRequestFor } from '../src/lib/useBoundaryChoices.ts';

// Only boundaryRequestFor is pure/testable in node:test — the hook itself
// needs a React runtime (DOM/act) and is exercised by the pages that adopt
// it in later units.

test('boundaryRequestFor: empty string, no suggestion requested yet -> suggest', () => {
  assert.deepEqual(boundaryRequestFor('', false), { kind: 'suggest' });
});

test('boundaryRequestFor: empty string, suggestion already requested -> none', () => {
  assert.deepEqual(boundaryRequestFor('', true), { kind: 'none' });
});

test('boundaryRequestFor: a valid ISO date -> around that date, regardless of the suggestion flag', () => {
  assert.deepEqual(boundaryRequestFor('2026-09-23', false), { kind: 'around', around: '2026-09-23' });
  assert.deepEqual(boundaryRequestFor('2026-09-23', true), { kind: 'around', around: '2026-09-23' });
});

test('boundaryRequestFor: an incomplete or invalid ISO string -> none (no request), regardless of the suggestion flag', () => {
  assert.deepEqual(boundaryRequestFor('2026-09', false), { kind: 'none' });
  assert.deepEqual(boundaryRequestFor('2026-13-01', false), { kind: 'none' });
  assert.deepEqual(boundaryRequestFor('not-a-date', true), { kind: 'none' });
  assert.deepEqual(boundaryRequestFor('2026-02-30', true), { kind: 'none' });
});

// ── Static check: the request effect must not depend on `fetcher` ─────────
//
// Callers will often pass a fresh inline closure every render; depending on
// `fetcher`'s identity would re-run the effect -> fetch -> setState ->
// re-render -> new closure -> refetch, forever. The effect instead reads the
// latest fetcher from a ref kept in sync by a separate, no-deps effect.

test('static: the request effect dependency array does not include `fetcher`', () => {
  const source = readFileSync(new URL('../src/lib/useBoundaryChoices.ts', import.meta.url), 'utf8');

  // Find the request effect: the one whose dependency array includes `key`
  // and `value` (as opposed to the key-reset effect, which depends on
  // `[key]` alone, or the fetcher-ref-sync effect, which has no deps array).
  const depArrayPattern = /\}, \[([^\]]*)\]\);/g;
  const depArrays = [...source.matchAll(depArrayPattern)].map((m) => m[1]);
  const requestEffectDeps = depArrays.find((deps) => deps.includes('key') && deps.includes('value'));

  assert.ok(requestEffectDeps, 'expected to find the request effect dependency array');
  const depsList = requestEffectDeps!.split(',').map((d) => d.trim());
  assert.ok(!depsList.includes('fetcher'), `request effect deps must not include 'fetcher': [${requestEffectDeps}]`);
  assert.deepEqual(depsList.sort(), ['debounceMs', 'enabled', 'key', 'value'].sort());
});
