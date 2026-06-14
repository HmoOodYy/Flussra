/**
 * ReviewDetailDialog — full review dialog for an InReview payroll period.
 *
 * Shows period summary, NMR blockers, driver totals, period pay, and
 * provides Approve / Return-for-Correction actions.
 *
 * Security: this component only receives data that the parent (ReviewPage)
 * fetched from the backend.  All authorization is enforced by the backend;
 * the frontend disables Approve when the backend would reject it anyway.
 */
import { useEffect, useState, useCallback } from 'react';
import { decideReviewItem } from '../lib/reviewApi';
import { useAuth } from '../store/authStore';
import { canDecideReview } from '../lib/permissions';
import { PeriodStatusBadge } from '../components/StatusBadge';
import { getDriverPeriodSummary, getPeriodPayLines, getDraftLines } from '../lib/payrollApi';
import type { ReviewItemSummary } from '../types/review';
import type { PeriodSummary, DriverPeriodSummary, PeriodPayLine, DraftLineSummary } from '../types/payroll';
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

/** Aggregate DriverPeriodSummary[] (which is per driver×lineType) into per-driver totals */
function buildDriverTotalsMap(rows: DriverPeriodSummary[]): Map<number, { name: string; gross: number; lines: number }> {
  const map = new Map<number, { name: string; gross: number; lines: number }>();
  for (const r of rows) {
    const cur = map.get(r.driver_id) ?? { name: r.driver_name, gross: 0, lines: 0 };
    cur.gross += parseFloat(r.total_calculated_amount) || 0;
    cur.lines += r.line_count;
    map.set(r.driver_id, cur);
  }
  return map;
}

export function ReviewDetailDialog({ item, period, onClose, onDecided }: Props) {
  const pid = period.payroll_period_id;
  const hasNmr = period.draft_lines_needing_attention > 0;

  const [driverRows, setDriverRows] = useState<DriverPeriodSummary[] | null>(null);
  const [payLines, setPayLines] = useState<PeriodPayLine[] | null>(null);
  const [nmrLines, setNmrLines] = useState<DraftLineSummary[]>([]);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  const [actionState, setActionState] = useState<ActionState>('idle');
  const [returnReason, setReturnReason] = useState('');
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [ds, pp, allLines] = await Promise.all([
        getDriverPeriodSummary(pid),
        getPeriodPayLines(pid),
        hasNmr ? getDraftLines(pid) : Promise.resolve([] as DraftLineSummary[]),
      ]);
      setDriverRows(ds);
      setPayLines(pp);
      setNmrLines(allLines.filter(l => l.needs_manager_review && l.status !== 'Void'));
    } catch {
      setLoadErr('Failed to load period details.');
    }
  }, [pid, hasNmr]);

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

  const driverTotals = driverRows ? buildDriverTotalsMap(driverRows) : null;
  const bonusLines = payLines?.filter(l => l.status !== 'Void') ?? [];
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
              <span className={styles.kpiValue}>{period.draft_drivers}</span>
            </div>
            <div className={styles.kpiChip}>
              <span className={styles.kpiLabel}>Lines</span>
              <span className={styles.kpiValue}>{period.draft_lines}</span>
            </div>
            <div className={styles.kpiChip}>
              <span className={styles.kpiLabel}>Needs Attention</span>
              <span className={styles.kpiValue} style={{ color: hasNmr ? '#dc2626' : undefined }}>
                {period.draft_lines_needing_attention}
              </span>
            </div>
            <div className={styles.kpiChip}>
              <span className={styles.kpiLabel}>Submitted by</span>
              <span className={styles.kpiValue} style={{ fontSize: '0.85rem' }}>
                {item.requested_by ?? '—'}
              </span>
            </div>
          </div>

          {/* NMR Blockers */}
          {hasNmr && (
            <div className={styles.section}>
              <h3 className={styles.sectionTitle}>Lines Requiring Manager Review</h3>
              {nmrLines.length > 0 ? (
                <div className={styles.blockerBox}>
                  <p className={styles.blockerTitle}>
                    ⚠ {nmrLines.length} line{nmrLines.length !== 1 ? 's' : ''} need manager review
                    — approval is blocked until resolved.
                  </p>
                  <ul className={styles.blockerList}>
                    {nmrLines.slice(0, 10).map(l => (
                      <li key={l.draft_line_id}>
                        <span className={styles.nmrLineType}>{l.line_type}</span>
                        {l.driver_name && <> — {l.driver_name}</>}
                        {l.work_date && <> on {fmtDate(l.work_date)}</>}
                        {l.notes && <> · {l.notes}</>}
                      </li>
                    ))}
                    {nmrLines.length > 10 && <li>…and {nmrLines.length - 10} more</li>}
                  </ul>
                </div>
              ) : (
                <div className={styles.blockerBox}>
                  <p className={styles.blockerTitle}>
                    ⚠ {period.draft_lines_needing_attention} line(s) flagged for attention.
                    Approve is disabled — reload or resolve before approving.
                  </p>
                </div>
              )}
            </div>
          )}

          {/* Load error */}
          {loadErr && <div className={styles.errorMsg}>{loadErr}</div>}

          {/* Driver totals */}
          <div className={styles.section}>
            <h3 className={styles.sectionTitle}>Driver Totals</h3>
            {!driverTotals ? (
              <p className={styles.loadingMsg}>Loading…</p>
            ) : driverTotals.size === 0 ? (
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
                  {Array.from(driverTotals.values()).map((d, i) => (
                    <tr key={i}>
                      <td>{d.name}</td>
                      <td className={styles.right}>{d.lines}</td>
                      <td className={styles.right}>{fmt(d.gross)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Period pay / bonus */}
          {payLines != null && bonusLines.length > 0 && (
            <div className={styles.section}>
              <h3 className={styles.sectionTitle}>Period Pay / Bonus</h3>
              {bonusLines.map(l => (
                <div key={l.draft_line_id} className={styles.payRow}>
                  <span>
                    {l.line_type}
                    {l.driver_name && <> — <em>{l.driver_name}</em></>}
                    {l.notes && <> · {l.notes}</>}
                  </span>
                  <span className={styles.payAmount}>
                    {fmt(l.calculated_amount ?? l.rate_amount)}
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
                  disabled={busy || hasNmr}
                  title={hasNmr ? 'Cannot approve: lines need manager review' : 'Approve this period'}
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
