import { useEffect, useReducer, useState, useCallback } from 'react';
import type { FormEvent } from 'react';
import apiClient from '../../../lib/apiClient';
import { friendlyPermLabel } from '../../../lib/permissionLabels';
import { useAuth } from '../../../store/authStore';
import { canManageRoles, canCreateRoles, canEditRoles, canDeleteRoles } from '../../../lib/permissions';
import type {
  CompanyRole,
  CompanyRoleCreate,
  CompanyRolePermissions,
  CompanyRoleUser,
  Permission,
} from '../../../types/admin';
import styles from './RolesPage.module.css';

// ─── Permission grouping & metadata ──────────────────────────────────────────

type BusinessGroup = {
  id: string;
  label: string;
  icon: React.ReactNode;
  codes: readonly string[];
};

// Business-domain groups — order and membership are explicit, independent of API module_code
const BUSINESS_GROUPS: BusinessGroup[] = [
  {
    id: 'org-admin',
    label: 'Organization Administration',
    icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/><rect x="13" y="13" width="3" height="3"/><rect x="13" y="6" width="3" height="3"/><rect x="6" y="13" width="3" height="3"/><rect x="6" y="6" width="3" height="3"/></svg>,
    codes: ['company.view', 'company.edit', 'branches.view', 'branches.create', 'branches.edit', 'users.view', 'users.create', 'users.edit', 'users.deactivate', 'roles.view', 'roles.create', 'roles.edit', 'roles.delete'],
  },
  {
    id: 'payroll-ops',
    label: 'Payroll Operations',
    icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>,
    codes: ['payroll.view', 'payroll.period.create', 'payroll.entry', 'payroll.approve', 'review.decide', 'payroll.finalize'],
  },
  {
    id: 'payroll-config',
    label: 'Payroll Configuration',
    icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>,
    codes: ['settings.view', 'settings.manage', 'setup.manage', 'payitems.view', 'payitems.edit', 'payrates.view', 'payrates.edit'],
  },
  {
    id: 'workforce',
    label: 'Workforce',
    icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="1" y="3" width="15" height="13" rx="2"/><path d="M16 8h4l3 3v5h-7V8z"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/></svg>,
    codes: ['drivers.view', 'drivers.create', 'drivers.edit'],
  },
  {
    id: 'reporting',
    label: 'Reporting',
    icon: <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>,
    codes: ['reports.view'],
  },
];

// Permissions excluded from the UI — preserved unchanged on every save via permsSt.codes
const HIDDEN_PERM_CODES = new Set(['dispatch.view', 'dispatch.edit']);

// Risk tiers — visual only, no enforcement
type RiskTier = 'readonly' | 'write' | 'approval' | 'critical';

const RISK_LABELS: Record<RiskTier, string> = {
  readonly: 'Read',
  write:    'Write',
  approval: 'Approval',
  critical: 'Critical',
};

const RISK_CLASS: Record<RiskTier, string> = {
  readonly: styles.riskReadOnly,
  write:    styles.riskWrite,
  approval: styles.riskApproval,
  critical: styles.riskCritical,
};

function getRiskTier(code: string): RiskTier {
  if (code === 'payroll.finalize' || code === 'users.deactivate' || code === 'roles.delete') return 'critical';
  if (code === 'payroll.approve' || code === 'review.decide' || code === 'roles.edit' || code === 'company.edit') return 'approval';
  if (code.endsWith('.view')) return 'readonly';
  return 'write';
}

// Child → required parent permission (for dependency enforcement)
const PERM_DEPS: Record<string, string> = {
  'company.edit':          'company.view',
  'branches.create':       'branches.view',
  'branches.edit':         'branches.view',
  'roles.create':          'roles.view',
  'roles.edit':            'roles.view',
  'roles.delete':          'roles.view',
  'users.create':          'users.view',
  'users.edit':            'users.view',
  'users.deactivate':      'users.view',
  'payroll.edit':          'payroll.view',
  'payroll.approve':       'payroll.view',
  'payroll.finalize':      'payroll.view',
  'payroll.entry':         'payroll.view',
  'payroll.period.create': 'payroll.view',
  'review.decide':         'payroll.view',
  'payitems.edit':         'payitems.view',
  'payrates.edit':         'payrates.view',
  'drivers.create':        'drivers.view',
  'drivers.edit':          'drivers.view',
  'dispatch.edit':         'dispatch.view',
  'settings.manage':       'settings.view',
};

// Parent → children that depend on it (only visible codes are toggled — hidden dispatch.* are safe)
const PERM_DEPENDENTS: Record<string, string[]> = {};
for (const [child, parent] of Object.entries(PERM_DEPS)) {
  if (HIDDEN_PERM_CODES.has(child)) continue; // dispatch cascade never triggered from visible UI
  if (!PERM_DEPENDENTS[parent]) PERM_DEPENDENTS[parent] = [];
  PERM_DEPENDENTS[parent].push(child);
}

// ─── State / reducers ─────────────────────────────────────────────────────────

type RolesState = { roles: CompanyRole[]; loading: boolean; error: string };
type RolesAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; roles: CompanyRole[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'ROLE_ADDED'; role: CompanyRole }
  | { type: 'ROLE_DELETED'; id: number }
  | { type: 'ROLE_RENAMED'; id: number; name: string };

function rolesReducer(s: RolesState, a: RolesAction): RolesState {
  switch (a.type) {
    case 'FETCH_START':  return { roles: [], loading: true,  error: '' };
    case 'FETCH_OK':     return { roles: a.roles, loading: false, error: '' };
    case 'FETCH_ERROR':  return { roles: [], loading: false, error: a.error };
    case 'ROLE_ADDED':   return { ...s, roles: [...s.roles, a.role] };
    case 'ROLE_DELETED': return { ...s, roles: s.roles.filter(r => r.company_role_id !== a.id) };
    case 'ROLE_RENAMED': return { ...s, roles: s.roles.map(r => r.company_role_id === a.id ? { ...r, role_name: a.name } : r) };
    default:             return s;
  }
}

type PermsState = { codes: Set<string>; savedCodes: Set<string>; loading: boolean; error: string; dirty: boolean };
type PermsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; codes: string[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'TOGGLE'; code: string }
  | { type: 'SET_MANY'; codes: string[]; enable: boolean }
  | { type: 'RESET_DIRTY' };

function setsEqual(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false;
  for (const v of a) if (!b.has(v)) return false;
  return true;
}

function permsReducer(s: PermsState, a: PermsAction): PermsState {
  switch (a.type) {
    case 'FETCH_START':   return { codes: new Set<string>(), savedCodes: new Set<string>(), loading: true, error: '', dirty: false };
    case 'FETCH_OK': {
      const codes = new Set<string>(a.codes);
      return { codes, savedCodes: codes, loading: false, error: '', dirty: false };
    }
    case 'FETCH_ERROR':   return { codes: new Set<string>(), savedCodes: new Set<string>(), loading: false, error: a.error, dirty: false };
    case 'TOGGLE': {
      const next = new Set<string>(s.codes);
      if (next.has(a.code)) {
        // Disabling: cascade-disable all dependents
        next.delete(a.code);
        for (const dep of PERM_DEPENDENTS[a.code] ?? []) {
          next.delete(dep);
        }
      } else {
        // Enabling: auto-enable required parent
        next.add(a.code);
        const parent = PERM_DEPS[a.code];
        if (parent) next.add(parent);
      }
      return { ...s, codes: next, dirty: !setsEqual(next, s.savedCodes) };
    }
    case 'SET_MANY': {
      const next = new Set<string>(s.codes);
      if (a.enable) {
        for (const code of a.codes) {
          next.add(code);
          const parent = PERM_DEPS[code];
          if (parent) next.add(parent);
        }
      } else {
        for (const code of a.codes) {
          next.delete(code);
          for (const dep of PERM_DEPENDENTS[code] ?? []) next.delete(dep);
        }
      }
      return { ...s, codes: next, dirty: !setsEqual(next, s.savedCodes) };
    }
    case 'RESET_DIRTY':   return { ...s, savedCodes: s.codes, dirty: false };
    default:              return s;
  }
}

type UsersModalState = {
  roleId: number | null;
  roleName: string;
  loading: boolean;
  users: CompanyRoleUser[];
  error: string;
};

type UsersModalAction =
  | { type: 'OPEN'; roleId: number; roleName: string }
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; users: CompanyRoleUser[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'CLOSE' };

function usersModalReducer(s: UsersModalState, a: UsersModalAction): UsersModalState {
  switch (a.type) {
    case 'OPEN':        return { roleId: a.roleId, roleName: a.roleName, loading: true, users: [], error: '' };
    case 'FETCH_START': return { ...s, loading: true, error: '' };
    case 'FETCH_OK':    return { ...s, loading: false, users: a.users };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
    case 'CLOSE':       return { roleId: null, roleName: '', loading: false, users: [], error: '' };
    default:            return s;
  }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d)) return d.map((i: unknown) => (i as { msg?: string })?.msg ?? '').filter(Boolean).join(' ');
  if (d && typeof d === 'object' && 'message' in d) return String((d as { message: unknown }).message);
  return 'An unexpected error occurred.';
}

function groupPermissionsByDomain(perms: Permission[]): [BusinessGroup, Permission[]][] {
  const byCode = new Map(perms.map(p => [p.permission_code, p]));
  return BUSINESS_GROUPS
    .map(group => {
      const grouped = group.codes
        .map(code => byCode.get(code))
        .filter((p): p is Permission => p !== undefined);
      return [group, grouped] as [BusinessGroup, Permission[]];
    })
    .filter(([, grouped]) => grouped.length > 0);
}

// ─── Main component ───────────────────────────────────────────────────────────

export function RolesPage() {
  const { user } = useAuth();

  const isAdmin     = !!user && canManageRoles(user);
  const canCreate   = !!user && canCreateRoles(user);
  const canEdit     = !!user && canEditRoles(user);
  const canDelete   = !!user && canDeleteRoles(user);
  const readOnly    = isAdmin && !canEdit;

  const [rolesSt, dispatchRoles]     = useReducer(rolesReducer, { roles: [], loading: true, error: '' });
  const [allPerms, setAllPerms]       = useState<Permission[]>([]);
  const [selectedId, setSelectedId]   = useState<number | null>(null);
  const [permsSt, dispatchPerms]      = useReducer(permsReducer, { codes: new Set<string>(), savedCodes: new Set<string>(), loading: false, error: '', dirty: false });
  const [usersModal, dispatchUsers]   = useReducer(usersModalReducer, { roleId: null, roleName: '', loading: false, users: [], error: '' });

  const [saving, setSaving]           = useState(false);
  const [saveError, setSaveError]     = useState('');
  const [saveOk, setSaveOk]           = useState(false);

  const [createOpen, setCreateOpen]   = useState(false);
  const [createName, setCreateName]   = useState('');
  const [createSaving, setCreateSaving] = useState(false);
  const [createError, setCreateError] = useState('');

  const [deleteId, setDeleteId]       = useState<number | null>(null);
  const [deleteWorking, setDeleteWorking] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [successInfo, setSuccessInfo]  = useState<{ title: string; sub: string } | null>(null);

  const [renameId, setRenameId]       = useState<number | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
  const [renameSaving, setRenameSaving] = useState(false);
  const [renameError, setRenameError] = useState('');

  const [roleSearch, setRoleSearch]   = useState('');
  const [roleFilter, setRoleFilter]   = useState<'all' | 'default' | 'custom'>('all');

  const showToast = useCallback((title: string, sub = '') => {
    setSuccessInfo({ title, sub });
  }, []);

  // ── Load roles + all UI-visible permissions ───────────────────────────────
  useEffect(() => {
    dispatchRoles({ type: 'FETCH_START' });
    Promise.all([
      apiClient.get<CompanyRole[]>('/admin/company-roles'),
      apiClient.get<Permission[]>('/admin/permissions'),   // ui_only=true by default
    ])
      .then(([rolesRes, permsRes]) => {
        dispatchRoles({ type: 'FETCH_OK', roles: rolesRes.data });
        setAllPerms(permsRes.data);
      })
      .catch(e => dispatchRoles({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, []);

  // ── Load permissions for selected role ────────────────────────────────────
  useEffect(() => {
    if (selectedId === null) return;
    dispatchPerms({ type: 'FETCH_START' });
    apiClient.get<CompanyRolePermissions>(`/admin/company-roles/${selectedId}/permissions`)
      .then(({ data }) => dispatchPerms({ type: 'FETCH_OK', codes: data.permission_codes }))
      .catch(e => dispatchPerms({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, [selectedId]);

  // ── Load users for users modal ─────────────────────────────────────────────
  useEffect(() => {
    if (usersModal.roleId === null) return;
    dispatchUsers({ type: 'FETCH_START' });
    apiClient.get<CompanyRoleUser[]>(`/admin/company-roles/${usersModal.roleId}/users`)
      .then(({ data }) => dispatchUsers({ type: 'FETCH_OK', users: data }))
      .catch(e => dispatchUsers({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, [usersModal.roleId]);

  const selectedRole   = rolesSt.roles.find(r => r.company_role_id === selectedId) ?? null;
  const groupedDomains = groupPermissionsByDomain(allPerms);
  const isOwner        = selectedRole?.role_code === 'COMPANY_OWNER';

  // ── Save permissions ───────────────────────────────────────────────────────
  async function savePermissions() {
    if (!selectedId || !permsSt.dirty || isOwner) return;
    setSaving(true);
    setSaveError('');
    setSaveOk(false);
    try {
      await apiClient.put(`/admin/company-roles/${selectedId}/permissions`, {
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

  // ── Create role ────────────────────────────────────────────────────────────
  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setCreateSaving(true);
    setCreateError('');
    const payload: CompanyRoleCreate = {
      role_name: createName.trim(),
      notes: null,
    };
    try {
      const { data } = await apiClient.post<CompanyRole>('/admin/company-roles', payload);
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

  // ── Delete role ────────────────────────────────────────────────────────────
  async function confirmDelete() {
    if (!deleteId) return;
    setDeleteWorking(true);
    setDeleteError('');
    try {
      await apiClient.delete(`/admin/company-roles/${deleteId}`);
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

  // ── Rename role ────────────────────────────────────────────────────────────
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
      const { data } = await apiClient.patch<CompanyRole>(`/admin/company-roles/${renameId}`, {
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

  // ─────────────────────────────────────────────────────────────────────────
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

  const defaultRoles = rolesSt.roles.filter(r => r.is_default || r.is_protected);
  const customRoles  = rolesSt.roles.filter(r => !r.is_default && !r.is_protected);

  const searchLower  = roleSearch.toLowerCase().trim();
  const matchSearch  = (name: string) => !searchLower || name.toLowerCase().includes(searchLower);
  const filteredDefaultRoles = roleFilter !== 'custom' ? defaultRoles.filter(r => matchSearch(r.role_name)) : [];
  const filteredCustomRoles  = roleFilter !== 'default' ? customRoles.filter(r => matchSearch(r.role_name))  : [];
  const hasSearchResults = filteredDefaultRoles.length > 0 || filteredCustomRoles.length > 0;

  return (
    <div className={styles.page}>

      {/* ── Success dialog ── */}
      {successInfo && (
        <div className={styles.modalOverlay} onClick={() => setSuccessInfo(null)}>
          <div className={styles.successDialog} onClick={e => e.stopPropagation()}>
            <div className={styles.successIconWrap}>
              <svg width="36" height="36" viewBox="0 0 24 24" fill="none" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" stroke="#bbf7d0" strokeWidth="1.5" fill="#f0fdf4" />
                <polyline points="20 6 9 17 4 12" stroke="#16a34a" strokeWidth="2.5" />
              </svg>
            </div>
            <h2 className={styles.successTitle}>{successInfo.title}</h2>
            {successInfo.sub && <p className={styles.successSub}>{successInfo.sub}</p>}
            <button className={styles.btnPrimary} onClick={() => setSuccessInfo(null)}>Done</button>
          </div>
        </div>
      )}

      {/* ── Body ── */}
      <div className={styles.body}>

        {/* ── LEFT: Roles list ── */}
        <div className={styles.rolesPanel}>
          <div className={styles.rolesPanelHeader}>
            <p className={styles.rolesPanelTitle}>Company Roles</p>
          </div>

          {/* Search + filter chips */}
          {!rolesSt.loading && !rolesSt.error && (
            <div className={styles.rolesControls}>
              <div className={styles.searchWrap}>
                <span className={styles.searchIcon}><SearchIcon /></span>
                <input
                  className={styles.searchInput}
                  type="text"
                  placeholder="Search roles..."
                  value={roleSearch}
                  onChange={e => setRoleSearch(e.target.value)}
                />
                {roleSearch && (
                  <button className={styles.searchClearBtn} onClick={() => setRoleSearch('')} title="Clear search">
                    <CloseIcon />
                  </button>
                )}
              </div>
              <div className={styles.filterChips}>
                {(['all', 'default', 'custom'] as const).map(f => (
                  <button
                    key={f}
                    className={`${styles.filterChip}${roleFilter === f ? ` ${styles.filterChipActive}` : ''}`}
                    onClick={() => setRoleFilter(f)}
                  >
                    {f === 'all' ? 'All' : f === 'default' ? 'Default' : 'Custom'}
                    <span className={styles.filterChipCount}>
                      {f === 'all' ? defaultRoles.length + customRoles.length : f === 'default' ? defaultRoles.length : customRoles.length}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className={styles.rolesList}>
            {rolesSt.loading && (
              <div style={{ padding: '1.5rem 1rem', display: 'flex', flexDirection: 'column', gap: '0.6rem' }}>
                {[...Array(4)].map((_, i) => <div key={i} className={styles.skeleton} style={{ width: `${[80, 65, 70, 55][i]}%` }} />)}
              </div>
            )}

            {!rolesSt.loading && rolesSt.error && (
              <div style={{ padding: '1rem', fontSize: '0.8rem', color: '#b91c1c' }}>{rolesSt.error}</div>
            )}

            {/* No-results empty state */}
            {!rolesSt.loading && !rolesSt.error && !hasSearchResults && (searchLower !== '' || roleFilter !== 'all') && (
              <div className={styles.searchEmpty}>
                <p className={styles.searchEmptyTitle}>No roles found</p>
                <p className={styles.searchEmptyText}>Try a different search.</p>
              </div>
            )}

            {/* Default / protected roles */}
            {!rolesSt.loading && !rolesSt.error && filteredDefaultRoles.length > 0 && (
              <>
                <div className={styles.roleSectionHeader}>Default Roles</div>
                {filteredDefaultRoles.map(role => (
                  <RoleRow
                    key={role.company_role_id}
                    role={role}
                    selected={selectedId === role.company_role_id}
                    onSelect={() => {
                      cancelRename();
                      setSelectedId(role.company_role_id);
                      setSaveError(''); setSaveOk(false);
                    }}
                    onDelete={null}
                    onShowUsers={() => dispatchUsers({
                      type: 'OPEN',
                      roleId: role.company_role_id,
                      roleName: role.role_name,
                    })}
                    onRenameStart={null}
                    isRenaming={false}
                    renameDraft=""
                    renameSaving={false}
                    renameError=""
                    onRenameDraftChange={() => {}}
                    onRenameCommit={() => {}}
                    onRenameCancel={() => {}}
                  />
                ))}
              </>
            )}

            {/* Custom roles */}
            {!rolesSt.loading && !rolesSt.error && roleFilter !== 'default' && (
              <>
                {filteredCustomRoles.length > 0 && (
                  <>
                    <div className={styles.roleSectionHeader}>Custom Roles</div>
                    {filteredCustomRoles.map(role => (
                      <RoleRow
                        key={role.company_role_id}
                        role={role}
                        selected={selectedId === role.company_role_id}
                        onSelect={() => {
                          cancelRename();
                          setSelectedId(role.company_role_id);
                          setSaveError(''); setSaveOk(false);
                        }}
                        onDelete={canDelete ? () => { cancelRename(); setDeleteId(role.company_role_id); setDeleteError(''); } : null}
                        onShowUsers={() => dispatchUsers({
                          type: 'OPEN',
                          roleId: role.company_role_id,
                          roleName: role.role_name,
                        })}
                        onRenameStart={canEdit ? () => startRename(role) : null}
                        isRenaming={renameId === role.company_role_id}
                        renameDraft={renameDraft}
                        renameSaving={renameSaving}
                        renameError={renameError}
                        onRenameDraftChange={setRenameDraft}
                        onRenameCommit={() => { void commitRename(); }}
                        onRenameCancel={cancelRename}
                      />
                    ))}
                  </>
                )}
                {filteredCustomRoles.length === 0 && !searchLower && (
                  <div style={{ padding: '0.75rem 1rem', fontSize: '0.8rem', color: '#94a3b8', fontStyle: 'italic' }}>
                    No custom roles yet.
                  </div>
                )}
              </>
            )}
          </div>

          {/* Add Role button — fixed at bottom, outside the scroll area */}
          {!rolesSt.loading && canCreate && (
            <div className={styles.addRoleBtnWrap}>
              <button className={styles.addRoleBtn} onClick={() => { setCreateOpen(true); setCreateError(''); }}>
                <PlusIcon /> Add Role
              </button>
            </div>
          )}
        </div>

        {/* ── RIGHT: Permissions panel ── */}
        <div className={styles.permPanel}>
          {!selectedRole ? (
            <div className={styles.permEmpty}>
              <div className={styles.permEmptyIconWrap}>
                <ShieldIcon size={32} />
              </div>
              <p className={styles.permEmptyTitle}>Select a role</p>
              <p className={styles.permEmptyText}>
                Choose a role on the left to view and manage its permissions.
              </p>
              {/* Decorative permission group skeletons */}
              <div className={styles.permEmptyPreview}>
                <div className={styles.permEmptyGroup}>
                  <div className={styles.permEmptyGroupLabel} />
                  <div className={styles.permEmptyRow}><div className={styles.permEmptyCheck} /><div className={styles.permEmptyLine} style={{ width: '62%' }} /></div>
                  <div className={styles.permEmptyRow}><div className={styles.permEmptyCheck} /><div className={styles.permEmptyLine} style={{ width: '48%' }} /></div>
                  <div className={styles.permEmptyRow}><div className={styles.permEmptyCheck} /><div className={styles.permEmptyLine} style={{ width: '55%' }} /></div>
                </div>
                <div className={styles.permEmptyGroup}>
                  <div className={styles.permEmptyGroupLabel} />
                  <div className={styles.permEmptyRow}><div className={styles.permEmptyCheck} /><div className={styles.permEmptyLine} style={{ width: '70%' }} /></div>
                  <div className={styles.permEmptyRow}><div className={styles.permEmptyCheck} /><div className={styles.permEmptyLine} style={{ width: '40%' }} /></div>
                </div>
                <div className={styles.permEmptyGroup}>
                  <div className={styles.permEmptyGroupLabel} />
                  <div className={styles.permEmptyRow}><div className={styles.permEmptyCheck} /><div className={styles.permEmptyLine} style={{ width: '52%' }} /></div>
                  <div className={styles.permEmptyRow}><div className={styles.permEmptyCheck} /><div className={styles.permEmptyLine} style={{ width: '44%' }} /></div>
                </div>
              </div>
            </div>
          ) : (
            <>
              {/* Header */}
              <div className={styles.permHeader}>
                <h2 className={styles.permRoleName}>{selectedRole.role_name}</h2>
                <div className={styles.permBadgeRow}>
                  {selectedRole.is_protected && (
                    <span className={`${styles.roleBadge} ${styles.badgeProtected}`}>
                      <LockSmallIcon /> Protected
                    </span>
                  )}
                  {selectedRole.is_default && !selectedRole.is_protected && (
                    <span className={`${styles.roleBadge} ${styles.badgeDefault}`}>Default</span>
                  )}
                  {selectedRole.is_custom && (
                    <span className={`${styles.roleBadge} ${styles.badgeCustom}`}>Custom</span>
                  )}
                  <button
                    className={styles.userCountBadge}
                    onClick={() => dispatchUsers({
                      type: 'OPEN',
                      roleId: selectedRole.company_role_id,
                      roleName: selectedRole.role_name,
                    })}
                  >
                    <UsersSmallIcon />
                    {selectedRole.user_count} {selectedRole.user_count === 1 ? 'user' : 'users'}
                  </button>
                </div>
              </div>

              {/* Company Owner: fully read-only banner */}
              {isOwner ? (
                <>
                  <div className={styles.ownerBanner}>
                    <ShieldCheckIcon />
                    <div>
                      <strong>Company Owner</strong> is protected and always has full access
                      to all permissions. Permissions cannot be modified for this role.
                    </div>
                  </div>

                  {/* Read-only permissions display */}
                  <div className={styles.permBody}>
                    {permsSt.loading && (
                      <div style={{ padding: '1rem 0', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                        {[...Array(6)].map((_, i) => <div key={i} className={styles.skeleton} style={{ width: `${[70, 55, 65, 50, 60, 45][i]}%` }} />)}
                      </div>
                    )}
                    {!permsSt.loading && !permsSt.error && groupedDomains.map(([group, perms]) => (
                      <div key={group.id} className={styles.permGroup}>
                        <div className={styles.permGroupTitle}>
                          <span className={styles.permGroupIcon}>{group.icon}</span>
                          {group.label}
                        </div>
                        {perms.map(perm => (
                          <div key={perm.permission_code} className={`${styles.permRow} ${styles.permRowReadOnly}`}>
                            <CheckboxCheckedReadOnly />
                            <label className={styles.permLabel}>{perm.permission_name}</label>
                            <span className={`${styles.riskBadge} ${RISK_CLASS[getRiskTier(perm.permission_code)]}`}>
                              {RISK_LABELS[getRiskTier(perm.permission_code)]}
                            </span>
                          </div>
                        ))}
                      </div>
                    ))}
                  </div>
                  {/* No footer for Company Owner — permissions are immutable */}
                </>
              ) : (
                <>
                  {/* Protected role (Driver) warning */}
                  {selectedRole.is_protected && (
                    <div className={styles.protectedBanner}>
                      <WarnIcon />
                      <span>
                        <strong>{selectedRole.role_name}</strong> is a protected default role.
                        You can adjust its permissions but it cannot be deleted.
                      </span>
                    </div>
                  )}

                  {/* Permissions body */}
                  <div className={styles.permBody}>
                    {permsSt.loading && (
                      <div style={{ padding: '1rem 0', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                        {[...Array(6)].map((_, i) => <div key={i} className={styles.skeleton} style={{ width: `${[70, 55, 65, 50, 60, 45][i]}%` }} />)}
                      </div>
                    )}
                    {!permsSt.loading && permsSt.error && (
                      <div className={styles.errorAlert} style={{ marginTop: '0.75rem' }}>
                        <AlertIcon /> {permsSt.error}
                      </div>
                    )}
                    {readOnly && (
                      <div style={{ padding: '0.5rem 0.25rem 0.75rem', fontSize: '0.8rem', color: '#6b7280', display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                        <LockSmallIcon /> You have view-only access. Editing permissions requires <code>roles.edit</code>.
                      </div>
                    )}
                    {!permsSt.loading && !permsSt.error && groupedDomains.map(([group, perms]) => {
                      const groupCodes   = perms.map(p => p.permission_code);
                      const checkedCount = groupCodes.filter(c => permsSt.codes.has(c)).length;
                      const allChecked   = checkedCount === groupCodes.length;
                      const someChecked  = checkedCount > 0 && !allChecked;
                      return (
                      <div key={group.id} className={styles.permGroup}>
                        <div className={styles.permGroupTitle}>
                          <span className={styles.permGroupIcon}>{group.icon}</span>
                          {group.label}
                          {!readOnly && (
                            <input
                              type="checkbox"
                              className={styles.groupSelectAll}
                              title={allChecked ? 'Deselect all' : 'Select all'}
                              ref={el => { if (el) el.indeterminate = someChecked; }}
                              checked={allChecked}
                              onChange={() => {
                                dispatchPerms({ type: 'SET_MANY', codes: groupCodes, enable: !allChecked });
                                setSaveError(''); setSaveOk(false);
                              }}
                            />
                          )}
                        </div>
                        {perms.map(perm => {
                          const checked   = permsSt.codes.has(perm.permission_code);
                          const parent    = PERM_DEPS[perm.permission_code];
                          const parentOff = parent ? !permsSt.codes.has(parent) : false;
                          return (
                            <div
                              key={perm.permission_code}
                              className={`${styles.permRow}${parentOff ? ` ${styles.permRowDimmed}` : ''}`}
                            >
                              <input
                                type="checkbox"
                                className={styles.permCheckboxHidden}
                                id={`perm-${perm.permission_code}`}
                                checked={checked}
                                disabled={readOnly}
                                onChange={() => {
                                  if (readOnly) return;
                                  dispatchPerms({ type: 'TOGGLE', code: perm.permission_code });
                                  setSaveError('');
                                  setSaveOk(false);
                                }}
                              />
                              <label
                                htmlFor={`perm-${perm.permission_code}`}
                                className={`${styles.permRowLabel}${readOnly ? ` ${styles.permRowLabelReadOnly}` : ''}`}
                              >
                                <span className={`${styles.customCheckbox}${checked ? ` ${styles.customCheckboxChecked}` : ''}`}>
                                  {checked && (
                                    <svg width="9" height="9" viewBox="0 0 9 9" fill="none" aria-hidden="true">
                                      <path d="M1.5 4.5l2 2 3.5-3.5" stroke="white" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"/>
                                    </svg>
                                  )}
                                </span>
                                {perm.permission_name}
                              </label>
                              <span className={`${styles.riskBadge} ${RISK_CLASS[getRiskTier(perm.permission_code)]}`}>
                                {RISK_LABELS[getRiskTier(perm.permission_code)]}
                              </span>
                              {parentOff && (
                                <span className={styles.permDepNote}>
                                  requires {allPerms.find(p => p.permission_code === parent)?.permission_name ?? friendlyPermLabel(parent)}
                                </span>
                              )}
                            </div>
                          );
                        })}
                      </div>
                      );
                    })}
                  </div>

                  {/* Footer — only shown when user can edit permissions */}
                  {canEdit && (
                    <div className={styles.permFooter}>
                      {saveError && (
                        <div className={styles.errorAlert} style={{ flex: 1, marginRight: '0.5rem' }}>
                          <AlertIcon /> {saveError}
                        </div>
                      )}
                      {!saveError && saveOk && (
                        <span style={{ flex: 1, fontSize: '0.8rem', color: '#15803d', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '0.35rem' }}>
                          <CheckIcon /> Saved
                        </span>
                      )}
                      {!saveError && !saveOk && (
                        <span className={styles.saveStatus}>
                          {permsSt.dirty ? 'Unsaved changes' : 'Up to date'}
                        </span>
                      )}
                      <button
                        className={styles.btnPrimary}
                        onClick={() => void savePermissions()}
                        disabled={saving || !permsSt.dirty}
                      >
                        {saving ? <><SpinnerIcon /> Saving…</> : 'Save Changes'}
                      </button>
                      {permsSt.dirty && (
                        <button
                          className={styles.btnSecondary}
                          onClick={() => {
                            if (selectedId !== null) {
                              dispatchPerms({ type: 'FETCH_START' });
                              apiClient.get<CompanyRolePermissions>(`/admin/company-roles/${selectedId}/permissions`)
                                .then(({ data }) => dispatchPerms({ type: 'FETCH_OK', codes: data.permission_codes }))
                                .catch(e => dispatchPerms({ type: 'FETCH_ERROR', error: apiError(e) }));
                            }
                            setSaveError('');
                          }}
                          disabled={saving}
                        >
                          Discard
                        </button>
                      )}
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </div>
      </div>

      {/* ── Create Role Modal ── */}
      {createOpen && (
        <div className={styles.modalOverlay}
          onClick={e => { if (e.target === e.currentTarget && !createSaving) setCreateOpen(false); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>Add Custom Role</h2>
              <button className={styles.modalCloseBtn}
                onClick={() => setCreateOpen(false)} disabled={createSaving}>
                <CloseIcon />
              </button>
            </div>

            {/* Visual band */}
            <div className={styles.createModalBand}>
              <div className={styles.createIconCircle}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
                  stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
                  <line x1="12" y1="8" x2="12" y2="16"/>
                  <line x1="8" y1="12" x2="16" y2="12"/>
                </svg>
              </div>
              <div className={styles.createBandText}>
                <p className={styles.createBandTitle}>New custom role</p>
                <p className={styles.createBandSub}>Start with a name — you'll set permissions after.</p>
              </div>
            </div>

            <form onSubmit={e => { void handleCreate(e); }}>
              <div className={styles.modalBody}>
                <div className={styles.formGroup}>
                  <label className={styles.label}>Role Name <span style={{ color: '#ef4444' }}>*</span></label>
                  <input
                    className={styles.input}
                    autoFocus
                    value={createName}
                    onChange={e => setCreateName(e.target.value)}
                    placeholder="e.g. Payroll Clerk"
                    maxLength={120}
                    required
                  />
                </div>
                <div className={styles.createHint}>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
                    stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                    style={{ flexShrink: 0, marginTop: '0.05rem' }} aria-hidden="true">
                    <circle cx="12" cy="12" r="10"/>
                    <line x1="12" y1="16" x2="12" y2="12"/>
                    <line x1="12" y1="8" x2="12.01" y2="8"/>
                  </svg>
                  After the role is created, you'll be able to assign its permissions from the Roles page.
                </div>
                {createError && (
                  <div className={styles.errorAlert}><AlertIcon /> {createError}</div>
                )}
              </div>
              <div className={styles.modalFooter}>
                <button type="submit" className={styles.btnPrimary}
                  disabled={createSaving || !createName.trim()}>
                  {createSaving ? <><SpinnerIcon /> Creating…</> : 'Create Role'}
                </button>
                <button type="button" className={styles.btnSecondary}
                  onClick={() => setCreateOpen(false)} disabled={createSaving}>
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ── Delete Role Confirm ── */}
      {deleteId !== null && (
        <div className={styles.modalOverlay}
          onClick={e => { if (e.target === e.currentTarget && !deleteWorking) setDeleteId(null); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>Delete role?</h2>
              <button className={styles.modalCloseBtn}
                onClick={() => setDeleteId(null)} disabled={deleteWorking}>
                <CloseIcon />
              </button>
            </div>

            {/* Visual band — icon + role name */}
            <div className={styles.deleteModalBand}>
              <div className={styles.deleteIconCircle}>
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
                  stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <polyline points="3 6 5 6 21 6"/>
                  <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
                  <path d="M10 11v6M14 11v6"/>
                  <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
                </svg>
              </div>
              <p className={styles.deleteRoleNameLabel}>
                "{rolesSt.roles.find(r => r.company_role_id === deleteId)?.role_name}"
              </p>
            </div>

            <div className={styles.modalBody}>
              <p style={{ fontSize: '0.875rem', color: '#374151', lineHeight: 1.55, margin: 0 }}>
                This role will be removed from the Roles list and can't be assigned to users anymore.
                Existing historical records that used this role will stay unchanged.
              </p>
              <p style={{ fontSize: '0.875rem', color: '#374151', lineHeight: 1.55, margin: 0 }}>
                If this role is currently assigned to active users, move those users to another role
                before deleting it.
              </p>
              {deleteError && (
                <div className={styles.errorAlert}>
                  <AlertIcon /> {deleteError}
                </div>
              )}
            </div>
            <div className={styles.modalFooter}>
              <button className={styles.btnDanger}
                onClick={() => void confirmDelete()} disabled={deleteWorking}>
                {deleteWorking ? <><SpinnerIcon /> Deleting…</> : 'Delete role'}
              </button>
              <button className={styles.btnSecondary}
                onClick={() => setDeleteId(null)} disabled={deleteWorking}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Users Modal ── */}
      {usersModal.roleId !== null && (
        <div className={styles.modalOverlay}
          onClick={e => { if (e.target === e.currentTarget) dispatchUsers({ type: 'CLOSE' }); }}>
          <div className={`${styles.modal} ${styles.modalWide}`}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>
                Users — {usersModal.roleName}
              </h2>
              <button className={styles.modalCloseBtn}
                onClick={() => dispatchUsers({ type: 'CLOSE' })}>
                <CloseIcon />
              </button>
            </div>
            <div className={styles.modalBody}>
              {usersModal.loading && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                  {[...Array(3)].map((_, i) => <div key={i} className={styles.skeleton} style={{ width: `${[80, 65, 72][i]}%`, height: '1.8rem' }} />)}
                </div>
              )}
              {!usersModal.loading && usersModal.error && (
                <div className={styles.errorAlert}><AlertIcon /> {usersModal.error}</div>
              )}
              {!usersModal.loading && !usersModal.error && usersModal.users.length === 0 && (
                <p style={{ fontSize: '0.84rem', color: '#64748b', textAlign: 'center', padding: '1.5rem 0' }}>
                  No users are assigned to this role.
                </p>
              )}
              {!usersModal.loading && !usersModal.error && usersModal.users.length > 0 && (
                <div className={styles.usersList}>
                  {usersModal.users.map(u => (
                    <div key={u.assignment_id} className={`${styles.userRow}${!u.is_active ? ` ${styles.userRowInactive}` : ''}`}>
                      <div className={styles.userAvatar}>
                        {u.display_name.charAt(0).toUpperCase()}
                      </div>
                      <div className={styles.userInfo}>
                        <span className={styles.userDisplayName}>{u.display_name}</span>
                        <span className={styles.userUsername}>@{u.username}{u.email ? ` · ${u.email}` : ''}</span>
                      </div>
                      <div className={styles.userMeta}>
                        <span className={styles.userScope}>
                          {u.scope_type === 'AllCompanyBranches' ? 'All Branches' :
                           u.scope_type === 'SpecificBranch' ? (u.branch_name ?? 'Branch') :
                           'Driver Only'}
                        </span>
                        {!u.is_active && (
                          <span className={styles.inactiveBadge}>Inactive</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
            <div className={styles.modalFooter}>
              <span style={{ flex: 1, fontSize: '0.8rem', color: '#94a3b8' }}>
                {usersModal.users.length} {usersModal.users.length === 1 ? 'assignment' : 'assignments'} — read only
              </span>
              <button className={styles.btnSecondary}
                onClick={() => dispatchUsers({ type: 'CLOSE' })}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Role row sub-component ───────────────────────────────────────────────────

function RoleRow({
  role, selected, onSelect, onDelete, onShowUsers,
  onRenameStart, isRenaming, renameDraft, renameSaving, renameError,
  onRenameDraftChange, onRenameCommit, onRenameCancel,
}: {
  role: CompanyRole;
  selected: boolean;
  onSelect: () => void;
  onDelete: (() => void) | null;
  onShowUsers: () => void;
  onRenameStart: (() => void) | null;
  isRenaming: boolean;
  renameDraft: string;
  renameSaving: boolean;
  renameError: string;
  onRenameDraftChange: (v: string) => void;
  onRenameCommit: () => void;
  onRenameCancel: () => void;
}) {
  return (
    <div
      className={`${styles.roleRow}${selected ? ` ${styles.roleRowSelected}` : ''}${isRenaming ? ` ${styles.roleRowRenaming}` : ''}`}
      onClick={isRenaming ? undefined : onSelect}
    >
      <div className={`${styles.roleIconWrap}${role.is_protected ? ` ${styles.roleIconProtected}` : role.is_custom ? ` ${styles.roleIconCustom}` : ` ${styles.roleIconDefault}`}`}>
        {role.is_protected ? <LockSmallIcon /> : <ShieldSmallIcon />}
      </div>

      {/* Info column — name + badges (or rename input) */}
      {isRenaming ? (
        <div className={styles.roleInfo} onClick={e => e.stopPropagation()}>
          <input
            className={styles.renameInput}
            autoFocus
            value={renameDraft}
            onChange={e => onRenameDraftChange(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter')  { e.preventDefault(); onRenameCommit(); }
              if (e.key === 'Escape') { e.preventDefault(); onRenameCancel(); }
            }}
            maxLength={120}
            disabled={renameSaving}
            aria-label="Edit role name"
          />
          {renameError && <p className={styles.renameError}>{renameError}</p>}
        </div>
      ) : (
        <div className={styles.roleInfo}>
          <span className={styles.roleName}>{role.role_name}</span>
          <div className={styles.roleMetaRow}>
            {role.is_protected && (
              <span className={`${styles.roleBadge} ${styles.badgeProtected}`}>Protected</span>
            )}
            {role.is_default && !role.is_protected && (
              <span className={`${styles.roleBadge} ${styles.badgeDefault}`}>Default</span>
            )}
            {role.is_custom && (
              <span className={`${styles.roleBadge} ${styles.badgeCustom}`}>Custom</span>
            )}
          </div>
        </div>
      )}

      {/* Action buttons — all direct row children so they share the same centre line */}
      {isRenaming ? (
        <>
          <button
            className={styles.renameCommitBtn}
            title="Save (Enter)"
            disabled={renameSaving || !renameDraft.trim()}
            onMouseDown={e => e.preventDefault()}
            onClick={e => { e.stopPropagation(); onRenameCommit(); }}
          >
            {renameSaving ? <SpinnerIcon /> : <CheckIcon />}
          </button>
          <button
            className={styles.renameCancelBtn}
            title="Cancel (Esc)"
            disabled={renameSaving}
            onMouseDown={e => e.preventDefault()}
            onClick={e => { e.stopPropagation(); onRenameCancel(); }}
          >
            <CloseIcon />
          </button>
        </>
      ) : (
        <>
          {onRenameStart && (
            <button
              className={styles.renameBtn}
              title="Rename role"
              onClick={e => { e.stopPropagation(); onRenameStart(); }}
            >
              <PencilIcon />
            </button>
          )}
          {onDelete && (
            <button
              className={styles.deleteBtn}
              title="Delete role"
              onClick={e => { e.stopPropagation(); onDelete(); }}
            >
              <TrashIcon />
            </button>
          )}
          <button
            className={styles.userCountBtn}
            onClick={e => { e.stopPropagation(); onShowUsers(); }}
            title="View assigned users"
          >
            <UsersSmallIcon />
            <span>{role.user_count}</span>
          </button>
        </>
      )}
    </div>
  );
}

// ─── Read-only checkbox for Company Owner ─────────────────────────────────────

function CheckboxCheckedReadOnly() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true" style={{ flexShrink: 0 }}>
      <rect x="0.5" y="0.5" width="15" height="15" rx="3.5" fill="#2563eb" stroke="#2563eb"/>
      <path d="M4 8l3 3 5-5" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/>
    </svg>
  );
}

// ─── Icons ────────────────────────────────────────────────────────────────────

function ShieldIcon({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
    </svg>
  );
}
function ShieldCheckIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
      <polyline points="9 12 11 14 15 10"/>
    </svg>
  );
}
function ShieldSmallIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
    </svg>
  );
}
function LockIcon({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="5" y="11" width="14" height="10" rx="2"/>
      <path d="M8 11V7a4 4 0 0 1 8 0v4"/>
    </svg>
  );
}
function LockSmallIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="5" y="11" width="14" height="10" rx="2"/>
      <path d="M8 11V7a4 4 0 0 1 8 0v4"/>
    </svg>
  );
}
function PlusIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
      <line x1="12" y1="5" x2="12" y2="19"/>
      <line x1="5" y1="12" x2="19" y2="12"/>
    </svg>
  );
}
function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
      <line x1="18" y1="6" x2="6" y2="18"/>
      <line x1="6" y1="6" x2="18" y2="18"/>
    </svg>
  );
}
function CheckIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  );
}
function WarnIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
      <line x1="12" y1="9" x2="12" y2="13"/>
      <line x1="12" y1="17" x2="12.01" y2="17"/>
    </svg>
  );
}
function AlertIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <circle cx="12" cy="12" r="10"/>
      <line x1="12" y1="8" x2="12" y2="12"/>
      <line x1="12" y1="16" x2="12.01" y2="16"/>
    </svg>
  );
}
function TrashIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="3 6 5 6 21 6"/>
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
      <path d="M10 11v6M14 11v6"/>
      <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
    </svg>
  );
}
function SpinnerIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"
      className={styles.spinner} aria-hidden="true">
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
    </svg>
  );
}
function SearchIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="8"/>
      <line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
  );
}
function PencilIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
    </svg>
  );
}
function UsersSmallIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
      <circle cx="9" cy="7" r="4"/>
      <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
      <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
    </svg>
  );
}
