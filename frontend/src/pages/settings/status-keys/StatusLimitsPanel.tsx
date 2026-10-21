import { useState } from 'react';
import type { BranchAdmin, StatusKeyBranchLimits } from '../../../types/settings';
import { AlertIcon, SkeletonBlock, SpinnerIcon } from './StatusKeysDisplay';
import {
  LIMIT_LABELS,
  LIMIT_SCOPES,
  limitsChanged,
  limitsToForm,
  type LimitScope,
  type LimitsForm,
} from './statusLimitsModel';
import { useBranchStatusLimits } from './useBranchStatusLimits';
import styles from './StatusKeysPage.module.css';

/**
 * Branch usage limits: pick a Branch, then cap how often each Status may be used there. A limit
 * is a hard limit of the Branch (per period, per driver, across all drivers, per day); a Status
 * with no limit enabled is unrestricted. The Status itself is shared by every Branch.
 */
export function StatusLimitsPanel({
  branches, branchesLoading, selectedBranchId, setSelectedBranchId, canEdit,
}: {
  branches: BranchAdmin[];
  branchesLoading: boolean;
  selectedBranchId: number | null;
  setSelectedBranchId: (branchId: number) => void;
  canEdit: boolean;
}) {
  const limits = useBranchStatusLimits(selectedBranchId);
  return (
    <section className={styles.panel} aria-labelledby="status-limits-title">
      <div className={styles.panelHeader}>
        <div>
          <h2 id="status-limits-title" className={styles.sectionTitle}>Branch usage limits</h2>
          <p className={styles.panelNote}>
            Optional hard caps on how often a Status can be used in one Branch. &quot;Per driver&quot; counts each
            Branch assignment of a driver separately.
          </p>
        </div>
        <div className={styles.branchCtxLeft}>
          <span className={styles.branchCtxLabel}>Branch</span>
          {branchesLoading ? (
            <span className={styles.branchCtxMuted}>Loading…</span>
          ) : branches.length === 0 ? (
            <span className={styles.branchCtxMuted}>No branches available</span>
          ) : (
            <select
              className={styles.branchSelect}
              value={selectedBranchId ?? ''}
              onChange={(e) => setSelectedBranchId(Number(e.target.value))}
              aria-label="Branch"
            >
              {branches.map((b) => (
                <option key={b.branch_id} value={b.branch_id}>{b.branch_name} ({b.branch_code})</option>
              ))}
            </select>
          )}
        </div>
      </div>

      {limits.error && <div className={styles.errorAlert}><AlertIcon /> {limits.error}</div>}
      {limits.loading ? (
        <div className={styles.loadingCard}><SkeletonBlock /></div>
      ) : selectedBranchId != null && !limits.error && limits.limits.length === 0 ? (
        <p className={styles.panelNote}>There are no active Statuses yet.</p>
      ) : selectedBranchId != null && limits.limits.length > 0 ? (
        <div className={styles.tableWrapFlat}>
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Status</th>
                {LIMIT_SCOPES.map((scope) => <th key={scope}>{LIMIT_LABELS[scope]}</th>)}
                {canEdit && <th />}
              </tr>
            </thead>
            <tbody>
              {limits.limits.map((row) => (
                <LimitsRow
                  key={`${selectedBranchId}:${row.status_key_id}`}
                  row={row}
                  canEdit={canEdit}
                  saving={limits.savingKeyId === row.status_key_id}
                  error={limits.saveError?.keyId === row.status_key_id ? limits.saveError.message : ''}
                  onChange={limits.clearSaveError}
                  onSave={(form) => limits.save(row.status_key_id, form)}
                />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function LimitsRow({
  row, canEdit, saving, error, onChange, onSave,
}: {
  row: StatusKeyBranchLimits;
  canEdit: boolean;
  saving: boolean;
  error: string;
  onChange: () => void;
  onSave: (form: LimitsForm) => Promise<StatusKeyBranchLimits | null>;
}) {
  const [form, setForm] = useState<LimitsForm>(() => limitsToForm(row));
  const dirty = limitsChanged(form, row);

  function patch(scope: LimitScope, change: Partial<LimitsForm[LimitScope]>) {
    onChange();
    setForm((prev) => ({ ...prev, [scope]: { ...prev[scope], ...change } }));
  }

  async function save() {
    const saved = await onSave(form);
    if (saved) setForm(limitsToForm(saved));
  }

  return (
    <>
      <tr>
        <td><span className={styles.statusCodeLabel}>{row.key_name}</span></td>
        {LIMIT_SCOPES.map((scope) => (
          <td key={scope}>
            <div className={styles.usageLimitRow}>
              <input
                type="checkbox"
                checked={form[scope].enabled}
                disabled={!canEdit || saving}
                onChange={(e) => patch(scope, { enabled: e.target.checked, value: e.target.checked ? form[scope].value : '' })}
                aria-label={`${row.key_name}: limit ${LIMIT_LABELS[scope].toLowerCase()}`}
              />
              <input
                type="number"
                className={styles.usageLimitInput}
                value={form[scope].value}
                disabled={!canEdit || saving || !form[scope].enabled}
                onChange={(e) => patch(scope, { value: e.target.value })}
                placeholder={form[scope].enabled ? 'e.g. 5' : '—'}
                min="1"
                step="1"
                aria-label={`${row.key_name}: ${LIMIT_LABELS[scope].toLowerCase()} count`}
              />
            </div>
          </td>
        ))}
        {canEdit && (
          <td className={styles.rowActions}>
            <button className={styles.btnPrimary} onClick={() => void save()} disabled={!dirty || saving}>
              {saving ? <SpinnerIcon /> : 'Save'}
            </button>
          </td>
        )}
      </tr>
      {error && (
        <tr>
          <td colSpan={LIMIT_SCOPES.length + 2}>
            <div className={styles.errorAlert}><AlertIcon /> {error}</div>
          </td>
        </tr>
      )}
    </>
  );
}
