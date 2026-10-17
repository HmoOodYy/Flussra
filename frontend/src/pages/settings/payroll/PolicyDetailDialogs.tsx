import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import { DraftEditorModal } from './DraftEditorModal';
import { draftCreatedLabel } from './payrollSetupsView';
import type { DraftResponse, SetupResponse, VersionResponse } from '../../../types/payrollSetup';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import type { DraftForm } from './draftEditor';
import type { EditForm } from './policyDetailTypes';
import type { ConfirmKind, DraftEditorMode } from './policyDetailTypes';
import styles from './PayrollSetupsPage.module.css';

type Props = {
  confirmKind: ConfirmKind | null;
  setConfirmKind: (kind: ConfirmKind | null) => void;
  mutating: boolean;
  handleConfirm: () => void;
  discardTarget: DraftResponse | null;
  setDiscardTarget: (draft: DraftResponse | null) => void;
  archiveMessage: string;
  confirmDiscardDraft: () => void;
  editOpen: boolean;
  selectedPolicy: SetupResponse | null;
  closeEditModal: () => void;
  editForm: EditForm;
  setEditForm: React.Dispatch<React.SetStateAction<EditForm>>;
  editError: ApiErrorInfo | null;
  submitEdit: () => void;
  draftEditorOpen: boolean;
  draftEditorMode: DraftEditorMode;
  editingDraftId: number | null;
  draftEditorInitialForm: DraftForm;
  versions: VersionResponse[];
  canPublish: boolean;
  setMutating: (value: boolean) => void;
  closeDraftEditorModal: () => void;
  setDraftEditorOpen: (open: boolean) => void;
  setSetupDataReloadKey: React.Dispatch<React.SetStateAction<number>>;
  showToast: (message: string) => void;
};

export function PolicyDetailDialogs(p: Props) {
  const {
    confirmKind,
    setConfirmKind,
    mutating,
    handleConfirm,
    discardTarget,
    setDiscardTarget,
    archiveMessage,
    confirmDiscardDraft,
    editOpen,
    selectedPolicy,
    closeEditModal,
    editForm,
    setEditForm,
    editError,
    submitEdit,
    draftEditorOpen,
    draftEditorMode,
    editingDraftId,
    draftEditorInitialForm,
    versions,
    canPublish,
    setMutating,
    closeDraftEditorModal,
    setDraftEditorOpen,
    setSetupDataReloadKey,
    showToast,
  } = p;
  return (
    <>
      {/* ── Confirm dialogs (policy-level) ── */}
      <ConfirmDialog
        open={confirmKind === 'clear-default'}
        title="Clear company default?"
        message="New branches will no longer have a default payroll policy available for automatic onboarding until another default is selected. Existing branch assignments are unchanged."
        confirmLabel="Clear default"
        variant="danger"
        loading={mutating}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmKind(null)}
      />
      <ConfirmDialog
        open={confirmKind === 'set-default'}
        title="Set company default?"
        message="Affects only branches created later; existing branch assignments never change."
        confirmLabel="Set default"
        loading={mutating}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmKind(null)}
      />
      <ConfirmDialog
        open={confirmKind === 'archive'}
        title="Archive payroll policy?"
        message={archiveMessage}
        confirmLabel="Archive"
        variant="danger"
        loading={mutating}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmKind(null)}
      />
      <ConfirmDialog
        open={discardTarget != null}
        title="Discard draft schedule?"
        message={
          discardTarget
            ? `Discard ${draftCreatedLabel(discardTarget.created_at_utc)}? Published policy updates are not changed.`
            : ''
        }
        confirmLabel="Discard"
        variant="danger"
        loading={mutating}
        onConfirm={confirmDiscardDraft}
        onCancel={() => setDiscardTarget(null)}
      />

      {/* ── Edit policy modal ── */}
      {editOpen && selectedPolicy && (
        <div
          className={styles.modalOverlay}
          onClick={(e) => {
            if (e.target === e.currentTarget) closeEditModal();
          }}
        >
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>{`Edit — ${selectedPolicy.setup_name}`}</h2>
              <button className={styles.modalCloseBtn} onClick={closeEditModal}>
                ×
              </button>
            </div>
            <div className={styles.modalBody}>
              <div className={styles.formGroup}>
                <label className={styles.label}>Policy name</label>
                <input
                  className={styles.input}
                  value={editForm.setup_name}
                  maxLength={200}
                  onChange={(e) => setEditForm((f) => ({ ...f, setup_name: e.target.value }))}
                  disabled={mutating}
                />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label}>Description</label>
                <textarea
                  className={styles.textarea}
                  value={editForm.description}
                  onChange={(e) => setEditForm((f) => ({ ...f, description: e.target.value }))}
                  disabled={mutating}
                />
              </div>
              {editError && <PayrollErrorNotice info={editError} />}
            </div>
            <div className={styles.modalFooter}>
              <button className={styles.btnPrimary} onClick={submitEdit} disabled={mutating}>
                {mutating ? 'Working…' : 'Save changes'}
              </button>
              <button className={styles.btnSecondary} onClick={closeEditModal} disabled={mutating}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Draft editor modal (create + edit) ── */}
      {draftEditorOpen && selectedPolicy && (
        <DraftEditorModal
          setupId={selectedPolicy.setup_id}
          mode={draftEditorMode}
          editingDraftId={editingDraftId}
          initialForm={draftEditorInitialForm}
          versions={versions}
          canPublish={canPublish}
          mutating={mutating}
          setMutating={setMutating}
          onClose={closeDraftEditorModal}
          onSaved={() => {
            setDraftEditorOpen(false);
            setSetupDataReloadKey((k) => k + 1);
          }}
          onDraftSaved={() => setSetupDataReloadKey((k) => k + 1)}
          onPublished={() => {
            setDraftEditorOpen(false);
            setSetupDataReloadKey((k) => k + 1);
          }}
          showToast={showToast}
        />
      )}
    </>
  );
}
