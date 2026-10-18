import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import { listAssignmentHistory, listDriverPayRates } from '../../../lib/compensationApi';
import type { DriverPayRateRow } from '../../../types/compensation';
import { apiError, type HistoryRow } from './payRatesView';
import { getStatusRateMatrix, type StatusRateMatrix } from './statusRatesApi';
import { getDriverPayRules, type DriverPayRule } from './driverPayRulesApi';

export function usePayRateReads(selectedDriverAssignmentId: number | null) {
  const selectedDriverRef = useRef(selectedDriverAssignmentId);
  useLayoutEffect(() => {
    selectedDriverRef.current = selectedDriverAssignmentId;
  }, [selectedDriverAssignmentId]);
  const ratesRequest = useRef(0);
  const rulesRequest = useRef(0);
  const historyRequest = useRef(0);
  const [rowsState, setRowsState] = useState<DriverPayRateRow[]>([]);
  const [ratesDriverAssignmentId, setRatesDriverAssignmentId] = useState<number | null>(null);
  const [loadingRows, setLoadingRows] = useState(false);
  const [loadingRowsDriverAssignmentId, setLoadingRowsDriverAssignmentId] = useState<number | null>(null);
  const [rowsError, setRowsError] = useState('');
  const [rowsErrorDriverAssignmentId, setRowsErrorDriverAssignmentId] = useState<number | null>(null);
  const [statusMatrixState, setStatusMatrixState] = useState<StatusRateMatrix | null>(null);
  const [statusDriverAssignmentId, setStatusDriverAssignmentId] = useState<number | null>(null);
  const [historyRowsState, setHistoryRows] = useState<HistoryRow[]>([]);
  const [historyDriverAssignmentId, setHistoryDriverAssignmentId] = useState<number | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingHistoryDriverAssignmentId, setLoadingHistoryDriverAssignmentId] = useState<number | null>(null);
  const [historyLoadedState, setHistoryLoaded] = useState(false);
  const [payRulesState, setPayRules] = useState<DriverPayRule[]>([]);
  const [payRulesDriverAssignmentId, setPayRulesDriverAssignmentId] = useState<number | null>(null);
  const [loadingPayRules, setLoadingPayRules] = useState(false);
  const [loadingPayRulesDriverAssignmentId, setLoadingPayRulesDriverAssignmentId] = useState<number | null>(null);
  const [payRulesLoadedState, setPayRulesLoaded] = useState(false);
  const [payRulesErrorState, setPayRulesError] = useState('');

  const loadRates = useCallback(async (driverAssignmentId: number) => {
    if (selectedDriverRef.current !== driverAssignmentId) return;
    const request = ++ratesRequest.current;
    setLoadingRows(true);
    setLoadingRowsDriverAssignmentId(driverAssignmentId);
    setRowsError('');
    setRowsErrorDriverAssignmentId(null);
    setRowsState([]);
    setRatesDriverAssignmentId(null);
    setStatusMatrixState(null);
    setStatusDriverAssignmentId(null);
    try {
      const values = await listDriverPayRates(driverAssignmentId);
      if (request === ratesRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setRowsState(values);
        setRatesDriverAssignmentId(driverAssignmentId);
      }
    } catch (err) {
      if (request === ratesRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setRowsError(apiError(err));
        setRowsErrorDriverAssignmentId(driverAssignmentId);
      }
    }
    try {
      const res = await getStatusRateMatrix(driverAssignmentId);
      if (request === ratesRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setStatusMatrixState(res.data);
        setStatusDriverAssignmentId(driverAssignmentId);
      }
    } catch {
      if (request === ratesRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setStatusMatrixState(null);
        setStatusDriverAssignmentId(driverAssignmentId);
      }
    } finally {
      if (request === ratesRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setLoadingRows(false);
        setLoadingRowsDriverAssignmentId(null);
      }
    }
  }, []);

  const loadPayRules = useCallback(async (driverAssignmentId: number) => {
    if (selectedDriverRef.current !== driverAssignmentId) return;
    const request = ++rulesRequest.current;
    setLoadingPayRules(true);
    setLoadingPayRulesDriverAssignmentId(driverAssignmentId);
    setPayRulesError('');
    setPayRulesDriverAssignmentId(null);
    setPayRulesLoaded(false);
    try {
      const res = await getDriverPayRules(driverAssignmentId);
      if (request === rulesRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setPayRules(res.data);
        setPayRulesDriverAssignmentId(driverAssignmentId);
        setPayRulesLoaded(true);
      }
    } catch (err) {
      if (request === rulesRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setPayRulesError(apiError(err));
        setPayRules([]);
        setPayRulesDriverAssignmentId(driverAssignmentId);
        setPayRulesLoaded(true);
      }
    } finally {
      if (request === rulesRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setLoadingPayRules(false);
        setLoadingPayRulesDriverAssignmentId(null);
      }
    }
  }, []);

  const loadHistory = useCallback(async (driverAssignmentId: number, current: DriverPayRateRow[]) => {
    if (selectedDriverRef.current !== driverAssignmentId) return;
    const request = ++historyRequest.current;
    setLoadingHistory(true);
    setLoadingHistoryDriverAssignmentId(driverAssignmentId);
    setHistoryDriverAssignmentId(null);
    setHistoryLoaded(false);
    try {
      const lists = await Promise.all(current.map(async (row) => {
        const items = await listAssignmentHistory(driverAssignmentId, row.rate_definition_id);
        return items.map((item) => ({
          ...item, definition_name: row.definition_name, unit: row.unit,
        }));
      }));
      if (request === historyRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setHistoryRows(lists.flat().sort((a, b) => b.effective_from.localeCompare(a.effective_from)));
        setHistoryDriverAssignmentId(driverAssignmentId);
        setHistoryLoaded(true);
      }
    } catch {
      if (request === historyRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setHistoryRows([]);
        setHistoryDriverAssignmentId(driverAssignmentId);
        setHistoryLoaded(true);
      }
    } finally {
      if (request === historyRequest.current && selectedDriverRef.current === driverAssignmentId) {
        setLoadingHistory(false);
        setLoadingHistoryDriverAssignmentId(null);
      }
    }
  }, []);

  const reloadAll = useCallback(async (driverAssignmentId: number) => {
    if (selectedDriverRef.current !== driverAssignmentId) return;
    await loadRates(driverAssignmentId);
    if (selectedDriverRef.current === driverAssignmentId) {
      setHistoryLoaded(false);
      setHistoryDriverAssignmentId(null);
    }
    if (payRulesDriverAssignmentId === driverAssignmentId) await loadPayRules(driverAssignmentId);
  }, [loadRates, loadPayRules, payRulesDriverAssignmentId]);

  const refreshTargetRows = useCallback(async (driverAssignmentId: number) => {
    if (selectedDriverRef.current !== driverAssignmentId) return;
    await loadRates(driverAssignmentId);
    if (selectedDriverRef.current === driverAssignmentId) {
      setHistoryLoaded(false);
      setHistoryDriverAssignmentId(null);
    }
  }, [loadRates]);

  const rows = ratesDriverAssignmentId === selectedDriverAssignmentId ? rowsState : [];
  const visibleRowsError = rowsErrorDriverAssignmentId === selectedDriverAssignmentId ? rowsError : '';
  const statusMatrix = statusDriverAssignmentId === selectedDriverAssignmentId ? statusMatrixState : null;
  const historyRows = historyDriverAssignmentId === selectedDriverAssignmentId ? historyRowsState : [];
  const historyLoaded = historyDriverAssignmentId === selectedDriverAssignmentId && historyLoadedState;
  const payRules = payRulesDriverAssignmentId === selectedDriverAssignmentId ? payRulesState : [];
  const payRulesLoaded = payRulesDriverAssignmentId === selectedDriverAssignmentId && payRulesLoadedState;
  const payRulesError = payRulesDriverAssignmentId === selectedDriverAssignmentId ? payRulesErrorState : '';

  return {
    rows, loadingRows: loadingRows && loadingRowsDriverAssignmentId === selectedDriverAssignmentId,
    rowsError: visibleRowsError, statusMatrix, historyRows,
    loadingHistory: loadingHistory && loadingHistoryDriverAssignmentId === selectedDriverAssignmentId,
    historyLoaded, setHistoryRows, setHistoryLoaded,
    payRules, loadingPayRules: loadingPayRules && loadingPayRulesDriverAssignmentId === selectedDriverAssignmentId,
    payRulesLoaded, setPayRules, setPayRulesLoaded, payRulesError, setPayRulesError,
    loadRates, loadPayRules, loadHistory, reloadAll, refreshTargetRows,
  };
}
