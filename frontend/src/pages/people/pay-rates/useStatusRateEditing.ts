import { useState } from 'react';
import { apiError, type EditRow } from './payRatesView';
import { saveStatusRateBatch, type StatusRateGroup, type StatusRateMatrix } from './statusRatesApi';

export function useStatusRateEditing(
  selectedDriverId: number | null,
  statusMatrix: StatusRateMatrix | null,
  loadRates: (driverId: number) => Promise<void>,
  referenceDate: string | undefined,
) {
  const [statusEditMode, setStatusEditMode] = useState(false);
  const [statusDrafts, setStatusDrafts] = useState<Record<string, EditRow>>({});
  const [statusOriginal, setStatusOriginal] = useState<Record<string, EditRow>>({});
  const [statusSaving, setStatusSaving] = useState(false);
  const [statusSaveError, setStatusSaveError] = useState('');

  const statusKey = (group: StatusRateGroup) => `s:${group.status_rate_column_id}`;

  function startStatusEdit() {
    const initial: Record<string, EditRow> = {};
    for (const group of statusMatrix?.groups ?? []) {
      initial[statusKey(group)] = { amount: group.current_rate?.amount ?? '', effective_from: referenceDate ?? '' };
    }
    setStatusDrafts(initial);
    setStatusOriginal(initial);
    setStatusEditMode(true);
    setStatusSaveError('');
  }

  function cancelStatusEdit() {
    setStatusEditMode(false);
    setStatusDrafts({});
    setStatusOriginal({});
    setStatusSaveError('');
  }

  function isStatusDirty(key: string): boolean {
    const cur = statusDrafts[key];
    const orig = statusOriginal[key];
    if (!cur || !orig) return false;
    return cur.amount !== orig.amount || cur.effective_from !== orig.effective_from;
  }

  const dirtyStatusKeys = Object.keys(statusDrafts).filter((k) => isStatusDirty(k) && statusDrafts[k].amount !== '');

  function setStatusField(key: string, field: keyof EditRow, value: string) {
    setStatusDrafts((prev) => ({ ...prev, [key]: { ...prev[key], [field]: value } }));
  }

  async function saveStatusRates() {
    if (!selectedDriverId) return;
    const driverId = selectedDriverId;
    setStatusSaving(true);
    setStatusSaveError('');
    try {
      const statusChanges = (statusMatrix?.groups ?? []).filter((g) => dirtyStatusKeys.includes(statusKey(g)));
      const dates = new Set(statusChanges.map((g) => statusDrafts[statusKey(g)].effective_from));
      if (dates.size > 1) {
        throw new Error('All changed Status pay rates must share one effective date.');
      }
      await saveStatusRateBatch(driverId, {
        effective_from: [...dates][0],
        changes: statusChanges.map((g) => ({
          status_rate_column_id: g.status_rate_column_id,
          rate_type_id: g.rate_type_id,
          amount: statusDrafts[statusKey(g)].amount,
        })),
      });
      cancelStatusEdit();
      await loadRates(driverId);
    } catch (err) {
      setStatusSaveError(err instanceof Error ? err.message : apiError(err));
      await loadRates(driverId);
    } finally {
      setStatusSaving(false);
    }
  }

  return { statusEditMode, setStatusEditMode, statusDrafts, setStatusDrafts, setStatusOriginal, statusSaving, statusSaveError, setStatusSaveError, statusKey, startStatusEdit, cancelStatusEdit, isStatusDirty, dirtyStatusKeys, setStatusField, saveStatusRates };
}
