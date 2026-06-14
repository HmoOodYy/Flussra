/**
 * DriversOffDialog — period-level off-drivers modal.
 *
 * Shows all DailyStatus lines with IsOffReason=TRUE for the entire period,
 * across all work dates. Uses the new GET /payroll/periods/{id}/drivers-off
 * endpoint added in CP-2.5.
 */
import { useEffect, useState } from 'react';
import { getDriversOff } from '../../lib/payrollApi';
import type { DriversOffEntry } from '../../types/payroll';
import styles from './DriversOffDialog.module.css';

// ---------------------------------------------------------------------------
// Date helpers
// ---------------------------------------------------------------------------

function formatWeekday(dateStr: string): string {
  const dt = new Date(dateStr + 'T00:00:00');
  return dt.toLocaleDateString('en-US', { weekday: 'long' });
}

function formatDate(dateStr: string): string {
  const dt = new Date(dateStr + 'T00:00:00');
  return dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface DriversOffDialogProps {
  periodId: number;
  periodName?: string;
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function DriversOffDialog({ periodId, periodName, onClose }: DriversOffDialogProps) {
  const [entries, setEntries] = useState<DriversOffEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);
    getDriversOff(periodId)
      .then((data) => setEntries(data.entries))
      .catch(() => setError('Failed to load drivers off data.'))
      .finally(() => setLoading(false));
  }, [periodId]);

  // Close on Escape
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div
        className={styles.dialog}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Drivers off"
      >
        {/* Header */}
        <div className={styles.dialogHeader}>
          <h3 className={styles.dialogTitle}>
            Drivers Off{periodName ? ` — ${periodName}` : ''}
          </h3>
          <button className={styles.closeBtn} onClick={onClose} aria-label="Close">
            &#x2715;
          </button>
        </div>

        {/* Body */}
        <div className={styles.dialogBody}>
          {loading ? (
            <div className={styles.stateMsg}>Loading…</div>
          ) : error ? (
            <div className={styles.errorMsg}>{error}</div>
          ) : entries.length === 0 ? (
            <div className={styles.emptyMsg}>No drivers marked off for this period.</div>
          ) : (
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Driver</th>
                  <th>Day</th>
                  <th>Date</th>
                  <th>Status</th>
                  <th>Notes</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => (
                  <tr key={i}>
                    <td className={styles.driverCell}>{e.driver_name}</td>
                    <td className={styles.weekdayCell}>{formatWeekday(e.work_date)}</td>
                    <td className={styles.dateCell}>{formatDate(e.work_date)}</td>
                    <td>{e.status_label ?? e.status_key_code}</td>
                    <td className={styles.notesCell}>{e.notes ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
