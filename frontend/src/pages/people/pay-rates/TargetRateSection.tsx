import type { ReactNode } from 'react';
import type { DriverPayRateRow } from '../../../types/compensation';
import { fmtAmount } from './payRatesView';
import { applicabilityFlag } from './payRatesDrivers';
import { isTieredSchedule, scalarAmount } from './payRatesTiers';
import { TierValueList } from './TierValueList';
import styles from './PayRatesPage.module.css';

type TargetRateSectionProps = {
  title: string;
  /** The first column's header: what each row is. */
  rowHeader: string;
  note?: string;
  empty: ReactNode;
  rows: DriverPayRateRow[];
  canEditMatrix: boolean;
  referenceDate: string | undefined;
  currencyCode: string | null;
  currencyDigits: number | null;
  targetDrafts: Record<number, { values: Record<number, string>; effective_from: string }>;
  targetErrors: Record<number, string>;
  targetSavingId: number | null;
  setTargetField: (id: number, field: 'effective_from' | number, value: string) => void;
  saveTargetRow: (row: DriverPayRateRow, approve: boolean) => Promise<void>;
  cancelTargetEdit: (id: number) => void;
  startTargetEdit: (row: DriverPayRateRow) => void;
};

export function TargetRateSection({ title, rowHeader, note, empty, rows, canEditMatrix, referenceDate, currencyCode, currencyDigits, targetDrafts, targetErrors, targetSavingId, setTargetField, saveTargetRow, cancelTargetEdit, startTargetEdit }: TargetRateSectionProps) {
  const showValues = (row: DriverPayRateRow, values: NonNullable<DriverPayRateRow['current']>['values']) =>
    row.kind === 'PayDefinition' && row.rate_shape === 'OrdinalTierSchedule' && isTieredSchedule(values)
      ? <TierValueList values={values} unit={row.unit} currencyCode={currencyCode} currencyDigits={currencyDigits} />
      : fmtAmount(row.kind === 'StatusRateColumn' ? values[0]?.amount ?? null : scalarAmount(values), row.unit, currencyCode, currencyDigits);
  const completeDraft = (row: DriverPayRateRow, draft: { values: Record<number, string>; effective_from: string }) =>
    Boolean(draft.effective_from && row.components.length > 0
      && row.components.every((component) => draft.values[component.rate_component_definition_id] !== undefined
        && draft.values[component.rate_component_definition_id] !== ''));
  return (
    <div className={styles.section}>
      <div className={styles.sectionHead}><span className={styles.sectionTitle}>{title}</span></div>
      {note && <div className={styles.statusPaneNote}>{note}</div>}
      {rows.length > 0 ? (
        <table className={styles.rateTable}>
          <thead><tr><th>{rowHeader}</th><th>Amount / Tier Values</th><th>Effective From</th><th>Status</th>{canEditMatrix && <th>Actions</th>}</tr></thead>
          <tbody>
            {rows.map((row) => {
              const id = row.rate_definition_id;
              const draft = targetDrafts[id];
              const editing = draft !== undefined;
              const busy = targetSavingId === id;
              return (
                <tr key={id}>
                  <td>
                    <strong>{row.definition_name}</strong>
                    {row.definition_code && <div style={{ fontSize: '0.74rem', color: '#6b7280' }}>{row.definition_code}</div>}
                    {applicabilityFlag(row) && <div className={styles.notApplicableFlag}>{applicabilityFlag(row)}</div>}
                    {row.future && <div style={{ fontSize: '0.72rem', color: '#d97706', marginTop: '2px' }}>Next from {row.future.effective_from}: {showValues(row, row.future.values)}</div>}
                    {targetErrors[id] && <div className={styles.actionErr} style={{ marginTop: '0.35rem' }}>{targetErrors[id]}</div>}
                  </td>
                  <td>
                    {editing ? (row.kind === 'PayDefinition' && row.rate_shape === 'OrdinalTierSchedule' ? (
                      <div className={styles.tierRateEditor}>
                        {row.components.slice().sort((a, b) => a.sequence_no - b.sequence_no).map((component) => (
                          <label className={styles.tierRateInput} key={component.rate_component_definition_id}>
                            <span>{component.ordinal_from == null ? 'Tier unavailable'
                              : component.ordinal_to === null ? `${component.ordinal_from}+`
                              : component.ordinal_from === component.ordinal_to ? `${component.ordinal_from}`
                                : `${component.ordinal_from}–${component.ordinal_to}`}</span>
                            <input className={styles.editInput} type="number" step="0.0001" min="0" placeholder="Rate" value={draft.values[component.rate_component_definition_id] ?? ''}
                              onChange={(e) => setTargetField(id, component.rate_component_definition_id, e.target.value)} />
                          </label>
                        ))}
                      </div>
                    ) : <input className={styles.editInput} type="number" step="0.0001" min="0" placeholder="0.0000"
                      value={draft.values[row.components[0]?.rate_component_definition_id] ?? ''}
                      onChange={(e) => setTargetField(id, row.components[0]?.rate_component_definition_id ?? -1, e.target.value)} />)
                      : row.current ? showValues(row, row.current.values) : <span className={styles.missingText}>No rate set</span>}
                  </td>
                  <td>
                    {editing ? <input className={styles.editDateInput} type="date" value={draft.effective_from} onChange={(e) => setTargetField(id, 'effective_from', e.target.value)} /> : row.current ? row.current.effective_from : '—'}
                  </td>
                  <td>
                    {row.pending && !editing && <span className={styles.pendingBadge} style={{ marginRight: '0.4rem' }}>Pending</span>}
                    {!editing && (row.current ? <span className={styles.okIcon}>&#10003;</span> : <span className={styles.missingIcon}>&#9888;</span>)}
                    {editing && <span className={styles.pendingBadge}>Editing</span>}
                  </td>
                  {canEditMatrix && <td>
                    {editing ? <div className={styles.actionBtns}>
                      <button className={styles.btnApprove} disabled={busy || targetSavingId != null || !completeDraft(row, draft)} onClick={() => void saveTargetRow(row, true)}>{busy ? 'Saving…' : 'Save & Approve'}</button>
                      <button className={styles.btnSecondary} disabled={busy || targetSavingId != null || !completeDraft(row, draft)} onClick={() => void saveTargetRow(row, false)}>Save as Pending</button>
                      <button className={styles.btnSecondary} disabled={busy} onClick={() => cancelTargetEdit(id)}>Cancel</button>
                    </div> : <button className={styles.btnSecondary} disabled={targetSavingId != null || !referenceDate} onClick={() => startTargetEdit(row)}>Edit rate</button>}
                  </td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <div className={styles.emptySection}>{empty}</div>
      )}
    </div>
  );
}
