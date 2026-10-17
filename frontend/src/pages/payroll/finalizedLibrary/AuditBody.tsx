import type { FinalizedAuditEvent, FinalizedAuditResponse, FinalizedSectionAvailability } from '../../../types/payroll';
import { availabilityMessage, formatHash, formatValue } from './formatting';
import { AvailabilityBadge, StateMessage } from './EvidencePresentation';
import styles from '../FinalizedPayrollLibraryDialog.module.css';

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

export function AuditBody({ response }: { response: FinalizedAuditResponse }) {
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
