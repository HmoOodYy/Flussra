import type { Dispatch } from 'react';
import type { UsersModalAction, UsersModalState } from './rolesPageState';
import { AlertIcon, CloseIcon } from './RoleIcons';
import styles from './RolesPage.module.css';
interface RoleUsersDialogProps { usersModal: UsersModalState; dispatchUsers: Dispatch<UsersModalAction> }
export function RoleUsersDialog({ usersModal, dispatchUsers }: RoleUsersDialogProps) { return (<div className={styles.modalOverlay}
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
        </div>); }

