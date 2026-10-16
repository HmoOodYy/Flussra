import type { FormEvent } from 'react';
import type { Branch } from '../../types/core';
import type { CompanyRole, Permission } from '../../types/admin';
import type { Action, WizState } from './peoplePageState';
import { ErrMsg, Field, PermissionPicker, ScopeFields } from './PeopleFormControls';
import { scopeLabel } from './peoplePresentation';
import styles from './PeoplePage.module.css';

// ─── WizardModal ──────────────────────────────────────────────────────────────

export interface WizardModalProps {
  wiz: WizState;
  roles: CompanyRole[];
  branches: Branch[];
  allPerms: Permission[];
  dispatch(a: Action): void;
  onStep1(e: FormEvent): void;
  onSelectRole(roleId: number): void;
  onStep3(): void;
  onStep4(): void;
  onFinish(): void;
}

const WIZ_STEPS = ['Person Info', 'Role', 'Scope', 'Extra Permissions', 'Review'] as const;

export function WizardModal({ wiz, roles, branches, allPerms, dispatch, onStep1, onSelectRole, onStep3, onStep4, onFinish }: WizardModalProps) {
  const selectedRole = roles.find(r => r.company_role_id === wiz.roleId);
  const isDriver = selectedRole?.role_code === 'DRIVER';
  // Role permissions for the selected role (for the extra perms step)
  // We don't have role perms in wiz state, but for the wizard we'll show all perms selectable
  // (role perms will be shown after creation in the detail panel)

  return (
    <div className={styles.overlay}>
      <div className={`${styles.modal} ${styles.wizModal}`}>
        <div className={styles.mHeader}>
          <h2>Add Person</h2>
          <button className={styles.closeBtn} onClick={() => dispatch({ type: 'WIZ_CLOSE' })}>✕</button>
        </div>

        {/* Step indicators */}
        <div className={styles.steps}>
          {WIZ_STEPS.map((label, i) => {
            const n = i + 1;
            const done = wiz.step > n;
            const active = wiz.step === n;
            return (
              <div key={label} className={`${styles.step}${active ? ` ${styles.stepActive}` : ''}${done ? ` ${styles.stepDone}` : ''}`}>
                <div className={styles.stepNum}>{done ? '✓' : n}</div>
                <div className={styles.stepLabel}>{label}</div>
              </div>
            );
          })}
        </div>

        <div className={styles.mBody}>

          {/* Step 1: Person info */}
          {wiz.step === 1 && (
            <form onSubmit={onStep1} className={styles.form}>
              <Field label="Display Name *"><input className={styles.inp} value={wiz.name} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'name', val: e.target.value })} placeholder="Full name" required /></Field>
              <Field label="Username *"><input className={styles.inp} value={wiz.username} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'username', val: e.target.value })} placeholder="lowercase username" required /></Field>
              <Field label="Password *"><input className={styles.inp} type="password" value={wiz.password} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'password', val: e.target.value })} minLength={8} required /></Field>
              <Field label="Email"><input className={styles.inp} type="email" value={wiz.email} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'email', val: e.target.value })} /></Field>
              <Field label="Phone"><input className={styles.inp} value={wiz.phone} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'phone', val: e.target.value })} /></Field>
              <div className={styles.checks}>
                <label><input type="checkbox" checked={wiz.canLogin} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'canLogin', val: e.target.checked })} /> Enable login after provisioning</label>
                <label><input type="checkbox" checked={wiz.mustChange} onChange={e => dispatch({ type: 'WIZ_FIELD', field: 'mustChange', val: e.target.checked })} /> Must change password on first login</label>
              </div>
              {wiz.error && <ErrMsg msg={wiz.error} />}
              <div className={styles.mf}>
                <button type="button" className={styles.cancelBtn} onClick={() => dispatch({ type: 'WIZ_CLOSE' })}>Cancel</button>
                <button type="submit" className={styles.primaryBtn} disabled={wiz.loading}>{wiz.loading ? 'Creating…' : 'Next: Role →'}</button>
              </div>
            </form>
          )}

          {/* Step 2: Role */}
          {wiz.step === 2 && (
            <div className={styles.form}>
              <p className={styles.helpTxt}><strong>Role</strong> determines what this person can do in the system. Company Owner is only transferable, not assignable here.</p>
              <div className={styles.roleGrid}>
                {roles.map(r => (
                  <button key={r.company_role_id} type="button"
                    className={`${styles.roleCard}${wiz.roleId === r.company_role_id ? ` ${styles.roleCardSel}` : ''}`}
                    onClick={() => onSelectRole(r.company_role_id)}
                  >
                    <div className={styles.rcName}>{r.role_name}</div>
                    {r.role_code === 'DRIVER' && <div className={styles.rcHint}>Requires an existing current Driver profile</div>}
                  </button>
                ))}
              </div>
              {wiz.error && <ErrMsg msg={wiz.error} />}
              <div className={styles.mf}>
                <button type="button" className={styles.cancelBtn} onClick={() => dispatch({ type: 'WIZ_STEP', step: 1 })}>← Back</button>
                <button type="button" className={styles.primaryBtn} disabled={!wiz.roleId} onClick={() => dispatch({ type: 'WIZ_STEP', step: 3 })}>Next: Scope →</button>
              </div>
            </div>
          )}

          {/* Step 3: Scope */}
          {wiz.step === 3 && (
            <div className={styles.form}>
              <p className={styles.helpTxt}><strong>Scope</strong> defines where this person's access applies — all branches or a specific one.</p>
              {isDriver ? <>
                <Field label="Existing Driver Employee *">
                  <select className={styles.inp} value={wiz.employeeId ?? ''} onChange={e => {
                    const employee = wiz.employeeOptions.find(item => item.employee_id === Number(e.target.value));
                    dispatch({ type: 'WIZ_FIELD', field: 'employeeId', val: employee?.employee_id ?? null });
                    dispatch({ type: 'WIZ_FIELD', field: 'scope', val: 'Self' });
                    dispatch({ type: 'WIZ_FIELD', field: 'branchId', val: null });
                  }}>
                    <option value="">Select an Employee</option>
                    {wiz.employeeOptions.map(employee => <option key={employee.employee_id} value={employee.employee_id}>{employee.full_name} · {employee.branch_name}</option>)}
                  </select>
                </Field>
                <p className={styles.helpTxt}>DRIVER Self access links an existing Employee to the Employee's current effective Driver profile.</p>
              </> : <ScopeFields roles={roles} branches={branches} roleId={wiz.roleId} scope={wiz.scope} branchId={wiz.branchId} showRole={false}
                onRole={() => {}}
                onScope={s => { dispatch({ type: 'WIZ_FIELD', field: 'scope', val: s }); if (s === 'AllCompanyBranches') dispatch({ type: 'WIZ_FIELD', field: 'branchId', val: null }); }}
                onBranch={b => dispatch({ type: 'WIZ_FIELD', field: 'branchId', val: b })}
              />}
              {wiz.partialSuccess && <div className={styles.partialWarn}>The account remains staged. Correct the role, scope, or Employee selection and retry; the User will not be recreated.</div>}
              {wiz.error && <ErrMsg msg={wiz.error} />}
              <div className={styles.mf}>
                <button type="button" className={styles.cancelBtn} onClick={() => dispatch({ type: 'WIZ_STEP', step: 2 })}>← Back</button>
                <button type="button" className={styles.primaryBtn} disabled={wiz.loading} onClick={onStep3}>{wiz.loading ? 'Saving…' : 'Next: Extra Permissions →'}</button>
              </div>
            </div>
          )}

          {/* Step 4: Extra permissions */}
          {wiz.step === 4 && (
            <div className={styles.form}>
              <p className={styles.helpTxt}><strong>Extra permissions</strong> are optional additions on top of the role. Permissions included by the role are shown as read-only.</p>
              <PermissionPicker
                allPerms={allPerms}
                rolePerms={new Set(wiz.createdUser?.role_permission_codes ?? [])}
                extraPerms={wiz.extraCodes}
                onToggle={(code, checked) => dispatch({ type: 'WIZ_EXTRA_TOGGLE', code, checked })}
              />
              {wiz.error && <ErrMsg msg={wiz.error} />}
              <div className={styles.mf}>
                <button type="button" className={styles.cancelBtn} onClick={() => dispatch({ type: 'WIZ_STEP', step: 3 })}>← Back</button>
                <button type="button" className={styles.primaryBtn} disabled={wiz.loading} onClick={onStep4}>{wiz.loading ? 'Saving…' : 'Next: Review →'}</button>
              </div>
            </div>
          )}

          {/* Step 5: Review */}
          {wiz.step === 5 && wiz.createdUser && (
            <div className={styles.form}>
              <div className={styles.reviewGrid}>
                <span className={styles.fl}>Name</span><span>{wiz.createdUser.display_name}</span>
                <span className={styles.fl}>Username</span><span>@{wiz.createdUser.username}</span>
                <span className={styles.fl}>Account</span><span>{wiz.createdUser.is_staged ? 'Staged — role and scope required' : 'Provisioned'}</span>
                <span className={styles.fl}>Can login</span><span>{wiz.createdUser.can_login ? 'Yes' : 'No'}</span>
                <span className={styles.fl}>Role</span><span>{wiz.createdUser.company_role_name ?? '— not assigned —'}</span>
                <span className={styles.fl}>Scope</span><span>{scopeLabel(wiz.createdUser.company_role_scope, wiz.createdUser.company_role_branch_name)}</span>
                <span className={styles.fl}>Extra permissions</span>
                <span>{wiz.createdUser.extra_permission_codes.length > 0 ? `${wiz.createdUser.extra_permission_codes.length} permission(s)` : 'None'}</span>
              </div>
              {isDriver && (
                <div className={styles.driverNext}>🚛 <strong>Driver access provisioned.</strong></div>
              )}
              <div className={styles.mf}>
                <button type="button" className={styles.primaryBtn} onClick={onFinish}>Done</button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
