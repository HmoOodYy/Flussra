import { useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { createStatusKey, updateStatusKey } from '../../../lib/settingsApi';
import type { StatusKey } from '../../../types/settings';
import {
  apiError,
  buildCreatePayload,
  buildUpdatePayload,
  EMPTY_KEY_FORM,
  keyToForm,
  sortStatusKeys,
  validateKeyForm,
  type ConfirmKind,
  type KeyForm,
  type KeysLoadAction,
} from './statusKeysModel';

export function useStatusKeyEditor({
  statusKeys,
  dispatchKeys,
  showToast,
  setConfirmKind,
}: {
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

  /** Validate the form, then open the confirm dialog (the save itself runs from the dialog). */
  function requestSaveKey() {
    const problem = validateKeyForm(keyForm);
    if (problem) { setKeySaveErr(problem); return; }
    setKeySaveErr('');
    setConfirmKind('save-key');
  }

  async function saveKey() {
    setSavingKey(true); setKeySaveErr('');
    try {
      if (keyModal === 'create') {
        const { data } = await createStatusKey(buildCreatePayload(keyForm));
        dispatchKeys({ type: 'FETCH_OK', keys: sortStatusKeys([...statusKeys, data]) });
        dispatchKeys({ type: 'SELECT', id: data.status_key_id });
        showToast(`Status "${data.key_name}" created.`);
      } else if (editingKey) {
        const payload = buildUpdatePayload(keyForm, editingKey);
        if (Object.keys(payload).length === 0) {
          closeKeyModal();
          return;
        }
        const { data } = await updateStatusKey(editingKey.status_key_id, payload);
        dispatchKeys({
          type: 'FETCH_OK',
          keys: sortStatusKeys(statusKeys.map(k => k.status_key_id === data.status_key_id ? data : k)),
        });
        showToast(`Status "${data.key_name}" updated.`);
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
