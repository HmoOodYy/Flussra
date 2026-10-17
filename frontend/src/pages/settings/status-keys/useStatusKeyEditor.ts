import { useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { createBranchStatusKey, updateBranchStatusKey } from '../../../lib/settingsApi';
import type { StatusKey, StatusKeyCreate } from '../../../types/settings';
import { apiError, EMPTY_KEY_FORM, keyToForm, type ConfirmKind, type KeyForm, type KeysLoadAction } from './statusKeysModel';

export function useStatusKeyEditor({
  selectedBranchId,
  statusKeys,
  dispatchKeys,
  showToast,
  setConfirmKind,
}: {
  selectedBranchId: number | null;
  statusKeys: StatusKey[];
  dispatchKeys: Dispatch<KeysLoadAction>;
  showToast: (msg: string) => void;
  setConfirmKind: Dispatch<SetStateAction<ConfirmKind | null>>;
}) {
  const [keyModal, setKeyModal] = useState<'create' | 'edit' | null>(null);
  const [editingKey, setEditingKey] = useState<StatusKey | null>(null);
  const [keyForm, setKeyForm] = useState<KeyForm>(EMPTY_KEY_FORM);
  const [savingKey, setSavingKey] = useState(false);
  const [keySaveErr, setKeySaveErr] = useState('');

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
        const { data } = await createBranchStatusKey(selectedBranchId, payload);
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
        const { data } = await updateBranchStatusKey(selectedBranchId, editingKey.status_key_id, patchPayload);
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

  return {
    keyModal,
    editingKey,
    keyForm,
    setKeyForm,
    savingKey,
    keySaveErr,
    openCreateKey,
    openEditKey,
    closeKeyModal,
    requestSaveKey,
    saveKey,
  };
}
