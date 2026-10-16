import { useEffect, useReducer, useState } from 'react';
import { getCompanyRolePermissions, updateCompanyRolePermissions } from '../../../lib/adminApi';
import { apiError } from './rolesPageState';
import { permsReducer } from './rolePermissionModel';

export function useRolePermissions(selectedId: number | null) {
  const [permsSt, dispatchPerms] = useReducer(permsReducer, { codes: new Set<string>(), savedCodes: new Set<string>(), loading: false, error: '', dirty: false });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [saveOk, setSaveOk] = useState(false);

  useEffect(() => {
    if (selectedId === null) return;
    dispatchPerms({ type: 'FETCH_START' });
    getCompanyRolePermissions(selectedId)
      .then(({ data }) => dispatchPerms({ type: 'FETCH_OK', codes: data.permission_codes }))
      .catch(e => dispatchPerms({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, [selectedId]);

  async function savePermissions(isOwner: boolean) {
    if (!selectedId || !permsSt.dirty || isOwner) return;
    setSaving(true);
    setSaveError('');
    setSaveOk(false);
    try {
      await updateCompanyRolePermissions(selectedId, {
        permission_codes: Array.from(permsSt.codes),
      });
      dispatchPerms({ type: 'RESET_DIRTY' });
      setSaveOk(true);
      setTimeout(() => setSaveOk(false), 3000);
    } catch (e) {
      setSaveError(apiError(e));
    } finally {
      setSaving(false);
    }
  }

  function discardPermissions() {
    if (selectedId !== null) {
      dispatchPerms({ type: 'FETCH_START' });
      getCompanyRolePermissions(selectedId)
        .then(({ data }) => dispatchPerms({ type: 'FETCH_OK', codes: data.permission_codes }))
        .catch(e => dispatchPerms({ type: 'FETCH_ERROR', error: apiError(e) }));
    }
    setSaveError('');
  }

  return {
    permsSt,
    dispatchPerms,
    saving,
    saveError,
    setSaveError,
    saveOk,
    setSaveOk,
    savePermissions,
    discardPermissions,
  };
}
