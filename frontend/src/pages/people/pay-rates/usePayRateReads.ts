import { useCallback, useState } from 'react';
import { listAssignmentHistory, listDriverPayRates } from '../../../lib/compensationApi';
import type { DriverPayRateRow } from '../../../types/compensation';
import { apiError, type HistoryRow } from './payRatesView';
import { getStatusRateMatrix, type StatusRateMatrix } from './statusRatesApi';
import { getDriverPayRules, type DriverPayRule } from './driverPayRulesApi';

export function usePayRateReads() {
  const [rows, setRows] = useState<DriverPayRateRow[]>([]);
  const [loadingRows, setLoadingRows] = useState(false);
  const [rowsError, setRowsError] = useState('');
  const [statusMatrix, setStatusMatrix] = useState<StatusRateMatrix | null>(null);
  const [historyRows, setHistoryRows] = useState<HistoryRow[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [payRules, setPayRules] = useState<DriverPayRule[]>([]);
  const [loadingPayRules, setLoadingPayRules] = useState(false);
  const [payRulesLoaded, setPayRulesLoaded] = useState(false);
  const [payRulesError, setPayRulesError] = useState('');

  const loadRates = useCallback(async (driverId: number) => {
    setLoadingRows(true);
    setRowsError('');
    setRows([]);
    setStatusMatrix(null);
    try {
      setRows(await listDriverPayRates(driverId));
    } catch (err) {
      setRowsError(apiError(err));
    }
    try {
      const res = await getStatusRateMatrix(driverId);
      setStatusMatrix(res.data);
    } catch {
      setStatusMatrix(null);
    } finally {
      setLoadingRows(false);
    }
  }, []);

  const loadPayRules = useCallback(async (driverId: number) => {
    setLoadingPayRules(true);
    setPayRulesError('');
    try {
      const res = await getDriverPayRules(driverId);
      setPayRules(res.data);
      setPayRulesLoaded(true);
    } catch (err) {
      setPayRulesError(apiError(err));
      setPayRules([]);
      setPayRulesLoaded(true);
    } finally {
      setLoadingPayRules(false);
    }
  }, []);

  const loadHistory = useCallback(async (driverId: number, current: DriverPayRateRow[]) => {
    setLoadingHistory(true);
    try {
      const lists = await Promise.all(current.map(async (row) => {
        const items = await listAssignmentHistory(driverId, row.rate_definition_id);
        return items.map((item) => ({
          ...item, definition_name: row.definition_name, unit: row.unit,
        }));
      }));
      setHistoryRows(lists.flat().sort((a, b) => b.effective_from.localeCompare(a.effective_from)));
    } catch {
      setHistoryRows([]);
    } finally {
      setHistoryLoaded(true);
      setLoadingHistory(false);
    }
  }, []);

  const reloadAll = useCallback(async (driverId: number) => {
    await loadRates(driverId);
    setHistoryLoaded(false);
    if (payRulesLoaded) await loadPayRules(driverId);
  }, [loadRates, loadPayRules, payRulesLoaded]);

  async function refreshTargetRows(driverId: number) {
    try {
      setRows(await listDriverPayRates(driverId));
      setHistoryLoaded(false);
    } catch (err) {
      setRowsError(apiError(err));
    }
  }

  return {
    rows, loadingRows, rowsError, statusMatrix, historyRows, loadingHistory, historyLoaded, setHistoryRows, setHistoryLoaded,
    payRules, loadingPayRules, payRulesLoaded, setPayRules, setPayRulesLoaded, payRulesError, setPayRulesError,
    loadRates, loadPayRules, loadHistory, reloadAll, refreshTargetRows,
  };
}
