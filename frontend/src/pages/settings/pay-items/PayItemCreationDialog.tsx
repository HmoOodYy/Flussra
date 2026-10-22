import type { FormEvent } from 'react';
import type { OrdinalTierInput, PayDefinitionCalculationMethod, PayDefinitionInputType } from '../../../types/compensation';
import { INPUT_TYPE_LABELS, METHOD_LABELS, validatePayMethod } from './payItemsModel';
import { OrdinalTierEditor } from './OrdinalTierEditor';
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
  createCalculationMethod: PayDefinitionCalculationMethod;
  setCreateCalculationMethod(value: PayDefinitionCalculationMethod): void;
  createOrdinalTiers: OrdinalTierInput[];
  setCreateOrdinalTiers(value: OrdinalTierInput[]): void;
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
  createInputType, setCreateInputType, createCalculationMethod, setCreateCalculationMethod,
  createOrdinalTiers, setCreateOrdinalTiers, createUnit, setCreateUnit, createNotes, setCreateNotes,
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
                  onChange={e => setCreateInputType(e.target.value as PayDefinitionInputType)} disabled={createSaving || createCalculationMethod === 'OrdinalTier'}>
                  <option value="Decimal">{INPUT_TYPE_LABELS.Decimal}</option>
                  <option value="WholeNumber">{INPUT_TYPE_LABELS.WholeNumber}</option>
                </select>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="create-method">Pay method</label>
                <select id="create-method" className={styles.input} value={createCalculationMethod}
                  onChange={e => setCreateCalculationMethod(e.target.value as PayDefinitionCalculationMethod)} disabled={createSaving}>
                  <option value="PerUnit">{METHOD_LABELS.PerUnit}</option>
                  <option value="OrdinalTier">{METHOD_LABELS.OrdinalTier}</option>
                </select>
                <div className={styles.inputNote}>{createCalculationMethod === 'PerUnit'
                  ? 'Quantity is multiplied by the Driver’s rate.'
                  : 'Each whole-number quantity unit uses the Driver’s rate for its tier.'}</div>
              </div>
              {createCalculationMethod === 'OrdinalTier' && (
                <OrdinalTierEditor tiers={createOrdinalTiers} setTiers={setCreateOrdinalTiers} disabled={createSaving} />
              )}
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="create-unit">Unit <span className={styles.optional}>(optional)</span></label>
                <input id="create-unit" className={styles.input} maxLength={50} value={createUnit}
                  onChange={e => setCreateUnit(e.target.value)} disabled={createSaving} />
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
              <button type="submit" className={styles.btnPrimary} disabled={createSaving || !createName.trim()
                || Boolean(validatePayMethod(createCalculationMethod, createInputType, createCalculationMethod === 'OrdinalTier' ? createOrdinalTiers : []))}>
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
