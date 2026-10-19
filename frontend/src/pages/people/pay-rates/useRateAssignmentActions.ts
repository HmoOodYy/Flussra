import { useState } from 'react';
import { voidAssignment } from '../../../lib/compensationApi';
import { apiError, type HistoryRow } from './payRatesView';

export function useRateAssignmentActions(
  selectedDriverAssignmentId: number | null,
  reloadAll: (driverAssignmentId: number) => Promise<void>,
) {
  const [actioningId, setActioningId] = useState<number | null>(null);
  const [pendingActionError, setPendingActionError] = useState('');
  const [voidTarget, setVoidTarget] = useState<HistoryRow | null>(null);
  const [voidReason, setVoidReason] = useState('');
  const [voidError, setVoidError] = useState('');
  const [voiding, setVoiding] = useState(false);

  async function runPendingAction(id: number, action: () => Promise<unknown>) {
    if (!selectedDriverAssignmentId) return;
    setActioningId(id);
    setPendingActionError('');
    try {
      await action();
      await reloadAll(selectedDriverAssignmentId);
    } catch (err) {
      setPendingActionError(apiError(err));
    } finally {
      setActioningId(null);
    }
  }

  async function executeVoid() {
    if (!voidTarget || !selectedDriverAssignmentId) return;
    if (!voidReason.trim()) {
      setVoidError('A reason is required.');
      return;
    }
    setVoiding(true);
    setVoidError('');
    try {
      await voidAssignment(voidTarget.driver_rate_assignment_id, voidReason.trim());
      setVoidTarget(null);
      setVoidReason('');
      await reloadAll(selectedDriverAssignmentId);
    } catch (err) {
      setVoidError(apiError(err));
    } finally {
      setVoiding(false);
    }
  }

  return { actioningId, pendingActionError, setPendingActionError, voidTarget, setVoidTarget, voidReason, setVoidReason, voidError, setVoidError, voiding, runPendingAction, executeVoid };
}
