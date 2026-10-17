import type { Dispatch, SetStateAction } from 'react';
import type { KeyForm } from './statusKeysModel';
import { AlertIcon, CloseIcon, SpinnerIcon } from './StatusKeysDisplay';
import styles from './StatusKeysPage.module.css';

export function StatusKeyEditorDialog({
  keyModal, closeKeyModal, editingKey, keyForm, setKeyForm, savingKey, keySaveErr, requestSaveKey,
}: {
  keyModal: 'create' | 'edit' | null;
  closeKeyModal: () => void;
  editingKey: { key_name: string } | null;
  keyForm: KeyForm;
  setKeyForm: Dispatch<SetStateAction<KeyForm>>;
  savingKey: boolean;
  keySaveErr: string;
  requestSaveKey: () => void;
}) {
  return (
    <>
      {/* Status Key modal */}
      {keyModal && (
        <div
          className={styles.modalOverlay}
          onClick={e => { if (e.target === e.currentTarget) closeKeyModal(); }}
        >
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>
                {keyModal === 'create' ? 'Add Status Key' : `Edit — ${editingKey?.key_name}`}
              </h2>
              <button className={styles.modalCloseBtn} onClick={closeKeyModal}>
                <CloseIcon />
              </button>
            </div>
            <div className={styles.modalBody}>
              <KeyFormFields
                form={keyForm}
                onChange={p => setKeyForm(f => ({ ...f, ...p }))}
                disabled={savingKey}
                s={styles}
              />
              {keySaveErr && (
                <div className={styles.errorAlert} style={{ marginTop: '0.75rem' }}>
                  <AlertIcon /> {keySaveErr}
                </div>
              )}
            </div>
            <div className={styles.modalFooter}>
              <button className={styles.btnPrimary} onClick={requestSaveKey} disabled={savingKey}>
                {savingKey
                  ? <><SpinnerIcon /> Saving…</>
                  : keyModal === 'create' ? 'Create Key' : 'Save Changes'}
              </button>
              <button className={styles.btnSecondary} onClick={closeKeyModal} disabled={savingKey}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function KeyFormFields({
  form, onChange, disabled, s,
}: {
  form: KeyForm;
  onChange: (p: Partial<KeyForm>) => void;
  disabled: boolean;
  s: Record<string, string>;
}) {
  return (
    <div className={s.formGrid}>
      {/* Key Name */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>
          Key Name <span className={s.required}>*</span>
        </label>
        <p className={s.fieldHint}>
          The label shown in payroll entry (e.g. "Vacation", "Sick Day", "On Leave").
        </p>
        <input
          className={s.input}
          value={form.key_name}
          onChange={e => onChange({ key_name: e.target.value })}
          disabled={disabled}
          maxLength={200}
          autoFocus
        />
      </div>

      {/* Hours Value */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Hours Value</label>
        <p className={s.fieldHint}>Hours counted for this status (0–24).</p>
        <input
          type="number"
          className={s.input}
          value={form.hours_value}
          onChange={e => onChange({ hours_value: e.target.value })}
          disabled={disabled}
          min="0" max="24" step="0.5"
          style={{ maxWidth: '120px' }}
        />
      </div>

      {/* Is Off Reason */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.checkboxLabel}>
          <input
            type="checkbox"
            checked={form.is_off_reason}
            onChange={e => {
              const checked = e.target.checked;
              onChange({
                is_off_reason: checked,
                ...(!checked && { deducts_from_yearly_allowance: false, allowance_category: '' }),
              });
            }}
            disabled={disabled}
          />
          <span>
            <strong>Can be used as a leave/off reason</strong>
            <span className={s.checkboxDesc}> — used for future leave/time-off tracking during a payroll period</span>
          </span>
        </label>
      </div>

      {/* Deducts Allowance — future feature, hidden until yearly allowance tracking is available */}
      {/* Allowance Category — future feature, hidden until yearly allowance tracking is available */}

      {/* Usage Limits */}
      <div className={`${s.formGroup} ${s.formGroupFull}`} style={{ borderTop: '1px solid #f1f5f9', paddingTop: '0.75rem', marginTop: '0.25rem' }}>
        <label className={s.label} style={{ marginBottom: '0.4rem' }}>Usage Limits</label>
        <p className={s.fieldHint} style={{ marginBottom: '0.75rem' }}>
          Optionally cap how many times this key can be used in various scopes.
        </p>
        {([
          { label: 'Limit uses per period',         enabledKey: 'limit_uses_per_period_enabled' as keyof KeyForm,         valueKey: 'limit_uses_per_period' as keyof KeyForm },
          { label: 'Limit uses per driver',         enabledKey: 'limit_uses_per_driver_enabled' as keyof KeyForm,         valueKey: 'limit_uses_per_driver' as keyof KeyForm },
          { label: 'Limit uses across all drivers', enabledKey: 'limit_uses_across_drivers_enabled' as keyof KeyForm,     valueKey: 'limit_uses_across_drivers' as keyof KeyForm },
          { label: 'Limit uses per day',            enabledKey: 'limit_uses_per_day_enabled' as keyof KeyForm,            valueKey: 'limit_uses_per_day' as keyof KeyForm },
        ] as const).map(({ label, enabledKey, valueKey }) => {
          const enabled = form[enabledKey] as boolean;
          return (
            <div key={enabledKey} className={s.usageLimitRow}>
              <input
                type="checkbox"
                checked={enabled}
                onChange={e => onChange({ [enabledKey]: e.target.checked, ...(!e.target.checked && { [valueKey]: '' }) })}
                disabled={disabled}
                aria-label={label}
              />
              <span style={{ flex: 1, fontSize: '0.83rem', color: '#374151' }}>{label}</span>
              <input
                type="number"
                className={s.usageLimitInput}
                value={form[valueKey] as string}
                onChange={e => onChange({ [valueKey]: e.target.value })}
                disabled={disabled || !enabled}
                placeholder={enabled ? 'e.g. 5' : '—'}
                min="1"
                aria-label={`${label} count`}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}
