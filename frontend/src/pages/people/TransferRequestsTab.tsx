/**
 * TransferRequestsTab — Driver Transfer Requests UI
 *
 * Layout mirrors PeoplePage: left panel (filter + list), right panel (detail).
 *
 * Left  : filter pills, search, transfer request cards
 * Right : timeline + actions + notes; or Create Request dialog
 *
 * Permissions:
 *   - canViewTransfers  → can open this tab at all
 *   - canEditTransfers  → can approve/decide/complete/cancel/create
 *   Backend is authoritative; frontend gates are UI conveniences only.
 */
import { useEffect, useReducer, useRef, useCallback } from 'react';
import type { FormEvent } from 'react';
import apiClient from '../../lib/apiClient';
import { useAuth } from '../../store/authStore';
import { canEditTransfers } from '../../lib/permissions';
import {
  listTransfers,
  approveSource,
  decideTarget,
  completeTransfer,
  cancelTransfer,
  createTransfer,
} from '../../lib/transferApi';
import type {
  DriverTransferRequest,
  TargetDecisionRequest,
} from '../../types/transfer';
import {
  statusLabel,
  statusKind,
  TRANSFER_ACTIVE,
  TRANSFER_TERMINAL,
} from '../../types/transfer';
import type { Branch, PersonSummary } from '../../types/core';
import styles from './PeoplePage.module.css';
import trStyles from './TransferRequestsTab.module.css';

// ─── Helpers ─────────────────────────────────────────────────────────────────

function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d))
    return d
      .map((i: unknown) =>
        i && typeof i === 'object' && 'msg' in i ? String((i as { msg: unknown }).msg) : null,
      )
      .filter(Boolean)
      .join(' ');
  return 'An unexpected error occurred.';
}

function fmtDate(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  });
}

function fmtDateTime(s: string | null | undefined): string {
  if (!s) return '—';
  return new Date(s).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

// ─── Filter types ─────────────────────────────────────────────────────────────

type FilterView = 'all' | 'active' | 'pending_source' | 'pending_target' | 'returned' | 'approved' | 'completed' | 'terminal';

const FILTER_LABELS: Record<FilterView, string> = {
  all:            'All',
  active:         'Active',
  pending_source: 'Awaiting Source',
  pending_target: 'Awaiting Target',
  returned:       'Returned',
  approved:       'Ready to Complete',
  completed:      'Completed',
  terminal:       'Closed',
};

function matchesFilter(req: DriverTransferRequest, filter: FilterView): boolean {
  switch (filter) {
    case 'all':            return true;
    case 'active':         return TRANSFER_ACTIVE.has(req.status);
    case 'pending_source': return req.status === 'PendingSourceApproval';
    case 'pending_target': return req.status === 'PendingTargetApproval';
    case 'returned':       return req.status === 'Returned';
    case 'approved':       return req.status === 'Approved';
    case 'completed':      return req.status === 'Completed';
    case 'terminal':       return TRANSFER_TERMINAL.has(req.status);
  }
}

// ─── State ────────────────────────────────────────────────────────────────────

interface TabState {
  requests: DriverTransferRequest[];
  loading: boolean;
  loadErr: string | null;
  selectedId: number | null;
  search: string;
  filter: FilterView;

  // Create dialog
  create: CreateState;

  // Action dialogs
  decideDialog: DecideDialogState;
  cancelDialog: CancelDialogState;
  approveDialog: { open: boolean; loading: boolean; error: string | null; notes: string };

  // Per-action loading for detail panel buttons
  actionLoading: boolean;
  actionError: string | null;

  // Toast
  toast: string | null;
}

interface CreateState {
  open: boolean; loading: boolean; error: string | null;
  drivers: PersonSummary[];
  driversLoading: boolean;
  driverId: number | null;
  targetBranchId: number | null;
  effectiveDate: string;
  reason: string;
  notes: string;
}

interface DecideDialogState {
  open: boolean; loading: boolean; error: string | null;
  decision: 'Approved' | 'Rejected' | 'Returned';
  notes: string;
}

interface CancelDialogState {
  open: boolean; loading: boolean; error: string | null;
  reason: string;
}

const CREATE0: CreateState = {
  open: false, loading: false, error: null,
  drivers: [], driversLoading: false,
  driverId: null, targetBranchId: null,
  effectiveDate: '', reason: '', notes: '',
};
const DECIDE0: DecideDialogState = { open: false, loading: false, error: null, decision: 'Approved', notes: '' };
const CANCEL0: CancelDialogState = { open: false, loading: false, error: null, reason: '' };
const APPROVE0 = { open: false, loading: false, error: null, notes: '' };

const INITIAL: TabState = {
  requests: [], loading: true, loadErr: null,
  selectedId: null, search: '', filter: 'active',
  create: CREATE0,
  decideDialog: DECIDE0,
  cancelDialog: CANCEL0,
  approveDialog: APPROVE0,
  actionLoading: false, actionError: null,
  toast: null,
};

type TabAction =
  | { type: 'LOADED'; requests: DriverTransferRequest[] }
  | { type: 'LOAD_ERR'; err: string }
  | { type: 'SELECT'; id: number | null }
  | { type: 'SEARCH'; val: string }
  | { type: 'FILTER'; val: FilterView }
  | { type: 'UPDATE'; req: DriverTransferRequest }
  | { type: 'ADD'; req: DriverTransferRequest }
  | { type: 'TOAST'; msg: string | null }
  | { type: 'ACTION_LOADING'; val: boolean }
  | { type: 'ACTION_ERR'; err: string | null }
  // Create dialog
  | { type: 'CREATE_OPEN' }
  | { type: 'CREATE_CLOSE' }
  | { type: 'CREATE_DRIVERS'; drivers: PersonSummary[] }
  | { type: 'CREATE_DRIVERS_LOADING'; val: boolean }
  | { type: 'CREATE_FIELD'; field: keyof Omit<CreateState, 'drivers' | 'driversLoading'>; val: unknown }
  | { type: 'CREATE_LOADING'; val: boolean }
  | { type: 'CREATE_ERR'; err: string | null }
  // Approve dialog
  | { type: 'APPROVE_OPEN' }
  | { type: 'APPROVE_CLOSE' }
  | { type: 'APPROVE_LOADING'; val: boolean }
  | { type: 'APPROVE_ERR'; err: string | null }
  | { type: 'APPROVE_NOTES'; val: string }
  // Decide dialog
  | { type: 'DECIDE_OPEN'; decision: DecideDialogState['decision'] }
  | { type: 'DECIDE_CLOSE' }
  | { type: 'DECIDE_LOADING'; val: boolean }
  | { type: 'DECIDE_ERR'; err: string | null }
  | { type: 'DECIDE_FIELD'; field: keyof DecideDialogState; val: unknown }
  // Cancel dialog
  | { type: 'CANCEL_OPEN' }
  | { type: 'CANCEL_CLOSE' }
  | { type: 'CANCEL_LOADING'; val: boolean }
  | { type: 'CANCEL_ERR'; err: string | null }
  | { type: 'CANCEL_REASON'; val: string };

function reducer(st: TabState, a: TabAction): TabState {
  switch (a.type) {
    case 'LOADED': return { ...st, loading: false, loadErr: null, requests: a.requests };
    case 'LOAD_ERR': return { ...st, loading: false, loadErr: a.err };
    case 'SELECT': return { ...st, selectedId: a.id, actionError: null };
    case 'SEARCH': return { ...st, search: a.val };
    case 'FILTER': return { ...st, filter: a.val };
    case 'UPDATE': return {
      ...st,
      requests: st.requests.map(r => r.transfer_request_id === a.req.transfer_request_id ? a.req : r),
      selectedId: st.selectedId === a.req.transfer_request_id ? st.selectedId : st.selectedId,
    };
    case 'ADD': return { ...st, requests: [a.req, ...st.requests], selectedId: a.req.transfer_request_id };
    case 'TOAST': return { ...st, toast: a.msg };
    case 'ACTION_LOADING': return { ...st, actionLoading: a.val };
    case 'ACTION_ERR': return { ...st, actionError: a.err, actionLoading: false };
    // Create
    case 'CREATE_OPEN': return { ...st, create: { ...CREATE0, open: true } };
    case 'CREATE_CLOSE': return { ...st, create: { ...st.create, open: false } };
    case 'CREATE_DRIVERS': return { ...st, create: { ...st.create, drivers: a.drivers, driversLoading: false } };
    case 'CREATE_DRIVERS_LOADING': return { ...st, create: { ...st.create, driversLoading: a.val } };
    case 'CREATE_FIELD': return { ...st, create: { ...st.create, [a.field]: a.val } };
    case 'CREATE_LOADING': return { ...st, create: { ...st.create, loading: a.val } };
    case 'CREATE_ERR': return { ...st, create: { ...st.create, error: a.err, loading: false } };
    // Approve
    case 'APPROVE_OPEN': return { ...st, approveDialog: { ...APPROVE0, open: true } };
    case 'APPROVE_CLOSE': return { ...st, approveDialog: { ...st.approveDialog, open: false } };
    case 'APPROVE_LOADING': return { ...st, approveDialog: { ...st.approveDialog, loading: a.val } };
    case 'APPROVE_ERR': return { ...st, approveDialog: { ...st.approveDialog, error: a.err, loading: false } };
    case 'APPROVE_NOTES': return { ...st, approveDialog: { ...st.approveDialog, notes: a.val } };
    // Decide
    case 'DECIDE_OPEN': return { ...st, decideDialog: { ...DECIDE0, open: true, decision: a.decision } };
    case 'DECIDE_CLOSE': return { ...st, decideDialog: { ...st.decideDialog, open: false } };
    case 'DECIDE_LOADING': return { ...st, decideDialog: { ...st.decideDialog, loading: a.val } };
    case 'DECIDE_ERR': return { ...st, decideDialog: { ...st.decideDialog, error: a.err, loading: false } };
    case 'DECIDE_FIELD': return { ...st, decideDialog: { ...st.decideDialog, [a.field]: a.val } };
    // Cancel
    case 'CANCEL_OPEN': return { ...st, cancelDialog: { ...CANCEL0, open: true } };
    case 'CANCEL_CLOSE': return { ...st, cancelDialog: { ...st.cancelDialog, open: false } };
    case 'CANCEL_LOADING': return { ...st, cancelDialog: { ...st.cancelDialog, loading: a.val } };
    case 'CANCEL_ERR': return { ...st, cancelDialog: { ...st.cancelDialog, error: a.err, loading: false } };
    case 'CANCEL_REASON': return { ...st, cancelDialog: { ...st.cancelDialog, reason: a.val } };
    default: return st;
  }
}

// ─── Props ────────────────────────────────────────────────────────────────────

interface Props {
  branches: Branch[];
}

// ─── Component ────────────────────────────────────────────────────────────────

export function TransferRequestsTab({ branches }: Props) {
  const { user } = useAuth();
  const [st, dispatch] = useReducer(reducer, INITIAL);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const canEdit = user ? canEditTransfers(user) : false;

  // Load on mount
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data = await listTransfers();
        if (!cancelled) dispatch({ type: 'LOADED', requests: data.items });
      } catch (e) {
        if (!cancelled) dispatch({ type: 'LOAD_ERR', err: apiError(e) });
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  function showToast(msg: string) {
    dispatch({ type: 'TOAST', msg });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => dispatch({ type: 'TOAST', msg: null }), 3500);
  }

  // Derived
  const q = st.search.toLowerCase();
  const filtered = st.requests.filter(r => {
    if (!matchesFilter(r, st.filter)) return false;
    if (q) {
      const hay = [
        r.driver_name, r.source_branch_name, r.target_branch_name, r.requested_by_name,
      ].map(s => (s ?? '').toLowerCase()).join(' ');
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const selected = st.selectedId != null
    ? st.requests.find(r => r.transfer_request_id === st.selectedId) ?? null
    : null;

  // ── Create: load drivers when dialog opens ──
  const openCreate = useCallback(async () => {
    dispatch({ type: 'CREATE_OPEN' });
    dispatch({ type: 'CREATE_DRIVERS_LOADING', val: true });
    try {
      const r = await apiClient.get<PersonSummary[]>('/core/people?employee_type=Driver');
      dispatch({ type: 'CREATE_DRIVERS', drivers: r.data.filter(p => p.driver_id != null && p.driver_status === 'Active') });
    } catch {
      dispatch({ type: 'CREATE_DRIVERS', drivers: [] });
    }
  }, []);

  // ── Create submit ──
  const submitCreate = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    const { create } = st;
    if (!create.driverId) { dispatch({ type: 'CREATE_ERR', err: 'Please select a driver.' }); return; }
    if (!create.targetBranchId) { dispatch({ type: 'CREATE_ERR', err: 'Please select a target branch.' }); return; }
    if (!create.effectiveDate) { dispatch({ type: 'CREATE_ERR', err: 'Effective date is required.' }); return; }
    dispatch({ type: 'CREATE_LOADING', val: true });
    try {
      const req = await createTransfer({
        driver_id:        create.driverId,
        target_branch_id: create.targetBranchId,
        effective_date:   create.effectiveDate,
        initiated_by:     'SourceBranch',
        reason:           create.reason.trim() || undefined,
        notes:            create.notes.trim() || undefined,
      });
      dispatch({ type: 'ADD', req });
      dispatch({ type: 'CREATE_CLOSE' });
      showToast(`Transfer request created for ${req.driver_name ?? 'driver'}.`);
    } catch (e) {
      dispatch({ type: 'CREATE_ERR', err: apiError(e) });
    }
  }, [st]);

  // ── Approve source ──
  const submitApproveSource = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    dispatch({ type: 'APPROVE_LOADING', val: true });
    try {
      const updated = await approveSource(selected.transfer_request_id, { notes: st.approveDialog.notes.trim() || undefined });
      dispatch({ type: 'UPDATE', req: updated });
      dispatch({ type: 'APPROVE_CLOSE' });
      showToast('Source approval submitted.');
    } catch (e) {
      dispatch({ type: 'APPROVE_ERR', err: apiError(e) });
    }
  }, [selected, st.approveDialog]);

  // ── Decide target ──
  const submitDecide = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    dispatch({ type: 'DECIDE_LOADING', val: true });
    const body: TargetDecisionRequest = {
      decision:       st.decideDialog.decision,
      decision_notes: st.decideDialog.notes.trim() || undefined,
    };
    try {
      const updated = await decideTarget(selected.transfer_request_id, body);
      dispatch({ type: 'UPDATE', req: updated });
      dispatch({ type: 'DECIDE_CLOSE' });
      showToast(`Transfer ${st.decideDialog.decision.toLowerCase()}.`);
    } catch (e) {
      dispatch({ type: 'DECIDE_ERR', err: apiError(e) });
    }
  }, [selected, st.decideDialog]);

  // ── Complete ──
  const doComplete = useCallback(async () => {
    if (!selected) return;
    dispatch({ type: 'ACTION_LOADING', val: true });
    try {
      const updated = await completeTransfer(selected.transfer_request_id);
      dispatch({ type: 'UPDATE', req: updated });
      dispatch({ type: 'ACTION_LOADING', val: false });
      showToast(`Transfer for ${updated.driver_name ?? 'driver'} completed. New profile created in ${updated.target_branch_name ?? 'target branch'}.`);
    } catch (e) {
      dispatch({ type: 'ACTION_ERR', err: apiError(e) });
    }
  }, [selected]);

  // ── Cancel ──
  const submitCancel = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    dispatch({ type: 'CANCEL_LOADING', val: true });
    try {
      const updated = await cancelTransfer(selected.transfer_request_id, {
        cancel_reason: st.cancelDialog.reason.trim() || undefined,
      });
      dispatch({ type: 'UPDATE', req: updated });
      dispatch({ type: 'CANCEL_CLOSE' });
      showToast('Transfer request cancelled.');
    } catch (e) {
      dispatch({ type: 'CANCEL_ERR', err: apiError(e) });
    }
  }, [selected, st.cancelDialog]);

  // ── Render ──

  if (st.loading) return <div className={styles.splash}>Loading transfer requests…</div>;
  if (st.loadErr) return <div className={styles.splash} style={{ color: '#dc2626' }}>Failed to load: {st.loadErr}</div>;

  return (
    <div className={styles.body}>

      {/* ── Left panel ── */}
      <aside className={styles.left}>
        <div className={styles.filters}>
          <input
            className={styles.search}
            placeholder="Search driver, branch…"
            value={st.search}
            onChange={e => dispatch({ type: 'SEARCH', val: e.target.value })}
          />
          {/* Filter pills — horizontal scroll */}
          <div className={trStyles.filterPills}>
            {(Object.keys(FILTER_LABELS) as FilterView[]).map(f => (
              <button
                key={f}
                className={`${trStyles.pill}${st.filter === f ? ` ${trStyles.pillActive}` : ''}`}
                onClick={() => dispatch({ type: 'FILTER', val: f })}
              >
                {FILTER_LABELS[f]}
                {f === 'active' && (
                  <span className={trStyles.pillCount}>
                    {st.requests.filter(r => TRANSFER_ACTIVE.has(r.status)).length}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>

        <div className={styles.list}>
          {filtered.length === 0 && (
            <div className={styles.emptyList}>
              {st.requests.length === 0
                ? 'No transfer requests found.'
                : 'No requests match your filters.'}
            </div>
          )}
          {filtered.map(req => (
            <TransferCard
              key={req.transfer_request_id}
              req={req}
              active={st.selectedId === req.transfer_request_id}
              onClick={() => dispatch({ type: 'SELECT', id: req.transfer_request_id })}
            />
          ))}
        </div>
        <div className={styles.listFoot}>{filtered.length} / {st.requests.length} requests</div>
      </aside>

      {/* ── Right panel ── */}
      <main className={styles.right}>
        {/* Create button bar (top of right panel) */}
        {canEdit && (
          <div className={trStyles.rightTopBar}>
            <button className={styles.addBtn} onClick={openCreate}>
              + New Transfer Request
            </button>
          </div>
        )}

        {!selected ? (
          <div className={styles.emptyRight}>
            <div className={styles.emptyRightCard}>
              <div className={styles.emptyRightIcon}>
                <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#94a3b8" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M5 12h14M12 5l7 7-7 7"/>
                </svg>
              </div>
              <p className={styles.emptyRightTitle}>No request selected</p>
              <p className={styles.emptyRightMsg}>
                Select a transfer request from the list to view its status, timeline, and available actions.
              </p>
            </div>
          </div>
        ) : (
          <TransferDetail
            req={selected}
            canEdit={canEdit}
            actionLoading={st.actionLoading}
            actionError={st.actionError}
            onApproveSource={() => dispatch({ type: 'APPROVE_OPEN' })}
            onDecide={(d) => dispatch({ type: 'DECIDE_OPEN', decision: d })}
            onComplete={doComplete}
            onCancel={() => dispatch({ type: 'CANCEL_OPEN' })}
            onClearError={() => dispatch({ type: 'ACTION_ERR', err: null })}
          />
        )}
      </main>

      {/* ── Toast ── */}
      {st.toast && <div className={styles.toast}>{st.toast}</div>}

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
                      {d.full_name} · {d.branch_name}
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

            <TrField label="Effective Date *">
              <input
                className={styles.inp}
                type="date"
                value={st.create.effectiveDate}
                onChange={e => dispatch({ type: 'CREATE_FIELD', field: 'effectiveDate', val: e.target.value })}
                required
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

      {/* ── Cancel dialog ── */}
      {st.cancelDialog.open && selected && (
        <TrModal
          title="Cancel Transfer Request"
          onClose={() => dispatch({ type: 'CANCEL_CLOSE' })}
        >
          <form onSubmit={submitCancel} className={styles.form}>
            <p className={styles.helpTxt}>
              Cancel the transfer request for <strong>{selected.driver_name}</strong>.
              This cannot be undone. The driver will remain in <strong>{selected.source_branch_name}</strong>.
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
    </div>
  );
}

// ─── TransferCard ─────────────────────────────────────────────────────────────

function TransferCard({
  req,
  active,
  onClick,
}: {
  req: DriverTransferRequest;
  active: boolean;
  onClick(): void;
}) {
  const kind = statusKind(req.status);
  return (
    <button
      className={`${styles.item}${active ? ` ${styles.itemActive}` : ''}`}
      onClick={onClick}
    >
      {/* Direction icon */}
      <div className={trStyles.cardIcon}>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M5 12h14M12 5l7 7-7 7"/>
        </svg>
      </div>

      <div className={styles.itemBody}>
        <div className={styles.itemName}>
          {req.driver_name ?? `Driver #${req.driver_id}`}
          <span className={`${trStyles.badge} ${trStyles[`badge_${kind}`]}`}>
            {statusLabel(req.status)}
          </span>
        </div>
        <div className={styles.itemMeta}>
          {req.source_branch_name ?? `Branch ${req.source_branch_id}`}
          {' → '}
          {req.target_branch_name ?? `Branch ${req.target_branch_id}`}
        </div>
        <div className={styles.itemRow}>
          <span className={styles.scopeChip}>
            {req.initiated_by === 'Driver' ? '🧑 Driver request' : '🏢 Branch initiated'}
          </span>
          <span className={styles.scopeChip}>Effective {fmtDate(req.effective_date)}</span>
        </div>
      </div>
    </button>
  );
}

// ─── TransferDetail ───────────────────────────────────────────────────────────

interface DetailProps {
  req: DriverTransferRequest;
  canEdit: boolean;
  actionLoading: boolean;
  actionError: string | null;
  onApproveSource(): void;
  onDecide(d: 'Approved' | 'Rejected' | 'Returned'): void;
  onComplete(): void;
  onCancel(): void;
  onClearError(): void;
}

function TransferDetail({
  req, canEdit, actionLoading, actionError,
  onApproveSource, onDecide, onComplete, onCancel, onClearError,
}: DetailProps) {
  const isTerminal = TRANSFER_TERMINAL.has(req.status);
  const kind = statusKind(req.status);

  return (
    <div className={styles.detail}>

      {/* Header card */}
      <div className={styles.dHeaderCard}>
        <div className={trStyles.detailIcon}>
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#6366f1" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M5 12h14M12 5l7 7-7 7"/>
          </svg>
        </div>
        <div className={styles.dHeaderInfo}>
          <div className={styles.dName}>
            {req.driver_name ?? `Driver #${req.driver_id}`}
            <span className={`${trStyles.badge} ${trStyles[`badge_${kind}`]}`}>
              {statusLabel(req.status)}
            </span>
          </div>
          <div className={styles.dSub}>
            {req.source_branch_name} → {req.target_branch_name}
          </div>
          <div className={styles.dBadges}>
            <span className={styles.scopeChip}>
              {req.initiated_by === 'Driver' ? '🧑 Driver requested' : '🏢 Branch initiated'}
            </span>
            <span className={styles.scopeChip}>Effective {fmtDate(req.effective_date)}</span>
            <span className={styles.scopeChip}>Requested {fmtDate(req.created_at_utc)}</span>
          </div>

          {/* Action buttons */}
          {canEdit && !isTerminal && (
            <div className={styles.dActions}>
              {(req.status === 'PendingSourceApproval' || req.status === 'Returned') && (
                <button className={`${styles.actBtn} ${styles.actBtnDriver}`} onClick={onApproveSource} disabled={actionLoading}>
                  ✓ Approve & Forward to Target
                </button>
              )}
              {req.status === 'PendingTargetApproval' && (
                <>
                  <button className={`${styles.actBtn} ${styles.actBtnDriver}`} onClick={() => onDecide('Approved')} disabled={actionLoading}>
                    ✓ Approve
                  </button>
                  <button className={styles.actBtn} onClick={() => onDecide('Returned')} disabled={actionLoading}>
                    ↩ Return for Changes
                  </button>
                  <button className={`${styles.actBtn} ${styles.actBtnDanger}`} onClick={() => onDecide('Rejected')} disabled={actionLoading}>
                    ✗ Reject
                  </button>
                </>
              )}
              {req.status === 'Approved' && (
                <button className={`${styles.actBtn} ${styles.actBtnDriver}`} onClick={onComplete} disabled={actionLoading}>
                  {actionLoading ? 'Completing…' : '🚀 Complete Transfer'}
                </button>
              )}
              {!isTerminal && (
                <button className={`${styles.actBtn} ${styles.actBtnDanger}`} onClick={onCancel} disabled={actionLoading}>
                  Cancel Request
                </button>
              )}
            </div>
          )}

          {actionError && (
            <div className={trStyles.actionErr}>
              {actionError}
              <button className={trStyles.actionErrClose} onClick={onClearError}>✕</button>
            </div>
          )}
        </div>
      </div>

      {/* Timeline */}
      <div className={styles.sec}>
        <h3 className={styles.secTitle}>Transfer Timeline</h3>
        <TransferTimeline req={req} />
      </div>

      {/* Details */}
      <div className={styles.sec}>
        <h3 className={styles.secTitle}>Request Details</h3>
        <div className={styles.grid2}>
          <span className={styles.fl}>Transfer ID</span>
          <span>#{req.transfer_request_id}</span>

          <span className={styles.fl}>Driver</span>
          <span>{req.driver_name ?? `Driver #${req.driver_id}`}</span>

          <span className={styles.fl}>Source branch</span>
          <span>{req.source_branch_name ?? `Branch ${req.source_branch_id}`}</span>

          <span className={styles.fl}>Target branch</span>
          <span>{req.target_branch_name ?? `Branch ${req.target_branch_id}`}</span>

          <span className={styles.fl}>Effective date</span>
          <span>{fmtDate(req.effective_date)}</span>

          <span className={styles.fl}>Requested by</span>
          <span>{req.requested_by_name ?? `User ${req.requested_by_user_id}`}</span>

          <span className={styles.fl}>Initiated by</span>
          <span>{req.initiated_by === 'Driver' ? 'Driver self-request' : 'Source branch'}</span>

          <span className={styles.fl}>Created</span>
          <span>{fmtDateTime(req.created_at_utc)}</span>

          {req.reason && (
            <>
              <span className={styles.fl}>Reason</span>
              <span>{req.reason}</span>
            </>
          )}
          {req.notes && (
            <>
              <span className={styles.fl}>Notes</span>
              <span>{req.notes}</span>
            </>
          )}
          {req.target_decision_notes && (
            <>
              <span className={styles.fl}>Target notes</span>
              <span>{req.target_decision_notes}</span>
            </>
          )}
          {req.cancel_reason && (
            <>
              <span className={styles.fl}>Cancel reason</span>
              <span>{req.cancel_reason}</span>
            </>
          )}
          {req.new_driver_id && (
            <>
              <span className={styles.fl}>New driver ID</span>
              <span>#{req.new_driver_id} (created in {req.target_branch_name})</span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Timeline ─────────────────────────────────────────────────────────────────

interface TimelineStep {
  label: string;
  sublabel: string | null;
  state: 'done' | 'active' | 'pending' | 'skipped';
}

function buildTimeline(req: DriverTransferRequest): TimelineStep[] {
  const s = req.status;

  const created: TimelineStep = {
    label: 'Request Created',
    sublabel: `${fmtDateTime(req.created_at_utc)}${req.requested_by_name ? ` by ${req.requested_by_name}` : ''}`,
    state: 'done',
  };

  const sourceApproval: TimelineStep = (() => {
    if (req.initiated_by === 'SourceBranch') {
      // Source approval is implicit — always done
      return {
        label: 'Source Branch Approved',
        sublabel: req.source_approved_at_utc
          ? `${fmtDateTime(req.source_approved_at_utc)} (branch-initiated)`
          : 'Branch-initiated (implicit approval)',
        state: 'done' as const,
      };
    }
    // Driver-initiated
    if (s === 'PendingSourceApproval') {
      return { label: 'Awaiting Source Approval', sublabel: 'Source branch must approve', state: 'active' as const };
    }
    if (s === 'Returned') {
      return {
        label: 'Source Approval — Returned',
        sublabel: 'Target returned for changes — re-approval needed',
        state: 'active' as const,
      };
    }
    return {
      label: 'Source Branch Approved',
      sublabel: req.source_approved_at_utc ? fmtDateTime(req.source_approved_at_utc) : null,
      state: 'done' as const,
    };
  })();

  const targetDecision: TimelineStep = (() => {
    if (s === 'PendingSourceApproval') {
      return { label: 'Target Branch Decision', sublabel: 'Waiting for source approval first', state: 'pending' as const };
    }
    if (s === 'Returned') {
      return {
        label: 'Target Returned Request',
        sublabel: req.target_decided_at_utc ? fmtDateTime(req.target_decided_at_utc) : null,
        state: 'done' as const,
      };
    }
    if (s === 'PendingTargetApproval') {
      return { label: 'Awaiting Target Decision', sublabel: 'Target branch must approve, reject, or return', state: 'active' as const };
    }
    if (s === 'Rejected') {
      return {
        label: 'Rejected by Target',
        sublabel: req.target_decided_at_utc ? fmtDateTime(req.target_decided_at_utc) : null,
        state: 'done' as const,
      };
    }
    return {
      label: 'Target Approved',
      sublabel: req.target_decided_at_utc ? fmtDateTime(req.target_decided_at_utc) : null,
      state: 'done' as const,
    };
  })();

  const completion: TimelineStep = (() => {
    if (s === 'Completed') {
      return {
        label: 'Transfer Completed',
        sublabel: req.completed_at_utc ? fmtDateTime(req.completed_at_utc) : null,
        state: 'done' as const,
      };
    }
    if (s === 'Cancelled') {
      return {
        label: 'Request Cancelled',
        sublabel: req.cancelled_at_utc ? fmtDateTime(req.cancelled_at_utc) : null,
        state: 'done' as const,
      };
    }
    if (s === 'Rejected') {
      return { label: 'Not Completed', sublabel: 'Request was rejected', state: 'skipped' as const };
    }
    if (s === 'Approved') {
      return { label: 'Ready to Complete', sublabel: 'Action required to finalize', state: 'active' as const };
    }
    return { label: 'Complete Transfer', sublabel: 'Pending prior steps', state: 'pending' as const };
  })();

  return [created, sourceApproval, targetDecision, completion];
}

function TransferTimeline({ req }: { req: DriverTransferRequest }) {
  const steps = buildTimeline(req);
  return (
    <div className={trStyles.timeline}>
      {steps.map((step, i) => (
        <div key={i} className={`${trStyles.tlStep} ${trStyles[`tlStep_${step.state}`]}`}>
          <div className={trStyles.tlLeft}>
            <div className={trStyles.tlDot} />
            {i < steps.length - 1 && <div className={trStyles.tlLine} />}
          </div>
          <div className={trStyles.tlContent}>
            <div className={trStyles.tlLabel}>{step.label}</div>
            {step.sublabel && <div className={trStyles.tlSub}>{step.sublabel}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Decide dialog helpers ────────────────────────────────────────────────────

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
          After approval, an authorized user can complete the transfer to create the new driver profile.
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
