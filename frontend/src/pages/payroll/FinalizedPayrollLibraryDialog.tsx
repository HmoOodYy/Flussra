import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { getFinalizedAudit, getFinalizedOffDrivers, getFinalizedOverview, getFinalizedRatesUsed, getFinalizedReport } from '../../lib/payrollApi';
import type {
  FinalizedAvailabilityState,
  FinalizedAuditEvent,
  FinalizedAuditResponse,
  FinalizedCalculationReportResponse,
  FinalizedOffDriversResponse,
  FinalizedOverviewResponse,
  FinalizedRatesUsedResponse,
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

type FinalizedLibraryTab = 'overview' | FinalizedReportView | 'off-status' | 'rates-used' | 'audit';

function isFinalizedReportView(tab: FinalizedLibraryTab): tab is FinalizedReportView {
  return REPORT_TABS.some((item) => item.view === tab);
}

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

function rateDefinitionLabel(definition: FinalizedRatesUsedResponse['used_rate_definitions'][number]): string {
  if (definition.evidence_kind === 'DriverRate') return 'Driver rate';
  if (definition.evidence_kind === 'DriverPayRule') return 'Driver pay rule';
  return definition.evidence_kind;
}

function RateDefinitionTable({ response }: { response: FinalizedRatesUsedResponse }) {
  if (response.used_rate_definitions.length === 0) return null;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Driver</th><th>Pay item</th><th>Definition</th><th>Rate / behavior</th><th>Rule metadata</th><th>Effective</th><th>Used</th></tr></thead>
        <tbody>
          {response.used_rate_definitions.map((definition) => {
            const driver = definition.driver_name || `Driver #${definition.driver_id}`;
            const payItem = definition.pay_item_label || definition.pay_item_code || (definition.pay_item_id == null ? 'Not provided' : `PayItem #${definition.pay_item_id}`);
            const rateType = definition.rate_type_name || definition.rate_type_code;
            const rateDetails = [rateType, definition.unit_name, definition.rate_behavior, definition.rate_status].filter(Boolean).join(' · ');
            const ruleDetails = [definition.rule_type, definition.rule_amount == null ? null : `Amount ${formatMoney(definition.rule_amount)}`, definition.block_size == null ? null : `Block ${definition.block_size}`, definition.rounding_rule, definition.rule_status].filter(Boolean).join(' · ');
            const effective = definition.effective_from || definition.effective_to
              ? `${definition.effective_from ?? 'Open'} to ${definition.effective_to ?? 'Open'}`
              : 'Not provided';
            return (
              <tr key={definition.used_rate_definition_id}>
                <td>{driver}<small>{definition.driver_code ?? `ID ${definition.driver_id}`}</small></td>
                <td>{payItem}<small>{definition.pay_item_id == null ? 'ID not provided' : `ID ${definition.pay_item_id}`}</small></td>
                <td>{rateDefinitionLabel(definition)}<small>{definition.source_type}</small></td>
                <td>{definition.rate_amount == null ? 'Not provided' : formatMoney(definition.rate_amount)}<small>{rateDetails || 'Details not provided'}</small></td>
                <td>{ruleDetails || 'Not provided'}</td>
                <td>{effective}</td>
                <td className={styles.numeric}>{definition.line_use_count}<small>{definition.snapshot_line_ids.length === 0 ? 'No line references' : `${definition.snapshot_line_ids.length} snapshot line${definition.snapshot_line_ids.length === 1 ? '' : 's'}`}</small></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function BonusEvidenceTable({ response }: { response: FinalizedRatesUsedResponse }) {
  if (response.bonus_events.length === 0) return null;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Driver</th><th>Amount</th><th>Reason</th><th>Notes</th><th>Revision</th><th>Creator</th><th>Created</th></tr></thead>
        <tbody>
          {response.bonus_events.map((event) => (
            <tr key={event.bonus_event_id}>
              <td>{event.driver_name || `Driver #${event.driver_id}`}<small>{event.driver_code ?? `ID ${event.driver_id}`}</small></td>
              <td className={styles.numeric}>{formatMoney(event.amount)}</td>
              <td>{event.reason ?? 'Not provided'}</td>
              <td>{event.notes ?? 'Not provided'}</td>
              <td className={styles.numeric}>{event.data_revision}</td>
              <td>{event.creator_display_name ?? (event.creator_user_id == null ? 'Not provided' : `User #${event.creator_user_id}`)}</td>
              <td>{new Date(event.created_at_utc).toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RatesUsedBody({ response }: { response: FinalizedRatesUsedResponse }) {
  const { metadata } = response;
  const ratesAvailability = metadata.section_availability.rates_rules;
  const bonusAvailability = metadata.section_availability.bonus_evidence;
  const ratesUnavailable = !ratesAvailability || ratesAvailability.state === 'UNAVAILABLE';
  const bonusUnavailable = !bonusAvailability || bonusAvailability.state === 'UNAVAILABLE';
  return (
    <>
      <div className={styles.metadataGrid}>
        <div><span>Period</span><strong>{metadata.period_name || metadata.period_code}</strong><small>{metadata.period_code}</small></div>
        <div><span>Status</span><strong>{metadata.period_status}</strong><small>Branch {metadata.branch_id}</small></div>
        <div><span>Authority</span><strong>{metadata.authority_kind}</strong><small>Immutable used definitions</small></div>
        <div><span>Revision</span><strong>{metadata.revision_number ?? 'Not provided'}</strong><small>{metadata.snapshot_id == null ? 'No snapshot' : `Snapshot ${metadata.snapshot_id}`}</small></div>
        <div><span>Evidence</span><strong><AvailabilityBadge availability={ratesAvailability} /></strong><small>{metadata.report_evidence_version == null ? 'Version not provided' : `Version ${metadata.report_evidence_version}`}</small></div>
      </div>
      <div className={styles.availabilityRow}>
        {Object.entries(metadata.section_availability).map(([key, availability]) => (
          <span key={key}><span className={styles.availabilityLabel}>{key.replaceAll('_', ' ')}</span><AvailabilityBadge availability={availability} /></span>
        ))}
      </div>
      <section className={styles.section}>
        <div className={styles.sectionTitleRow}><h3>Rates and rules used</h3><AvailabilityBadge availability={ratesAvailability} /></div>
        {ratesUnavailable ? <EvidenceSectionMessage availability={ratesAvailability} emptyText="No used rate or rule definitions were captured." /> : (
          <>
            {ratesAvailability?.state === 'PARTIAL' && <EvidenceSectionMessage availability={ratesAvailability} emptyText="No used rate or rule definitions were captured." />}
            {ratesAvailability?.state === 'EMPTY' && <EvidenceSectionMessage availability={ratesAvailability} emptyText="No used rate or rule definitions were captured." />}
            {ratesAvailability?.state === 'AVAILABLE' && response.used_rate_definitions.length === 0 && <StateMessage>No used rate or rule definitions were captured.</StateMessage>}
            <RateDefinitionTable response={response} />
          </>
        )}
      </section>
      <section className={styles.section}>
        <div className={styles.sectionTitleRow}><h3>Bonus used</h3><AvailabilityBadge availability={bonusAvailability} /></div>
        {bonusUnavailable ? <EvidenceSectionMessage availability={bonusAvailability} emptyText="No Bonus evidence was captured." /> : (
          <>
            {bonusAvailability?.state === 'PARTIAL' && <EvidenceSectionMessage availability={bonusAvailability} emptyText="No Bonus evidence was captured." />}
            {bonusAvailability?.state === 'EMPTY' && <EvidenceSectionMessage availability={bonusAvailability} emptyText="No Bonus evidence was captured." />}
            {bonusAvailability?.state === 'AVAILABLE' && response.bonus_events.length === 0 && <StateMessage>No Bonus evidence was captured.</StateMessage>}
            <BonusEvidenceTable response={response} />
          </>
        )}
      </section>
    </>
  );
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return 'Not provided';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatAuditValue(value: unknown): string {
  if (value == null) return 'Not provided';
  if (typeof value === 'object') {
    if (Array.isArray(value)) return value.map((item) => formatAuditValue(item)).join(', ');
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${key}: ${formatAuditValue(item)}`)
      .join('; ');
  }
  return formatValue(value);
}

function AuditAvailabilityList({ response }: { response: FinalizedAuditResponse }) {
  return (
    <div className={styles.availabilityRow}>
      {Object.entries(response.metadata.section_availability).map(([key, availability]) => (
        <span key={key}>
          <span className={styles.availabilityLabel}>{key.replaceAll('_', ' ')}</span>
          <AvailabilityBadge availability={availability} />
          {availability.reason_code && <small>{availability.reason_code}</small>}
        </span>
      ))}
    </div>
  );
}

function AuditStateChanges({ event }: { event: FinalizedAuditEvent }) {
  const before = event.before_state ?? {};
  const after = event.after_state ?? {};
  const fields = [...new Set([...Object.keys(before), ...Object.keys(after)])]
    .filter((key) => JSON.stringify(before[key]) !== JSON.stringify(after[key]));
  if (fields.length === 0) return <StateMessage>No field-level change details were captured.</StateMessage>;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Changed field</th><th>Before</th><th>After</th></tr></thead>
        <tbody>{fields.map((field) => (
          <tr key={field}>
            <th scope="row">{field}</th>
            <td>{formatAuditValue(before[field])}</td>
            <td>{formatAuditValue(after[field])}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function AuditEventCard({ event }: { event: FinalizedAuditEvent }) {
  return (
    <article className={styles.auditEvent}>
      <div className={styles.auditEventHeading}>
        <div><strong>{event.action_code}</strong><span>{event.domain} · Event {event.event_id}</span></div>
        <span>{formatDateTime(event.occurred_at_utc)}</span>
      </div>
      <dl className={styles.auditFacts}>
        <div><dt>Actor</dt><dd>{event.actor_display_name || `User #${event.actor_user_id}`}</dd></div>
        <div><dt>Responsibility</dt><dd>{formatAuditValue(event.responsibility_context)}</dd></div>
        <div><dt>Revision</dt><dd>{event.revision_number ?? 'Not provided'}</dd></div>
        <div><dt>Snapshot</dt><dd>{event.snapshot_id ?? 'Not provided'}</dd></div>
        <div><dt>Source</dt><dd>{event.source_entity_type} · {event.source_entity_id}</dd></div>
        {event.driver_id != null && <div><dt>Driver</dt><dd>{event.driver_id}</dd></div>}
        {event.work_date != null && <div><dt>Work date</dt><dd>{event.work_date}</dd></div>}
        {event.review_item_id != null && <div><dt>Review item</dt><dd>{event.review_item_id}</dd></div>}
        {event.reason != null && <div><dt>Reason</dt><dd>{event.reason}</dd></div>}
        {event.correlation_id != null && <div><dt>Correlation</dt><dd>{event.correlation_id}</dd></div>}
      </dl>
      <AuditStateChanges event={event} />
    </article>
  );
}

function AuditEventSection({
  title,
  availability,
  events,
}: {
  title: string;
  availability: FinalizedSectionAvailability | undefined;
  events: FinalizedAuditEvent[];
}) {
  const state = availability?.state;
  const unavailable = !availability || state === 'UNAVAILABLE';
  return (
    <section className={styles.section}>
      <div className={styles.sectionTitleRow}><h3>{title}</h3><AvailabilityBadge availability={availability} /></div>
      {unavailable ? (
        <StateMessage>{availabilityMessage(availability, 'Immutable audit rows are unavailable.')}</StateMessage>
      ) : state === 'EMPTY' ? (
        <StateMessage>No captured events.</StateMessage>
      ) : (
        <>
          {state === 'PARTIAL' && <StateMessage>{availabilityMessage(availability, 'Immutable audit history is partial.')}</StateMessage>}
          {events.length === 0 ? <StateMessage>{state === 'PARTIAL' ? 'No available rows were captured for this section.' : 'No captured events.'}</StateMessage> : (
            <div className={styles.auditEventList}>{events.map((event) => <AuditEventCard key={event.event_id} event={event} />)}</div>
          )}
        </>
      )}
    </section>
  );
}

function LifecycleEventTable({ events }: { events: FinalizedAuditResponse['lifecycle_events'] }) {
  if (events.length === 0) return <StateMessage>No captured lifecycle events.</StateMessage>;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Action</th><th>Actor</th><th>When</th><th>Revision</th><th>Snapshot</th><th>Reason</th></tr></thead>
        <tbody>{events.map((event, index) => (
          <tr key={`${event.action_code}-${event.action_at_utc}-${index}`}>
            <td>{event.action_code}</td>
            <td>{event.actor_display_name || `User #${event.actor_user_id}`}<small>{formatAuditValue(event.responsibility_context)}</small></td>
            <td>{formatDateTime(event.action_at_utc)}</td>
            <td>{event.revision_number ?? 'Not provided'}</td>
            <td>{event.snapshot_id ?? 'Not provided'}</td>
            <td>{event.reason ?? 'Not provided'}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function RevisionGroupsTable({ groups }: { groups: FinalizedAuditResponse['revision_groups'] }) {
  if (groups.length === 0) return <StateMessage>No revision groups are available.</StateMessage>;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Revision</th><th>Snapshot</th><th>Submission</th><th>Final approved</th><th>Events</th><th>Review comments</th></tr></thead>
        <tbody>{groups.map((group) => (
          <tr key={group.snapshot_id}>
            <td>{group.revision_number}</td>
            <td>{group.snapshot_id}</td>
            <td>{group.submit_action ?? 'Not provided'}</td>
            <td>{group.is_final_approved_revision ? 'Yes' : 'No'}</td>
            <td>{group.event_ids.length}</td>
            <td>{group.review_comment_event_ids.length}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function RateRuleProvenanceTable({ rows }: { rows: FinalizedAuditResponse['rate_rule_provenance'] }) {
  if (rows.length === 0) return <StateMessage>No rate/rule provenance rows are available.</StateMessage>;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Definition</th><th>Evidence kind</th><th>Fingerprint</th></tr></thead>
        <tbody>{rows.map((row, index) => (
          <tr key={String(row.used_rate_definition_id ?? index)}>
            <td>{formatAuditValue(row.used_rate_definition_id)}</td>
            <td>{formatAuditValue(row.evidence_kind)}</td>
            <td>{formatAuditValue(row.definition_fingerprint)}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function AuditChronologyTable({ response }: { response: FinalizedAuditResponse }) {
  const relevantAvailability = Object.entries(response.metadata.section_availability)
    .filter(([key]) => key !== 'snapshot_provenance');
  const hasUnavailableSections = relevantAvailability.some(([, availability]) => (
    availability.state === 'PARTIAL' || availability.state === 'UNAVAILABLE'
  ));
  if (response.chronology.length === 0) {
    return <StateMessage>{hasUnavailableSections
      ? 'Immutable chronology rows are not fully available; see the section states above.'
      : 'No captured chronology events.'}</StateMessage>;
  }
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Event</th><th>When</th><th>Domain / action</th><th>Actor</th><th>Revision</th><th>Snapshot</th></tr></thead>
        <tbody>{response.chronology.map((event) => (
          <tr key={event.event_id}>
            <td>{event.event_id}</td>
            <td>{formatDateTime(event.occurred_at_utc)}</td>
            <td>{event.domain}<small>{event.action_code}</small></td>
            <td>{event.actor_display_name || `User #${event.actor_user_id}`}</td>
            <td>{event.revision_number ?? 'Not provided'}</td>
            <td>{event.snapshot_id ?? 'Not provided'}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function AuditBody({ response }: { response: FinalizedAuditResponse }) {
  const { metadata } = response;
  return (
    <>
      <div className={styles.metadataGrid}>
        <div><span>Period</span><strong>{metadata.period_name || metadata.period_code}</strong><small>{metadata.period_code}</small></div>
        <div><span>Status</span><strong>{metadata.period_status}</strong><small>Branch {metadata.branch_id}</small></div>
        <div><span>Authority</span><strong>{metadata.authority_kind}</strong><small>Immutable P6D evidence</small></div>
        <div><span>Revision</span><strong>{metadata.revision_number ?? 'Not provided'}</strong><small>{metadata.snapshot_id == null ? 'No snapshot' : `Snapshot ${metadata.snapshot_id}`}</small></div>
        <div><span>Evidence</span><strong>{metadata.evidence_version ?? 'Not provided'}</strong><small>{metadata.complete_period_chronology_available ? 'Complete chronology available' : 'Chronology has unavailable sections'}</small></div>
      </div>
      <section className={styles.section}>
        <div className={styles.sectionTitleRow}><h3>Audit availability</h3><span className={styles.muted}>Immutable evidence only</span></div>
        <AuditAvailabilityList response={response} />
        <div className={styles.provenanceGrid}>
          <div><span>Snapshot hash</span><strong title={metadata.snapshot_hash ?? undefined}>{formatHash(metadata.snapshot_hash)}</strong></div>
          <div><span>Period chronology</span><strong>{metadata.complete_period_chronology_available ? 'Complete' : 'Partial'}</strong></div>
          <div><span>Generated</span><strong>{formatDateTime(metadata.generated_at_utc)}</strong></div>
        </div>
      </section>
      <section className={styles.section}>
        <div className={styles.sectionTitleRow}><h3>Workflow lifecycle</h3><span className={styles.muted}>Frozen actors and responsibility</span></div>
        <LifecycleEventTable events={response.lifecycle_events} />
      </section>
      <section className={styles.section}>
        <div className={styles.sectionTitleRow}><h3>Evidence chronology</h3><span className={styles.muted}>Exact backend event order</span></div>
        <AuditChronologyTable response={response} />
      </section>
      <AuditEventSection title="Source changes" availability={metadata.section_availability.source} events={response.source_events} />
      <AuditEventSection title="Status and note changes" availability={metadata.section_availability.status_note} events={response.status_note_events} />
      <AuditEventSection title="Bonus changes" availability={metadata.section_availability.bonus} events={response.bonus_events} />
      <AuditEventSection title="Review comments and decisions" availability={metadata.section_availability.review_comment} events={response.review_events} />
      <section className={styles.section}>
        <div className={styles.sectionTitleRow}><h3>Revision groups</h3><span className={styles.muted}>Exact backend grouping</span></div>
        <RevisionGroupsTable groups={response.revision_groups} />
      </section>
      <section className={styles.section}>
        <div className={styles.sectionTitleRow}><h3>Rate/rule provenance</h3><span className={styles.muted}>Immutable used definitions</span></div>
        <RateRuleProvenanceTable rows={response.rate_rule_provenance} />
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
  const [ratesUsed, setRatesUsed] = useState<FinalizedRatesUsedResponse | null>(null);
  const [ratesUsedLoading, setRatesUsedLoading] = useState(false);
  const [ratesUsedError, setRatesUsedError] = useState<string | null>(null);
  const [ratesUsedRetry, setRatesUsedRetry] = useState(0);
  const [audit, setAudit] = useState<FinalizedAuditResponse | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditRetry, setAuditRetry] = useState(0);
  const requestRef = useRef(0);
  const auditRequestRef = useRef(0);
  const auditAvailability = overview?.section_availability.audit;
  const canViewAudit = auditAvailability != null && auditAvailability.state !== 'UNAVAILABLE';
  const visibleTab: FinalizedLibraryTab = activeTab === 'audit' && !canViewAudit ? 'overview' : activeTab;

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

  const loadRatesUsed = useCallback(async () => {
    setRatesUsedLoading(true);
    setRatesUsedError(null);
    try {
      setRatesUsed(await getFinalizedRatesUsed(period.payroll_period_id));
    } catch (error: unknown) {
      setRatesUsedError(errorDetail(error, 'Failed to load finalized rates and Bonus evidence.'));
    } finally {
      setRatesUsedLoading(false);
    }
  }, [period.payroll_period_id]);

  const loadAudit = useCallback(async () => {
    const requestId = auditRequestRef.current + 1;
    auditRequestRef.current = requestId;
    setAuditLoading(true);
    setAuditError(null);
    try {
      const data = await getFinalizedAudit(period.payroll_period_id);
      if (auditRequestRef.current === requestId) setAudit(data);
    } catch (error: unknown) {
      if (auditRequestRef.current === requestId) setAuditError(errorDetail(error, 'Failed to load finalized audit history.'));
    } finally {
      if (auditRequestRef.current === requestId) setAuditLoading(false);
    }
  }, [period.payroll_period_id]);

  useEffect(() => {
    if (!overview || activeTab !== 'off-status' || offStatus != null) return;
    const timer = window.setTimeout(() => { void loadOffStatus(); }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, loadOffStatus, offStatus, offStatusRetry, overview]);

  useEffect(() => {
    if (!overview || activeTab !== 'rates-used' || ratesUsed != null) return;
    const timer = window.setTimeout(() => { void loadRatesUsed(); }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, loadRatesUsed, overview, ratesUsed, ratesUsedRetry]);

  useEffect(() => {
    if (!overview || !canViewAudit || activeTab !== 'audit' || audit != null) return;
    const timer = window.setTimeout(() => { void loadAudit(); }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, audit, auditRetry, canViewAudit, loadAudit, overview]);

  useEffect(() => {
    if (!overview || !isFinalizedReportView(activeTab)) return;
    const reportView = activeTab;
    const timer = window.setTimeout(() => { void loadReport(reportView); }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, loadReport, overview, reportRetry]);

  useEffect(() => {
    function handleKey(event: KeyboardEvent) { if (event.key === 'Escape') onClose(); }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  useEffect(() => () => { auditRequestRef.current += 1; }, []);

  function selectTab(tab: FinalizedLibraryTab) {
    if (tab === activeTab) return;
    auditRequestRef.current += 1;
    setActiveTab(tab);
    setReport(null);
    setReportError(null);
    setReportLoading(tab !== 'overview' && tab !== 'off-status' && tab !== 'rates-used');
    setOffStatusError(null);
    setOffStatusLoading(tab === 'off-status' && offStatus == null);
    setRatesUsedError(null);
    setRatesUsedLoading(tab === 'rates-used' && ratesUsed == null);
    setAuditError(null);
    setAuditLoading(tab === 'audit' && audit == null);
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
          <button className={activeTab === 'rates-used' ? styles.activeTab : styles.tab} onClick={() => selectTab('rates-used')} type="button" role="tab" aria-selected={activeTab === 'rates-used'} disabled={!overview}>Rates / Bonus</button>
          {canViewAudit && <button className={activeTab === 'audit' ? styles.activeTab : styles.tab} onClick={() => selectTab('audit')} type="button" role="tab" aria-selected={activeTab === 'audit'}>Audit</button>}
        </nav>
        <div className={styles.body}>
          {overviewLoading ? <StateMessage>Loading finalized payroll overview…</StateMessage> : overviewError ? (
            <div className={styles.errorBox}><p>{overviewError}</p><button className={styles.retryButton} onClick={() => { auditRequestRef.current += 1; setOverviewLoading(true); setOverviewError(null); setOverview(null); setAudit(null); setActiveTab('overview'); setOverviewRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : overview == null ? <StateMessage>Finalized overview is unavailable.</StateMessage> : visibleTab === 'overview' ? <OverviewBody overview={overview} /> : visibleTab === 'off-status' ? offStatusLoading ? (
            <StateMessage>Loading finalized Off and Status history…</StateMessage>
          ) : offStatusError ? (
            <div className={styles.errorBox}><p>{offStatusError}</p><button className={styles.retryButton} onClick={() => { setOffStatusLoading(true); setOffStatusError(null); setOffStatus(null); setOffStatusRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : offStatus == null ? <StateMessage>Finalized Off and Status history is unavailable.</StateMessage> : <OffStatusBody response={offStatus} /> : visibleTab === 'rates-used' ? ratesUsedLoading ? (
            <StateMessage>Loading finalized rates and Bonus evidence…</StateMessage>
          ) : ratesUsedError ? (
            <div className={styles.errorBox}><p>{ratesUsedError}</p><button className={styles.retryButton} onClick={() => { setRatesUsedLoading(true); setRatesUsedError(null); setRatesUsed(null); setRatesUsedRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : ratesUsed == null ? <StateMessage>Finalized rates and Bonus evidence is unavailable.</StateMessage> : <RatesUsedBody response={ratesUsed} /> : visibleTab === 'audit' ? auditLoading ? (
            <StateMessage>Loading finalized audit history…</StateMessage>
          ) : auditError ? (
            <div className={styles.errorBox}><p>{auditError}</p><button className={styles.retryButton} onClick={() => { setAuditLoading(true); setAuditError(null); setAudit(null); setAuditRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : audit == null ? <StateMessage>Finalized audit history is unavailable.</StateMessage> : <AuditBody response={audit} /> : reportLoading ? (
            <StateMessage>Loading {REPORT_TABS.find((tab) => tab.view === activeTab)?.label.toLowerCase() ?? 'report'}…</StateMessage>
          ) : reportError ? (
            <div className={styles.errorBox}><p>{reportError}</p><button className={styles.retryButton} onClick={() => { setReportLoading(true); setReportError(null); setReportRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : report == null ? <StateMessage>No finalized report data is available.</StateMessage> : <ReportBody report={report} view={isFinalizedReportView(visibleTab) ? visibleTab : 'drivers'} />}
        </div>
      </div>
    </div>
  );
}
