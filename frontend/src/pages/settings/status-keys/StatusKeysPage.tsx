import { useEffect, useState, useReducer, useCallback, useMemo, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import { useAuth } from '../../../store/authStore';
import { canManageSettingsAdmin } from '../../../lib/permissions';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import type {
  BranchAdmin,
  StatusKey,
  StatusKeyCreate,
} from '../../../types/settings';
import styles from './StatusKeysPage.module.css';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })
    ?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d) && d.length > 0)
    return d
      .map((i: unknown) =>
        i && typeof i === 'object' && 'msg' in i
          ? String((i as { msg: unknown }).msg)
          : null,
      )
      .filter(Boolean)
      .join(' ');
  return 'An unexpected error occurred.';
}

// ─── Form types ───────────────────────────────────────────────────────────────

interface KeyForm {
  key_name: string;
  hours_value: string;
  is_off_reason: boolean;
  deducts_from_yearly_allowance: boolean;
  allowance_category: string;
  // Usage limits
  limit_uses_per_period_enabled: boolean;
  limit_uses_per_period: string;
  limit_uses_per_driver_enabled: boolean;
  limit_uses_per_driver: string;
  limit_uses_across_drivers_enabled: boolean;
  limit_uses_across_drivers: string;
  limit_uses_per_day_enabled: boolean;
  limit_uses_per_day: string;
}

const EMPTY_KEY_FORM: KeyForm = {
  key_name:                          '',
  hours_value:                       '0',
  is_off_reason:                     true,
  deducts_from_yearly_allowance:     false,
  allowance_category:                '',
  limit_uses_per_period_enabled:     false,
  limit_uses_per_period:             '',
  limit_uses_per_driver_enabled:     false,
  limit_uses_per_driver:             '',
  limit_uses_across_drivers_enabled: false,
  limit_uses_across_drivers:         '',
  limit_uses_per_day_enabled:        false,
  limit_uses_per_day:                '',
};

function keyToForm(k: StatusKey): KeyForm {
  return {
    key_name:                          k.key_name,
    hours_value:                       String(k.hours_value),
    is_off_reason:                     k.is_off_reason,
    deducts_from_yearly_allowance:     k.deducts_from_yearly_allowance,
    allowance_category:                k.allowance_category ?? '',
    limit_uses_per_period_enabled:     k.limit_uses_per_period_enabled,
    limit_uses_per_period:             k.limit_uses_per_period != null ? String(k.limit_uses_per_period) : '',
    limit_uses_per_driver_enabled:     k.limit_uses_per_driver_enabled,
    limit_uses_per_driver:             k.limit_uses_per_driver != null ? String(k.limit_uses_per_driver) : '',
    limit_uses_across_drivers_enabled: k.limit_uses_across_drivers_enabled,
    limit_uses_across_drivers:         k.limit_uses_across_drivers != null ? String(k.limit_uses_across_drivers) : '',
    limit_uses_per_day_enabled:        k.limit_uses_per_day_enabled,
    limit_uses_per_day:                k.limit_uses_per_day != null ? String(k.limit_uses_per_day) : '',
  };
}

// ─── Reducer for async data loading ──────────────────────────────────────────
// Using useReducer avoids calling multiple setState functions synchronously
// inside effect bodies (react-hooks/set-state-in-effect).

type KeysLoadState = { keys: StatusKey[]; loading: boolean; error: string; selectedKeyId: number | null };
type KeysLoadAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    keys: StatusKey[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'SELECT';      id: number | null };

function keysLoadReducer(s: KeysLoadState, a: KeysLoadAction): KeysLoadState {
  switch (a.type) {
    case 'FETCH_START': return { keys: [],      loading: true,  error: '', selectedKeyId: null };
    case 'FETCH_OK':    return { keys: a.keys,  loading: false, error: '', selectedKeyId: s.selectedKeyId };
    case 'FETCH_ERROR': return { keys: [],      loading: false, error: a.error, selectedKeyId: null };
    case 'SELECT':      return { ...s, selectedKeyId: a.id };
  }
}

// ─── Main component ───────────────────────────────────────────────────────────

export function StatusKeysPage() {
  const { user } = useAuth();
  const canEdit = user !== null && canManageSettingsAdmin(user);

  // ── URL params (deep-link from Company & Branches "Missing" badge) ────────
  const [searchParams] = useSearchParams();

  // Read branchId param once at mount into a ref so it can be consumed by the
  // branch-load effect without becoming a reactive dependency (avoids re-runs).
  const urlBranchId = Number(searchParams.get('branchId'));
  const urlBranchIdRef = useRef<number | null>(urlBranchId > 0 ? urlBranchId : null);

  // ── Branches ──────────────────────────────────────────────────────────────
  const [branches, setBranches]             = useState<BranchAdmin[]>([]);
  const [branchesLoading, setBranchesLoading] = useState(true);
  const [selectedBranchId, setSelectedBranchId] = useState<number | null>(null);

  const [toast, setToast] = useState('');

  // ── Confirmation dialog ───────────────────────────────────────────────────
  type ConfirmKind = 'save-key' | 'deactivate-key' | 'reactivate-key';
  const [confirmKind, setConfirmKind] = useState<ConfirmKind | null>(null);
  const [confirming,  setConfirming]  = useState(false);

  // ── Status Keys (loaded data via reducer; modal state via useState) ────────
  const [keysState, dispatchKeys] = useReducer(keysLoadReducer, {
    keys: [], loading: false, error: '', selectedKeyId: null,
  });
  const statusKeys    = keysState.keys;
  const keysLoading   = keysState.loading;
  const keysError     = keysState.error;
  const selectedKeyId = keysState.selectedKeyId;

  const [keyStatusFilter, setKeyStatusFilter] = useState<'Active' | 'Inactive'>('Active');
  const [keyModal, setKeyModal]               = useState<'create' | 'edit' | null>(null);
  const [editingKey, setEditingKey]     = useState<StatusKey | null>(null);
  const [keyForm, setKeyForm]           = useState<KeyForm>(EMPTY_KEY_FORM);
  const [savingKey, setSavingKey]       = useState(false);
  const [keySaveErr, setKeySaveErr]     = useState('');
  const [deactivating, setDeactivating] = useState<number | null>(null);
  const [targetKey,    setTargetKey]    = useState<StatusKey | null>(null);

  // ── Toast ─────────────────────────────────────────────────────────────────
  const showToast = useCallback((msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(''), 4000);
  }, []);

  // ── Load branches ─────────────────────────────────────────────────────────
  useEffect(() => {
    apiClient.get<BranchAdmin[]>('/settings/branches')
      .then(({ data }) => {
        setBranches(data);
        setBranchesLoading(false);
        // If a branchId was supplied in the URL, select that branch;
        // fall back to the first branch if the param is missing or invalid.
        const urlId = urlBranchIdRef.current;
        const target = urlId ? (data.find(b => b.branch_id === urlId) ?? null) : null;
        setSelectedBranchId(target ? target.branch_id : (data[0]?.branch_id ?? null));
      })
      .catch(() => setBranchesLoading(false));
  }, []);

  // ── Load status keys ──────────────────────────────────────────────────────
  // Single dispatchKeys call (lint-safe).
  const includeInactiveKeys = keyStatusFilter === 'Inactive';

  useEffect(() => {
    if (!selectedBranchId) return;
    dispatchKeys({ type: 'FETCH_START' }); // also resets selectedKeyId via reducer
    apiClient.get<StatusKey[]>(`/settings/branches/${selectedBranchId}/status-keys`, {
      params: { include_inactive: includeInactiveKeys },
    })
      .then(({ data }) => dispatchKeys({ type: 'FETCH_OK', keys: data }))
      .catch((e) => dispatchKeys({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, [selectedBranchId, includeInactiveKeys]);

  // ── Unified confirm handler ───────────────────────────────────────────────
  async function handleConfirm() {
    if (!confirmKind) return;
    setConfirming(true);
    try {
      if (confirmKind === 'save-key') {
        await saveKey();
      } else if (confirmKind === 'deactivate-key' && targetKey) {
        await deactivateKey(targetKey);
      } else if (confirmKind === 'reactivate-key' && targetKey) {
        await reactivateKey(targetKey);
      }
    } finally {
      setConfirming(false);
      setConfirmKind(null);
    }
  }

  // ── Status key CRUD ───────────────────────────────────────────────────────
  function openCreateKey() {
    setKeyForm(EMPTY_KEY_FORM); setKeySaveErr('');
    setEditingKey(null); setKeyModal('create');
  }

  function openEditKey(k: StatusKey) {
    setKeyForm(keyToForm(k)); setKeySaveErr('');
    setEditingKey(k); setKeyModal('edit');
  }

  function closeKeyModal() {
    setKeyModal(null); setEditingKey(null); setKeySaveErr('');
  }

  /** Validate key form then open confirm dialog (actual save via handleConfirm). */
  function requestSaveKey() {
    if (!keyForm.key_name.trim()) { setKeySaveErr('Key Name is required.'); return; }
    const hrs = parseFloat(keyForm.hours_value);
    if (isNaN(hrs) || hrs < 0 || hrs > 24) {
      setKeySaveErr('Hours value must be between 0 and 24.'); return;
    }
    if (keyForm.deducts_from_yearly_allowance && !keyForm.is_off_reason) {
      setKeySaveErr('Deducts from allowance requires "Can be used as leave/off reason" to be enabled.'); return;
    }
    if (keyForm.deducts_from_yearly_allowance && !keyForm.allowance_category) {
      setKeySaveErr('Allowance category is required when deducting from yearly allowance.'); return;
    }
    // Validate usage limits
    if (keyForm.limit_uses_per_period_enabled) {
      const v = parseInt(keyForm.limit_uses_per_period);
      if (isNaN(v) || v <= 0) { setKeySaveErr('Limit per period must be a positive number when enabled.'); return; }
    }
    if (keyForm.limit_uses_per_driver_enabled) {
      const v = parseInt(keyForm.limit_uses_per_driver);
      if (isNaN(v) || v <= 0) { setKeySaveErr('Limit per driver must be a positive number when enabled.'); return; }
    }
    if (keyForm.limit_uses_across_drivers_enabled) {
      const v = parseInt(keyForm.limit_uses_across_drivers);
      if (isNaN(v) || v <= 0) { setKeySaveErr('Limit across all drivers must be a positive number when enabled.'); return; }
    }
    if (keyForm.limit_uses_per_day_enabled) {
      const v = parseInt(keyForm.limit_uses_per_day);
      if (isNaN(v) || v <= 0) { setKeySaveErr('Limit per day must be a positive number when enabled.'); return; }
    }
    setKeySaveErr('');
    setConfirmKind('save-key');
  }

  async function saveKey() {
    if (!selectedBranchId) return;
    const hrs = parseFloat(keyForm.hours_value);
    setSavingKey(true); setKeySaveErr('');
    try {
      // Helper to parse a usage limit value string
      const parseLimitVal = (s: string): number | null => {
        const v = parseInt(s);
        return isNaN(v) || v <= 0 ? null : v;
      };

      if (keyModal === 'create') {
        const payload: StatusKeyCreate = {
          key_name:                          keyForm.key_name.trim(),
          hours_value:                       hrs,
          is_off_reason:                     keyForm.is_off_reason,
          deducts_from_yearly_allowance:     keyForm.deducts_from_yearly_allowance,
          allowance_category:                keyForm.deducts_from_yearly_allowance ? (keyForm.allowance_category || null) : null,
          is_active:                         true,
          limit_uses_per_period_enabled:     keyForm.limit_uses_per_period_enabled,
          limit_uses_per_period:             keyForm.limit_uses_per_period_enabled ? parseLimitVal(keyForm.limit_uses_per_period) : null,
          limit_uses_per_driver_enabled:     keyForm.limit_uses_per_driver_enabled,
          limit_uses_per_driver:             keyForm.limit_uses_per_driver_enabled ? parseLimitVal(keyForm.limit_uses_per_driver) : null,
          limit_uses_across_drivers_enabled: keyForm.limit_uses_across_drivers_enabled,
          limit_uses_across_drivers:         keyForm.limit_uses_across_drivers_enabled ? parseLimitVal(keyForm.limit_uses_across_drivers) : null,
          limit_uses_per_day_enabled:        keyForm.limit_uses_per_day_enabled,
          limit_uses_per_day:                keyForm.limit_uses_per_day_enabled ? parseLimitVal(keyForm.limit_uses_per_day) : null,
        };
        const { data } = await apiClient.post<StatusKey>(
          `/settings/branches/${selectedBranchId}/status-keys`, payload);
        dispatchKeys({
          type: 'FETCH_OK',
          keys: [...statusKeys, data].sort((a, b) => a.key_name.localeCompare(b.key_name)),
        });
        showToast(`Status key "${data.key_name}" created.`);
      } else if (editingKey) {
        const patchPayload: Record<string, unknown> = {
          key_name:                          keyForm.key_name.trim(),
          hours_value:                       hrs,
          is_off_reason:                     keyForm.is_off_reason,
          deducts_from_yearly_allowance:     keyForm.deducts_from_yearly_allowance,
          limit_uses_per_period_enabled:     keyForm.limit_uses_per_period_enabled,
          limit_uses_per_period:             keyForm.limit_uses_per_period_enabled ? parseLimitVal(keyForm.limit_uses_per_period) : null,
          limit_uses_per_driver_enabled:     keyForm.limit_uses_per_driver_enabled,
          limit_uses_per_driver:             keyForm.limit_uses_per_driver_enabled ? parseLimitVal(keyForm.limit_uses_per_driver) : null,
          limit_uses_across_drivers_enabled: keyForm.limit_uses_across_drivers_enabled,
          limit_uses_across_drivers:         keyForm.limit_uses_across_drivers_enabled ? parseLimitVal(keyForm.limit_uses_across_drivers) : null,
          limit_uses_per_day_enabled:        keyForm.limit_uses_per_day_enabled,
          limit_uses_per_day:                keyForm.limit_uses_per_day_enabled ? parseLimitVal(keyForm.limit_uses_per_day) : null,
        };
        if (keyForm.deducts_from_yearly_allowance) {
          patchPayload.allowance_category = keyForm.allowance_category || null;
        }
        const { data } = await apiClient.patch<StatusKey>(
          `/settings/branches/${selectedBranchId}/status-keys/${editingKey.status_key_id}`,
          patchPayload,
        );
        dispatchKeys({
          type: 'FETCH_OK',
          keys: statusKeys
            .map(k => k.status_key_id === data.status_key_id ? data : k)
            .sort((a, b) => a.key_name.localeCompare(b.key_name)),
        });
        showToast(`Status key "${data.key_name}" updated.`);
      }
      closeKeyModal();
    } catch (e) { setKeySaveErr(apiError(e)); }
    finally { setSavingKey(false); }
  }

  function requestDeactivate(k: StatusKey) { setTargetKey(k); setConfirmKind('deactivate-key'); }
  function requestReactivate(k: StatusKey) { setTargetKey(k); setConfirmKind('reactivate-key'); }

  async function deactivateKey(k: StatusKey) {
    if (!selectedBranchId) return;
    setDeactivating(k.status_key_id);
    try {
      const { data } = await apiClient.delete<StatusKey>(
        `/settings/branches/${selectedBranchId}/status-keys/${k.status_key_id}`);
      dispatchKeys({
        type: 'FETCH_OK',
        keys: includeInactiveKeys
          ? statusKeys.map(sk => sk.status_key_id === data.status_key_id ? data : sk)
          : statusKeys.filter(sk => sk.status_key_id !== data.status_key_id),
      });
      showToast(`"${k.key_name}" deactivated.`);
    } catch (e) { showToast(apiError(e)); }
    finally { setDeactivating(null); }
  }

  async function reactivateKey(k: StatusKey) {
    if (!selectedBranchId) return;
    setDeactivating(k.status_key_id);
    try {
      const { data } = await apiClient.patch<StatusKey>(
        `/settings/branches/${selectedBranchId}/status-keys/${k.status_key_id}`,
        { is_active: true });
      dispatchKeys({
        type: 'FETCH_OK',
        keys: statusKeys.map(sk => sk.status_key_id === data.status_key_id ? data : sk),
      });
      showToast(`"${k.key_name}" reactivated.`);
    } catch (e) { showToast(apiError(e)); }
    finally { setDeactivating(null); }
  }

  // ── Derived state ─────────────────────────────────────────────────────────
  const selectedBranch = useMemo(
    () => branches.find(b => b.branch_id === selectedBranchId) ?? null,
    [branches, selectedBranchId],
  );

  const selectedKey = useMemo(
    () => statusKeys.find(k => k.status_key_id === selectedKeyId) ?? null,
    [statusKeys, selectedKeyId],
  );

  // ─────────────────────────────────────────────────────────────────────────
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
          {branchesLoading ? (
            <span className={styles.branchCtxMuted}>Loading…</span>
          ) : branches.length === 0 ? (
            <span className={styles.branchCtxMuted}>No branches available</span>
          ) : canEdit && branches.length > 1 ? (
            <select
              className={styles.branchSelect}
              value={selectedBranchId ?? ''}
              onChange={e => setSelectedBranchId(Number(e.target.value))}
            >
              {branches.map(b => (
                <option key={b.branch_id} value={b.branch_id}>
                  {b.branch_name} ({b.branch_code})
                </option>
              ))}
            </select>
          ) : (
            <span className={styles.branchCtxValue}>
              {selectedBranch?.branch_name ?? '—'}
              {selectedBranch?.branch_code && (
                <span className={styles.branchCodePill}>{selectedBranch.branch_code}</span>
              )}
            </span>
          )}
        </div>
      </div>

      {/* ══ STATUS KEYS ══ */}
      <div className={styles.statusKeysSection}>

        {/* Header row */}
        <div className={styles.sectionHeader}>
          <div className={styles.sectionLeft}>
            <h2 className={styles.sectionTitle}>Status Keys</h2>
            {!keysLoading && statusKeys.length > 0 && (
              <span className={styles.countChip}>{statusKeys.length}</span>
            )}
            {/* Segmented status filter replaces the old checkbox */}
            <div className={styles.segGroup}>
              {(['Active', 'Inactive'] as const).map(f => (
                <button
                  key={f}
                  className={`${styles.segBtn}${keyStatusFilter === f ? ` ${styles.segBtnActive}` : ''}`}
                  onClick={() => setKeyStatusFilter(f)}
                >
                  {f}
                </button>
              ))}
            </div>
          </div>
          {canEdit && (
            <button className={styles.btnPrimary} onClick={openCreateKey}>
              <PlusIcon /> Add Status Key
            </button>
          )}
        </div>

        {/* Two-column body */}
        <div className={styles.keysBody}>

          {/* LEFT — list */}
          <div className={styles.keysListCard}>
            {keysError ? (
              <div className={styles.errorAlert}><AlertIcon /> {keysError}</div>
            ) : keysLoading ? (
              <div className={styles.loadingCard}><SkeletonBlock /></div>
            ) : statusKeys.length === 0 ? (
              <div className={styles.emptyCard}>
                <KeyIcon />
                <span className={styles.emptyTitle}>
                  {keyStatusFilter === 'Inactive' ? 'No inactive status keys' : 'No status keys yet'}
                </span>
                <span className={styles.emptyDesc}>
                  {keyStatusFilter === 'Inactive'
                    ? 'There are no deactivated status keys for this branch.'
                    : 'Status keys define payroll entry types such as Vacation, Sick Day, or On Leave.'}
                </span>
                {canEdit && keyStatusFilter === 'Active' && (
                  <button
                    className={styles.btnPrimary}
                    onClick={openCreateKey}
                    style={{ marginTop: '1.25rem' }}
                  >
                    <PlusIcon /> Add First Status Key
                  </button>
                )}
              </div>
            ) : (
              <div className={styles.tableWrap}>
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th>Key Name</th>
                      <th className={styles.numTh}>Hours</th>
                      <th>Leave/Off</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {statusKeys.map(k => {
                      const isSelected = k.status_key_id === selectedKeyId;
                      return (
                        <tr
                          key={k.status_key_id}
                          className={`${styles.tableRow}${isSelected ? ` ${styles.tableRowSelected}` : ''}${!k.is_active ? ` ${styles.rowInactive}` : ''}`}
                          onClick={() => dispatchKeys({ type: 'SELECT', id: k.status_key_id })}
                        >
                          <td><span className={styles.statusCodeLabel}>{k.key_name}</span></td>
                          <td className={styles.numCell}>{k.hours_value}h</td>
                          <td>
                            {k.is_off_reason
                              ? <span className={`${styles.badge} ${styles.badgeBlue}`}>Yes</span>
                              : <span className={`${styles.badge} ${styles.badgeGray}`}>No</span>}
                          </td>
                          <td>
                            {k.is_active
                              ? <span className={`${styles.badge} ${styles.badgeGreen}`}>Active</span>
                              : <span className={`${styles.badge} ${styles.badgeRed}`}>Inactive</span>}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* RIGHT — detail panel */}
          <div className={styles.keysDetailPanel}>
            {!selectedKey ? (
              <div className={styles.keysDetailEmpty}>
                <KeyIcon />
                <p className={styles.keysDetailEmptyTitle}>Choose a status key to show details</p>
                <p className={styles.keysDetailEmptyText}>
                  Select a key from the list to view its configuration and settings.
                </p>
              </div>
            ) : (
              <StatusKeyDetail
                statusKey={selectedKey}
                canEdit={canEdit}
                isDeactivating={deactivating === selectedKey.status_key_id}
                onEdit={() => openEditKey(selectedKey)}
                onDeactivate={() => requestDeactivate(selectedKey)}
                onReactivate={() => requestReactivate(selectedKey)}
                styles={styles}
              />
            )}
          </div>
        </div>
      </div>

      {/* Confirm dialog */}
      <ConfirmDialog
        open={confirmKind !== null}
        title={
          confirmKind === 'save-key'         ? (keyModal === 'create' ? 'Create status key?' : 'Save status key changes?') :
          confirmKind === 'deactivate-key'   ? 'Deactivate status key?' :
          confirmKind === 'reactivate-key'   ? 'Reactivate status key?' :
          'Confirm'
        }
        message={
          confirmKind === 'save-key'
            ? keyModal === 'create'
              ? `Create status key "${keyForm.key_name || '…'}"?`
              : `Save changes to "${editingKey?.key_name}"?`
            : confirmKind === 'deactivate-key'
            ? `Deactivate "${targetKey?.key_name}"? It will no longer appear in payroll entry.`
            : confirmKind === 'reactivate-key'
            ? `Reactivate "${targetKey?.key_name}"? It will appear in payroll entry again.`
            : ''
        }
        confirmLabel={
          confirmKind === 'deactivate-key' ? 'Deactivate' :
          confirmKind === 'reactivate-key' ? 'Reactivate' :
          'Save'
        }
        variant={confirmKind === 'deactivate-key' ? 'danger' : 'primary'}
        loading={confirming || savingKey || deactivating !== null}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmKind(null)}
      />

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

    </div>
  );
}

// ─── Key form fields ──────────────────────────────────────────────────────────

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

// ─── Status Key Detail Panel ──────────────────────────────────────────────────

function StatusKeyDetail({
  statusKey: k,
  canEdit,
  isDeactivating,
  onEdit,
  onDeactivate,
  onReactivate,
  styles: s,
}: {
  statusKey: StatusKey;
  canEdit: boolean;
  isDeactivating: boolean;
  onEdit: () => void;
  onDeactivate: () => void;
  onReactivate: () => void;
  styles: Record<string, string>;
}) {
  const usageLimitRows: { label: string; enabled: boolean; value: number | null }[] = [
    { label: 'Per period',         enabled: k.limit_uses_per_period_enabled,    value: k.limit_uses_per_period },
    { label: 'Per driver',         enabled: k.limit_uses_per_driver_enabled,    value: k.limit_uses_per_driver },
    { label: 'Across all drivers', enabled: k.limit_uses_across_drivers_enabled, value: k.limit_uses_across_drivers },
    { label: 'Per day',            enabled: k.limit_uses_per_day_enabled,       value: k.limit_uses_per_day },
  ];
  const anyLimitEnabled = usageLimitRows.some(r => r.enabled);

  return (
    <div className={s.keyDetailContent}>

      {/* Header */}
      <div className={s.keyDetailHeader}>
        <div className={s.keyDetailTitleBlock}>
          <span className={s.keyDetailCodeLabel}>{k.key_name}</span>
          <span className={k.is_active
            ? `${s.badge} ${s.badgeGreen}`
            : `${s.badge} ${s.badgeRed}`}>
            {k.is_active ? 'Active' : 'Inactive'}
          </span>
        </div>
        {canEdit && (
          <button className={s.btnGhost} onClick={onEdit}>
            <EditIconInline /> Edit
          </button>
        )}
      </div>

      {/* Body */}
      <div className={s.keyDetailBody}>

        {/* Key Details */}
        <div className={s.keyDetailSection}>
          <p className={s.keyDetailSectionTitle}>Key Details</p>
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Hours Value</span>
            <span className={s.keyDetailValue}>{k.hours_value}h</span>
          </div>
        </div>

        {/* Leave Settings */}
        <div className={s.keyDetailSection}>
          <p className={s.keyDetailSectionTitle}>Leave / Off Settings</p>
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Leave / off reason</span>
            <span className={s.keyDetailValue}>{k.is_off_reason ? 'Yes' : 'No'}</span>
          </div>
          {k.is_off_reason && (
            <p className={s.keyDetailHint}>
              Can be used for leave/time-off tracking during a payroll period.
            </p>
          )}
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Deducts allowance</span>
            <span className={s.keyDetailValue} style={{ color: 'var(--color-text-muted, #888)', fontStyle: 'italic' }}>Future feature</span>
          </div>
        </div>

        {/* Usage Limits */}
        <div className={s.keyDetailSection}>
          <div className={s.keyDetailSectionHeader}>
            <p className={s.keyDetailSectionTitle}>Usage Limits</p>
            {!anyLimitEnabled && <span className={s.plannedBadge}>Not configured</span>}
          </div>
          {!anyLimitEnabled ? (
            <p className={s.plannedNote}>
              No usage limits set. Edit this key to add limits.
              <br />
              <em>Note: limits are stored but not yet enforced at payroll entry time.</em>
            </p>
          ) : (
            <>
              {usageLimitRows.filter(r => r.enabled).map(row => (
                <div key={row.label} className={s.keyDetailRow}>
                  <span className={s.keyDetailLabel}>{row.label}</span>
                  <span className={s.keyDetailValue}>{row.value ?? '—'}</span>
                </div>
              ))}
              <p className={s.plannedNote} style={{ marginTop: '0.4rem' }}>
                <em>Usage limits are stored but not yet enforced at payroll entry time.</em>
              </p>
            </>
          )}
        </div>

      </div>

      {/* Actions */}
      {canEdit && (
        <div className={s.keyDetailActions}>
          {k.is_active ? (
            <button
              className={s.btnRowDeactivate}
              onClick={onDeactivate}
              disabled={isDeactivating}
              style={{ width: 'auto', height: 'auto', padding: '0.38rem 0.85rem', fontSize: '0.82rem', gap: '0.35rem' }}
            >
              {isDeactivating ? <SpinnerIcon /> : <DeactivateIcon />}
              Deactivate
            </button>
          ) : (
            <button
              className={s.btnRowReactivate}
              onClick={onReactivate}
              disabled={isDeactivating}
              style={{ width: 'auto', height: 'auto', padding: '0.38rem 0.85rem', fontSize: '0.82rem', gap: '0.35rem' }}
            >
              {isDeactivating ? <SpinnerIcon /> : <ReactivateIcon />}
              Reactivate
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// An inline version of EditIcon used inside StatusKeyDetail (avoids re-export issue)
function EditIconInline() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
    </svg>
  );
}

// ─── Small read-only display components ──────────────────────────────────────

function SkeletonBlock() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.65rem', padding: '1.5rem' }}>
      {[55, 38, 72, 30].map((w, i) => (
        <div key={i} className={styles.skeleton} style={{ width: `${w}%` }} />
      ))}
    </div>
  );
}

// ─── Icons ────────────────────────────────────────────────────────────────────

function KeyIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="7.5" cy="15.5" r="5.5"/>
      <path d="M21 2l-9.6 9.6"/>
      <path d="M15.5 7.5l3 3L22 7l-3-3"/>
    </svg>
  );
}

function BranchIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
      <polyline points="9 22 9 12 15 12 15 22"/>
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
      <line x1="12" y1="5" x2="12" y2="19"/>
      <line x1="5"  y1="12" x2="19" y2="12"/>
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <circle cx="12" cy="12" r="10"/>
      <line x1="12" y1="8" x2="12" y2="12"/>
      <line x1="12" y1="16" x2="12.01" y2="16"/>
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
      <line x1="18" y1="6" x2="6"  y2="18"/>
      <line x1="6"  y1="6" x2="18" y2="18"/>
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" className={styles.spinner} aria-hidden="true">
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
    </svg>
  );
}

function DeactivateIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10"/>
      <line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>
    </svg>
  );
}

function ReactivateIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="23 4 23 10 17 10"/>
      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
    </svg>
  );
}
