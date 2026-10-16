import { useCallback } from 'react';
import type { Dispatch, FormEvent } from 'react';
import apiClient from '../../lib/apiClient';
import {
  createAdminUser,
  getAdminUser,
  linkAdminUserEmployee,
  provisionAdminUser,
  updateAdminUserPermissionOverrides,
} from '../../lib/adminApi';
import { apiError } from './peoplePresentation';
import type { Action, PageState } from './peoplePageState';
import type { CompanyRoleAssignmentCreate, UserCreate, UserPermissionOverridesUpdate, UserProvision } from '../../types/admin';
import type { PersonSummary } from '../../types/core';

interface UsePeopleProvisioningOptions {
  st: PageState;
  dispatch: Dispatch<Action>;
  userCanProvisionAccount: boolean;
  userCanEditPeople: boolean;
  showToast(msg: string): void;
}

export function usePeopleProvisioning({ st, dispatch, userCanProvisionAccount, userCanEditPeople, showToast }: UsePeopleProvisioningOptions) {
  // Preserve source-page callback dependencies; dispatch remains the same stable useReducer dispatch.

  const wizStep1 = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    const { wiz } = st;
    if (!wiz.name.trim()) { dispatch({ type: 'WIZ_ERROR', err: 'Display name is required.' }); return; }
    if (!wiz.username.trim()) { dispatch({ type: 'WIZ_ERROR', err: 'Username is required.' }); return; }
    if (wiz.password.length < 8) { dispatch({ type: 'WIZ_ERROR', err: 'Password must be at least 8 characters.' }); return; }
    dispatch({ type: 'WIZ_LOADING', val: true });
    try {
      const payload: UserCreate = { display_name: wiz.name.trim(), username: wiz.username.trim().toLowerCase(), password: wiz.password, email: wiz.email.trim() || null, phone: wiz.phone.trim() || null, is_staged: true, can_login: false, must_change_password: wiz.mustChange, is_active: true };
      const r = await createAdminUser(payload);
      dispatch({ type: 'ADD_PERSON', person: r.data });
      dispatch({ type: 'WIZ_USER_CREATED', user: r.data });
      if (!userCanProvisionAccount) {
        dispatch({ type: 'WIZ_STEP', step: 5 });
      }
    } catch (e) { dispatch({ type: 'WIZ_ERROR', err: apiError(e) }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st, userCanProvisionAccount]);

  const wizSelectRole = useCallback(async (roleId: number) => {
    dispatch({ type: 'WIZ_FIELD', field: 'roleId', val: roleId });
    const selectedRole = st.roles.find(role => role.company_role_id === roleId);
    if (selectedRole?.role_code !== 'DRIVER') {
      dispatch({ type: 'WIZ_FIELD', field: 'employeeId', val: null });
      dispatch({ type: 'WIZ_EMPLOYEES', employees: [] });
      dispatch({ type: 'WIZ_FIELD', field: 'scope', val: 'AllCompanyBranches' });
      dispatch({ type: 'WIZ_FIELD', field: 'branchId', val: null });
      return;
    }
    try {
      const result = await apiClient.get<PersonSummary[]>('/core/people?driver_state=current');
      dispatch({ type: 'WIZ_EMPLOYEES', employees: result.data });
    } catch (e) {
      dispatch({ type: 'WIZ_ERROR', err: `Could not load current Driver Employees: ${apiError(e)}` });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st.roles]);

  const wizStep3 = useCallback(async () => {
    const { wiz } = st;
    if (!wiz.roleId) { dispatch({ type: 'WIZ_ERROR', err: 'Please select a role.' }); return; }
    if (!wiz.createdUser) return;
    const selectedRole = st.roles.find(role => role.company_role_id === wiz.roleId);
    if (selectedRole?.role_code === 'DRIVER' && !wiz.employeeId) { dispatch({ type: 'WIZ_ERROR', err: 'Select an existing current Driver Employee.' }); return; }
    if (wiz.scope === 'SpecificBranch' && !wiz.branchId) { dispatch({ type: 'WIZ_ERROR', err: 'Please select a branch.' }); return; }
    dispatch({ type: 'WIZ_LOADING', val: true });
    try {
      const body: CompanyRoleAssignmentCreate = { company_role_id: wiz.roleId, scope_type: wiz.scope, branch_id: wiz.scope === 'SpecificBranch' ? wiz.branchId : null };
      if (selectedRole?.role_code === 'DRIVER') {
        await linkAdminUserEmployee(wiz.createdUser.user_id, wiz.employeeId);
      }
      const provision: UserProvision = { role_assignment: body, can_login: wiz.canLogin };
      const provisioned = await provisionAdminUser(wiz.createdUser.user_id, provision);
      dispatch({ type: 'UPDATE_PERSON', person: provisioned.data });
      dispatch({ type: 'WIZ_USER_CREATED', user: provisioned.data });
      dispatch({ type: 'WIZ_LOADING', val: false });
      dispatch({ type: 'WIZ_STEP', step: userCanEditPeople ? 4 : 5 });
    } catch (e) {
      // The staged account remains available for a later provisioning attempt.
      dispatch({ type: 'WIZ_PARTIAL', user: wiz.createdUser });
      dispatch({ type: 'WIZ_ERROR', err: `Access account remains staged and unprovisioned: ${apiError(e)}` });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st, userCanEditPeople]);

  const wizStep4 = useCallback(async () => {
    const { wiz } = st;
    if (!wiz.createdUser) return;
    dispatch({ type: 'WIZ_LOADING', val: true });
    try {
      // Fix 6: Filter out codes already in role to avoid sending redundant overrides
      const rolePermsSet = new Set(wiz.createdUser.role_permission_codes);
      const extraOnly = Array.from(wiz.extraCodes).filter(c => !rolePermsSet.has(c));
      if (extraOnly.length > 0) {
        const body: UserPermissionOverridesUpdate = { permission_codes: extraOnly };
        await updateAdminUserPermissionOverrides(wiz.createdUser.user_id, body);
      }
      const updated = await getAdminUser(wiz.createdUser.user_id);
      dispatch({ type: 'UPDATE_PERSON', person: updated.data });
      dispatch({ type: 'WIZ_USER_CREATED', user: updated.data }); // update createdUser with fresh data
      dispatch({ type: 'WIZ_LOADING', val: false });
      dispatch({ type: 'WIZ_STEP', step: 5 });
    } catch (e) { dispatch({ type: 'WIZ_ERROR', err: apiError(e) }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st]);

  const wizFinish = useCallback(() => {
    const { wiz } = st;
    if (!wiz.createdUser) { dispatch({ type: 'WIZ_CLOSE' }); return; }
    const roleName = st.roles.find(r => r.company_role_id === wiz.roleId)?.role_name ?? '';
    const isDriver = st.roles.find(r => r.company_role_id === wiz.roleId)?.role_code === 'DRIVER';
    dispatch({ type: 'WIZ_DONE', user: wiz.createdUser });
    showToast(isDriver ? `${wiz.createdUser.display_name} Driver access provisioned.` : `${wiz.createdUser.display_name} added${roleName ? ` as ${roleName}` : ''}.`);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- Preserve the existing page callback dependencies.
  }, [st]);

  return { wizStep1, wizSelectRole, wizStep3, wizStep4, wizFinish };
}
