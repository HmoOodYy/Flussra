/**
 * PayrollEntryDialog — large workbench modal containing the daily entry grid.
 *
 * Extracted from PeriodDetailPage.tsx. Opens with a periodId prop, fetches
 * its own data via getDayGrid, and cleans up on close.
 */
import { useEffect, useState, useCallback } from 'react';
import {
  getDayGrid,
  saveDayGrid,
} from '../../lib/payrollApi';
import type {
  DayGridResponse,
  DayGridRow,
  DayGridSaveRow,
} from '../../types/payroll';
import { PeriodStatusBadge } from '../../components/StatusBadge';
import { useAuth } from '../../store/authStore';
import { canEntryPayroll } from '../../lib/permissions';
import { formatGrossTotal } from './grossDisplay';
import styles from './PayrollEntryDialog.module.css';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const EDITABLE_STATUSES = new Set(['Draft', 'Open', 'Returned']);

// ---------------------------------------------------------------------------
// Date helpers
// ---------------------------------------------------------------------------

function subtractDay(d: string): string {
  const dt = new Date(d + 'T00:00:00');
  dt.setDate(dt.getDate() - 1);
  return dt.toISOString().slice(0, 10);
}

function addDay(d: string): string {
  const dt = new Date(d + 'T00:00:00');
  dt.setDate(dt.getDate() + 1);
  return dt.toISOString().slice(0, 10);
}

function formatDayLabel(d: string): string {
  const dt = new Date(d + 'T00:00:00');
  return dt.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' });
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

  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [grid, setGrid] = useState<DayGridResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [dirtyRows, setDirtyRows] = useState<Map<number, DayGridSaveRow>>(new Map());
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [notesOpenDriverId, setNotesOpenDriverId] = useState<number | null>(null);

  // ── Fetch grid ──────────────────────────────────────────────────────────── //
  const fetchGrid = useCallback(async (date: string | null) => {
    setLoading(true);
    setError(null);
    setDirtyRows(new Map());
    setSaveError(null);
    try {
      const data = await getDayGrid(periodId, date ?? undefined);
      setGrid(data);
      setSelectedDate(data.work_date);
    } catch (e: unknown) {
      const msg =
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Failed to load grid.';
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [periodId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void fetchGrid(selectedDate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDate, periodId]);

  // ── Day navigation ──────────────────────────────────────────────────────── //
  const atStart = grid && selectedDate ? selectedDate <= grid.period.start_date : true;
  const atEnd   = grid && selectedDate ? selectedDate >= grid.period.end_date   : true;

  function prevDay() {
    setSelectedDate((d) => d ? subtractDay(d) : d);
  }
  function nextDay() {
    setSelectedDate((d) => d ? addDay(d) : d);
  }

  // ── Save ────────────────────────────────────────────────────────────────── //
  const isEditable = grid ? EDITABLE_STATUSES.has(grid.period.status) : false;
  // canEdit = period status allows edits AND user has payroll.entry for this period's branch.
  // payroll.view-only users must not see editable inputs or the save bar.
  const canEdit = isEditable && grid !== null && user !== null && canEntryPayroll(user, grid.period.branch_id);

  async function handleSave() {
    if (dirtyRows.size === 0 || !grid || !selectedDate) return;
    setSaving(true);
    setSaveError(null);
    try {
      const resp = await saveDayGrid(periodId, {
        work_date: selectedDate,
        rows: Array.from(dirtyRows.values()),
      });
      setGrid(resp);
      setDirtyRows(new Map());
    } catch (e: unknown) {
      const msg =
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Save failed.';
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  // ── Cell change helpers ─────────────────────────────────────────────────── //
  function buildSaveRowFromGrid(driverId: number): DayGridSaveRow {
    const row = grid?.rows.find((r) => r.driver_id === driverId);
    return {
      driver_id: driverId,
      values: Object.fromEntries(
        Object.entries(row?.values ?? {}).map(([k, v]) => [k, v.quantity ?? '']),
      ),
      status_key: row?.status_key ?? null,
      notes: row?.notes ?? null,
    };
  }

  function handleCellChange(driverId: number, code: string, value: string) {
    setDirtyRows((prev) => {
      const m = new Map(prev);
      const existing = m.get(driverId) ?? buildSaveRowFromGrid(driverId);
      m.set(driverId, { ...existing, values: { ...existing.values, [code]: value } });
      return m;
    });
  }

  function handleStatusChange(driverId: number, statusKey: string | null) {
    setDirtyRows((prev) => {
      const m = new Map(prev);
      const existing = m.get(driverId) ?? buildSaveRowFromGrid(driverId);
      m.set(driverId, { ...existing, status_key: statusKey });
      return m;
    });
  }

  function handleNotesChange(driverId: number, notes: string) {
    setDirtyRows((prev) => {
      const m = new Map(prev);
      const existing = m.get(driverId) ?? buildSaveRowFromGrid(driverId);
      m.set(driverId, { ...existing, notes: notes || null });
      return m;
    });
  }

  function getCellValue(row: DayGridRow, code: string): string {
    const dirty = dirtyRows.get(row.driver_id);
    if (dirty) return dirty.values[code] ?? '';
    return row.values[code]?.quantity ?? '';
  }

  function getStatusValue(row: DayGridRow): string {
    const dirty = dirtyRows.get(row.driver_id);
    if (dirty) return dirty.status_key ?? '';
    return row.status_key ?? '';
  }

  function getNotesValue(row: DayGridRow): string {
    const dirty = dirtyRows.get(row.driver_id);
    if (dirty) return dirty.notes ?? '';
    return row.notes ?? '';
  }

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
              <span className={styles.summaryItem}>
                <span className={styles.summaryLabel}>Hours:</span>
                <span className={styles.summaryValue}>{grid.summary.total_hours}</span>
              </span>
              <span className={styles.summaryItem}>
                <span className={styles.summaryLabel}>Miles:</span>
                <span className={styles.summaryValue}>{grid.summary.total_miles}</span>
              </span>
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
                      <th key={col.pay_item_code}>{col.label}</th>
                    ))}
                    <th>Notes</th>
                  </tr>
                </thead>
                <tbody>
                  {grid.rows.map((row) => {
                    const isDirty = dirtyRows.has(row.driver_id);
                    const isOff = row.is_off;
                    return (
                      <tr key={row.driver_id} className={isOff ? styles.offRow : undefined}>
                        <td className={styles.driverName}>
                          {row.driver_name}
                          {isDirty && <span className={styles.dirtyDot}>●</span>}
                        </td>
                        <td>
                          <select
                            className={styles.statusSelect}
                            value={getStatusValue(row)}
                            disabled={!canEdit}
                            onChange={(e) => handleStatusChange(row.driver_id, e.target.value || null)}
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
                          const val = row.values[col.pay_item_code];
                          const nmr = val?.needs_manager_review ?? false;
                          return (
                            <td key={col.pay_item_code}>
                              {nmr && (
                                <span className={styles.attentionIcon} title="Needs manager review">
                                  &#9888;
                                </span>
                              )}
                              <input
                                className={styles.qtyInput}
                                type="number"
                                step="0.01"
                                min="0"
                                value={getCellValue(row, col.pay_item_code)}
                                disabled={!canEdit}
                                onChange={(e) =>
                                  handleCellChange(row.driver_id, col.pay_item_code, e.target.value)
                                }
                              />
                            </td>
                          );
                        })}
                        <td>
                          {notesOpenDriverId === row.driver_id ? (
                            <input
                              type="text"
                              className={styles.notesInput}
                              value={getNotesValue(row)}
                              disabled={!canEdit}
                              onChange={(e) => handleNotesChange(row.driver_id, e.target.value)}
                              onBlur={() => setNotesOpenDriverId(null)}
                              autoFocus
                            />
                          ) : (
                            <button
                              className={`${styles.notesBtn} ${getNotesValue(row) ? styles.notesBtnActive : ''}`}
                              onClick={() => canEdit ? setNotesOpenDriverId(row.driver_id) : undefined}
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
