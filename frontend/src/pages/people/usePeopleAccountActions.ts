import { useCallback } from 'react';
import type { Dispatch, FormEvent } from 'react';
import {
  assignAdminUserCompanyRole,
  resetAdminUserPassword,
  transferCompanyOwner,
  updateAdminUser,
  updateAdminUserPermissionOverrides,
} from '../../lib/adminApi';
import { apiError } from './peoplePresentation';
import type { Action, PageState } from './peoplePageState';
import type { CompanyRoleAssignmentCreate, OwnerTransferRequest, UserAdmin, UserPasswordReset, UserPermissionOverridesUpdate, UserUpdate } from '../../types/admin';

interface UsePeopleAccountActionsOptions {
  st: PageState;
  selected: UserAdmin | null;
  dispatch: Dispatch<Action>;
  showToast(msg: string): void;
  refreshUser(userId: number): Promise<UserAdmin | null>;
  logout(): void;
}

export function usePeopleAccountActions({ st, selected, dispatch, showToast, refreshUser, logout }: UsePeopleAccountActionsOptions) {
  // Preserve source-page callback dependencies; dispatch remains the same stable useReducer dispatch.

  const submitEdit = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    dispatch({ type: 'EDIT_LOADING', val: true });
    try {
      const payload: UserUpdate = { display_name: st.edit.name.trim() || null, email: st.edit.email.trim() || null, phone: st.edit.phone.trim() || null, can_login: st.edit.canLogin, must_change_password: st.edit.mustChange };
      const r = await updateAdminUser(selected.user_id, payload);
      dispatch({ type: 'UPDATE_PERSON', person: r.data });
      dispatch({ type: 'EDIT_CLOSE' });
      showToast(`${r.data.display_name} updated.`);
    } catch (e) { dispatch({ type: 'EDIT_ERROR', err: apiError(e) }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st, selected]);

  const submitReset = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    if (st.reset.pw.length < 8) { dispatch({ type: 'RESET_ERROR', err: 'Password must be at least 8 characters.' }); return; }
    dispatch({ type: 'RESET_LOADING', val: true });
    try {
      const payload: UserPasswordReset = { new_password: st.reset.pw, must_change_password: st.reset.mustChange };
      await resetAdminUserPassword(selected.user_id, payload);
      dispatch({ type: 'RESET_CLOSE' });
      showToast('Password reset successfully.');
    } catch (e) { dispatch({ type: 'RESET_ERROR', err: apiError(e) }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st, selected]);

  const confirmToggleActive = useCallback(async () => {
    const { userId, toActive } = st.confirmToggle;
    if (!userId) return;
    dispatch({ type: 'CONFIRM_TOGGLE_CLOSE' });
    try {
      const r = await updateAdminUser(userId, { is_active: toActive });
      dispatch({ type: 'UPDATE_PERSON', person: r.data });
      showToast(`${r.data.display_name} ${toActive ? 'activated' : 'deactivated'}.`);
    } catch (e) { showToast(apiError(e)); }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st.confirmToggle]);

  const submitChangeRole = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected || !st.changeRole.roleId) { dispatch({ type: 'CR_ERROR', err: 'Please select a role.' }); return; }
    if (st.changeRole.scope === 'SpecificBranch' && !st.changeRole.branchId) { dispatch({ type: 'CR_ERROR', err: 'Please select a branch.' }); return; }
    dispatch({ type: 'CR_LOADING', val: true });
    try {
      const body: CompanyRoleAssignmentCreate = { company_role_id: st.changeRole.roleId, scope_type: st.changeRole.scope, branch_id: st.changeRole.scope === 'SpecificBranch' ? st.changeRole.branchId : null };
      await assignAdminUserCompanyRole(selected.user_id, body);
      await refreshUser(selected.user_id);
      dispatch({ type: 'CR_CLOSE' });
      showToast('Role updated.');
    } catch (e) { dispatch({ type: 'CR_ERROR', err: apiError(e) }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st, selected]);

  const submitExtraPerms = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    dispatch({ type: 'EP_LOADING', val: true });
    try {
      const body: UserPermissionOverridesUpdate = { permission_codes: Array.from(st.extraPerms.codes) };
      await updateAdminUserPermissionOverrides(selected.user_id, body);
      await refreshUser(selected.user_id);
      dispatch({ type: 'EP_CLOSE' });
      showToast('Extra permissions updated.');
    } catch (e) { dispatch({ type: 'EP_ERROR', err: apiError(e) }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st, selected]);

  const submitTransfer = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!st.transfer.targetId) return;
    if (st.transfer.confirmText.trim().toUpperCase() !== 'TRANSFER') { dispatch({ type: 'TRANSFER_ERROR', err: 'You must type TRANSFER to confirm.' }); return; }
    dispatch({ type: 'TRANSFER_LOADING', val: true });
    try {
      const body: OwnerTransferRequest = { target_user_id: st.transfer.targetId, replacement_company_role_id: st.transfer.replacementRoleId, confirmation: 'TRANSFER' };
      const r = await transferCompanyOwner(body);
      dispatch({ type: 'TRANSFER_DONE', result: r.data });
      // Fix 3: The caller is always the outgoing owner. Force logout so they
      // re-authenticate with their updated (reduced) access level.
      showToast('Ownership transferred. Please sign in again with your updated access.');
      setTimeout(() => logout(), 2500);
    } catch (e) { dispatch({ type: 'TRANSFER_ERROR', err: apiError(e) }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st.transfer, logout]);

  return { submitEdit, submitReset, confirmToggleActive, submitChangeRole, submitExtraPerms, submitTransfer };
}

