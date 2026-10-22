import { useEffect, useReducer, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import { getAdminPermissions, getAdminUser, getAdminUsersIncludingInactive, getCompanyRoles } from '../../../lib/adminApi';
import {
  canAssignPeopleRole,
  canCreatePeople,
  canEditPeople,
  canManageRoles,
  canProvisionPeopleAccount,
  canTogglePeopleActive,
  canViewWorkforce,
} from '../../../lib/permissions';
import { useAuth } from '../../../store/authStore';
import type { CompanyRole } from '../../../types/admin';
import type { Branch } from '../../../types/core';
import { apiError } from '../peoplePresentation';
import { filterUsers } from './accessModel';
import { ACCESS_INITIAL, accessReducer } from './accessState';
import { useAccessActions } from './useAccessActions';

async function fetchCompanyRoles(canList: boolean): Promise<CompanyRole[]> {
  if (!canList) return [];
  return (await getCompanyRoles()).data;
}

/** State and actions of the Access area: Users, with or without an Employee. */
export function useAccessController() {
  const { user: authUser, logout } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const [st, dispatch] = useReducer(accessReducer, ACCESS_INITIAL);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const deepLinkedUser = useRef(searchParams.get('user'));
  // Workforce sends "create an account for this Employee" here as ?newForEmployee=<id>.
  const deepLinkedEmployee = useRef(Number(searchParams.get('newForEmployee')) || null);

  // /admin/company-roles needs roles.view (or the admin fallback), so a users.view-only operator
  // must still be able to load the rest of the area.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const wantRoles = !!authUser && canManageRoles(authUser);
        const [users, branches, permissions, roles] = await Promise.all([
          getAdminUsersIncludingInactive(),
          apiClient.get<Branch[]>('/core/branches'),
          getAdminPermissions(true),
          fetchCompanyRoles(wantRoles),
        ]);
        if (cancelled) return;
        dispatch({ type: 'LOADED', users: users.data, roles, branches: branches.data, allPerms: permissions.data });
        const wanted = Number(deepLinkedUser.current);
        if (Number.isInteger(wanted) && users.data.some(u => u.user_id === wanted)) {
          dispatch({ type: 'FILTERS', filters: { status: 'all' } });
          dispatch({ type: 'SELECT', id: wanted });
        }
        const forEmployee = deepLinkedEmployee.current;
        if (forEmployee !== null && authUser && canCreatePeople(authUser)) {
          deepLinkedEmployee.current = null;
          dispatch({ type: 'CREATE_OPEN' });
          dispatch({ type: 'CREATE_PRESELECT', employeeId: forEmployee });
          const next = new URLSearchParams(window.location.search);
          next.delete('newForEmployee');
          setSearchParams(next, { replace: true });
        }
      } catch (err) {
        if (!cancelled) dispatch({ type: 'LOAD_ERR', error: apiError(err) });
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [authUser, setSearchParams]);

  function showToast(msg: string) {
    dispatch({ type: 'TOAST', msg });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => dispatch({ type: 'TOAST', msg: null }), 3500);
  }

  function select(id: number | null) {
    dispatch({ type: 'SELECT', id });
    const next = new URLSearchParams(searchParams);
    if (id === null) next.delete('user'); else next.set('user', String(id));
    setSearchParams(next, { replace: true });
  }

  async function refreshUser(userId: number) {
    try {
      const r = await getAdminUser(userId);
      dispatch({ type: 'UPDATE_USER', user: r.data });
      return r.data;
    } catch { return null; }
  }

  const filtered = filterUsers(st.users, st.filters);
  const selected = st.selectedId !== null ? st.users.find(u => u.user_id === st.selectedId) ?? null : null;
  const amOwner = !!(authUser && st.users.find(u => u.user_id === authUser.user_id)?.company_role_code === 'COMPANY_OWNER');
  const can = {
    create: authUser ? canCreatePeople(authUser) : false,
    edit: authUser ? canEditPeople(authUser) : false,
    toggle: authUser ? canTogglePeopleActive(authUser) : false,
    changeRole: authUser ? canAssignPeopleRole(authUser) && canManageRoles(authUser) : false,
    provision: authUser ? canProvisionPeopleAccount(authUser) : false,
    // Linking is an Access action: it needs account-edit authority, never Workforce authority.
    link: authUser ? canEditPeople(authUser) : false,
    // A contextual jump to Workforce is offered only to an operator who can open it.
    openWorkforce: authUser ? canViewWorkforce(authUser) : false,
  };
  const assignableRoles = st.roles.filter(r => r.role_code !== 'COMPANY_OWNER' && r.is_active);
  const ownerTransferRoles = st.roles.filter(r => r.is_active && r.role_code !== 'COMPANY_OWNER' && r.role_code !== 'DRIVER');

  const actions = useAccessActions({
    st, selected, dispatch, showToast, refreshUser, logout,
    canReadCandidates: can.create || can.edit,
  });

  return { st, dispatch, select, filtered, selected, amOwner, can, assignableRoles, ownerTransferRoles, ...actions };
}
