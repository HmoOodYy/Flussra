import type { Dispatch, SetStateAction } from 'react';
import type { HistoryRow } from './payRatesView';
import { fmtAmount, statusBadgeClass } from './payRatesView';
import { RETIRED_LABEL, canVoidHistoryRow } from './payRatesHistory';
import styles from './PayRatesPage.module.css';

type RateHistoryTabProps = {
  historyRows: HistoryRow[];
  loadingHistory: boolean;
  canEditMatrix: boolean;
  currencyCode: string | null;
  currencyDigits: number | null;
  setVoidTarget: Dispatch<SetStateAction<HistoryRow | null>>;
  setVoidReason: (reason: string) => void;
  setVoidError: (error: string) => void;
};

export function RateHistoryTab({ historyRows, loadingHistory, canEditMatrix, currencyCode, currencyDigits, setVoidTarget, setVoidReason, setVoidError }: RateHistoryTabProps) {
  return (
    <div className={styles.section}>
      <div className={styles.sectionHead}><span className={styles.sectionTitle}>Rate History</span></div>
      {loadingHistory ? (
        <div className={styles.loading}>Loading history...</div>
      ) : historyRows.length === 0 ? (
        <div className={styles.emptySection}>No rate history for this driver.</div>
      ) : (
        <table className={styles.rateTable}>
          <thead><tr><th>Pay Item</th><th>Amount</th><th>Effective</th><th>Status</th>{canEditMatrix && <th>Actions</th>}</tr></thead>
          <tbody>
            {historyRows.map((r) => (
              <tr key={r.driver_rate_schedule_id}>
                <td><strong>{r.definition_name}</strong>{r.retired && <span className={styles.statusBadge} style={{ marginLeft: '0.4rem' }}>{RETIRED_LABEL}</span>}{r.void_reason && <div style={{ fontSize: '0.74rem', color: '#6b7280' }}>{r.void_reason}</div>}</td>
                <td>{fmtAmount(r.values[0]?.amount ?? null, r.unit, currencyCode, currencyDigits)}</td>
                <td>{r.effective_from}{r.effective_to ? ` – ${r.effective_to}` : ''}</td>
                <td><span className={statusBadgeClass(r.status)}>{r.status}</span></td>
                {canEditMatrix && <td>{canVoidHistoryRow(r, canEditMatrix) && <button className={styles.btnVoid} onClick={() => { setVoidTarget(r); setVoidReason(''); setVoidError(''); }}>Void</button>}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

type VoidRateDialogProps = {
  voidTarget: HistoryRow | null;
  voiding: boolean;
  voidReason: string;
  voidError: string;
  setVoidTarget: Dispatch<SetStateAction<HistoryRow | null>>;
  setVoidReason: (reason: string) => void;
  executeVoid: () => Promise<void>;
};

export function VoidRateDialog({ voidTarget, voiding, voidReason, voidError, setVoidTarget, setVoidReason, executeVoid }: VoidRateDialogProps) {
  return voidTarget && (
    <div className={styles.modalOverlay} onClick={() => { if (!voiding) setVoidTarget(null); }}>
      <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
        <div className={styles.modalHead}>
          <strong>Void rate</strong>
          <button className={styles.modalClose} onClick={() => setVoidTarget(null)} disabled={voiding}>✕</button>
        </div>
        <div className={styles.modalBody}>
          <p className={styles.modalDesc}>
            Voiding the {voidTarget.status} rate for <strong>{voidTarget.definition_name}</strong>{' '}
            ({voidTarget.effective_from}) removes it from rate resolution. It is kept as history.
          </p>
          <label className={styles.formLabel} htmlFor="void-reason">Reason</label>
          <input id="void-reason" className={styles.editInput} style={{ width: '100%' }} type="text" value={voidReason} onChange={(e) => setVoidReason(e.target.value)} disabled={voiding} />
          {voidError && <div className={styles.actionErr}>{voidError}</div>}
        </div>
        <div className={styles.modalFoot}>
          <button className={styles.btnPrimary} disabled={voiding || !voidReason.trim()} onClick={() => void executeVoid()}>{voiding ? 'Voiding…' : 'Void Rate'}</button>
          <button className={styles.btnSecondary} onClick={() => setVoidTarget(null)} disabled={voiding}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
