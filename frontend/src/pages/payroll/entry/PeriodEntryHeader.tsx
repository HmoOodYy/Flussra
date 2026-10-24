import type { MouseEvent } from 'react';
import { Link } from 'react-router-dom';
import { PeriodStatusBadge } from '../../../components/StatusBadge';
import { formatIsoLong, formatIsoRange } from '../../../lib/isoDate';
import type { DayGridPeriod } from '../../../types/payroll/entry';
import { periodLifecycle } from './dayEntryPageModel';
import styles from './PayrollEntryPage.module.css';

const CURRENT_PAYROLL_PATH = '/payroll/periods';

/** The back link to Current payroll, shared by the page's header and its error states. */
export function BackToCurrentPayroll({ onClick }: { onClick?: (event: MouseEvent<HTMLAnchorElement>) => void }) {
  return (
    <Link to={CURRENT_PAYROLL_PATH} className={styles.backLink} onClick={onClick}>
      &larr; Current payroll
    </Link>
  );
}

interface PeriodEntryHeaderProps {
  period: DayGridPeriod;
  onBack: (event: MouseEvent<HTMLAnchorElement>) => void;
}

export function PeriodEntryHeader({ period, onBack }: PeriodEntryHeaderProps) {
  const stages = periodLifecycle(period.status);
  return (
    <header className={styles.header}>
      <BackToCurrentPayroll onClick={onBack} />
      <div className={styles.titleRow}>
        <h1 className={styles.title}>{period.period_name}</h1>
        <PeriodStatusBadge status={period.status} />
      </div>
      <p className={styles.periodMeta}>
        {[
          period.branch_name,
          formatIsoRange(period.start_date, period.end_date),
          period.pay_date ? `Pay date ${formatIsoLong(period.pay_date)}` : null,
        ].filter(Boolean).join(' · ')}
      </p>
      {stages && (
        <ol className={styles.lifecycle} aria-label="Payroll lifecycle">
          {stages.map((stage) => (
            <li
              key={stage.key}
              className={`${styles.stage} ${styles[`stage_${stage.state}`]}`}
              aria-current={stage.state === 'current' ? 'step' : undefined}
            >
              <span className={styles.stageMark} aria-hidden="true" />
              {stage.label}
            </li>
          ))}
        </ol>
      )}
    </header>
  );
}

/** Why the grid cannot be edited, in the words of the period's status. */
export function PeriodStatusBanner({ status, isEditable }: { status: string; isEditable: boolean }) {
  const message = (() => {
    if (status === 'Draft')
      return <>This period is <strong>Prepared</strong> — operational source entry is available, but it cannot be submitted until the backend workflow promotes it to Open.</>;
    if (status === 'InReview')
      return <>This period is <strong>In Review</strong> — grid is read-only while awaiting approval.</>;
    if (status === 'Returned')
      return <>This period was <strong>returned for correction</strong>. Update its live source entries, then resubmit it for review.</>;
    if (status === 'Approved')
      return <>This period is <strong>Approved</strong> — read-only. Finalize from the payroll hub.</>;
    if (!isEditable)
      return <>This period is <strong>{status}</strong> and cannot be edited.</>;
    return null;
  })();
  return message ? <div className={styles.banner} role="status">{message}</div> : null;
}
