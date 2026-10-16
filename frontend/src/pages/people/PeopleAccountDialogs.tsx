import type { Dispatch } from 'react';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import type { CompanyRole, UserAdmin } from '../../types/admin';
import type { Action, PageState } from './peoplePageState';
import { ErrMsg, Field, Modal, ModalFoot, PermissionPicker, ScopeFields } from './PeopleFormControls';
import styles from './PeoplePage.module.css';

interface PeopleAccountDialogsProps {
  st: PageState;
  selected: UserAdmin | null;
  targetPerson: UserAdmin | null | undefined;
  assignableRoles: CompanyRole[];
  ownerTransferRoles: CompanyRole[];
  dispatch: Dispatch<Action>;
  submitEdit(e: React.FormEvent): void;
  submitReset(e: React.FormEvent): void;
  submitChangeRole(e: React.FormEvent): void;
  submitExtraPerms(e: React.FormEvent): void;
  confirmToggleActive(): void;
  submitTransfer(e: React.FormEvent): void;
}

export function PeopleAccountDialogs({ st, selected, targetPerson, assignableRoles, ownerTransferRoles, dispatch, submitEdit, submitReset, submitChangeRole, submitExtraPerms, confirmToggleActive, submitTransfer }: PeopleAccountDialogsProps) {
  return (
    <>
      {st.edit.open && selected && (
        <Modal title="Edit Person" onClose={() => dispatch({ type: 'EDIT_CLOSE' })}>
          <form onSubmit={submitEdit} className={styles.form}>
            <Field label="Display Name *"><input className={styles.inp} value={st.edit.name} onChange={e => dispatch({ type: 'EDIT_FIELD', field: 'name', val: e.target.value })} required /></Field>
            <Field label="Email"><input className={styles.inp} type="email" value={st.edit.email} onChange={e => dispatch({ type: 'EDIT_FIELD', field: 'email', val: e.target.value })} /></Field>
            <Field label="Phone"><input className={styles.inp} value={st.edit.phone} onChange={e => dispatch({ type: 'EDIT_FIELD', field: 'phone', val: e.target.value })} /></Field>
            <div className={styles.checks}>
              <label><input type="checkbox" checked={st.edit.canLogin} onChange={e => dispatch({ type: 'EDIT_FIELD', field: 'canLogin', val: e.target.checked })} /> Can login</label>
              <label><input type="checkbox" checked={st.edit.mustChange} onChange={e => dispatch({ type: 'EDIT_FIELD', field: 'mustChange', val: e.target.checked })} /> Must change password on next login</label>
            </div>
            {st.edit.error && <ErrMsg msg={st.edit.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'EDIT_CLOSE' })} loading={st.edit.loading} label="Save Changes" />
          </form>
        </Modal>
      )}

      {st.reset.open && selected && (
        <Modal title="Reset Password" onClose={() => dispatch({ type: 'RESET_CLOSE' })}>
          <form onSubmit={submitReset} className={styles.form}>
            <p className={styles.helpTxt}>Setting a new password for <strong>{selected.display_name}</strong>.</p>
            <Field label="New Password *"><input className={styles.inp} type="password" value={st.reset.pw} onChange={e => dispatch({ type: 'RESET_PW', val: e.target.value })} minLength={8} required /></Field>
            <div className={styles.checks}>
              <label><input type="checkbox" checked={st.reset.mustChange} onChange={e => dispatch({ type: 'RESET_MUST', val: e.target.checked })} /> Require password change on next login</label>
            </div>
            {st.reset.error && <ErrMsg msg={st.reset.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'RESET_CLOSE' })} loading={st.reset.loading} label="Reset Password" />
          </form>
        </Modal>
      )}

      {st.changeRole.open && selected && (
        <Modal title="Change Role & Scope" onClose={() => dispatch({ type: 'CR_CLOSE' })}>
          <form onSubmit={submitChangeRole} className={styles.form}>
            <p className={styles.helpTxt}>Changing role for <strong>{selected.display_name}</strong>. The current role assignment will be revoked.</p>
            <ScopeFields roles={assignableRoles} branches={st.branches} roleId={st.changeRole.roleId} scope={st.changeRole.scope} branchId={st.changeRole.branchId} showRole
              onRole={id => dispatch({ type: 'CR_FIELD', field: 'roleId', val: id })}
              onScope={s => { dispatch({ type: 'CR_FIELD', field: 'scope', val: s }); if (s === 'AllCompanyBranches') dispatch({ type: 'CR_FIELD', field: 'branchId', val: null }); }}
              onBranch={b => dispatch({ type: 'CR_FIELD', field: 'branchId', val: b })}
            />
            {st.changeRole.error && <ErrMsg msg={st.changeRole.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'CR_CLOSE' })} loading={st.changeRole.loading} label="Save Role" />
          </form>
        </Modal>
      )}

      {st.extraPerms.open && selected && (
        <Modal title="Edit Extra Permissions" onClose={() => dispatch({ type: 'EP_CLOSE' })} wide>
          <form onSubmit={submitExtraPerms} className={styles.form}>
            <p className={styles.helpTxt}>Extra permissions for <strong>{selected.display_name}</strong>. These are added on top of the role's permissions. Use only for exceptions.</p>
            <PermissionPicker
              allPerms={st.allPerms}
              rolePerms={new Set(selected.role_permission_codes)}
              extraPerms={st.extraPerms.codes}
              onToggle={(code, checked) => dispatch({ type: 'EP_TOGGLE', code, checked })}
            />
            {st.extraPerms.error && <ErrMsg msg={st.extraPerms.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'EP_CLOSE' })} loading={st.extraPerms.loading} label="Save Extra Permissions" />
          </form>
        </Modal>
      )}

      {st.confirmToggle.open && (
        <ConfirmDialog
          open={st.confirmToggle.open}
          title={st.confirmToggle.toActive ? 'Activate Person' : 'Deactivate Person'}
          message={st.confirmToggle.toActive ? 'This will restore access for this person.' : 'This will prevent this person from logging in.'}
          confirmLabel={st.confirmToggle.toActive ? 'Activate' : 'Deactivate'}
          variant={st.confirmToggle.toActive ? 'primary' : 'danger'}
          onConfirm={confirmToggleActive}
          onCancel={() => dispatch({ type: 'CONFIRM_TOGGLE_CLOSE' })}
        />
      )}

      {st.transfer.open && targetPerson && (
        <Modal title="Transfer Company Ownership" onClose={() => dispatch({ type: 'TRANSFER_CLOSE' })} wide>
          <form onSubmit={submitTransfer} className={styles.form}>
            <div className={styles.xferWarn}>
              <span>⚠️</span>
              <div>
                <p><strong>You are about to transfer Company Owner authority to {targetPerson.display_name}.</strong></p>
                <p>You will no longer be Company Owner after this transfer. This action is audited and cannot be undone without the new owner's cooperation.</p>
              </div>
            </div>
            <div className={styles.xferSummary}>
              <span className={styles.fl}>New owner</span>
              <strong>{targetPerson.display_name}</strong> ({targetPerson.username})
            </div>
            <Field label="Replacement role for you after transfer (optional)">
              <select className={styles.inp} value={st.transfer.replacementRoleId ?? ''} onChange={e => dispatch({ type: 'TRANSFER_FIELD', field: 'replacementRoleId', val: e.target.value ? parseInt(e.target.value) : null })}>
                <option value="">— No replacement role —</option>
                {ownerTransferRoles.map(r => <option key={r.company_role_id} value={r.company_role_id}>{r.role_name}</option>)}
              </select>
            </Field>
            <Field label={<>Type <strong>TRANSFER</strong> to confirm *</>}>
              <input className={`${styles.inp} ${styles.monoInput}`} value={st.transfer.confirmText} onChange={e => dispatch({ type: 'TRANSFER_FIELD', field: 'confirmText', val: e.target.value })} placeholder="TRANSFER" autoComplete="off" />
            </Field>
            {st.transfer.error && <ErrMsg msg={st.transfer.error} />}
            <div className={styles.mf}>
              <button type="button" className={styles.cancelBtn} onClick={() => dispatch({ type: 'TRANSFER_CLOSE' })}>Cancel</button>
              <button type="submit" className={styles.dangerBtn} disabled={st.transfer.loading || st.transfer.confirmText.trim().toUpperCase() !== 'TRANSFER'}>
                {st.transfer.loading ? 'Transferring…' : 'Transfer Ownership'}
              </button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}
