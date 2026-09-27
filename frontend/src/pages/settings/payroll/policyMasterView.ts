/**
 * Pure derivation helpers for the Payroll Policies master pane (master-detail
 * redesign Step 2): the "usage state" a policy shows in the list, the filter
 * segmented control, and the search box.
 *
 * PURE MODULE: no React, no apiClient, `import type` only for API types.
 * Never constructs a JS Date object or reads the wall clock — ISO date
 * strings from the backend are compared/rendered as strings only.
 */
import { formatIsoLong } from '../../../lib/isoDate.ts';
import type { BranchPolicySummaryResponse, SetupResponse } from '../../../types/payrollSetup';

export type PolicyUsageState = 'Active' | 'Inactive' | 'Archived';
export type PolicyFilter = 'All' | 'Active' | 'Inactive' | 'Archived';

export interface PolicyUsage {
  state: PolicyUsageState;
  /** Earliest upcoming effective_from_date across every Branch — Inactive only, else null. */
  futureStart: string | null;
}

/**
 * One usage entry per Setup, keyed by setup_id. Never derives 'Active' from
 * the raw `setup.status` — a policy is only "Active" here when some Branch's
 * `current` assignment actually points at it right now.
 *
 *   - Archived: setup.status === 'Archived'.
 *   - Active: not Archived, and ANY summary's `current.setup_id` matches
 *     (regardless of that Branch's own branch_status).
 *   - Inactive: everything else — including a raw status of 'Active' with no
 *     current usage, and a policy that only appears in future assignments.
 */
export function policyUsage(
  setups: readonly SetupResponse[],
  summaries: readonly BranchPolicySummaryResponse[],
): Map<number, PolicyUsage> {
  const usage = new Map<number, PolicyUsage>();
  for (const setup of setups) {
    if (setup.status === 'Archived') {
      usage.set(setup.setup_id, { state: 'Archived', futureStart: null });
      continue;
    }
    const isCurrentlyUsed = summaries.some((s) => s.current?.setup_id === setup.setup_id);
    if (isCurrentlyUsed) {
      usage.set(setup.setup_id, { state: 'Active', futureStart: null });
      continue;
    }
    // Inactive — the minimum effective_from_date over every upcoming
    // assignment (any Branch, any position) that names this Setup. ISO date
    // strings compare lexicographically; this is a string minimum, not a
    // chronology computation.
    let futureStart: string | null = null;
    for (const summary of summaries) {
      for (const upcoming of summary.upcoming_assignments) {
        if (upcoming.setup_id !== setup.setup_id) continue;
        if (futureStart === null || upcoming.effective_from_date < futureStart) {
          futureStart = upcoming.effective_from_date;
        }
      }
    }
    usage.set(setup.setup_id, { state: 'Inactive', futureStart });
  }
  return usage;
}

const FILTER_RANK: Record<PolicyUsageState, number> = { Active: 0, Inactive: 1, Archived: 2 };

/**
 * Filter first (All shows everything, otherwise only matching usage states),
 * then search (case-insensitive substring of setup_name after trim(); an
 * empty search matches all — search never bypasses the filter).
 *
 * For 'All', stable-sorts by rank Active -> Inactive -> Archived, preserving
 * the incoming API order within each rank. Single filters keep the API order
 * as-is (every visible row already shares one rank).
 */
export function visiblePolicies(
  setups: readonly SetupResponse[],
  usage: ReadonlyMap<number, PolicyUsage>,
  filter: PolicyFilter,
  search: string,
): SetupResponse[] {
  const byFilter = filter === 'All'
    ? setups
    : setups.filter((s) => usage.get(s.setup_id)?.state === filter);

  const term = search.trim().toLowerCase();
  const bySearch = term === ''
    ? byFilter
    : byFilter.filter((s) => s.setup_name.toLowerCase().includes(term));

  if (filter !== 'All') return bySearch.slice();
  return bySearch
    .slice()
    .sort((a, b) => FILTER_RANK[usage.get(a.setup_id)?.state ?? 'Inactive']
      - FILTER_RANK[usage.get(b.setup_id)?.state ?? 'Inactive']);
}

/** "Starts <formatted date>" for the Inactive-row secondary line. */
export function futureStartLabel(iso: string): string {
  return `Starts ${formatIsoLong(iso)}`;
}
