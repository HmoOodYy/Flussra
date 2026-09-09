import { useCallback, useEffect, useState } from 'react';
import { getCalculationPreview } from '../../lib/payrollApi';
import type {
  CalculationPreviewDriver,
  CalculationPreviewLine,
  CalculationPreviewResponse,
} from '../../types/payroll';
import styles from './CalculationPreviewDialog.module.css';

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

function lineLabel(line: CalculationPreviewLine): string {
  if (line.line_type === 'SYS_MIN_TOPUP') return 'Minimum top-up';
  if (line.line_type === 'SYS_MAX_CAP') return 'Maximum cap';
  if (line.source_type === 'StatusEntryState') return 'Status payment';
  if (line.source_type === 'BonusEvent') return 'Bonus event';
  return line.line_type;
}

function errorDetail(error: unknown, fallback: string): string {
  const detail =
    (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return fallback;
}

function NoticeList({ title, values, tone }: { title: string; values: string[]; tone: 'blocker' | 'warning' }) {
  if (values.length === 0) return null;
  return (
    <section className={tone === 'blocker' ? styles.blockers : styles.warnings}>
      <strong>{title}</strong>
      <ul>
        {values.map((value, index) => <li key={`${value}-${index}`}>{value}</li>)}
      </ul>
    </section>
  );
}

function DriverTotals({ drivers }: { drivers: CalculationPreviewDriver[] }) {
  if (drivers.length === 0) return <p className={styles.empty}>No driver totals are currently available.</p>;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Driver</th>
            <th className={styles.numeric}>Daily</th>
            <th className={styles.numeric}>Status</th>
            <th className={styles.numeric}>Period</th>
            <th className={styles.numeric}>Min</th>
            <th className={styles.numeric}>Max</th>
            <th className={styles.numeric}>Bonus</th>
            <th className={styles.numeric}>Expected Pay</th>
            <th>Blockers</th>
          </tr>
        </thead>
        <tbody>
          {drivers.map((driver) => (
            <tr key={driver.driver_id} className={driver.needs_manager_review ? styles.reviewRow : undefined}>
              <td>{driver.driver_name ?? `Driver #${driver.driver_id}`}</td>
              <td className={styles.numeric}>{formatMoney(driver.daily_pay)}</td>
              <td className={styles.numeric}>{formatMoney(driver.status_pay)}</td>
              <td className={styles.numeric}>{formatMoney(driver.period_pay)}</td>
              <td className={styles.numeric}>{formatMoney(driver.minimum_adjustment)}</td>
              <td className={styles.numeric}>{formatMoney(driver.maximum_adjustment)}</td>
              <td className={styles.numeric}>{formatMoney(driver.bonus_total)}</td>
              <td className={`${styles.numeric} ${styles.expectedPay}`}>{formatMoney(driver.expected_pay)}</td>
              <td>{driver.blockers.length > 0 ? driver.blockers.join(' ') : driver.needs_manager_review ? 'Needs review' : '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function FinancialLines({ drivers }: { drivers: CalculationPreviewDriver[] }) {
  const rows = drivers.flatMap((driver) => driver.lines.map((line, index) => ({ driver, line, index })));
  if (rows.length === 0) return <p className={styles.empty}>No financial lines are currently available.</p>;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Driver</th>
            <th>Source</th>
            <th>Line</th>
            <th>Date</th>
            <th className={styles.numeric}>Quantity</th>
            <th className={styles.numeric}>Resolved Rate</th>
            <th className={styles.numeric}>Calculated Amount</th>
            <th>Issue</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ driver, line, index }) => (
            <tr key={`${driver.driver_id}-${line.source_type}-${line.source_id ?? index}`} className={line.needs_manager_review ? styles.reviewRow : undefined}>
              <td>{driver.driver_name ?? `Driver #${driver.driver_id}`}</td>
              <td>{line.source_type}</td>
              <td>{lineLabel(line)}</td>
              <td>{line.work_date ?? '-'}</td>
              <td className={styles.numeric}>{formatQuantity(line.quantity)}</td>
              <td className={styles.numeric}>{line.resolved_rate == null ? '-' : formatMoney(line.resolved_rate)}</td>
              <td className={styles.numeric}>{formatMoney(line.calculated_amount)}</td>
              <td>{line.blocker_reason ?? (line.needs_manager_review ? 'Needs review' : '-')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface CalculationPreviewDialogProps {
  periodId: number;
  periodName?: string;
  workingDrivers?: number | null;
  onClose: () => void;
  onLifecycleChanged: () => void;
}

export function CalculationPreviewDialog({
  periodId,
  periodName,
  workingDrivers,
  onClose,
  onLifecycleChanged,
}: CalculationPreviewDialogProps) {
  const [preview, setPreview] = useState<CalculationPreviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const loadPreview = useCallback(async () => {
    setLoading(true);
    setPreview(null);
    setLoadError(null);
    try {
      const data = await getCalculationPreview(periodId);
      setPreview(data);
    } catch (error: unknown) {
      const status = (error as { response?: { status?: number } })?.response?.status;
      setLoadError(errorDetail(
        error,
        status === 422
          ? 'This period is no longer Open or Returned. Current workflow has been refreshed.'
          : 'Failed to load expected payroll.',
      ));
      if (status === 422) onLifecycleChanged();
    } finally {
      setLoading(false);
    }
  }, [onLifecycleChanged, periodId]);

  useEffect(() => {
    let active = true;
    async function load() {
      setLoading(true);
      setPreview(null);
      setLoadError(null);
      try {
        const data = await getCalculationPreview(periodId);
        if (active) setPreview(data);
      } catch (error: unknown) {
        if (!active) return;
        const status = (error as { response?: { status?: number } })?.response?.status;
        setLoadError(errorDetail(
          error,
          status === 422
            ? 'This period is no longer Open or Returned. Current workflow has been refreshed.'
            : 'Failed to load expected payroll.',
        ));
        if (status === 422) onLifecycleChanged();
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => { active = false; };
  }, [onLifecycleChanged, periodId]);

  useEffect(() => {
    function handleKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div className={styles.dialog} onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-label="Expected Payroll">
        <header className={styles.header}>
          <div>
            <h2>Expected Payroll</h2>
            <p>{periodName ?? 'Current payroll'} - live and provisional from current source data</p>
          </div>
          <button className={styles.closeButton} onClick={onClose} aria-label="Close">&#x2715;</button>
        </header>

        <div className={styles.body}>
          {loading ? (
            <p className={styles.state}>Loading expected payroll...</p>
          ) : loadError ? (
            <div className={styles.error}>
              <p>{loadError}</p>
              <button className={styles.retryButton} onClick={() => void loadPreview()}>Retry</button>
            </div>
          ) : preview == null ? null : (
            <>
              <div className={styles.summary}>
                <div>
                  <span>Expected Payroll</span>
                  <strong>{preview.financials_available ? formatMoney(preview.total_expected_pay) : 'Unavailable'}</strong>
                </div>
                <div>
                  <span>Period State</span>
                  <strong>{preview.status}</strong>
                </div>
                <div>
                  <span>Working Drivers</span>
                  <strong>{workingDrivers == null ? 'Unavailable' : workingDrivers}</strong>
                </div>
                <div>
                  <span>Calculation</span>
                  <strong>{preview.has_blockers ? 'Blockers present' : 'No reported blockers'}</strong>
                </div>
              </div>

              {!preview.financials_available && (
                <div className={styles.error}>Financial details are not currently available from the backend.</div>
              )}
              <NoticeList title="Resolve before submitting" values={preview.blockers} tone="blocker" />
              <NoticeList title="Warnings" values={preview.warnings} tone="warning" />

              <section className={styles.section}>
                <h3>Driver Totals</h3>
                <DriverTotals drivers={preview.drivers} />
              </section>

              <section className={styles.section}>
                <h3>Financial Lines</h3>
                <p className={styles.sectionHint}>Lines are resolved live by the backend. A missing calculated amount is shown as unresolved rather than derived in this view.</p>
                <FinancialLines drivers={preview.drivers} />
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
