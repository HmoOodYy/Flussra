/**
 * Transfer request types — mirrors backend app/transfer/schemas.py
 */

export type TransferStatus =
  | 'PendingSourceApproval'
  | 'PendingTargetApproval'
  | 'Returned'
  | 'Rejected'
  | 'Approved'
  | 'Completed'
  | 'Cancelled';

export interface DriverTransferRequest {
  transfer_request_id: number;
  company_id: number;
  driver_id: number;
  source_branch_id: number;
  target_branch_id: number;
  requested_by_user_id: number;
  initiated_by: 'Driver' | 'SourceBranch';
  status: TransferStatus;
  effective_date: string;          // ISO date "YYYY-MM-DD"
  reason: string | null;
  notes: string | null;
  source_approved_by_user_id: number | null;
  source_approved_at_utc: string | null;
  target_decided_by_user_id: number | null;
  target_decided_at_utc: string | null;
  target_decision_notes: string | null;
  new_driver_id: number | null;
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
  effective_date: string;          // ISO date "YYYY-MM-DD"
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

// ── Display helpers ──────────────────────────────────────────────────────────

export const TRANSFER_TERMINAL: ReadonlySet<TransferStatus> = new Set([
  'Completed', 'Cancelled', 'Rejected',
]);

export const TRANSFER_ACTIVE: ReadonlySet<TransferStatus> = new Set([
  'PendingSourceApproval', 'PendingTargetApproval', 'Returned', 'Approved',
]);

export function statusLabel(s: TransferStatus): string {
  switch (s) {
    case 'PendingSourceApproval': return 'Awaiting Source Approval';
    case 'PendingTargetApproval': return 'Awaiting Target Decision';
    case 'Returned':              return 'Returned to Source';
    case 'Approved':              return 'Approved — Ready to Complete';
    case 'Rejected':              return 'Rejected';
    case 'Completed':             return 'Completed';
    case 'Cancelled':             return 'Cancelled';
  }
}

export type StatusBadgeKind = 'pending' | 'returned' | 'approved' | 'done' | 'rejected' | 'cancelled';

export function statusKind(s: TransferStatus): StatusBadgeKind {
  switch (s) {
    case 'PendingSourceApproval':
    case 'PendingTargetApproval': return 'pending';
    case 'Returned':              return 'returned';
    case 'Approved':              return 'approved';
    case 'Completed':             return 'done';
    case 'Rejected':              return 'rejected';
    case 'Cancelled':             return 'cancelled';
  }
}
