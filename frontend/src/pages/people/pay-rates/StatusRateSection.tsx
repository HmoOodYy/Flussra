import type { StatusRateMatrix } from './statusRatesApi';
import { fmtAmount } from './payRatesView';
import styles from './PayRatesPage.module.css';

type StatusRateSectionProps = {
  statusMatrix: StatusRateMatrix | null;
  canEditMatrix: boolean;
  referenceDate: string | undefined;
  currencyCode: string | null;
  currencyDigits: number | null;
  statusEditMode: boolean;
  statusDrafts: Record<string, { amount: string; effective_from: string }>;
  statusSaving: boolean;
  statusSaveError: string;
  dirtyStatusKeys: string[];
  statusKey: (group: StatusRateMatrix['groups'][number]) => string;
  startStatusEdit: () => void;
  setStatusField: (key: string, field: 'amount' | 'effective_from', value: string) => void;
  isStatusDirty: (key: string) => boolean;
  saveStatusRates: () => Promise<void>;
  cancelStatusEdit: () => void;
};

export function StatusRateSection({ statusMatrix, canEditMatrix, referenceDate, currencyCode, currencyDigits, statusEditMode, statusDrafts, statusSaving, statusSaveError, dirtyStatusKeys, statusKey, startStatusEdit, setStatusField, isStatusDirty, saveStatusRates, cancelStatusEdit }: StatusRateSectionProps) {
  return (statusMatrix?.groups.length ?? 0) > 0 ? (
    <div className={styles.section}>
      <div className={styles.sectionHead}>
        <span className={styles.sectionTitle}>Status Pay Rates</span>
        {canEditMatrix && !statusEditMode && <button className={styles.btnSecondary} disabled={!referenceDate} onClick={startStatusEdit}>Edit Status Rates</button>}
      </div>
      {statusSaveError && <div className={styles.errBanner} style={{ margin: '0.75rem 1.25rem 0' }}>{statusSaveError}</div>}
      <table className={styles.rateTable}>
        <thead><tr><th>Rate</th><th>Amount</th><th>Effective From</th><th>Status</th></tr></thead>
        <tbody>
          {statusMatrix!.groups.map((g) => {
            const key = statusKey(g);
            return (
              <tr key={g.group_key}>
                <td><strong>{g.rate_name}</strong></td>
                <td>{statusEditMode ? <input className={styles.editInput} type="number" step="0.0001" min="0" placeholder="0.0000" value={statusDrafts[key]?.amount ?? ''} onChange={(e) => setStatusField(key, 'amount', e.target.value)} /> : g.current_rate ? fmtAmount(g.current_rate.amount, g.unit_name, currencyCode, currencyDigits) : <span className={styles.missingText}>No rate set</span>}</td>
                <td>{statusEditMode ? <input className={styles.editDateInput} type="date" value={statusDrafts[key]?.effective_from ?? ''} onChange={(e) => setStatusField(key, 'effective_from', e.target.value)} /> : g.current_rate ? g.current_rate.effective_from : '—'}</td>
                <td>
                  {g.pending_rate && !statusEditMode && <span className={styles.pendingBadge} style={{ marginRight: '0.4rem' }}>Pending</span>}
                  {!statusEditMode && (g.current_rate ? <span className={styles.okIcon}>&#10003;</span> : <span className={styles.missingIcon}>&#9888;</span>)}
                  {statusEditMode && isStatusDirty(key) && <span className={styles.pendingBadge}>Edited</span>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {statusEditMode && <div className={styles.saveBar}>
        <button className={styles.btnPrimary} onClick={() => void saveStatusRates()} disabled={statusSaving || dirtyStatusKeys.length === 0}>{statusSaving ? 'Saving...' : `Save ${dirtyStatusKeys.length} Status Rate Change${dirtyStatusKeys.length !== 1 ? 's' : ''}`}</button>
        <button className={styles.btnSecondary} onClick={cancelStatusEdit} disabled={statusSaving}>Cancel</button>
        <span style={{ fontSize: '0.74rem', color: '#6b7280' }}>Status rates are saved together on the temporary Status rate path, separately from Pay Item rates.</span>
      </div>}
    </div>
  ) : null;
}
