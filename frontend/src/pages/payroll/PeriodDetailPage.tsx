/**
 * PeriodDetailPage — /payroll/periods/:periodId
 *
 * Focused daily-entry view for a single payroll period.
 * Period-level actions (Bonus, Drivers Off, Open/Submit/Finalize) live on the
 * Current Payroll hub; this page handles the daily grid only.
 */
import { useParams, Link } from 'react-router-dom';
import { useEffect } from 'react';
import { PeriodStatusBadge } from '../../components/StatusBadge';
import { useAuth } from '../../store/authStore';
import { columnKey, daySaveErrorMessage, quantityInputProps, quantityTotalViews } from './dayGridModel';
import { StatusCellInput } from './StatusCellInput';
import { formatDayLabel, useDailyEntryController } from './useDailyEntryController';
import { formatGrossTotal } from './grossDisplay';
import styles from './PeriodDetailPage.module.css';

// ---------------------------------------------------------------------------
// Stage B3 Unit 8C-4: Locked/Archived read Status from immutable
// calculation-snapshot evidence (DayGridResponse.status_evidence), never
// from the live StatusKey list -- a key may have been renamed, deactivated,
// or had its off-reason flag changed since the period was finalized.
const FINALIZED_STATUSES = new Set(['Locked', 'Archived']);

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

const pageSaveError = daySaveErrorMessage;

export function PeriodDetailPage() {
  const { periodId } = useParams<{ periodId: string }>();
  const numericPeriodId = Number(periodId);

  const { user } = useAuth();

  const {
    selectedDate, grid, loading, error, dirtyRows, saving, saveError,
    notesOpenDriverAssignmentId, setNotesOpenDriverAssignmentId, fetchGrid, atStart, atEnd, prevDay, nextDay,
    isEditable, canEdit, handleSave, handleCellChange, handleStatusChange, handleNotesChange,
    getCellValue, getStatusText, getNotesValue,
  } = useDailyEntryController({ periodId: numericPeriodId, user, formatSaveError: pageSaveError });

  useEffect(() => {
    if (!periodId) return;
    void fetchGrid(selectedDate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDate, numericPeriodId]);


  // ── Render ─────────────────────────────────────────────────────────────── //
  if (loading && !grid) return <div className={styles.loading}>Loading…</div>;
  if (error && !grid) return <div className={styles.error}>{error}</div>;
  if (!grid) return null;

  const { period, columns, status_keys, rows, summary, status_evidence } = grid;
  const isFinalized = FINALIZED_STATUSES.has(period.status);
  // AVAILABLE and EMPTY are both trustworthy historical reads (EMPTY is a
  // positively-captured "nobody had a Status" snapshot, not a gap) -- only
  // UNAVAILABLE means the historical Status genuinely cannot be shown.
  const isHistoricalStatusReadable =
    !!status_evidence && (status_evidence.state === 'AVAILABLE' || status_evidence.state === 'EMPTY');

  const readOnlyBanner = (() => {
    if (period.status === 'Draft')
      return 'This period is Prepared — operational source entry is available, but it cannot be submitted until the backend workflow promotes it to Open.';
    if (period.status === 'InReview')
      return 'This period is in review — grid is read-only while awaiting approval.';
    if (period.status === 'Returned')
      return 'This period was returned for correction. Update its live source entries, then resubmit it for review.';
    if (period.status === 'Approved')
      return 'This period is Approved — read-only. Finalize from the payroll hub.';
    if (!isEditable)
      return `This period is ${period.status} — read-only.`;
    return null;
  })();

  return (
    <div className={styles.page}>

      {/* ── Header ──────────────────────────────────────────────────────── */}
      <div className={styles.headerRow}>
        <Link to="/payroll/periods" className={styles.backLink}>&larr; Periods</Link>
        <h2 className={styles.title}>{period.period_name}</h2>
        <PeriodStatusBadge status={period.status} />
      </div>

      {/* ── Meta ────────────────────────────────────────────────────────── */}
      <div className={styles.metaGrid}>
        <div className={styles.metaCell}>
          <span className={styles.metaLabel}>Branch:</span>
          <span className={styles.metaValue}>{period.branch_name}</span>
        </div>
        <div className={styles.metaCell}>
          <span className={styles.metaLabel}>Period:</span>
          <span className={styles.metaValue}>{period.start_date} – {period.end_date}</span>
        </div>
        {period.pay_date && (
          <div className={styles.metaCell}>
            <span className={styles.metaLabel}>Pay Date:</span>
            <span className={styles.metaValue}>{period.pay_date}</span>
          </div>
        )}
      </div>

      {/* ── Read-only / status banner ────────────────────────────────────── */}
      {readOnlyBanner && (
        <div className={styles.readOnlyBanner}>{readOnlyBanner}</div>
      )}

      {/* ── Day navigation ───────────────────────────────────────────────── */}
      <div className={styles.dayNav}>
        <button
          className={styles.dayNavBtn}
          onClick={prevDay}
          disabled={atStart || loading}
          aria-label="Previous day"
        >
          &#8592;
        </button>
        <span className={styles.dayNavLabel}>
          {selectedDate ? formatDayLabel(selectedDate) : '…'}
        </span>
        <button
          className={styles.dayNavBtn}
          onClick={nextDay}
          disabled={atEnd || loading}
          aria-label="Next day"
        >
          &#8594;
        </button>
      </div>

      {/* ── Summary bar ──────────────────────────────────────────────────── */}
      <div className={styles.summaryBar}>
        <div className={styles.summaryItem}>
          <span className={styles.summaryLabel}>Drivers:</span>
          <span className={styles.summaryValue}>{summary.total_drivers}</span>
        </div>
        <div className={styles.summaryItem}>
          <span className={styles.summaryLabel}>Worked:</span>
          <span className={styles.summaryValue}>{summary.worked}</span>
        </div>
        <div className={styles.summaryItem}>
          <span className={styles.summaryLabel}>PTO:</span>
          <span className={styles.summaryValue}>{summary.pto}</span>
        </div>
        <div className={styles.summaryItem}>
          <span className={styles.summaryLabel}>Off:</span>
          <span className={styles.summaryValue}>{summary.off}</span>
        </div>
        {quantityTotalViews(columns, summary).map((total) => (
          <div className={styles.summaryItem} key={total.key}>
            <span className={styles.summaryLabel}>{total.label}:</span>
            <span className={styles.summaryValue}>
              {total.quantity}{total.unit ? ` ${total.unit}` : ''}
            </span>
          </div>
        ))}
        <div className={styles.summaryItem}>
          <span className={styles.summaryLabel}>Gross:</span>
          <span className={styles.summaryValue}>{summary.financials_available
            ? formatGrossTotal(summary.gross_total, user?.currency_code, user?.currency_minor_unit_digits)
            : 'Not available'}</span>
        </div>
        {summary.needs_attention > 0 && (
          <div className={styles.summaryItem}>
            <span className={styles.attentionIcon}>&#9888;</span>
            <span className={styles.summaryLabel}>Needs attention:</span>
            <span className={styles.summaryValue}>{summary.needs_attention}</span>
          </div>
        )}
      </div>

      {/* ── Grid ─────────────────────────────────────────────────────────── */}
      {loading ? (
        <div className={styles.loading}>Loading…</div>
      ) : (
        <div className={styles.gridWrapper}>
          <table className={styles.grid}>
            <thead>
              <tr>
                <th className={styles.driverCol}>Driver</th>
                <th>Status</th>
                {columns.map((col) => (
                  <th key={columnKey(col)}>{col.label}{col.unit && <small> ({col.unit})</small>}</th>
                ))}
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const isDirty = dirtyRows.has(row.driver_assignment_id);
                const isOff = row.is_off;
                return (
                  <tr key={row.driver_assignment_id} className={isOff ? styles.offRow : undefined}>
                    <td className={styles.driverName}>
                      {row.driver_name}
                      {isDirty && <span style={{ color: '#f59e0b', marginLeft: 4 }}>●</span>}
                    </td>
                    <td>
                      {isFinalized ? (
                        isHistoricalStatusReadable ? (
                          <span className={styles.statusHistorical}>
                            {row.status_label ?? '—'}
                          </span>
                        ) : (
                          <span
                            className={styles.statusUnavailable}
                            title={
                              status_evidence?.reason_code
                                ? `Historical Status unavailable (${status_evidence.reason_code})`
                                : 'Historical Status unavailable'
                            }
                          >
                            Unavailable
                          </span>
                        )
                      ) : (
                        <StatusCellInput
                          className={styles.statusSelect}
                          shownText={getStatusText(row)}
                          statusKeys={status_keys}
                          disabled={!canEdit}
                          onCommit={(text) => handleStatusChange(row.driver_assignment_id, text)}
                        />
                      )}
                    </td>
                    {columns.map((col) => {
                      const key = columnKey(col);
                      const val = row.values[key];
                      const nmr = val?.needs_manager_review ?? false;
                      const inputProps = quantityInputProps(col);
                      return (
                        <td key={key}>
                          {nmr && (
                            <span className={styles.attentionIcon} title="Needs attention: rate unresolved">
                              &#9888;
                            </span>
                          )}
                          <input
                            className={styles.qtyInput}
                            type="number"
                            step={inputProps.step}
                            min={inputProps.min}
                            value={getCellValue(row, key)}
                            disabled={!canEdit}
                            onChange={(e) => handleCellChange(row.driver_assignment_id, key, e.target.value)}
                          />
                        </td>
                      );
                    })}
                    <td>
                      {notesOpenDriverAssignmentId === row.driver_assignment_id ? (
                        <input
                          type="text"
                          style={{ width: 150, fontSize: '0.78rem', padding: '0.2rem' }}
                          value={getNotesValue(row)}
                          disabled={!canEdit}
                          onChange={(e) => handleNotesChange(row.driver_assignment_id, e.target.value)}
                          onBlur={() => setNotesOpenDriverAssignmentId(null)}
                          autoFocus
                        />
                      ) : (
                        <button
                          className={`${styles.notesBtn} ${getNotesValue(row) ? styles.notesBtnActive : ''}`}
                          onClick={() => canEdit ? setNotesOpenDriverAssignmentId(row.driver_assignment_id) : undefined}
                          title={getNotesValue(row) || (canEdit ? 'Add note' : 'Read-only')}
                        >
                          {getNotesValue(row) ? '📝' : (canEdit ? '+' : '—')}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* ── Save bar ──────────────────────────────────────────────────────── */}
      {canEdit && (
        <div className={styles.saveBar}>
          <button
            className={styles.saveBtn}
            disabled={dirtyRows.size === 0 || saving}
            onClick={() => void handleSave()}
          >
            {saving ? 'Saving…' : 'Save Changes'}
          </button>
          {dirtyRows.size > 0 && (
            <span className={styles.dirtyCount}>{dirtyRows.size} driver(s) unsaved</span>
          )}
          {saveError && <span className={styles.saveError}>{saveError}</span>}
        </div>
      )}
    </div>
  );
}
