import { useCallback, useState } from 'react';
import { useAuth } from '../../../store/authStore';
import { canManageSettingsAdmin } from '../../../lib/permissions';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import styles from './StatusKeysPage.module.css';
import { CheckIcon } from './StatusKeysDisplay';
import { StatusKeyEditorDialog } from './StatusKeyEditorDialog';
import { StatusKeysWorkspace } from './StatusKeysWorkspace';
import { StatusLimitsPanel } from './StatusLimitsPanel';
import { StatusRateColumnsPanel } from './StatusRateColumnsPanel';
import { useStatusKeyBranches } from './useStatusKeyBranches';
import { useStatusKeyCollection } from './useStatusKeyCollection';
import { useStatusKeyEditor } from './useStatusKeyEditor';
import { useStatusRateColumns } from './useStatusRateColumns';
import type { ConfirmKind } from './statusKeysModel';

/**
 * Settings > Status Keys. The Status catalog belongs to the Company and is shared by every
 * Branch: the list and editor have no Branch picker. Only the Branch usage limits are per Branch.
 */
export function StatusKeysPage() {
  const { user } = useAuth();
  const canEdit = user !== null && canManageSettingsAdmin(user);

  const branches = useStatusKeyBranches();
  const [toast, setToast] = useState('');

  // ── Confirmation dialog ───────────────────────────────────────────────────
  const [confirmKind, setConfirmKind] = useState<ConfirmKind | null>(null);
  const [confirming, setConfirming] = useState(false);

  // ── Toast ─────────────────────────────────────────────────────────────────
  const showToast = useCallback((msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(''), 4000);
  }, []);

  const collection = useStatusKeyCollection({ showToast, setConfirmKind });
  const columns = useStatusRateColumns(showToast);
  const editor = useStatusKeyEditor({
    statusKeys: collection.statusKeys,
    dispatchKeys: collection.dispatchKeys,
    showToast,
    setConfirmKind,
  });

  async function handleConfirm() {
    if (!confirmKind) return;
    setConfirming(true);
    try {
      if (confirmKind === 'save-key') {
        await editor.saveKey();
      } else if (confirmKind === 'deactivate-key' && collection.targetKey) {
        await collection.deactivateKey(collection.targetKey);
      } else if (confirmKind === 'reactivate-key' && collection.targetKey) {
        await collection.reactivateKey(collection.targetKey);
      }
    } finally {
      setConfirming(false);
      setConfirmKind(null);
    }
  }

  return (
    <div className={styles.page}>

      {/* Page header */}
      <div className={styles.pageHeader}>
        <div>
          <h1 className={styles.pageTitle}>Status Keys</h1>
          <p className={styles.pageSubtitle}>
            Statuses are shared by every Branch: set their names, aliases, hours and pay once for the
            Company, then limit their use per Branch.
          </p>
        </div>
      </div>

      {/* Toast */}
      {toast && <div className={styles.successAlert}><CheckIcon /> {toast}</div>}

      <StatusKeysWorkspace
        statusKeys={collection.statusKeys}
        visibleKeys={collection.visibleKeys}
        columns={columns.columns}
        keysLoading={collection.keysLoading}
        keysError={collection.keysError}
        keyStatusFilter={collection.keyStatusFilter}
        setKeyStatusFilter={collection.setKeyStatusFilter}
        canEdit={canEdit}
        openCreateKey={editor.openCreateKey}
        selectedKeyId={collection.selectedKeyId}
        dispatchKeys={collection.dispatchKeys}
        selectedKey={collection.selectedKey}
        deactivating={collection.deactivating}
        openEditKey={editor.openEditKey}
        requestDeactivate={collection.requestDeactivate}
        requestReactivate={collection.requestReactivate}
      />

      <StatusRateColumnsPanel columns={columns} statusKeys={collection.statusKeys} canEdit={canEdit} />

      <StatusLimitsPanel
        branches={branches.branches}
        branchesLoading={branches.branchesLoading}
        selectedBranchId={branches.selectedBranchId}
        setSelectedBranchId={branches.setSelectedBranchId}
        canEdit={canEdit}
      />

      {/* Confirm dialog */}
      <ConfirmDialog
        open={confirmKind !== null}
        title={
          confirmKind === 'save-key'         ? (editor.keyModal === 'create' ? 'Create Status?' : 'Save Status changes?') :
          confirmKind === 'deactivate-key'   ? 'Deactivate Status?' :
          confirmKind === 'reactivate-key'   ? 'Reactivate Status?' :
          'Confirm'
        }
        message={
          confirmKind === 'save-key'
            ? editor.keyModal === 'create'
              ? `Create Status "${editor.keyForm.key_name || '…'}" for every Branch?`
              : `Save changes to "${editor.editingKey?.key_name}"?`
            : confirmKind === 'deactivate-key'
            ? `Deactivate "${collection.targetKey?.key_name}"? It can no longer be chosen in payroll entry; days that already have it keep it.`
            : confirmKind === 'reactivate-key'
            ? `Reactivate "${collection.targetKey?.key_name}"? It can be chosen in payroll entry again.`
            : ''
        }
        confirmLabel={
          confirmKind === 'deactivate-key' ? 'Deactivate' :
          confirmKind === 'reactivate-key' ? 'Reactivate' :
          'Save'
        }
        variant={confirmKind === 'deactivate-key' ? 'danger' : 'primary'}
        loading={confirming || editor.savingKey || collection.deactivating !== null}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmKind(null)}
      />

      <StatusKeyEditorDialog
        keyModal={editor.keyModal}
        closeKeyModal={editor.closeKeyModal}
        editingKey={editor.editingKey}
        keyForm={editor.keyForm}
        setKeyForm={editor.setKeyForm}
        columns={columns.columns}
        savingKey={editor.savingKey}
        keySaveErr={editor.keySaveErr}
        requestSaveKey={editor.requestSaveKey}
      />

    </div>
  );
}
