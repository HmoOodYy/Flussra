import type { Dispatch, FormEvent } from 'react';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import type { CompanyRole, EmployeeLinkCandidate, UserAdmin } from '../../../types/admin';
import { ErrMsg, Field, Modal, ModalFoot, PermissionPicker, ScopeFields } from '../PeopleFormControls';
import { eligibleCandidates, withSelected } from './accessModel';
import type { AccessAction, AccessPageState } from './accessState';
import styles from '../PeoplePage.module.css';

interface AccessDialogsProps {
  st: AccessPageState;
  selected: UserAdmin | null;
  ownerTransferTarget: UserAdmin | null | undefined;
  assignableRoles: CompanyRole[];
  ownerTransferRoles: CompanyRole[];
  dispatch: Dispatch<AccessAction>;
  submitCreate(e: FormEvent): void;
  submitProvision(e: FormEvent): void;
  submitLink(e: FormEvent): void;
  submitEdit(e: FormEvent): void;
  submitReset(e: FormEvent): void;
  submitChangeRole(e: FormEvent): void;
  submitExtraPerms(e: FormEvent): void;
  confirmToggleActive(): void;
  submitOwnerTransfer(e: FormEvent): void;
  loadMoreCandidates(target: 'create' | 'link'): void;
}

function candidateLabel(c: EmployeeLinkCandidate): string {
  const driver = c.driver_state === 'current' ? ' · Driver' : c.driver_state === 'pending' ? ' · Driver (starts later)' : '';
  return `${c.full_name}${c.employee_key ? ` (${c.employee_key})` : ''} · ${c.branch_name}${driver}`;
}

export function AccessDialogs(props: AccessDialogsProps) {
  const { st, selected, ownerTransferTarget, assignableRoles, ownerTransferRoles, dispatch } = props;
  const createRole = assignableRoles.find(r => r.company_role_id === st.create.form.roleId) ?? null;
  const createIsDriver = st.create.form.mode === 'provisioned' && createRole?.role_code === 'DRIVER';
  const createCandidates = withSelected(
    st.create.form.mode === 'provisioned' ? eligibleCandidates(createRole, st.create.candidates) : st.create.candidates,
    st.create.selected,
  );
  const provisionRole = assignableRoles.find(r => r.company_role_id === st.provision.form.roleId) ?? null;
  const linkCandidates = st.link.candidates;

  return (
    <>
      {/* ── Create account: one atomic request ── */}
      {st.create.open && (
        <Modal title="Create account" onClose={() => dispatch({ type: 'CREATE_CLOSE' })} wide>
          <form onSubmit={props.submitCreate} className={styles.form}>
            <p className={styles.helpTxt}>
              An account is separate from a Workforce record. It can be linked to an Employee, or stand alone
              (an external administrator, for example). Everything below is saved in one step.
            </p>
            <Field label="Account type">
              <select className={styles.inp} value={st.create.form.mode} onChange={e => dispatch({ type: 'CREATE_FORM', form: { mode: e.target.value as 'staged' | 'provisioned' } })}>
                <option value="provisioned">Provisioned — role and scope assigned now</option>
                <option value="staged">Staged — no role yet, cannot log in until provisioned</option>
              </select>
            </Field>
            <Field label="Display name *"><input className={styles.inp} value={st.create.form.displayName} onChange={e => dispatch({ type: 'CREATE_FORM', form: { displayName: e.target.value } })} required /></Field>
            <Field label="Username *"><input className={styles.inp} value={st.create.form.username} onChange={e => dispatch({ type: 'CREATE_FORM', form: { username: e.target.value } })} autoComplete="off" required /></Field>
            <Field label="Initial password * (min 8 characters)"><input className={styles.inp} type="password" value={st.create.form.password} onChange={e => dispatch({ type: 'CREATE_FORM', form: { password: e.target.value } })} minLength={8} autoComplete="new-password" required /></Field>
            <Field label="Email"><input className={styles.inp} type="email" value={st.create.form.email} onChange={e => dispatch({ type: 'CREATE_FORM', form: { email: e.target.value } })} /></Field>
            <Field label="Phone"><input className={styles.inp} value={st.create.form.phone} onChange={e => dispatch({ type: 'CREATE_FORM', form: { phone: e.target.value } })} /></Field>
            <div className={styles.checks}>
              <label><input type="checkbox" checked={st.create.form.mustChangePassword} onChange={e => dispatch({ type: 'CREATE_FORM', form: { mustChangePassword: e.target.checked } })} /> Must change password on first login</label>
              {st.create.form.mode === 'provisioned' && (
                <label><input type="checkbox" checked={st.create.form.canLogin} onChange={e => dispatch({ type: 'CREATE_FORM', form: { canLogin: e.target.checked } })} /> Can log in</label>
              )}
            </div>
            {st.create.form.mode === 'provisioned' && (
              <ScopeFields
                roles={assignableRoles} branches={st.branches}
                roleId={st.create.form.roleId} scope={st.create.form.scope} branchId={st.create.form.branchId} showRole
                onRole={id => dispatch({ type: 'CREATE_FORM', form: { roleId: id } })}
                onScope={s => dispatch({ type: 'CREATE_FORM', form: { scope: s, ...(s === 'AllCompanyBranches' ? { branchId: null } : {}) } })}
                onBranch={b => dispatch({ type: 'CREATE_FORM', form: { branchId: b } })}
              />
            )}
            <Field label="Search Employees">
              <input className={styles.inp} placeholder="Name or employee key…" value={st.create.search}
                onChange={e => dispatch({ type: 'CREATE_SEARCH', val: e.target.value })} />
            </Field>
            <Field label={createIsDriver ? 'Employee * (a current Driver)' : 'Link to an Employee (optional)'}>
              <select className={styles.inp} value={st.create.form.employeeId ?? ''} required={createIsDriver}
                onChange={e => dispatch({ type: 'CREATE_PICK', employee: createCandidates.find(c => c.employee_id === parseInt(e.target.value)) ?? null })}>
                <option value="">{createIsDriver ? '— Select the current Driver —' : '— No Employee (stand-alone account) —'}</option>
                {createCandidates.map(c => <option key={c.employee_id} value={c.employee_id}>{candidateLabel(c)}</option>)}
              </select>
            </Field>
            {(st.create.candidatesLoading || st.create.resolving) && <p className={styles.helpTxt}>Loading Employees…</p>}
            {st.create.candidatesMore && (
              <button type="button" className={styles.linkBtn} onClick={() => props.loadMoreCandidates('create')}>Load more Employees</button>
            )}
            {createIsDriver && createCandidates.length === 0 && !st.create.candidatesLoading && (
              <p className={styles.helpTxt}>No current Driver is waiting for an account. Add the Driver in Workforce first.</p>
            )}
            {st.create.error && <ErrMsg msg={st.create.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'CREATE_CLOSE' })} loading={st.create.loading} label="Create account" />
          </form>
        </Modal>
      )}

      {/* ── Provision a staged account ── */}
      {st.provision.open && selected && (
        <Modal title="Provision account" onClose={() => dispatch({ type: 'PROVISION_CLOSE' })}>
          <form onSubmit={props.submitProvision} className={styles.form}>
            <p className={styles.helpTxt}>
              Give <strong>{selected.display_name}</strong> a role and scope. {selected.employee_id === null
                ? 'DRIVER access needs an Employee linked first.' : `Linked Employee: ${selected.employee_name ?? selected.employee_id}.`}
            </p>
            <ScopeFields
              roles={assignableRoles} branches={st.branches}
              roleId={st.provision.form.roleId} scope={st.provision.form.scope} branchId={st.provision.form.branchId} showRole
              onRole={id => dispatch({ type: 'PROVISION_FORM', form: { roleId: id } })}
              onScope={s => dispatch({ type: 'PROVISION_FORM', form: { scope: s, ...(s === 'AllCompanyBranches' ? { branchId: null } : {}) } })}
              onBranch={b => dispatch({ type: 'PROVISION_FORM', form: { branchId: b } })}
            />
            <div className={styles.checks}>
              <label><input type="checkbox" checked={st.provision.form.canLogin} onChange={e => dispatch({ type: 'PROVISION_FORM', form: { canLogin: e.target.checked } })} /> Can log in</label>
            </div>
            {provisionRole?.role_code === 'DRIVER' && selected.employee_id === null && (
              <p className={styles.helpTxt}>Link the current Driver Employee to this account first.</p>
            )}
            {st.provision.error && <ErrMsg msg={st.provision.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'PROVISION_CLOSE' })} loading={st.provision.loading} label="Provision" />
          </form>
        </Modal>
      )}

      {/* ── Link an Employee (an Access action with an Access-scoped candidate read) ── */}
      {st.link.open && selected && (
        <Modal title="Link Employee" onClose={() => dispatch({ type: 'LINK_CLOSE' })}>
          <form onSubmit={props.submitLink} className={styles.form}>
            <p className={styles.helpTxt}>Link <strong>{selected.display_name}</strong> to an Employee that has no account yet.</p>
            <Field label="Search Employees">
              <input className={styles.inp} placeholder="Name or employee key…" value={st.link.search} onChange={e => dispatch({ type: 'LINK_SEARCH', val: e.target.value })} />
            </Field>
            <Field label="Employee *">
              <select className={styles.inp} size={6} value={st.link.employeeId ?? ''} onChange={e => dispatch({ type: 'LINK_PICK', employeeId: e.target.value ? parseInt(e.target.value) : null })}>
                {linkCandidates.map(c => <option key={c.employee_id} value={c.employee_id}>{candidateLabel(c)}</option>)}
              </select>
            </Field>
            {st.link.candidatesLoading && <p className={styles.helpTxt}>Loading Employees…</p>}
            {st.link.candidatesMore && (
              <button type="button" className={styles.linkBtn} onClick={() => props.loadMoreCandidates('link')}>Load more Employees</button>
            )}
            {!st.link.candidatesLoading && linkCandidates.length === 0 && <p className={styles.helpTxt}>No Employee without an account matches.</p>}
            {st.link.error && <ErrMsg msg={st.link.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'LINK_CLOSE' })} loading={st.link.loading} label="Link Employee" />
          </form>
        </Modal>
      )}

      {st.edit.open && selected && (
        <Modal title="Edit account" onClose={() => dispatch({ type: 'EDIT_CLOSE' })}>
          <form onSubmit={props.submitEdit} className={styles.form}>
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
          <form onSubmit={props.submitReset} className={styles.form}>
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
          <form onSubmit={props.submitChangeRole} className={styles.form}>
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
          <form onSubmit={props.submitExtraPerms} className={styles.form}>
            <p className={styles.helpTxt}>Extra permissions for <strong>{selected.display_name}</strong>. These are added on top of the role's permissions. Use only for exceptions.</p>
            <PermissionPicker allPerms={st.allPerms} rolePerms={new Set(selected.role_permission_codes)} extraPerms={st.extraPerms.codes}
              onToggle={(code, checked) => dispatch({ type: 'EP_TOGGLE', code, checked })} />
            {st.extraPerms.error && <ErrMsg msg={st.extraPerms.error} />}
            <ModalFoot onCancel={() => dispatch({ type: 'EP_CLOSE' })} loading={st.extraPerms.loading} label="Save Extra Permissions" />
          </form>
        </Modal>
      )}

      {st.confirmToggle.open && (
        <ConfirmDialog
          open={st.confirmToggle.open}
          title={st.confirmToggle.toActive ? 'Activate account' : 'Deactivate account'}
          message={st.confirmToggle.toActive ? 'This will restore access for this account.' : 'This will prevent this account from logging in.'}
          confirmLabel={st.confirmToggle.toActive ? 'Activate' : 'Deactivate'}
          variant={st.confirmToggle.toActive ? 'primary' : 'danger'}
          onConfirm={props.confirmToggleActive}
          onCancel={() => dispatch({ type: 'CONFIRM_TOGGLE_CLOSE' })}
        />
      )}

      {st.transfer.open && ownerTransferTarget && (
        <Modal title="Transfer Company Ownership" onClose={() => dispatch({ type: 'TRANSFER_CLOSE' })} wide>
          <form onSubmit={props.submitOwnerTransfer} className={styles.form}>
            <div className={styles.xferWarn}>
              <span>⚠️</span>
              <div>
                <p><strong>You are about to transfer Company Owner authority to {ownerTransferTarget.display_name}.</strong></p>
                <p>You will no longer be Company Owner after this transfer. This action is audited and cannot be undone without the new owner's cooperation.</p>
              </div>
            </div>
            <div className={styles.xferSummary}>
              <span className={styles.fl}>New owner</span>
              <strong>{ownerTransferTarget.display_name}</strong> ({ownerTransferTarget.username})
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
