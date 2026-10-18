import type { BranchAdmin } from '../../../types/settings';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import type { BulkBranchConfigResult } from '../../../types/compensation';
import styles from './PayItemsPage.module.css';

interface ConfigurationEditorProps {
  editOpen: boolean;
  setEditOpen(value: boolean): void;
  branchMode: 'single' | 'all';
  branches: BranchAdmin[];
  selectedBranchId: number | null;
  setSelectedBranchId(value: number): void;
  editTarget: 'single' | 'all';
  setEditTarget(value: 'single' | 'all'): void;
  editActive: boolean;
  setEditActive(value: boolean): void;
  editUseDate: boolean;
  setEditUseDate(value: boolean): void;
  editDate: string;
  setEditDate(value: string): void;
  editNotes: string;
  setEditNotes(value: string): void;
  editError: string;
  editSaving: boolean;
  setBulkConfirmOpen(value: boolean): void;
  bulkConfirmOpen: boolean;
  executeSave(): Promise<void>;
}

export function ConfigurationEditor({
  editOpen, setEditOpen, branchMode, branches, selectedBranchId, setSelectedBranchId,
  editTarget, setEditTarget, editActive, setEditActive, editUseDate, setEditUseDate,
  editDate, setEditDate, editNotes, setEditNotes, editError, editSaving,
  setBulkConfirmOpen, bulkConfirmOpen, executeSave,
}: ConfigurationEditorProps) {
  return (
    <>
      {/* Edit configuration modal */}
      {editOpen && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget) setEditOpen(false); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}><h2 className={styles.modalTitle}>Edit Configuration</h2></div>
            <div className={styles.modalBody}>
              {branchMode === 'all' && (
                <div className={styles.targetSelector}>
                  <span className={styles.targetSelectorLabel}>Apply to</span>
                  <div className={styles.targetOptions}>
                    <label className={styles.targetOption}>
                      <input type="radio" name="editTarget" checked={editTarget === 'all'} onChange={() => setEditTarget('all')} />
                      All active branches
                    </label>
                    <label className={styles.targetOption}>
                      <input type="radio" name="editTarget" checked={editTarget === 'single'} onChange={() => setEditTarget('single')} />
                      <select className={styles.input} style={{ width: 'auto' }} value={selectedBranchId ?? ''}
                        onChange={e => { setEditTarget('single'); setSelectedBranchId(Number(e.target.value)); }}>
                        {branches.map(b => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
                      </select>
                    </label>
                  </div>
                </div>
              )}
              <div className={styles.toggleRow}>
                <div className={styles.toggleInfo}>
                  <span className={styles.toggleLabel}>Active</span>
                  <span className={styles.toggleDesc}>Whether this pay item is available to the branch.</span>
                </div>
                <label className={styles.switch}>
                  <input type="checkbox" checked={editActive} onChange={e => setEditActive(e.target.checked)} />
                  <span className={styles.switchTrack} />
                </label>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label}>Effective date</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.82rem' }}>
                  <input type="checkbox" checked={editUseDate} onChange={e => setEditUseDate(e.target.checked)} />
                  Pick a specific date
                </label>
                {editUseDate ? (
                  <input type="date" className={styles.input} value={editDate}
                    onChange={e => setEditDate(e.target.value)} />
                ) : (
                  <div className={styles.inputNote}>Each Branch uses its business date, subject to open payroll period rules.</div>
                )}
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="edit-notes">Notes</label>
                <textarea id="edit-notes" className={styles.textarea} maxLength={500} value={editNotes}
                  onChange={e => setEditNotes(e.target.value)} />
              </div>
              {editError && <div className={styles.errorAlert}>{editError}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button type="button" className={styles.btnPrimary} disabled={editSaving}
                onClick={() => (editTarget === 'all' ? setBulkConfirmOpen(true) : void executeSave())}>
                {editSaving ? 'Saving…' : editTarget === 'all' ? 'Apply to All Branches…' : 'Save Configuration'}
              </button>
              <button type="button" className={styles.btnSecondary} disabled={editSaving} onClick={() => setEditOpen(false)}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={bulkConfirmOpen}
        title="Apply to all branches?"
        message="This updates the configuration across all active branches in one atomic operation: either every branch is updated or none."
        confirmLabel="Apply to All Branches"
        variant="primary"
        loading={editSaving}
        onConfirm={() => { setBulkConfirmOpen(false); void executeSave(); }}
        onCancel={() => setBulkConfirmOpen(false)}
      />
    </>
  );
}

export function BulkConfigurationResult({ bulkResult, setBulkResult }: {
  bulkResult: BulkBranchConfigResult | null;
  setBulkResult(value: BulkBranchConfigResult | null): void;
}) {
  return (
    <>
      {/* Bulk result */}
      {bulkResult && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget) setBulkResult(null); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}><h2 className={styles.modalTitle}>Configuration Applied</h2></div>
            <div className={styles.modalBody}>
              <div className={styles.infoBanner}>
                {bulkResult.updated_branch_count} of {bulkResult.requested_branch_count} branch(es) updated in one transaction.
              </div>
              <div className={styles.bulkResultList}>
                {bulkResult.results.map(r => (
                  <div key={r.branch_id} className={`${styles.bulkResultItem} ${styles.bulkResultSuccess}`}>
                    {r.branch_name} — <strong>{r.status}</strong> (effective {r.effective_from})
                  </div>
                ))}
              </div>
            </div>
            <div className={styles.modalFooter}>
              <button type="button" className={styles.btnPrimary} onClick={() => setBulkResult(null)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
