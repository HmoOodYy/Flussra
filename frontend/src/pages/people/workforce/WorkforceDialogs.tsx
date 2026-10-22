import { useState } from 'react';
import type { FormEvent } from 'react';
import {
  createDriverForEmployee,
  createEmployee,
  terminateEmployee,
  updateDriver,
  updateEmployee,
} from '../../../lib/workforceApi';
import type { Branch } from '../../../types/core';
import type { EmployeeDetail } from '../../../types/workforce';
import { ErrMsg, Field, Modal, ModalFoot } from '../PeopleFormControls';
import { apiError } from '../peoplePresentation';
import {
  EMPLOYEE_FORM0,
  buildCreatePayload,
  buildDriverUpdate,
  buildEmployeeUpdate,
  driverFormFrom,
  editFormFrom,
  employmentFieldsEditable,
  type CreateKind,
  type DriverForm,
  type EmployeeEditForm,
  type EmployeeForm,
} from './workforceModel';
import styles from '../PeoplePage.module.css';

interface Done { (detail: EmployeeDetail, message: string): void | Promise<void> }

// ─── Add Driver / Add Employee: one atomic request ───────────────────────────

export function EmployeeCreateDialog({ kind, branches, onClose, onDone }: {
  kind: CreateKind; branches: Branch[]; onClose(): void; onDone: Done;
}) {
  const [form, setForm] = useState<EmployeeForm>({ ...EMPLOYEE_FORM0, branchId: branches.length === 1 ? branches[0].branch_id : null });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isDriver = kind === 'driver';
  const set = (patch: Partial<EmployeeForm>) => { setForm((f) => ({ ...f, ...patch })); setError(null); };

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (loading) return;
    const payload = buildCreatePayload(kind, form);
    if (typeof payload === 'string') { setError(payload); return; }
    setLoading(true);
    try {
      const created = await createEmployee(payload);
      await onDone(created, isDriver ? `${created.full_name} added as a Driver.` : `${created.full_name} added.`);
    } catch (err) {
      // Nothing was created: the form keeps its values for a corrected retry.
      setError(apiError(err));
      setLoading(false);
    }
  }

  return (
    <Modal title={isDriver ? 'Add Driver' : 'Add Employee'} onClose={onClose}>
      <form onSubmit={submit} className={styles.form}>
        <p className={styles.helpTxt}>
          {isDriver
            ? 'Creates the Employee, the permanent Driver and the first home-Branch assignment together. No login is created: accounts are managed under Access.'
            : 'Creates an Employee with no Driver identity and no login.'}
        </p>
        <Field label="Home Branch *">
          <select className={styles.inp} value={form.branchId ?? ''} onChange={(e) => set({ branchId: e.target.value ? parseInt(e.target.value) : null })} required>
            <option value="">— Select a Branch —</option>
            {branches.map((b) => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
          </select>
        </Field>
        <Field label="Full name *"><input className={styles.inp} value={form.fullName} onChange={(e) => set({ fullName: e.target.value })} required /></Field>
        <Field label="Preferred name"><input className={styles.inp} value={form.preferredName} onChange={(e) => set({ preferredName: e.target.value })} /></Field>
        <Field label="Employee key (generated when empty)"><input className={styles.inp} value={form.employeeKey} onChange={(e) => set({ employeeKey: e.target.value })} /></Field>
        <Field label="Email"><input className={styles.inp} type="email" value={form.email} onChange={(e) => set({ email: e.target.value })} /></Field>
        <Field label="Phone"><input className={styles.inp} value={form.phone} onChange={(e) => set({ phone: e.target.value })} /></Field>
        <Field label="Hire date"><input className={styles.inp} type="date" value={form.hireDate} onChange={(e) => set({ hireDate: e.target.value })} /></Field>
        {isDriver && (
          <>
            <Field label="Driver number"><input className={styles.inp} value={form.driverNumber} onChange={(e) => set({ driverNumber: e.target.value })} /></Field>
            <Field label="CDL number"><input className={styles.inp} value={form.cdlNumber} onChange={(e) => set({ cdlNumber: e.target.value })} /></Field>
          </>
        )}
        {error && <ErrMsg msg={error} />}
        <ModalFoot onCancel={onClose} loading={loading} label={isDriver ? 'Add Driver' : 'Add Employee'} />
      </form>
    </Modal>
  );
}

// ─── Edit Employee ───────────────────────────────────────────────────────────

export function EmployeeEditDialog({ detail, branches, onClose, onDone }: {
  detail: EmployeeDetail; branches: Branch[]; onClose(): void; onDone: Done;
}) {
  const [form, setForm] = useState<EmployeeEditForm>(() => editFormFrom(detail));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<EmployeeEditForm>) => { setForm((f) => ({ ...f, ...patch })); setError(null); };
  const lifecycleEditable = employmentFieldsEditable(detail);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (loading) return;
    const update = buildEmployeeUpdate(detail, form);
    if (typeof update === 'string') { setError(update); return; }
    if (Object.keys(update).length === 0) { onClose(); return; }
    setLoading(true);
    try {
      await onDone(await updateEmployee(detail.employee_id, update), `${form.fullName.trim()} updated.`);
    } catch (err) { setError(apiError(err)); setLoading(false); }
  }

  return (
    <Modal title="Edit Employee" onClose={onClose}>
      <form onSubmit={submit} className={styles.form}>
        <Field label="Full name *"><input className={styles.inp} value={form.fullName} onChange={(e) => set({ fullName: e.target.value })} required /></Field>
        <Field label="Preferred name"><input className={styles.inp} value={form.preferredName} onChange={(e) => set({ preferredName: e.target.value })} /></Field>
        <Field label="Email"><input className={styles.inp} type="email" value={form.email} onChange={(e) => set({ email: e.target.value })} /></Field>
        <Field label="Phone"><input className={styles.inp} value={form.phone} onChange={(e) => set({ phone: e.target.value })} /></Field>
        <Field label="Hire date"><input className={styles.inp} type="date" value={form.hireDate} onChange={(e) => set({ hireDate: e.target.value })} /></Field>
        {lifecycleEditable ? (
          <>
            <Field label="Employment status">
              <select className={styles.inp} value={form.employmentStatus} onChange={(e) => set({ employmentStatus: e.target.value })}>
                <option value="Active">Active</option>
                <option value="Inactive">Inactive</option>
                <option value="Terminated">Terminated</option>
              </select>
            </Field>
            {form.employmentStatus === 'Terminated' && (
              <Field label="Termination date"><input className={styles.inp} type="date" value={form.terminationDate} onChange={(e) => set({ terminationDate: e.target.value })} /></Field>
            )}
            <Field label="Branch">
              <select className={styles.inp} value={form.branchId ?? ''} onChange={(e) => set({ branchId: e.target.value ? parseInt(e.target.value) : null })}>
                {branches.map((b) => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
              </select>
            </Field>
          </>
        ) : (
          <p className={styles.helpTxt}>
            This Employee is a Driver: employment status changes through Terminate, and the home Branch changes through a transfer request.
          </p>
        )}
        {error && <ErrMsg msg={error} />}
        <ModalFoot onCancel={onClose} loading={loading} label="Save Changes" />
      </form>
    </Modal>
  );
}

// ─── Add Driver identity to an existing Employee / edit the Driver ───────────

export function DriverDialog({ detail, mode, onClose, onDone }: {
  detail: EmployeeDetail; mode: 'add' | 'edit'; onClose(): void; onDone: Done;
}) {
  const [form, setForm] = useState<DriverForm>(() => driverFormFrom(mode === 'edit' ? detail.driver : null));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<DriverForm>) => { setForm((f) => ({ ...f, ...patch })); setError(null); };

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (loading) return;
    setLoading(true);
    try {
      if (mode === 'add') {
        const created = await createDriverForEmployee(detail.employee_id, {
          driver_number: form.driverNumber.trim() || null, cdl_number: form.cdlNumber.trim() || null,
        });
        await onDone(created, `${created.full_name} is now a Driver in ${created.branch_name}.`);
      } else if (detail.driver) {
        const update = buildDriverUpdate(detail.driver, form);
        if (Object.keys(update).length === 0) { onClose(); return; }
        await onDone(await updateDriver(detail.driver.driver_id, update), 'Driver details updated.');
      }
    } catch (err) { setError(apiError(err)); setLoading(false); }
  }

  return (
    <Modal title={mode === 'add' ? 'Add Driver identity' : 'Edit Driver'} onClose={onClose}>
      <form onSubmit={submit} className={styles.form}>
        {mode === 'add' && (
          <p className={styles.helpTxt}>
            Creates the permanent Driver and its first home assignment in <strong>{detail.branch_name}</strong>. Any later change of home Branch is a transfer.
          </p>
        )}
        <Field label="Driver number"><input className={styles.inp} value={form.driverNumber} onChange={(e) => set({ driverNumber: e.target.value })} /></Field>
        <Field label="CDL number"><input className={styles.inp} value={form.cdlNumber} onChange={(e) => set({ cdlNumber: e.target.value })} /></Field>
        {mode === 'edit' && (
          <Field label="Driver work status">
            <select className={styles.inp} value={form.status} onChange={(e) => set({ status: e.target.value })}>
              <option value="Active">Active</option>
              <option value="OnLeave">On leave</option>
              <option value="Inactive">Inactive</option>
            </select>
          </Field>
        )}
        {error && <ErrMsg msg={error} />}
        <ModalFoot onCancel={onClose} loading={loading} label={mode === 'add' ? 'Add Driver identity' : 'Save Driver'} />
      </form>
    </Modal>
  );
}

// ─── Terminate ───────────────────────────────────────────────────────────────

export function TerminateDialog({ detail, onClose, onDone }: {
  detail: EmployeeDetail; onClose(): void; onDone: Done;
}) {
  const [date, setDate] = useState('');
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (loading) return;
    if (!date) { setError('Choose the termination date.'); return; }
    if (!reason.trim()) { setError('A reason is required.'); return; }
    setLoading(true);
    try {
      const result = await terminateEmployee(detail.employee_id, { termination_date: date, reason: reason.trim() });
      const cancelled = result.cancelled_transfer_request_ids.length;
      await onDone(result, `${result.full_name} terminated${cancelled ? `; ${cancelled} scheduled transfer(s) cancelled` : ''}.`);
    } catch (err) { setError(apiError(err)); setLoading(false); }
  }

  return (
    <Modal title="Terminate employment" onClose={onClose}>
      <form onSubmit={submit} className={styles.form}>
        <div className={styles.xferWarn}>
          <span>⚠️</span>
          <div>
            <p><strong>Terminate {detail.full_name}?</strong></p>
            <p>Employment ends and the Driver's assignments close at the end of the termination day in each Branch's own time zone. A scheduled transfer taking effect after that is cancelled.</p>
          </div>
        </div>
        <Field label="Termination date *"><input className={styles.inp} type="date" value={date} onChange={(e) => { setDate(e.target.value); setError(null); }} required /></Field>
        <Field label="Reason *"><input className={styles.inp} value={reason} onChange={(e) => { setReason(e.target.value); setError(null); }} required /></Field>
        {error && <ErrMsg msg={error} />}
        <div className={styles.mf}>
          <button type="button" className={styles.cancelBtn} onClick={onClose}>Cancel</button>
          <button type="submit" className={styles.dangerBtn} disabled={loading}>{loading ? 'Terminating…' : 'Terminate'}</button>
        </div>
      </form>
    </Modal>
  );
}
