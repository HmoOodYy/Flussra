import { useAuth } from '../../../store/authStore';
import { canManageRoles, canCreateRoles, canEditRoles, canDeleteRoles } from '../../../lib/permissions';
import styles from './RolesPage.module.css';
import { LockIcon } from './RoleIcons';
import { useCompanyRoles } from './useCompanyRoles';
import { useRolePermissions } from './useRolePermissions';
import { useRoleUsers } from './useRoleUsers';
import { RoleList } from './RoleList';
import { RolePermissionEditor } from './RolePermissionEditor';
import { RoleSuccessDialog, RoleCreateDialog, RoleDeleteDialog } from './RoleManagementDialogs';
import { RoleUsersDialog } from './RoleUsersDialog';

export function RolesPage() {
  const { user } = useAuth();

  const isAdmin     = !!user && canManageRoles(user);
  const canCreate   = !!user && canCreateRoles(user);
  const canEdit     = !!user && canEditRoles(user);
  const canDelete   = !!user && canDeleteRoles(user);
  const readOnly    = isAdmin && !canEdit;

  const roles = useCompanyRoles();
  const permission = useRolePermissions(roles.selectedId);
  const users = useRoleUsers();

  if (!isAdmin) {
    return (
      <div className={styles.page}>
        <div style={{ padding: '3rem 2rem', textAlign: 'center', color: '#64748b' }}>
          <LockIcon size={40} />
          <p style={{ marginTop: '1rem', fontSize: '0.95rem', fontWeight: 600 }}>Access Restricted</p>
          <p style={{ fontSize: '0.84rem' }}>You need company-level scope and at least <code>roles.view</code> to view roles.</p>
        </div>
      </div>
    );
  }

  const defaultRoles = roles.rolesSt.roles.filter(r => r.is_default || r.is_protected);
  const customRoles  = roles.rolesSt.roles.filter(r => !r.is_default && !r.is_protected);
  const searchLower  = roles.roleSearch.toLowerCase().trim();
  const matchSearch  = (name: string) => !searchLower || name.toLowerCase().includes(searchLower);
  const filteredDefaultRoles = roles.roleFilter !== 'custom' ? defaultRoles.filter(r => matchSearch(r.role_name)) : [];
  const filteredCustomRoles  = roles.roleFilter !== 'default' ? customRoles.filter(r => matchSearch(r.role_name))  : [];
  const hasSearchResults = filteredDefaultRoles.length > 0 || filteredCustomRoles.length > 0;
  const selectedRole = roles.rolesSt.roles.find(r => r.company_role_id === roles.selectedId) ?? null;
  // Someone else must change the permissions of a role you hold; the Company Owner's role is never editable anyway.
  const holdsSelectedRole = !!user && !!selectedRole && selectedRole.role_code !== 'COMPANY_OWNER'
    && user.branches.some(b => b.role_code === selectedRole.role_code);

  return (
    <div className={styles.page}>
      {roles.successInfo && (
        <RoleSuccessDialog successInfo={roles.successInfo} setSuccessInfo={roles.setSuccessInfo} />
      )}
      <div className={styles.body}>
        <RoleList
          rolesSt={roles.rolesSt}
          selectedId={roles.selectedId}
          roleSearch={roles.roleSearch}
          setRoleSearch={roles.setRoleSearch}
          roleFilter={roles.roleFilter}
          setRoleFilter={roles.setRoleFilter}
          defaultRoles={defaultRoles}
          customRoles={customRoles}
          filteredDefaultRoles={filteredDefaultRoles}
          filteredCustomRoles={filteredCustomRoles}
          searchLower={searchLower}
          hasSearchResults={hasSearchResults}
          cancelRename={roles.cancelRename}
          setSelectedId={roles.setSelectedId}
          setSaveError={permission.setSaveError}
          setSaveOk={permission.setSaveOk}
          dispatchUsers={users.dispatchUsers}
          canDelete={canDelete}
          setDeleteId={roles.setDeleteId}
          setDeleteError={roles.setDeleteError}
          canEdit={canEdit}
          startRename={roles.startRename}
          renameId={roles.renameId}
          renameDraft={roles.renameDraft}
          renameSaving={roles.renameSaving}
          renameError={roles.renameError}
          setRenameDraft={roles.setRenameDraft}
          commitRename={roles.commitRename}
          canCreate={canCreate}
          setCreateOpen={roles.setCreateOpen}
          setCreateError={roles.setCreateError}
        />
        <RolePermissionEditor
          selectedRole={selectedRole}
          allPerms={roles.allPerms}
          permsSt={permission.permsSt}
          dispatchPerms={permission.dispatchPerms}
          dispatchUsers={users.dispatchUsers}
          readOnly={readOnly || holdsSelectedRole}
          holdsRole={holdsSelectedRole}
          canEdit={canEdit && !holdsSelectedRole}
          saveError={permission.saveError}
          setSaveError={permission.setSaveError}
          saveOk={permission.saveOk}
          setSaveOk={permission.setSaveOk}
          savePermissions={() => permission.savePermissions(selectedRole?.role_code === 'COMPANY_OWNER')}
          saving={permission.saving}
          discardPermissions={permission.discardPermissions}
        />
      </div>
      {roles.createOpen && (
        <RoleCreateDialog
          createName={roles.createName}
          setCreateName={roles.setCreateName}
          createSaving={roles.createSaving}
          createError={roles.createError}
          setCreateOpen={roles.setCreateOpen}
          handleCreate={roles.handleCreate}
        />
      )}
      {roles.deleteId !== null && (
        <RoleDeleteDialog
          rolesSt={roles.rolesSt}
          deleteId={roles.deleteId}
          deleteWorking={roles.deleteWorking}
          deleteError={roles.deleteError}
          setDeleteId={roles.setDeleteId}
          confirmDelete={roles.confirmDelete}
        />
      )}
      {users.usersModal.roleId !== null && (
        <RoleUsersDialog usersModal={users.usersModal} dispatchUsers={users.dispatchUsers} />
      )}
    </div>
  );
}