import { useCallback, useEffect, useRef, useState } from 'react';
import { getCalculationReport } from '../../lib/payrollApi';
import type {
  CalculationReportResponse,
  CalculationReportView,
  ReportDriver,
} from '../../types/payroll';
import { buildPeriodPayTable } from './periodPayTable';
import { PeriodPayMatrix } from './PeriodPayMatrix';
import styles from './CurrentPayrollReportsDialog.module.css';

const REPORT_TABS: readonly { view: CalculationReportView; label: string }[] = [
  { view: 'drivers', label: 'Drivers' },
  { view: 'period-work', label: 'Period Work' },
  { view: 'period-pay', label: 'Period Pay' },
  { view: 'mixed', label: 'Mixed' },
];

function errorDetail(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return fallback;
}

function errorStatus(error: unknown): number | undefined {
  return (error as { response?: { status?: number } })?.response?.status;
}

function formatMoney(value: string | null | undefined): string {
  if (value == null) return 'Unavailable';
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(numeric)
    : value;
}

function formatQuantity(value: unknown): string {
  if (value == null) return '—';
  if (typeof value !== 'string' && typeof value !== 'number') return formatValue(value);
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? numeric.toLocaleString(undefined, { maximumFractionDigits: 4 })
    : String(value);
}

function formatValue(value: unknown): string {
  if (value == null) return '—';
  if (typeof value === 'string') return value;
  if (typeof value === 'number') return value.toLocaleString();
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function rowValue(row: Record<string, unknown>, key: string): unknown {
  return row[key];
}

function EmptyReport({ children }: { children: string }) {
  return <p className={styles.empty}>{children}</p>;
}

function RecordTable({
  rows,
  columns,
  emptyMessage = 'No entries are currently available.',
}: {
  rows: Record<string, unknown>[];
  columns: readonly { key: string; label: string; quantity?: boolean; money?: boolean }[];
  emptyMessage?: string;
}) {
  if (rows.length === 0) return <EmptyReport>{emptyMessage}</EmptyReport>;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>{columns.map((column) => <th key={column.key}>{column.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column.key} className={column.quantity ? styles.numeric : undefined}>
                  {column.money
                    ? formatMoney(rowValue(row, column.key) as string | null | undefined)
                    : column.quantity
                      ? formatQuantity(rowValue(row, column.key))
                      : formatValue(rowValue(row, column.key))}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const WORK_COLUMNS = [
  { key: 'work_date', label: 'Date' },
  { key: 'line_type', label: 'Work item' },
  { key: 'pay_item_id', label: 'Pay item' },
  { key: 'quantity', label: 'Quantity', quantity: true },
  { key: 'line_scope', label: 'Scope' },
] as const;

const STATUS_ENTRY_COLUMNS = [
  { key: 'work_date', label: 'Date' },
  { key: 'code', label: 'Code' },
  { key: 'label', label: 'Status' },
  { key: 'is_off', label: 'Off' },
] as const;

const STATUS_SUMMARY_COLUMNS = [
  { key: 'code', label: 'Code' },
  { key: 'label', label: 'Status' },
  { key: 'count', label: 'Count', quantity: true },
  { key: 'is_off', label: 'Off' },
] as const;

const FINANCIAL_LINE_COLUMNS = [
  { key: 'source_type', label: 'Source' },
  { key: 'line_type', label: 'Line' },
  { key: 'work_date', label: 'Date' },
  { key: 'quantity', label: 'Quantity', quantity: true },
  { key: 'resolved_rate_amount', label: 'Rate', money: true },
  { key: 'calculated_amount', label: 'Amount', money: true },
] as const;

const BONUS_COLUMNS = [
  { key: 'amount', label: 'Amount', money: true },
  { key: 'reason', label: 'Reason' },
  { key: 'notes', label: 'Notes' },
  { key: 'created_at_utc', label: 'Created' },
] as const;

function NoticeList({ title, values, tone }: { title: string; values: string[]; tone: 'blocker' | 'warning' }) {
  if (values.length === 0) return null;
  return (
    <section className={tone === 'blocker' ? styles.blockers : styles.warnings}>
      <strong>{title}</strong>
      <ul>{values.map((value, index) => <li key={`${value}-${index}`}>{value}</li>)}</ul>
    </section>
  );
}

function Dictionary({
  title,
  values,
  money,
}: {
  title: string;
  values: Record<string, string> | null;
  money: boolean;
}) {
  if (values == null) return <div className={styles.dictionary}><h4>{title}</h4><EmptyReport>Unavailable from the backend.</EmptyReport></div>;
  const entries = Object.entries(values);
  return (
    <div className={styles.dictionary}>
      <h4>{title}</h4>
      {entries.length === 0 ? <EmptyReport>No values are currently available.</EmptyReport> : (
        <dl>
          {entries.map(([key, value]) => (
          <div key={key}>
              <dt>{key}</dt>
              <dd>{money ? formatMoney(value) : value}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

function WorkSection({ driver, evidenceAvailable }: { driver: ReportDriver; evidenceAvailable: boolean }) {
  const evidenceMessage = evidenceAvailable
    ? 'No Status entries were captured.'
    : 'Status history is unavailable from this report authority.';
  return (
    <section className={styles.subsection}>
      <h4>Work</h4>
      <h5>Daily rows</h5>
      <RecordTable rows={driver.work.daily_rows} columns={WORK_COLUMNS} />
      <h5>Status summaries</h5>
      <RecordTable rows={driver.work.status_summaries} columns={STATUS_SUMMARY_COLUMNS} emptyMessage={evidenceMessage} />
      <h5>Status entries</h5>
      <RecordTable rows={driver.work.status_entries} columns={STATUS_ENTRY_COLUMNS} emptyMessage={evidenceMessage} />
    </section>
  );
}

function PaySection({ driver }: { driver: ReportDriver }) {
  if (driver.pay == null) {
    return <section className={styles.subsection}><h4>Pay</h4><EmptyReport>Financial details are unavailable from the backend.</EmptyReport></section>;
  }
  const pay = driver.pay;
  const fields: readonly [string, string][] = [
    ['Daily pay', pay.daily_pay],
    ['Status pay', pay.status_pay],
    ['Period pay', pay.period_pay],
    ['Minimum adjustment', pay.minimum_adjustment],
    ['Maximum adjustment', pay.maximum_adjustment],
    ['Bonus total', pay.bonus_total],
    ['Total pay', pay.total_pay],
  ];
  return (
    <section className={styles.subsection}>
      <h4>Pay</h4>
      <dl className={styles.payGrid}>
        {fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{formatMoney(value)}</dd></div>)}
      </dl>
      <h5>Financial lines</h5>
      <RecordTable rows={pay.financial_lines} columns={FINANCIAL_LINE_COLUMNS} />
    </section>
  );
}

function BonusSection({ driver, evidenceAvailable }: { driver: ReportDriver; evidenceAvailable: boolean }) {
  return (
    <section className={styles.subsection}>
      <h4>Bonus events</h4>
      <RecordTable
        rows={driver.bonus_events}
        columns={BONUS_COLUMNS}
        emptyMessage={evidenceAvailable ? 'No Bonus events were captured.' : 'Bonus history is unavailable from this report authority.'}
      />
    </section>
  );
}

function DriverCard({
  driver,
  showWork,
  showPay,
  evidenceAvailable,
}: {
  driver: ReportDriver;
  showWork: boolean;
  showPay: boolean;
  evidenceAvailable: boolean;
}) {
  return (
    <article className={styles.driverCard}>
      <div className={styles.driverHeading}>
        <div>
          <strong>{driver.driver_name ?? `Driver #${driver.driver_id}`}</strong>
          <span>{driver.driver_code ?? `ID ${driver.driver_id}`}</span>
        </div>
        <span className={styles.driverId}>Driver ID {driver.driver_id}</span>
      </div>
      {showWork && <WorkSection driver={driver} evidenceAvailable={evidenceAvailable} />}
      {showPay && <PaySection driver={driver} />}
      {showPay && <BonusSection driver={driver} evidenceAvailable={evidenceAvailable} />}
    </article>
  );
}

function periodPayCell(value: string | null): string {
  return value == null ? '—' : formatMoney(value);
}

function ReportBody({ report, view }: { report: CalculationReportResponse; view: CalculationReportView }) {
  const showWork = view === 'drivers' || view === 'period-work' || view === 'mixed';
  const showPay = view === 'drivers' || view === 'period-pay' || view === 'mixed';
  return (
    <>
      <div className={styles.metadataGrid}>
        <div><span>Period</span><strong>{report.metadata.period_name || report.metadata.period_code}</strong><small>{report.metadata.period_code}</small></div>
        <div><span>Status</span><strong>{report.metadata.period_status}</strong><small>Branch {report.metadata.branch_id}</small></div>
        <div><span>Authority</span><strong>{report.metadata.authority_kind}</strong><small>{report.metadata.financials_available ? 'Financials available' : 'Financials unavailable'}</small></div>
        <div><span>Revision</span><strong>{report.metadata.revision_number == null ? 'Not provided' : report.metadata.revision_number}</strong><small>{report.metadata.snapshot_id == null ? 'No snapshot' : `Snapshot ${report.metadata.snapshot_id}`}</small></div>
        <div><span>Report evidence</span><strong>{report.metadata.report_evidence_available ? 'Available' : 'Unavailable'}</strong><small>{report.metadata.report_evidence_version == null ? 'Version not provided' : `Version ${report.metadata.report_evidence_version}`}</small></div>
      </div>
      {!report.metadata.financials_available && report.metadata.unavailable_reason && (
        <div className={styles.info}>Financial details: {report.metadata.unavailable_reason}</div>
      )}
      <NoticeList title="Blockers" values={report.metadata.blockers} tone="blocker" />
      <NoticeList title="Warnings" values={report.metadata.warnings} tone="warning" />

      {view === 'period-pay' ? (
        <section className={styles.section}>
          <h3>Period Pay</h3>
          <PeriodPayMatrix
            table={buildPeriodPayTable(report.pay_item_columns, report.drivers, report.pay_item_totals, report.pay_totals)}
            formatCell={periodPayCell}
            emptyState={<EmptyReport>No driver records are currently available.</EmptyReport>}
          />
        </section>
      ) : (
        <>
          <section className={styles.totals}>
            <Dictionary title="Work totals" values={report.work_totals} money={false} />
            {(view === 'drivers' || view === 'mixed') && <Dictionary title="Pay totals" values={report.pay_totals} money />}
          </section>

          {report.columns.length > 0 && (
            <div className={styles.columns} aria-label="Report columns">
              <span>Report columns</span>
              {report.columns.map((column) => <span key={column.pay_item_id}>{column.label} <small>({column.code})</small></span>)}
            </div>
          )}

          <section className={styles.section}>
            <h3>Drivers</h3>
            {report.drivers.length === 0 ? <EmptyReport>No driver records are currently available.</EmptyReport> : (
              <div className={styles.driverList}>
                {report.drivers.map((driver) => (
                  <DriverCard
                    key={driver.driver_id}
                    driver={driver}
                    showWork={showWork}
                    showPay={showPay}
                    evidenceAvailable={report.metadata.report_evidence_available}
                  />
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </>
  );
}

interface CurrentPayrollReportsDialogProps {
  periodId: number;
  periodName?: string;
  onClose: () => void;
}

export function CurrentPayrollReportsDialog({ periodId, periodName, onClose }: CurrentPayrollReportsDialogProps) {
  const [view, setView] = useState<CalculationReportView>('drivers');
  const [report, setReport] = useState<CalculationReportResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const requestRef = useRef(0);

  const loadReport = useCallback(async () => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    setLoading(true);
    setReport(null);
    setLoadError(null);
    try {
      const data = await getCalculationReport(periodId, view);
      if (requestRef.current === requestId) setReport(data);
    } catch (error: unknown) {
      if (requestRef.current !== requestId) return;
      const status = errorStatus(error);
      setLoadError(errorDetail(
        error,
        status === 422
          ? 'This report is unavailable for the period’s current lifecycle state.'
        : 'Failed to load the payroll report.',
      ));
    } finally {
      if (requestRef.current === requestId) setLoading(false);
    }
  }, [periodId, view]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadReport(); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadReport, retry]);

  useEffect(() => {
    function handleKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  function selectView(nextView: CalculationReportView) {
    if (nextView === view) return;
    setView(nextView);
    setReport(null);
    setLoadError(null);
    setLoading(true);
  }

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div className={styles.dialog} onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-label="Current Payroll Reports">
        <header className={styles.header}>
          <div>
            <h2>Current Payroll Reports</h2>
            <p>{periodName ?? 'Current payroll'}</p>
          </div>
          <button className={styles.closeButton} onClick={onClose} aria-label="Close">&#x2715;</button>
        </header>
        <nav className={styles.tabs} aria-label="Report views" role="tablist">
          {REPORT_TABS.map((tab) => (
            <button
              key={tab.view}
              className={tab.view === view ? styles.activeTab : styles.tab}
              onClick={() => selectView(tab.view)}
              role="tab"
              aria-selected={tab.view === view}
            >
              {tab.label}
            </button>
          ))}
        </nav>
        <div className={styles.body}>
          {loading ? <p className={styles.state}>Loading {REPORT_TABS.find((tab) => tab.view === view)?.label.toLowerCase()} report…</p> : loadError ? (
            <div className={styles.error}>
              <p>{loadError}</p>
              <button className={styles.retryButton} onClick={() => setRetry((value) => value + 1)}>Retry</button>
            </div>
          ) : report == null ? <EmptyReport>No report data is currently available.</EmptyReport> : <ReportBody report={report} view={view} />}
        </div>
      </div>
    </div>
  );
}
