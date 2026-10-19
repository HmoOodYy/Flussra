import type { DriverPayRateRow } from '../../../types/compensation';
import { fmtAmount } from './payRatesView';
import { applicabilityFlag } from './payRatesDrivers';
import styles from './PayRatesPage.module.css';

type TargetRateSectionProps = {
  rows: DriverPayRateRow[];
  canEditMatrix: boolean;
  referenceDate: string | undefined;
  currencyCode: string | null;
  currencyDigits: number | null;
  targetDrafts: Record<number, { amount: string; effective_from: string }>;
  targetErrors: Record<number, string>;
  targetSavingId: number | null;
  setTargetField: (id: number, field: 'amount' | 'effective_from', value: string) => void;
  saveTargetRow: (row: DriverPayRateRow, approve: boolean) => Promise<void>;
  cancelTargetEdit: (id: number) => void;
  startTargetEdit: (row: DriverPayRateRow) => void;
};

export function TargetRateSection({ rows, canEditMatrix, referenceDate, currencyCode, currencyDigits, targetDrafts, targetErrors, targetSavingId, setTargetField, saveTargetRow, cancelTargetEdit, startTargetEdit }: TargetRateSectionProps) {
  return (
    <div className={styles.section}>
      <div className={styles.sectionHead}><span className={styles.sectionTitle}>Pay Item Rates</span></div>
      {rows.length > 0 ? (
        <table className={styles.rateTable}>
          <thead><tr><th>Pay Item</th><th>Amount</th><th>Effective From</th><th>Status</th>{canEditMatrix && <th>Actions</th>}</tr></thead>
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
                    <div style={{ fontSize: '0.74rem', color: '#6b7280' }}>{row.definition_code}</div>
                    {applicabilityFlag(row) && <div className={styles.notApplicableFlag}>{applicabilityFlag(row)}</div>}
                    {row.future && <div style={{ fontSize: '0.72rem', color: '#d97706', marginTop: '2px' }}>Next: {fmtAmount(row.future.amount, row.unit, currencyCode, currencyDigits)} from {row.future.effective_from}</div>}
                    {targetErrors[id] && <div className={styles.actionErr} style={{ marginTop: '0.35rem' }}>{targetErrors[id]}</div>}
                  </td>
                  <td>
                    {editing ? <input className={styles.editInput} type="number" step="0.0001" min="0" placeholder="0.0000" value={draft.amount} onChange={(e) => setTargetField(id, 'amount', e.target.value)} /> : row.current ? fmtAmount(row.current.amount, row.unit, currencyCode, currencyDigits) : <span className={styles.missingText}>No rate set</span>}
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
                      <button className={styles.btnApprove} disabled={busy || targetSavingId != null || draft.amount === ''} onClick={() => void saveTargetRow(row, true)}>{busy ? 'Saving…' : 'Save & Approve'}</button>
                      <button className={styles.btnSecondary} disabled={busy || targetSavingId != null || draft.amount === ''} onClick={() => void saveTargetRow(row, false)}>Save as Pending</button>
                      <button className={styles.btnSecondary} disabled={busy} onClick={() => cancelTargetEdit(id)}>Cancel</button>
                    </div> : <button className={styles.btnSecondary} disabled={targetSavingId != null || !referenceDate} onClick={() => startTargetEdit(row)}>Edit rate</button>}
                  </td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      ) : (
        <div className={styles.emptySection}>
          <p>No pay items are available to rate yet.</p>
          <p className={styles.emptySectionHint}>Pay items are created and activated per branch under <strong>Settings &gt; Pay Items</strong>.</p>
        </div>
      )}
    </div>
  );
}
