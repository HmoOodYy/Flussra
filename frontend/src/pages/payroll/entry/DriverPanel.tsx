import { Link } from 'react-router-dom';
import { formatMoney, formatRate } from '../../../lib/money';
import type { CalculationPreviewDriver } from '../../../types/payroll/calculation';
import type { DayGridColumn } from '../../../types/payroll/entry';
import { dayChip } from './dayEntryPageModel';
import { payParts, ratesByColumn, type WeekBar } from './periodOverviewModel';
import styles from './PayrollEntryPage.module.css';

interface DriverPanelProps {
  name: string;
  number: string | null;
  branchName: string;
  columns: readonly DayGridColumn[];
  /** The column the week bars show. */
  showKey: string;
  onShowKey: (key: string) => void;
  /** The driver's visible week; null when the overview could not be read. */
  bars: readonly WeekBar[] | null;
  daysWorked: number | null;
  /** The currency of the period's calculation preview; null when it has none. */
  preview: { currency_code: string | null; currency_minor_unit_digits: number | null } | null;
  previewDriver: CalculationPreviewDriver | undefined;
  canFixRates: boolean;
  onClose: () => void;
}

/** The side panel of one driver: the week, days worked, pay so far and the rates used. */
export function DriverPanel({
  name, number, branchName, columns, showKey, onShowKey, bars, daysWorked, preview, previewDriver, canFixRates, onClose,
}: DriverPanelProps) {
  const column = columns.find((c) => String(c.payroll_period_definition_id) === showKey) ?? columns[0];
  const money = (amount: string) => formatMoney(amount, preview?.currency_code, preview?.currency_minor_unit_digits);

  return (
    <aside className={styles.panel} aria-label={`Details for ${name}`}>
      <div className={styles.panelHead}>
        <div className={styles.panelWho}>
          <h2 className={styles.panelName}>{name}</h2>
          <p className={styles.panelMeta}>{[number, branchName].filter(Boolean).join(' · ')}</p>
        </div>
        <button type="button" className={styles.panelClose} aria-label="Close driver details" onClick={onClose}>
          &#x2715;
        </button>
      </div>

      <section className={styles.panelSection}>
        <div className={styles.panelTitleRow}>
          <h3 className={styles.panelTitle}>This week{column ? ` · ${column.label}` : ''}</h3>
          {columns.length > 1 && (
            <select
              className={styles.panelSelect}
              aria-label="Column shown for the week"
              value={showKey}
              onChange={(e) => onShowKey(e.target.value)}
            >
              {columns.map((c) => (
                <option key={c.payroll_period_definition_id} value={String(c.payroll_period_definition_id)}>{c.label}</option>
              ))}
            </select>
          )}
        </div>
        {bars === null ? (
          <p className={styles.panelNote}>The week is not available right now.</p>
        ) : (
          <ul className={styles.bars}>
            {bars.map((bar) => (
              <li key={bar.date} className={styles.barRow}>
                <span className={styles.barDate}>{`${dayChip(bar.date).weekday} ${dayChip(bar.date).dayOfMonth}`}</span>
                <span className={styles.barTrack} aria-hidden="true">
                  {bar.kind === 'quantity' && (
                    <span className={styles.barFill} style={{ width: `${Math.round(bar.ratio * 100)}%` }} />
                  )}
                </span>
                <span className={`${styles.barLabel} ${bar.kind === 'status' ? styles.weekStatus : bar.kind === 'quantity' ? '' : styles.panelMuted}`}>
                  {bar.label}
                  {bar.kind === 'quantity' && column?.unit && <span className={styles.panelMuted}> {column.unit}</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className={styles.panelSection}>
        <h3 className={styles.panelTitle}>Days worked</h3>
        <p className={styles.panelBig}>{daysWorked === null ? '—' : daysWorked}</p>
      </section>

      <section className={styles.panelSection}>
        <h3 className={styles.panelTitle}>Period pay so far</h3>
        {preview === null ? (
          <p className={styles.panelNote}>Pay is shown once the period is Open</p>
        ) : previewDriver === undefined ? (
          <p className={styles.panelBig}>—</p>
        ) : (
          <>
            <p className={styles.panelBig}>{money(previewDriver.expected_pay)}</p>
            <dl className={styles.parts}>
              {payParts(previewDriver).map((part) => (
                <div key={part.key} className={styles.partRow}>
                  <dt>{part.label}</dt>
                  <dd>{money(part.amount)}</dd>
                </div>
              ))}
            </dl>
          </>
        )}
      </section>

      <section className={styles.panelSection}>
        <h3 className={styles.panelTitle}>Rates used</h3>
        {preview === null ? (
          <p className={styles.panelNote}>Rates are shown once the period is Open</p>
        ) : (
          <ul className={styles.rateList}>
            {ratesByColumn(previewDriver, columns).map((entry) => (
              <li key={entry.key} className={styles.rateRow}>
                <span className={styles.rateLabel}>{entry.label}</span>
                {entry.noEntries ? (
                  <span className={styles.panelMuted}>No entries yet</span>
                ) : entry.needsReview ? (
                  <span className={styles.rateWarn}>
                    No approved rate
                    {canFixRates && <Link to="/people/pay-rates" className={styles.rateFix}>Fix in Drivers pay rate</Link>}
                  </span>
                ) : (
                  <span>
                    {entry.rates.length === 0
                      ? '—'
                      : entry.rates
                        .map((rate) => `${formatRate(rate, preview.currency_code, preview.currency_minor_unit_digits)}${entry.unit ? ` per ${entry.unit}` : ''}`)
                        .join(', ')}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}
