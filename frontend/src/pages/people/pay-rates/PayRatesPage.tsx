/**
 * PayRatesPage — /people/pay-rates
 *
 * Two-panel layout:
 *  Left : filterable driver list
 *  Right: selected driver panel with tabs
 *         Current Rates | Pending Changes | History | Pay Rules
 *
 * Ordinary PayDefinition rates are target DriverRateAssignments. The Status pay
 * rate section below them still uses the temporary Status-only rate path until
 * Status compensation is cut over.
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
import { useRateAssignmentActions } from './useRateAssignmentActions';
import { useDriverPayRules } from './useDriverPayRules';
import { PayRateDriverList } from './PayRateDriverList';
import { TargetRateSection } from './TargetRateSection';
import { StatusRateSection } from './StatusRateSection';
import { PendingRateChanges } from './PendingRateChanges';
import { AssignmentHistoryTab, VoidRateDialog } from './AssignmentHistoryTab';
import { DriverPayRulesTab } from './DriverPayRulesTab';

export function PayRatesPage() {
  const { user } = useAuth();
  const driver = usePayRateDriverContext(user);
  const reads = usePayRateReads(driver.selectedDriverId);
  const branchBusinessDate = driver.branches.find((b) => b.branch_id === driver.selectedDriver?.branch_id)?.business_date;
  const referenceDate = reads.statusMatrix?.driver_id === driver.selectedDriverId
    ? reads.statusMatrix.as_of
    : branchBusinessDate;
  const target = useTargetRateEditing(driver.selectedDriverId, reads.refreshTargetRows, referenceDate);
  const status = useStatusRateEditing(driver.selectedDriverId, reads.statusMatrix, reads.loadRates, referenceDate);
  const actions = useRateAssignmentActions(driver.selectedDriverId, reads.reloadAll);
  const rules = useDriverPayRules(driver.selectedDriverId, reads.loadPayRules, reads.setPayRulesError);
  const { selectedDriverId, activeTab } = driver;
  const { rows, loadingRows, historyLoaded, loadingHistory, payRulesLoaded, loadingPayRules, setHistoryRows, setHistoryLoaded, setPayRules, setPayRulesLoaded, setPayRulesError, loadRates, loadHistory, loadPayRules } = reads;
  const { setPendingActionError } = actions;
  const { setTargetDrafts, setTargetErrors } = target;
  const { setStatusEditMode, setStatusDrafts, setStatusOriginal, setStatusSaveError } = status;
  const { setRuleForm, setRuleFormError } = rules;

  // ── When selected driver changes: reset state + load ──────────────────────
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
      setStatusEditMode(false);
      setStatusDrafts({});
      setStatusOriginal({});
      setStatusSaveError('');
      setPayRules([]);
      setPayRulesLoaded(false);
      setPayRulesError('');
      setRuleForm(null);
      setRuleFormError('');
      await loadRates(selectedDriverId);
    })();
    return () => { cancelled = true; };
  }, [selectedDriverId, loadRates, setHistoryRows, setHistoryLoaded, setPendingActionError, setTargetDrafts, setTargetErrors, setStatusEditMode, setStatusDrafts, setStatusOriginal, setStatusSaveError, setPayRules, setPayRulesLoaded, setPayRulesError, setRuleForm, setRuleFormError]);

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
  const canEditMatrix = user && driver.selectedDriver ? canEditPayRates(user, driver.selectedDriver.branch_id) : false;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <p className={styles.title}>Pay Rates</p>
          <p className={styles.sub}>Set each driver&apos;s rate for the pay items active in their branch.</p>
        </div>
      </div>

      <div className={styles.body}>
        <PayRateDriverList
          search={driver.search} setSearch={driver.setSearch}
          branchFilter={driver.branchFilter} setBranchFilter={driver.setBranchFilter}
          statusFilter={driver.statusFilter} setStatusFilter={driver.setStatusFilter}
          branches={driver.branches} loadingDrivers={driver.loadingDrivers}
          filteredDrivers={driver.filteredDrivers} selectedDriverId={driver.selectedDriverId}
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
          ) : driver.selectedDriverId == null ? (
            <div className={styles.emptyRight}>
              <div className={styles.emptyRightCard}>
                <p className={styles.emptyRightTitle}>Select a driver</p>
                <p className={styles.emptyRightMsg}>Choose a driver from the list to view and manage their pay rates.</p>
              </div>
            </div>
          ) : reads.loadingRows && reads.rows.length === 0 && !reads.statusMatrix ? (
            <div className={styles.loading}>Loading rates...</div>
          ) : reads.rowsError ? (
            <div className={styles.detail}><div className={styles.errBanner}>{reads.rowsError}</div></div>
          ) : (
            <div className={styles.detailTabbed}>
              <div className={styles.dHeaderCard} style={{ marginBottom: '0.75rem' }}>
                <div className={styles.dHeaderTop}>
                  <div>
                    <p className={styles.dName}>{driver.selectedDriver?.full_name ?? `Driver ${driver.selectedDriverId}`}</p>
                    <p className={styles.dMeta}>{driver.selectedDriver?.driver_code ?? 'No code'} &middot; {driver.selectedDriver?.branch_name ?? ''}</p>
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
                <button className={`${styles.tab} ${driver.activeTab === 'current' ? styles.tabActive : ''}`} onClick={() => { driver.setActiveTab('current'); cancelAllEdits(); }}>Current Rates</button>
                <button className={`${styles.tab} ${driver.activeTab === 'pending' ? styles.tabActive : ''}`} onClick={() => { driver.setActiveTab('pending'); cancelAllEdits(); }}>
                  Pending Changes {pendingCount > 0 && <span className={`${styles.tabCount} ${styles.tabCountWarn}`}>{pendingCount}</span>}
                </button>
                <button className={`${styles.tab} ${driver.activeTab === 'history' ? styles.tabActive : ''}`} onClick={() => { driver.setActiveTab('history'); cancelAllEdits(); }}>History</button>
                <button className={`${styles.tab} ${driver.activeTab === 'pay-rules' ? styles.tabActive : ''}`} onClick={() => { driver.setActiveTab('pay-rules'); cancelAllEdits(); }}>Pay Rules</button>
              </div>

              <div className={styles.tabPanel}>
                {driver.activeTab === 'current' && <>
                  <TargetRateSection rows={reads.rows} canEditMatrix={!!canEditMatrix} referenceDate={referenceDate} currencyCode={currencyCode} currencyDigits={currencyDigits}
                    targetDrafts={target.targetDrafts} targetErrors={target.targetErrors} targetSavingId={target.targetSavingId}
                    setTargetField={target.setTargetField} saveTargetRow={target.saveTargetRow} cancelTargetEdit={target.cancelTargetEdit} startTargetEdit={target.startTargetEdit} />
                  <StatusRateSection statusMatrix={reads.statusMatrix} canEditMatrix={!!canEditMatrix} referenceDate={referenceDate} currencyCode={currencyCode} currencyDigits={currencyDigits}
                    statusEditMode={status.statusEditMode} statusDrafts={status.statusDrafts} statusSaving={status.statusSaving} statusSaveError={status.statusSaveError}
                    dirtyStatusKeys={status.dirtyStatusKeys} statusKey={status.statusKey} startStatusEdit={status.startStatusEdit} setStatusField={status.setStatusField}
                    isStatusDirty={status.isStatusDirty} saveStatusRates={status.saveStatusRates} cancelStatusEdit={status.cancelStatusEdit} />
                </>}
                {driver.activeTab === 'pending' && <PendingRateChanges pendingRows={pendingRows} statusPending={statusPending} pendingCount={pendingCount}
                  canEditMatrix={!!canEditMatrix} actioningId={actions.actioningId} pendingActionError={actions.pendingActionError}
                  currencyCode={currencyCode} currencyDigits={currencyDigits} runPendingAction={actions.runPendingAction} />}
                {driver.activeTab === 'history' && <AssignmentHistoryTab historyRows={reads.historyRows} loadingHistory={reads.loadingHistory} canEditMatrix={!!canEditMatrix}
                  currencyCode={currencyCode} currencyDigits={currencyDigits} setVoidTarget={actions.setVoidTarget} setVoidReason={actions.setVoidReason} setVoidError={actions.setVoidError} />}
                {driver.activeTab === 'pay-rules' && <DriverPayRulesTab payRules={reads.payRules} payRulesError={reads.payRulesError} loadingPayRules={reads.loadingPayRules}
                  canEditMatrix={!!canEditMatrix} referenceDate={referenceDate} currencyCode={user?.currency_code} currencyDigits={user?.currency_minor_unit_digits}
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
