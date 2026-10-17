import { useEffect, useMemo, useReducer, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import {
  deactivateBranchStatusKey,
  getBranchStatusKeys,
  updateBranchStatusKey,
} from '../../../lib/settingsApi';
import type { StatusKey } from '../../../types/settings';
import { apiError, keysLoadReducer, type ConfirmKind } from './statusKeysModel';

export function useStatusKeyCollection({
  selectedBranchId,
  showToast,
  setConfirmKind,
}: {
  selectedBranchId: number | null;
  showToast: (msg: string) => void;
  setConfirmKind: Dispatch<SetStateAction<ConfirmKind | null>>;
}) {
  const [keysState, dispatchKeys] = useReducer(keysLoadReducer, {
    keys: [], loading: false, error: '', selectedKeyId: null,
  });
  const statusKeys = keysState.keys;
  const keysLoading = keysState.loading;
  const keysError = keysState.error;
  const selectedKeyId = keysState.selectedKeyId;

  const [keyStatusFilter, setKeyStatusFilter] = useState<'Active' | 'Inactive'>('Active');
  const [deactivating, setDeactivating] = useState<number | null>(null);
  const [targetKey, setTargetKey] = useState<StatusKey | null>(null);

  // Single dispatchKeys call (lint-safe).
  const includeInactiveKeys = keyStatusFilter === 'Inactive';

  useEffect(() => {
    if (!selectedBranchId) return;
    dispatchKeys({ type: 'FETCH_START' }); // also resets selectedKeyId via reducer
    getBranchStatusKeys(selectedBranchId, includeInactiveKeys)
      .then(({ data }) => dispatchKeys({ type: 'FETCH_OK', keys: data }))
      .catch((e) => dispatchKeys({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, [selectedBranchId, includeInactiveKeys]);

  function requestDeactivate(k: StatusKey) { setTargetKey(k); setConfirmKind('deactivate-key'); }
  function requestReactivate(k: StatusKey) { setTargetKey(k); setConfirmKind('reactivate-key'); }

  async function deactivateKey(k: StatusKey) {
    if (!selectedBranchId) return;
    setDeactivating(k.status_key_id);
    try {
      const { data } = await deactivateBranchStatusKey(selectedBranchId, k.status_key_id);
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
      const { data } = await updateBranchStatusKey(selectedBranchId, k.status_key_id, { is_active: true });
      dispatchKeys({
        type: 'FETCH_OK',
        keys: statusKeys.map(sk => sk.status_key_id === data.status_key_id ? data : sk),
      });
      showToast(`"${k.key_name}" reactivated.`);
    } catch (e) { showToast(apiError(e)); }
    finally { setDeactivating(null); }
  }

  const selectedKey = useMemo(
    () => statusKeys.find(k => k.status_key_id === selectedKeyId) ?? null,
    [statusKeys, selectedKeyId],
  );

  return {
    dispatchKeys,
    statusKeys,
    keysLoading,
    keysError,
    selectedKeyId,
    selectedKey,
    keyStatusFilter,
    setKeyStatusFilter,
    deactivating,
    targetKey,
    requestDeactivate,
    requestReactivate,
    deactivateKey,
    reactivateKey,
  };
}
