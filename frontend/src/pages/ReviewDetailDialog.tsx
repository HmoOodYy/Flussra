/**
 * ReviewDetailDialog - immutable financial packet for one ReviewItem.
 *
 * The dialog deliberately reads ReviewItem history and its linked submitted
 * snapshot. It never reads mutable payroll source data as review authority.
 */
import { useEffect, useState } from 'react';
import { decideReviewItem, getReviewItem, getReviewItemPayrollSnapshot } from '../lib/reviewApi';
import { useAuth } from '../store/authStore';
import { canDecideReview } from '../lib/permissions';
import type {
  ReviewItemDetail,
  ReviewItemSummary,
  ReviewPayrollSnapshot,
  ReviewPayrollSnapshotLine,
} from '../types/review';
import styles from './ReviewDetailDialog.module.css';

interface Props {
  item: ReviewItemSummary;
  onClose: () => void;
  onDecided: () => void;
}

type ActionState = 'idle' | 'approving' | 'returning';

function formatMoney(value: string | null | undefined): string {
  if (value == null) return 'Not resolved';
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(numeric)
    : value;
}

function formatQuantity(value: string | null): string {
  if (value == null) return '-';
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toLocaleString(undefined, { maximumFractionDigits: 4 }) : value;
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : date.toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function formatDate(value: string | null): string {
  if (!value) return '-';
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.valueOf())
    ? value
    : date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function statusLabel(status: string): string {
  if (status === 'EditRequested') return 'Returned for Correction';
  return status;
}

function lineLabel(line: ReviewPayrollSnapshotLine): string {
  if (line.line_type === 'SYS_MIN_TOPUP') return 'Minimum top-up';
  if (line.line_type === 'SYS_MAX_CAP') return 'Maximum cap';
  if (line.source_type === 'StatusEntryState') return 'Status payment';
  if (line.source_type === 'BonusEvent') return 'Bonus event';
  return line.line_type;
}

function errorDetail(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') {
    if (detail.includes('SNAPSHOT_REQUIRED_FOR_APPROVAL')) {
      return 'A submitted payroll snapshot is required before this legacy review item can be approved. Return it for correction and resubmit the payroll.';
    }
    return detail;
  }
  return fallback;
}

function historicalMessage(status: string): string | null {
  if (status === 'Pending') return null;
  if (status === 'EditRequested' || status === 'Rejected') {
    return 'This review revision was returned for correction and is historical. A newer resubmission, when present, has its own Pending review item.';
  }
  if (status === 'Approved') return 'This review revision is approved history and is no longer actionable.';
  return 'This review item is historical and is no longer actionable.';
}

export function ReviewDetailDialog({ item, onClose, onDecided }: Props) {
  const [detail, setDetail] = useState<ReviewItemDetail | null>(null);
  const [snapshot, setSnapshot] = useState<ReviewPayrollSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [snapshotError, setSnapshotError] = useState<string | null>(null);
  const [actionState, setActionState] = useState<ActionState>('idle');
  const [returnReason, setReturnReason] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function load() {
      setLoading(true);
      setDetail(null);
      setSnapshot(null);
      setDetailError(null);
      setSnapshotError(null);
      const [detailResult, snapshotResult] = await Promise.allSettled([
        getReviewItem(item.review_item_id),
        getReviewItemPayrollSnapshot(item.review_item_id),
      ]);
      if (!active) return;

      if (detailResult.status === 'fulfilled') {
        setDetail(detailResult.value);
      } else {
        setDetailError(errorDetail(detailResult.reason, 'Failed to load review item history.'));
      }
      if (snapshotResult.status === 'fulfilled') {
        setSnapshot(snapshotResult.value);
      } else {
        setSnapshotError(errorDetail(snapshotResult.reason, 'Failed to load the submitted payroll snapshot.'));
      }
      setLoading(false);
    }
    void load();
    return () => { active = false; };
  }, [item.review_item_id]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape' && actionState === 'idle') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [actionState, onClose]);

  const { user } = useAuth();
  const reviewItem = detail ?? item;
  const isPending = reviewItem.status === 'Pending';
  const canAct = detail !== null && detail.status === 'Pending' && user !== null && canDecideReview(user, detail.branch_id);
  const busy = actionState !== 'idle';

  async function handleApprove() {
    setActionState('approving');
    setActionError(null);
    try {
      await decideReviewItem(item.review_item_id, { decision: 'Approved' });
      onDecided();
    } catch (error: unknown) {
      setActionError(errorDetail(error, 'Approval failed. Please try again.'));
      setActionState('idle');
    }
  }

  async function handleReturn() {
    const reason = returnReason.trim();
    if (!reason) {
      setActionError('A return reason is required before this payroll can be returned for correction.');
      return;
    }
    setActionState('returning');
    setActionError(null);
    try {
      await decideReviewItem(item.review_item_id, {
        decision: 'EditRequested',
        decision_reason: reason,
      });
      onDecided();
    } catch (error: unknown) {
      setActionError(errorDetail(error, 'Return action failed. Please try again.'));
      setActionState('idle');
    }
  }

  const decisionHistory = detail?.decisions ?? [];
  const finalReason = detail?.final_decision_reason ?? item.final_decision_reason;
  const driverNames = new Map(snapshot?.driver_totals.map((driver) => [
    driver.driver_id,
    driver.driver_name_snapshot ?? driver.driver_code_snapshot ?? `Driver ${driver.driver_id}`,
  ]));

  return (
    <div className={styles.overlay} onClick={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
      <div className={styles.dialog} role="dialog" aria-modal="true" aria-label="Payroll review">
        <header className={styles.header}>
          <div className={styles.headerLeft}>
            <div className={styles.titleRow}>
              <h2 className={styles.dialogTitle}>{reviewItem.title}</h2>
              <span className={isPending ? styles.pendingBadge : styles.historyBadge}>{statusLabel(reviewItem.status)}</span>
            </div>
            <div className={styles.headerMeta}>
              <span>{reviewItem.branch_name ?? 'Branch unavailable'}</span>
              {reviewItem.entity_id && <><span className={styles.metaSep}>/</span><span>Payroll period #{reviewItem.entity_id}</span></>}
              <span className={styles.metaSep}>/</span>
              <span>Submitted {formatTimestamp(reviewItem.created_at_utc)}</span>
              {reviewItem.requested_by && <><span className={styles.metaSep}>/</span><span>Submitted by {reviewItem.requested_by}</span></>}
            </div>
          </div>
          <button className={styles.closeBtn} onClick={onClose} disabled={busy} aria-label="Close">x</button>
        </header>

        <div className={styles.body}>
          {historicalMessage(reviewItem.status) && <div className={styles.historyNotice}>{historicalMessage(reviewItem.status)}</div>}
          {detailError && <div className={styles.errorMsg}>{detailError}</div>}
          {snapshotError && <div className={styles.errorMsg}>{snapshotError}</div>}

          {loading ? (
            <p className={styles.loadingMsg}>Loading submitted payroll snapshot...</p>
          ) : snapshot ? (
            <>
              <section className={styles.snapshotIdentity}>
                <div>
                  <span className={styles.identityLabel}>Submitted payroll snapshot</span>
                  <strong>Revision {snapshot.revision_number}</strong>
                </div>
                <div>
                  <span className={styles.identityLabel}>Captured at</span>
                  <strong>{formatTimestamp(snapshot.captured_at_utc)}</strong>
                </div>
                <div>
                  <span className={styles.identityLabel}>Expected payroll</span>
                  <strong>{formatMoney(snapshot.total_expected_pay)}</strong>
                </div>
              </section>

              <section className={styles.section}>
                <h3 className={styles.sectionTitle}>Driver Totals</h3>
                {snapshot.driver_totals.length === 0 ? (
                  <p className={styles.emptyMsg}>No driver totals were captured.</p>
                ) : (
                  <div className={styles.tableWrap}>
                    <table className={styles.table}>
                      <thead>
                        <tr>
                          <th>Driver</th>
                          <th className={styles.right}>Daily</th>
                          <th className={styles.right}>Status</th>
                          <th className={styles.right}>Period</th>
                          <th className={styles.right}>Min</th>
                          <th className={styles.right}>Max</th>
                          <th className={styles.right}>Bonus</th>
                          <th className={styles.right}>Expected Pay</th>
                        </tr>
                      </thead>
                      <tbody>
                        {snapshot.driver_totals.map((driver) => (
                          <tr key={driver.driver_id}>
                            <td>{driver.driver_name_snapshot ?? driver.driver_code_snapshot ?? `Driver ${driver.driver_id}`}</td>
                            <td className={styles.right}>{formatMoney(driver.daily_pay)}</td>
                            <td className={styles.right}>{formatMoney(driver.status_pay)}</td>
                            <td className={styles.right}>{formatMoney(driver.period_pay)}</td>
                            <td className={styles.right}>{formatMoney(driver.minimum_adjustment)}</td>
                            <td className={styles.right}>{formatMoney(driver.maximum_adjustment)}</td>
                            <td className={styles.right}>{formatMoney(driver.bonus_total)}</td>
                            <td className={`${styles.right} ${styles.expectedPay}`}>{formatMoney(driver.expected_pay)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              <section className={styles.section}>
                <h3 className={styles.sectionTitle}>Submitted Financial Lines</h3>
                {snapshot.lines.length === 0 ? (
                  <p className={styles.emptyMsg}>No financial lines were captured.</p>
                ) : (
                  <div className={styles.tableWrap}>
                    <table className={styles.table}>
                      <thead>
                        <tr>
                          <th>Driver</th>
                          <th>Source</th>
                          <th>Line</th>
                          <th>Date</th>
                          <th className={styles.right}>Quantity</th>
                          <th className={styles.right}>Resolved Rate</th>
                          <th className={styles.right}>Calculated Amount</th>
                        </tr>
                      </thead>
                      <tbody>
                        {snapshot.lines.map((line, index) => (
                          <tr key={`${line.driver_id}-${line.source_type}-${line.line_type}-${index}`}>
                            <td>{driverNames.get(line.driver_id) ?? `Driver ${line.driver_id}`}</td>
                            <td>{line.source_type}</td>
                            <td>{lineLabel(line)}</td>
                            <td>{formatDate(line.work_date)}</td>
                            <td className={styles.right}>{formatQuantity(line.quantity)}</td>
                            <td className={styles.right}>{line.resolved_rate_amount == null ? '-' : formatMoney(line.resolved_rate_amount)}</td>
                            <td className={styles.right}>{formatMoney(line.calculated_amount)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </>
          ) : null}

          <section className={styles.section}>
            <h3 className={styles.sectionTitle}>Review History</h3>
            {decisionHistory.length > 0 ? (
              <div className={styles.decisionList}>
                {decisionHistory.map((decision) => (
                  <div key={decision.review_decision_id} className={styles.decisionRow}>
                    <strong>{statusLabel(decision.decision)}</strong>
                    <span>{decision.decided_by ?? 'Reviewer'} / {formatTimestamp(decision.created_at_utc)}</span>
                    {decision.decision_reason && <p>{decision.decision_reason}</p>}
                  </div>
                ))}
              </div>
            ) : finalReason ? (
              <p className={styles.decisionFallback}>{finalReason}</p>
            ) : (
              <p className={styles.emptyMsg}>No decision has been recorded for this review item.</p>
            )}
          </section>

          {actionError && <div className={styles.errorMsg}>{actionError}</div>}
        </div>

        <footer className={styles.footer}>
          <div className={styles.footerLeft}>
            {canAct && (
              <input
                className={styles.reasonInput}
                placeholder="Reason for return (required)"
                value={returnReason}
                onChange={(event) => setReturnReason(event.target.value)}
                disabled={busy}
                aria-label="Return reason"
              />
            )}
          </div>
          <div className={styles.footerRight}>
            <button className={styles.btnClose} onClick={onClose} disabled={busy}>Close</button>
            {canAct && (
              <>
                <button className={styles.btnReturn} onClick={handleReturn} disabled={busy} title="Return this submitted revision for correction">
                  {actionState === 'returning' ? 'Returning...' : 'Return for Correction'}
                </button>
                <button
                  className={styles.btnApprove}
                  onClick={handleApprove}
                  disabled={busy || !snapshot}
                  title={snapshot ? 'Approve this submitted payroll snapshot' : 'A submitted payroll snapshot is required before approval'}
                >
                  {actionState === 'approving' ? 'Approving...' : 'Approve Period'}
                </button>
              </>
            )}
          </div>
        </footer>
      </div>
    </div>
  );
}
