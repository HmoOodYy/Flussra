import { useCallback, useEffect, useRef } from 'react';
import type { Dispatch, FormEvent } from 'react';
import {
  assignAdminUserCompanyRole,
  createAdminUser,
  getEmployeeLinkCandidates,
  linkAdminUserEmployee,
  provisionAdminUser,
  resetAdminUserPassword,
  transferCompanyOwner,
  unlinkAdminUserEmployee,
  updateAdminUser,
  updateAdminUserPermissionOverrides,
} from '../../../lib/adminApi';
import type {
  CompanyRoleAssignmentCreate,
  OwnerTransferRequest,
  UserAdmin,
  UserPasswordReset,
  UserPermissionOverridesUpdate,
  UserUpdate,
} from '../../../types/admin';
import { apiError } from '../peoplePresentation';
import { buildCreateAccountPayload, buildProvisionPayload, eligibleCandidates, unlinkBlockedReason } from './accessModel';
import type { AccessAction, AccessPageState } from './accessState';

interface Options {
  st: AccessPageState;
  selected: UserAdmin | null;
  dispatch: Dispatch<AccessAction>;
  showToast(msg: string): void;
  refreshUser(userId: number): Promise<UserAdmin | null>;
  logout(): void;
  canReadCandidates: boolean;
}

export function useAccessActions({ st, selected, dispatch, showToast, refreshUser, logout, canReadCandidates }: Options) {
  // ── Create account: one atomic request, never a multi-step persistence ──
  const submitCreate = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (st.create.loading) return;
    const payload = buildCreateAccountPayload(st.create.form, st.roles);
    if (typeof payload === 'string') { dispatch({ type: 'CREATE_ERROR', err: payload }); return; }
    dispatch({ type: 'CREATE_LOADING', val: true });
    try {
      const created = await createAdminUser(payload);
      dispatch({ type: 'ADD_USER', user: created.data });
      dispatch({ type: 'SELECT', id: created.data.user_id });
      dispatch({ type: 'CREATE_CLOSE' });
      showToast(created.data.is_staged
        ? `${created.data.display_name} created as a staged account.`
        : `${created.data.display_name} created and provisioned.`);
    } catch (err) {
      // Nothing was persisted: the dialog stays open with its values for a corrected retry.
      dispatch({ type: 'CREATE_ERROR', err: apiError(err) });
    }
  }, [st.create.form, st.create.loading, st.roles, dispatch, showToast]);

  // ── Provision a staged account: one atomic request ──
  const submitProvision = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected || st.provision.loading) return;
    const payload = buildProvisionPayload(selected, st.provision.form, st.roles);
    if (typeof payload === 'string') { dispatch({ type: 'PROVISION_ERROR', err: payload }); return; }
    dispatch({ type: 'PROVISION_LOADING', val: true });
    try {
      const provisioned = await provisionAdminUser(selected.user_id, payload);
      dispatch({ type: 'UPDATE_USER', user: provisioned.data });
      dispatch({ type: 'PROVISION_CLOSE' });
      showToast(`${provisioned.data.display_name} provisioned.`);
    } catch (err) {
      dispatch({ type: 'PROVISION_ERROR', err: apiError(err) });
    }
  }, [selected, st.provision.form, st.provision.loading, st.roles, dispatch, showToast]);

  // ── Employee link ──
  const loadCandidates = useCallback(async (
    target: 'create' | 'link', search: string, driverStateOnly: boolean,
  ) => {
    if (!canReadCandidates) {
      dispatch(target === 'create'
        ? { type: 'CREATE_CANDIDATES', candidates: [] }
        : { type: 'LINK_CANDIDATES', candidates: [] });
      return;
    }
    try {
      const result = await getEmployeeLinkCandidates({
        q: search.trim() || undefined, driver_state: driverStateOnly ? 'current' : undefined,
      });
      dispatch(target === 'create'
        ? { type: 'CREATE_CANDIDATES', candidates: result.data }
        : { type: 'LINK_CANDIDATES', candidates: result.data });
    } catch (err) {
      dispatch(target === 'create'
        ? { type: 'CREATE_ERROR', err: `Could not load Employees to link: ${apiError(err)}` }
        : { type: 'LINK_ERROR', err: `Could not load Employees to link: ${apiError(err)}` });
    }
  }, [canReadCandidates, dispatch]);

  // Candidates for the create dialog follow the chosen role (DRIVER needs a current Driver).
  const createRoleId = st.create.form.roleId;
  const createMode = st.create.form.mode;
  const createOpen = st.create.open;
  const roles = st.roles;
  useEffect(() => {
    if (!createOpen) return;
    const role = roles.find(r => r.company_role_id === createRoleId);
    void loadCandidates('create', '', createMode === 'provisioned' && role?.role_code === 'DRIVER');
  }, [createOpen, createRoleId, createMode, roles, loadCandidates]);

  // Search-as-you-type for the link dialog (debounced).
  const linkOpen = st.link.open;
  const linkSearch = st.link.search;
  const linkTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!linkOpen) return;
    if (linkTimer.current) clearTimeout(linkTimer.current);
    linkTimer.current = setTimeout(() => { void loadCandidates('link', linkSearch, false); }, 200);
    return () => { if (linkTimer.current) clearTimeout(linkTimer.current); };
  }, [linkOpen, linkSearch, loadCandidates]);

  const submitLink = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected || st.link.loading) return;
    if (st.link.employeeId === null) { dispatch({ type: 'LINK_ERROR', err: 'Select an Employee.' }); return; }
    dispatch({ type: 'LINK_LOADING', val: true });
    try {
      const linked = await linkAdminUserEmployee(selected.user_id, st.link.employeeId);
      dispatch({ type: 'UPDATE_USER', user: linked.data });
      dispatch({ type: 'LINK_CLOSE' });
      showToast(`${linked.data.display_name} linked to ${linked.data.employee_name ?? 'the Employee'}.`);
    } catch (err) {
      dispatch({ type: 'LINK_ERROR', err: apiError(err) });
    }
  }, [selected, st.link.employeeId, st.link.loading, dispatch, showToast]);

  const unlink = useCallback(async () => {
    if (!selected) return;
    const blocked = unlinkBlockedReason(selected);
    if (blocked) { showToast(blocked); return; }
    try {
      const result = await unlinkAdminUserEmployee(selected.user_id);
      dispatch({ type: 'UPDATE_USER', user: result.data });
      showToast(`${result.data.display_name} unlinked from the Employee.`);
    } catch (err) { showToast(apiError(err)); }
  }, [selected, dispatch, showToast]);

  // ── Account lifecycle (unchanged behaviour) ──
  const submitEdit = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    dispatch({ type: 'EDIT_LOADING', val: true });
    try {
      const payload: UserUpdate = {
        display_name: st.edit.name.trim() || null, email: st.edit.email.trim() || null,
        phone: st.edit.phone.trim() || null, can_login: st.edit.canLogin,
        must_change_password: st.edit.mustChange,
      };
      const r = await updateAdminUser(selected.user_id, payload);
      dispatch({ type: 'UPDATE_USER', user: r.data });
      dispatch({ type: 'EDIT_CLOSE' });
      showToast(`${r.data.display_name} updated.`);
    } catch (err) { dispatch({ type: 'EDIT_ERROR', err: apiError(err) }); }
  }, [st.edit, selected, dispatch, showToast]);

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
    } catch (err) { dispatch({ type: 'RESET_ERROR', err: apiError(err) }); }
  }, [st.reset, selected, dispatch, showToast]);

  const confirmToggleActive = useCallback(async () => {
    const { userId, toActive } = st.confirmToggle;
    if (!userId) return;
    dispatch({ type: 'CONFIRM_TOGGLE_CLOSE' });
    try {
      const r = await updateAdminUser(userId, { is_active: toActive });
      dispatch({ type: 'UPDATE_USER', user: r.data });
      showToast(`${r.data.display_name} ${toActive ? 'activated' : 'deactivated'}.`);
    } catch (err) { showToast(apiError(err)); }
  }, [st.confirmToggle, dispatch, showToast]);

  const submitChangeRole = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected || !st.changeRole.roleId) { dispatch({ type: 'CR_ERROR', err: 'Please select a role.' }); return; }
    if (st.changeRole.scope === 'SpecificBranch' && !st.changeRole.branchId) { dispatch({ type: 'CR_ERROR', err: 'Please select a branch.' }); return; }
    dispatch({ type: 'CR_LOADING', val: true });
    try {
      const body: CompanyRoleAssignmentCreate = {
        company_role_id: st.changeRole.roleId, scope_type: st.changeRole.scope,
        branch_id: st.changeRole.scope === 'SpecificBranch' ? st.changeRole.branchId : null,
      };
      await assignAdminUserCompanyRole(selected.user_id, body);
      await refreshUser(selected.user_id);
      dispatch({ type: 'CR_CLOSE' });
      showToast('Role updated.');
    } catch (err) { dispatch({ type: 'CR_ERROR', err: apiError(err) }); }
  }, [st.changeRole, selected, dispatch, showToast, refreshUser]);

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
    } catch (err) { dispatch({ type: 'EP_ERROR', err: apiError(err) }); }
  }, [st.extraPerms.codes, selected, dispatch, showToast, refreshUser]);

  const submitOwnerTransfer = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!st.transfer.targetId) return;
    if (st.transfer.confirmText.trim().toUpperCase() !== 'TRANSFER') { dispatch({ type: 'TRANSFER_ERROR', err: 'You must type TRANSFER to confirm.' }); return; }
    dispatch({ type: 'TRANSFER_LOADING', val: true });
    try {
      const body: OwnerTransferRequest = {
        target_user_id: st.transfer.targetId,
        replacement_company_role_id: st.transfer.replacementRoleId, confirmation: 'TRANSFER',
      };
      const r = await transferCompanyOwner(body);
      dispatch({ type: 'TRANSFER_DONE', result: r.data });
      // The caller is always the outgoing owner: sign out so they re-authenticate with reduced access.
      showToast('Ownership transferred. Please sign in again with your updated access.');
      setTimeout(() => logout(), 2500);
    } catch (err) { dispatch({ type: 'TRANSFER_ERROR', err: apiError(err) }); }
  }, [st.transfer, dispatch, showToast, logout]);

  return {
    submitCreate, submitProvision, submitLink, unlink, submitEdit, submitReset,
    confirmToggleActive, submitChangeRole, submitExtraPerms, submitOwnerTransfer,
    eligibleCandidates,
  };
}
