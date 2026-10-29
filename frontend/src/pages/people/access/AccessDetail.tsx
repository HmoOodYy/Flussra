import { Link } from 'react-router-dom';
import { OWN_ACCESS_NOTICE } from '../../../lib/accessSafetyErrors';
import { friendlyPermLabel } from '../../../lib/permissionLabels';
import type { Permission, UserAdmin } from '../../../types/admin';
import { avatarInitials, fmtDate, fmtDateTime, scopeLabel } from '../peoplePresentation';
import { accessFacts, accessStatus, canLinkUser, linkSummary, unlinkBlockedReason } from './accessModel';
import styles from '../PeoplePage.module.css';

export interface AccessDetailProps {
  user: UserAdmin;
  allPerms: Permission[];
  amOwner: boolean;
  /** The signed-in person's own account: their role and extra permissions are changed by someone else. */
  isSelf: boolean;
  can: { edit: boolean; toggle: boolean; changeRole: boolean; provision: boolean; link: boolean; openWorkforce: boolean };
  canViewPayRates: boolean;
  onEdit(): void; onToggleActive(): void; onResetPw(): void; onChangeRole(): void;
  onEditExtraPerms(): void; onOwnerTransfer(): void; onProvision(): void; onLink(): void; onUnlink(): void;
}

/** One User's account: login state, role and scope, overrides and Employee link. Not an Employee record. */
export function AccessDetail({ user, allPerms, amOwner, isSelf, can, canViewPayRates, onEdit, onToggleActive, onResetPw, onChangeRole, onEditExtraPerms, onOwnerTransfer, onProvision, onLink, onUnlink }: AccessDetailProps) {
  const permByCode = new Map(allPerms.map(p => [p.permission_code, p]));
  const status = accessStatus(user);
  const facts = accessFacts(user);
  const link = linkSummary(user);
  const isOwner = user.company_role_code === 'COMPANY_OWNER';
  const canOwnerTransfer = amOwner && !isOwner && user.is_active && user.can_login;
  const unlinkBlocked = unlinkBlockedReason(user);

  return (
    <div className={styles.detail}>
      <div className={styles.dHeaderCard}>
        <div className={`${styles.dAv}${isOwner ? ` ${styles.dAvOwner}` : ''}`}>{avatarInitials(user.display_name)}</div>
        <div className={styles.dHeaderInfo}>
          <div className={styles.dName}>
            {user.display_name}
            {isOwner && <span className={styles.ownerPill}>Company Owner</span>}
            {link.isDriver && <span className={styles.driverPill}>Linked Driver</span>}
          </div>
          <div className={styles.dSub}>@{user.username}</div>
          <div className={styles.dBadges}>
            <span className={`${styles.asBadge} ${styles[`as_${status.kind}`]}`}>{status.label}</span>
            <span className={user.is_active ? styles.badgeOk : styles.badgeErr}>Account: {facts.active}</span>
            <span className={user.can_login ? styles.badgeLogin : styles.badgeErr}>{facts.login}</span>
          </div>
          <div className={styles.dActions}>
            {can.edit && <button className={styles.actBtn} onClick={onEdit}>Edit</button>}
            {can.toggle && (
              <button className={`${styles.actBtn}${user.is_active ? ` ${styles.actBtnDanger}` : ''}`} onClick={onToggleActive}>
                {user.is_active ? 'Deactivate' : 'Activate'}
              </button>
            )}
            {can.edit && <button className={styles.actBtn} onClick={onResetPw}>Reset Password</button>}
            {user.is_staged && can.provision && <button className={`${styles.actBtn} ${styles.actBtnDriver}`} onClick={onProvision}>Provision account</button>}
            {can.changeRole && !isOwner && !isSelf && !user.is_staged && <button className={styles.actBtn} onClick={onChangeRole}>Change Role</button>}
            {canOwnerTransfer && <button className={`${styles.actBtn} ${styles.actBtnOwner}`} onClick={onOwnerTransfer}>Transfer Ownership</button>}
          </div>
        </div>
      </div>

      <div className={styles.sections}>
        <section className={styles.sec}>
          <h3 className={styles.secTitle}>Profile</h3>
          <div className={styles.grid2}>
            <span className={styles.fl}>Email</span><span>{user.email ?? '—'}</span>
            <span className={styles.fl}>Phone</span><span>{user.phone ?? '—'}</span>
            <span className={styles.fl}>Last login</span><span>{fmtDateTime(user.last_login_at_utc)}</span>
            <span className={styles.fl}>Account created</span><span>{fmtDate(user.created_at_utc)}</span>
          </div>
        </section>

        <section className={styles.sec}>
          <h3 className={styles.secTitle}>Account state</h3>
          <div className={styles.grid2}>
            <span className={styles.fl}>Provisioning</span><span>{facts.lifecycle}{user.is_staged ? ' — no role yet, cannot log in' : ''}</span>
            <span className={styles.fl}>Account</span><span>{facts.active}</span>
            <span className={styles.fl}>Login</span><span>{facts.login}</span>
            <span className={styles.fl}>Must change password</span><span>{user.must_change_password ? 'Yes — on next login' : 'No'}</span>
          </div>
        </section>

        <section className={styles.sec}>
          <div className={styles.secTitleRow}>
            <h3 className={styles.secTitle}>Employee link</h3>
            {can.link && canLinkUser(user) && <button className={styles.secEditBtn} onClick={onLink}>Link Employee</button>}
            {can.link && link.linked && (
              <button className={styles.secEditBtn} onClick={onUnlink} disabled={unlinkBlocked !== null} title={unlinkBlocked ?? undefined}>Unlink</button>
            )}
          </div>
          {link.linked ? (
            <div className={styles.grid2}>
              <span className={styles.fl}>Employee</span>
              <span>
                {link.employeeName ?? `Employee ${link.employeeId}`}
                {can.openWorkforce && link.employeeId !== null && (
                  <> · <Link className={styles.linkBtn} to={`/people/workforce?employee=${link.employeeId}`}>Open in Workforce →</Link></>
                )}
              </span>
              <span className={styles.fl}>Driver</span><span>{link.isDriver ? 'Yes — the Employee is a permanent Driver' : 'No'}</span>
              {link.isDriver && canViewPayRates && user.driver_id !== null && (
                <><span className={styles.fl}>Pay rates</span><span><Link className={styles.linkBtn} to={`/people/pay-rates?driverId=${user.driver_id}`}>Go to Pay Rates →</Link></span></>
              )}
            </div>
          ) : (
            <p className={styles.emptySecMsg}>Not linked to an Employee. An account does not need one, except for DRIVER access.</p>
          )}
        </section>

        <section className={styles.sec}>
          <h3 className={styles.secTitle}>Role & Scope</h3>
          {isOwner && (
            <div className={styles.ownerNote}>
              Company Owner has full access to all permissions. Role, scope, and extra permissions cannot be changed here. Use Transfer Ownership to hand over authority.
            </div>
          )}
          {isSelf && !isOwner && <p className={styles.helpTxt}>{OWN_ACCESS_NOTICE}</p>}
          <div className={styles.grid2}>
            <span className={styles.fl}>Role</span>
            <span>{user.company_role_name ?? <span className={styles.noRole}>No role assigned</span>}</span>
            <span className={styles.fl}>Scope</span>
            <span>{scopeLabel(user.company_role_scope, user.company_role_branch_name)}</span>
            {user.role_permission_codes.length > 0 && (
              <><span className={styles.fl}>Role permissions</span><span className={styles.permCount}>{user.role_permission_codes.length} permissions from role</span></>
            )}
          </div>
        </section>

        {!isOwner && (
          <section className={styles.sec}>
            <div className={styles.secTitleRow}>
              <h3 className={styles.secTitle}>Extra Permissions</h3>
              {can.edit && !isSelf && <button className={styles.secEditBtn} onClick={onEditExtraPerms}>Edit</button>}
            </div>
            {user.extra_permission_codes.length === 0 ? (
              <p className={styles.emptySecMsg}>No extra permissions. Role permissions apply.</p>
            ) : (
              <div className={styles.extraPermList}>
                {user.extra_permission_codes.map(code => (
                  <span key={code} className={styles.extraPermChip}>{permByCode.get(code)?.permission_name ?? friendlyPermLabel(code)}</span>
                ))}
              </div>
            )}
            <p className={styles.helpTxt}>Extra permissions are additions on top of the role. Use only for exceptions. Role definitions live in Settings → Roles &amp; Permissions.</p>
          </section>
        )}
      </div>
    </div>
  );
}
