import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { getFinalizedOffDrivers, getFinalizedOverview, getFinalizedReport } from '../../lib/payrollApi';
import type {
  FinalizedAvailabilityState,
  FinalizedCalculationReportResponse,
  FinalizedOffDriversResponse,
  FinalizedOverviewResponse,
  FinalizedReportView,
  FinalizedSectionAvailability,
  ReportDriver,
} from '../../types/payroll';
import styles from './FinalizedPayrollLibraryDialog.module.css';

const REPORT_TABS: readonly { view: FinalizedReportView; label: string }[] = [
  { view: 'drivers', label: 'Drivers' },
  { view: 'period-work', label: 'Period Work' },
  { view: 'period-pay', label: 'Period Pay' },
  { view: 'mixed', label: 'Mixed' },
];

type FinalizedLibraryTab = 'overview' | FinalizedReportView | 'off-status';

const AVAILABILITY_LABELS: Record<FinalizedAvailabilityState, string> = {
  AVAILABLE: 'Available',
  EMPTY: 'Empty',
  PARTIAL: 'Partial',
  UNAVAILABLE: 'Unavailable',
};

function errorDetail(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return fallback;
}

function formatMoney(value: unknown): string {
  if (value == null) return 'Unavailable';
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(numeric)
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

function formatHash(value: string | null): string {
  if (!value) return 'Not available';
  return value.length > 18 ? `${value.slice(0, 18)}…` : value;
}

function availabilityMessage(
  availability: FinalizedSectionAvailability | undefined,
  emptyText: string,
): string {
  if (!availability || availability.state === 'UNAVAILABLE') {
    return availability?.reason_code
      ? `Unavailable: ${availability.reason_code}`
      : 'Unavailable from finalized history.';
  }
  if (availability.state === 'EMPTY') return emptyText;
  if (availability.state === 'PARTIAL') {
    return availability.reason_code
      ? `Partially available: ${availability.reason_code}`
      : 'Partially available from finalized history.';
  }
  return emptyText;
}

function AvailabilityBadge({ availability }: { availability: FinalizedSectionAvailability | undefined }) {
  const state = availability?.state ?? 'UNAVAILABLE';
  return (
    <span className={`${styles.availability} ${styles[state.toLowerCase()]}`}>
      {AVAILABILITY_LABELS[state]}
    </span>
  );
}

function StateMessage({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <p className={error ? styles.error : styles.state}>{children}</p>;
}

function Dictionary({
  title,
  values,
  money = false,
}: {
  title: string;
  values: Record<string, string> | null;
  money?: boolean;
}) {
  if (values == null) {
    return <div className={styles.dictionary}><h4>{title}</h4><StateMessage>Unavailable from finalized history.</StateMessage></div>;
  }
  const entries = Object.entries(values);
  return (
    <div className={styles.dictionary}>
      <h4>{title}</h4>
      {entries.length === 0 ? <StateMessage>No values are available.</StateMessage> : (
        <dl>
          {entries.map(([key, value]) => (
            <div key={key}><dt>{key}</dt><dd>{money ? formatMoney(value) : formatValue(value)}</dd></div>
          ))}
        </dl>
      )}
    </div>
  );
}

const WORK_COLUMNS = [
  { key: 'work_date', label: 'Date' },
  { key: 'line_type', label: 'Work item' },
  { key: 'pay_item_id', label: 'Pay item' },
  { key: 'quantity', label: 'Quantity' },
  { key: 'line_scope', label: 'Scope' },
] as const;

const STATUS_COLUMNS = [
  { key: 'work_date', label: 'Date' },
  { key: 'code', label: 'Code' },
  { key: 'label', label: 'Status' },
  { key: 'is_off', label: 'Off' },
] as const;

const FINANCIAL_COLUMNS = [
  { key: 'source_type', label: 'Source' },
  { key: 'line_type', label: 'Line' },
  { key: 'work_date', label: 'Date' },
  { key: 'quantity', label: 'Quantity' },
  { key: 'resolved_rate_amount', label: 'Rate' },
  { key: 'calculated_amount', label: 'Amount' },
] as const;

const BONUS_COLUMNS = [
  { key: 'amount', label: 'Amount' },
  { key: 'reason', label: 'Reason' },
  { key: 'notes', label: 'Notes' },
  { key: 'created_at_utc', label: 'Created' },
] as const;

function RecordTable({
  rows,
  columns,
  emptyText,
  moneyKeys = [],
}: {
  rows: Record<string, unknown>[];
  columns: readonly { key: string; label: string }[];
  emptyText: string;
  moneyKeys?: readonly string[];
}) {
  if (rows.length === 0) return <StateMessage>{emptyText}</StateMessage>;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr>{columns.map((column) => <th key={column.key}>{column.label}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {columns.map((column) => {
                const value = row[column.key];
                const isMoney = moneyKeys.includes(column.key);
                return <td key={column.key} className={isMoney ? styles.numeric : undefined}>{isMoney ? formatMoney(value) : formatValue(value)}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DriverReport({
  driver,
  showWork,
  showPay,
  evidenceMessage,
}: {
  driver: ReportDriver;
  showWork: boolean;
  showPay: boolean;
  evidenceMessage: string;
}) {
  return (
    <article className={styles.driverCard}>
      <div className={styles.driverHeading}>
        <div><strong>{driver.driver_name ?? `Driver #${driver.driver_id}`}</strong><span>{driver.driver_code ?? `ID ${driver.driver_id}`}</span></div>
        <span className={styles.driverId}>Driver ID {driver.driver_id}</span>
      </div>
      {showWork && (
        <section className={styles.subsection}>
          <h4>Work</h4>
          <h5>Daily rows</h5>
          <RecordTable rows={driver.work.daily_rows} columns={WORK_COLUMNS} emptyText="No work rows are available." />
          <h5>Status evidence</h5>
          <RecordTable rows={driver.work.status_entries} columns={STATUS_COLUMNS} emptyText={evidenceMessage} />
        </section>
      )}
      {showPay && (
        <section className={styles.subsection}>
          <h4>Pay</h4>
          {driver.pay == null ? <StateMessage>Financial details are unavailable from finalized history.</StateMessage> : (
            <>
              <dl className={styles.payGrid}>
                {([
                  ['Daily pay', driver.pay.daily_pay],
                  ['Status pay', driver.pay.status_pay],
                  ['Period pay', driver.pay.period_pay],
                  ['Minimum adjustment', driver.pay.minimum_adjustment],
                  ['Maximum adjustment', driver.pay.maximum_adjustment],
                  ['Bonus total', driver.pay.bonus_total],
                  ['Total pay', driver.pay.total_pay],
                ] as const).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{formatMoney(value)}</dd></div>)}
              </dl>
              <h5>Financial lines</h5>
              <RecordTable rows={driver.pay.financial_lines} columns={FINANCIAL_COLUMNS} moneyKeys={['resolved_rate_amount', 'calculated_amount']} emptyText="No financial lines are available." />
            </>
          )}
        </section>
      )}
      {showPay && (
        <section className={styles.subsection}>
          <h4>Bonus evidence</h4>
          <RecordTable rows={driver.bonus_events} columns={BONUS_COLUMNS} moneyKeys={['amount']} emptyText={evidenceMessage} />
        </section>
      )}
    </article>
  );
}

function ReportBody({ report, view }: { report: FinalizedCalculationReportResponse; view: FinalizedReportView }) {
  const showWork = view === 'drivers' || view === 'period-work' || view === 'mixed';
  const showPay = view === 'drivers' || view === 'period-pay' || view === 'mixed';
  const evidenceMessage = availabilityMessage(
    report.metadata.section_availability.report_evidence,
    'No immutable Status or Bonus evidence was captured.',
  );
  return (
    <>
      <div className={styles.metadataGrid}>
        <div><span>Period</span><strong>{report.metadata.period_name || report.metadata.period_code}</strong><small>{report.metadata.period_code}</small></div>
        <div><span>Status</span><strong>{report.metadata.period_status}</strong><small>Branch {report.metadata.branch_id}</small></div>
        <div><span>Authority</span><strong>{report.metadata.authority_kind}</strong><small>Finalized history</small></div>
        <div><span>Revision</span><strong>{report.metadata.revision_number ?? 'Not provided'}</strong><small>{report.metadata.snapshot_id == null ? 'No snapshot' : `Snapshot ${report.metadata.snapshot_id}`}</small></div>
        <div><span>Evidence</span><strong><AvailabilityBadge availability={report.metadata.section_availability.report_evidence} /></strong><small>{report.metadata.report_evidence_version == null ? 'Version not provided' : `Version ${report.metadata.report_evidence_version}`}</small></div>
      </div>
      <div className={styles.availabilityRow}>
        {Object.entries(report.metadata.section_availability).map(([key, availability]) => (
          <span key={key}><span className={styles.availabilityLabel}>{key.replaceAll('_', ' ')}</span><AvailabilityBadge availability={availability} /></span>
        ))}
      </div>
      <section className={styles.totals}>
        {showWork && <Dictionary title="Work totals" values={report.work_totals} />}
        {showPay && <Dictionary title="Pay totals" values={report.pay_totals} money />}
      </section>
      {report.columns.length > 0 && (
        <section className={styles.columns}>
          <h3>Report columns</h3>
          <div>{report.columns.map((column) => <span key={column.pay_item_id}>{column.label} <small>({column.code})</small></span>)}</div>
        </section>
      )}
      <section className={styles.section}>
        <h3>Drivers</h3>
        {report.drivers.length === 0 ? <StateMessage>No driver records are available.</StateMessage> : (
          <div className={styles.driverList}>
            {report.drivers.map((driver) => <DriverReport key={driver.driver_id} driver={driver} showWork={showWork} showPay={showPay} evidenceMessage={evidenceMessage} />)}
          </div>
        )}
      </section>
    </>
  );
}

function OffDriverTable({ response }: { response: FinalizedOffDriversResponse }) {
  if (response.fully_off_drivers.length === 0) return null;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Driver</th><th>Code</th><th>Eligible days</th><th>Off days</th></tr></thead>
        <tbody>
          {response.fully_off_drivers.map((driver) => (
            <tr key={driver.driver_id}>
              <td>{driver.driver_name || `Driver #${driver.driver_id}`}</td>
              <td>{driver.driver_code ?? 'Not provided'}</td>
              <td className={styles.numeric}>{driver.eligible_scheduled_day_count}</td>
              <td className={styles.numeric}>{driver.off_day_count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatusEvidenceTable({ response }: { response: FinalizedOffDriversResponse }) {
  if (response.status_entries.length === 0) return null;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Date</th><th>Driver</th><th>Code</th><th>Status code</th><th>Status</th><th>Off reason</th></tr></thead>
        <tbody>
          {response.status_entries.map((entry, index) => (
            <tr key={`${entry.driver_id}-${entry.work_date}-${entry.status_key_id}-${index}`}>
              <td>{entry.work_date}</td>
              <td>{entry.driver_name ?? `Driver #${entry.driver_id}`}</td>
              <td>{entry.driver_code ?? 'Not provided'}</td>
              <td>{entry.status_code}</td>
              <td>{entry.status_label}</td>
              <td>{entry.is_off_reason ? 'Yes' : 'No'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EvidenceSectionMessage({
  availability,
  emptyText,
}: {
  availability: FinalizedSectionAvailability | undefined;
  emptyText: string;
}) {
  if (!availability || availability.state === 'UNAVAILABLE') {
    return <StateMessage>{availabilityMessage(availability, emptyText)}</StateMessage>;
  }
  if (availability.state === 'EMPTY') return <StateMessage>{emptyText}</StateMessage>;
  if (availability.state === 'PARTIAL') {
    return <StateMessage>{availabilityMessage(availability, emptyText)}</StateMessage>;
  }
  return null;
}

function OffStatusBody({ response }: { response: FinalizedOffDriversResponse }) {
  const { metadata } = response;
  const offAvailability = metadata.section_availability.off_drivers;
  const statusAvailability = metadata.section_availability.status_evidence;
  const availabilityEntries = Object.entries(metadata.section_availability);
  const offUnavailable = !offAvailability || offAvailability.state === 'UNAVAILABLE';
  const statusUnavailable = !statusAvailability || statusAvailability.state === 'UNAVAILABLE';
  return (
    <>
      <div className={styles.metadataGrid}>
        <div><span>Period</span><strong>{metadata.period_name || metadata.period_code}</strong><small>{metadata.period_code}</small></div>
        <div><span>Status</span><strong>{metadata.period_status}</strong><small>Branch {metadata.branch_id}</small></div>
        <div><span>Authority</span><strong>{metadata.authority_kind}</strong><small>Frozen Status evidence</small></div>
        <div><span>Revision</span><strong>{metadata.revision_number ?? 'Not provided'}</strong><small>{metadata.snapshot_id == null ? 'No snapshot' : `Snapshot ${metadata.snapshot_id}`}</small></div>
        <div><span>Evidence</span><strong><AvailabilityBadge availability={statusAvailability} /></strong><small>{metadata.report_evidence_version == null ? 'Version not provided' : `Version ${metadata.report_evidence_version}`}</small></div>
      </div>
      <div className={styles.availabilityRow}>
        {availabilityEntries.map(([key, availability]) => (
          <span key={key}><span className={styles.availabilityLabel}>{key.replaceAll('_', ' ')}</span><AvailabilityBadge availability={availability} /></span>
        ))}
      </div>
      <section className={styles.section}>
        <div className={styles.sectionTitleRow}>
          <h3>Fully Off Drivers</h3>
          <AvailabilityBadge availability={offAvailability} />
        </div>
        {offUnavailable ? <EvidenceSectionMessage availability={offAvailability} emptyText="No fully off drivers were captured." /> : (
          <>
            {offAvailability?.state === 'PARTIAL' && <EvidenceSectionMessage availability={offAvailability} emptyText="No fully off drivers were captured." />}
            <div className={styles.offSummaryGrid}>
              <div><strong>{response.total_fully_off_drivers}</strong><span>Fully off drivers</span></div>
              <div><strong>{metadata.period_code}</strong><span>Finalized period</span></div>
            </div>
            {offAvailability?.state === 'EMPTY' && <EvidenceSectionMessage availability={offAvailability} emptyText="No fully off drivers were captured." />}
            {response.fully_off_drivers.length === 0 && offAvailability?.state === 'AVAILABLE' && <StateMessage>No fully off drivers were captured.</StateMessage>}
            <OffDriverTable response={response} />
          </>
        )}
      </section>
      <section className={styles.section}>
        <div className={styles.sectionTitleRow}>
          <h3>Status Evidence</h3>
          <AvailabilityBadge availability={statusAvailability} />
        </div>
        {statusUnavailable ? <EvidenceSectionMessage availability={statusAvailability} emptyText="No Status entries were captured." /> : (
          <>
            {statusAvailability?.state === 'PARTIAL' && <EvidenceSectionMessage availability={statusAvailability} emptyText="No Status entries were captured." />}
            {response.status_entries.length === 0 && statusAvailability?.state === 'AVAILABLE' && <StateMessage>No Status entries were captured.</StateMessage>}
            {statusAvailability?.state === 'EMPTY' && <EvidenceSectionMessage availability={statusAvailability} emptyText="No Status entries were captured." />}
            <StatusEvidenceTable response={response} />
          </>
        )}
      </section>
    </>
  );
}

function OverviewBody({ overview }: { overview: FinalizedOverviewResponse }) {
  const summary = overview.financial_summary;
  const availabilityEntries = Object.entries(overview.section_availability);
  return (
    <div>
      <div className={styles.identityHeader}>
        <div><span className={styles.eyebrow}>Finalized Payroll Library</span><h3>{overview.period_name || overview.period_code}</h3><p>{overview.branch_name} · {overview.period_code}</p></div>
        <span className={styles.finalizedPill}>{overview.period_status}</span>
      </div>
      <div className={styles.kpiGrid}>
        <div><strong>{formatMoney(summary.total_pay)}</strong><span>Total pay</span></div>
        <div><strong>{summary.driver_count}</strong><span>Drivers</span></div>
        <div><strong>{summary.final_line_count}</strong><span>Final lines</span></div>
        <div><strong>{overview.finalized_at_utc ? new Date(overview.finalized_at_utc).toLocaleString() : 'Not provided'}</strong><span>Finalized</span></div>
      </div>
      <section className={styles.overviewSection}>
        <div className={styles.sectionTitleRow}><h3>Immutable provenance</h3><AvailabilityBadge availability={overview.section_availability.snapshot_provenance} /></div>
        <div className={styles.provenanceGrid}>
          <div><span>Snapshot</span><strong>{overview.snapshot_provenance.snapshot_id ?? 'Not available'}</strong></div>
          <div><span>Revision</span><strong>{overview.snapshot_provenance.revision_number ?? 'Not available'}</strong></div>
          <div><span>Snapshot hash</span><strong title={overview.snapshot_provenance.snapshot_hash ?? undefined}>{formatHash(overview.snapshot_provenance.snapshot_hash)}</strong></div>
          <div><span>Source config hash</span><strong title={overview.snapshot_provenance.source_config_hash ?? undefined}>{formatHash(overview.snapshot_provenance.source_config_hash)}</strong></div>
          <div><span>Finalized by</span><strong>{overview.finalized_by_user_id ?? 'Not provided'}</strong></div>
        </div>
      </section>
      <section className={styles.overviewSection}>
        <div className={styles.sectionTitleRow}><h3>Library sections</h3><span className={styles.muted}>Loaded when selected</span></div>
        <div className={styles.sectionList}>
          {availabilityEntries.map(([key, availability]) => (
            <div key={key}><span>{key.replaceAll('_', ' ')}</span><AvailabilityBadge availability={availability} />{availability.reason_code && <small>{availability.reason_code}</small>}</div>
          ))}
        </div>
      </section>
    </div>
  );
}

export interface FinalizedPayrollLibraryPeriodContext {
  payroll_period_id: number;
  period_name: string;
  period_code: string;
  branch_name: string;
  status: string;
}

interface FinalizedPayrollLibraryDialogProps {
  period: FinalizedPayrollLibraryPeriodContext;
  onClose: () => void;
}

export function FinalizedPayrollLibraryDialog({ period, onClose }: FinalizedPayrollLibraryDialogProps) {
  const [activeTab, setActiveTab] = useState<FinalizedLibraryTab>('overview');
  const [overview, setOverview] = useState<FinalizedOverviewResponse | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [report, setReport] = useState<FinalizedCalculationReportResponse | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const [overviewRetry, setOverviewRetry] = useState(0);
  const [reportRetry, setReportRetry] = useState(0);
  const [offStatus, setOffStatus] = useState<FinalizedOffDriversResponse | null>(null);
  const [offStatusLoading, setOffStatusLoading] = useState(false);
  const [offStatusError, setOffStatusError] = useState<string | null>(null);
  const [offStatusRetry, setOffStatusRetry] = useState(0);
  const requestRef = useRef(0);

  useEffect(() => {
    let active = true;
    void getFinalizedOverview(period.payroll_period_id)
      .then((data) => { if (active) setOverview(data); })
      .catch((error: unknown) => { if (active) setOverviewError(errorDetail(error, 'Failed to load finalized payroll overview.')); })
      .finally(() => { if (active) setOverviewLoading(false); });
    return () => { active = false; };
  }, [period.payroll_period_id, overviewRetry]);

  const loadReport = useCallback(async (view: FinalizedReportView) => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    setReportLoading(true);
    setReport(null);
    setReportError(null);
    try {
      const data = await getFinalizedReport(period.payroll_period_id, view);
      if (requestRef.current === requestId) setReport(data);
    } catch (error: unknown) {
      if (requestRef.current === requestId) setReportError(errorDetail(error, 'Failed to load finalized report.'));
    } finally {
      if (requestRef.current === requestId) setReportLoading(false);
    }
  }, [period.payroll_period_id]);

  const loadOffStatus = useCallback(async () => {
    setOffStatusLoading(true);
    setOffStatusError(null);
    try {
      setOffStatus(await getFinalizedOffDrivers(period.payroll_period_id));
    } catch (error: unknown) {
      setOffStatusError(errorDetail(error, 'Failed to load finalized Off and Status history.'));
    } finally {
      setOffStatusLoading(false);
    }
  }, [period.payroll_period_id]);

  useEffect(() => {
    if (!overview || activeTab !== 'off-status' || offStatus != null) return;
    const timer = window.setTimeout(() => { void loadOffStatus(); }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, loadOffStatus, offStatus, offStatusRetry, overview]);

  useEffect(() => {
    if (!overview || activeTab === 'overview' || activeTab === 'off-status') return;
    const timer = window.setTimeout(() => { void loadReport(activeTab); }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, loadReport, overview, reportRetry]);

  useEffect(() => {
    function handleKey(event: KeyboardEvent) { if (event.key === 'Escape') onClose(); }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  function selectTab(tab: FinalizedLibraryTab) {
    if (tab === activeTab) return;
    setActiveTab(tab);
    setReport(null);
    setReportError(null);
    setReportLoading(tab !== 'overview' && tab !== 'off-status');
    setOffStatusError(null);
    setOffStatusLoading(tab === 'off-status' && offStatus == null);
  }

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div className={styles.dialog} onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-label="Finalized Payroll Library">
        <header className={styles.header}>
          <div><h2>Finalized Payroll Library</h2><p>{period.period_name || period.period_code} · {period.branch_name}</p></div>
          <button className={styles.closeButton} onClick={onClose} type="button" aria-label="Close">&#x2715;</button>
        </header>
        <nav className={styles.tabs} aria-label="Finalized payroll sections" role="tablist">
          <button className={activeTab === 'overview' ? styles.activeTab : styles.tab} onClick={() => selectTab('overview')} type="button" role="tab" aria-selected={activeTab === 'overview'}>Overview</button>
          {REPORT_TABS.map((tab) => <button key={tab.view} className={activeTab === tab.view ? styles.activeTab : styles.tab} onClick={() => selectTab(tab.view)} type="button" role="tab" aria-selected={activeTab === tab.view} disabled={!overview}>{tab.label}</button>)}
          <button className={activeTab === 'off-status' ? styles.activeTab : styles.tab} onClick={() => selectTab('off-status')} type="button" role="tab" aria-selected={activeTab === 'off-status'} disabled={!overview}>Off / Status</button>
        </nav>
        <div className={styles.body}>
          {overviewLoading ? <StateMessage>Loading finalized payroll overview…</StateMessage> : overviewError ? (
            <div className={styles.errorBox}><p>{overviewError}</p><button className={styles.retryButton} onClick={() => { setOverviewLoading(true); setOverviewError(null); setOverview(null); setActiveTab('overview'); setOverviewRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : overview == null ? <StateMessage>Finalized overview is unavailable.</StateMessage> : activeTab === 'overview' ? <OverviewBody overview={overview} /> : activeTab === 'off-status' ? offStatusLoading ? (
            <StateMessage>Loading finalized Off and Status history…</StateMessage>
          ) : offStatusError ? (
            <div className={styles.errorBox}><p>{offStatusError}</p><button className={styles.retryButton} onClick={() => { setOffStatusLoading(true); setOffStatusError(null); setOffStatus(null); setOffStatusRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : offStatus == null ? <StateMessage>Finalized Off and Status history is unavailable.</StateMessage> : <OffStatusBody response={offStatus} /> : reportLoading ? (
            <StateMessage>Loading {REPORT_TABS.find((tab) => tab.view === activeTab)?.label.toLowerCase() ?? 'report'}…</StateMessage>
          ) : reportError ? (
            <div className={styles.errorBox}><p>{reportError}</p><button className={styles.retryButton} onClick={() => { setReportLoading(true); setReportError(null); setReportRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : report == null ? <StateMessage>No finalized report data is available.</StateMessage> : <ReportBody report={report} view={activeTab} />}
        </div>
      </div>
    </div>
  );
}
