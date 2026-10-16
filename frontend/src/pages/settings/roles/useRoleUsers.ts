import { useEffect, useReducer } from 'react';
import { getCompanyRoleUsers } from '../../../lib/adminApi';
import { apiError, usersModalReducer } from './rolesPageState';

export function useRoleUsers() {
  const [usersModal, dispatchUsers] = useReducer(usersModalReducer, { roleId: null, roleName: '', loading: false, users: [], error: '' });

  useEffect(() => {
    if (usersModal.roleId === null) return;
    dispatchUsers({ type: 'FETCH_START' });
    getCompanyRoleUsers(usersModal.roleId)
      .then(({ data }) => dispatchUsers({ type: 'FETCH_OK', users: data }))
      .catch(e => dispatchUsers({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, [usersModal.roleId]);

  return { usersModal, dispatchUsers };
}
