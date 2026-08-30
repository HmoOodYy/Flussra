/**
 * BonusDialog — period-level bonus management modal.
 *
 * Extracted from BonusPanel in PeriodDetailPage.tsx and promoted to a
 * standalone dialog. Fetches eligible drivers itself given a periodId.
 */
import { useEffect, useState, useCallback } from 'react';
import {
  createBonusEvent,
  getBonusSummary,
  voidBonusEvent,
} from '../../lib/payrollApi';
import type { BonusSummary } from '../../types/payroll';
import { useAuth } from '../../store/authStore';
import { canEntryPayroll } from '../../lib/permissions';
import styles from './BonusDialog.module.css';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const BONUS_EDITABLE_STATUSES = new Set(['Open', 'Returned']);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtAmount(v: string | null | undefined): string {
  if (v == null) return '—';
  const n = parseFloat(v);
  return isNaN(n) ? String(v) : `$${n.toFixed(2)}`;
}

function getErrorDetail(error: unknown, fallback: string): string {
  const detail =
    (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return fallback;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface BonusDialogProps {
  periodId: number;
  periodName?: string;
  periodStatus: string;
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function BonusDialog({ periodId, periodName, periodStatus, onClose }: BonusDialogProps) {
  const { user } = useAuth();
  const userCanEntry = user ? canEntryPayroll(user) : false;
  const [summary, setSummary] = useState<BonusSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [showAddForm, setShowAddForm] = useState(false);
  const [addDriverId, setAddDriverId] = useState('');
  const [addAmount, setAddAmount] = useState('');
  const [addReason, setAddReason] = useState('');
  const [addSaving, setAddSaving] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const [voidingId, setVoidingId] = useState<number | null>(null);
  const [confirmVoidId, setConfirmVoidId] = useState<number | null>(null);
  const [voidError, setVoidError] = useState<string | null>(null);

  // canEdit = period status allows bonus edits AND user has payroll.entry permission.
  const canEdit = BONUS_EDITABLE_STATUSES.has(periodStatus) && userCanEntry;

  const fetchSummary = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setSummary(await getBonusSummary(periodId));
    } catch (error: unknown) {
      setLoadError(getErrorDetail(error, 'Failed to load bonus events.'));
    } finally {
      setLoading(false);
    }
  }, [periodId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void fetchSummary();
  }, [fetchSummary]);

  // Close on Escape
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKey);
    return () => document.removeEventListener('keydown', handleKey);
  }, [onClose]);

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    if (!addDriverId || !addAmount || !addReason.trim()) return;
    setAddSaving(true);
    setAddError(null);
    try {
      await createBonusEvent(periodId, {
        driver_id: Number(addDriverId),
        amount: addAmount,
        reason: addReason.trim(),
      });
      setShowAddForm(false);
      setAddDriverId('');
      setAddAmount('');
      setAddReason('');
      await fetchSummary();
    } catch (err: unknown) {
      setAddError(getErrorDetail(err, 'Failed to add bonus event.'));
    } finally {
      setAddSaving(false);
    }
  }

  async function handleVoid(bonusEventId: number) {
    setVoidingId(bonusEventId);
    setVoidError(null);
    try {
      await voidBonusEvent(periodId, bonusEventId);
      setConfirmVoidId(null);
      await fetchSummary();
    } catch (error: unknown) {
      setVoidError(getErrorDetail(error, 'Failed to void bonus event.'));
      await fetchSummary();
    } finally {
      setVoidingId(null);
    }
  }

  const activeRows = summary?.drivers.flatMap((driver) =>
    driver.events
      .filter((event) => event.status === 'Active')
      .map((event) => ({
        event,
        driverName: driver.driver_name ?? driver.driver_code ?? `Driver ${driver.driver_id}`,
        canVoid: driver.capabilities.can_void,
      })),
  ) ?? [];
  const eligibleDrivers = summary?.drivers.filter((driver) => driver.capabilities.can_create) ?? [];
  const canCreate = canEdit && eligibleDrivers.length > 0;

  return (
    <div className={styles.backdrop} onClick={onClose}>
      <div
        className={styles.dialog}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Bonus"
      >
        {/* Header */}
        <div className={styles.dialogHeader}>
          <h3 className={styles.dialogTitle}>
            Bonus{periodName ? ` — ${periodName}` : ''}
          </h3>
          <div className={styles.headerRight}>
            {canCreate && (
              <button
                className={styles.addBtn}
                onClick={() => { setAddError(null); setShowAddForm(true); }}
              >
                + Add Bonus
              </button>
            )}
            <button className={styles.closeBtn} onClick={onClose} aria-label="Close">
              &#x2715;
            </button>
          </div>
        </div>

        {/* Body */}
        <div className={styles.dialogBody}>
          {loading ? (
            <div className={styles.stateMsg}>Loading…</div>
          ) : loadError ? (
            <div className={styles.errorMsg}>{loadError}</div>
          ) : (
            <>
              <div className={styles.summary}>
                Total: <strong>{fmtAmount(summary?.active_bonus_total)}</strong>
                &nbsp;|&nbsp;{summary?.active_event_count ?? 0} entr{summary?.active_event_count === 1 ? 'y' : 'ies'}
              </div>

              {activeRows.length === 0 ? (
                <div className={styles.emptyMsg}>No active bonus events for this period.</div>
              ) : (
                <table className={styles.table}>
                  <thead>
                    <tr>
                      <th>Driver</th>
                      <th className={styles.amtCol}>Amount</th>
                      <th>Reason</th>
                      <th>Status</th>
                      {canEdit && <th>Actions</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {activeRows.map(({ event, driverName, canVoid }) => (
                      <tr key={event.bonus_event_id}>
                        <td className={styles.driverCell}>{driverName}</td>
                        <td className={styles.amtCol}>{fmtAmount(event.amount)}</td>
                        <td>{event.reason ?? event.notes ?? '—'}</td>
                        <td>{event.status}</td>
                        {canEdit && (
                          <td className={styles.actionsCell}>
                            {!canVoid ? '—' : confirmVoidId === event.bonus_event_id ? (
                              <span className={styles.voidConfirm}>
                                Void?{' '}
                                <button
                                  className={styles.dangerBtn}
                                  disabled={voidingId === event.bonus_event_id}
                                  onClick={() => void handleVoid(event.bonus_event_id)}
                                >
                                  {voidingId === event.bonus_event_id ? '…' : 'Yes'}
                                </button>
                                <button
                                  className={styles.cancelBtn}
                                  onClick={() => setConfirmVoidId(null)}
                                >
                                  No
                                </button>
                              </span>
                            ) : (
                              <button
                                className={styles.dangerBtn}
                                onClick={() => setConfirmVoidId(event.bonus_event_id)}
                              >
                                Void
                              </button>
                            )}
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}

              {voidError && <p className={styles.formError}>{voidError}</p>}

              {/* Add form */}
              {showAddForm && (
                <div className={styles.addForm}>
                  <h4 className={styles.addFormTitle}>Add Bonus</h4>
                  <form onSubmit={(e) => void handleAdd(e)} className={styles.addFormFields}>
                    <label className={styles.formLabel}>
                      Driver
                      <select
                        className={styles.formSelect}
                        value={addDriverId}
                        onChange={(e) => setAddDriverId(e.target.value)}
                        required
                      >
                        <option value="">— select driver —</option>
                        {eligibleDrivers.map((driver) => (
                          <option key={driver.driver_id} value={String(driver.driver_id)}>
                            {driver.driver_name ?? driver.driver_code ?? `Driver ${driver.driver_id}`}
                          </option>
                        ))}
                      </select>
                    </label>

                    <label className={styles.formLabel}>
                      Amount ($)
                      <input
                        className={styles.formInput}
                        type="number"
                        step="0.01"
                        min="0.01"
                        placeholder="0.00"
                        value={addAmount}
                        onChange={(e) => setAddAmount(e.target.value)}
                        required
                      />
                    </label>

                    <label className={styles.formLabel} style={{ flex: 1 }}>
                      Reason
                      <input
                        className={styles.formInput}
                        type="text"
                        placeholder="e.g. Monthly bonus"
                        value={addReason}
                        onChange={(e) => setAddReason(e.target.value)}
                        required
                        style={{ width: '100%' }}
                      />
                    </label>

                    {addError && <p className={styles.formError}>{addError}</p>}

                    <div className={styles.addFormActions}>
                      <button
                        type="button"
                        className={styles.cancelBtn}
                        onClick={() => { setShowAddForm(false); setAddError(null); }}
                      >
                        Cancel
                      </button>
                      <button
                        type="submit"
                        className={styles.primaryBtn}
                        disabled={addSaving || !addDriverId || !addAmount || !addReason.trim()}
                      >
                        {addSaving ? 'Saving…' : 'Save'}
                      </button>
                    </div>
                  </form>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
