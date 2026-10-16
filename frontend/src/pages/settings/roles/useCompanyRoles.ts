import { useEffect, useReducer, useState, useCallback } from 'react';
import type { FormEvent } from 'react';
import { createCompanyRole, deleteCompanyRole, getAdminPermissions, getCompanyRoles, renameCompanyRole } from '../../../lib/adminApi';
import { apiError, rolesReducer } from './rolesPageState';
import type { CompanyRole, CompanyRoleCreate, Permission } from '../../../types/admin';

export function useCompanyRoles() {
  const [rolesSt, dispatchRoles] = useReducer(rolesReducer, { roles: [], loading: true, error: '' });
  const [allPerms, setAllPerms] = useState<Permission[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState('');
  const [createSaving, setCreateSaving] = useState(false);
  const [createError, setCreateError] = useState('');
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [deleteWorking, setDeleteWorking] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [successInfo, setSuccessInfo] = useState<{ title: string; sub: string } | null>(null);
  const [renameId, setRenameId] = useState<number | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
  const [renameSaving, setRenameSaving] = useState(false);
  const [renameError, setRenameError] = useState('');
  const [roleSearch, setRoleSearch] = useState('');
  const [roleFilter, setRoleFilter] = useState<'all' | 'default' | 'custom'>('all');

  const showToast = useCallback((title: string, sub = '') => {
    setSuccessInfo({ title, sub });
  }, []);

  useEffect(() => {
    dispatchRoles({ type: 'FETCH_START' });
    Promise.all([
      getCompanyRoles(),
      getAdminPermissions(),
    ])
      .then(([rolesRes, permsRes]) => {
        dispatchRoles({ type: 'FETCH_OK', roles: rolesRes.data });
        setAllPerms(permsRes.data);
      })
      .catch(e => dispatchRoles({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, []);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setCreateSaving(true);
    setCreateError('');
    const payload: CompanyRoleCreate = {
      role_name: createName.trim(),
      notes: null,
    };
    try {
      const { data } = await createCompanyRole(payload);
      dispatchRoles({ type: 'ROLE_ADDED', role: data });
      setCreateOpen(false);
      setCreateName('');
      showToast('Role Created!', `"${data.role_name}" has been successfully added.`);
      setSelectedId(data.company_role_id);
    } catch (e) {
      setCreateError(apiError(e));
    } finally {
      setCreateSaving(false);
    }
  }

  async function confirmDelete() {
    if (!deleteId) return;
    setDeleteWorking(true);
    setDeleteError('');
    try {
      await deleteCompanyRole(deleteId);
      const role = rolesSt.roles.find(r => r.company_role_id === deleteId);
      dispatchRoles({ type: 'ROLE_DELETED', id: deleteId });
      if (selectedId === deleteId) setSelectedId(null);
      setDeleteId(null);
      showToast('Role Deleted', `"${role?.role_name}" has been removed from the Roles list.`);
    } catch (e) {
      setDeleteError(apiError(e));
    } finally {
      setDeleteWorking(false);
    }
  }

  function startRename(role: CompanyRole) {
    setRenameId(role.company_role_id);
    setRenameDraft(role.role_name);
    setRenameError('');
  }

  function cancelRename() {
    setRenameId(null);
    setRenameDraft('');
    setRenameError('');
  }

  async function commitRename() {
    if (!renameId || !renameDraft.trim()) return;
    const role = rolesSt.roles.find(r => r.company_role_id === renameId);
    if (!role) { cancelRename(); return; }
    if (renameDraft.trim() === role.role_name) { cancelRename(); return; }
    setRenameSaving(true);
    setRenameError('');
    try {
      const { data } = await renameCompanyRole(renameId, {
        role_name: renameDraft.trim(),
      });
      dispatchRoles({ type: 'ROLE_RENAMED', id: data.company_role_id, name: data.role_name });
      cancelRename();
    } catch (e) {
      setRenameError(apiError(e));
    } finally {
      setRenameSaving(false);
    }
  }

  return {
    rolesSt,
    allPerms,
    selectedId,
    setSelectedId,
    createOpen,
    setCreateOpen,
    createName,
    setCreateName,
    createSaving,
    createError,
    setCreateError,
    deleteId,
    setDeleteId,
    deleteWorking,
    deleteError,
    setDeleteError,
    successInfo,
    setSuccessInfo,
    renameId,
    renameDraft,
    renameSaving,
    renameError,
    setRenameDraft,
    roleSearch,
    setRoleSearch,
    roleFilter,
    setRoleFilter,
    handleCreate,
    confirmDelete,
    startRename,
    cancelRename,
    commitRename,
  };
}
