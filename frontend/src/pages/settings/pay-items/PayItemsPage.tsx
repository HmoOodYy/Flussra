import { useCallback, useState } from 'react';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { useAuth } from '../../../store/authStore';
import {
  canConfigureAllBranchPayDefinitions,
  canConfigureBranchPayDefinitions,
  canCreatePayDefinitionDirectly,
  canGovernPayDefinitionsCompanyWide,
  canManagePayDefinitionsForBranch,
} from '../../../lib/permissions';
import { usePayItemBranchContext } from './usePayItemBranchContext';
import { useCompanyPayDefinitions } from './useCompanyPayDefinitions';
import { usePayItemApplicability } from './usePayItemApplicability';
import { usePayDefinitionRequests } from './usePayDefinitionRequests';
import { usePayItemConfiguration } from './usePayItemConfiguration';
import { usePayItemCreation } from './usePayItemCreation';
import { PayItemCatalog } from './PayItemCatalog';
import { PayDefinitionRequestsPanel } from './PayDefinitionRequestsPanel';
import { ConfigurationEditor, BulkConfigurationResult } from './BranchConfigurationDialogs';
import { PayItemCreationDialog } from './PayItemCreationDialog';
import styles from './PayItemsPage.module.css';

export function PayItemsPage() {
  const { user } = useAuth();
  const canAllBranches = canConfigureAllBranchPayDefinitions(user);
  const canDirectCreate = canCreatePayDefinitionDirectly(user);
  const canGovern = canGovernPayDefinitionsCompanyWide(user);

  const branchContext = usePayItemBranchContext();
  const {
    branches, branchesError, branchMode, setBranchMode, selectedBranchId,
    setSelectedBranchId, activeBranches, branchName,
  } = branchContext;
  const [pageTab, setPageTab] = useState<'items' | 'requests'>('items');
  const [reloadKey, setReloadKey] = useState(0);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [toast, setToast] = useState('');

  const showToast = useCallback((message: string) => {
    setToast(message);
    setTimeout(() => setToast(''), 4000);
  }, []);

  const branchEditable = selectedBranchId !== null && canConfigureBranchPayDefinitions(user, selectedBranchId);
  const canRequest = !canDirectCreate && selectedBranchId !== null
    && canManagePayDefinitionsForBranch(user, selectedBranchId);
  const showAddButton = canDirectCreate || canRequest;
  const showRequestsTab = canGovern || (selectedBranchId !== null
    && canManagePayDefinitionsForBranch(user, selectedBranchId));

  const company = useCompanyPayDefinitions({
    canGovern, reloadKey, selectedId, setSelectedId, setReloadKey, showToast,
  });
  const { selectedCompany, retireOpen, setRetireOpen, retireWorking, confirmRetire } = company;
  const applicability = usePayItemApplicability({
    branchMode, selectedBranchId, branches, activeBranches, reloadKey, selectedId, setSelectedId,
  });
  const { setSearch, setStatusFilter } = applicability;
  const requests = usePayDefinitionRequests({
    selectedBranchId, canGovern, showRequestsTab, pageTab, setReloadKey, showToast,
  });
  const configuration = usePayItemConfiguration({
    branchMode, selectedBranchId,
    selectedBranchBusinessDate: branches.find((branch) => branch.branch_id === selectedBranchId)?.business_date,
    selectedId, selectedItem: applicability.selectedItem,
    selectedAgg: applicability.selectedAgg, setReloadKey, showToast,
  });
  const creation = usePayItemCreation({
    canDirectCreate, selectedBranchId, setReloadKey, setPageTab,
    setRequestFilter: requests.setRequestFilter, setRequestKey: requests.setRequestKey, showToast,
  });

  const { setCreateError, setCreateOpen } = creation;

  return (
    <div className={styles.page}>
      {toast && <div className={styles.successAlert}>{toast}</div>}

      <div className={styles.branchBar}>
        <span className={styles.branchBarLabel}>View</span>
        {canAllBranches ? (
          <div className={styles.modeGroup}>
            {(['single', 'all'] as const).map(m => (
              <button key={m} type="button"
                className={`${styles.modeBtn}${branchMode === m ? ` ${styles.modeBtnActive}` : ''}`}
                onClick={() => { setBranchMode(m); setSearch(''); setStatusFilter('all'); }}>
                {m === 'single' ? 'Single Branch' : 'All Branches'}
              </button>
            ))}
          </div>
        ) : (
          <span className={styles.branchBarLabel}>Single Branch</span>
        )}
        <div className={styles.branchSep} />
        {branchMode === 'single' ? (
          <select className={styles.input} style={{ width: 'auto' }}
            value={selectedBranchId ?? ''} aria-label="Branch"
            onChange={e => setSelectedBranchId(Number(e.target.value))}>
            {branches.map(b => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
          </select>
        ) : (
          <span className={styles.branchTag}>All {activeBranches.length} active branches</span>
        )}
        <div style={{ flex: 1 }} />
        <p className={styles.pageSubtitle} style={{ margin: 0 }}>
          Pay items belong to the company; set which branches can use each one. Changes take effect from a chosen date.
        </p>
        <div className={styles.pageActions}>
          {!showAddButton && <span className={styles.readonlyNote}>Read-only</span>}
          {showAddButton && (
            <button type="button" className={styles.btnPrimary}
              onClick={() => { setCreateError(''); setCreateOpen(true); }}>
              + Add Pay Item
            </button>
          )}
        </div>
      </div>

      {branchesError && <div className={styles.errorAlert}>{branchesError}</div>}

      {showRequestsTab && (
        <div className={styles.pageTabs} role="tablist">
          {(['items', 'requests'] as const).map(tab => (
            <button key={tab} role="tab" type="button" aria-selected={pageTab === tab}
              className={`${styles.pageTabBtn}${pageTab === tab ? ` ${styles.pageTabBtnActive}` : ''}`}
              onClick={() => setPageTab(tab)}>
              {tab === 'items' ? 'Pay Items' : 'Requests'}
            </button>
          ))}
        </div>
      )}
      {(!showRequestsTab || pageTab === 'items') && (
        <PayItemCatalog
          branchMode={branchMode}
          loading={applicability.loading}
          loadError={applicability.loadError}
          statusFilter={applicability.statusFilter}
          setStatusFilter={applicability.setStatusFilter}
          search={applicability.search}
          setSearch={applicability.setSearch}
          visibleItems={applicability.visibleItems}
          selectedId={selectedId}
          setSelectedId={setSelectedId}
          setHistory={applicability.setHistory}
          visibleAggregates={applicability.visibleAggregates}
          selectedItem={applicability.selectedItem}
          selectedAgg={applicability.selectedAgg}
          selectedCompany={company.selectedCompany}
          branchName={branchName}
          canAllBranches={canAllBranches}
          canGovern={canGovern}
          setRetireOpen={company.setRetireOpen}
          branchEditable={branchEditable}
          openEdit={configuration.openEdit}
          loadHistory={applicability.loadHistory}
          history={applicability.history}
        />
      )}

      <PayDefinitionRequestsPanel
        showRequestsTab={showRequestsTab}
        pageTab={pageTab}
        requestFilter={requests.requestFilter}
        setRequestFilter={requests.setRequestFilter}
        requestsLoading={requests.requestsLoading}
        requestsError={requests.requestsError}
        requests={requests.requests}
        branchName={branchName}
        canGovern={canGovern}
        user={user}
        openDecide={requests.openDecide}
        openDraftEdit={requests.openDraftEdit}
        copyRejected={requests.copyRejected}
        toggleRequestHistory={requests.toggleRequestHistory}
        expandedRequestId={requests.expandedRequestId}
        requestEvents={requests.requestEvents}
        decideTarget={requests.decideTarget}
        decideAction={requests.decideAction}
        setDecideTarget={requests.setDecideTarget}
        decideSaving={requests.decideSaving}
        decideReason={requests.decideReason}
        setDecideReason={requests.setDecideReason}
        decideError={requests.decideError}
        executeDecide={requests.executeDecide}
        draftTarget={requests.draftTarget}
        setDraftTarget={requests.setDraftTarget}
        draftSaving={requests.draftSaving}
        draftName={requests.draftName}
        setDraftName={requests.setDraftName}
        draftInputType={requests.draftInputType}
        setDraftInputType={requests.setDraftInputType}
        draftCalculationMethod={requests.draftCalculationMethod}
        setDraftCalculationMethod={requests.setDraftCalculationMethod}
        draftOrdinalTiers={requests.draftOrdinalTiers}
        setDraftOrdinalTiers={requests.setDraftOrdinalTiers}
        draftUnit={requests.draftUnit}
        setDraftUnit={requests.setDraftUnit}
        draftNotes={requests.draftNotes}
        setDraftNotes={requests.setDraftNotes}
        draftError={requests.draftError}
        executeDraftEdit={requests.executeDraftEdit}
      />

      <ConfigurationEditor
        editOpen={configuration.editOpen}
        setEditOpen={configuration.setEditOpen}
        branchMode={branchMode}
        branches={branches}
        selectedBranchId={selectedBranchId}
        setSelectedBranchId={setSelectedBranchId}
        editTarget={configuration.editTarget}
        setEditTarget={configuration.setEditTarget}
        editActive={configuration.editActive}
        setEditActive={configuration.setEditActive}
        editUseDate={configuration.editUseDate}
        setEditUseDate={configuration.setEditUseDate}
        editDate={configuration.editDate}
        setEditDate={configuration.setEditDate}
        editNotes={configuration.editNotes}
        setEditNotes={configuration.setEditNotes}
        editError={configuration.editError}
        editSaving={configuration.editSaving}
        setBulkConfirmOpen={configuration.setBulkConfirmOpen}
        bulkConfirmOpen={configuration.bulkConfirmOpen}
        executeSave={configuration.executeSave}
      />

      <PayItemCreationDialog
        createOpen={creation.createOpen}
        setCreateOpen={creation.setCreateOpen}
        createName={creation.createName}
        setCreateName={creation.setCreateName}
        createCode={creation.createCode}
        setCreateCode={creation.setCreateCode}
        createInputType={creation.createInputType}
        setCreateInputType={creation.setCreateInputType}
        createCalculationMethod={creation.createCalculationMethod}
        setCreateCalculationMethod={creation.setCreateCalculationMethod}
        createOrdinalTiers={creation.createOrdinalTiers}
        setCreateOrdinalTiers={creation.setCreateOrdinalTiers}
        createUnit={creation.createUnit}
        setCreateUnit={creation.setCreateUnit}
        createNotes={creation.createNotes}
        setCreateNotes={creation.setCreateNotes}
        createSaving={creation.createSaving}
        createError={creation.createError}
        canDirectCreate={canDirectCreate}
        saveCreate={creation.saveCreate}
      />

      <ConfirmDialog
        open={retireOpen}
        title="Retire pay item?"
        message={`"${selectedCompany?.definition_name ?? ''}" will be retired. It can no longer be activated for a branch or given new rates; its history, rates and provenance are preserved.`}
        confirmLabel="Retire"
        variant="danger"
        loading={retireWorking}
        onConfirm={() => void confirmRetire()}
        onCancel={() => setRetireOpen(false)}
      />

      <BulkConfigurationResult bulkResult={configuration.bulkResult} setBulkResult={configuration.setBulkResult} />
    </div>
  );
}
