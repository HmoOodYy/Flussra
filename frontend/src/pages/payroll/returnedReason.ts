/**
 * Returned-correction reason — resolves the backend-authoritative review
 * item (app.review.service, PeriodApproval Rejected/EditRequested decision)
 * linked by PeriodSummary.current_return_review_item_id into display text
 * for the Current Payroll card.
 *
 * This module never reconstructs or infers a return reason itself. It only
 * decides what to show given the fetch outcome the caller already has —
 * the review domain (GET /review/items/{id}) remains the sole authority for
 * *why* a period was returned. When that authority can't be reached (no
 * linked review item, or the fetch failed), the operator still sees an
 * explicit neutral message rather than the section silently disappearing —
 * the whole point of this feature is that the operator should never be left
 * wondering whether "nothing shown" means "no reason" or "couldn't load".
 */
import type { ReviewItemDetail } from '../../types/review';

/** True only when there is a review item to fetch for a Returned period. */
export function shouldFetchReturnReason(
  isReturned: boolean,
  reviewItemId: number | null,
): boolean {
  return isReturned && reviewItemId != null;
}

export type ReturnReasonDisplay =
  | { kind: 'none' }
  | { kind: 'loading' }
  | { kind: 'unavailable' }
  | { kind: 'ready'; message: string };

export const RETURN_REASON_NOT_RECORDED_MESSAGE = 'No reason was recorded for this return.';
export const RETURN_REASON_UNAVAILABLE_MESSAGE = 'Return reason unavailable.';

/**
 * Resolve what to show for a Returned period's correction reason.
 *
 * - Not Returned -> 'none' (nothing to show; this is the only case that
 *   renders nothing).
 * - Returned, but no linked review item id (malformed/unavailable review
 *   context) -> 'unavailable'. Still visible — never silently disappears.
 * - Returned with an id, fetch in flight -> 'loading'.
 * - Returned with an id, fetch failed -> 'unavailable' (non-crashing,
 *   visible fallback; never fabricates a reason).
 * - Returned with an id, fetch succeeded -> 'ready' with the backend's own
 *   final_decision_reason, or a neutral fallback string if the backend
 *   recorded no reason text (never fabricated from a status/decision code).
 */
export function resolveReturnReasonDisplay(
  isReturned: boolean,
  reviewItemId: number | null,
  loading: boolean,
  errored: boolean,
  detail: ReviewItemDetail | null,
): ReturnReasonDisplay {
  if (!isReturned) {
    return { kind: 'none' };
  }
  if (reviewItemId == null) {
    return { kind: 'unavailable' };
  }
  if (loading) {
    return { kind: 'loading' };
  }
  if (errored || detail == null) {
    return { kind: 'unavailable' };
  }
  const reason = detail.final_decision_reason?.trim();
  return { kind: 'ready', message: reason ? reason : RETURN_REASON_NOT_RECORDED_MESSAGE };
}
