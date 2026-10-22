import { useEffect, useMemo, useReducer, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { deactivateStatusKey, getStatusKeys, updateStatusKey } from '../../../lib/settingsApi';
import type { StatusKey } from '../../../types/settings';
import {
  apiError,
  keysLoadReducer,
  sortStatusKeys,
  visibleStatusKeys,
  type ConfirmKind,
  type KeyFilter,
} from './statusKeysModel';

/** The Company Status catalog (one list for every Branch), whole, inactive Statuses included. */
export function useStatusKeyCollection({
  showToast,
  setConfirmKind,
}: {
  showToast: (msg: string) => void;
  setConfirmKind: Dispatch<SetStateAction<ConfirmKind | null>>;
}) {
  const [keysState, dispatchKeys] = useReducer(keysLoadReducer, {
    keys: [], loading: true, error: '', selectedKeyId: null,
  });
  const statusKeys = keysState.keys;
  const keysLoading = keysState.loading;
  const keysError = keysState.error;
  const selectedKeyId = keysState.selectedKeyId;

  const [keyStatusFilter, setKeyStatusFilter] = useState<KeyFilter>('Active');
  const [deactivating, setDeactivating] = useState<number | null>(null);
  const [targetKey, setTargetKey] = useState<StatusKey | null>(null);

  useEffect(() => {
    getStatusKeys(true)
      .then(({ data }) => dispatchKeys({ type: 'FETCH_OK', keys: sortStatusKeys(data) }))
      .catch((e) => dispatchKeys({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, []);

  function requestDeactivate(k: StatusKey) { setTargetKey(k); setConfirmKind('deactivate-key'); }
  function requestReactivate(k: StatusKey) { setTargetKey(k); setConfirmKind('reactivate-key'); }

  function replaceKey(data: StatusKey) {
    dispatchKeys({
      type: 'FETCH_OK',
      keys: sortStatusKeys(statusKeys.map(sk => sk.status_key_id === data.status_key_id ? data : sk)),
    });
  }

  async function deactivateKey(k: StatusKey) {
    setDeactivating(k.status_key_id);
    try {
      const { data } = await deactivateStatusKey(k.status_key_id);
      replaceKey(data);
      showToast(`"${k.key_name}" deactivated.`);
    } catch (e) { showToast(apiError(e)); }
    finally { setDeactivating(null); }
  }

  async function reactivateKey(k: StatusKey) {
    setDeactivating(k.status_key_id);
    try {
      const { data } = await updateStatusKey(k.status_key_id, { is_active: true });
      replaceKey(data);
      showToast(`"${k.key_name}" reactivated.`);
    } catch (e) { showToast(apiError(e)); }
    finally { setDeactivating(null); }
  }

  const visibleKeys = useMemo(
    () => visibleStatusKeys(statusKeys, keyStatusFilter),
    [statusKeys, keyStatusFilter],
  );
  const selectedKey = useMemo(
    () => statusKeys.find(k => k.status_key_id === selectedKeyId) ?? null,
    [statusKeys, selectedKeyId],
  );

  return {
    dispatchKeys,
    statusKeys,
    visibleKeys,
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
