/**
 * PayRatesPage — /people/pay-rates
 *
 * Two-panel layout:
 *  Left : filterable driver list (one row per permanent Driver the user can read)
 *  Right: selected driver panel with tabs
 *         Current Rates | Pending Changes | History | Pay Rules
 *
 * Ordinary PayDefinition rates (Driver rate schedules) and Min/Max pay rules belong to the
 * permanent Driver and are addressed by driver_id. Status pay rates are the one exception:
 * they stay owned by a Branch assignment, so their pane keeps an assignment picker.
 */
import { useEffect } from 'react';
import { Link } from 'react-router-dom';
import { useAuth } from '../../../store/authStore';
import { canEditPayRates } from '../../../lib/permissions';
import styles from './PayRatesPage.module.css';
import { usePayRateDriverContext } from './usePayRateDriverContext';
import { usePayRateReads } from './usePayRateReads';
import { useTargetRateEditing } from './useTargetRateEditing';
import { useStatusRateEditing } from './useStatusRateEditing';
import { useRateScheduleActions } from './useRateScheduleActions';
import { useDriverPayRules } from './useDriverPayRules';
import { PayRateDriverList } from './PayRateDriverList';
import { TargetRateSection } from './TargetRateSection';
import { StatusRateSection } from './StatusRateSection';
import { PendingRateChanges } from './PendingRateChanges';
import { RateHistoryTab, VoidRateDialog } from './RateHistoryTab';
import { DriverPayRulesTab } from './DriverPayRulesTab';
import {
  PAST_BRANCH_BANNER,
  canDiscardPendingRates,
  canEditDriverCompensation,
  isPastBranchView,
  canEditStatusRates,
  driverBranchSummary,
  visibleTabs,
} from './payRatesDrivers';

export function PayRatesPage() {
  const { user } = useAuth();
  const driver = usePayRateDriverContext(user);
  const selectedDriverId = driver.selectedDriverId;
  const selectedDriver = driver.selectedDriver;
  const statusAssignment = driver.statusAssignment;
  const statusAssignmentId = statusAssignment?.driver_assignment_id ?? null;
  const reads = usePayRateReads(selectedDriverId, statusAssignmentId);

  // Edit controls follow the backend's authority: the Driver's current Branch for Driver-owned
  // data, the viewed assignment for Status rates. The backend stays authoritative.
  const canEditAtBranch = (branchId: number) => (user ? canEditPayRates(user, branchId) : false);
  const canEditDriver = canEditDriverCompensation(selectedDriver, reads.access, canEditAtBranch);
  const canDiscardDriver = canDiscardPendingRates(reads.access);
  const canEditStatus = canEditStatusRates(statusAssignment, canEditAtBranch);
  const authorityBranchId = (selectedDriver?.current ?? selectedDriver?.pending ?? selectedDriver?.home)?.branch_id;
  const businessDateOf = (branchId: number | undefined) =>
    driver.branches.find((b) => b.branch_id === branchId)?.business_date;
  const driverReferenceDate = businessDateOf(authorityBranchId);
  const statusReferenceDate = reads.statusMatrix?.driver_assignment_id === statusAssignmentId
    ? reads.statusMatrix.as_of
    : businessDateOf(statusAssignment?.branch_id);

  const target = useTargetRateEditing(selectedDriverId, reads.refreshTargetRows, driverReferenceDate);
  const status = useStatusRateEditing(statusAssignmentId, reads.statusMatrix, reads.loadStatus, statusReferenceDate);
  const actions = useRateScheduleActions(selectedDriverId, reads.reloadAll);
  const rules = useDriverPayRules(selectedDriverId, reads.loadPayRules, reads.setPayRulesError);
  const { activeTab } = driver;
  const { rows, loadingRows, historyLoaded, loadingHistory, payRulesLoaded, loadingPayRules, setHistoryRows, setHistoryLoaded, setPayRules, setPayRulesLoaded, setPayRulesError, loadRates, loadStatus, loadHistory, loadPayRules } = reads;
  const { setPendingActionError } = actions;
  const { setTargetDrafts, setTargetErrors } = target;
  const { setStatusEditMode, setStatusDrafts, setStatusOriginal, setStatusSaveError } = status;
  const { setRuleForm, setRuleFormError } = rules;

  // ── When the selected driver changes: reset Driver-owned state + load ─────
  useEffect(() => {
    if (selectedDriverId == null) return;
    let cancelled = false;
    void (async () => {
      if (cancelled) return;
      setHistoryRows([]);
      setHistoryLoaded(false);
      setPendingActionError('');
      setTargetDrafts({});
      setTargetErrors({});
      setPayRules([]);
      setPayRulesLoaded(false);
      setPayRulesError('');
      setRuleForm(null);
      setRuleFormError('');
      await loadRates(selectedDriverId);
    })();
    return () => { cancelled = true; };
  }, [selectedDriverId, loadRates, setHistoryRows, setHistoryLoaded, setPendingActionError, setTargetDrafts, setTargetErrors, setPayRules, setPayRulesLoaded, setPayRulesError, setRuleForm, setRuleFormError]);

  // ── When the Status assignment changes: reset Status edits + load its matrix ──
  useEffect(() => {
    if (statusAssignmentId == null) return;
    let cancelled = false;
    void (async () => {
      if (cancelled) return;
      setStatusEditMode(false);
      setStatusDrafts({});
      setStatusOriginal({});
      setStatusSaveError('');
      await loadStatus(statusAssignmentId);
    })();
    return () => { cancelled = true; };
  }, [statusAssignmentId, loadStatus, setStatusEditMode, setStatusDrafts, setStatusOriginal, setStatusSaveError]);

  // ── Lazy tabs ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (activeTab !== 'history' || selectedDriverId == null || historyLoaded || loadingHistory || loadingRows) return;
    const driverId = selectedDriverId;
    void (async () => { await loadHistory(driverId, rows); })();
  }, [activeTab, selectedDriverId, historyLoaded, loadingHistory, loadingRows, rows, loadHistory]);

  useEffect(() => {
    if (activeTab !== 'pay-rules' || selectedDriverId == null || payRulesLoaded || loadingPayRules) return;
    const driverId = selectedDriverId;
    void (async () => { await loadPayRules(driverId); })();
  }, [activeTab, selectedDriverId, payRulesLoaded, loadingPayRules, loadPayRules]);

  function cancelAllEdits() {
    setTargetDrafts({});
    setTargetErrors({});
    setStatusEditMode(false);
    setStatusDrafts({});
    setStatusOriginal({});
    setStatusSaveError('');
  }

  const currencyCode = user?.currency_code ?? null;
  const currencyDigits = user?.currency_minor_unit_digits ?? null;
  const missingCount = reads.rows.filter((r) => r.current === null).length;
  const pendingRows = reads.rows.filter((r) => r.pending !== null);
  const statusPending = (reads.statusMatrix?.groups ?? []).filter((g) => g.pending_rate !== null);
  const pendingCount = pendingRows.length + statusPending.length;
  const futureCount = reads.rows.filter((r) => r.future !== null).length;
  const tabs = visibleTabs(reads.access);
  const tab = tabs.includes(activeTab) ? activeTab : 'current';
  const pastBranchView = isPastBranchView(reads.access);

  function openTab(next: typeof activeTab) {
    driver.setActiveTab(next);
    cancelAllEdits();
  }

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <p className={styles.title}>Pay Rates</p>
          <p className={styles.sub}>Set each driver&apos;s rates and pay rules. They follow the driver if they move Branch.</p>
        </div>
      </div>

      <div className={styles.body}>
        <PayRateDriverList
          search={driver.search} setSearch={driver.setSearch}
          branchFilter={driver.branchFilter} setBranchFilter={driver.setBranchFilter}
          statusFilter={driver.statusFilter} setStatusFilter={driver.setStatusFilter}
          tenureFilter={driver.tenureFilter} setTenureFilter={driver.setTenureFilter}
          branches={driver.branches} loadingDrivers={driver.loadingDrivers}
          filteredDrivers={driver.filteredDrivers} selectedDriverId={selectedDriverId}
          selectDriver={driver.selectDriver}
        />

        <div className={styles.right}>
          {driver.noProfile ? (
            <div className={styles.emptyRight}>
              <div className={styles.emptyRightCard}>
                <p className={styles.emptyRightTitle}>No driver profile</p>
                <p className={styles.emptyRightMsg}>This user does not have a driver profile yet.</p>
                <Link to="/people" style={{ fontSize: '0.83rem', color: '#6366f1' }}>Back to People</Link>
              </div>
            </div>
          ) : selectedDriverId == null ? (
            <div className={styles.emptyRight}>
              <div className={styles.emptyRightCard}>
                <p className={styles.emptyRightTitle}>Select a driver</p>
                <p className={styles.emptyRightMsg}>Choose a driver from the list to view and manage their pay rates.</p>
              </div>
            </div>
          ) : selectedDriver == null ? (
            driver.loadingDrivers ? (
              <div className={styles.loading}>Loading driver...</div>
            ) : (
              <div className={styles.emptyRight}>
                <div className={styles.emptyRightCard}>
                  <p className={styles.emptyRightTitle}>Driver not available</p>
                  <p className={styles.emptyRightMsg}>This driver does not exist or you cannot view their pay rates.</p>
                </div>
              </div>
            )
          ) : reads.loadingRows && reads.rows.length === 0 && !reads.statusMatrix ? (
            <div className={styles.loading}>Loading rates...</div>
          ) : reads.rowsError ? (
            <div className={styles.detail}><div className={styles.errBanner}>{reads.rowsError}</div></div>
          ) : (
            <div className={styles.detailTabbed}>
              <div className={styles.dHeaderCard} style={{ marginBottom: '0.75rem' }}>
                <div className={styles.dHeaderTop}>
                  <div>
                    <p className={styles.dName}>{selectedDriver.fullName}</p>
                    <p className={styles.dMeta}>{selectedDriver.driverNumber ?? 'No number'} &middot; {driverBranchSummary(selectedDriver)}</p>
                    {pastBranchView && <p className={styles.readOnlyBanner}>{PAST_BRANCH_BANNER}</p>}
                    <div className={styles.summaryBadges}>
                      {missingCount > 0 && <span className={styles.missingChip}>{missingCount} missing</span>}
                      {pendingCount > 0 && <span className={styles.pendingBadge}>{pendingCount} pending</span>}
                      {futureCount > 0 && <span className={styles.futureBadge}>{futureCount} future approved</span>}
                    </div>
                  </div>
                  <div className={styles.dActions}></div>
                </div>
              </div>

              <div className={styles.tabs}>
                <button className={`${styles.tab} ${tab === 'current' ? styles.tabActive : ''}`} onClick={() => openTab('current')}>Current Rates</button>
                {tabs.includes('pending') && (
                  <button className={`${styles.tab} ${tab === 'pending' ? styles.tabActive : ''}`} onClick={() => openTab('pending')}>
                    Pending Changes {pendingCount > 0 && <span className={`${styles.tabCount} ${styles.tabCountWarn}`}>{pendingCount}</span>}
                  </button>
                )}
                <button className={`${styles.tab} ${tab === 'history' ? styles.tabActive : ''}`} onClick={() => openTab('history')}>History</button>
                <button className={`${styles.tab} ${tab === 'pay-rules' ? styles.tabActive : ''}`} onClick={() => openTab('pay-rules')}>Pay Rules</button>
              </div>

              <div className={styles.tabPanel}>
                {tab === 'current' && <>
                  <TargetRateSection rows={reads.rows} canEditMatrix={canEditDriver} referenceDate={driverReferenceDate} currencyCode={currencyCode} currencyDigits={currencyDigits}
                    targetDrafts={target.targetDrafts} targetErrors={target.targetErrors} targetSavingId={target.targetSavingId}
                    setTargetField={target.setTargetField} saveTargetRow={target.saveTargetRow} cancelTargetEdit={target.cancelTargetEdit} startTargetEdit={target.startTargetEdit} />
                  <StatusRateSection assignments={selectedDriver.tenures} selectedAssignmentId={statusAssignmentId} selectAssignment={driver.selectStatusAssignment}
                    statusMatrix={reads.statusMatrix} canEditMatrix={canEditStatus} referenceDate={statusReferenceDate} currencyCode={currencyCode} currencyDigits={currencyDigits}
                    statusEditMode={status.statusEditMode} statusDrafts={status.statusDrafts} statusSaving={status.statusSaving} statusSaveError={status.statusSaveError}
                    dirtyStatusKeys={status.dirtyStatusKeys} statusKey={status.statusKey} startStatusEdit={status.startStatusEdit} setStatusField={status.setStatusField}
                    isStatusDirty={status.isStatusDirty} saveStatusRates={status.saveStatusRates} cancelStatusEdit={status.cancelStatusEdit} />
                </>}
                {tab === 'pending' && <PendingRateChanges pendingRows={pendingRows} statusPending={statusPending} pendingCount={pendingCount}
                  canEditDriver={canEditDriver} canDiscardDriver={canDiscardDriver} canEditStatus={canEditStatus} actioningId={actions.actioningId} pendingActionError={actions.pendingActionError}
                  currencyCode={currencyCode} currencyDigits={currencyDigits} runPendingAction={actions.runPendingAction} />}
                {tab === 'history' && <RateHistoryTab historyRows={reads.historyRows} loadingHistory={reads.loadingHistory} canEditMatrix={canEditDriver}
                  currencyCode={currencyCode} currencyDigits={currencyDigits} setVoidTarget={actions.setVoidTarget} setVoidReason={actions.setVoidReason} setVoidError={actions.setVoidError} />}
                {tab === 'pay-rules' && <DriverPayRulesTab payRules={reads.payRules} payRulesError={reads.payRulesError} loadingPayRules={reads.loadingPayRules}
                  canEditMatrix={canEditDriver} referenceDate={driverReferenceDate} currencyCode={user?.currency_code} currencyDigits={user?.currency_minor_unit_digits}
                  ruleForm={rules.ruleForm} setRuleForm={rules.setRuleForm} savingRule={rules.savingRule} ruleFormError={rules.ruleFormError}
                  setRuleFormError={rules.setRuleFormError} voidPayRule={rules.voidPayRule} createPayRule={rules.createPayRule} endPayRule={rules.endPayRule} />}
              </div>

              <VoidRateDialog voidTarget={actions.voidTarget} voiding={actions.voiding} voidReason={actions.voidReason} voidError={actions.voidError}
                setVoidTarget={actions.setVoidTarget} setVoidReason={actions.setVoidReason} executeVoid={actions.executeVoid} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
