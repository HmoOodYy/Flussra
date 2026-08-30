/**
 * PeriodDetailPage — /payroll/periods/:periodId
 *
 * Focused daily-entry view for a single payroll period.
 * Period-level actions (Bonus, Drivers Off, Open/Submit/Finalize) live on the
 * Current Payroll hub; this page handles the daily grid only.
 */
import { useParams, Link } from 'react-router-dom';
import { useEffect, useState, useCallback } from 'react';
import { getDayGrid, saveDayGrid } from '../../lib/payrollApi';
import type { DayGridResponse, DayGridRow, DayGridSaveRow } from '../../types/payroll';
import { PeriodStatusBadge } from '../../components/StatusBadge';
import { useAuth } from '../../store/authStore';
import { canEntryPayroll } from '../../lib/permissions';
import styles from './PeriodDetailPage.module.css';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const EDITABLE_STATUSES = new Set(['Open', 'Returned']);

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
// Main page
// ---------------------------------------------------------------------------

export function PeriodDetailPage() {
  const { periodId } = useParams<{ periodId: string }>();
  const numericPeriodId = Number(periodId);

  const { user } = useAuth();
  const userCanEntry = user ? canEntryPayroll(user) : false;

  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [grid, setGrid] = useState<DayGridResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [dirtyRows, setDirtyRows] = useState<Map<number, DayGridSaveRow>>(new Map());
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [notesOpenDriverId, setNotesOpenDriverId] = useState<number | null>(null);

  // ── Fetch grid ─────────────────────────────────────────────────────────── //
  const fetchGrid = useCallback(async (date: string | null) => {
    if (!periodId) return;
    setLoading(true);
    setError(null);
    setDirtyRows(new Map());
    setSaveError(null);
    try {
      const data = await getDayGrid(numericPeriodId, date ?? undefined);
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
  }, [numericPeriodId, periodId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void fetchGrid(selectedDate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedDate, numericPeriodId]);

  // ── Day navigation ─────────────────────────────────────────────────────── //
  const atStart = grid && selectedDate ? selectedDate <= grid.period.start_date : true;
  const atEnd   = grid && selectedDate ? selectedDate >= grid.period.end_date   : true;

  function prevDay() { setSelectedDate((d) => d ? subtractDay(d) : d); }
  function nextDay() { setSelectedDate((d) => d ? addDay(d) : d); }

  // ── Save ───────────────────────────────────────────────────────────────── //
  const isEditable = grid ? EDITABLE_STATUSES.has(grid.period.status) : false;
  const canEdit    = isEditable && userCanEntry;

  async function handleSave() {
    if (dirtyRows.size === 0 || !grid || !selectedDate) return;

    // Normalize time-column values to decimal strings before sending to backend.
    // If any value is invalid, block the save and surface the error.
    const timeCodes = new Set(
      grid.columns.filter((c) => c.is_time).map((c) => c.pay_item_code),
    );
    const normalizedRows: DayGridSaveRow[] = [];
    for (const row of dirtyRows.values()) {
      const normalizedValues: Record<string, string> = { ...row.values };
      for (const [code, val] of Object.entries(row.values)) {
        if (!timeCodes.has(code)) continue;
        if (val === '') continue;
        const parsed = parseTimeInput(val);
        if (parsed === null) {
          setSaveError(
            `"${val}" is not a valid time. Use formats like 1.5, 1:30, 1h 30m, or 90m.`,
          );
          return;
        }
        normalizedValues[code] = parsed;
      }
      normalizedRows.push({ ...row, values: normalizedValues });
    }

    setSaving(true);
    setSaveError(null);
    try {
      const resp = await saveDayGrid(numericPeriodId, {
        work_date: selectedDate,
        rows: normalizedRows,
      });
      setGrid(resp);
      setDirtyRows(new Map());
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      let msg = 'Save failed.';
      if (typeof detail === 'string' && detail.trim()) {
        msg = detail;
      } else if (Array.isArray(detail)) {
        msg = (detail as Array<{ msg?: string }>)
          .map((i) => i.msg ?? String(i))
          .filter(Boolean)
          .join(' ');
      }
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  // ── Time-format parser ──────────────────────────────────────────────────── //
  // Returns the decimal-hours string on success, or null if the input is invalid.
  // Empty string returns empty string (clears the cell).
  function parseTimeInput(v: string): string | null {
    const s = v.trim();
    if (!s) return '';

    // Plain decimal / integer — must be non-negative
    if (/^\d*\.?\d+$/.test(s)) {
      const n = parseFloat(s);
      return n >= 0 ? s : null;
    }

    // H:MM or H:MM:SS — minutes and seconds must each be 0–59
    const colonMatch = /^(\d+):(\d{1,2})(?::(\d{1,2}))?$/.exec(s);
    if (colonMatch) {
      const h = parseInt(colonMatch[1], 10);
      const m = parseInt(colonMatch[2], 10);
      const sec = colonMatch[3] !== undefined ? parseInt(colonMatch[3], 10) : 0;
      if (m > 59 || sec > 59) return null;
      const total = h + m / 60 + sec / 3600;
      return String(Math.round(total * 1_000_000) / 1_000_000);
    }

    // Word format: "1h 30m", "1hr 30min", "1 hour 30 minutes", "90m", "30mins", …
    // Hours: h | hr | hrs | hour | hours
    // Minutes: m | min | mins | minute | minutes
    // Minutes may exceed 59 (e.g. "90m" = 1.5 h); this is deliberate.
    const hmMatch =
      /^(?:(\d+)\s*(?:hours|hour|hrs|hr|h))?(?:\s*(\d+)\s*(?:minutes|minute|mins|min|m))?$/i.exec(s);
    if (hmMatch && (hmMatch[1] || hmMatch[2])) {
      const h = parseInt(hmMatch[1] ?? '0', 10);
      const m = parseInt(hmMatch[2] ?? '0', 10);
      const total = h + m / 60;
      return String(Math.round(total * 1_000_000) / 1_000_000);
    }

    return null;
  }

  function handleTimeCellBlur(driverId: number, code: string, raw: string) {
    const parsed = parseTimeInput(raw);
    if (parsed === null) {
      // Invalid — revert to the last saved server value and surface the error
      const serverVal =
        grid?.rows.find((r) => r.driver_id === driverId)?.values[code]?.quantity ?? '';
      handleCellChange(driverId, code, serverVal);
      setSaveError(
        `"${raw}" is not a valid time. Use formats like 1.5, 1:30, 1h 30m, or 90m.`,
      );
      return;
    }
    if (parsed !== raw) {
      handleCellChange(driverId, code, parsed);
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

  // ── Render ─────────────────────────────────────────────────────────────── //
  if (loading && !grid) return <div className={styles.loading}>Loading…</div>;
  if (error && !grid) return <div className={styles.error}>{error}</div>;
  if (!grid) return null;

  const { period, columns, status_keys, rows, summary } = grid;

  const readOnlyBanner = (() => {
    if (period.status === 'Draft')
      return 'This period is Prepared — read-only until the backend workflow promotes it to Open.';
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
        <div className={styles.summaryItem}>
          <span className={styles.summaryLabel}>Hours:</span>
          <span className={styles.summaryValue}>{summary.total_hours}</span>
        </div>
        <div className={styles.summaryItem}>
          <span className={styles.summaryLabel}>Miles:</span>
          <span className={styles.summaryValue}>{summary.total_miles}</span>
        </div>
        <div className={styles.summaryItem}>
          <span className={styles.summaryLabel}>Gross:</span>
          <span className={styles.summaryValue}>${summary.gross_total}</span>
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
                  <th key={col.pay_item_code}>{col.label}</th>
                ))}
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const isDirty = dirtyRows.has(row.driver_id);
                const isOff = row.is_off;
                return (
                  <tr key={row.driver_id} className={isOff ? styles.offRow : undefined}>
                    <td className={styles.driverName}>
                      {row.driver_name}
                      {isDirty && <span style={{ color: '#f59e0b', marginLeft: 4 }}>●</span>}
                    </td>
                    <td>
                      <select
                        className={styles.statusSelect}
                        value={getStatusValue(row)}
                        disabled={!canEdit}
                        onChange={(e) => handleStatusChange(row.driver_id, e.target.value || null)}
                      >
                        <option value="">—</option>
                        {status_keys.map((sk) => (
                          <option key={sk.key_code} value={sk.key_code}>{sk.label}</option>
                        ))}
                      </select>
                    </td>
                    {columns.map((col) => {
                      const val = row.values[col.pay_item_code];
                      const nmr = val?.needs_manager_review ?? false;
                      const cellVal = getCellValue(row, col.pay_item_code);
                      return (
                        <td key={col.pay_item_code}>
                          {nmr && (
                            <span className={styles.attentionIcon} title="Needs manager review">
                              &#9888;
                            </span>
                          )}
                          {col.is_time ? (
                            <input
                              className={styles.qtyInput}
                              type="text"
                              inputMode="decimal"
                              placeholder="e.g. 1:30"
                              value={cellVal}
                              disabled={!canEdit}
                              onChange={(e) =>
                                handleCellChange(row.driver_id, col.pay_item_code, e.target.value)
                              }
                              onBlur={(e) =>
                                handleTimeCellBlur(row.driver_id, col.pay_item_code, e.target.value)
                              }
                            />
                          ) : (
                            <input
                              className={styles.qtyInput}
                              type="number"
                              step="0.01"
                              min="0"
                              value={cellVal}
                              disabled={!canEdit}
                              onChange={(e) =>
                                handleCellChange(row.driver_id, col.pay_item_code, e.target.value)
                              }
                            />
                          )}
                        </td>
                      );
                    })}
                    <td>
                      {notesOpenDriverId === row.driver_id ? (
                        <input
                          type="text"
                          style={{ width: 150, fontSize: '0.78rem', padding: '0.2rem' }}
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
