import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import { listDriverPayRates, listScheduleHistory } from '../../../lib/compensationApi';
import type { DriverPayRateRow } from '../../../types/compensation';
import { apiError, type HistoryRow } from './payRatesView';
import { getStatusRateMatrix, type StatusRateMatrix } from './statusRatesApi';
import { getDriverPayRules, type DriverPayRule } from './driverPayRulesApi';

/**
 * Reads for the selected Driver. Ordinary rates, rate history and Min/Max rules are keyed by
 * the permanent Driver; the Status pay-rate matrix is keyed by one DriverAssignment, because
 * Status rates stay owned by a Branch tenure.
 */
export function usePayRateReads(
  selectedDriverId: number | null,
  statusAssignmentId: number | null,
) {
  const selectedDriverRef = useRef(selectedDriverId);
  const statusAssignmentRef = useRef(statusAssignmentId);
  useLayoutEffect(() => {
    selectedDriverRef.current = selectedDriverId;
    statusAssignmentRef.current = statusAssignmentId;
  }, [selectedDriverId, statusAssignmentId]);
  const ratesRequest = useRef(0);
  const statusRequest = useRef(0);
  const rulesRequest = useRef(0);
  const historyRequest = useRef(0);
  const [rowsState, setRowsState] = useState<DriverPayRateRow[]>([]);
  const [ratesDriverId, setRatesDriverId] = useState<number | null>(null);
  const [loadingRows, setLoadingRows] = useState(false);
  const [loadingRowsDriverId, setLoadingRowsDriverId] = useState<number | null>(null);
  const [rowsError, setRowsError] = useState('');
  const [rowsErrorDriverId, setRowsErrorDriverId] = useState<number | null>(null);
  const [statusMatrixState, setStatusMatrixState] = useState<StatusRateMatrix | null>(null);
  const [statusMatrixAssignmentId, setStatusMatrixAssignmentId] = useState<number | null>(null);
  const [historyRowsState, setHistoryRows] = useState<HistoryRow[]>([]);
  const [historyDriverId, setHistoryDriverId] = useState<number | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingHistoryDriverId, setLoadingHistoryDriverId] = useState<number | null>(null);
  const [historyLoadedState, setHistoryLoaded] = useState(false);
  const [payRulesState, setPayRules] = useState<DriverPayRule[]>([]);
  const [payRulesDriverId, setPayRulesDriverId] = useState<number | null>(null);
  const [loadingPayRules, setLoadingPayRules] = useState(false);
  const [loadingPayRulesDriverId, setLoadingPayRulesDriverId] = useState<number | null>(null);
  const [payRulesLoadedState, setPayRulesLoaded] = useState(false);
  const [payRulesErrorState, setPayRulesError] = useState('');

  const loadRates = useCallback(async (driverId: number) => {
    if (selectedDriverRef.current !== driverId) return;
    const request = ++ratesRequest.current;
    setLoadingRows(true);
    setLoadingRowsDriverId(driverId);
    setRowsError('');
    setRowsErrorDriverId(null);
    setRowsState([]);
    setRatesDriverId(null);
    try {
      const values = await listDriverPayRates(driverId);
      if (request === ratesRequest.current && selectedDriverRef.current === driverId) {
        setRowsState(values);
        setRatesDriverId(driverId);
      }
    } catch (err) {
      if (request === ratesRequest.current && selectedDriverRef.current === driverId) {
        setRowsError(apiError(err));
        setRowsErrorDriverId(driverId);
      }
    } finally {
      if (request === ratesRequest.current && selectedDriverRef.current === driverId) {
        setLoadingRows(false);
        setLoadingRowsDriverId(null);
      }
    }
  }, []);

  /** Status pay rates of ONE assignment (the Status pane's own picker). */
  const loadStatus = useCallback(async (driverAssignmentId: number) => {
    if (statusAssignmentRef.current !== driverAssignmentId) return;
    const request = ++statusRequest.current;
    setStatusMatrixState(null);
    setStatusMatrixAssignmentId(null);
    try {
      const res = await getStatusRateMatrix(driverAssignmentId);
      if (request === statusRequest.current && statusAssignmentRef.current === driverAssignmentId) {
        setStatusMatrixState(res.data);
        setStatusMatrixAssignmentId(driverAssignmentId);
      }
    } catch {
      if (request === statusRequest.current && statusAssignmentRef.current === driverAssignmentId) {
        setStatusMatrixState(null);
        setStatusMatrixAssignmentId(driverAssignmentId);
      }
    }
  }, []);

  const loadPayRules = useCallback(async (driverId: number) => {
    if (selectedDriverRef.current !== driverId) return;
    const request = ++rulesRequest.current;
    setLoadingPayRules(true);
    setLoadingPayRulesDriverId(driverId);
    setPayRulesError('');
    setPayRulesDriverId(null);
    setPayRulesLoaded(false);
    try {
      const res = await getDriverPayRules(driverId);
      if (request === rulesRequest.current && selectedDriverRef.current === driverId) {
        setPayRules(res.data);
        setPayRulesDriverId(driverId);
        setPayRulesLoaded(true);
      }
    } catch (err) {
      if (request === rulesRequest.current && selectedDriverRef.current === driverId) {
        setPayRulesError(apiError(err));
        setPayRules([]);
        setPayRulesDriverId(driverId);
        setPayRulesLoaded(true);
      }
    } finally {
      if (request === rulesRequest.current && selectedDriverRef.current === driverId) {
        setLoadingPayRules(false);
        setLoadingPayRulesDriverId(null);
      }
    }
  }, []);

  const loadHistory = useCallback(async (driverId: number, current: DriverPayRateRow[]) => {
    if (selectedDriverRef.current !== driverId) return;
    const request = ++historyRequest.current;
    setLoadingHistory(true);
    setLoadingHistoryDriverId(driverId);
    setHistoryDriverId(null);
    setHistoryLoaded(false);
    try {
      const lists = await Promise.all(current.map(async (row) => {
        const items = await listScheduleHistory(driverId, row.rate_definition_id);
        return items.map((item) => ({
          ...item, definition_name: row.definition_name, unit: row.unit,
        }));
      }));
      if (request === historyRequest.current && selectedDriverRef.current === driverId) {
        setHistoryRows(lists.flat().sort((a, b) => b.effective_from.localeCompare(a.effective_from)));
        setHistoryDriverId(driverId);
        setHistoryLoaded(true);
      }
    } catch {
      if (request === historyRequest.current && selectedDriverRef.current === driverId) {
        setHistoryRows([]);
        setHistoryDriverId(driverId);
        setHistoryLoaded(true);
      }
    } finally {
      if (request === historyRequest.current && selectedDriverRef.current === driverId) {
        setLoadingHistory(false);
        setLoadingHistoryDriverId(null);
      }
    }
  }, []);

  const reloadAll = useCallback(async (driverId: number) => {
    if (selectedDriverRef.current !== driverId) return;
    await loadRates(driverId);
    if (selectedDriverRef.current === driverId) {
      setHistoryLoaded(false);
      setHistoryDriverId(null);
    }
    const statusAssignment = statusAssignmentRef.current;
    if (statusAssignment != null) await loadStatus(statusAssignment);
    if (payRulesDriverId === driverId) await loadPayRules(driverId);
  }, [loadRates, loadStatus, loadPayRules, payRulesDriverId]);

  const refreshTargetRows = useCallback(async (driverId: number) => {
    if (selectedDriverRef.current !== driverId) return;
    await loadRates(driverId);
    if (selectedDriverRef.current === driverId) {
      setHistoryLoaded(false);
      setHistoryDriverId(null);
    }
  }, [loadRates]);

  const rows = ratesDriverId === selectedDriverId ? rowsState : [];
  const visibleRowsError = rowsErrorDriverId === selectedDriverId ? rowsError : '';
  const statusMatrix = statusMatrixAssignmentId === statusAssignmentId ? statusMatrixState : null;
  const historyRows = historyDriverId === selectedDriverId ? historyRowsState : [];
  const historyLoaded = historyDriverId === selectedDriverId && historyLoadedState;
  const payRules = payRulesDriverId === selectedDriverId ? payRulesState : [];
  const payRulesLoaded = payRulesDriverId === selectedDriverId && payRulesLoadedState;
  const payRulesError = payRulesDriverId === selectedDriverId ? payRulesErrorState : '';

  return {
    rows, loadingRows: loadingRows && loadingRowsDriverId === selectedDriverId,
    rowsError: visibleRowsError, statusMatrix, historyRows,
    loadingHistory: loadingHistory && loadingHistoryDriverId === selectedDriverId,
    historyLoaded, setHistoryRows, setHistoryLoaded,
    payRules, loadingPayRules: loadingPayRules && loadingPayRulesDriverId === selectedDriverId,
    payRulesLoaded, setPayRules, setPayRulesLoaded, payRulesError, setPayRulesError,
    loadRates, loadStatus, loadPayRules, loadHistory, reloadAll, refreshTargetRows,
  };
}
