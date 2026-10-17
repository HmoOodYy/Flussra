import { useCallback, useEffect, useRef, useState } from 'react';
import { getFinalizedAudit, getFinalizedOffDrivers, getFinalizedOverview, getFinalizedRatesUsed, getFinalizedReport } from '../../lib/payrollApi';
import type {
  FinalizedAuditResponse,
  FinalizedCalculationReportResponse,
  FinalizedOffDriversResponse,
  FinalizedOverviewResponse,
  FinalizedRatesUsedResponse,
  FinalizedReportView,
} from '../../types/payroll';
import { AuditBody } from './finalizedLibrary/AuditBody';
import { StateMessage } from './finalizedLibrary/EvidencePresentation';
import { OffStatusBody } from './finalizedLibrary/OffStatusBody';
import { OverviewBody } from './finalizedLibrary/OverviewBody';
import { ReportBody } from './finalizedLibrary/ReportBody';
import { RatesUsedBody } from './finalizedLibrary/RatesUsedBody';
import styles from './FinalizedPayrollLibraryDialog.module.css';

const REPORT_TABS: readonly { view: FinalizedReportView; label: string }[] = [
  { view: 'drivers', label: 'Drivers' },
  { view: 'period-work', label: 'Period Work' },
  { view: 'period-pay', label: 'Period Pay' },
  { view: 'mixed', label: 'Mixed' },
];

type FinalizedLibraryTab = 'overview' | FinalizedReportView | 'off-status' | 'rates-used' | 'audit';

function isFinalizedReportView(tab: FinalizedLibraryTab): tab is FinalizedReportView {
  return REPORT_TABS.some((item) => item.view === tab);
}

function errorDetail(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return fallback;
}

export interface FinalizedPayrollLibraryPeriodContext {
  payroll_period_id: number;
  period_name: string;
  period_code: string;
  branch_name: string;
  status: string;
}

interface FinalizedPayrollLibraryDialogProps {
  period: FinalizedPayrollLibraryPeriodContext;
  onClose: () => void;
}

export function FinalizedPayrollLibraryDialog({ period, onClose }: FinalizedPayrollLibraryDialogProps) {
  const [activeTab, setActiveTab] = useState<FinalizedLibraryTab>('overview');
  const [overview, setOverview] = useState<FinalizedOverviewResponse | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [overviewError, setOverviewError] = useState<string | null>(null);
  const [report, setReport] = useState<FinalizedCalculationReportResponse | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);
  const [overviewRetry, setOverviewRetry] = useState(0);
  const [reportRetry, setReportRetry] = useState(0);
  const [offStatus, setOffStatus] = useState<FinalizedOffDriversResponse | null>(null);
  const [offStatusLoading, setOffStatusLoading] = useState(false);
  const [offStatusError, setOffStatusError] = useState<string | null>(null);
  const [offStatusRetry, setOffStatusRetry] = useState(0);
  const [ratesUsed, setRatesUsed] = useState<FinalizedRatesUsedResponse | null>(null);
  const [ratesUsedLoading, setRatesUsedLoading] = useState(false);
  const [ratesUsedError, setRatesUsedError] = useState<string | null>(null);
  const [ratesUsedRetry, setRatesUsedRetry] = useState(0);
  const [audit, setAudit] = useState<FinalizedAuditResponse | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [auditRetry, setAuditRetry] = useState(0);
  const requestRef = useRef(0);
  const auditRequestRef = useRef(0);
  const auditAvailability = overview?.section_availability.audit;
  const canViewAudit = auditAvailability != null && auditAvailability.state !== 'UNAVAILABLE';
  const visibleTab: FinalizedLibraryTab = activeTab === 'audit' && !canViewAudit ? 'overview' : activeTab;

  useEffect(() => {
    let active = true;
    void getFinalizedOverview(period.payroll_period_id)
      .then((data) => { if (active) setOverview(data); })
      .catch((error: unknown) => { if (active) setOverviewError(errorDetail(error, 'Failed to load finalized payroll overview.')); })
      .finally(() => { if (active) setOverviewLoading(false); });
    return () => { active = false; };
  }, [period.payroll_period_id, overviewRetry]);

  const loadReport = useCallback(async (view: FinalizedReportView) => {
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    setReportLoading(true);
    setReport(null);
    setReportError(null);
    try {
      const data = await getFinalizedReport(period.payroll_period_id, view);
      if (requestRef.current === requestId) setReport(data);
    } catch (error: unknown) {
      if (requestRef.current === requestId) setReportError(errorDetail(error, 'Failed to load finalized report.'));
    } finally {
      if (requestRef.current === requestId) setReportLoading(false);
    }
  }, [period.payroll_period_id]);

  const loadOffStatus = useCallback(async () => {
    setOffStatusLoading(true);
    setOffStatusError(null);
    try {
      setOffStatus(await getFinalizedOffDrivers(period.payroll_period_id));
    } catch (error: unknown) {
      setOffStatusError(errorDetail(error, 'Failed to load finalized Off and Status history.'));
    } finally {
      setOffStatusLoading(false);
    }
  }, [period.payroll_period_id]);

  const loadRatesUsed = useCallback(async () => {
    setRatesUsedLoading(true);
    setRatesUsedError(null);
    try {
      setRatesUsed(await getFinalizedRatesUsed(period.payroll_period_id));
    } catch (error: unknown) {
      setRatesUsedError(errorDetail(error, 'Failed to load finalized rates and Bonus evidence.'));
    } finally {
      setRatesUsedLoading(false);
    }
  }, [period.payroll_period_id]);

  const loadAudit = useCallback(async () => {
    const requestId = auditRequestRef.current + 1;
    auditRequestRef.current = requestId;
    setAuditLoading(true);
    setAuditError(null);
    try {
      const data = await getFinalizedAudit(period.payroll_period_id);
      if (auditRequestRef.current === requestId) setAudit(data);
    } catch (error: unknown) {
      if (auditRequestRef.current === requestId) setAuditError(errorDetail(error, 'Failed to load finalized audit history.'));
    } finally {
      if (auditRequestRef.current === requestId) setAuditLoading(false);
    }
  }, [period.payroll_period_id]);

  useEffect(() => {
    if (!overview || activeTab !== 'off-status' || offStatus != null) return;
    const timer = window.setTimeout(() => { void loadOffStatus(); }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, loadOffStatus, offStatus, offStatusRetry, overview]);

  useEffect(() => {
    if (!overview || activeTab !== 'rates-used' || ratesUsed != null) return;
    const timer = window.setTimeout(() => { void loadRatesUsed(); }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, loadRatesUsed, overview, ratesUsed, ratesUsedRetry]);

  useEffect(() => {
    if (!overview || !canViewAudit || activeTab !== 'audit' || audit != null) return;
    const timer = window.setTimeout(() => { void loadAudit(); }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, audit, auditRetry, canViewAudit, loadAudit, overview]);

  useEffect(() => {
    if (!overview || !isFinalizedReportView(activeTab)) return;
    const reportView = activeTab;
    const timer = window.setTimeout(() => { void loadReport(reportView); }, 0);
    return () => window.clearTimeout(timer);
  }, [activeTab, loadReport, overview, reportRetry]);

  useEffect(() => {
    function handleKey(event: KeyboardEvent) { if (event.key === 'Escape') onClose(); }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  useEffect(() => () => { auditRequestRef.current += 1; }, []);

  function selectTab(tab: FinalizedLibraryTab) {
    if (tab === activeTab) return;
    auditRequestRef.current += 1;
    setActiveTab(tab);
    setReport(null);
    setReportError(null);
    setReportLoading(tab !== 'overview' && tab !== 'off-status' && tab !== 'rates-used');
    setOffStatusError(null);
    setOffStatusLoading(tab === 'off-status' && offStatus == null);
    setRatesUsedError(null);
    setRatesUsedLoading(tab === 'rates-used' && ratesUsed == null);
    setAuditError(null);
    setAuditLoading(tab === 'audit' && audit == null);
  }

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div className={styles.dialog} onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true" aria-label="Finalized Payroll Library">
        <header className={styles.header}>
          <div><h2>Finalized Payroll Library</h2><p>{period.period_name || period.period_code} · {period.branch_name}</p></div>
          <button className={styles.closeButton} onClick={onClose} type="button" aria-label="Close">&#x2715;</button>
        </header>
        <nav className={styles.tabs} aria-label="Finalized payroll sections" role="tablist">
          <button className={activeTab === 'overview' ? styles.activeTab : styles.tab} onClick={() => selectTab('overview')} type="button" role="tab" aria-selected={activeTab === 'overview'}>Overview</button>
          {REPORT_TABS.map((tab) => <button key={tab.view} className={activeTab === tab.view ? styles.activeTab : styles.tab} onClick={() => selectTab(tab.view)} type="button" role="tab" aria-selected={activeTab === tab.view} disabled={!overview}>{tab.label}</button>)}
          <button className={activeTab === 'off-status' ? styles.activeTab : styles.tab} onClick={() => selectTab('off-status')} type="button" role="tab" aria-selected={activeTab === 'off-status'} disabled={!overview}>Off / Status</button>
          <button className={activeTab === 'rates-used' ? styles.activeTab : styles.tab} onClick={() => selectTab('rates-used')} type="button" role="tab" aria-selected={activeTab === 'rates-used'} disabled={!overview}>Rates / Bonus</button>
          {canViewAudit && <button className={activeTab === 'audit' ? styles.activeTab : styles.tab} onClick={() => selectTab('audit')} type="button" role="tab" aria-selected={activeTab === 'audit'}>Audit</button>}
        </nav>
        <div className={styles.body}>
          {overviewLoading ? <StateMessage>Loading finalized payroll overview…</StateMessage> : overviewError ? (
            <div className={styles.errorBox}><p>{overviewError}</p><button className={styles.retryButton} onClick={() => { auditRequestRef.current += 1; setOverviewLoading(true); setOverviewError(null); setOverview(null); setAudit(null); setActiveTab('overview'); setOverviewRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : overview == null ? <StateMessage>Finalized overview is unavailable.</StateMessage> : visibleTab === 'overview' ? <OverviewBody overview={overview} /> : visibleTab === 'off-status' ? offStatusLoading ? (
            <StateMessage>Loading finalized Off and Status history…</StateMessage>
          ) : offStatusError ? (
            <div className={styles.errorBox}><p>{offStatusError}</p><button className={styles.retryButton} onClick={() => { setOffStatusLoading(true); setOffStatusError(null); setOffStatus(null); setOffStatusRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : offStatus == null ? <StateMessage>Finalized Off and Status history is unavailable.</StateMessage> : <OffStatusBody response={offStatus} /> : visibleTab === 'rates-used' ? ratesUsedLoading ? (
            <StateMessage>Loading finalized rates and Bonus evidence…</StateMessage>
          ) : ratesUsedError ? (
            <div className={styles.errorBox}><p>{ratesUsedError}</p><button className={styles.retryButton} onClick={() => { setRatesUsedLoading(true); setRatesUsedError(null); setRatesUsed(null); setRatesUsedRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : ratesUsed == null ? <StateMessage>Finalized rates and Bonus evidence is unavailable.</StateMessage> : <RatesUsedBody response={ratesUsed} /> : visibleTab === 'audit' ? auditLoading ? (
            <StateMessage>Loading finalized audit history…</StateMessage>
          ) : auditError ? (
            <div className={styles.errorBox}><p>{auditError}</p><button className={styles.retryButton} onClick={() => { setAuditLoading(true); setAuditError(null); setAudit(null); setAuditRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : audit == null ? <StateMessage>Finalized audit history is unavailable.</StateMessage> : <AuditBody response={audit} /> : reportLoading ? (
            <StateMessage>Loading {REPORT_TABS.find((tab) => tab.view === activeTab)?.label.toLowerCase() ?? 'report'}…</StateMessage>
          ) : reportError ? (
            <div className={styles.errorBox}><p>{reportError}</p><button className={styles.retryButton} onClick={() => { setReportLoading(true); setReportError(null); setReportRetry((value) => value + 1); }} type="button">Retry</button></div>
          ) : report == null ? <StateMessage>No finalized report data is available.</StateMessage> : <ReportBody report={report} view={isFinalizedReportView(visibleTab) ? visibleTab : 'drivers'} />}
        </div>
      </div>
    </div>
  );
}
