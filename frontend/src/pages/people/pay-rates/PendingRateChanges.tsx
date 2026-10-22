import { approveSchedule, discardSchedule } from '../../../lib/compensationApi';
import { fmtAmount } from './payRatesView';
import { RETIRED_LABEL } from './payRatesHistory';
import { canApprovePendingRow, canDiscardPendingRow, type PendingScheduleRow } from './payRatesPending';
import styles from './PayRatesPage.module.css';

type PendingRateChangesProps = {
  pendingRows: PendingScheduleRow[];
  pendingCount: number;
  /** Approve a Pending rate: the backend's full history, a live tenure and edit permission. */
  canEditDriver: boolean;
  /** Discard a Pending rate: the backend's discard authority (kept when the rate is orphaned). */
  canDiscardDriver: boolean;
  actioningId: number | null;
  pendingActionError: string;
  currencyCode: string | null;
  currencyDigits: number | null;
  runPendingAction: (id: number, action: () => Promise<unknown>) => Promise<void>;
};

export function PendingRateChanges({ pendingRows, pendingCount, canEditDriver, canDiscardDriver, actioningId, pendingActionError, currencyCode, currencyDigits, runPendingAction }: PendingRateChangesProps) {
  return (
    <div className={styles.section}>
      <div className={styles.sectionHead}><span className={styles.sectionTitle}>Pending Changes</span></div>
      {pendingActionError && <div className={styles.actionErr}>{pendingActionError}</div>}
      {pendingCount === 0 ? (
        <div className={styles.emptySection}>No pending rate changes for this driver.</div>
      ) : (
        <table className={styles.rateTable}>
          <thead><tr><th>Rate</th><th>Current</th><th>Pending Amount</th><th>Effective From</th>{(canEditDriver || canDiscardDriver) && <th>Actions</th>}</tr></thead>
          <tbody>
            {pendingRows.map((row) => {
              const busy = actioningId === row.scheduleId;
              const approve = canApprovePendingRow(row, canEditDriver);
              const discard = canDiscardPendingRow(canDiscardDriver);
              return (
                <tr key={row.scheduleId}>
                  <td>
                    <strong>{row.definitionName}</strong>
                    {row.kind === 'StatusRateColumn' && <span className={styles.statusBadge} style={{ marginLeft: '0.4rem' }}>Status rate</span>}
                    {row.retired && <span className={styles.statusBadge} style={{ marginLeft: '0.4rem' }}>{RETIRED_LABEL}</span>}
                  </td>
                  <td>{!row.inMatrix ? '—' : row.current ? fmtAmount(row.current.amount, row.unit, currencyCode, currencyDigits) : <span className={styles.missingText}>No current rate</span>}</td>
                  <td>{row.amount === null ? <span className={styles.missingText}>Not set</span> : <strong>{fmtAmount(row.amount, row.unit, currencyCode, currencyDigits)}</strong>}</td>
                  <td>{row.effectiveFrom}</td>
                  {(canEditDriver || canDiscardDriver) && <td>{(approve || discard) && <div className={styles.actionBtns}>
                    {approve && <button className={styles.btnApprove} disabled={busy || actioningId != null || row.amount === null} onClick={() => void runPendingAction(row.scheduleId, () => approveSchedule(row.scheduleId))}>{busy ? '…' : 'Approve'}</button>}
                    {discard && <button className={styles.btnVoid} disabled={busy || actioningId != null} onClick={() => void runPendingAction(row.scheduleId, () => discardSchedule(row.scheduleId))}>{busy ? '…' : 'Discard'}</button>}
                  </div>}</td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
