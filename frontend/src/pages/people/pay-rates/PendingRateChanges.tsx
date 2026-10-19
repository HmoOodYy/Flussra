import { approveSchedule, discardSchedule } from '../../../lib/compensationApi';
import type { DriverPayRateRow } from '../../../types/compensation';
import { fmtAmount } from './payRatesView';
import { approveStatusRate, voidStatusRate, type StatusRateGroup } from './statusRatesApi';
import styles from './PayRatesPage.module.css';

type PendingRateChangesProps = {
  pendingRows: DriverPayRateRow[];
  statusPending: StatusRateGroup[];
  pendingCount: number;
  /** Approve a Pending rate: the backend's full history, a live tenure and edit permission. */
  canEditDriver: boolean;
  /** Discard a Pending rate: the backend's discard authority (kept when the rate is orphaned). */
  canDiscardDriver: boolean;
  /** Status rates: authority follows the viewed assignment. */
  canEditStatus: boolean;
  actioningId: number | null;
  pendingActionError: string;
  currencyCode: string | null;
  currencyDigits: number | null;
  runPendingAction: (id: number, action: () => Promise<unknown>) => Promise<void>;
};

export function PendingRateChanges({ pendingRows, statusPending, pendingCount, canEditDriver, canDiscardDriver, canEditStatus, actioningId, pendingActionError, currencyCode, currencyDigits, runPendingAction }: PendingRateChangesProps) {
  return (
    <div className={styles.section}>
      <div className={styles.sectionHead}><span className={styles.sectionTitle}>Pending Changes</span></div>
      {pendingActionError && <div className={styles.actionErr}>{pendingActionError}</div>}
      {pendingCount === 0 ? (
        <div className={styles.emptySection}>No pending rate changes for this driver.</div>
      ) : (
        <table className={styles.rateTable}>
          <thead><tr><th>Pay Item</th><th>Current</th><th>Pending Amount</th><th>Effective From</th>{(canEditDriver || canDiscardDriver || canEditStatus) && <th>Actions</th>}</tr></thead>
          <tbody>
            {pendingRows.map((row) => {
              const pending = row.pending!;
              const busy = actioningId === pending.driver_rate_schedule_id;
              return (
                <tr key={pending.driver_rate_schedule_id}>
                  <td><strong>{row.definition_name}</strong></td>
                  <td>{row.current ? fmtAmount(row.current.amount, row.unit, currencyCode, currencyDigits) : <span className={styles.missingText}>No current rate</span>}</td>
                  <td>{pending.amount === null ? <span className={styles.missingText}>Not set</span> : <strong>{fmtAmount(pending.amount, row.unit, currencyCode, currencyDigits)}</strong>}</td>
                  <td>{pending.effective_from}</td>
                  {(canEditDriver || canDiscardDriver || canEditStatus) && <td>{(canEditDriver || canDiscardDriver) && <div className={styles.actionBtns}>
                    {canEditDriver && <button className={styles.btnApprove} disabled={busy || actioningId != null || pending.amount === null} onClick={() => void runPendingAction(pending.driver_rate_schedule_id, () => approveSchedule(pending.driver_rate_schedule_id))}>{busy ? '…' : 'Approve'}</button>}
                    {canDiscardDriver && <button className={styles.btnVoid} disabled={busy || actioningId != null} onClick={() => void runPendingAction(pending.driver_rate_schedule_id, () => discardSchedule(pending.driver_rate_schedule_id))}>{busy ? '…' : 'Discard'}</button>}
                  </div>}</td>}
                </tr>
              );
            })}
            {statusPending.map((g) => {
              const rate = g.pending_rate!;
              const busy = actioningId === rate.driver_rate_id;
              return (
                <tr key={`s${rate.driver_rate_id}`}>
                  <td><strong>{g.rate_name}</strong> <span className={styles.statusBadge}>Branch status rate</span></td>
                  <td>{g.current_rate ? fmtAmount(g.current_rate.amount, g.unit_name, currencyCode, currencyDigits) : <span className={styles.missingText}>No current rate</span>}</td>
                  <td><strong>{fmtAmount(rate.amount, g.unit_name, currencyCode, currencyDigits)}</strong></td>
                  <td>{rate.effective_from}</td>
                  {(canEditDriver || canDiscardDriver || canEditStatus) && <td>{canEditStatus && <div className={styles.actionBtns}>
                    <button className={styles.btnApprove} disabled={busy || actioningId != null} onClick={() => void runPendingAction(rate.driver_rate_id, () => approveStatusRate(rate.driver_rate_id))}>{busy ? '…' : 'Approve'}</button>
                    <button className={styles.btnVoid} disabled={busy || actioningId != null} onClick={() => void runPendingAction(rate.driver_rate_id, () => voidStatusRate(rate.driver_rate_id))}>{busy ? '…' : 'Void'}</button>
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
