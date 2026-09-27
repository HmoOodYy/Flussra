/**
 * Reusable debounced "auto preview" React hook (Phase 6 Unit A, hardened in
 * the Unit A review) — used by both the Publish panel
 * (previewPublicationImpact) and the branch "Change policy" reassignment
 * flow (previewReassignmentImpact).
 *
 * Never decides preview eligibility itself: the caller passes `fetcher` as
 * null whenever its own inputs are incomplete/invalid (e.g. the date is not
 * a valid ISO date yet) — this hook does not inspect or validate inputs, it
 * only debounces, deduplicates, and guards against out-of-order responses.
 *
 * Stale-preview safety: `stored.inputs` is an independent SNAPSHOT of the
 * caller's `inputs` taken at the moment the request was actually issued
 * (after the debounce), never the caller's current, possibly-since-changed
 * inputs. Re-stamping the live inputs onto whatever response happens to be
 * in state would make a caller's `canPublishFromPreview(currentInputs,
 * stored)` check tautological (current vs. itself) — the whole point of
 * that check is comparing CURRENT inputs against the inputs a specific
 * preview response actually corresponds to. Callers must always compare
 * their own live inputs against `stored.inputs`, never assume they match.
 *
 * Staleness contract: the moment `requestKey` (or readiness) changes, the
 * previous stored preview is cleared IMMEDIATELY (during the same render,
 * before any debounce/fetch happens) — the UI must never keep showing a
 * preview that no longer matches the current inputs. Callers render
 * `loading` as "Checking impact…" rather than reusing the old `stored`.
 */
import { useEffect, useRef, useState } from 'react';
import { readApiError, type ApiErrorInfo } from './payrollSetupErrors.ts';

export type AutoPreviewFetcher<TInputs, TResponse> = (inputs: TInputs) => Promise<TResponse>;

export interface StoredAutoPreview<TInputs, TResponse> {
  inputs: TInputs;
  response: TResponse;
}

export interface UseAutoPreviewResult<TInputs, TResponse> {
  stored: StoredAutoPreview<TInputs, TResponse> | null;
  loading: boolean;
  error: ApiErrorInfo | null;
}

const DEFAULT_DEBOUNCE_MS = 400;

/**
 * Builds a stable string key from an ordered tuple of primitive input
 * fields (e.g. `[setupId, draftId, draftScheduleKey, effectiveFromDate,
 * replacesVersionId]` for publish, or `[branchId, destinationSetupId,
 * effectiveFromDate]` for reassignment). Changes if and only if any part
 * changes — pure, no React, directly testable. The caller must derive this
 * from exactly the same fields as the `inputs` object it passes in, so a
 * `requestKey` change and an `inputs` value change always happen together.
 */
export function buildAutoPreviewKey(parts: readonly (string | number | null)[]): string {
  return JSON.stringify(parts);
}

export function useAutoPreview<TInputs, TResponse>(
  fetcher: AutoPreviewFetcher<TInputs, TResponse> | null,
  inputs: TInputs,
  requestKey: string,
  options?: { debounceMs?: number },
): UseAutoPreviewResult<TInputs, TResponse> {
  const debounceMs = options?.debounceMs ?? DEFAULT_DEBOUNCE_MS;
  const enabled = fetcher !== null;
  const [stored, setStored] = useState<StoredAutoPreview<TInputs, TResponse> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiErrorInfo | null>(null);
  const requestIdRef = useRef(0);

  // Compound key: readiness (enabled) is part of what makes a preview
  // current, exactly like the individual inputs are — flipping it must
  // reset the same way. Reset happens during render (not in an effect) so
  // the stale preview never paints even for a single frame.
  const compoundKey = `${enabled}|${requestKey}`;
  const [trackedKey, setTrackedKey] = useState(compoundKey);
  if (trackedKey !== compoundKey) {
    setTrackedKey(compoundKey);
    setStored(null);
    setError(null);
    setLoading(enabled);
  }

  // Same fetcher-identity guard as useBoundaryChoices: callers will often
  // pass a fresh inline closure every render, so the debounce effect below
  // depends only on [requestKey, debounceMs, enabled], reading the latest
  // fetcher (and inputs — see inputsRef below) from refs kept fresh by
  // these no-deps effects.
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  // The inputs snapshot the eventual request will be issued with. Read at
  // fire time (after the debounce), not captured in the effect's closure —
  // this is what makes `stored.inputs` a true "as-of-request-time"
  // snapshot rather than whatever the effect closed over when it was
  // scheduled.
  const inputsRef = useRef(inputs);
  useEffect(() => {
    inputsRef.current = inputs;
  });

  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;
    const requestId = ++requestIdRef.current;

    function apply(update: () => void) {
      if (cancelled || requestId !== requestIdRef.current) return;
      update();
    }

    const timer = setTimeout(() => {
      const activeFetcher = fetcherRef.current;
      if (!activeFetcher) return;
      // Snapshot now, at the moment the request is actually issued.
      const snapshotInputs = inputsRef.current;
      setLoading(true);
      activeFetcher(snapshotInputs)
        .then((response) => {
          apply(() => {
            setStored({ inputs: snapshotInputs, response });
            setError(null);
            setLoading(false);
          });
        })
        .catch((err: unknown) => {
          apply(() => {
            setStored(null);
            setError(readApiError(err, 'Failed to preview impact.'));
            setLoading(false);
          });
        });
    }, debounceMs);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // `fetcher` and `inputs` are intentionally excluded — see the
    // fetcherRef/inputsRef notes above.
  }, [requestKey, debounceMs, enabled]);

  return { stored, loading, error };
}
