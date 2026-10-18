import { friendlyPermLabel } from '../../lib/permissionLabels';
import type { Branch } from '../../types/core';
import type { CompanyRole, Permission, UserAdmin } from '../../types/admin';
import { accessStatus, avatarInitials, fmtDate, fmtDateTime, scopeLabel } from './peoplePresentation';
import styles from './PeoplePage.module.css';

// ─── PersonDetail ─────────────────────────────────────────────────────────────

export interface PersonDetailProps {
  person: UserAdmin;
  roles: CompanyRole[];
  branches: Branch[];
  allPerms: Permission[];
  amOwner: boolean;
  canEditProfile: boolean;
  canToggleActive: boolean;
  canResetPassword: boolean;
  canChangeRole: boolean;
  canEditExtraPermissions: boolean;
  onEdit(): void; onToggleActive(): void; onResetPw(): void;
  onChangeRole(): void; onEditExtraPerms(): void;
  onTransfer(): void; onGoPayRates(): void;
}

export function PersonDetail({ person, allPerms, amOwner, canEditProfile, canToggleActive, canResetPassword, canChangeRole, canEditExtraPermissions, onEdit, onToggleActive, onResetPw, onChangeRole, onEditExtraPerms, onTransfer, onGoPayRates }: PersonDetailProps) {
  const permByCode = new Map(allPerms.map(p => [p.permission_code, p]));
  const as = accessStatus(person);
  const isOwner = person.company_role_code === 'COMPANY_OWNER';
  const isDriver = person.company_role_code === 'DRIVER';
  const canTransfer = amOwner && !isOwner && person.is_active && person.can_login;

  return (
    <div className={styles.detail}>
      {/* Person header card */}
      <div className={styles.dHeaderCard}>
        <div className={`${styles.dAv}${isOwner ? ` ${styles.dAvOwner}` : ''}`}>{avatarInitials(person.display_name)}</div>
        <div className={styles.dHeaderInfo}>
          <div className={styles.dName}>
            {person.display_name}
            {isOwner && <span className={styles.ownerPill}>Company Owner</span>}
            {isDriver && <span className={styles.driverPill}>Driver</span>}
          </div>
          <div className={styles.dSub}>@{person.username}</div>
          <div className={styles.dBadges}>
            <span className={person.is_active ? styles.badgeOk : styles.badgeErr}>{person.is_active ? 'Active' : 'Inactive'}</span>
            <span className={`${styles.asBadge} ${styles[`as_${as.kind}`]}`}>{as.label}</span>
            {person.can_login && person.is_active && <span className={styles.badgeLogin}>Login enabled</span>}
          </div>
          {/* Action buttons — horizontal row */}
          <div className={styles.dActions}>
            {canEditProfile && <button className={styles.actBtn} onClick={onEdit}>Edit</button>}
            {canToggleActive && (
              <button className={`${styles.actBtn}${person.is_active ? ` ${styles.actBtnDanger}` : ''}`} onClick={onToggleActive}>
                {person.is_active ? 'Deactivate' : 'Activate'}
              </button>
            )}
            {canResetPassword && <button className={styles.actBtn} onClick={onResetPw}>Reset Password</button>}
            {canChangeRole && !isOwner && <button className={styles.actBtn} onClick={onChangeRole}>Change Role</button>}
            {canTransfer && <button className={`${styles.actBtn} ${styles.actBtnOwner}`} onClick={onTransfer}>Transfer Ownership</button>}
            {isDriver && <button className={`${styles.actBtn} ${styles.actBtnDriver}`} onClick={onGoPayRates}>Pay Rates →</button>}
          </div>
        </div>
      </div>

      {/* Sections */}
      <div className={styles.sections}>

        {/* Profile */}
        <section className={styles.sec}>
          <h3 className={styles.secTitle}>Profile</h3>
          <div className={styles.grid2}>
            <span className={styles.fl}>Email</span><span>{person.email ?? '—'}</span>
            <span className={styles.fl}>Phone</span><span>{person.phone ?? '—'}</span>
            <span className={styles.fl}>Last login</span><span>{fmtDateTime(person.last_login_at_utc)}</span>
            <span className={styles.fl}>Member since</span><span>{fmtDate(person.created_at_utc)}</span>
          </div>
        </section>

        {/* Access */}
        <section className={styles.sec}>
          <h3 className={styles.secTitle}>Account Access</h3>
          <div className={styles.grid2}>
            <span className={styles.fl}>Can login</span><span>{person.can_login ? '✓ Yes' : '✗ No'}</span>
            <span className={styles.fl}>Must change password</span><span>{person.must_change_password ? 'Yes — on next login' : 'No'}</span>
            <span className={styles.fl}>Access status</span><span className={`${styles.asBadge} ${styles[`as_${as.kind}`]}`}>{as.label}</span>
          </div>
        </section>

        {/* Role & Scope */}
        <section className={styles.sec}>
          <h3 className={styles.secTitle}>Role & Scope</h3>
          {isOwner && (
            <div className={styles.ownerNote}>
              Company Owner has full access to all permissions. Role, scope, and extra permissions cannot be changed here. Use Transfer Ownership to hand over authority.
            </div>
          )}
          <div className={styles.grid2}>
            <span className={styles.fl}>Role</span>
            <span>{person.company_role_name ?? <span className={styles.noRole}>No role assigned</span>}</span>
            <span className={styles.fl}>Scope</span>
            <span>{scopeLabel(person.company_role_scope, person.company_role_branch_name)}</span>
            {person.role_permission_codes.length > 0 && (
              <>
                <span className={styles.fl}>Role permissions</span>
                <span className={styles.permCount}>{person.role_permission_codes.length} permissions from role</span>
              </>
            )}
          </div>
        </section>

        {/* Extra Permissions */}
        {!isOwner && (
          <section className={styles.sec}>
            <div className={styles.secTitleRow}>
              <h3 className={styles.secTitle}>Extra Permissions</h3>
              {canEditExtraPermissions && <button className={styles.secEditBtn} onClick={onEditExtraPerms}>Edit</button>}
            </div>
            {person.extra_permission_codes.length === 0 ? (
              <p className={styles.emptySecMsg}>No extra permissions. Role permissions apply.</p>
            ) : (
              <div className={styles.extraPermList}>
                {person.extra_permission_codes.map(code => (
                  <span key={code} className={styles.extraPermChip}>
                    {permByCode.get(code)?.permission_name ?? friendlyPermLabel(code)}
                  </span>
                ))}
              </div>
            )}
            <p className={styles.helpTxt}>Extra permissions are additions on top of this person's role. Use only for exceptions.</p>
          </section>
        )}

        {/* Driver section */}
        {isDriver && (
          <section className={styles.sec}>
            <h3 className={styles.secTitle}>Driver</h3>
            {person.driver_id ? (
              <div className={styles.grid2}>
                <span className={styles.fl}>Driver</span><span>Linked</span>
                <span className={styles.fl}>Pay rates</span><span><button className={styles.linkBtn} onClick={onGoPayRates}>Go to Pay Rates →</button></span>
              </div>
            ) : (
              <div className={styles.driverGapBox}>
                <p>Driver not linked yet. Pay rates require a Driver to be created separately via the Drivers module.</p>
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
