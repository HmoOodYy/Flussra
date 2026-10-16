import { useEffect, useReducer, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import apiClient from '../../lib/apiClient';
import { getAdminUser, getCompanyRoles, getAdminPermissions, getAdminUsersIncludingInactive } from '../../lib/adminApi';
import {
  canAssignPeopleRole,
  canCreatePeople,
  canEditPeople,
  canManageRoles,
  canProvisionPeopleAccount,
  canTogglePeopleActive,
  canViewTransfers,
} from '../../lib/permissions';
import { useAuth } from '../../store/authStore';
import type { CompanyRole } from '../../types/admin';
import type { Branch } from '../../types/core';
import { apiError } from './peoplePresentation';
import { INITIAL, reducer } from './peoplePageState';
import { usePeopleAccountActions } from './usePeopleAccountActions';
import { usePeopleProvisioning } from './usePeopleProvisioning';

async function fetchCompanyRoles(canList: boolean): Promise<CompanyRole[]> {
  if (!canList) return [];
  const r = await getCompanyRoles();
  return r.data;
}

export function usePeopleController() {
  const { user: authUser, logout } = useAuth();
  const navigate = useNavigate();
  const [st, dispatch] = useReducer(reducer, INITIAL);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load. /admin/company-roles requires roles.view/fallback (canManageRoles) —
  // a users.view-only person must still be able to load the rest of the page.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const wantRoles = !!authUser && canManageRoles(authUser);
        const [pR, bR, permR, roles] = await Promise.all([
          getAdminUsersIncludingInactive(),
          apiClient.get<Branch[]>('/core/branches'),
          getAdminPermissions(true),
          fetchCompanyRoles(wantRoles),
        ]);
        if (!cancelled) dispatch({ type: 'LOADED', people: pR.data, roles, branches: bR.data, allPerms: permR.data });
      } catch (e) {
        if (!cancelled) dispatch({ type: 'LOAD_ERR', error: apiError(e) });
      }
    }
    load();
    return () => { cancelled = true; };
  }, [authUser]);

  function showToast(msg: string) {
    dispatch({ type: 'TOAST', msg });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => dispatch({ type: 'TOAST', msg: null }), 3500);
  }

  async function refreshUser(userId: number) {
    try {
      const r = await getAdminUser(userId);
      dispatch({ type: 'UPDATE_PERSON', person: r.data });
      return r.data;
    } catch { return null; }
  }

  // Derived
  const filtered = st.people.filter(p => {
    const q = st.search.toLowerCase();
    if (q && !p.display_name.toLowerCase().includes(q) && !p.username.toLowerCase().includes(q) && !(p.email ?? '').toLowerCase().includes(q)) return false;
    if (st.filterStatus === 'active' && !p.is_active) return false;
    if (st.filterStatus === 'inactive' && p.is_active) return false;
    if (st.filterRoleId !== null && p.company_role_id !== st.filterRoleId) return false;
    return true;
  }).sort((a, b) => a.display_name.localeCompare(b.display_name));

  const selected = st.selectedId !== null ? st.people.find(p => p.user_id === st.selectedId) ?? null : null;
  const amOwner = !!(authUser && st.people.find(p => p.user_id === authUser.user_id)?.company_role_code === 'COMPANY_OWNER');
  const userCanCreate     = authUser ? canCreatePeople(authUser)       : false;
  const userCanEditPeople = authUser ? canEditPeople(authUser)         : false;
  const userCanToggle     = authUser ? canTogglePeopleActive(authUser) : false;
  const userCanAssignRole = authUser ? canAssignPeopleRole(authUser)   : false;
  const userCanManageRoles = authUser ? canManageRoles(authUser)       : false;
  const userCanChangeRole = userCanAssignRole && userCanManageRoles;
  const userCanProvisionAccount = authUser ? canProvisionPeopleAccount(authUser) : false;
  const userCanTransfers  = authUser ? canViewTransfers(authUser)      : false;
  const assignableRoles = st.roles.filter(r => r.role_code !== 'COMPANY_OWNER' && r.is_active);
  const ownerTransferRoles = st.roles.filter(r => r.is_active && r.role_code !== 'COMPANY_OWNER' && r.role_code !== 'DRIVER');

  const { wizStep1, wizSelectRole, wizStep3, wizStep4, wizFinish } = usePeopleProvisioning({
    st, dispatch, userCanProvisionAccount, userCanEditPeople, showToast,
  });
  const { submitEdit, submitReset, confirmToggleActive, submitChangeRole, submitExtraPerms, submitTransfer } = usePeopleAccountActions({
    st, selected, dispatch, showToast, refreshUser, logout,
  });

  return {
    st,
    dispatch,
    navigate,
    filtered,
    selected,
    amOwner,
    userCanCreate,
    userCanEditPeople,
    userCanToggle,
    userCanChangeRole,
    userCanTransfers,
    assignableRoles,
    ownerTransferRoles,
    wizStep1,
    wizSelectRole,
    wizStep3,
    wizStep4,
    wizFinish,
    submitEdit,
    submitReset,
    confirmToggleActive,
    submitChangeRole,
    submitExtraPerms,
    submitTransfer,
  };
}


