import type { Dispatch, SetStateAction } from 'react';
import type * as React from 'react';
import { AlertIcon, CloseIcon, SpinnerIcon } from './RoleIcons';
import type { RolesState } from './rolesPageState';
import styles from './RolesPage.module.css';

export interface SuccessInfo { title: string; sub: string }
interface RoleSuccessDialogProps { successInfo: SuccessInfo; setSuccessInfo: Dispatch<SetStateAction<SuccessInfo | null>> }
export function RoleSuccessDialog({ successInfo, setSuccessInfo }: RoleSuccessDialogProps) { return (<div className={styles.modalOverlay} onClick={() => setSuccessInfo(null)}>
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
        </div>); }

interface RoleCreateDialogProps { createName: string; setCreateName: Dispatch<SetStateAction<string>>; createSaving: boolean; createError: string; setCreateOpen: Dispatch<SetStateAction<boolean>>; handleCreate(e: React.FormEvent): Promise<void> }
export function RoleCreateDialog({ createName, setCreateName, createSaving, createError, setCreateOpen, handleCreate }: RoleCreateDialogProps) { return (<div className={styles.modalOverlay}
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
        </div>); }

interface RoleDeleteDialogProps { rolesSt: RolesState; deleteId: number; deleteWorking: boolean; deleteError: string; setDeleteId: Dispatch<SetStateAction<number | null>>; confirmDelete(): Promise<void> }
export function RoleDeleteDialog({ rolesSt, deleteId, deleteWorking, deleteError, setDeleteId, confirmDelete }: RoleDeleteDialogProps) { return (<div className={styles.modalOverlay}
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
        </div>); }
