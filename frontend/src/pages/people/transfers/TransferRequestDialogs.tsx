import type { Dispatch, FormEvent } from 'react';
import type { Branch } from '../../../types/core';
import type { DriverTransferRequest } from '../../../types/transfer';
import type { TabAction, TabState, DecideDialogState } from './transferRequestsState';
import styles from '../PeoplePage.module.css';
import trStyles from './TransferRequests.module.css';

interface Props {
  st: Pick<TabState, 'create' | 'approveDialog' | 'decideDialog' | 'cancelDialog' | 'rescheduleDialog'>;
  selected: DriverTransferRequest | null;
  branches: Branch[];
  dispatch: Dispatch<TabAction>;
  submitCreate(e: FormEvent): void;
  submitApproveSource(e: FormEvent): void;
  submitDecide(e: FormEvent): void;
  submitCancel(e: FormEvent): void;
  submitReschedule(e: FormEvent): void;
}

export function TransferRequestDialogs({ st, selected, branches, dispatch, submitCreate, submitApproveSource, submitDecide, submitCancel, submitReschedule }: Props) {
  return (
    <>
      {/* ── Create dialog ── */}
      {st.create.open && (
        <TrModal title="New Transfer Request" onClose={() => dispatch({ type: 'CREATE_CLOSE' })} wide>
          <form onSubmit={submitCreate} className={styles.form}>
            <p className={styles.helpTxt}>
              Create a branch-initiated transfer request. The request will go to
              <strong> PendingTargetApproval</strong> immediately (source approval is implicit).
            </p>

            <TrField label="Driver *">
              {st.create.driversLoading ? (
                <div className={trStyles.selectLoading}>Loading drivers…</div>
              ) : (
                <select
                  className={styles.inp}
                  value={st.create.driverId ?? ''}
                  onChange={e => dispatch({ type: 'CREATE_FIELD', field: 'driverId', val: e.target.value ? parseInt(e.target.value) : null })}
                  required
                >
                  <option value="">— Select a driver —</option>
                  {st.create.drivers.map(d => (
                    <option key={d.driver_id} value={d.driver_id!}>
                      {d.full_name}{d.driver_number ? ` (${d.driver_number})` : ''} · {d.source_branch_name}
                    </option>
                  ))}
                </select>
              )}
            </TrField>

            <TrField label="Target Branch *">
              <select
                className={styles.inp}
                value={st.create.targetBranchId ?? ''}
                onChange={e => dispatch({ type: 'CREATE_FIELD', field: 'targetBranchId', val: e.target.value ? parseInt(e.target.value) : null })}
                required
              >
                <option value="">— Select target branch —</option>
                {branches.map(b => (
                  <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>
                ))}
              </select>
            </TrField>

            <TrField label="Effective time (UTC, optional)">
              <p className={styles.helpTxt}>Leave blank to apply the transfer when it is completed.</p>
              <input
                className={styles.inp}
                type="datetime-local"
                step={60}
                value={st.create.effectiveAt}
                onChange={e => dispatch({ type: 'CREATE_FIELD', field: 'effectiveAt', val: e.target.value })}
              />
            </TrField>

            <TrField label="Reason">
              <input
                className={styles.inp}
                value={st.create.reason}
                placeholder="Optional reason for transfer"
                onChange={e => dispatch({ type: 'CREATE_FIELD', field: 'reason', val: e.target.value })}
              />
            </TrField>

            <TrField label="Notes">
              <textarea
                className={`${styles.inp} ${trStyles.textarea}`}
                value={st.create.notes}
                placeholder="Optional additional notes"
                onChange={e => dispatch({ type: 'CREATE_FIELD', field: 'notes', val: e.target.value })}
              />
            </TrField>

            {st.create.error && <ErrMsg msg={st.create.error} />}
            <TrModalFoot
              onCancel={() => dispatch({ type: 'CREATE_CLOSE' })}
              loading={st.create.loading}
              label="Create Request"
            />
          </form>
        </TrModal>
      )}

      {/* ── Approve Source dialog ── */}
      {st.approveDialog.open && selected && (
        <TrModal
          title={`Approve Source — ${selected.driver_name ?? 'Driver'}`}
          onClose={() => dispatch({ type: 'APPROVE_CLOSE' })}
        >
          <form onSubmit={submitApproveSource} className={styles.form}>
            <p className={styles.helpTxt}>
              Approving this request as the source branch will forward it to the target branch
              (<strong>{selected.target_branch_name}</strong>) for their decision.
            </p>
            <TrField label="Notes (optional)">
              <input
                className={styles.inp}
                value={st.approveDialog.notes}
                placeholder="Optional notes for target branch"
                onChange={e => dispatch({ type: 'APPROVE_NOTES', val: e.target.value })}
              />
            </TrField>
            {st.approveDialog.error && <ErrMsg msg={st.approveDialog.error} />}
            <TrModalFoot
              onCancel={() => dispatch({ type: 'APPROVE_CLOSE' })}
              loading={st.approveDialog.loading}
              label="Approve & Forward"
            />
          </form>
        </TrModal>
      )}

      {/* ── Decide Target dialog ── */}
      {st.decideDialog.open && selected && (
        <TrModal
          title={decideDialogTitle(st.decideDialog.decision, selected)}
          onClose={() => dispatch({ type: 'DECIDE_CLOSE' })}
        >
          <form onSubmit={submitDecide} className={styles.form}>
            <DecideBody decision={st.decideDialog.decision} req={selected} />
            <TrField label="Notes (optional)">
              <input
                className={styles.inp}
                value={st.decideDialog.notes}
                placeholder={
                  st.decideDialog.decision === 'Returned'
                    ? 'Explain what needs to change…'
                    : 'Optional notes'
                }
                onChange={e => dispatch({ type: 'DECIDE_FIELD', field: 'notes', val: e.target.value })}
              />
            </TrField>
            {st.decideDialog.error && <ErrMsg msg={st.decideDialog.error} />}
            <TrModalFoot
              onCancel={() => dispatch({ type: 'DECIDE_CLOSE' })}
              loading={st.decideDialog.loading}
              label={decideSubmitLabel(st.decideDialog.decision)}
              danger={st.decideDialog.decision === 'Rejected'}
            />
          </form>
        </TrModal>
      )}

      {/* ── Reschedule dialog (Scheduled requests that have not taken effect) ── */}
      {st.rescheduleDialog.open && selected && (
        <TrModal
          title="Reschedule Transfer"
          onClose={() => dispatch({ type: 'RESCHEDULE_CLOSE' })}
        >
          <form onSubmit={submitReschedule} className={styles.form}>
            <p className={styles.helpTxt}>
              Move the transfer of <strong>{selected.driver_name}</strong> to a new future time. The
              driver stays in <strong>{selected.source_branch_name}</strong> until then and joins
              <strong> {selected.target_branch_name}</strong> at the new time.
            </p>
            <TrField label="New effective time (UTC) *">
              <input
                className={styles.inp}
                type="datetime-local"
                step={60}
                value={st.rescheduleDialog.effectiveAt}
                required
                onChange={e => dispatch({ type: 'RESCHEDULE_AT', val: e.target.value })}
              />
            </TrField>
            {st.rescheduleDialog.error && <ErrMsg msg={st.rescheduleDialog.error} />}
            <TrModalFoot
              onCancel={() => dispatch({ type: 'RESCHEDULE_CLOSE' })}
              loading={st.rescheduleDialog.loading}
              label="Reschedule"
            />
          </form>
        </TrModal>
      )}

      {/* ── Cancel dialog ── */}
      {st.cancelDialog.open && selected && (
        <TrModal
          title="Cancel Transfer Request"
          onClose={() => dispatch({ type: 'CANCEL_CLOSE' })}
        >
          <form onSubmit={submitCancel} className={styles.form}>
            <p className={styles.helpTxt}>
              Cancel the transfer request for <strong>{selected.driver_name}</strong>.
              This cannot be undone. The driver will remain in <strong>{selected.source_branch_name}</strong>
              {selected.status === 'Scheduled' ? ' and the scheduled assignment will be removed' : ''}.
            </p>
            <TrField label="Reason (optional)">
              <input
                className={styles.inp}
                value={st.cancelDialog.reason}
                placeholder="Why is this request being cancelled?"
                onChange={e => dispatch({ type: 'CANCEL_REASON', val: e.target.value })}
              />
            </TrField>
            {st.cancelDialog.error && <ErrMsg msg={st.cancelDialog.error} />}
            <TrModalFoot
              onCancel={() => dispatch({ type: 'CANCEL_CLOSE' })}
              loading={st.cancelDialog.loading}
              label="Cancel Request"
              danger
            />
          </form>
        </TrModal>
      )}
    </>
  );
}

function decideDialogTitle(d: DecideDialogState['decision'], req: DriverTransferRequest): string {
  switch (d) {
    case 'Approved': return `Approve Transfer — ${req.driver_name ?? 'Driver'}`;
    case 'Rejected': return `Reject Transfer — ${req.driver_name ?? 'Driver'}`;
    case 'Returned': return `Return for Changes — ${req.driver_name ?? 'Driver'}`;
  }
}

function decideSubmitLabel(d: DecideDialogState['decision']): string {
  switch (d) {
    case 'Approved': return 'Approve Transfer';
    case 'Rejected': return 'Reject Transfer';
    case 'Returned': return 'Return for Changes';
  }
}

function DecideBody({ decision, req }: { decision: DecideDialogState['decision']; req: DriverTransferRequest }) {
  switch (decision) {
    case 'Approved':
      return (
        <p className={styles.helpTxt}>
          Approve the transfer of <strong>{req.driver_name}</strong> from{' '}
          <strong>{req.source_branch_name}</strong> to <strong>{req.target_branch_name}</strong>.
          After approval, an authorized user can complete the transfer to schedule or apply the new assignment.
        </p>
      );
    case 'Rejected':
      return (
        <div className={trStyles.decideWarn}>
          <strong>Rejecting this request is final.</strong> The request will be closed.
          The driver remains in <strong>{req.source_branch_name}</strong>.
          A new request must be created if the transfer should be reconsidered.
        </div>
      );
    case 'Returned':
      return (
        <p className={styles.helpTxt}>
          Return this request to <strong>{req.source_branch_name}</strong> for changes.
          They will be able to re-approve it once the issue is resolved. Provide a reason below.
        </p>
      );
  }
}

// ─── Small primitives ────────────────────────────────────────────────────────

function TrModal({
  title, onClose, children, wide,
}: {
  title: string; onClose(): void; children: React.ReactNode; wide?: boolean;
}) {
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

function TrField({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className={styles.field}>
      <label className={styles.lbl}>{label}</label>
      {children}
    </div>
  );
}

function ErrMsg({ msg }: { msg: string }) {
  return <div className={styles.errMsg}>{msg}</div>;
}

function TrModalFoot({
  onCancel, loading, label, danger,
}: {
  onCancel(): void; loading: boolean; label: string; danger?: boolean;
}) {
  return (
    <div className={styles.mf}>
      <button type="button" className={styles.cancelBtn} onClick={onCancel}>Cancel</button>
      <button
        type="submit"
        className={danger ? styles.dangerBtn : styles.primaryBtn}
        disabled={loading}
      >
        {loading ? 'Saving…' : label}
      </button>
    </div>
  );
}
