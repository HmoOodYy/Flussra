import type { FinalizedOffDriversResponse } from '../../../types/payroll/finalized';
import { AvailabilityBadge, EvidenceSectionMessage, StateMessage } from './EvidencePresentation';
import styles from '../FinalizedPayrollLibraryDialog.module.css';

function OffDriverTable({ response }: { response: FinalizedOffDriversResponse }) {
  if (response.fully_off_drivers.length === 0) return null;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Driver</th><th>Code</th><th>Eligible days</th><th>Off days</th></tr></thead>
        <tbody>
          {response.fully_off_drivers.map((driver) => (
            <tr key={driver.driver_assignment_id}>
              <td>{driver.driver_name || 'Unnamed driver'}</td>
              <td>{driver.driver_number ?? 'Not provided'}</td>
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
            <tr key={`${entry.driver_assignment_id}-${entry.work_date}-${entry.status_key_id}-${index}`}>
              <td>{entry.work_date}</td>
              <td>{entry.driver_name ?? 'Unnamed driver'}</td>
              <td>{entry.driver_number ?? 'Not provided'}</td>
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

export function OffStatusBody({ response }: { response: FinalizedOffDriversResponse }) {
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
