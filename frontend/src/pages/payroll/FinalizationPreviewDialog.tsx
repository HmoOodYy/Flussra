/**
 * FinalizationPreviewDialog - CP-4F approved snapshot finalization.
 *
 * Shows the exact immutable payroll packet approved by review before it is
 * projected into FinalLines and the period is locked.
 *
 * Rules:
 *  - Opened only from Approved period cards on the Current Payroll hub.
 *  - All monetary values come from the approved-packet backend response.
 *  - If blockers exist, the Finalize button is disabled.
 *  - On success the hub refreshes and the period becomes Locked.
 *  - No manual Min/Max/Adjustment controls — those are backend-only.
 */
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { getFinalizationPreview, finalizePeriod } from '../../lib/payrollApi';
import type {
  FinalizationPreviewResponse,
  FinalizationPreviewDriverTotal,
  FinalizationPreviewSysAdjustment,
  FinalizationPreviewLine,
} from '../../types/payroll';
import { finalizedLedgerPath } from './finalizedNavigation';
import styles from './FinalizationPreviewDialog.module.css';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(v: string | null | undefined): string {
  if (v == null) return '-';
  const n = Number(v);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : String(v);
}

function fmtAdj(v: string): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  const abs = Math.abs(n).toFixed(2);
  return n >= 0 ? `+$${abs}` : `-$${abs}`;
}

function fmtQty(v: string | null | undefined): string {
  if (v == null) return '-';
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(v);
}

function hasNonZero(value: string): boolean {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric !== 0;
}

function previewError(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') {
    if (detail.includes('SNAPSHOT_REQUIRED_FOR_FINALIZATION')) {
      return 'This approved payroll does not have the required submitted snapshot and cannot be finalized.';
    }
    if (detail.includes('APPROVED_SNAPSHOT_NOT_FOUND_FOR_FINALIZATION')) {
      return 'This approved payroll does not have a usable approved snapshot and cannot be finalized.';
    }
    if (detail.includes('APPROVED_SNAPSHOT_INTEGRITY_ERROR')) {
      return 'The approved payroll packet could not be verified and cannot be finalized.';
    }
    return detail;
  }
  return fallback;
}

function lineLabel(line: FinalizationPreviewLine): string {
  if (line.line_type === 'SYS_MIN_TOPUP') return 'Minimum top-up';
  if (line.line_type === 'SYS_MAX_CAP') return 'Maximum cap';
  return line.line_type;
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
          <th className={styles.numCol}>Status</th>
          <th className={styles.numCol}>Period Pay</th>
          <th className={styles.numCol}>Sys Adj</th>
          <th className={styles.numCol}>Bonus</th>
          <th className={styles.numCol}>Final Pay</th>
          <th className={styles.numCol}>Lines</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.driver_id}>
            <td className={styles.nameCell}>{r.driver_name ?? `Driver #${r.driver_id}`}</td>
            <td className={styles.numCol}>{fmt(r.daily_pay)}</td>
            <td className={styles.numCol}>{fmt(r.status_pay)}</td>
            <td className={styles.numCol}>{fmt(r.period_pay)}</td>
            <td className={`${styles.numCol} ${hasNonZero(r.sys_adjustment) ? styles.adjCell : ''}`}>
              {hasNonZero(r.sys_adjustment) ? fmtAdj(r.sys_adjustment) : '-'}
            </td>
            <td className={styles.numCol}>{fmt(r.bonus_total)}</td>
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
  if (rows.length === 0) return <div className={styles.emptyMsg}>No approved financial lines.</div>;
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
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.source_key}>
            <td className={styles.nameCell}>{r.driver_name ?? `Driver #${r.driver_id}`}</td>
            <td className={styles.dateCell}>{r.work_date ?? '-'}</td>
            <td>{lineLabel(r)}</td>
            <td>{r.line_scope}</td>
            <td className={styles.numCol}>{fmtQty(r.quantity)}</td>
            <td className={styles.numCol}>{fmt(r.rate_amount)}</td>
            <td className={styles.numCol}>{fmt(r.calculated_amount)}</td>
            <td className={`${styles.numCol} ${styles.finalPayCell}`}>{fmt(r.final_amount)}</td>
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
          You are about to lock <strong>{preview.period_name}</strong> using its approved payroll packet. This will:
        </p>
        <ul className={styles.confirmList}>
          <li>Project <strong>{preview.final_line_count_estimate}</strong> approved financial lines into locked history</li>
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
  onFinalized: () => void;
  onStateConflict: () => void;
}

export function FinalizationPreviewDialog({
  periodId,
  periodName,
  onClose,
  onFinalized,
  onStateConflict,
}: FinalizationPreviewDialogProps) {
  const navigate = useNavigate();
  const [preview, setPreview] = useState<FinalizationPreviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [showConfirm, setShowConfirm] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [finalizeError, setFinalizeError] = useState<string | null>(null);
  const [finalizeSuccess, setFinalizeSuccess] = useState(false);

  useEffect(() => {
    let active = true;
    async function loadPreview() {
      setLoading(true);
      setPreview(null);
      setLoadError(null);
      setFinalizeError(null);
      setFinalizeSuccess(false);
      try {
        const data = await getFinalizationPreview(periodId);
        if (active) setPreview(data);
      } catch (error: unknown) {
        if (!active) return;
        const status = (error as { response?: { status?: number } })?.response?.status;
        setLoadError(
          status === 403
            ? 'Access denied. You do not have permission to view finalization data.'
            : previewError(error, 'Failed to load the approved payroll packet.'),
        );
      } finally {
        if (active) setLoading(false);
      }
    }
    void loadPreview();
    return () => { active = false; };
  }, [periodId]);

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
      setShowConfirm(false);
      if (status === 403) {
        setFinalizeError('Access denied. You do not have permission to finalize this period.');
      } else if (status === 409 || status === 422) {
        setPreview(null);
        setLoadError(previewError(err, 'This payroll is no longer available for finalization. The payroll hub has been refreshed.'));
        onStateConflict();
      } else {
        setFinalizeError(previewError(err, 'Finalization failed. The period was not locked.'));
      }
    } finally {
      setFinalizing(false);
    }
  }

  function handleViewFinalized() {
    onClose();
    navigate(finalizedLedgerPath(periodId));
  }

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
              Approved Payroll Finalization
              {periodName ? <span className={styles.dialogSubtitle}> - {periodName}</span> : null}
            </div>
            {preview && (
              <div className={styles.headerMeta}>
                <span>{preview.branch_name ?? ''}</span>
                {preview.branch_name && <span className={styles.metaSep}>·</span>}
                <span className={styles.statusPill}>{preview.period_status}</span>
                <span className={styles.metaSep}>·</span>
                <span>Approved immutable payroll packet</span>
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
                {preview.final_line_count_estimate} approved financial lines were written to locked history.
              </p>
              <div className={styles.successActions}>
                <button className={styles.doneBtn} onClick={onClose}>Close</button>
                <button className={styles.viewFinalizedBtn} onClick={handleViewFinalized}>
                  View Finalized Payroll
                </button>
              </div>
            </div>
          ) : (
            <>
              {/* ── KPIs ─────────────────────────────────────────── */}
              <div className={styles.kpiRow}>
                <KpiCard label="Drivers Paid" value={String(preview.driver_count)} />
                <KpiCard label="Approved Total" value={fmt(preview.total_final_gross)} />
                <KpiCard label="Bonus Events" value={String(preview.bonus_event_count)} />
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
                badge={preview.final_line_count_estimate}
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
