import type { FinalizedRatesUsedResponse } from '../../../types/payroll/finalized';
import { formatMoney } from './formatting';
import { AvailabilityBadge, EvidenceSectionMessage, StateMessage } from './EvidencePresentation';
import styles from '../FinalizedPayrollLibraryDialog.module.css';

function rateDefinitionLabel(definition: FinalizedRatesUsedResponse['used_rate_definitions'][number]): string {
  if (definition.evidence_kind === 'DriverRateSchedule') {
    return definition.status_rate_column_id == null ? 'Driver rate schedule' : 'Driver Status rate schedule';
  }
  if (definition.evidence_kind === 'DriverPayRule') return 'Driver pay rule';
  return definition.evidence_kind;
}

function RateDefinitionTable({ response }: { response: FinalizedRatesUsedResponse }) {
  if (response.used_rate_definitions.length === 0) return null;
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead><tr><th>Driver</th><th>Pay definition / Status column</th><th>Definition</th><th>Rate / behavior</th><th>Rule metadata</th><th>Effective</th><th>Used</th></tr></thead>
        <tbody>
          {response.used_rate_definitions.map((definition) => {
            const driver = definition.driver_name || 'Unnamed driver';
            const payDefinition = definition.status_rate_column_name || definition.pay_definition_label || definition.pay_definition_code || (definition.pay_definition_id == null ? 'Not applicable' : `PayDefinition #${definition.pay_definition_id}`);
            const rateDetails = [definition.unit_name, definition.rate_status].filter(Boolean).join(' · ');
            const ruleDetails = [definition.rule_type, definition.rule_amount == null ? null : `Amount ${formatMoney(definition.rule_amount, response.metadata.currency_code, response.metadata.currency_minor_unit_digits)}`, definition.rule_prorated_amount == null ? null : `Prorated ${formatMoney(definition.rule_prorated_amount, response.metadata.currency_code, response.metadata.currency_minor_unit_digits)}`, definition.min_max_status, definition.rule_status].filter(Boolean).join(' · ');
            const effective = definition.effective_from || definition.effective_to
              ? `${definition.effective_from ?? 'Open'} to ${definition.effective_to ?? 'Open'}`
              : 'Not provided';
            return (
              <tr key={`${definition.evidence_kind}-${definition.used_rate_definition_id}`}>
                <td>{driver}<small>{definition.driver_number ?? '—'}</small></td>
                <td>{payDefinition}<small>{definition.status_rate_column_id != null ? `Status rate column ${definition.status_rate_column_id}` : definition.pay_definition_id == null ? 'No definition' : `ID ${definition.pay_definition_id}`}</small></td>
                <td>{rateDefinitionLabel(definition)}<small>{definition.source_type}</small></td>
                <td>{definition.rate_amount == null ? 'Not provided' : formatMoney(definition.rate_amount, response.metadata.currency_code, response.metadata.currency_minor_unit_digits)}<small>{rateDetails || 'Details not provided'}</small></td>
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
              <td>{event.driver_name || 'Unnamed driver'}<small>{event.driver_number ?? '—'}</small></td>
              <td className={styles.numeric}>{formatMoney(event.amount, response.metadata.currency_code, response.metadata.currency_minor_unit_digits)}</td>
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

export function RatesUsedBody({ response }: { response: FinalizedRatesUsedResponse }) {
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
