import { useEffect, useReducer, useState, useMemo, useCallback } from 'react';
import type { FormEvent } from 'react';
import apiClient from '../../../lib/apiClient';
import { useAuth } from '../../../store/authStore';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import type {
  UserAdmin,
  UserCreate,
  UserUpdate,
  UserPasswordReset,
  RoleAssignment,
  RoleAssignmentCreate,
  Role,
} from '../../../types/admin';
import type { Branch } from '../../../types/core';
import styles from './UsersPage.module.css';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d))
    return d.map((i: unknown) =>
      i && typeof i === 'object' && 'msg' in i ? String((i as { msg: unknown }).msg) : null
    ).filter(Boolean).join(' ');
  if (d !== null && typeof d === 'object' && !Array.isArray(d)) {
    const obj = d as { message?: unknown };
    if (typeof obj.message === 'string') return obj.message;
  }
  return 'An unexpected error occurred.';
}

function fmtDate(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function fmtDateTime(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

/** Returns only currently active role assignments — NEVER show revoked as current. */
function activeRoles(user: UserAdmin): RoleAssignment[] {
  return user.role_assignments.filter(r => r.is_active);
}

function scopeClass(scope: string): string {
  if (scope === 'AllCompanyBranches') return styles.scopeAll;
  if (scope === 'SpecificBranch')     return styles.scopeSpecific;
  if (scope === 'OwnDriverDataOnly')  return styles.scopeOwn;
  return '';
}

function scopeShort(scope: string): string {
  if (scope === 'AllCompanyBranches') return 'All Branches';
  if (scope === 'SpecificBranch')     return 'Branch';
  if (scope === 'OwnDriverDataOnly')  return 'Own Data';
  return scope;
}

function branchSummary(roles: RoleAssignment[]): string {
  if (roles.length === 0) return '—';
  if (roles.some(r => r.scope_type === 'AllCompanyBranches')) return 'All';
  const names = roles
    .map(r => r.branch_name)
    .filter((v): v is string => !!v)
    .filter((v, i, a) => a.indexOf(v) === i);
  if (names.length === 0) return '—';
  if (names.length <= 2)  return names.join(', ');
  return `${names[0]}, +${names.length - 1}`;
}

// ─── Reducers ─────────────────────────────────────────────────────────────────

type UsersState = { users: UserAdmin[]; loading: boolean; error: string };
type UsersAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    users: UserAdmin[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'UPDATE_USER'; user: UserAdmin }
  | { type: 'ADD_USER';    user: UserAdmin };

function usersReducer(s: UsersState, a: UsersAction): UsersState {
  switch (a.type) {
    case 'FETCH_START':  return { ...s, loading: true,  error: '' };
    case 'FETCH_OK':     return { users: a.users, loading: false, error: '' };
    case 'FETCH_ERROR':  return { ...s, loading: false, error: a.error };
    case 'UPDATE_USER':  return { ...s, users: s.users.map(u => u.user_id === a.user.user_id ? a.user : u) };
    // Insert at front; if already present (e.g. from background refetch), replace it
    case 'ADD_USER': {
      const exists = s.users.some(u => u.user_id === a.user.user_id);
      return {
        ...s,
        users: exists
          ? s.users.map(u => u.user_id === a.user.user_id ? a.user : u)
          : [a.user, ...s.users],
      };
    }
    default:             return s;
  }
}

type RolesState = { roles: Role[]; loading: boolean };
type RolesAction = { type: 'FETCH_START' } | { type: 'FETCH_OK'; roles: Role[] };
function rolesReducer(s: RolesState, a: RolesAction): RolesState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true };
    case 'FETCH_OK':    return { roles: a.roles, loading: false };
    default:            return s;
  }
}

type BranchesState = { branches: Branch[]; loading: boolean };
type BranchesAction = { type: 'FETCH_START' } | { type: 'FETCH_OK'; branches: Branch[] };
function branchesReducer(s: BranchesState, a: BranchesAction): BranchesState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true };
    case 'FETCH_OK':    return { branches: a.branches, loading: false };
    default:            return s;
  }
}

// ─── Types ────────────────────────────────────────────────────────────────────

type StatusFilter = 'Active' | 'Inactive' | 'All';

// ─── Main page ────────────────────────────────────────────────────────────────

export function UsersPage() {
  const { user: currentUser } = useAuth();

  // ── Core data ──────────────────────────────────────────────────────────────
  const [usersSt,    dispatchUsers]    = useReducer(usersReducer,    { users: [], loading: true, error: '' });
  const [rolesSt,    dispatchRoles]    = useReducer(rolesReducer,    { roles: [], loading: true });
  const [branchesSt, dispatchBranches] = useReducer(branchesReducer, { branches: [], loading: true });

  // ── Filters ────────────────────────────────────────────────────────────────
  const [search,       setSearch]       = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('Active');
  const [scopeFilter,  setScopeFilter]  = useState('');
  const [roleFilter,   setRoleFilter]   = useState('');

  // ── Selection ──────────────────────────────────────────────────────────────
  const [selectedUserId, setSelectedUserId] = useState<number | null>(null);

  // ── Modal states ───────────────────────────────────────────────────────────
  const [addUserOpen,    setAddUserOpen]    = useState(false);
  const [editUserOpen,   setEditUserOpen]   = useState(false);
  const [resetPwOpen,    setResetPwOpen]    = useState(false);
  const [assignRoleOpen, setAssignRoleOpen] = useState(false);

  // ── Confirm dialogs ────────────────────────────────────────────────────────
  const [confirmToggleActive, setConfirmToggleActive] = useState(false);
  const [confirmRevoke, setConfirmRevoke] = useState<{ assignmentId: number; roleName: string } | null>(null);
  const [togglingActive, setTogglingActive] = useState(false);
  const [revoking, setRevoking] = useState(false);

  // ── Role history ───────────────────────────────────────────────────────────
  const [roleHistoryExpanded, setRoleHistoryExpanded] = useState(false);
  const [roleHistory,         setRoleHistory]         = useState<RoleAssignment[]>([]);
  const [roleHistoryLoading,  setRoleHistoryLoading]  = useState(false);

  // ── Toast ──────────────────────────────────────────────────────────────────
  const [toast, setToast] = useState<{ msg: string; warn?: boolean } | null>(null);

  function showToast(msg: string, warn = false) {
    setToast({ msg, warn });
    setTimeout(() => setToast(null), warn ? 8000 : 3500);
  }

  // ── Permission gate ────────────────────────────────────────────────────────
  const canAdmin = !!(currentUser?.scope_type === 'AllCompanyBranches' && currentUser.has_setup_manage);

  // ── Fetch users ────────────────────────────────────────────────────────────
  const includeInactive = statusFilter !== 'Active';

  const fetchUsers = useCallback(async () => {
    dispatchUsers({ type: 'FETCH_START' });
    try {
      const { data } = await apiClient.get<UserAdmin[]>('/admin/users', {
        params: { include_inactive: includeInactive },
      });
      dispatchUsers({ type: 'FETCH_OK', users: data });
    } catch (e) {
      dispatchUsers({ type: 'FETCH_ERROR', error: apiError(e) });
    }
  }, [includeInactive]);

  useEffect(() => { void fetchUsers(); }, [fetchUsers]);

  // ── Fetch roles + branches (once) ──────────────────────────────────────────
  useEffect(() => {
    dispatchRoles({ type: 'FETCH_START' });
    apiClient.get<Role[]>('/admin/roles')
      .then(r => dispatchRoles({ type: 'FETCH_OK', roles: r.data }))
      .catch(() => dispatchRoles({ type: 'FETCH_OK', roles: [] }));
  }, []);

  useEffect(() => {
    dispatchBranches({ type: 'FETCH_START' });
    apiClient.get<Branch[]>('/core/branches')
      .then(r => dispatchBranches({ type: 'FETCH_OK', branches: r.data }))
      .catch(() => dispatchBranches({ type: 'FETCH_OK', branches: [] }));
  }, []);

  // ── Derived: selected user ─────────────────────────────────────────────────
  const selectedUser = useMemo(
    () => usersSt.users.find(u => u.user_id === selectedUserId) ?? null,
    [usersSt.users, selectedUserId]
  );

  // ── Derived: filtered users ────────────────────────────────────────────────
  const filteredUsers = useMemo(() => {
    let list = usersSt.users;

    // Apply status filter client-side so local mutations (activate/deactivate)
    // take effect immediately without waiting for a refetch.
    if (statusFilter === 'Active')   list = list.filter(u => u.is_active);
    if (statusFilter === 'Inactive') list = list.filter(u => !u.is_active);

    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(u =>
        u.display_name.toLowerCase().includes(q) ||
        u.username.toLowerCase().includes(q)
      );
    }

    if (scopeFilter) {
      list = list.filter(u =>
        activeRoles(u).some(r => r.scope_type === scopeFilter)
      );
    }

    if (roleFilter) {
      list = list.filter(u =>
        activeRoles(u).some(r => r.role_code === roleFilter)
      );
    }

    return list;
  }, [usersSt.users, statusFilter, search, scopeFilter, roleFilter]);

  // ── Helpers ────────────────────────────────────────────────────────────────
  function selectUser(userId: number) {
    setSelectedUserId(userId);
    setRoleHistoryExpanded(false);
    setRoleHistory([]);
  }

  function isSelf(u: UserAdmin): boolean {
    return u.user_id === currentUser?.user_id;
  }

  async function loadRoleHistory(userId: number) {
    setRoleHistoryLoading(true);
    try {
      const { data } = await apiClient.get<RoleAssignment[]>(`/admin/users/${userId}/roles`);
      setRoleHistory(data);
    } catch {
      // non-fatal — show empty history
    } finally {
      setRoleHistoryLoading(false);
    }
  }

  // ── Action: toggle active ──────────────────────────────────────────────────
  async function handleToggleActive() {
    if (!selectedUser) return;
    setTogglingActive(true);
    try {
      const { data } = await apiClient.patch<UserAdmin>(
        `/admin/users/${selectedUser.user_id}`,
        { is_active: !selectedUser.is_active } as UserUpdate,
      );
      dispatchUsers({ type: 'UPDATE_USER', user: data });
      setConfirmToggleActive(false);
      showToast(`User ${data.is_active ? 'activated' : 'deactivated'}.`);
    } catch (e) {
      setConfirmToggleActive(false);
      showToast(apiError(e));
    } finally {
      setTogglingActive(false);
    }
  }

  // ── Action: revoke role ────────────────────────────────────────────────────
  async function handleRevokeRole(assignmentId: number) {
    if (!selectedUser) return;
    setRevoking(true);
    try {
      await apiClient.delete(`/admin/users/${selectedUser.user_id}/roles/${assignmentId}`);
      const { data } = await apiClient.get<UserAdmin>(`/admin/users/${selectedUser.user_id}`);
      dispatchUsers({ type: 'UPDATE_USER', user: data });
      setConfirmRevoke(null);
      // If history panel is open, reload it so the revoked entry appears immediately.
      if (roleHistoryExpanded) {
        void loadRoleHistory(data.user_id);
      } else {
        setRoleHistory([]);
      }
      showToast('Role revoked.');
    } catch (e) {
      setConfirmRevoke(null);
      showToast(apiError(e));
    } finally {
      setRevoking(false);
    }
  }

  // ── Render ─────────────────────────────────────────────────────────────────

  if (!canAdmin) {
    return (
      <div className={styles.noPermission}>
        <LockBigIcon />
        <p className={styles.noPermissionTitle}>Access Restricted</p>
        <p className={styles.noPermissionText}>
          You do not have permission to manage users. This section requires
          AllCompanyBranches scope with admin access.
        </p>
      </div>
    );
  }

  return (
    <div className={styles.page}>

      {/* Page header */}
      <div className={styles.pageHeader}>
        <div className={styles.pageTitleGroup}>
          <h2 className={styles.pageTitle}>Users & Permissions</h2>
          <p className={styles.pageSubtitle}>Manage account access, roles, and branch permissions.</p>
        </div>
        <div className={styles.pageActions}>
          <button className={styles.btnPrimary} onClick={() => setAddUserOpen(true)}>
            <PlusIcon /> Add User
          </button>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div className={`${styles.toast} ${toast.warn ? styles.toastWarn : ''}`}>
          {toast.msg}
        </div>
      )}

      {/* Two-column body */}
      <div className={styles.body}>

        {/* ── LEFT: users list ── */}
        <div className={styles.listCard}>

          {/* Toolbar */}
          <div className={styles.listToolbar}>
            <div className={styles.searchWrap}>
              <SearchIcon className={styles.searchIcon} />
              <input
                type="search"
                className={styles.searchInput}
                placeholder="Search name or username…"
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
            </div>

            <div className={styles.filterGroup}>
              {(['Active', 'All', 'Inactive'] as StatusFilter[]).map(f => (
                <button
                  key={f}
                  className={`${styles.filterPill} ${statusFilter === f ? styles.filterPillActive : ''}`}
                  onClick={() => setStatusFilter(f)}
                >
                  {f}
                </button>
              ))}
            </div>

            <select
              className={styles.filterSelect}
              value={scopeFilter}
              onChange={e => setScopeFilter(e.target.value)}
              aria-label="Filter by scope"
            >
              <option value="">All Scopes</option>
              <option value="AllCompanyBranches">All Branches</option>
              <option value="SpecificBranch">Specific Branch</option>
              <option value="OwnDriverDataOnly">Own Driver Data</option>
            </select>

            {rolesSt.roles.length > 0 && (
              <select
                className={styles.filterSelect}
                value={roleFilter}
                onChange={e => setRoleFilter(e.target.value)}
                aria-label="Filter by role"
              >
                <option value="">All Roles</option>
                {rolesSt.roles.map(r => (
                  <option key={r.role_id} value={r.role_code}>{r.role_name}</option>
                ))}
              </select>
            )}
          </div>

          {/* Table */}
          {usersSt.loading ? (
            <div className={styles.stateMsg}>Loading users…</div>
          ) : usersSt.error ? (
            <div className={styles.errorMsg}>{usersSt.error}</div>
          ) : filteredUsers.length === 0 ? (
            <div className={styles.emptyMsg}>
              {usersSt.users.length === 0
                ? 'No users found.'
                : 'No users match the current filters.'}
            </div>
          ) : (
            <div className={styles.tableWrap}>
              <table className={styles.table}>
                <colgroup>
                  <col style={{ width: '26%' }} />
                  <col style={{ width: '10%' }} />
                  <col style={{ width: '9%' }} />
                  <col style={{ width: '24%' }} />
                  <col style={{ width: '16%' }} />
                  <col style={{ width: '15%' }} />
                </colgroup>
                <thead>
                  <tr>
                    <th>User</th>
                    <th>Status</th>
                    <th>Login</th>
                    <th>Role(s)</th>
                    <th>Scope</th>
                    <th>Branches</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredUsers.map(u => {
                    const active = activeRoles(u);
                    const isSelected = u.user_id === selectedUserId;
                    return (
                      <tr
                        key={u.user_id}
                        className={`${styles.tableRow} ${isSelected ? styles.tableRowSelected : ''}`}
                        onClick={() => selectUser(u.user_id)}
                      >
                        <td>
                          <div className={styles.userCell}>
                            <span className={styles.displayName}>{u.display_name}</span>
                            <span className={styles.usernameChip}>@{u.username}</span>
                          </div>
                        </td>
                        <td>
                          <span className={`${styles.badge} ${u.is_active ? styles.badgeActive : styles.badgeInactive}`}>
                            <span className={`${styles.dot} ${u.is_active ? styles.dotActive : styles.dotInactive}`} />
                            {u.is_active ? 'Active' : 'Inactive'}
                          </span>
                        </td>
                        <td>
                          {u.can_login
                            ? <span className={`${styles.badge} ${styles.badgeLogin}`}>Yes</span>
                            : <span className={`${styles.badge} ${styles.badgeNoLogin}`}>No</span>
                          }
                        </td>
                        <td>
                          {active.length === 0 ? (
                            <span className={styles.muted}>—</span>
                          ) : (
                            <div className={styles.roleChips}>
                              <span className={styles.roleChip}>{active[0].role_name}</span>
                              {active.length > 1 && (
                                <span className={styles.roleChipMore}>+{active.length - 1}</span>
                              )}
                            </div>
                          )}
                        </td>
                        <td>
                          {active.length === 0 ? (
                            <span className={styles.muted}>—</span>
                          ) : (
                            <span className={`${styles.scopeBadge} ${scopeClass(active[0].scope_type)}`}>
                              {scopeShort(active[0].scope_type)}
                            </span>
                          )}
                        </td>
                        <td>
                          <span className={styles.branchesCell}>
                            {branchSummary(active)}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* ── RIGHT: detail panel ── */}
        <div className={styles.detailPanel}>
          {!selectedUser ? (
            <div className={styles.detailEmpty}>
              <EmptyUsersIcon />
              <p className={styles.detailEmptyTitle}>Choose a user to show details</p>
              <p className={styles.detailEmptyText}>
                Select a user to view account access, role assignments, and branch permissions.
              </p>
            </div>
          ) : (
            <UserDetailPanel
              user={selectedUser}
              isSelf={isSelf(selectedUser)}
              roleHistoryExpanded={roleHistoryExpanded}
              roleHistory={roleHistory}
              roleHistoryLoading={roleHistoryLoading}
              onEdit={() => setEditUserOpen(true)}
              onResetPw={() => setResetPwOpen(true)}
              onAssignRole={() => setAssignRoleOpen(true)}
              onToggleActive={() => setConfirmToggleActive(true)}
              onRevokeRole={(assignmentId, roleName) =>
                setConfirmRevoke({ assignmentId, roleName })
              }
              onToggleHistory={() => {
                const willExpand = !roleHistoryExpanded;
                setRoleHistoryExpanded(willExpand);
                if (willExpand && roleHistory.length === 0) {
                  void loadRoleHistory(selectedUser.user_id);
                }
              }}
            />
          )}
        </div>
      </div>

      {/* ── Modals ── */}
      {addUserOpen && (
        <AddUserModal
          roles={rolesSt.roles}
          branches={branchesSt.branches}
          onClose={() => setAddUserOpen(false)}
          onCreated={async (newUser) => {
            setAddUserOpen(false);
            // Insert immediately so the user is visible without waiting for refetch.
            dispatchUsers({ type: 'ADD_USER', user: newUser });
            // Clear text/scope/role filters that might hide the new user.
            setSearch('');
            setScopeFilter('');
            setRoleFilter('');
            // If current status filter would hide the new user, switch to All.
            if (statusFilter === 'Inactive' && newUser.is_active) setStatusFilter('All');
            if (statusFilter === 'Active'   && !newUser.is_active) setStatusFilter('All');
            selectUser(newUser.user_id);
            // Background refetch to sync any server-side changes.
            void fetchUsers();
          }}
          showToast={showToast}
        />
      )}

      {editUserOpen && selectedUser && (
        <EditUserModal
          user={selectedUser}
          isSelf={isSelf(selectedUser)}
          onClose={() => setEditUserOpen(false)}
          onSaved={(updated) => {
            dispatchUsers({ type: 'UPDATE_USER', user: updated });
            setEditUserOpen(false);
            showToast('User updated.');
          }}
        />
      )}

      {resetPwOpen && selectedUser && (
        <ResetPasswordModal
          user={selectedUser}
          onClose={() => setResetPwOpen(false)}
          onDone={() => {
            setResetPwOpen(false);
            showToast('Password reset successfully.');
          }}
        />
      )}

      {assignRoleOpen && selectedUser && (
        <AssignRoleModal
          user={selectedUser}
          roles={rolesSt.roles}
          branches={branchesSt.branches}
          onClose={() => setAssignRoleOpen(false)}
          onAssigned={async () => {
            const { data } = await apiClient.get<UserAdmin>(`/admin/users/${selectedUser.user_id}`);
            dispatchUsers({ type: 'UPDATE_USER', user: data });
            setAssignRoleOpen(false);
            setRoleHistory([]);
            showToast('Role assigned.');
          }}
        />
      )}

      {/* Confirm: toggle active */}
      {selectedUser && (
        <ConfirmDialog
          open={confirmToggleActive}
          title={selectedUser.is_active ? 'Deactivate User' : 'Activate User'}
          message={selectedUser.is_active
            ? `Deactivate ${selectedUser.display_name}? They will no longer be able to log in.`
            : `Activate ${selectedUser.display_name}?`}
          confirmLabel={selectedUser.is_active ? 'Deactivate' : 'Activate'}
          variant={selectedUser.is_active ? 'danger' : 'primary'}
          loading={togglingActive}
          onConfirm={handleToggleActive}
          onCancel={() => setConfirmToggleActive(false)}
        />
      )}

      {/* Confirm: revoke role */}
      {confirmRevoke && (
        <ConfirmDialog
          open={!!confirmRevoke}
          title="Revoke Role Assignment"
          message={`Revoke the "${confirmRevoke.roleName}" role assignment?`}
          confirmLabel="Revoke"
          variant="danger"
          loading={revoking}
          onConfirm={() => handleRevokeRole(confirmRevoke.assignmentId)}
          onCancel={() => setConfirmRevoke(null)}
        />
      )}
    </div>
  );
}

// ─── User Detail Panel ────────────────────────────────────────────────────────

function UserDetailPanel({
  user,
  isSelf,
  roleHistoryExpanded,
  roleHistory,
  roleHistoryLoading,
  onEdit,
  onResetPw,
  onAssignRole,
  onToggleActive,
  onRevokeRole,
  onToggleHistory,
}: {
  user: UserAdmin;
  isSelf: boolean;
  roleHistoryExpanded: boolean;
  roleHistory: RoleAssignment[];
  roleHistoryLoading: boolean;
  onEdit: () => void;
  onResetPw: () => void;
  onAssignRole: () => void;
  onToggleActive: () => void;
  onRevokeRole: (assignmentId: number, roleName: string) => void;
  onToggleHistory: () => void;
}) {
  const roles = activeRoles(user);

  return (
    <div className={styles.detailContent}>

      {/* Header */}
      <div className={styles.detailHeader}>
        <div className={styles.detailHeaderTop}>
          <div className={styles.detailUserInfo}>
            <h3 className={styles.detailDisplayName}>{user.display_name}</h3>
            <span className={styles.detailUsernameChip}>@{user.username}</span>
          </div>
          <div className={styles.detailHeaderBadges}>
            <span className={`${styles.badge} ${user.is_active ? styles.badgeActive : styles.badgeInactive}`}>
              <span className={`${styles.dot} ${user.is_active ? styles.dotActive : styles.dotInactive}`} />
              {user.is_active ? 'Active' : 'Inactive'}
            </span>
            {!user.can_login && (
              <span className={`${styles.badge} ${styles.badgeNoLogin}`}>Login Disabled</span>
            )}
          </div>
        </div>
        <div className={styles.detailHeaderActions}>
          <button className={styles.btnSecondary} onClick={onEdit}>Edit</button>
          <button className={styles.btnSecondary} onClick={onResetPw}>Reset Password</button>
        </div>
      </div>

      {/* Body */}
      <div className={styles.detailBody}>

        {/* Account Access */}
        <div className={styles.detailSection}>
          <p className={styles.detailSectionTitle}>Account Access</p>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Status</span>
            <span className={styles.detailValue}>
              <span className={`${styles.badge} ${user.is_active ? styles.badgeActive : styles.badgeInactive}`}>
                <span className={`${styles.dot} ${user.is_active ? styles.dotActive : styles.dotInactive}`} />
                {user.is_active ? 'Active' : 'Inactive'}
              </span>
            </span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Can Login</span>
            <span className={styles.detailValue}>
              {user.can_login
                ? <span className={`${styles.badge} ${styles.badgeLogin}`}>Enabled</span>
                : <span className={`${styles.badge} ${styles.badgeNoLogin}`}>Disabled</span>
              }
            </span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Must Change Password</span>
            <span className={user.must_change_password ? styles.detailValueWarn : styles.detailValue}>
              {user.must_change_password ? 'Yes' : 'No'}
            </span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Last Login</span>
            <span className={styles.detailValue}>{fmtDateTime(user.last_login_at_utc)}</span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Created</span>
            <span className={styles.detailValue}>{fmtDate(user.created_at_utc)}</span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Updated</span>
            <span className={styles.detailValue}>{fmtDate(user.updated_at_utc)}</span>
          </div>
        </div>

        {/* Role Assignments */}
        <div className={styles.detailSection}>
          <div className={styles.detailSectionHeader}>
            <p className={styles.detailSectionTitle}>Role Assignments</p>
            <button className={styles.btnGhost} onClick={onAssignRole}>
              <PlusIcon /> Assign Role
            </button>
          </div>
          {roles.length === 0 ? (
            <p className={styles.noRolesHint}>No active role assignments.</p>
          ) : (
            <div className={styles.roleAssignmentList}>
              {roles.map(r => (
                <div key={r.assignment_id} className={styles.roleAssignmentRow}>
                  <div className={styles.roleAssignmentInfo}>
                    <span className={styles.roleAssignmentName}>{r.role_name}</span>
                    <div className={styles.roleAssignmentMeta}>
                      <span className={`${styles.scopeBadge} ${scopeClass(r.scope_type)}`}>
                        {scopeShort(r.scope_type)}
                      </span>
                      {r.branch_name && (
                        <span className={styles.branchPill}>{r.branch_name}</span>
                      )}
                    </div>
                    {r.notes && <span className={styles.roleNotes}>{r.notes}</span>}
                    <span className={styles.roleGrantedDate}>Granted {fmtDate(r.granted_at_utc)}</span>
                  </div>
                  <button
                    className={styles.revokeBtn}
                    onClick={() => onRevokeRole(r.assignment_id, r.role_name)}
                    title="Revoke role assignment"
                  >
                    <XSmallIcon />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Role History (collapsed) */}
        <div className={styles.detailSection}>
          <button className={styles.historyToggle} onClick={onToggleHistory}>
            <ChevronIcon expanded={roleHistoryExpanded} />
            Role History
          </button>
          {roleHistoryExpanded && (
            <div className={styles.historyContent}>
              {roleHistoryLoading ? (
                <p className={styles.muted}>Loading…</p>
              ) : roleHistory.length === 0 ? (
                <p className={styles.muted}>No role history.</p>
              ) : (
                roleHistory.map(r => (
                  <div
                    key={r.assignment_id}
                    className={`${styles.historyRow} ${!r.is_active ? styles.historyRowRevoked : ''}`}
                  >
                    <div className={styles.historyRowMain}>
                      <span className={styles.roleAssignmentName}>{r.role_name}</span>
                      <span className={`${styles.badge} ${r.is_active ? styles.badgeActive : styles.badgeRevoked}`}>
                        {r.is_active ? 'Active' : 'Revoked'}
                      </span>
                    </div>
                    <div className={styles.roleAssignmentMeta}>
                      <span className={`${styles.scopeBadge} ${scopeClass(r.scope_type)}`}>
                        {scopeShort(r.scope_type)}
                      </span>
                      {r.branch_name && (
                        <span className={styles.branchPill}>{r.branch_name}</span>
                      )}
                    </div>
                    <div className={styles.historyDates}>
                      <span>Granted: {fmtDate(r.granted_at_utc)}</span>
                      {r.revoked_at_utc && <span>Revoked: {fmtDate(r.revoked_at_utc)}</span>}
                    </div>
                  </div>
                ))
              )}
            </div>
          )}
        </div>

        {/* Contact */}
        <div className={styles.detailSection}>
          <p className={styles.detailSectionTitle}>Contact</p>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Email</span>
            <span className={user.email ? styles.detailValue : styles.detailValueMuted}>
              {user.email ?? '—'}
            </span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Phone</span>
            <span className={user.phone ? styles.detailValue : styles.detailValueMuted}>
              {user.phone ?? '—'}
            </span>
          </div>
        </div>

      </div>

      {/* Danger Zone */}
      <div className={styles.dangerZone}>
        {isSelf && (
          <p className={styles.selfWarning}>
            <InfoIcon /> You cannot deactivate or disable login for your own account.
          </p>
        )}
        <button
          className={user.is_active ? styles.btnDanger : styles.btnSecondary}
          onClick={onToggleActive}
          disabled={isSelf}
        >
          {user.is_active ? 'Deactivate User' : 'Activate User'}
        </button>
      </div>
    </div>
  );
}

// ─── Add User Modal ───────────────────────────────────────────────────────────

function AddUserModal({
  roles,
  branches,
  onClose,
  onCreated,
  showToast,
}: {
  roles: Role[];
  branches: Branch[];
  onClose: () => void;
  onCreated: (user: UserAdmin) => Promise<void>;
  showToast: (msg: string, warn?: boolean) => void;
}) {
  const [displayName, setDisplayName] = useState('');
  const [username,    setUsername]    = useState('');
  const [password,    setPassword]    = useState('');
  const [email,       setEmail]       = useState('');
  const [phone,       setPhone]       = useState('');
  const [isActive,    setIsActive]    = useState(true);
  const [canLogin,    setCanLogin]    = useState(true);
  const [mustChange,  setMustChange]  = useState(true);

  // First role (optional)
  const [addRole,   setAddRole]   = useState(roles.length > 0);
  const [roleId,    setRoleId]    = useState<number | ''>(roles[0]?.role_id ?? '');
  const [scopeType, setScopeType] = useState<'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly'>('AllCompanyBranches');
  const [branchId,  setBranchId]  = useState<number | ''>(branches[0]?.branch_id ?? '');
  const [roleNotes, setRoleNotes] = useState('');

  const [saving, setSaving] = useState(false);
  const [error,  setError]  = useState('');

  function validate(): string | null {
    if (!displayName.trim()) return 'Display name is required.';
    if (!username.trim())    return 'Username is required.';
    if (!password)           return 'Password is required.';
    if (password.length < 8) return 'Password must be at least 8 characters.';
    if (addRole) {
      if (!roleId) return 'Please select a role.';
      if (scopeType !== 'AllCompanyBranches' && !branchId) return 'Please select a branch for this scope.';
    }
    return null;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const err = validate();
    if (err) { setError(err); return; }
    setSaving(true);
    setError('');
    try {
      // Step 1: Create user
      const { data: newUser } = await apiClient.post<UserAdmin>('/admin/users', {
        display_name:        displayName.trim(),
        username:            username.trim(),
        password,
        email:               email.trim()  || null,
        phone:               phone.trim()  || null,
        is_active:           isActive,
        can_login:           canLogin,
        must_change_password: mustChange,
      } as UserCreate);

      // Step 2: Assign first role (if requested)
      if (addRole && roleId) {
        try {
          const { data: assignment } = await apiClient.post<RoleAssignment>(
            `/admin/users/${newUser.user_id}/roles`,
            {
              role_id:    Number(roleId),
              scope_type: scopeType,
              branch_id:  scopeType !== 'AllCompanyBranches' ? Number(branchId) : null,
              notes:      roleNotes.trim() || null,
            } as RoleAssignmentCreate,
          );
          newUser.role_assignments = [...newUser.role_assignments, assignment];
        } catch (roleErr) {
          // Partial success — user created but role assignment failed
          showToast(
            `User created, but role assignment failed: ${apiError(roleErr)}. Assign a role manually from the details panel.`,
            true,
          );
          await onCreated(newUser);
          return;
        }
      }

      await onCreated(newUser);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className={styles.modalOverlay}
      onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}
      role="dialog"
      aria-modal="true"
    >
      <div className={styles.modal}>
        <div className={styles.modalHeader}>
          <h3 className={styles.modalTitle}>Add User</h3>
          <button className={styles.modalCloseBtn} onClick={onClose} type="button"><XSmallIcon /></button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className={styles.modalBody}>
            {error && <div className={styles.formError}>{error}</div>}

            <div className={styles.formGrid}>
              <div className={styles.formGroup}>
                <label className={styles.formLabel}>Display Name <span className={styles.required}>*</span></label>
                <input className={styles.formInput} value={displayName} onChange={e => setDisplayName(e.target.value)} maxLength={120} autoFocus />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.formLabel}>Username <span className={styles.required}>*</span></label>
                <input className={styles.formInput} value={username} onChange={e => setUsername(e.target.value.toLowerCase())} maxLength={80} autoComplete="off" />
                <span className={styles.formHint}>Lowercase only. Cannot be changed after creation.</span>
              </div>
            </div>

            <div className={styles.formGroup}>
              <label className={styles.formLabel}>Password <span className={styles.required}>*</span></label>
              <input type="password" className={styles.formInput} value={password} onChange={e => setPassword(e.target.value)} autoComplete="new-password" />
              <span className={styles.formHint}>Minimum 8 characters.</span>
            </div>

            <div className={styles.formGrid}>
              <div className={styles.formGroup}>
                <label className={styles.formLabel}>Email</label>
                <input type="email" className={styles.formInput} value={email} onChange={e => setEmail(e.target.value)} placeholder="Optional" />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.formLabel}>Phone</label>
                <input className={styles.formInput} value={phone} onChange={e => setPhone(e.target.value)} placeholder="Optional" />
              </div>
            </div>

            <div className={styles.flagRow}>
              <label className={styles.checkLabel}><input type="checkbox" checked={isActive}   onChange={e => setIsActive(e.target.checked)}   /> Active</label>
              <label className={styles.checkLabel}><input type="checkbox" checked={canLogin}   onChange={e => setCanLogin(e.target.checked)}   /> Can Login</label>
              <label className={styles.checkLabel}><input type="checkbox" checked={mustChange} onChange={e => setMustChange(e.target.checked)} /> Must Change Password</label>
            </div>

            {/* Initial role */}
            <div className={styles.formSection}>
              <label className={styles.checkLabel}>
                <input type="checkbox" checked={addRole} onChange={e => setAddRole(e.target.checked)} />
                Assign an initial role
              </label>
              {addRole && (
                <div className={styles.roleSubForm}>
                  <div className={styles.formGrid}>
                    <div className={styles.formGroup}>
                      <label className={styles.formLabel}>Role <span className={styles.required}>*</span></label>
                      <select className={styles.formSelect} value={roleId} onChange={e => setRoleId(Number(e.target.value))}>
                        <option value="" disabled>— Select role —</option>
                        {roles.map(r => <option key={r.role_id} value={r.role_id}>{r.role_name}</option>)}
                      </select>
                    </div>
                    <div className={styles.formGroup}>
                      <label className={styles.formLabel}>Scope <span className={styles.required}>*</span></label>
                      <select className={styles.formSelect} value={scopeType} onChange={e => setScopeType(e.target.value as typeof scopeType)}>
                        <option value="AllCompanyBranches">All Company Branches</option>
                        <option value="SpecificBranch">Specific Branch</option>
                        <option value="OwnDriverDataOnly">Own Driver Data Only</option>
                      </select>
                    </div>
                  </div>
                  {scopeType !== 'AllCompanyBranches' && (
                    <div className={styles.formGroup}>
                      <label className={styles.formLabel}>Branch <span className={styles.required}>*</span></label>
                      <select className={styles.formSelect} value={branchId} onChange={e => setBranchId(Number(e.target.value))}>
                        <option value="" disabled>— Select branch —</option>
                        {branches.map(b => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
                      </select>
                    </div>
                  )}
                  <div className={styles.formGroup}>
                    <label className={styles.formLabel}>Notes (optional)</label>
                    <input className={styles.formInput} value={roleNotes} onChange={e => setRoleNotes(e.target.value)} />
                  </div>
                </div>
              )}
            </div>
          </div>

          <div className={styles.modalFooter}>
            <button type="submit" className={styles.btnPrimary} disabled={saving}>
              {saving ? 'Creating…' : 'Create User'}
            </button>
            <button type="button" className={styles.btnSecondary} onClick={onClose} disabled={saving}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Edit User Modal ──────────────────────────────────────────────────────────

function EditUserModal({
  user,
  isSelf,
  onClose,
  onSaved,
}: {
  user: UserAdmin;
  isSelf: boolean;
  onClose: () => void;
  onSaved: (updated: UserAdmin) => void;
}) {
  const [displayName, setDisplayName] = useState(user.display_name);
  const [email,       setEmail]       = useState(user.email ?? '');
  const [phone,       setPhone]       = useState(user.phone ?? '');
  const [isActive,    setIsActive]    = useState(user.is_active);
  const [canLogin,    setCanLogin]    = useState(user.can_login);
  const [mustChange,  setMustChange]  = useState(user.must_change_password);

  const [saving,      setSaving]      = useState(false);
  const [error,       setError]       = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);

  const deactivating   = !isActive  && user.is_active;
  const disablingLogin = !canLogin  && user.can_login;

  function validate(): string | null {
    if (!displayName.trim()) return 'Display name is required.';
    return null;
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const err = validate();
    if (err) { setError(err); return; }
    setConfirmOpen(true);
  }

  async function doSave() {
    setConfirmOpen(false);
    setSaving(true);
    setError('');
    try {
      const { data } = await apiClient.patch<UserAdmin>(
        `/admin/users/${user.user_id}`,
        {
          display_name:        displayName.trim(),
          email:               email.trim() || null,
          phone:               phone.trim() || null,
          is_active:           isActive,
          can_login:           canLogin,
          must_change_password: mustChange,
        } as UserUpdate,
      );
      onSaved(data);
    } catch (e) {
      setError(apiError(e));
    } finally {
      setSaving(false);
    }
  }

  const confirmTitle   = deactivating ? 'Deactivate User' : disablingLogin ? 'Disable Login' : 'Save Changes';
  const confirmMsg     = deactivating
    ? `Deactivate ${user.display_name}? They will no longer be able to log in.`
    : disablingLogin
    ? `Disable login for ${user.display_name}?`
    : `Save changes to ${user.display_name}?`;
  const confirmVariant: 'danger' | 'primary' = (deactivating || disablingLogin) ? 'danger' : 'primary';

  return (
    <div
      className={styles.modalOverlay}
      onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}
      role="dialog"
      aria-modal="true"
    >
      <div className={styles.modal}>
        <div className={styles.modalHeader}>
          <h3 className={styles.modalTitle}>Edit User</h3>
          <button className={styles.modalCloseBtn} onClick={onClose} type="button"><XSmallIcon /></button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className={styles.modalBody}>
            {error && <div className={styles.formError}>{error}</div>}

            <div className={styles.formGroup}>
              <label className={styles.formLabel}>Username</label>
              <input className={`${styles.formInput} ${styles.formInputReadonly}`} value={user.username} readOnly />
              <span className={styles.formHint}>Username cannot be changed after creation.</span>
            </div>

            <div className={styles.formGroup}>
              <label className={styles.formLabel}>Display Name <span className={styles.required}>*</span></label>
              <input className={styles.formInput} value={displayName} onChange={e => setDisplayName(e.target.value)} autoFocus maxLength={120} />
            </div>

            <div className={styles.formGrid}>
              <div className={styles.formGroup}>
                <label className={styles.formLabel}>Email</label>
                <input type="email" className={styles.formInput} value={email} onChange={e => setEmail(e.target.value)} />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.formLabel}>Phone</label>
                <input className={styles.formInput} value={phone} onChange={e => setPhone(e.target.value)} />
              </div>
            </div>

            <div className={styles.flagSection}>
              <div className={styles.flagRow}>
                <label className={`${styles.checkLabel} ${isSelf ? styles.checkLabelDisabled : ''}`}>
                  <input type="checkbox" checked={isActive}   onChange={e => setIsActive(e.target.checked)}   disabled={isSelf} />
                  Active
                </label>
                <label className={`${styles.checkLabel} ${isSelf ? styles.checkLabelDisabled : ''}`}>
                  <input type="checkbox" checked={canLogin}   onChange={e => setCanLogin(e.target.checked)}   disabled={isSelf} />
                  Can Login
                </label>
                <label className={styles.checkLabel}>
                  <input type="checkbox" checked={mustChange} onChange={e => setMustChange(e.target.checked)} />
                  Must Change Password
                </label>
              </div>
              {isSelf && (
                <p className={styles.selfWarning}>
                  <InfoIcon /> You cannot deactivate or disable login for your own account.
                </p>
              )}
            </div>
          </div>

          <div className={styles.modalFooter}>
            <button type="submit" className={styles.btnPrimary} disabled={saving}>
              {saving ? 'Saving…' : 'Save Changes'}
            </button>
            <button type="button" className={styles.btnSecondary} onClick={onClose} disabled={saving}>
              Cancel
            </button>
          </div>
        </form>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title={confirmTitle}
        message={confirmMsg}
        confirmLabel={deactivating ? 'Deactivate' : disablingLogin ? 'Disable Login' : 'Save'}
        variant={confirmVariant}
        loading={saving}
        onConfirm={doSave}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}

// ─── Reset Password Modal ─────────────────────────────────────────────────────

function ResetPasswordModal({
  user,
  onClose,
  onDone,
}: {
  user: UserAdmin;
  onClose: () => void;
  onDone: () => void;
}) {
  const [newPassword,     setNewPassword]     = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [mustChange,      setMustChange]      = useState(true);
  const [saving,          setSaving]          = useState(false);
  const [error,           setError]           = useState('');
  const [confirmOpen,     setConfirmOpen]     = useState(false);

  function validate(): string | null {
    if (!newPassword)            return 'Password is required.';
    if (newPassword.length < 8)  return 'Password must be at least 8 characters.';
    if (newPassword !== confirmPassword) return 'Passwords do not match.';
    return null;
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const err = validate();
    if (err) { setError(err); return; }
    setConfirmOpen(true);
  }

  async function doReset() {
    setConfirmOpen(false);
    setSaving(true);
    setError('');
    try {
      await apiClient.post<UserAdmin>(
        `/admin/users/${user.user_id}/reset-password`,
        { new_password: newPassword, must_change_password: mustChange } as UserPasswordReset,
      );
      onDone();
    } catch (e) {
      setError(apiError(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className={styles.modalOverlay}
      onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}
      role="dialog"
      aria-modal="true"
    >
      <div className={styles.modal}>
        <div className={styles.modalHeader}>
          <h3 className={styles.modalTitle}>Reset Password — {user.display_name}</h3>
          <button className={styles.modalCloseBtn} onClick={onClose} type="button"><XSmallIcon /></button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className={styles.modalBody}>
            {error && <div className={styles.formError}>{error}</div>}

            <div className={styles.formGroup}>
              <label className={styles.formLabel}>New Password <span className={styles.required}>*</span></label>
              <input type="password" className={styles.formInput} value={newPassword} onChange={e => setNewPassword(e.target.value)} autoComplete="new-password" autoFocus />
              <span className={styles.formHint}>Minimum 8 characters.</span>
            </div>

            <div className={styles.formGroup}>
              <label className={styles.formLabel}>Confirm Password <span className={styles.required}>*</span></label>
              <input type="password" className={styles.formInput} value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)} autoComplete="new-password" />
            </div>

            <label className={styles.checkLabel}>
              <input type="checkbox" checked={mustChange} onChange={e => setMustChange(e.target.checked)} />
              Require password change on next login
            </label>
          </div>

          <div className={styles.modalFooter}>
            <button type="submit" className={styles.btnPrimary} disabled={saving}>
              {saving ? 'Resetting…' : 'Reset Password'}
            </button>
            <button type="button" className={styles.btnSecondary} onClick={onClose} disabled={saving}>
              Cancel
            </button>
          </div>
        </form>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title="Reset Password"
        message={`Reset password for ${user.display_name}?`}
        confirmLabel="Reset Password"
        variant="danger"
        loading={saving}
        onConfirm={doReset}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}

// ─── Assign Role Modal ────────────────────────────────────────────────────────

function AssignRoleModal({
  user,
  roles,
  branches,
  onClose,
  onAssigned,
}: {
  user: UserAdmin;
  roles: Role[];
  branches: Branch[];
  onClose: () => void;
  onAssigned: () => Promise<void>;
}) {
  const [roleId,    setRoleId]    = useState<number | ''>(roles[0]?.role_id ?? '');
  const [scopeType, setScopeType] = useState<'AllCompanyBranches' | 'SpecificBranch' | 'OwnDriverDataOnly'>('AllCompanyBranches');
  const [branchId,  setBranchId]  = useState<number | ''>(branches[0]?.branch_id ?? '');
  const [notes,     setNotes]     = useState('');
  const [saving,    setSaving]    = useState(false);
  const [error,     setError]     = useState('');

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!roleId) { setError('Please select a role.'); return; }
    if (scopeType !== 'AllCompanyBranches' && !branchId) { setError('Please select a branch.'); return; }
    setSaving(true);
    setError('');
    try {
      await apiClient.post<RoleAssignment>(
        `/admin/users/${user.user_id}/roles`,
        {
          role_id:    Number(roleId),
          scope_type: scopeType,
          branch_id:  scopeType !== 'AllCompanyBranches' ? Number(branchId) : null,
          notes:      notes.trim() || null,
        } as RoleAssignmentCreate,
      );
      await onAssigned();
    } catch (e) {
      const status = (e as { response?: { status?: number } })?.response?.status;
      if (status === 422) {
        const msg = apiError(e);
        // Check for duplicate / unique constraint messages
        if (/duplicate|already|unique/i.test(msg)) {
          setError('This role + scope + branch combination is already assigned to this user.');
        } else {
          setError(msg);
        }
      } else {
        setError(apiError(e));
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className={styles.modalOverlay}
      onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}
      role="dialog"
      aria-modal="true"
    >
      <div className={styles.modal}>
        <div className={styles.modalHeader}>
          <h3 className={styles.modalTitle}>Assign Role — {user.display_name}</h3>
          <button className={styles.modalCloseBtn} onClick={onClose} type="button"><XSmallIcon /></button>
        </div>
        <form onSubmit={handleSubmit}>
          <div className={styles.modalBody}>
            {error && <div className={styles.formError}>{error}</div>}

            <div className={styles.infoNote}>
              <InfoIcon />
              To change an existing role's scope or branch, revoke it first then assign a new one.
            </div>

            <div className={styles.formGroup}>
              <label className={styles.formLabel}>Role <span className={styles.required}>*</span></label>
              <select className={styles.formSelect} value={roleId} onChange={e => setRoleId(Number(e.target.value))}>
                <option value="" disabled>— Select role —</option>
                {roles.map(r => (
                  <option key={r.role_id} value={r.role_id}>
                    {r.role_name} ({r.role_code})
                  </option>
                ))}
              </select>
            </div>

            <div className={styles.formGroup}>
              <label className={styles.formLabel}>Scope <span className={styles.required}>*</span></label>
              <select className={styles.formSelect} value={scopeType} onChange={e => setScopeType(e.target.value as typeof scopeType)}>
                <option value="AllCompanyBranches">All Company Branches</option>
                <option value="SpecificBranch">Specific Branch</option>
                <option value="OwnDriverDataOnly">Own Driver Data Only</option>
              </select>
            </div>

            {scopeType !== 'AllCompanyBranches' && (
              <div className={styles.formGroup}>
                <label className={styles.formLabel}>Branch <span className={styles.required}>*</span></label>
                <select className={styles.formSelect} value={branchId} onChange={e => setBranchId(Number(e.target.value))}>
                  <option value="" disabled>— Select branch —</option>
                  {branches.map(b => (
                    <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>
                  ))}
                </select>
              </div>
            )}

            <div className={styles.formGroup}>
              <label className={styles.formLabel}>Notes (optional)</label>
              <textarea
                className={styles.formTextarea}
                value={notes}
                onChange={e => setNotes(e.target.value)}
                rows={2}
                placeholder="e.g. temporary access until Q3"
              />
            </div>
          </div>

          <div className={styles.modalFooter}>
            <button type="submit" className={styles.btnPrimary} disabled={saving}>
              {saving ? 'Assigning…' : 'Assign Role'}
            </button>
            <button type="button" className={styles.btnSecondary} onClick={onClose} disabled={saving}>
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Icons ────────────────────────────────────────────────────────────────────

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
      <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
    </svg>
  );
}

function XSmallIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
      <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
    </svg>
  );
}

function SearchIcon({ className }: { className?: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" className={className} aria-hidden="true">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
  );
}

function EmptyUsersIcon() {
  return (
    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"
      style={{ color: '#cbd5e1' }} aria-hidden="true">
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
      <circle cx="9" cy="7" r="4"/>
      <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
      <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
    </svg>
  );
}

function LockBigIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
      style={{ color: '#9ca3af' }} aria-hidden="true">
      <rect x="5" y="11" width="14" height="10" rx="2"/>
      <path d="M8 11V7a4 4 0 0 1 8 0v4"/>
    </svg>
  );
}

function InfoIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <circle cx="12" cy="12" r="10"/>
      <line x1="12" y1="8" x2="12" y2="8"/><line x1="12" y1="12" x2="12" y2="16"/>
    </svg>
  );
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round"
      style={{ transition: 'transform 0.15s', transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)' }}
      aria-hidden="true">
      <polyline points="9 18 15 12 9 6"/>
    </svg>
  );
}
