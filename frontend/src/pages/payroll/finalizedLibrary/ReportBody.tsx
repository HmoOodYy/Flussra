import type {
  FinalizedCalculationReportResponse,
  FinalizedReportView,
} from '../../../types/payroll/finalized';
import type { ReportDriver } from '../../../types/payroll/reports';
import { buildPeriodPayTable } from '../periodPayTable';
import { PeriodPayMatrix } from '../PeriodPayMatrix';
import { WorkTotals } from '../WorkTotals';
import { availabilityMessage, formatMoney, formatValue } from './formatting';
import { AvailabilityBadge, StateMessage } from './EvidencePresentation';
import styles from '../FinalizedPayrollLibraryDialog.module.css';

function Dictionary({
  title,
  values,
  money = false,
  code,
  digits,
}: {
  title: string;
  values: Record<string, string> | null;
  money?: boolean;
  code?: string | null;
  digits?: number | null;
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
            <div key={key}><dt>{key}</dt><dd>{money ? formatMoney(value, code, digits) : formatValue(value)}</dd></div>
          ))}
        </dl>
      )}
    </div>
  );
}

const WORK_COLUMNS = [
  { key: 'work_date', label: 'Date' },
  { key: 'definition_name', label: 'Pay definition' },
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
  { key: 'line', label: 'Line' },
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
  code,
  digits,
}: {
  rows: Record<string, unknown>[];
  columns: readonly { key: string; label: string }[];
  emptyText: string;
  moneyKeys?: readonly string[];
  code?: string | null;
  digits?: number | null;
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
                return <td key={column.key} className={isMoney ? styles.numeric : undefined}>{isMoney ? formatMoney(value, code, digits) : formatValue(value)}</td>;
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
  code,
  digits,
}: {
  driver: ReportDriver;
  showWork: boolean;
  showPay: boolean;
  evidenceMessage: string;
  code: string;
  digits: number;
}) {
  return (
    <article className={styles.driverCard}>
      <div className={styles.driverHeading}>
        <div><strong>{driver.driver_name ?? 'Unnamed driver'}</strong><span>{driver.driver_number ?? '—'}</span></div>
        <span className={styles.driverAssignmentId}>Driver ID {driver.driver_assignment_id}</span>
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
                  ['Minimum adjustment', driver.pay.minimum_adjustment],
                  ['Maximum adjustment', driver.pay.maximum_adjustment],
                  ['Bonus total', driver.pay.bonus_total],
                  ['Total pay', driver.pay.total_pay],
                ] as const).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{formatMoney(value, code, digits)}</dd></div>)}
              </dl>
              <h5>Financial lines</h5>
              <RecordTable rows={driver.pay.financial_lines.map((l) => ({ ...l, line: l.definition_name ?? l.line_type }))} columns={FINANCIAL_COLUMNS} moneyKeys={['resolved_rate_amount', 'calculated_amount']} code={code} digits={digits} emptyText="No financial lines are available." />
            </>
          )}
        </section>
      )}
      {showPay && (
        <section className={styles.subsection}>
          <h4>Bonus evidence</h4>
          <RecordTable rows={driver.bonus_events} columns={BONUS_COLUMNS} moneyKeys={['amount']} code={code} digits={digits} emptyText={evidenceMessage} />
        </section>
      )}
    </article>
  );
}

function periodPayCell(value: string | null, code: string, digits: number): string {
  return value == null ? '—' : formatMoney(value, code, digits);
}

export function ReportBody({ report, view }: { report: FinalizedCalculationReportResponse; view: FinalizedReportView }) {
  const { currency_code: code, currency_minor_unit_digits: digits } = report.metadata;
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
      {view === 'period-pay' ? (
        <section className={styles.section}>
          <h3>Period Pay</h3>
          <PeriodPayMatrix
            table={buildPeriodPayTable(report.definition_columns, report.drivers, report.definition_totals, report.pay_totals)}
            formatCell={(value) => periodPayCell(value, code, digits)}
            emptyState={<StateMessage>No driver records are available.</StateMessage>}
          />
        </section>
      ) : (
        <>
          <section className={styles.totals}>
            {showWork && <WorkTotals totals={report.work_totals} columns={report.columns} className={styles.dictionary} />}
            {showPay && <Dictionary title="Pay totals" values={report.pay_totals} money code={code} digits={digits} />}
          </section>
          {report.columns.length > 0 && (
            <section className={styles.columns}>
              <h3>Report columns</h3>
              <div>{report.columns.map((column) => <span key={column.payroll_period_definition_id}>{column.label} <small>({column.code})</small></span>)}</div>
            </section>
          )}
          <section className={styles.section}>
            <h3>Drivers</h3>
            {report.drivers.length === 0 ? <StateMessage>No driver records are available.</StateMessage> : (
              <div className={styles.driverList}>
                {report.drivers.map((driver) => <DriverReport key={driver.driver_assignment_id} driver={driver} showWork={showWork} showPay={showPay} evidenceMessage={evidenceMessage} code={code} digits={digits} />)}
              </div>
            )}
          </section>
        </>
      )}
    </>
  );
}
