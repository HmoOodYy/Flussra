/**
 * PayrollEntryDialog — large workbench modal containing the daily entry grid.
 *
 * Extracted from PeriodDetailPage.tsx. Opens with a periodId prop, fetches
 * its own data via getDayGrid, and cleans up on close.
 */
import { useEffect } from 'react';
import { PeriodStatusBadge } from '../../components/StatusBadge';
import { useAuth } from '../../store/authStore';
import { columnKey, quantityInputProps, quantityTotalViews } from './dayGridModel';
import { formatDayLabel, useDailyEntryController } from './useDailyEntryController';
import { formatGrossTotal } from './grossDisplay';
import styles from './PayrollEntryDialog.module.css';

function dialogSaveError(e: unknown): string {
  return (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
    'Save failed.';
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface PayrollEntryDialogProps {
  periodId: number;
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function PayrollEntryDialog({ periodId, onClose }: PayrollEntryDialogProps) {
  const { user } = useAuth();

  const {
    selectedDate, grid, loading, error, dirtyRows, saving, saveError,
    notesOpenDriverAssignmentId, setNotesOpenDriverAssignmentId, fetchGrid, atStart, atEnd, prevDay, nextDay,
    isEditable, canEdit, handleSave, handleCellChange, handleStatusChange, handleNotesChange,
    getCellValue, getStatusValue, getNotesValue,
  } = useDailyEntryController({ periodId, user, formatSaveError: dialogSaveError });

  useEffect(() => {
    void fetchGrid(selectedDate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDate, periodId]);

  // ── Close on Escape ──────────────────────────────────────────────────────── //
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  // ── Render ──────────────────────────────────────────────────────────────── //
  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div
        className={styles.dialog}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Payroll entry workbench"
      >
        {/* ── Dialog header ──────────────────────────────────────────── */}
        <div className={styles.dialogHeader}>
          <div className={styles.headerLeft}>
            <h2 className={styles.dialogTitle}>
              {grid ? grid.period.period_name : 'Loading…'}
            </h2>
            {grid && <PeriodStatusBadge status={grid.period.status} />}
          </div>
          <button className={styles.closeBtn} onClick={onClose} aria-label="Close">
            &#x2715;
          </button>
        </div>

        {/* ── Dialog body ────────────────────────────────────────────── */}
        <div className={styles.dialogBody}>

          {/* Period meta */}
          {grid && (
            <div className={styles.metaRow}>
              <span className={styles.metaItem}>
                <span className={styles.metaLabel}>Branch:</span>
                <span>{grid.period.branch_name}</span>
              </span>
              <span className={styles.metaItem}>
                <span className={styles.metaLabel}>Period:</span>
                <span>{grid.period.start_date} – {grid.period.end_date}</span>
              </span>
              {grid.period.pay_date && (
                <span className={styles.metaItem}>
                  <span className={styles.metaLabel}>Pay Date:</span>
                  <span>{grid.period.pay_date}</span>
                </span>
              )}
            </div>
          )}

          {/* Status / read-only banner */}
          {grid && (() => {
            const s = grid.period.status;
            if (s === 'Draft')
              return <div className={styles.readOnlyBanner}>This period is <strong>Prepared</strong> — operational source entry is available, but it cannot be submitted until the backend workflow promotes it to Open.</div>;
            if (s === 'InReview')
              return <div className={styles.readOnlyBanner}>This period is <strong>In Review</strong> — grid is read-only while awaiting approval.</div>;
            if (s === 'Returned')
              return <div className={styles.readOnlyBanner}>This period was <strong>returned for correction</strong>. Update its live source entries, then resubmit it for review.</div>;
            if (s === 'Approved')
              return <div className={styles.readOnlyBanner}>This period is <strong>Approved</strong> — read-only. Finalize from the payroll hub.</div>;
            if (!isEditable)
              return <div className={styles.readOnlyBanner}>This period is <strong>{s}</strong> and cannot be edited.</div>;
            return null;
          })()}

          {/* Day navigation */}
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

          {/* Summary bar */}
          {grid && (
            <div className={styles.summaryBar}>
              <span className={styles.summaryItem}>
                <span className={styles.summaryLabel}>Drivers:</span>
                <span className={styles.summaryValue}>{grid.summary.total_drivers}</span>
              </span>
              <span className={styles.summaryItem}>
                <span className={styles.summaryLabel}>Worked:</span>
                <span className={styles.summaryValue}>{grid.summary.worked}</span>
              </span>
              <span className={styles.summaryItem}>
                <span className={styles.summaryLabel}>Off:</span>
                <span className={styles.summaryValue}>{grid.summary.off}</span>
              </span>
              {quantityTotalViews(grid.columns, grid.summary).map((total) => (
                <span className={styles.summaryItem} key={total.key}>
                  <span className={styles.summaryLabel}>{total.label}:</span>
                  <span className={styles.summaryValue}>
                    {total.quantity}{total.unit ? ` ${total.unit}` : ''}
                  </span>
                </span>
              ))}
              <span className={styles.summaryItem}>
                <span className={styles.summaryLabel}>Gross:</span>
                <span className={styles.summaryValue}>{formatGrossTotal(grid.summary.gross_total)}</span>
              </span>
              {grid.summary.needs_attention > 0 && (
                <span className={styles.summaryItem}>
                  <span className={styles.attentionIcon}>&#9888;</span>
                  <span className={styles.summaryLabel}>Needs attention:</span>
                  <span className={styles.summaryValue}>{grid.summary.needs_attention}</span>
                </span>
              )}
            </div>
          )}

          {/* Grid */}
          {loading ? (
            <div className={styles.stateMsg}>Loading…</div>
          ) : error ? (
            <div className={styles.errorMsg}>{error}</div>
          ) : grid ? (
            <div className={styles.gridWrapper}>
              <table className={styles.grid}>
                <thead>
                  <tr>
                    <th className={styles.driverCol}>Driver</th>
                    <th>Status</th>
                    {grid.columns.map((col) => (
                      <th key={columnKey(col)}>
                        {col.label}
                        {col.unit && <small> ({col.unit})</small>}
                      </th>
                    ))}
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {grid.rows.map((row) => {
                    const isDirty = dirtyRows.has(row.driver_assignment_id);
                    const isOff = row.is_off;
                    return (
                      <tr key={row.driver_assignment_id} className={isOff ? styles.offRow : undefined}>
                        <td className={styles.driverName}>
                          {row.driver_name}
                          {isDirty && <span className={styles.dirtyDot}>●</span>}
                        </td>
                        <td>
                          <select
                            className={styles.statusSelect}
                            value={getStatusValue(row)}
                            disabled={!canEdit}
                            onChange={(e) => handleStatusChange(row.driver_assignment_id, e.target.value || null)}
                          >
                            <option value="">—</option>
                            {grid.status_keys.map((sk) => (
                              <option key={sk.key_code} value={sk.key_code}>
                                {sk.label}
                              </option>
                            ))}
                          </select>
                        </td>
                        {grid.columns.map((col) => {
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
                                onChange={(e) =>
                                  handleCellChange(row.driver_assignment_id, key, e.target.value)
                                }
                              />
                            </td>
                          );
                        })}
                        <td>
                          {notesOpenDriverAssignmentId === row.driver_assignment_id ? (
                            <input
                              type="text"
                              className={styles.notesInput}
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
          ) : null}

          {/* Save bar — only shown when user has payroll.entry AND period is editable */}
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
      </div>
    </div>
  );
}
