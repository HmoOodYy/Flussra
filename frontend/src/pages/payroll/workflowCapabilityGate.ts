/**
 * Workflow capability gate — resolves Current Payroll Hub capability objects
 * into UI-facing gate state for the page's own lifecycle action buttons
 * (Submit for Review, Resubmit, Day Grid access).
 *
 * This module is deliberately thin. It does not decide payroll lifecycle
 * rules itself — it only looks up the backend-computed capability for a
 * given period and translates its {allowed, reason_message} shape into
 * {disabled, reasonMessage} for a button. The backend
 * (app.payroll.current_hub) remains the sole authority for *why* an action
 * is or isn't allowed; this module never reconstructs a reason from a
 * reason_code, and never encodes its own lifecycle/status rules.
 */
import type {
  CurrentPayrollHubBranch,
  PeriodStatus,
  PeriodWorkflowCapabilities,
  WorkflowCapability,
} from '../../types/payroll';

/**
 * Look up the backend-computed workflow capabilities for one period.
 *
 * Returns null when the branch isn't present in the Hub response, or when
 * the period isn't part of the Hub's active workflow slot set (Draft/Open/
 * InReview/Returned — e.g. an Approved period has no entry). Callers must
 * not assume this null means "allowed" — see resolveCapabilityGate, which
 * distinguishes an expected absence (status outside the Hub's tracked set)
 * from an unexpected one (status inside it, but the Hub had no opinion).
 */
export function getPeriodWorkflowCapabilities(
  branches: CurrentPayrollHubBranch[],
  branchId: number,
  periodId: number,
): PeriodWorkflowCapabilities | null {
  const branch = branches.find((b) => b.branch_id === branchId);
  if (!branch) return null;
  return branch.capabilities.periods[String(periodId)] ?? null;
}

// Statuses the Current Payroll Hub tracks in its active workflow slot set
// (app.payroll.current_hub's active_periods query: status IN ('Draft',
// 'Open', 'InReview', 'Returned')). A period in one of these statuses is
// always expected to have a capabilities.periods[period_id] entry when the
// Hub response for its branch loaded successfully.
const HUB_ACTIVE_WORKFLOW_STATUSES: ReadonlySet<PeriodStatus> = new Set([
  'Draft',
  'Open',
  'InReview',
  'Returned',
]);

/**
 * True for a period status the Hub is expected to cover in its active
 * workflow capability map (Draft/Open/InReview/Returned). False for any
 * status the Hub deliberately does not track (e.g. Approved, Locked,
 * Cancelled, Archived) — those periods are expected to have no capability
 * entry, and that absence is not a failure.
 */
export function isHubActiveWorkflowStatus(status: PeriodStatus): boolean {
  return HUB_ACTIVE_WORKFLOW_STATUSES.has(status);
}

/** Neutral, backend-agnostic message shown when an active-workflow period's
 * Hub capability is unexpectedly missing (Hub still loading, failed, or the
 * branch/period wasn't present in its response). This is never a stand-in
 * for a backend business reason — when the backend capability is actually
 * present and denies the action, its own reason_message is always used
 * instead of this string. */
export const WORKFLOW_CONTEXT_UNAVAILABLE_MESSAGE =
  'Current payroll workflow context is unavailable. Refresh and try again.';

export interface CapabilityGateState {
  /** True when the action must not be allowed to fire. */
  disabled: boolean;
  /** Explanation to show the user when disabled — either the backend's own
   * reason_message, or WORKFLOW_CONTEXT_UNAVAILABLE_MESSAGE when the Hub had
   * no opinion for a status it was expected to cover. Never derived from a
   * reason_code, and never a fabricated business reason. */
  reasonMessage: string | null;
}

/**
 * Resolve a single backend WorkflowCapability into button gate state.
 *
 * `isActiveWorkflowStatus` (see isHubActiveWorkflowStatus) tells this
 * function whether the period's status is one the Hub is expected to cover:
 *
 * - capability present, allowed=true  -> enabled.
 * - capability present, allowed=false -> disabled; backend's own
 *   reason_message.
 * - capability missing (null) and isActiveWorkflowStatus=true  -> FAIL
 *   CLOSED: disabled, WORKFLOW_CONTEXT_UNAVAILABLE_MESSAGE. The Hub may
 *   still be loading, may have failed, or may unexpectedly be missing this
 *   period — never treat that as "allowed".
 * - capability missing (null) and isActiveWorkflowStatus=false -> expected
 *   absence (e.g. Approved, which the Hub never tracks): not disabled, no
 *   reason — defers entirely to whatever behavior the caller already has
 *   for that status, rather than inventing a new gate for it.
 */
export function resolveCapabilityGate(
  capability: WorkflowCapability | null,
  isActiveWorkflowStatus: boolean,
): CapabilityGateState {
  if (capability == null) {
    if (isActiveWorkflowStatus) {
      return { disabled: true, reasonMessage: WORKFLOW_CONTEXT_UNAVAILABLE_MESSAGE };
    }
    return { disabled: false, reasonMessage: null };
  }
  if (capability.allowed) {
    return { disabled: false, reasonMessage: null };
  }
  return { disabled: true, reasonMessage: capability.reason_message };
}
