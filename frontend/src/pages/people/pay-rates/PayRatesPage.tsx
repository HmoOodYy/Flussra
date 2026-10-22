/**
 * PayRatesPage — /people/pay-rates
 *
 * Two-panel layout:
 *  Left : filterable driver list (one row per permanent Driver the user can read)
 *  Right: selected driver panel with tabs
 *         Current Rates | Pending Changes | History | Pay Rules
 *
 * Every rate is a schedule owned by the permanent Driver and addressed by driver_id: Pay Item
 * rates (PayDefinitions) and Status pay rates (Company Status rate columns) are authored with
 * the same compensation schedule routes, and Min/Max pay rules are the Driver's too. Nothing is
 * tied to a Branch assignment, so the rates follow the Driver across a transfer.
 */
import { useEffect } from 'react';
import { useAuth } from '../../../store/authStore';
import { canEditPayRates } from '../../../lib/permissions';
import styles from './PayRatesPage.module.css';
import { usePayRateDriverContext } from './usePayRateDriverContext';
import { usePayRateReads } from './usePayRateReads';
import { useTargetRateEditing } from './useTargetRateEditing';
import { useRateScheduleActions } from './useRateScheduleActions';
import { useDriverPayRules } from './useDriverPayRules';
import { PayRateDriverList } from './PayRateDriverList';
import { TargetRateSection } from './TargetRateSection';
import { PendingRateChanges } from './PendingRateChanges';
import { RateHistoryTab, VoidRateDialog } from './RateHistoryTab';
import { DriverPayRulesTab } from './DriverPayRulesTab';
import { buildPendingScheduleRows } from './payRatesPending';
import {
  PAST_BRANCH_BANNER,
  canDiscardPendingRates,
  canEditDriverCompensation,
  isPastBranchView,
  driverBranchSummary,
  visibleTabs,
} from './payRatesDrivers';

export function PayRatesPage() {
  const { user } = useAuth();
  const driver = usePayRateDriverContext(user);
  const selectedDriverId = driver.selectedDriverId;
  const selectedDriver = driver.selectedDriver;
  const reads = usePayRateReads(selectedDriverId);

  // Edit controls follow the backend's authority over the Driver (the current Branch).
  // The backend stays authoritative.
  const canEditAtBranch = (branchId: number) => (user ? canEditPayRates(user, branchId) : false);
  const canEditDriver = canEditDriverCompensation(selectedDriver, reads.access, canEditAtBranch);
  const canDiscardDriver = canDiscardPendingRates(reads.access);
  const authorityBranchId = (selectedDriver?.current ?? selectedDriver?.pending ?? selectedDriver?.home)?.branch_id;
  const businessDateOf = (branchId: number | undefined) =>
    driver.branches.find((b) => b.branch_id === branchId)?.business_date;
  const driverReferenceDate = businessDateOf(authorityBranchId);

  const target = useTargetRateEditing(selectedDriverId, reads.refreshTargetRows, driverReferenceDate);
  const actions = useRateScheduleActions(selectedDriverId, reads.reloadAll);
  const rules = useDriverPayRules(selectedDriverId, reads.loadPayRules, reads.setPayRulesError);
  const { activeTab } = driver;
  const { loadingRows, historyLoaded, loadingHistory, payRulesLoaded, loadingPayRules, setHistoryRows, setHistoryLoaded, setPayRules, setPayRulesLoaded, setPayRulesError, loadRates, loadHistory, loadPayRules } = reads;
  const { setPendingActionError } = actions;
  const { setTargetDrafts, setTargetErrors } = target;
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

  // ── Lazy tabs ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (activeTab !== 'history' || selectedDriverId == null || historyLoaded || loadingHistory || loadingRows) return;
    const driverId = selectedDriverId;
    void (async () => { await loadHistory(driverId); })();
  }, [activeTab, selectedDriverId, historyLoaded, loadingHistory, loadingRows, loadHistory]);

  useEffect(() => {
    if (activeTab !== 'pay-rules' || selectedDriverId == null || payRulesLoaded || loadingPayRules) return;
    const driverId = selectedDriverId;
    void (async () => { await loadPayRules(driverId); })();
  }, [activeTab, selectedDriverId, payRulesLoaded, loadingPayRules, loadPayRules]);

  function cancelAllEdits() {
    setTargetDrafts({});
    setTargetErrors({});
  }

  const currencyCode = user?.currency_code ?? null;
  const currencyDigits = user?.currency_minor_unit_digits ?? null;
  const payItemRows = reads.rows.filter((r) => r.kind === 'PayDefinition');
  const statusRows = reads.rows.filter((r) => r.kind === 'StatusRateColumn');
  // A Status rate column nobody uses is not a gap, so only Pay Item rates count as missing.
  const missingCount = payItemRows.filter((r) => r.current === null).length;
  // Pending schedules come from the schedule summary and history, not the Active-only matrix.
  const pendingRows = buildPendingScheduleRows(
    reads.pendingSource.summary, reads.pendingSource.historyByDefinition, reads.rows);
  const pendingCount = pendingRows.length;
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
          {selectedDriverId == null ? (
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
          ) : reads.loadingRows && reads.rows.length === 0 ? (
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
                  <TargetRateSection title="Pay Item Rates" rowHeader="Pay Item" empty={<>
                      <p>No pay items are available to rate yet.</p>
                      <p className={styles.emptySectionHint}>Pay items are created and activated per branch under <strong>Settings &gt; Pay Items</strong>.</p>
                    </>}
                    rows={payItemRows} canEditMatrix={canEditDriver} referenceDate={driverReferenceDate} currencyCode={currencyCode} currencyDigits={currencyDigits}
                    targetDrafts={target.targetDrafts} targetErrors={target.targetErrors} targetSavingId={target.targetSavingId}
                    setTargetField={target.setTargetField} saveTargetRow={target.saveTargetRow} cancelTargetEdit={target.cancelTargetEdit} startTargetEdit={target.startTargetEdit} />
                  <TargetRateSection title="Status Pay Rates" rowHeader="Status rate" note="A paid Status earns its hours times the driver's rate for its Status rate column. The rate belongs to the driver and follows them if they move Branch."
                    empty={<p>No Status rate columns are available yet. They are managed under <strong>Settings &gt; Status Keys</strong>.</p>}
                    rows={statusRows} canEditMatrix={canEditDriver} referenceDate={driverReferenceDate} currencyCode={currencyCode} currencyDigits={currencyDigits}
                    targetDrafts={target.targetDrafts} targetErrors={target.targetErrors} targetSavingId={target.targetSavingId}
                    setTargetField={target.setTargetField} saveTargetRow={target.saveTargetRow} cancelTargetEdit={target.cancelTargetEdit} startTargetEdit={target.startTargetEdit} />
                </>}
                {tab === 'pending' && <PendingRateChanges pendingRows={pendingRows} pendingCount={pendingCount}
                  canEditDriver={canEditDriver} canDiscardDriver={canDiscardDriver} actioningId={actions.actioningId} pendingActionError={actions.pendingActionError}
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
