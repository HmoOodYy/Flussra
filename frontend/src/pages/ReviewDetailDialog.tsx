/**
 * ReviewDetailDialog — full review dialog for an InReview payroll period.
 *
 * Shows the immutable submitted payroll packet and
 * provides Approve / Return-for-Correction actions.
 *
 * Security: this component only receives data that the parent (ReviewPage)
 * fetched from the backend.  All authorization is enforced by the backend;
 * the frontend disables Approve when the backend would reject it anyway.
 */
import { useEffect, useState, useCallback } from 'react';
import { decideReviewItem, getReviewItemPayrollSnapshot } from '../lib/reviewApi';
import { useAuth } from '../store/authStore';
import { canDecideReview } from '../lib/permissions';
import { PeriodStatusBadge } from '../components/StatusBadge';
import type { ReviewItemSummary, ReviewPayrollSnapshot } from '../types/review';
import type { PeriodSummary } from '../types/payroll';
import styles from './ReviewDetailDialog.module.css';

interface Props {
  item: ReviewItemSummary;
  period: PeriodSummary;
  onClose: () => void;
  onDecided: () => void;
}

type ActionState = 'idle' | 'approving' | 'returning';

function fmt(v: string | number | null | undefined): string {
  if (v == null) return '—';
  const n = typeof v === 'string' ? parseFloat(v) : v;
  if (isNaN(n)) return '—';
  return '$' + n.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function fmtDate(d: string): string {
  // "2085-03-03" → "Mar 3, 2085"
  const dt = new Date(d + 'T00:00:00');
  return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function ReviewDetailDialog({ item, period, onClose, onDecided }: Props) {
  const [snapshot, setSnapshot] = useState<ReviewPayrollSnapshot | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  const [actionState, setActionState] = useState<ActionState>('idle');
  const [returnReason, setReturnReason] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setLoadErr(null);
      setSnapshot(await getReviewItemPayrollSnapshot(item.review_item_id));
    } catch {
      setSnapshot(null);
      setLoadErr('Failed to load the submitted payroll snapshot.');
    }
  }, [item.review_item_id]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  // Keyboard: Esc closes (only when not mid-action)
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && actionState === 'idle') onClose();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, actionState]);

  async function handleApprove() {
    setActionState('approving');
    setActionError(null);
    try {
      await decideReviewItem(item.review_item_id, { decision: 'Approved' });
      onDecided();
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        ?? 'Approval failed. Please try again.';
      setActionError(msg);
      setActionState('idle');
    }
  }

  async function handleReturn() {
    setActionState('returning');
    setActionError(null);
    try {
      await decideReviewItem(item.review_item_id, {
        decision: 'EditRequested',
        decision_reason: returnReason.trim() || undefined,
      });
      onDecided();
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
        ?? 'Return action failed. Please try again.';
      setActionError(msg);
      setActionState('idle');
    }
  }

  const { user } = useAuth();
  const userCanDecide = user ? canDecideReview(user) : false;

  const periodLines = snapshot?.lines.filter(line => line.line_scope === 'Period') ?? [];
  const busy = actionState !== 'idle';

  return (
    <div className={styles.overlay} onClick={e => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className={styles.dialog} role="dialog" aria-modal="true" aria-label="Review Period">

        {/* Header */}
        <div className={styles.header}>
          <div className={styles.headerLeft}>
            <h2 className={styles.dialogTitle}>
              {period.period_name}
              <span style={{ marginLeft: '0.6rem', verticalAlign: 'middle' }}>
                <PeriodStatusBadge status={period.status} />
              </span>
            </h2>
            <div className={styles.headerMeta}>
              <span>{period.branch_name}</span>
              <span className={styles.metaSep}>·</span>
              <span>{fmtDate(period.start_date)} – {fmtDate(period.end_date)}</span>
              <span className={styles.metaSep}>·</span>
              <span>{period.period_type}</span>
              {period.pay_date && (
                <>
                  <span className={styles.metaSep}>·</span>
                  <span>Pay: {fmtDate(period.pay_date)}</span>
                </>
              )}
            </div>
          </div>
          <button className={styles.closeBtn} onClick={onClose} disabled={busy} aria-label="Close">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className={styles.body}>

          {/* KPI chips */}
          <div className={styles.kpiRow}>
            <div className={styles.kpiChip}>
              <span className={styles.kpiLabel}>Drivers</span>
              <span className={styles.kpiValue}>{snapshot?.driver_totals.length ?? '—'}</span>
            </div>
            <div className={styles.kpiChip}>
              <span className={styles.kpiLabel}>Lines</span>
              <span className={styles.kpiValue}>{snapshot?.lines.length ?? '—'}</span>
            </div>
            <div className={styles.kpiChip}>
              <span className={styles.kpiLabel}>Expected Pay</span>
              <span className={styles.kpiValue}>{fmt(snapshot?.total_expected_pay)}</span>
            </div>
            <div className={styles.kpiChip}>
              <span className={styles.kpiLabel}>Submitted by</span>
              <span className={styles.kpiValue} style={{ fontSize: '0.85rem' }}>
                {item.requested_by ?? '—'}
              </span>
            </div>
          </div>

          {/* Load error */}
          {loadErr && <div className={styles.errorMsg}>{loadErr}</div>}

          {/* Driver totals */}
          <div className={styles.section}>
            <h3 className={styles.sectionTitle}>Driver Totals</h3>
            {!snapshot ? (
              <p className={styles.loadingMsg}>Loading…</p>
            ) : snapshot.driver_totals.length === 0 ? (
              <p className={styles.emptyMsg}>No driver lines.</p>
            ) : (
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>Driver</th>
                    <th className={styles.right}>Lines</th>
                    <th className={styles.right}>Gross Pay</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.driver_totals.map(d => (
                    <tr key={d.driver_id}>
                      <td>{d.driver_name_snapshot ?? d.driver_code_snapshot ?? `Driver ${d.driver_id}`}</td>
                      <td className={styles.right}>{snapshot.lines.filter(line => line.driver_id === d.driver_id).length}</td>
                      <td className={styles.right}>{fmt(d.expected_pay)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Period pay / bonus */}
          {periodLines.length > 0 && (
            <div className={styles.section}>
              <h3 className={styles.sectionTitle}>Period Pay / Bonus</h3>
              {periodLines.map((line, index) => (
                <div key={`${line.driver_id}-${line.line_type}-${index}`} className={styles.payRow}>
                  <span>
                    {line.line_type}
                    {line.work_date && <> · {fmtDate(line.work_date)}</>}
                  </span>
                  <span className={styles.payAmount}>
                    {fmt(line.calculated_amount)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* Action error */}
          {actionError && <div className={styles.errorMsg}>{actionError}</div>}

        </div>

        {/* Footer */}
        <div className={styles.footer}>
          <div className={styles.footerLeft}>
            {userCanDecide && (
              <input
                className={styles.reasonInput}
                placeholder="Reason for return (optional)"
                value={returnReason}
                onChange={e => setReturnReason(e.target.value)}
                disabled={busy}
                aria-label="Return reason"
              />
            )}
          </div>
          <div className={styles.footerRight}>
            <button className={styles.btnClose} onClick={onClose} disabled={busy}>
              Close
            </button>
            {userCanDecide && (
              <>
                <button
                  className={styles.btnReturn}
                  onClick={handleReturn}
                  disabled={busy}
                  title="Return period to Open for corrections (EditRequested)"
                >
                  {actionState === 'returning' ? 'Returning…' : 'Return for Correction'}
                </button>
                <button
                  className={styles.btnApprove}
                  onClick={handleApprove}
                  disabled={busy || !snapshot}
                  title={snapshot ? 'Approve the submitted payroll snapshot' : 'Submitted payroll snapshot unavailable'}
                >
                  {actionState === 'approving' ? 'Approving…' : 'Approve Period'}
                </button>
              </>
            )}
          </div>
        </div>

      </div>
    </div>
  );
}
