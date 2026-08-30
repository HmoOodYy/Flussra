/**
 * FinalSummaryDialog — CP-4
 *
 * Read-only dialog showing PayrollFinalLines for a Locked/Archived period.
 * Opens from the Ledger page.  No edit, no recalculate, no finalize.
 *
 * All monetary values come from the backend (FinalLineSummary.final_amount).
 * Driver grouping is display-only over immutable FinalLines, never drafts,
 * rates, or a payroll formula.
 */
import { useEffect, useState } from 'react';
import { getFinalLines } from '../../lib/payrollApi';
import type { FinalLineSummary, PeriodSummary } from '../../types/payroll';
import styles from './FinalSummaryDialog.module.css';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(v: string | number | null | undefined): string {
  if (v == null) return '-';
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : String(v);
}

function fmtQty(v: string | null | undefined): string {
  if (v == null) return '-';
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 4 }) : String(v);
}

// Display-only grouping of immutable FinalLines. Financial authority remains
// the persisted FinalLine rows and period aggregates returned by the backend.
interface DriverTotal {
  driver_id: number;
  driver_name: string;
  daily_pay: number;
  period_pay: number;
  sys_adjustment: number;
  final_pay: number;
  line_count: number;
}

function buildDriverTotals(lines: FinalLineSummary[]): DriverTotal[] {
  const map = new Map<number, DriverTotal>();
  for (const line of lines) {
    const amt = Number(line.final_amount);
    if (!map.has(line.driver_id)) {
      map.set(line.driver_id, {
        driver_id: line.driver_id,
        driver_name: line.driver_name,
        daily_pay: 0,
        period_pay: 0,
        sys_adjustment: 0,
        final_pay: 0,
        line_count: 0,
      });
    }
    const dt = map.get(line.driver_id)!;
    if (!Number.isFinite(amt)) continue;
    if (line.line_type.startsWith('SYS_')) {
      dt.sys_adjustment += amt;
    } else if (line.line_scope === 'Period') {
      dt.period_pay += amt;
    } else {
      dt.daily_pay += amt;
    }
    dt.final_pay += amt;
    dt.line_count++;
  }
  return Array.from(map.values()).sort((a, b) => a.driver_name.localeCompare(b.driver_name));
}

function lineLabel(line: FinalLineSummary): string {
  if (line.line_type === 'SYS_MIN_TOPUP') return 'Minimum top-up';
  if (line.line_type === 'SYS_MAX_CAP') return 'Maximum cap';
  if (line.source_type === 'StatusEntryState') return 'Status payment';
  if (line.source_type === 'BonusEvent') return 'Bonus event';
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

function DriverTotalsTable({ rows }: { rows: DriverTotal[] }) {
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
            <td className={styles.nameCell}>{r.driver_name}</td>
            <td className={styles.numCol}>{fmt(r.daily_pay)}</td>
            <td className={styles.numCol}>{fmt(r.period_pay)}</td>
            <td className={`${styles.numCol} ${r.sys_adjustment !== 0 ? styles.adjCell : ''}`}>
              {r.sys_adjustment !== 0
                ? `${r.sys_adjustment >= 0 ? '+' : ''}$${Math.abs(r.sys_adjustment).toFixed(2)}`
                : '-'}
            </td>
            <td className={`${styles.numCol} ${styles.finalPayCell}`}>{fmt(r.final_pay)}</td>
            <td className={styles.numCol}>{r.line_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function SysAdjTable({ lines }: { lines: FinalLineSummary[] }) {
  const sysLines = lines.filter((l) => l.line_type.startsWith('SYS_'));
  if (sysLines.length === 0) return <div className={styles.emptyMsg}>No system adjustments.</div>;
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>Driver</th>
          <th>Type</th>
          <th className={styles.numCol}>Final Amount</th>
          <th>Notes</th>
        </tr>
      </thead>
      <tbody>
        {sysLines.map((l) => (
          <tr key={l.final_line_id}>
            <td className={styles.nameCell}>{l.driver_name}</td>
            <td>
              <span className={`${styles.typePill} ${l.line_type === 'SYS_MIN_TOPUP' ? styles.topupPill : styles.capPill}`}>
                {l.line_type === 'SYS_MIN_TOPUP' ? 'Min Top-Up' : 'Max Cap'}
              </span>
            </td>
            <td className={`${styles.numCol} ${styles.adjCell}`}>{fmt(l.final_amount)}</td>
            <td>{l.notes ?? '-'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PeriodPayTable({ lines }: { lines: FinalLineSummary[] }) {
  const periodLines = lines.filter((l) => l.line_scope === 'Period' && !l.line_type.startsWith('SYS_'));
  if (periodLines.length === 0) return <div className={styles.emptyMsg}>No period pay / bonus lines.</div>;
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>Driver</th>
          <th>Type</th>
          <th className={styles.numCol}>Amount</th>
          <th>Notes</th>
        </tr>
      </thead>
      <tbody>
        {periodLines.map((l) => (
          <tr key={l.final_line_id}>
            <td className={styles.nameCell}>{l.driver_name}</td>
            <td>{lineLabel(l)}</td>
            <td className={`${styles.numCol} ${styles.finalPayCell}`}>{fmt(l.final_amount)}</td>
            <td>{l.notes ?? '-'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LineDetailsTable({ lines }: { lines: FinalLineSummary[] }) {
  const dailyLines = lines.filter((l) => l.line_scope === 'Daily' && !l.line_type.startsWith('SYS_'));
  if (dailyLines.length === 0) return <div className={styles.emptyMsg}>No daily lines.</div>;
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th>Driver</th>
          <th>Date</th>
          <th>Type</th>
          <th className={styles.numCol}>Qty</th>
          <th className={styles.numCol}>Rate</th>
          <th className={styles.numCol}>Final</th>
        </tr>
      </thead>
      <tbody>
        {dailyLines.map((l) => (
          <tr key={l.final_line_id}>
            <td className={styles.nameCell}>{l.driver_name}</td>
            <td className={styles.dateCell}>{l.work_date ?? '-'}</td>
            <td>
              {lineLabel(l)}
              {l.rate_behavior && (
                <span style={{ fontSize: '0.7rem', color: '#64748b', marginLeft: 4 }}>
                  ({l.rate_behavior})
                </span>
              )}
            </td>
            <td className={styles.numCol}>{fmtQty(l.quantity)}</td>
            <td className={styles.numCol}>{fmt(l.resolved_rate_amount ?? l.rate_amount)}</td>
            <td className={`${styles.numCol} ${styles.finalPayCell}`}>{fmt(l.final_amount)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// Main dialog
// ---------------------------------------------------------------------------

interface FinalSummaryDialogProps {
  period: PeriodSummary;
  onClose: () => void;
}

export function FinalSummaryDialog({ period, onClose }: FinalSummaryDialogProps) {
  const [lines, setLines] = useState<FinalLineSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    async function loadLines() {
      setLoading(true);
      setLines([]);
      setLoadError(null);
      try {
        const data = await getFinalLines(period.payroll_period_id);
        if (active) setLines(data);
      } catch (err: unknown) {
        if (!active) return;
        const status = (err as { response?: { status?: number } })?.response?.status;
        const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
        setLoadError(
          status === 403
            ? 'Access denied. You do not have permission to view this period\'s final lines.'
            : detail ?? 'Failed to load final lines.',
        );
      } finally {
        if (active) setLoading(false);
      }
    }
    void loadLines();
    return () => { active = false; };
  }, [period.payroll_period_id]);

  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  const driverTotals = buildDriverTotals(lines);
  const sysCount = lines.filter((l) => l.line_type.startsWith('SYS_')).length;
  const periodPayCount = lines.filter((l) => l.line_scope === 'Period' && !l.line_type.startsWith('SYS_')).length;
  const dailyCount = lines.filter((l) => l.line_scope === 'Daily' && !l.line_type.startsWith('SYS_')).length;

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div
        className={styles.dialog}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Final Summary"
      >
        {/* ── Header ─────────────────────────────────────────────────── */}
        <div className={styles.dialogHeader}>
          <div>
            <div className={styles.dialogTitle}>
              Final Summary
              <span className={styles.dialogSubtitle}> — {period.period_name || period.period_code}</span>
            </div>
            <div className={styles.headerMeta}>
              <span>{period.branch_name}</span>
              <span className={styles.metaSep}>·</span>
              <span>{period.start_date} – {period.end_date}</span>
              {period.pay_date && (
                <>
                  <span className={styles.metaSep}>·</span>
                  <span>Pay date: {period.pay_date}</span>
                </>
              )}
              <span className={styles.metaSep}>·</span>
              <span className={styles.statusPill}>{period.status}</span>
            </div>
          </div>
          <button className={styles.closeBtn} onClick={onClose} aria-label="Close">
            &#x2715;
          </button>
        </div>

        {/* ── Body ───────────────────────────────────────────────────── */}
        <div className={styles.dialogBody}>
          {loading ? (
            <div className={styles.stateMsg}>Loading final lines…</div>
          ) : loadError ? (
            <div className={styles.errorBox}>{loadError}</div>
          ) : lines.length === 0 ? (
            <div className={styles.emptyBox}>
              <p>No final lines found for this period.</p>
              <p className={styles.emptyHint}>
                Locked and archived history is displayed from persisted FinalLines.
              </p>
            </div>
          ) : (
            <>
              {/* ── KPIs ─────────────────────────────────────────── */}
              <div className={styles.kpiRow}>
                <KpiCard label="Drivers Paid" value={String(period.final_driver_count)} />
                <KpiCard label="Final Gross" value={fmt(period.final_gross)} />
                <KpiCard label="Total Lines" value={String(period.final_lines)} />
                {sysCount > 0 && (
                  <KpiCard label="Sys Adjustments" value={String(sysCount)} />
                )}
                {periodPayCount > 0 && (
                  <KpiCard label="Period Pay" value={String(periodPayCount)} />
                )}
              </div>

              {/* ── Driver Totals ─────────────────────────────── */}
              <Section title="Driver Totals" badge={driverTotals.length}>
                <DriverTotalsTable rows={driverTotals} />
              </Section>

              {/* ── Period Pay / Bonus ───────────────────────── */}
              <Section
                title="Period Pay / Bonus"
                badge={periodPayCount}
                defaultOpen={periodPayCount > 0}
              >
                <PeriodPayTable lines={lines} />
              </Section>

              {/* ── System Adjustments ───────────────────────── */}
              <Section
                title="System Adjustments"
                badge={sysCount}
                defaultOpen={sysCount > 0}
              >
                <SysAdjTable lines={lines} />
              </Section>

              {/* ── Daily Line Details ───────────────────────── */}
              <Section
                title="Daily Line Details"
                badge={dailyCount}
                defaultOpen={false}
              >
                <LineDetailsTable lines={lines} />
              </Section>
            </>
          )}
        </div>

        {/* ── Footer ─────────────────────────────────────────────────── */}
        <div className={styles.dialogFooter}>
          <span className={styles.readOnlyLabel}>Read-only - FinalLines-backed payroll history</span>
          <button className={styles.closeFooterBtn} onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
