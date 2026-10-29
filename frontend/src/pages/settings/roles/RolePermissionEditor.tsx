import type { Dispatch, SetStateAction } from 'react';
import type * as React from 'react';
import { friendlyPermLabel } from '../../../lib/permissionLabels';
import type { CompanyRole, Permission } from '../../../types/admin';
import { PERM_DEPS, getRiskTier, groupPermissionsByDomain, isUserVisibleParentMissing } from './rolePermissionModel';
import type { PermsAction, PermsState, RiskTier } from './rolePermissionModel';
import type { UsersModalAction } from './rolesPageState';
import { AlertIcon, CheckIcon, CheckboxCheckedReadOnly, LockSmallIcon, ShieldCheckIcon, ShieldIcon, SpinnerIcon, UsersSmallIcon, WarnIcon } from './RoleIcons';
import styles from './RolesPage.module.css';

const GROUP_ICONS: Record<string, React.ReactNode> = {
  'org-admin': <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 3v18"/><rect x="13" y="13" width="3" height="3"/><rect x="13" y="6" width="3" height="3"/><rect x="6" y="13" width="3" height="3"/><rect x="6" y="6" width="3" height="3"/></svg>,
  'payroll-ops': <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>,
  'payroll-config': <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>,
  'payroll-setup': <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>,
  'workforce': <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="1" y="3" width="15" height="13" rx="2"/><path d="M16 8h4l3 3v5h-7V8z"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/></svg>,
  'reporting': <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>,
};

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

interface RolePermissionEditorProps {
  selectedRole: CompanyRole | null;
  allPerms: Permission[];
  permsSt: PermsState;
  dispatchPerms: Dispatch<PermsAction>;
  dispatchUsers: Dispatch<UsersModalAction>;
  readOnly: boolean;
  canEdit: boolean;
  saveError: string;
  setSaveError: Dispatch<SetStateAction<string>>;
  saveOk: boolean;
  setSaveOk: Dispatch<SetStateAction<boolean>>;
  savePermissions(): Promise<void>;
  saving: boolean;
  discardPermissions(): void;
}

export function RolePermissionEditor({
  selectedRole,
  allPerms,
  permsSt,
  dispatchPerms,
  dispatchUsers,
  readOnly,
  canEdit,
  saveError,
  setSaveError,
  saveOk,
  setSaveOk,
  savePermissions,
  saving,
  discardPermissions,
}: RolePermissionEditorProps) {
  const groupedDomains = groupPermissionsByDomain(allPerms);
  const isOwner = selectedRole?.role_code === 'COMPANY_OWNER';
  return (
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
                      <span className={styles.permGroupIcon}>{GROUP_ICONS[group.id]}</span>
                      {group.label}
                    </div>
                    {group.note && (
                      <div style={{ fontSize: '0.72rem', color: '#94a3b8', fontWeight: 400, fontStyle: 'italic', textTransform: 'none', letterSpacing: 'normal', margin: '-0.35rem 0 0.55rem 0.4rem' }}>
                        {group.note}
                      </div>
                    )}
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
                      <span className={styles.permGroupIcon}>{GROUP_ICONS[group.id]}</span>
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
                    {group.note && (
                      <div style={{ fontSize: '0.72rem', color: '#94a3b8', fontWeight: 400, fontStyle: 'italic', textTransform: 'none', letterSpacing: 'normal', margin: '-0.35rem 0 0.55rem 0.4rem' }}>
                        {group.note}
                      </div>
                    )}
                    {perms.map(perm => {
                      const checked   = permsSt.codes.has(perm.permission_code);
                      const parent    = PERM_DEPS[perm.permission_code];
                      const parentOff = isUserVisibleParentMissing(parent, permsSt.codes);
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
                      onClick={discardPermissions}
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
  );
}

