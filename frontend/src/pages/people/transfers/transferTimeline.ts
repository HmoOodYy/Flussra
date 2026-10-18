import type { DriverTransferRequest } from '../../../types/transfer';
import { fmtDateTime } from './transferRequestsView.ts';

interface TimelineStep {
  label: string;
  sublabel: string | null;
  state: 'done' | 'active' | 'pending' | 'skipped';
}

export function buildTimeline(req: DriverTransferRequest): TimelineStep[] {
  const s = req.status;

  const created: TimelineStep = {
    label: 'Request Created',
    sublabel: `${fmtDateTime(req.created_at_utc)}${req.requested_by_name ? ` by ${req.requested_by_name}` : ''}`,
    state: 'done',
  };

  const sourceApproval: TimelineStep = (() => {
    if (req.initiated_by === 'SourceBranch') {
      // Source approval is implicit — always done
      return {
        label: 'Source Branch Approved',
        sublabel: req.source_approved_at_utc
          ? `${fmtDateTime(req.source_approved_at_utc)} (branch-initiated)`
          : 'Branch-initiated (implicit approval)',
        state: 'done' as const,
      };
    }
    // Driver-initiated
    if (s === 'PendingSourceApproval') {
      return { label: 'Awaiting Source Approval', sublabel: 'Source branch must approve', state: 'active' as const };
    }
    if (s === 'Returned') {
      return {
        label: 'Source Approval — Returned',
        sublabel: 'Target returned for changes — re-approval needed',
        state: 'active' as const,
      };
    }
    return {
      label: 'Source Branch Approved',
      sublabel: req.source_approved_at_utc ? fmtDateTime(req.source_approved_at_utc) : null,
      state: 'done' as const,
    };
  })();

  const targetDecision: TimelineStep = (() => {
    if (s === 'PendingSourceApproval') {
      return { label: 'Target Branch Decision', sublabel: 'Waiting for source approval first', state: 'pending' as const };
    }
    if (s === 'Returned') {
      return {
        label: 'Target Returned Request',
        sublabel: req.target_decided_at_utc ? fmtDateTime(req.target_decided_at_utc) : null,
        state: 'done' as const,
      };
    }
    if (s === 'PendingTargetApproval') {
      return { label: 'Awaiting Target Decision', sublabel: 'Target branch must approve, reject, or return', state: 'active' as const };
    }
    if (s === 'Rejected') {
      return {
        label: 'Rejected by Target',
        sublabel: req.target_decided_at_utc ? fmtDateTime(req.target_decided_at_utc) : null,
        state: 'done' as const,
      };
    }
    return {
      label: 'Target Approved',
      sublabel: req.target_decided_at_utc ? fmtDateTime(req.target_decided_at_utc) : null,
      state: 'done' as const,
    };
  })();

  const completion: TimelineStep = (() => {
    if (s === 'Completed') {
      return {
        label: 'Transfer Completed',
        sublabel: req.completed_at_utc ? fmtDateTime(req.completed_at_utc) : null,
        state: 'done' as const,
      };
    }
    if (s === 'Cancelled') {
      return {
        label: 'Request Cancelled',
        sublabel: req.cancelled_at_utc ? fmtDateTime(req.cancelled_at_utc) : null,
        state: 'done' as const,
      };
    }
    if (s === 'Rejected') {
      return { label: 'Not Completed', sublabel: 'Request was rejected', state: 'skipped' as const };
    }
    if (s === 'Approved') {
      return { label: 'Ready to Complete', sublabel: 'Action required to finalize', state: 'active' as const };
    }
    return { label: 'Complete Transfer', sublabel: 'Pending prior steps', state: 'pending' as const };
  })();

  return [created, sourceApproval, targetDecision, completion];
}
