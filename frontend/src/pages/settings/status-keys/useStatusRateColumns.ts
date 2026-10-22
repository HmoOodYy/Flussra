import { useCallback, useEffect, useState } from 'react';
import {
  createStatusRateColumn,
  getStatusRateColumns,
  updateStatusRateColumn,
} from '../../../lib/settingsApi';
import type { StatusRateColumn, StatusRateColumnUpdate } from '../../../types/settings';
import { sortColumns, validateColumnName } from './statusColumnsModel';
import { apiError } from './statusKeysModel';

/** The Company Status rate columns (inactive ones included), with the writers of the panel. */
export function useStatusRateColumns(showToast: (msg: string) => void) {
  const [columns, setColumns] = useState<StatusRateColumn[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busyId, setBusyId] = useState<number | 'new' | null>(null);

  const load = useCallback(async () => {
    try {
      const { data } = await getStatusRateColumns(true);
      setColumns(sortColumns(data));
      setError('');
    } catch (e) {
      setError(apiError(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // The first load, with its state set from the response callbacks only.
  useEffect(() => {
    let cancelled = false;
    getStatusRateColumns(true)
      .then(({ data }) => { if (!cancelled) { setColumns(sortColumns(data)); setLoading(false); } })
      .catch((e) => { if (!cancelled) { setError(apiError(e)); setLoading(false); } });
    return () => { cancelled = true; };
  }, []);

  async function create(name: string, makeDefault: boolean): Promise<boolean> {
    const problem = validateColumnName(name);
    if (problem) { setActionError(problem); return false; }
    setBusyId('new'); setActionError('');
    try {
      const { data } = await createStatusRateColumn({ column_name: name.trim(), is_default: makeDefault });
      await load();
      showToast(`Status rate column "${data.column_name}" created.`);
      return true;
    } catch (e) { setActionError(apiError(e)); return false; }
    finally { setBusyId(null); }
  }

  async function update(
    column: StatusRateColumn, payload: StatusRateColumnUpdate, message: string,
  ): Promise<boolean> {
    setBusyId(column.status_rate_column_id); setActionError('');
    try {
      await updateStatusRateColumn(column.status_rate_column_id, payload);
      await load();
      showToast(message);
      return true;
    } catch (e) { setActionError(apiError(e)); return false; }
    finally { setBusyId(null); }
  }

  return { columns, loading, error, actionError, setActionError, busyId, create, update, reload: load };
}
