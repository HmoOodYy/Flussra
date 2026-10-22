import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import {
  getCompensationAccess, listDriverPayRates, listDriverRateSummary, listScheduleHistory,
} from '../../../lib/compensationApi';
import type {
  CompensationAccess, DriverPayRateRow, DriverRateSummaryItem, ScheduleSummary,
} from '../../../types/compensation';
import { apiError } from './payRatesView';
import { buildHistoryRows, type HistoryRow } from './payRatesHistory';
import { getDriverPayRules, type DriverPayRule } from './driverPayRulesApi';

/** The Driver's schedule summary and the history of each definition that has a Pending schedule. */
export interface PendingSource {
  summary: DriverRateSummaryItem[];
  historyByDefinition: Record<number, ScheduleSummary[]>;
}

const EMPTY_PENDING_SOURCE: PendingSource = { summary: [], historyByDefinition: {} };

/**
 * Pending schedules are discovered from the read-scope-filtered summary and history, not from
 * the Active-only authoring matrix, so a retired definition's Pending schedule is found too.
 */
async function loadPendingSource(driverId: number): Promise<PendingSource> {
  const summary = await listDriverRateSummary(driverId);
  const historyByDefinition: Record<number, ScheduleSummary[]> = {};
  await Promise.all(summary.filter((item) => item.pending_schedule_id != null).map(async (item) => {
    historyByDefinition[item.rate_definition_id] = await listScheduleHistory(driverId, item.rate_definition_id);
  }));
  return { summary, historyByDefinition };
}

/**
 * Reads for the selected Driver. Every rate (ordinary PayDefinition rates and Status rate
 * column rates), the rate history and the Min/Max rules belong to the permanent Driver and are
 * keyed by driver_id.
 */
export function usePayRateReads(selectedDriverId: number | null) {
  const selectedDriverRef = useRef(selectedDriverId);
  useLayoutEffect(() => {
    selectedDriverRef.current = selectedDriverId;
  }, [selectedDriverId]);
  const ratesRequest = useRef(0);
  const rulesRequest = useRef(0);
  const historyRequest = useRef(0);
  const [rowsState, setRowsState] = useState<DriverPayRateRow[]>([]);
  const [ratesDriverId, setRatesDriverId] = useState<number | null>(null);
  const [loadingRows, setLoadingRows] = useState(false);
  const [loadingRowsDriverId, setLoadingRowsDriverId] = useState<number | null>(null);
  const [accessState, setAccessState] = useState<CompensationAccess | null>(null);
  const [accessDriverId, setAccessDriverId] = useState<number | null>(null);
  const [pendingSourceState, setPendingSourceState] = useState<PendingSource>(EMPTY_PENDING_SOURCE);
  const [pendingSourceDriverId, setPendingSourceDriverId] = useState<number | null>(null);
  const [rowsError, setRowsError] = useState('');
  const [rowsErrorDriverId, setRowsErrorDriverId] = useState<number | null>(null);
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
    setAccessState(null);
    setAccessDriverId(null);
    setPendingSourceState(EMPTY_PENDING_SOURCE);
    setPendingSourceDriverId(null);
    try {
      // The backend's own decisions (full history, discard, orphaned Pending) load with the rates.
      const [values, access, source] = await Promise.all([
        listDriverPayRates(driverId), getCompensationAccess(driverId), loadPendingSource(driverId)]);
      if (request === ratesRequest.current && selectedDriverRef.current === driverId) {
        // This screen edits one value per rate. Tiered (OrdinalTier) rates carry a complete
        // value set per tier and have their own authoring screen; they are not offered here.
        setRowsState(values.filter((row) => row.rate_shape === 'Scalar'));
        setRatesDriverId(driverId);
        setAccessState(access);
        setAccessDriverId(driverId);
        setPendingSourceState(source);
        setPendingSourceDriverId(driverId);
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

  /** History comes from the Driver's schedule summary, not from the Active-only authoring matrix. */
  const loadHistory = useCallback(async (driverId: number) => {
    if (selectedDriverRef.current !== driverId) return;
    const request = ++historyRequest.current;
    setLoadingHistory(true);
    setLoadingHistoryDriverId(driverId);
    setHistoryDriverId(null);
    setHistoryLoaded(false);
    try {
      const items = await listDriverRateSummary(driverId);
      const lists = await Promise.all(
        items.map((item) => listScheduleHistory(driverId, item.rate_definition_id)));
      if (request === historyRequest.current && selectedDriverRef.current === driverId) {
        setHistoryRows(buildHistoryRows(items, lists));
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
    if (payRulesDriverId === driverId) await loadPayRules(driverId);
  }, [loadRates, loadPayRules, payRulesDriverId]);

  const refreshTargetRows = useCallback(async (driverId: number) => {
    if (selectedDriverRef.current !== driverId) return;
    await loadRates(driverId);
    if (selectedDriverRef.current === driverId) {
      setHistoryLoaded(false);
      setHistoryDriverId(null);
    }
  }, [loadRates]);

  const rows = ratesDriverId === selectedDriverId ? rowsState : [];
  const access = accessDriverId === selectedDriverId ? accessState : null;
  const pendingSource = pendingSourceDriverId === selectedDriverId ? pendingSourceState : EMPTY_PENDING_SOURCE;
  const visibleRowsError = rowsErrorDriverId === selectedDriverId ? rowsError : '';
  const historyRows = historyDriverId === selectedDriverId ? historyRowsState : [];
  const historyLoaded = historyDriverId === selectedDriverId && historyLoadedState;
  const payRules = payRulesDriverId === selectedDriverId ? payRulesState : [];
  const payRulesLoaded = payRulesDriverId === selectedDriverId && payRulesLoadedState;
  const payRulesError = payRulesDriverId === selectedDriverId ? payRulesErrorState : '';

  return {
    rows, access, pendingSource, loadingRows: loadingRows && loadingRowsDriverId === selectedDriverId,
    rowsError: visibleRowsError, historyRows,
    loadingHistory: loadingHistory && loadingHistoryDriverId === selectedDriverId,
    historyLoaded, setHistoryRows, setHistoryLoaded,
    payRules, loadingPayRules: loadingPayRules && loadingPayRulesDriverId === selectedDriverId,
    payRulesLoaded, setPayRules, setPayRulesLoaded, payRulesError, setPayRulesError,
    loadRates, loadPayRules, loadHistory, reloadAll, refreshTargetRows,
  };
}
