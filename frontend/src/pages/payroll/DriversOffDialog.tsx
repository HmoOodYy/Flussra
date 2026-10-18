/**
 * DriversOffDialog — period-level off-drivers modal.
 *
 * Shows CP-5B's authoritative selected-day Off-driver response.
 */
import { useEffect, useRef, useState } from 'react';
import { getSelectedDayOffDrivers } from '../../lib/payrollApi';
import { formatIsoShort } from '../../lib/isoDate';
import type { SelectedDayOffDriver } from '../../types/payroll/entry';
import styles from './DriversOffDialog.module.css';

// ---------------------------------------------------------------------------
// Date helpers
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface DriversOffDialogProps {
  periodId: number;
  periodName?: string;
  periodStartDate: string;
  periodEndDate: string;
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function DriversOffDialog({
  periodId,
  periodName,
  periodStartDate,
  periodEndDate,
  onClose,
}: DriversOffDialogProps) {
  const [workDate, setWorkDate] = useState<string | null>(null);
  const [entries, setEntries] = useState<SelectedDayOffDriver[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [responseDate, setResponseDate] = useState(workDate);
  const [responseDayName, setResponseDayName] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestVersion = useRef(0);

  const boundedWorkDate = workDate == null || workDate === '' ? undefined : workDate < periodStartDate
    ? periodStartDate
    : workDate > periodEndDate
      ? periodEndDate
      : workDate;

  useEffect(() => {
    const version = ++requestVersion.current;
    // Fetch lifecycle state is intentionally synchronized with the selected date request.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);
    getSelectedDayOffDrivers(periodId, boundedWorkDate)
      .then((data) => {
        if (version !== requestVersion.current) return;
        setEntries(data.drivers);
        setTotalCount(data.total_count);
        setResponseDate(data.work_date);
        setResponseDayName(data.day_name);
      })
      .catch((err: unknown) => {
        const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
        if (version === requestVersion.current) setError(typeof detail === 'string' ? detail : 'Failed to load drivers off data.');
      })
      .finally(() => {
        if (version === requestVersion.current) setLoading(false);
      });
    return () => {
      requestVersion.current += 1;
    };
  }, [periodId, boundedWorkDate]);

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
          <div className={styles.controls}>
            <label htmlFor="drivers-off-work-date">Work date</label>
            <input
              id="drivers-off-work-date"
              className={styles.dateInput}
              type="date"
              min={periodStartDate}
              max={periodEndDate}
              value={boundedWorkDate ?? responseDate ?? ''}
              onChange={(event) => setWorkDate(event.target.value)}
            />
            {!loading && !error && responseDate && (
              <span className={styles.resultSummary}>
                {responseDayName} · {formatIsoShort(responseDate)} · {totalCount} driver{totalCount === 1 ? '' : 's'}
              </span>
            )}
          </div>
          {loading ? (
            <div className={styles.stateMsg}>Loading…</div>
          ) : error ? (
            <div className={styles.errorMsg}>{error}</div>
          ) : entries.length === 0 ? (
            <div className={styles.emptyMsg}>No drivers marked off for this day.</div>
          ) : (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>Driver</th>
                    <th>Code</th>
                    <th>Status</th>
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((e) => (
                    <tr key={e.driver_assignment_id}>
                      <td className={styles.driverCell}>{e.driver_name}</td>
                      <td className={styles.codeCell}>{e.driver_number ?? '—'}</td>
                      <td>{e.status_label ?? e.status_code ?? '—'}</td>
                      <td className={styles.notesCell}>{e.note ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
