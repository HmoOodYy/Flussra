import { useCallback, useState } from 'react';
import { useAuth } from '../../../store/authStore';
import { canManageSettingsAdmin } from '../../../lib/permissions';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import styles from './StatusKeysPage.module.css';
import { BranchIcon, CheckIcon } from './StatusKeysDisplay';
import { StatusKeyEditorDialog } from './StatusKeyEditorDialog';
import { StatusKeysWorkspace } from './StatusKeysWorkspace';
import { useStatusKeyBranches } from './useStatusKeyBranches';
import { useStatusKeyCollection } from './useStatusKeyCollection';
import { useStatusKeyEditor } from './useStatusKeyEditor';
import type { ConfirmKind } from './statusKeysModel';

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

  const collection = useStatusKeyCollection({
    selectedBranchId: branches.selectedBranchId,
    showToast,
    setConfirmKind,
  });
  const editor = useStatusKeyEditor({
    selectedBranchId: branches.selectedBranchId,
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
            Configure branch payroll entry status keys.
          </p>
        </div>
      </div>

      {/* Toast */}
      {toast && <div className={styles.successAlert}><CheckIcon /> {toast}</div>}

      {/* Branch context card */}
      <div className={styles.branchCtx}>
        <div className={styles.branchCtxLeft}>
          <span className={styles.branchCtxLabel}><BranchIcon /> Branch</span>
          {branches.branchesLoading ? (
            <span className={styles.branchCtxMuted}>Loading…</span>
          ) : branches.branches.length === 0 ? (
            <span className={styles.branchCtxMuted}>No branches available</span>
          ) : canEdit && branches.branches.length > 1 ? (
            <select
              className={styles.branchSelect}
              value={branches.selectedBranchId ?? ''}
              onChange={e => branches.setSelectedBranchId(Number(e.target.value))}
            >
              {branches.branches.map(b => (
                <option key={b.branch_id} value={b.branch_id}>
                  {b.branch_name} ({b.branch_code})
                </option>
              ))}
            </select>
          ) : (
            <span className={styles.branchCtxValue}>
              {branches.selectedBranch?.branch_name ?? '—'}
              {branches.selectedBranch?.branch_code && (
                <span className={styles.branchCodePill}>{branches.selectedBranch.branch_code}</span>
              )}
            </span>
          )}
        </div>
      </div>

      <StatusKeysWorkspace
        statusKeys={collection.statusKeys}
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

      {/* Confirm dialog */}
      <ConfirmDialog
        open={confirmKind !== null}
        title={
          confirmKind === 'save-key'         ? (editor.keyModal === 'create' ? 'Create status key?' : 'Save status key changes?') :
          confirmKind === 'deactivate-key'   ? 'Deactivate status key?' :
          confirmKind === 'reactivate-key'   ? 'Reactivate status key?' :
          'Confirm'
        }
        message={
          confirmKind === 'save-key'
            ? editor.keyModal === 'create'
              ? `Create status key "${editor.keyForm.key_name || '…'}"?`
              : `Save changes to "${editor.editingKey?.key_name}"?`
            : confirmKind === 'deactivate-key'
            ? `Deactivate "${collection.targetKey?.key_name}"? It will no longer appear in payroll entry.`
            : confirmKind === 'reactivate-key'
            ? `Reactivate "${collection.targetKey?.key_name}"? It will appear in payroll entry again.`
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
        savingKey={editor.savingKey}
        keySaveErr={editor.keySaveErr}
        requestSaveKey={editor.requestSaveKey}
      />

    </div>
  );
}
