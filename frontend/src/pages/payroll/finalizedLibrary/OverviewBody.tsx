import type { FinalizedOverviewResponse } from '../../../types/payroll';
import { formatHash, formatMoney } from './formatting';
import { AvailabilityBadge } from './EvidencePresentation';
import styles from '../FinalizedPayrollLibraryDialog.module.css';

export function OverviewBody({ overview }: { overview: FinalizedOverviewResponse }) {
  const summary = overview.financial_summary;
  const availabilityEntries = Object.entries(overview.section_availability);
  return (
    <div>
      <div className={styles.identityHeader}>
        <div><span className={styles.eyebrow}>Finalized Payroll Library</span><h3>{overview.period_name || overview.period_code}</h3><p>{overview.branch_name} · {overview.period_code}</p></div>
        <span className={styles.finalizedPill}>{overview.period_status}</span>
      </div>
      <div className={styles.kpiGrid}>
        <div><strong>{formatMoney(summary.total_pay, overview.currency_code, overview.currency_minor_unit_digits)}</strong><span>Total pay</span></div>
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
