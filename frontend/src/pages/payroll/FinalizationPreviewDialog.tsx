/**
 * FinalizationPreviewDialog — CP-3B
 *
 * Shows a read-only preview of what finalize_period would do for an Approved
 * period.  If can_finalize=true, the user can confirm and execute finalization.
 *
 * Rules:
 *  - Opened only from Approved period cards on the Current Payroll hub.
 *  - All monetary values come from the backend — no frontend computation.
 *  - If blockers exist, the Finalize button is disabled.
 *  - On success the hub refreshes and the period becomes Locked.
 *  - No manual Min/Max/Adjustment controls — those are backend-only.
 */
import { useEffect, useState, useCallback } from 'react';
import { getFinalizationPreview, finalizePeriod } from '../../lib/payrollApi';
import type {
  FinalizationPreviewResponse,
  FinalizationPreviewDriverTotal,
  FinalizationPreviewSysAdjustment,
  FinalizationPreviewLine,
} from '../../types/payroll';
import styles from './FinalizationPreviewDialog.module.css';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(v: string | null | undefined): string {
  if (v == null) return '—';
  const n = parseFloat(v);
  return isNaN(n) ? String(v) : `$${n.toFixed(2)}`;
}

function fmtAdj(v: string): string {
  const n = parseFloat(v);
  if (isNaN(n)) return String(v);
  const abs = Math.abs(n).toFixed(2);
  return n >= 0 ? `+$${abs}` : `-$${abs}`;
}

function fmtQty(v: string | null | undefined): string {
  if (v == null) return '—';
  const n = parseFloat(v);
  return isNaN(n) ? String(v) : n.toFixed(2);
}

// ---------------------------------------------------------------------------
// Sub-sections
// ---------------------------------------------------------------------------

function KpiCard({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.kpiCard}>
      <div className={styles.kpiValue}>{value}</div>
      <div className={styles.kpiLabel}>{label}</div>
    </div>
  );
}

function BlockersWarnings({ blockers, warnings }: { blockers: string[]; warnings: string[] }) {
  if (blockers.length === 0 && warnings.length === 0) return null;
  return (
    <div className={styles.alertsSection}>
      {blockers.length > 0 && (
        <div className={styles.blockersBox}>
          <div className={styles.alertHeader}>
            <span className={styles.alertIcon}>&#9940;</span>
            Blockers — must be resolved before finalization
          </div>
          <ul className={styles.alertList}>
            {blockers.map((b, i) => <li key={i}>{b}</li>)}
          </ul>
        </div>
      )}
      {warnings.length > 0 && (
        <div className={styles.warningsBox}>
          <div className={styles.alertHeader}>
            <span className={styles.alertIcon}>&#9888;</span>
            Warnings — review before finalizing
          </div>
          <ul className={styles.alertList}>
            {warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

function DriverTotalsTable({ rows }: { rows: FinalizationPreviewDriverTotal[] }) {
  if (rows.length === 0) return <div className={styles.emptyMsg}>No driver totals.</div>;
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>Driver</th>
          <th className={styles.numCol}>Daily Pay</th>
          <th className={styles.numCol}>Period Pay</th>
          <th className={styles.numCol}>Sys Adj</th>
          <th className={styles.numCol}>Final Pay</th>
          <th className={styles.numCol}>Lines</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.driver_id}>
            <td className={styles.nameCell}>{r.driver_name ?? `Driver #${r.driver_id}`}</td>
            <td className={styles.numCol}>{fmt(r.daily_pay)}</td>
            <td className={styles.numCol}>{fmt(r.period_pay)}</td>
            <td className={`${styles.numCol} ${parseFloat(r.sys_adjustment) !== 0 ? styles.adjCell : ''}`}>
              {parseFloat(r.sys_adjustment) !== 0 ? fmtAdj(r.sys_adjustment) : '—'}
            </td>
            <td className={`${styles.numCol} ${styles.finalPayCell}`}>{fmt(r.final_pay)}</td>
            <td className={styles.numCol}>{r.line_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SysAdjustmentsTable({ rows }: { rows: FinalizationPreviewSysAdjustment[] }) {
  if (rows.length === 0) return <div className={styles.emptyMsg}>No system adjustments for this period.</div>;
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>Driver</th>
          <th>Type</th>
          <th className={styles.numCol}>Gross Before</th>
          <th className={styles.numCol}>Adjustment</th>
          <th className={styles.numCol}>Final Pay</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td className={styles.nameCell}>{r.driver_name ?? `Driver #${r.driver_id}`}</td>
            <td>
              <span className={`${styles.typePill} ${r.adjustment_type === 'SYS_MIN_TOPUP' ? styles.topupPill : styles.capPill}`}>
                {r.adjustment_type === 'SYS_MIN_TOPUP' ? 'Min Top-Up' : 'Max Cap'}
              </span>
            </td>
            <td className={styles.numCol}>{fmt(r.gross_before)}</td>
            <td className={`${styles.numCol} ${styles.adjCell}`}>{fmtAdj(r.adjustment_amount)}</td>
            <td className={`${styles.numCol} ${styles.finalPayCell}`}>{fmt(r.final_pay)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LineDetailsTable({ rows }: { rows: FinalizationPreviewLine[] }) {
  if (rows.length === 0) return <div className={styles.emptyMsg}>No draft lines.</div>;
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>Driver</th>
          <th>Date</th>
          <th>Type</th>
          <th>Scope</th>
          <th className={styles.numCol}>Qty</th>
          <th className={styles.numCol}>Rate</th>
          <th className={styles.numCol}>Calculated</th>
          <th className={styles.numCol}>Final</th>
          <th>Review</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.draft_line_id} className={r.needs_manager_review ? styles.reviewRow : undefined}>
            <td className={styles.nameCell}>{r.driver_name ?? `Driver #${r.driver_id}`}</td>
            <td className={styles.dateCell}>{r.work_date ?? '—'}</td>
            <td>{r.line_type}</td>
            <td>{r.line_scope}</td>
            <td className={styles.numCol}>{fmtQty(r.quantity)}</td>
            <td className={styles.numCol}>{fmt(r.rate_amount)}</td>
            <td className={styles.numCol}>{fmt(r.calculated_amount)}</td>
            <td className={`${styles.numCol} ${styles.finalPayCell}`}>{fmt(r.final_amount)}</td>
            <td>{r.needs_manager_review ? <span className={styles.reviewFlag}>&#9888;</span> : null}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// Collapsible section
// ---------------------------------------------------------------------------

function Section({
  title,
  badge,
  defaultOpen = true,
  children,
}: {
  title: string;
  badge?: string | number;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className={styles.section}>
      <button className={styles.sectionHeader} onClick={() => setOpen((o) => !o)}>
        <span className={styles.sectionToggle}>{open ? '▾' : '▸'}</span>
        <span className={styles.sectionTitle}>{title}</span>
        {badge != null && <span className={styles.sectionBadge}>{badge}</span>}
      </button>
      {open && <div className={styles.sectionBody}>{children}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Confirmation overlay
// ---------------------------------------------------------------------------

interface ConfirmFinalizeProps {
  preview: FinalizationPreviewResponse;
  onConfirm: () => void;
  onCancel: () => void;
  finalizing: boolean;
}

function ConfirmFinalize({ preview, onConfirm, onCancel, finalizing }: ConfirmFinalizeProps) {
  return (
    <div className={styles.confirmOverlay}>
      <div className={styles.confirmBox}>
        <div className={styles.confirmTitle}>Confirm Finalization</div>
        <p className={styles.confirmText}>
          You are about to finalize <strong>{preview.period_name}</strong>. This will:
        </p>
        <ul className={styles.confirmList}>
          <li>Write <strong>{preview.final_line_count_estimate}</strong> final payroll lines</li>
          <li>Lock the period — no further edits</li>
          <li>Total gross: <strong>{fmt(preview.total_final_gross)}</strong></li>
          <li>Drivers paid: <strong>{preview.driver_count}</strong></li>
        </ul>
        <p className={styles.confirmWarning}>This action cannot be undone.</p>
        <div className={styles.confirmActions}>
          <button className={styles.cancelBtn} onClick={onCancel} disabled={finalizing}>
            Cancel
          </button>
          <button className={styles.finalizeBtn} onClick={onConfirm} disabled={finalizing}>
            {finalizing ? 'Finalizing…' : 'Confirm Finalize'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main dialog
// ---------------------------------------------------------------------------

interface FinalizationPreviewDialogProps {
  periodId: number;
  periodName?: string;
  onClose: () => void;
  onFinalized: () => void; // called after successful finalization → hub refreshes
}

export function FinalizationPreviewDialog({
  periodId,
  periodName,
  onClose,
  onFinalized,
}: FinalizationPreviewDialogProps) {
  const [preview, setPreview] = useState<FinalizationPreviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [showConfirm, setShowConfirm] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [finalizeError, setFinalizeError] = useState<string | null>(null);
  const [finalizeSuccess, setFinalizeSuccess] = useState(false);

  const fetchPreview = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const data = await getFinalizationPreview(periodId);
      setPreview(data);
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status;
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      if (status === 403) {
        setLoadError('Access denied. You do not have permission to view finalization data.');
      } else if (status === 422) {
        setLoadError(detail ?? 'This period is not in Approved status and cannot be previewed.');
      } else {
        setLoadError(detail ?? 'Failed to load finalization preview.');
      }
    } finally {
      setLoading(false);
    }
  }, [periodId]);

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void fetchPreview(); }, [fetchPreview]);

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && !showConfirm && !finalizing) onClose();
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose, showConfirm, finalizing]);

  async function handleFinalize() {
    setFinalizing(true);
    setFinalizeError(null);
    try {
      await finalizePeriod(periodId);
      setShowConfirm(false);
      setFinalizeSuccess(true);
      onFinalized(); // trigger hub refresh
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status;
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setShowConfirm(false);
      if (status === 403) {
        setFinalizeError('Access denied. You do not have permission to finalize this period.');
      } else {
        setFinalizeError(detail ?? 'Finalization failed. The period was not locked.');
      }
    } finally {
      setFinalizing(false);
    }
  }

  // Derived
  const periodPayTotal = preview
    ? preview.driver_totals.reduce((s, d) => s + parseFloat(d.period_pay), 0)
    : 0;
  const sysTotal = preview
    ? preview.sys_adjustments.reduce((s, a) => s + parseFloat(a.adjustment_amount), 0)
    : 0;

  return (
    <div
      className={styles.backdrop}
      onClick={!showConfirm && !finalizing ? onClose : undefined}
    >
      <div
        className={styles.dialog}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Finalization Preview"
      >
        {/* ── Header ─────────────────────────────────────────────────── */}
        <div className={styles.dialogHeader}>
          <div>
            <div className={styles.dialogTitle}>
              Finalization Preview
              {periodName ? <span className={styles.dialogSubtitle}> — {periodName}</span> : null}
            </div>
            {preview && (
              <div className={styles.headerMeta}>
                <span>{preview.branch_name ?? ''}</span>
                {preview.branch_name && <span className={styles.metaSep}>·</span>}
                <span className={styles.statusPill}>{preview.period_status}</span>
              </div>
            )}
          </div>
          <button className={styles.closeBtn} onClick={onClose} aria-label="Close" disabled={finalizing}>
            &#x2715;
          </button>
        </div>

        {/* ── Body ───────────────────────────────────────────────────── */}
        <div className={styles.dialogBody}>
          {loading ? (
            <div className={styles.stateMsg}>Loading preview…</div>
          ) : loadError ? (
            <div className={styles.errorBox}>{loadError}</div>
          ) : preview == null ? null : finalizeSuccess ? (
            <div className={styles.successBox}>
              <div className={styles.successIcon}>&#10003;</div>
              <div className={styles.successTitle}>Period Finalized</div>
              <p className={styles.successText}>
                <strong>{preview.period_name}</strong> has been locked.{' '}
                {preview.final_line_count_estimate} final payroll lines were written.
              </p>
              <button className={styles.doneBtn} onClick={onClose}>Close</button>
            </div>
          ) : (
            <>
              {/* ── KPIs ─────────────────────────────────────────── */}
              <div className={styles.kpiRow}>
                <KpiCard label="Drivers Paid" value={String(preview.driver_count)} />
                <KpiCard
                  label="Period Pay / Bonus"
                  value={periodPayTotal !== 0 ? `$${periodPayTotal.toFixed(2)}` : '—'}
                />
                <KpiCard
                  label="System Adjustments"
                  value={sysTotal !== 0 ? fmtAdj(sysTotal.toFixed(2)) : 'None'}
                />
                <KpiCard label="Final Gross" value={fmt(preview.total_final_gross)} />
                <KpiCard label="Final Lines" value={String(preview.final_line_count_estimate)} />
              </div>

              {/* ── Blockers / Warnings ───────────────────────── */}
              <BlockersWarnings blockers={preview.blockers} warnings={preview.warnings} />

              {/* ── Driver Totals ─────────────────────────────── */}
              <Section
                title="Driver Totals"
                badge={preview.driver_totals.length}
              >
                <DriverTotalsTable rows={preview.driver_totals} />
              </Section>

              {/* ── System Adjustments ───────────────────────── */}
              <Section
                title="System Adjustments"
                badge={preview.sys_adjustment_count}
                defaultOpen={preview.sys_adjustment_count > 0}
              >
                <SysAdjustmentsTable rows={preview.sys_adjustments} />
              </Section>

              {/* ── Line Details ─────────────────────────────── */}
              <Section
                title="Line Details"
                badge={`${preview.draft_line_count} draft`}
                defaultOpen={false}
              >
                <LineDetailsTable rows={preview.lines} />
              </Section>

              {/* ── Finalize error ───────────────────────────── */}
              {finalizeError && (
                <div className={styles.errorBox}>{finalizeError}</div>
              )}
            </>
          )}
        </div>

        {/* ── Footer ─────────────────────────────────────────────────── */}
        {!loading && !loadError && preview && !finalizeSuccess && (
          <div className={styles.dialogFooter}>
            <div className={styles.footerLeft}>
              {!preview.can_finalize && preview.blockers.length > 0 && (
                <span className={styles.blockerHint}>
                  Resolve {preview.blockers.length} blocker{preview.blockers.length > 1 ? 's' : ''} before finalizing
                </span>
              )}
            </div>
            <div className={styles.footerRight}>
              <button className={styles.cancelBtn} onClick={onClose} disabled={finalizing}>
                Close
              </button>
              <button
                className={styles.finalizeBtn}
                disabled={!preview.can_finalize || finalizing}
                onClick={() => setShowConfirm(true)}
              >
                Finalize Payroll
              </button>
            </div>
          </div>
        )}

        {/* ── Confirm overlay ─────────────────────────────────────────── */}
        {showConfirm && preview && (
          <ConfirmFinalize
            preview={preview}
            onConfirm={() => void handleFinalize()}
            onCancel={() => setShowConfirm(false)}
            finalizing={finalizing}
          />
        )}
      </div>
    </div>
  );
}
