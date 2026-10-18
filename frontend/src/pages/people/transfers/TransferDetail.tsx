import type { DriverTransferRequest } from '../../../types/transfer';
import { statusKind, statusLabel, TRANSFER_TERMINAL } from '../../../types/transfer';
import { fmtDate, fmtDateTime, fmtEffectiveAt } from './transferRequestsView';
import { buildTimeline } from './transferTimeline';
import styles from '../PeoplePage.module.css';
import trStyles from '../TransferRequestsTab.module.css';

interface DetailProps {
  req: DriverTransferRequest;
  canApproveSource: boolean;
  canDecideTarget: boolean;
  canComplete: boolean;
  canCancel: boolean;
  actionLoading: boolean;
  actionError: string | null;
  onApproveSource(): void;
  onDecide(d: 'Approved' | 'Rejected' | 'Returned'): void;
  onComplete(): void;
  onCancel(): void;
  onClearError(): void;
}

export function TransferDetail({
  req, canApproveSource, canDecideTarget, canComplete, canCancel, actionLoading, actionError,
  onApproveSource, onDecide, onComplete, onCancel, onClearError,
}: DetailProps) {
  const isTerminal = TRANSFER_TERMINAL.has(req.status);
  const kind = statusKind(req.status);

  return (
    <div className={styles.detail}>

      {/* Header card */}
      <div className={styles.dHeaderCard}>
        <div className={trStyles.detailIcon}>
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M5 12h14M12 5l7 7-7 7"/>
          </svg>
        </div>
        <div className={styles.dHeaderInfo}>
          <div className={styles.dName}>
            {req.driver_name ?? `Driver #${req.driver_id}`}
            <span className={`${trStyles.badge} ${trStyles[`badge_${kind}`]}`}>
              {statusLabel(req.status)}
            </span>
          </div>
          <div className={styles.dSub}>
            {req.source_branch_name} → {req.target_branch_name}
          </div>
          <div className={styles.dBadges}>
            <span className={styles.scopeChip}>
              {req.initiated_by === 'Driver' ? '🧑 Driver requested' : '🏢 Branch initiated'}
            </span>
            <span className={styles.scopeChip}>Effective {fmtEffectiveAt(req.effective_at)}</span>
            <span className={styles.scopeChip}>Requested {fmtDate(req.created_at_utc)}</span>
          </div>

          {/* Action buttons */}
          {!isTerminal && (canApproveSource || canDecideTarget || canComplete || canCancel) && (
            <div className={styles.dActions}>
              {canApproveSource && (req.status === 'PendingSourceApproval' || req.status === 'Returned') && (
                <button className={`${styles.actBtn} ${styles.actBtnDriver}`} onClick={onApproveSource} disabled={actionLoading}>
                  ✓ Approve & Forward to Target
                </button>
              )}
              {canDecideTarget && req.status === 'PendingTargetApproval' && (
                <>
                  <button className={`${styles.actBtn} ${styles.actBtnDriver}`} onClick={() => onDecide('Approved')} disabled={actionLoading}>
                    ✓ Approve
                  </button>
                  <button className={styles.actBtn} onClick={() => onDecide('Returned')} disabled={actionLoading}>
                    ↩ Return for Changes
                  </button>
                  <button className={`${styles.actBtn} ${styles.actBtnDanger}`} onClick={() => onDecide('Rejected')} disabled={actionLoading}>
                    ✗ Reject
                  </button>
                </>
              )}
              {canComplete && req.status === 'Approved' && (
                <button className={`${styles.actBtn} ${styles.actBtnDriver}`} onClick={onComplete} disabled={actionLoading}>
                  {actionLoading ? 'Completing…' : '🚀 Complete Transfer'}
                </button>
              )}
              {canCancel && (
                <button className={`${styles.actBtn} ${styles.actBtnDanger}`} onClick={onCancel} disabled={actionLoading}>
                  Cancel Request
                </button>
              )}
            </div>
          )}

          {actionError && (
            <div className={trStyles.actionErr}>
              {actionError}
              <button className={trStyles.actionErrClose} onClick={onClearError}>✕</button>
            </div>
          )}
        </div>
      </div>

      {/* Timeline */}
      <div className={styles.sec}>
        <h3 className={styles.secTitle}>Transfer Timeline</h3>
        <TransferTimeline req={req} />
      </div>

      {/* Details */}
      <div className={styles.sec}>
        <h3 className={styles.secTitle}>Request Details</h3>
        <div className={styles.grid2}>
          <span className={styles.fl}>Transfer ID</span>
          <span>#{req.transfer_request_id}</span>

          <span className={styles.fl}>Driver</span>
          <span>{req.driver_name ?? `Driver #${req.driver_id}`}</span>

          <span className={styles.fl}>Source branch</span>
          <span>{req.source_branch_name ?? `Branch ${req.source_branch_id}`}</span>

          <span className={styles.fl}>Target branch</span>
          <span>{req.target_branch_name ?? `Branch ${req.target_branch_id}`}</span>

          <span className={styles.fl}>Effective time</span>
          <span>{fmtEffectiveAt(req.effective_at)}</span>

          <span className={styles.fl}>Requested by</span>
          <span>{req.requested_by_name ?? `User ${req.requested_by_user_id}`}</span>

          <span className={styles.fl}>Initiated by</span>
          <span>{req.initiated_by === 'Driver' ? 'Driver self-request' : 'Source branch'}</span>

          <span className={styles.fl}>Created</span>
          <span>{fmtDateTime(req.created_at_utc)}</span>

          {req.reason && (
            <>
              <span className={styles.fl}>Reason</span>
              <span>{req.reason}</span>
            </>
          )}
          {req.notes && (
            <>
              <span className={styles.fl}>Notes</span>
              <span>{req.notes}</span>
            </>
          )}
          {req.target_decision_notes && (
            <>
              <span className={styles.fl}>Target notes</span>
              <span>{req.target_decision_notes}</span>
            </>
          )}
          {req.cancel_reason && (
            <>
              <span className={styles.fl}>Cancel reason</span>
              <span>{req.cancel_reason}</span>
            </>
          )}
          {req.new_driver_id && (
            <>
              <span className={styles.fl}>New driver ID</span>
              <span>#{req.new_driver_id} (created in {req.target_branch_name})</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function TransferTimeline({ req }: { req: DriverTransferRequest }) {
  const steps = buildTimeline(req);
  return (
    <div className={trStyles.timeline}>
      {steps.map((step, i) => (
        <div key={i} className={`${trStyles.tlStep} ${trStyles[`tlStep_${step.state}`]}`}>
          <div className={trStyles.tlLeft}>
            <div className={trStyles.tlDot} />
            {i < steps.length - 1 && <div className={trStyles.tlLine} />}
          </div>
          <div className={trStyles.tlContent}>
            <div className={trStyles.tlLabel}>{step.label}</div>
            {step.sublabel && <div className={trStyles.tlSub}>{step.sublabel}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}
