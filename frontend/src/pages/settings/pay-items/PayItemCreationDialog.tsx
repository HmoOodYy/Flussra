import type { FormEvent } from 'react';
import type { PayDefinitionInputType } from '../../../types/compensation';
import { INPUT_TYPE_LABELS } from './payItemsModel';
import styles from './PayItemsPage.module.css';

interface PayItemCreationDialogProps {
  createOpen: boolean;
  setCreateOpen(value: boolean): void;
  createName: string;
  setCreateName(value: string): void;
  createCode: string;
  setCreateCode(value: string): void;
  createInputType: PayDefinitionInputType;
  setCreateInputType(value: PayDefinitionInputType): void;
  createUnit: string;
  setCreateUnit(value: string): void;
  createNotes: string;
  setCreateNotes(value: string): void;
  createSaving: boolean;
  createError: string;
  canDirectCreate: boolean;
  saveCreate(event: FormEvent): Promise<void>;
}

export function PayItemCreationDialog({
  createOpen, setCreateOpen, createName, setCreateName, createCode, setCreateCode,
  createInputType, setCreateInputType, createUnit, setCreateUnit, createNotes, setCreateNotes,
  createSaving, createError, canDirectCreate, saveCreate,
}: PayItemCreationDialogProps) {
  return (
    <>
      {/* Create modal */}
      {createOpen && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget && !createSaving) setCreateOpen(false); }}>
          <form className={styles.modal} onSubmit={saveCreate}>
            <div className={styles.modalHeader}><h2 className={styles.modalTitle}>Add Pay Item</h2></div>
            <div className={styles.modalBody}>
              <div className={styles.infoBanner}>
                {canDirectCreate
                  ? 'The pay item is created for the whole company and starts inactive in every branch.'
                  : 'Your request is sent to the company for approval. When approved, the pay item becomes active in this branch.'}
              </div>
              <div className={styles.formGroup} style={{ marginTop: '0.75rem' }}>
                <label className={styles.label} htmlFor="create-name">Name <span className={styles.required}>*</span></label>
                <input id="create-name" className={styles.input} maxLength={200} autoFocus value={createName}
                  onChange={e => setCreateName(e.target.value)} disabled={createSaving} />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="create-code">Code <span className={styles.optional}>(optional)</span></label>
                <input id="create-code" className={styles.input} maxLength={50} value={createCode}
                  onChange={e => setCreateCode(e.target.value)} disabled={createSaving} />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="create-input">Quantity entered as</label>
                <select id="create-input" className={styles.input} value={createInputType}
                  onChange={e => setCreateInputType(e.target.value as PayDefinitionInputType)} disabled={createSaving}>
                  <option value="Decimal">{INPUT_TYPE_LABELS.Decimal}</option>
                  <option value="WholeNumber">{INPUT_TYPE_LABELS.WholeNumber}</option>
                </select>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="create-unit">Unit <span className={styles.optional}>(optional)</span></label>
                <input id="create-unit" className={styles.input} maxLength={50} value={createUnit}
                  onChange={e => setCreateUnit(e.target.value)} disabled={createSaving} />
              </div>
              <div className={styles.formGroup}>
                <span className={styles.label}>Pay method</span>
                <div className={styles.inputNote}>Each unit is paid at the driver&apos;s own rate.</div>
              </div>
              {!canDirectCreate && (
                <div className={styles.formGroup}>
                  <label className={styles.label} htmlFor="create-notes">Notes <span className={styles.optional}>(optional)</span></label>
                  <textarea id="create-notes" className={styles.textarea} maxLength={500} value={createNotes}
                    onChange={e => setCreateNotes(e.target.value)} disabled={createSaving} />
                </div>
              )}
              {createError && <div className={styles.errorAlert}>{createError}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button type="submit" className={styles.btnPrimary} disabled={createSaving || !createName.trim()}>
                {createSaving ? 'Working…' : canDirectCreate ? 'Create Pay Item' : 'Submit for Approval'}
              </button>
              <button type="button" className={styles.btnSecondary} disabled={createSaving} onClick={() => setCreateOpen(false)}>Cancel</button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}