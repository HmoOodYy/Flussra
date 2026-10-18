/**
 * Transfer request types — mirrors backend app/transfer/schemas.py
 */

export type TransferStatus =
  | 'PendingSourceApproval'
  | 'PendingTargetApproval'
  | 'Returned'
  | 'Rejected'
  | 'Approved'
  | 'Scheduled'
  | 'Completed'
  | 'Cancelled';

export interface DriverTransferRequest {
  transfer_request_id: number;
  company_id: number;
  /** The permanent Driver. */
  driver_id: number;
  driver_number: string | null;
  /** The home-Branch tenure the Driver is leaving / joining. */
  source_driver_assignment_id: number;
  destination_driver_assignment_id: number | null;
  source_branch_id: number;
  target_branch_id: number;
  requested_by_user_id: number;
  initiated_by: 'Driver' | 'SourceBranch';
  /**
   * Effective status reported by the server. A Scheduled request reads `Completed`
   * once its EffectiveAt has passed; the client never derives this.
   */
  status: TransferStatus;
  /** Status as stored (a Scheduled request stays `Scheduled` in storage). */
  stored_status: TransferStatus;
  effective_at: string | null;     // ISO instant; null until an immediate request completes
  reason: string | null;
  notes: string | null;
  source_approved_by_user_id: number | null;
  source_approved_at_utc: string | null;
  target_decided_by_user_id: number | null;
  target_decided_at_utc: string | null;
  target_decision_notes: string | null;
  scheduled_at_utc: string | null;
  scheduled_by_user_id: number | null;
  completed_at_utc: string | null;
  completed_by_user_id: number | null;
  cancelled_at_utc: string | null;
  cancelled_by_user_id: number | null;
  cancel_reason: string | null;
  created_at_utc: string;
  updated_at_utc: string | null;
  // Denormalized display fields
  driver_name: string | null;
  source_branch_name: string | null;
  target_branch_name: string | null;
  requested_by_name: string | null;
}

export interface TransferListResponse {
  items: DriverTransferRequest[];
  total: number;
}

export interface DriverTransferCreate {
  driver_id: number;
  target_branch_id: number;
  effective_at?: string;           // Explicit scheduled ISO instant; omitted means immediate on completion
  initiated_by: 'Driver' | 'SourceBranch';
  reason?: string;
  notes?: string;
}

export interface SourceApprovalRequest {
  notes?: string;
}

export interface TargetDecisionRequest {
  decision: 'Approved' | 'Rejected' | 'Returned';
  decision_notes?: string;
}

export interface CancelRequest {
  cancel_reason?: string;
}

/** Move a request's EffectiveAt to another future instant (aware ISO instant). */
export interface RescheduleRequest {
  effective_at: string;
}

// ── Display helpers ──────────────────────────────────────────────────────────

export const TRANSFER_TERMINAL: ReadonlySet<TransferStatus> = new Set([
  'Completed', 'Cancelled', 'Rejected',
]);

export const TRANSFER_ACTIVE: ReadonlySet<TransferStatus> = new Set([
  'PendingSourceApproval', 'PendingTargetApproval', 'Returned', 'Approved', 'Scheduled',
]);

export function statusLabel(s: TransferStatus): string {
  switch (s) {
    case 'PendingSourceApproval': return 'Awaiting Source Approval';
    case 'PendingTargetApproval': return 'Awaiting Target Decision';
    case 'Returned':              return 'Returned to Source';
    case 'Approved':              return 'Approved — Ready to Complete';
    case 'Scheduled':             return 'Scheduled';
    case 'Rejected':              return 'Rejected';
    case 'Completed':             return 'Completed';
    case 'Cancelled':             return 'Cancelled';
  }
}

export type StatusBadgeKind = 'pending' | 'returned' | 'approved' | 'scheduled' | 'done' | 'rejected' | 'cancelled';

export function statusKind(s: TransferStatus): StatusBadgeKind {
  switch (s) {
    case 'PendingSourceApproval':
    case 'PendingTargetApproval': return 'pending';
    case 'Returned':              return 'returned';
    case 'Approved':              return 'approved';
    case 'Scheduled':             return 'scheduled';
    case 'Completed':             return 'done';
    case 'Rejected':              return 'rejected';
    case 'Cancelled':             return 'cancelled';
  }
}
