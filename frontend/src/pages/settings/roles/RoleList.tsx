import type { Dispatch, SetStateAction } from 'react';
import type { CompanyRole } from '../../../types/admin';
import { CheckIcon, CloseIcon, LockSmallIcon, PencilIcon, PlusIcon, SearchIcon, ShieldSmallIcon, SpinnerIcon, TrashIcon, UsersSmallIcon } from './RoleIcons';
import type { RolesState, UsersModalAction } from './rolesPageState';
import styles from './RolesPage.module.css';

interface RoleListProps {
  rolesSt: RolesState;
  selectedId: number | null;
  roleSearch: string;
  setRoleSearch(value: string): void;
  roleFilter: 'all' | 'default' | 'custom';
  setRoleFilter(value: 'all' | 'default' | 'custom'): void;
  defaultRoles: CompanyRole[];
  customRoles: CompanyRole[];
  filteredDefaultRoles: CompanyRole[];
  filteredCustomRoles: CompanyRole[];
  searchLower: string;
  hasSearchResults: boolean;
  cancelRename(): void;
  setSelectedId: Dispatch<SetStateAction<number | null>>;
  setSaveError: Dispatch<SetStateAction<string>>;
  setSaveOk: Dispatch<SetStateAction<boolean>>;
  dispatchUsers: Dispatch<UsersModalAction>;
  canDelete: boolean;
  setDeleteId: Dispatch<SetStateAction<number | null>>;
  setDeleteError: Dispatch<SetStateAction<string>>;
  canEdit: boolean;
  startRename(role: CompanyRole): void;
  renameId: number | null;
  renameDraft: string;
  renameSaving: boolean;
  renameError: string;
  setRenameDraft: Dispatch<SetStateAction<string>>;
  commitRename(): Promise<void>;
  canCreate: boolean;
  setCreateOpen: Dispatch<SetStateAction<boolean>>;
  setCreateError: Dispatch<SetStateAction<string>>;
}

export function RoleList({
  rolesSt,
  selectedId,
  roleSearch,
  setRoleSearch,
  roleFilter,
  setRoleFilter,
  defaultRoles,
  customRoles,
  filteredDefaultRoles,
  filteredCustomRoles,
  searchLower,
  hasSearchResults,
  cancelRename,
  setSelectedId,
  setSaveError,
  setSaveOk,
  dispatchUsers,
  canDelete,
  setDeleteId,
  setDeleteError,
  canEdit,
  startRename,
  renameId,
  renameDraft,
  renameSaving,
  renameError,
  setRenameDraft,
  commitRename,
  canCreate,
  setCreateOpen,
  setCreateError,
}: RoleListProps) {
  return (
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
  );
}

export function RoleRow({
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

