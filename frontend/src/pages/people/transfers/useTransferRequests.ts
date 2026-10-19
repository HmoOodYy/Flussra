import { useCallback, useEffect, useReducer, useRef } from 'react';
import type { FormEvent } from 'react';
import type { Branch } from '../../../types/core';
import { canEditTransfers } from '../../../lib/permissions';
import {
  listTransfers, approveSource, decideTarget, completeTransfer, cancelTransfer, createTransfer,
  rescheduleTransfer, listTransferDriverPickerPeople,
} from '../../../lib/transferApi';
import type { TargetDecisionRequest } from '../../../types/transfer';
import { useAuth } from '../../../store/authStore';
import { apiError, fmtEffectiveAt, matchesFilter, normalizeUtcDateTimeLocal } from './transferRequestsView';
import { INITIAL, reducer } from './transferRequestsState';

export function useTransferRequests(branches: Branch[]) {
  const { user } = useAuth();
  const [st, dispatch] = useReducer(reducer, INITIAL);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const canEditAnyBranch = user ? branches.some(b => canEditTransfers(user, b.branch_id)) : false;

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
        r.driver_name, r.driver_number, r.source_branch_name, r.target_branch_name, r.requested_by_name,
      ].map(s => (s ?? '').toLowerCase()).join(' ');
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const selected = st.selectedId != null
    ? st.requests.find(r => r.transfer_request_id === st.selectedId) ?? null
    : null;

  // Per-action authority, mapped to the branch each backend guard actually checks.
  const canApproveSource = user && selected ? canEditTransfers(user, selected.source_branch_id) : false;
  const canDecideTarget = user && selected ? canEditTransfers(user, selected.target_branch_id) : false;
  const canActOnEitherBranch = user && selected
    ? canEditTransfers(user, selected.source_branch_id) || canEditTransfers(user, selected.target_branch_id)
    : false;

  // ── Create: load drivers when dialog opens ──
  const openCreate = useCallback(async () => {
    dispatch({ type: 'CREATE_OPEN' });
    dispatch({ type: 'CREATE_DRIVERS_LOADING', val: true });
    try {
      const r = await listTransferDriverPickerPeople();
      // Restrict to drivers whose source branch the user can edit, so the form never becomes submit-capable for an unauthorized branch.
      dispatch({
        type: 'CREATE_DRIVERS',
        drivers: r.data.filter(p =>
          p.driver_id != null && p.driver_status === 'Active' &&
          (user ? canEditTransfers(user, p.branch_id) : false)
        ),
      });
    } catch {
      dispatch({ type: 'CREATE_DRIVERS', drivers: [] });
    }
  }, [user]);

  // ── Create submit ──
  const submitCreate = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    const { create } = st;
    if (!create.driverId) { dispatch({ type: 'CREATE_ERR', err: 'Please select a driver.' }); return; }
    if (!create.targetBranchId) { dispatch({ type: 'CREATE_ERR', err: 'Please select a target branch.' }); return; }
    const parsedEffectiveAt = create.effectiveAt ? normalizeUtcDateTimeLocal(create.effectiveAt) : null;
    if (create.effectiveAt && !parsedEffectiveAt) {
      dispatch({ type: 'CREATE_ERR', err: 'Enter a valid effective time in UTC.' }); return;
    }
    const effectiveAt = parsedEffectiveAt ?? undefined;
    dispatch({ type: 'CREATE_LOADING', val: true });
    try {
      const req = await createTransfer({
        driver_id:        create.driverId,
        target_branch_id: create.targetBranchId,
        effective_at:     effectiveAt,
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
      // The server decides: a future EffectiveAt is Scheduled, an immediate one Completed.
      showToast(updated.status === 'Scheduled'
        ? `Transfer for ${updated.driver_name ?? 'driver'} scheduled for ${fmtEffectiveAt(updated.effective_at)}.`
        : `Transfer for ${updated.driver_name ?? 'driver'} completed. Now assigned to ${updated.target_branch_name ?? 'target branch'}.`);
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

  // ── Reschedule (Scheduled requests only; the new instant must be in the future) ──
  const submitReschedule = useCallback(async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    const effectiveAt = st.rescheduleDialog.effectiveAt
      ? normalizeUtcDateTimeLocal(st.rescheduleDialog.effectiveAt)
      : null;
    if (!effectiveAt) {
      dispatch({ type: 'RESCHEDULE_ERR', err: 'Enter a valid effective time in UTC.' });
      return;
    }
    dispatch({ type: 'RESCHEDULE_LOADING', val: true });
    try {
      const updated = await rescheduleTransfer(selected.transfer_request_id, { effective_at: effectiveAt });
      dispatch({ type: 'UPDATE', req: updated });
      dispatch({ type: 'RESCHEDULE_CLOSE' });
      showToast(`Transfer rescheduled for ${fmtEffectiveAt(updated.effective_at)}.`);
    } catch (e) {
      dispatch({ type: 'RESCHEDULE_ERR', err: apiError(e) });
    }
  }, [selected, st.rescheduleDialog]);

  return {
    st, dispatch, canEditAnyBranch, filtered, selected,
    canApproveSource, canDecideTarget, canActOnEitherBranch,
    openCreate, submitCreate, submitApproveSource, submitDecide, doComplete, submitCancel,
    submitReschedule,
  };
}
