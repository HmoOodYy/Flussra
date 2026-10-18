import type { DriverTransferRequest } from '../../../types/transfer';
import type { PersonSummary } from '../../../types/core';
import type { FilterView } from './transferRequestsView';

export interface TabState {
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
  effectiveAt: string;
  reason: string;
  notes: string;
}

export interface DecideDialogState {
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
  effectiveAt: '', reason: '', notes: '',
};
const DECIDE0: DecideDialogState = { open: false, loading: false, error: null, decision: 'Approved', notes: '' };
const CANCEL0: CancelDialogState = { open: false, loading: false, error: null, reason: '' };
const APPROVE0 = { open: false, loading: false, error: null, notes: '' };

export const INITIAL: TabState = {
  requests: [], loading: true, loadErr: null,
  selectedId: null, search: '', filter: 'active',
  create: CREATE0,
  decideDialog: DECIDE0,
  cancelDialog: CANCEL0,
  approveDialog: APPROVE0,
  actionLoading: false, actionError: null,
  toast: null,
};

export type TabAction =
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

export function reducer(st: TabState, a: TabAction): TabState {
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
