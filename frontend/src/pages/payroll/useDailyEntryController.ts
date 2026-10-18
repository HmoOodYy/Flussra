import { useCallback, useState } from 'react';
import { getDayGrid, saveDayGrid } from '../../lib/payrollApi';
import { canEntryPayroll } from '../../lib/permissions';
import { addIsoDays, formatIsoShort, weekdayOfIso } from '../../lib/isoDate';
import type { UserProfile } from '../../store/authStore';
import type { DayGridResponse, DayGridRow, DayGridSaveRow } from '../../types/payroll/entry';
import { buildSaveRowFromGrid } from './dayGridModel';

const EDITABLE_STATUSES = new Set(['Draft', 'Open', 'Returned']);

// Shared grid state and actions; each surface owns its read trigger and save-error presentation.

export function formatDayLabel(d: string): string {
  const weekday = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][weekdayOfIso(d)];
  return `${weekday}, ${formatIsoShort(d)}`;
}

interface DailyEntryControllerOptions {
  periodId: number;
  user: UserProfile | null;
  formatSaveError: (error: unknown) => string;
}

export function useDailyEntryController({ periodId, user, formatSaveError }: DailyEntryControllerOptions) {
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [grid, setGrid] = useState<DayGridResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [dirtyRows, setDirtyRows] = useState<Map<number, DayGridSaveRow>>(new Map());
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [notesOpenDriverId, setNotesOpenDriverId] = useState<number | null>(null);

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
      const detail = (e as {
        response?: { data?: { detail?: string | { message?: unknown } } };
      })?.response?.data?.detail;
      const msg =
        typeof detail === 'string'
          ? detail
          : typeof detail?.message === 'string'
            ? detail.message
            : 'Failed to load grid.';
      setError(msg);
    } finally {
      setLoading(false);
    }
  }, [periodId]);

  const atStart = grid && selectedDate ? selectedDate <= grid.period.start_date : true;
  const atEnd   = grid && selectedDate ? selectedDate >= grid.period.end_date   : true;

  function prevDay() { setSelectedDate((d) => d ? addIsoDays(d, -1) : d); }
  function nextDay() { setSelectedDate((d) => d ? addIsoDays(d, 1) : d); }

  const isEditable = grid ? EDITABLE_STATUSES.has(grid.period.status) : false;
  const canEdit    = isEditable && grid !== null && user !== null && canEntryPayroll(user, grid.period.branch_id);

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
      const msg = formatSaveError(e);
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }

  function handleCellChange(driverId: number, key: string, value: string) {
    setDirtyRows((prev) => {
      const m = new Map(prev);
      const existing = m.get(driverId) ?? buildSaveRowFromGrid(grid, driverId);
      m.set(driverId, { ...existing, values: { ...existing.values, [key]: value } });
      return m;
    });
  }

  function handleStatusChange(driverId: number, statusKey: string | null) {
    setDirtyRows((prev) => {
      const m = new Map(prev);
      const existing = m.get(driverId) ?? buildSaveRowFromGrid(grid, driverId);
      m.set(driverId, { ...existing, status_key: statusKey });
      return m;
    });
  }

  function handleNotesChange(driverId: number, notes: string) {
    setDirtyRows((prev) => {
      const m = new Map(prev);
      const existing = m.get(driverId) ?? buildSaveRowFromGrid(grid, driverId);
      m.set(driverId, { ...existing, notes: notes || null });
      return m;
    });
  }

  function getCellValue(row: DayGridRow, key: string): string {
    const dirty = dirtyRows.get(row.driver_id);
    if (dirty) return dirty.values[key] ?? '';
    return row.values[key]?.quantity ?? '';
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

  return {
    selectedDate,
    grid,
    loading,
    error,
    dirtyRows,
    saving,
    saveError,
    notesOpenDriverId,
    setNotesOpenDriverId,
    fetchGrid,
    atStart,
    atEnd,
    prevDay,
    nextDay,
    isEditable,
    canEdit,
    handleSave,
    handleCellChange,
    handleStatusChange,
    handleNotesChange,
    getCellValue,
    getStatusValue,
    getNotesValue,
  };
}
