import type { Dispatch, SetStateAction } from 'react';
import { STATUS_CATEGORIES, type StatusKey, type StatusRateColumn } from '../../../types/settings';
import { selectableColumns } from './statusColumnsModel';
import { STATUS_NAME_MAX_LENGTH, type KeyForm } from './statusKeysModel';
import { AlertIcon, CloseIcon, SpinnerIcon } from './StatusKeysDisplay';
import styles from './StatusKeysPage.module.css';

export function StatusKeyEditorDialog({
  keyModal, closeKeyModal, editingKey, keyForm, setKeyForm, columns, savingKey, keySaveErr, requestSaveKey,
}: {
  keyModal: 'create' | 'edit' | null;
  closeKeyModal: () => void;
  editingKey: StatusKey | null;
  keyForm: KeyForm;
  setKeyForm: Dispatch<SetStateAction<KeyForm>>;
  columns: readonly StatusRateColumn[];
  savingKey: boolean;
  keySaveErr: string;
  requestSaveKey: () => void;
}) {
  return (
    <>
      {keyModal && (
        <div
          className={styles.modalOverlay}
          onClick={e => { if (e.target === e.currentTarget) closeKeyModal(); }}
        >
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>
                {keyModal === 'create' ? 'Add Status' : `Edit — ${editingKey?.key_name}`}
              </h2>
              <button className={styles.modalCloseBtn} onClick={closeKeyModal}>
                <CloseIcon />
              </button>
            </div>
            <div className={styles.modalBody}>
              <KeyFormFields
                form={keyForm}
                onChange={p => setKeyForm(f => ({ ...f, ...p }))}
                columns={columns}
                locked={editingKey?.is_in_use ?? false}
                editing={keyModal === 'edit'}
                currentColumnId={editingKey?.status_rate_column_id ?? null}
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
                  : keyModal === 'create' ? 'Create Status' : 'Save Changes'}
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
  form, onChange, columns, locked, editing, currentColumnId, disabled, s,
}: {
  form: KeyForm;
  onChange: (p: Partial<KeyForm>) => void;
  columns: readonly StatusRateColumn[];
  /** The Status is in use: hours, the off-day setting, Category and rate column are fixed. */
  locked: boolean;
  editing: boolean;
  currentColumnId: number | null;
  disabled: boolean;
  s: Record<string, string>;
}) {
  const lockedDisabled = disabled || locked;
  return (
    <div className={s.formGrid}>
      {locked && (
        <div className={`${s.formGroupFull} ${s.lockNote}`}>
          This Status is in use by payroll entries or frozen payroll, so its hours, full-day-off
          setting, Category and rate column can no longer change. Name and aliases can.
        </div>
      )}

      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>
          Name <span className={s.required}>*</span>
        </label>
        <p className={s.fieldHint}>
          The name shown everywhere (e.g. &quot;Vacation&quot;, &quot;Sick Day&quot;). It is unique in the Company.
        </p>
        <input
          className={s.input}
          value={form.key_name}
          onChange={e => onChange({ key_name: e.target.value })}
          disabled={disabled}
          maxLength={STATUS_NAME_MAX_LENGTH}
          autoFocus
        />
      </div>

      <div className={s.formGroup}>
        <label className={s.label}>Category</label>
        <select
          className={s.select}
          value={form.category}
          onChange={e => onChange({ category: e.target.value as KeyForm['category'] })}
          disabled={lockedDisabled}
        >
          {STATUS_CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>

      <div className={s.formGroup}>
        <label className={s.label}>Hours</label>
        <input
          type="number"
          className={s.input}
          value={form.hours_value}
          onChange={e => onChange({ hours_value: e.target.value })}
          disabled={lockedDisabled}
          min="0" max="24" step="0.25"
          aria-label="Hours"
        />
      </div>

      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.checkboxLabel}>
          <input
            type="checkbox"
            checked={form.is_off_reason}
            onChange={e => onChange({ is_off_reason: e.target.checked })}
            disabled={lockedDisabled}
          />
          <span>
            <strong>Counts as a full day off</strong>
            <span className={s.checkboxDesc}> — a day with this Status cannot also have ordinary work quantities</span>
          </span>
        </label>
      </div>

      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Status rate column</label>
        <p className={s.fieldHint}>
          A paid Status earns its hours times the driver&apos;s rate for this column. Leave empty for a Status that is not paid.
        </p>
        <select
          className={s.select}
          value={form.status_rate_column_id}
          onChange={e => onChange({ status_rate_column_id: e.target.value })}
          disabled={lockedDisabled}
          aria-label="Status rate column"
        >
          <option value="">Not paid</option>
          {selectableColumns(columns, currentColumnId).map(c => (
            <option key={c.status_rate_column_id} value={String(c.status_rate_column_id)}>
              {c.column_name}{c.is_default ? ' (default)' : ''}{c.is_active ? '' : ' (inactive)'}
            </option>
          ))}
        </select>
      </div>

      <AliasEditor
        aliases={form.aliases}
        onChange={aliases => onChange({ aliases })}
        disabled={disabled}
        s={s}
      />

      <div className={s.formGroup}>
        <label className={s.label}>Display order</label>
        <input
          type="number"
          className={s.input}
          value={form.display_order}
          onChange={e => onChange({ display_order: e.target.value })}
          disabled={disabled}
          step="1"
          aria-label="Display order"
        />
      </div>

      {editing && (
        <div className={s.formGroup}>
          <label className={s.checkboxLabel} style={{ marginTop: '1.6rem' }}>
            <input
              type="checkbox"
              checked={form.is_active}
              onChange={e => onChange({ is_active: e.target.checked })}
              disabled={disabled}
            />
            <span><strong>Active</strong></span>
          </label>
        </div>
      )}
    </div>
  );
}

/** The alias list: one input per alias, add and remove. Aliases are shortcuts typed in payroll entry. */
function AliasEditor({
  aliases, onChange, disabled, s,
}: {
  aliases: string[];
  onChange: (aliases: string[]) => void;
  disabled: boolean;
  s: Record<string, string>;
}) {
  return (
    <div className={`${s.formGroup} ${s.formGroupFull}`}>
      <label className={s.label}>Aliases</label>
      <p className={s.fieldHint}>
        Shortcuts typed in payroll entry (e.g. &quot;V&quot; or &quot;Vac&quot;). An alias selects this Status; the
        name is always what is stored and shown. An alias cannot match another Status&apos;s name or alias.
      </p>
      <div className={s.aliasList}>
        {aliases.map((alias, index) => (
          <div key={index} className={s.aliasRow}>
            <input
              className={s.input}
              value={alias}
              onChange={e => onChange(aliases.map((a, i) => i === index ? e.target.value : a))}
              disabled={disabled}
              maxLength={STATUS_NAME_MAX_LENGTH}
              aria-label={`Alias ${index + 1}`}
            />
            <button
              type="button"
              className={s.btnGhost}
              onClick={() => onChange(aliases.filter((_, i) => i !== index))}
              disabled={disabled}
              aria-label={`Remove alias ${index + 1}`}
            >
              Remove
            </button>
          </div>
        ))}
        <div>
          <button
            type="button"
            className={s.btnSecondary}
            onClick={() => onChange([...aliases, ''])}
            disabled={disabled}
          >
            Add alias
          </button>
        </div>
      </div>
    </div>
  );
}
