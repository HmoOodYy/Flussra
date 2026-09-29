/**
 * Pure row-presentation helpers for the Branch Assignments tab (Phase 6
 * Unit A), driven entirely by BranchPolicySummaryResponse (one GET
 * /payroll-setup/branch-summaries call — no per-branch history fetch).
 *
 * PURE MODULE: no React, no apiClient, `import type` only for API types.
 * Never constructs a JS Date object or reads the wall clock, and never
 * computes payroll chronology — `current`/`scheduled_change` and
 * `readiness_reason` are the backend's own judgment, copied/formatted here
 * only using isoDate's pure calendar arithmetic.
 */
import { formatIsoLong, formatIsoShort } from '../../../lib/isoDate.ts';
import { frequencyNoun } from '../../../lib/payrollBoundaryView.ts';
import type { BranchPolicySummaryResponse, PolicyAssignmentSummaryResponse } from '../../../types/payrollSetup';

function assignmentFrequencyNoun(a: PolicyAssignmentSummaryResponse): string {
  return a.payroll_frequency != null ? frequencyNoun(a.payroll_frequency, a.custom_interval_days) : 'Unscheduled';
}

/**
 * "<name> — <Weekly/Biweekly/Monthly/N-day>", "Payroll not set up" when the
 * branch has no policy at all, or "Starts <date>" for a future-only
 * onboarding (payroll_set_up but no `current` yet — see scheduled_change).
 */
export function currentPolicyLabel(summary: BranchPolicySummaryResponse): string {
  if (!summary.payroll_set_up) return 'Payroll not set up';
  if (summary.current != null) {
    return `${summary.current.setup_name} — ${assignmentFrequencyNoun(summary.current)}`;
  }
  if (summary.scheduled_change != null) {
    return `Starts ${formatIsoLong(summary.scheduled_change.effective_from_date)}`;
  }
  return 'Payroll not set up';
}

/** "Changes <Mon D> → <name> — <frequency>", or "None" when there is no scheduled change. */
export function scheduledChangeLabel(summary: BranchPolicySummaryResponse): string {
  const change = summary.scheduled_change;
  if (change == null) return 'None';
  return `Changes ${formatIsoShort(change.effective_from_date)} → ${change.setup_name} — ${assignmentFrequencyNoun(change)}`;
}

export type BranchPolicyActionKind = 'assign' | 'manage';

/**
 * 'assign' for a branch with no policy at all (never 'manage'/"Reassign…" —
 * there is nothing to change yet); 'manage' otherwise.
 */
export function branchPolicyActionKind(summary: BranchPolicySummaryResponse): BranchPolicyActionKind {
  return summary.payroll_set_up ? 'manage' : 'assign';
}

export const BRANCH_POLICY_ACTION_LABEL: Record<BranchPolicyActionKind, string> = {
  assign: 'Assign policy',
  manage: 'Manage',
};
