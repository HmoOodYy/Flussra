import { useEffect } from 'react';
import type { Branch } from '../../types/core';
import type { CompanyRole, Permission } from '../../types/admin';
import { friendlyPermLabel } from '../../lib/permissionLabels';
import { PERM_DEPS, groupPerms } from './peoplePermissionModel';
import styles from './PeoplePage.module.css';

// ─── PermissionPicker ─────────────────────────────────────────────────────────

export interface PermissionPickerProps {
  allPerms: Permission[];
  rolePerms: Set<string>;          // permissions already included by the role
  extraPerms: Set<string>;         // currently selected extra permissions
  onToggle(code: string, checked: boolean): void;
}

export function PermissionPicker({ allPerms, rolePerms, extraPerms, onToggle }: PermissionPickerProps) {
  const groups = groupPerms(allPerms);
  return (
    <div className={styles.permPicker}>
      {groups.map(g => (
        <div key={g.module} className={styles.permGroup}>
          <div className={styles.permGroupTitle}>{g.label}</div>
          {g.note && (
            <div style={{ padding: '0 0.9rem 0.35rem', fontSize: '0.72rem', color: '#9ca3af', fontStyle: 'italic' }}>
              {g.note}
            </div>
          )}
          {g.perms.map(perm => {
            const code = perm.permission_code;
            const inRole = rolePerms.has(code);
            const inExtra = extraPerms.has(code);
            const parentCode = PERM_DEPS[code];
            const parentOff = parentCode && !rolePerms.has(parentCode) && !extraPerms.has(parentCode);
            return (
              <label key={code} className={`${styles.permRow}${inRole ? ` ${styles.permRowRole}` : ''}${parentOff && !inRole ? ` ${styles.permRowDim}` : ''}`}>
                <input
                  type="checkbox"
                  checked={inRole || inExtra}
                  disabled={inRole}
                  onChange={e => { if (!inRole) onToggle(code, e.target.checked); }}
                />
                <div>
                  <span className={styles.permName}>{perm.permission_name}</span>
                  {inRole && <span className={styles.permFromRole}> · included by role</span>}
                  {parentCode && !inRole && !inExtra && (
                  <span className={styles.permDep}> · requires {allPerms.find(p => p.permission_code === parentCode)?.permission_name ?? friendlyPermLabel(parentCode)}</span>
                )}
                </div>
              </label>
            );
          })}
        </div>
      ))}
    </div>
  );
}

// ─── ScopeFields ─────────────────────────────────────────────────────────────

export interface ScopeFieldsProps {
  roles: CompanyRole[]; branches: Branch[];
  roleId: number | null;
  scope: 'AllCompanyBranches' | 'SpecificBranch' | 'Self';
  branchId: number | null;
  showRole?: boolean;
  onRole(id: number | null): void;
  onScope(s: 'AllCompanyBranches' | 'SpecificBranch' | 'Self'): void;
  onBranch(b: number | null): void;
}

export function ScopeFields({ roles, branches, roleId, scope, branchId, showRole = true, onRole, onScope, onBranch }: ScopeFieldsProps) {
  const isDriverRole = roles.find(r => r.company_role_id === roleId)?.role_code === 'DRIVER';

  // DRIVER assignments use Self without a branch; other roles use company or branch scope.
  useEffect(() => {
    if (isDriverRole && scope !== 'Self') {
      onScope('Self');
    } else if (!isDriverRole && scope === 'Self') {
      onScope('AllCompanyBranches');
    }
  }, [isDriverRole, scope, onScope]);

  return (
    <>
      {showRole && (
        <Field label="Role *">
          <select className={styles.inp} value={roleId ?? ''} onChange={e => onRole(e.target.value ? parseInt(e.target.value) : null)} required>
            <option value="">— Select a role —</option>
            {roles.map(r => <option key={r.company_role_id} value={r.company_role_id}>{r.role_name}</option>)}
          </select>
        </Field>
      )}
      <Field label="Scope *">
        <select className={styles.inp} value={scope} onChange={e => onScope(e.target.value as typeof scope)}>
          {!isDriverRole && (
            <option value="AllCompanyBranches">All company branches — full company access</option>
          )}
          {!isDriverRole && <option value="SpecificBranch">Specific branch — limited to one branch</option>}
          {isDriverRole && <option value="Self">Self — own current Driver identity</option>}
        </select>
      </Field>
      {isDriverRole && (
        <p className={styles.helpTxt}>
          🚛 Self carries no branch access. Generic payroll, rate, and administration actions remain unavailable to DRIVER accounts.
        </p>
      )}
      {scope === 'SpecificBranch' && (
        <Field label="Branch *">
          <select className={styles.inp} value={branchId ?? ''} onChange={e => onBranch(e.target.value ? parseInt(e.target.value) : null)} required>
            <option value="">— Select a branch —</option>
            {branches.map(b => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
          </select>
        </Field>
      )}
    </>
  );
}

// ─── Small shared primitives ─────────────────────────────────────────────────

export function Modal({ title, onClose, children, wide }: { title: string; onClose(): void; children: React.ReactNode; wide?: boolean }) {
  return (
    <div className={styles.overlay}>
      <div className={`${styles.modal}${wide ? ` ${styles.modalWide}` : ''}`}>
        <div className={styles.mHeader}>
          <h2>{title}</h2>
          <button className={styles.closeBtn} onClick={onClose}>✕</button>
        </div>
        <div className={styles.mBody}>{children}</div>
      </div>
    </div>
  );
}

export function Field({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className={styles.field}>
      <label className={styles.lbl}>{label}</label>
      {children}
    </div>
  );
}

export function ErrMsg({ msg }: { msg: string }) {
  return <div className={styles.errMsg}>{msg}</div>;
}

export function ModalFoot({ onCancel, loading, label }: { onCancel(): void; loading: boolean; label: string }) {
  return (
    <div className={styles.mf}>
      <button type="button" className={styles.cancelBtn} onClick={onCancel}>Cancel</button>
      <button type="submit" className={styles.primaryBtn} disabled={loading}>{loading ? 'Saving…' : label}</button>
    </div>
  );
}
