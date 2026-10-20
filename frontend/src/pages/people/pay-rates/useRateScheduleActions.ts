import { useState } from 'react';
import { voidSchedule } from '../../../lib/compensationApi';
import { apiError, type HistoryRow } from './payRatesView';

export function useRateScheduleActions(
  selectedDriverId: number | null,
  reloadAll: (driverId: number) => Promise<void>,
) {
  const [actioningId, setActioningId] = useState<number | null>(null);
  const [pendingActionError, setPendingActionError] = useState('');
  const [voidTarget, setVoidTarget] = useState<HistoryRow | null>(null);
  const [voidReason, setVoidReason] = useState('');
  const [voidError, setVoidError] = useState('');
  const [voiding, setVoiding] = useState(false);

  async function runPendingAction(id: number, action: () => Promise<unknown>) {
    if (!selectedDriverId) return;
    setActioningId(id);
    setPendingActionError('');
    try {
      await action();
      await reloadAll(selectedDriverId);
    } catch (err) {
      setPendingActionError(apiError(err));
    } finally {
      setActioningId(null);
    }
  }

  async function executeVoid() {
    if (!voidTarget || !selectedDriverId) return;
    if (!voidReason.trim()) {
      setVoidError('A reason is required.');
      return;
    }
    setVoiding(true);
    setVoidError('');
    try {
      await voidSchedule(voidTarget.driver_rate_schedule_id, voidReason.trim());
      setVoidTarget(null);
      setVoidReason('');
      await reloadAll(selectedDriverId);
    } catch (err) {
      setVoidError(apiError(err));
    } finally {
      setVoiding(false);
    }
  }

  return { actioningId, pendingActionError, setPendingActionError, voidTarget, setVoidTarget, voidReason, setVoidReason, voidError, setVoidError, voiding, runPendingAction, executeVoid };
}
