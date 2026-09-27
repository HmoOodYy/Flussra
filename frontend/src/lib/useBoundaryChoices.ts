/**
 * React hook wrapping a BoundaryChoicesResponse fetcher (publication,
 * assignment, reassignment, or branch-onboarding choices) with debouncing,
 * request-key invalidation, and out-of-order-response protection.
 *
 * Never synthesizes or modifies the backend's response — `choices` is
 * exposed exactly as the fetcher returned it. The frontend does not decide
 * which dates are valid; it only asks the backend and displays the answer.
 *
 * `key` is the caller's declaration of which context `fetcher` targets (e.g.
 * `${setupId}|${draftId}` for a publication fetcher). Callers MUST change
 * `key` whenever the thing `fetcher` would fetch for changes — the request
 * effect below intentionally does not depend on `fetcher`'s identity (see
 * the "fetcher identity" note above the effect), only on `key`, so passing a
 * fresh inline closure every render is safe, but pointing that closure at a
 * different setup/branch/draft without also changing `key` will NOT trigger
 * a re-fetch.
 */
import { useEffect, useRef, useState } from 'react';
import { isValidIsoDate } from './isoDate.ts';
import { readApiError, type ApiErrorInfo } from './payrollSetupErrors.ts';
import type { BoundaryChoicesResponse } from '../types/payrollSetup.ts';

export type BoundaryFetcher = (around?: string) => Promise<BoundaryChoicesResponse>;

export type BoundaryRequestPlan =
  | { kind: 'suggest' }
  | { kind: 'around'; around: string }
  | { kind: 'none' };

/**
 * Classifies what request (if any) a given `value` warrants:
 *   - '' and no suggestion requested yet for this key -> ask the backend for
 *     its own suggestion (no `around`), exactly once per key.
 *   - '' after that first suggestion request -> no request (a suggestion was
 *     already asked for; repeatedly re-requesting it on every keystroke that
 *     leaves the date blank/incomplete would also risk a caller overwriting
 *     what the user is currently typing with a stale "suggested" value).
 *   - a valid ISO date -> ask around that date.
 *   - anything else (incomplete/invalid typing) -> no request.
 */
export function boundaryRequestFor(
  value: string,
  suggestionAlreadyRequested: boolean,
): BoundaryRequestPlan {
  if (value === '') return suggestionAlreadyRequested ? { kind: 'none' } : { kind: 'suggest' };
  if (isValidIsoDate(value)) return { kind: 'around', around: value };
  return { kind: 'none' };
}

export interface UseBoundaryChoicesResult {
  choices: BoundaryChoicesResponse | null;
  loading: boolean;
  error: ApiErrorInfo | null;
}

const DEFAULT_DEBOUNCE_MS = 300;

export function useBoundaryChoices(
  fetcher: BoundaryFetcher | null,
  key: string,
  value: string,
  options?: { debounceMs?: number },
): UseBoundaryChoicesResult {
  const debounceMs = options?.debounceMs ?? DEFAULT_DEBOUNCE_MS;
  const [choices, setChoices] = useState<BoundaryChoicesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiErrorInfo | null>(null);
  const requestIdRef = useRef(0);
  const suggestionRequestedRef = useRef(false);

  // `key` identifies the context (e.g. `${setupId}|${draftId}`). Reset
  // immediately when it changes, following React's documented pattern for
  // "adjusting state when a prop changes": compare against a tracked-in-
  // state previous value during render (not in an effect), so the reset is
  // visible in the very same render rather than one tick later.
  // (Any fetch still in flight for the old key is separately ignored by the
  // fetch effect's own cleanup below, which runs before the new effect when
  // `key` changes.)
  const [trackedKey, setTrackedKey] = useState(key);
  if (trackedKey !== key) {
    setTrackedKey(key);
    setChoices(null);
    setError(null);
    setLoading(false);
  }

  // The "suggestion already requested" flag is per-key bookkeeping, not
  // renderable state, so it lives in a ref — reset inside an effect (ref
  // writes during render are disallowed; ref writes/reads inside effects are
  // fine) rather than in the render-time block above.
  useEffect(() => {
    suggestionRequestedRef.current = false;
  }, [key]);

  // Fetcher identity: callers will often pass a fresh inline closure every
  // render (e.g. `(around) => getPublicationChoices(setupId, draftId,
  // around)`). If the request effect depended on `fetcher` directly, that
  // new identity every render would re-run the effect -> fetch -> setState
  // -> re-render -> new closure -> refetch, forever. Instead we keep the
  // latest fetcher in a ref (always fresh by the time an effect reads it,
  // since this sync effect has no deps and so runs after every render,
  // before any effect declared below it in the same commit) and the request
  // effect below depends only on whether a fetcher is present at all.
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  const enabled = fetcher !== null;

  useEffect(() => {
    if (!enabled) return;
    if (!fetcherRef.current) return;
    // Bind to a const so the narrowed (non-null) type survives inside the
    // nested `run` function below.
    const activeFetcher = fetcherRef.current;

    const plan = boundaryRequestFor(value, suggestionRequestedRef.current);
    if (plan.kind === 'none') {
      // Incomplete/invalid typing, or a suggestion already asked for this
      // key: no request. Existing choices are left in place — the caller
      // treats them as stale via their requested_date.
      return;
    }

    let cancelled = false;
    const requestId = ++requestIdRef.current;

    function apply(update: () => void) {
      if (cancelled || requestId !== requestIdRef.current) return;
      update();
    }

    function run() {
      if (plan.kind === 'suggest') {
        suggestionRequestedRef.current = true;
      }
      setLoading(true);
      const around = plan.kind === 'around' ? plan.around : undefined;
      activeFetcher(around)
        .then((response) => {
          apply(() => {
            setChoices(response);
            setError(null);
            setLoading(false);
          });
        })
        .catch((err: unknown) => {
          apply(() => {
            setError(readApiError(err, 'Could not check payroll dates.'));
            setLoading(false);
          });
        });
    }

    if (plan.kind === 'suggest') {
      run();
      return () => {
        cancelled = true;
      };
    }

    const timer = setTimeout(run, debounceMs);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // `fetcher` is intentionally excluded — see the fetcherRef note above.
    // (Not flagged by exhaustive-deps: the effect only ever reads
    // fetcherRef.current, never `fetcher` itself.)
  }, [key, value, debounceMs, enabled]);

  // No fetcher means "idle, no request" regardless of any loading flag left
  // over from a previous fetcher — never reported as loading.
  return { choices, loading: enabled ? loading : false, error };
}
