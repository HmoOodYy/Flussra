/**
 * BonusDialog — period-level bonus management modal.
 *
 * Extracted from BonusPanel in PeriodDetailPage.tsx and promoted to a
 * standalone dialog. Fetches the backend-owned bonus summary given a periodId.
 */
import { useEffect, useState, useCallback, useRef } from 'react';
import {
  createBonusBatch,
  getBonusSummary,
  voidBonusEvent,
} from '../../lib/payrollApi';
import type { BonusBatchItem, BonusSummary } from '../../types/payroll';
import styles from './BonusDialog.module.css';

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

function createIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `bonus-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

interface BonusDraftRow {
  rowId: number;
  driverId: string;
  amount: string;
  reason: string;
}

function newBonusDraftRow(rowId: number): BonusDraftRow {
  return { rowId, driverId: '', amount: '', reason: '' };
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface BonusDialogProps {
  periodId: number;
  periodName?: string;
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function BonusDialog({ periodId, periodName, onClose }: BonusDialogProps) {
  const [summary, setSummary] = useState<BonusSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [showAddForm, setShowAddForm] = useState(false);
  const [addRows, setAddRows] = useState<BonusDraftRow[]>([newBonusDraftRow(1)]);
  const nextRowId = useRef(2);
  const idempotencyKey = useRef<string | null>(null);
  const [addSaving, setAddSaving] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const [voidingId, setVoidingId] = useState<number | null>(null);
  const [confirmVoidId, setConfirmVoidId] = useState<number | null>(null);
  const [voidError, setVoidError] = useState<string | null>(null);

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
    if (
      !summary ||
      addRows.some((row) => !row.driverId || !row.amount || !row.reason.trim())
    ) return;
    setAddSaving(true);
    setAddError(null);
    const requestKey = idempotencyKey.current ?? createIdempotencyKey();
    idempotencyKey.current = requestKey;
    try {
      const items: BonusBatchItem[] = addRows.map((row) => ({
        driver_id: Number(row.driverId),
        amount: row.amount,
        reason: row.reason.trim(),
      }));
      await createBonusBatch(periodId, {
        idempotency_key: requestKey,
        expected_bonus_data_revision: summary.bonus_data_revision,
        items,
      });
      setShowAddForm(false);
      setAddRows([newBonusDraftRow(nextRowId.current++)]);
      idempotencyKey.current = null;
      await fetchSummary();
    } catch (err: unknown) {
      setAddError(getErrorDetail(err, 'Failed to add bonus event.'));
      // A response means the server definitively rejected this request. With
      // no response, retain the key so a retry can safely replay the batch.
      const response = (err as { response?: { status?: unknown } })?.response;
      if (response?.status === 409) {
        idempotencyKey.current = null;
        await fetchSummary();
      } else if (response) {
        idempotencyKey.current = null;
      }
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
  const canCreate = eligibleDrivers.length > 0;
  const canVoidAny = activeRows.some((row) => row.canVoid);

  function startAddForm() {
    idempotencyKey.current = null;
    setAddRows([newBonusDraftRow(nextRowId.current++)]);
    setAddError(null);
    setShowAddForm(true);
  }

  function updateAddRow(rowId: number, field: keyof Omit<BonusDraftRow, 'rowId'>, value: string) {
    setAddRows((rows) => rows.map((row) => (
      row.rowId === rowId ? { ...row, [field]: value } : row
    )));
    idempotencyKey.current = null;
  }

  function addBonusRow() {
    setAddRows((rows) => [...rows, newBonusDraftRow(nextRowId.current++)]);
    idempotencyKey.current = null;
  }

  function removeBonusRow(rowId: number) {
    setAddRows((rows) => rows.length === 1 ? rows : rows.filter((row) => row.rowId !== rowId));
    idempotencyKey.current = null;
  }

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
                onClick={startAddForm}
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
                      {canVoidAny && <th>Actions</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {activeRows.map(({ event, driverName, canVoid }) => (
                      <tr key={event.bonus_event_id}>
                        <td className={styles.driverCell}>{driverName}</td>
                        <td className={styles.amtCol}>{fmtAmount(event.amount)}</td>
                        <td>{event.reason ?? event.notes ?? '—'}</td>
                        <td>{event.status}</td>
                        {canVoidAny && (
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
                  <div className={styles.addFormHeading}>
                    <h4 className={styles.addFormTitle}>Add Bonus</h4>
                    <button
                      type="button"
                      className={styles.addRowBtn}
                      onClick={addBonusRow}
                      aria-label="Add another bonus row"
                    >
                      + Add row
                    </button>
                  </div>
                  <form onSubmit={(e) => void handleAdd(e)}>
                    <div className={styles.formRows}>
                      {addRows.map((row) => (
                        <div className={styles.formRow} key={row.rowId}>
                          <label className={`${styles.formLabel} ${styles.driverField}`}>
                            Driver
                            <select
                              className={styles.formSelect}
                              value={row.driverId}
                              onChange={(e) => updateAddRow(row.rowId, 'driverId', e.target.value)}
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
                              value={row.amount}
                              onChange={(e) => updateAddRow(row.rowId, 'amount', e.target.value)}
                              required
                            />
                          </label>

                          <label className={`${styles.formLabel} ${styles.reasonField}`}>
                            Reason
                            <input
                              className={styles.formInput}
                              type="text"
                              placeholder="e.g. Monthly bonus"
                              value={row.reason}
                              onChange={(e) => updateAddRow(row.rowId, 'reason', e.target.value)}
                              required
                            />
                          </label>

                          <button
                            type="button"
                            className={styles.removeRowBtn}
                            onClick={() => removeBonusRow(row.rowId)}
                            disabled={addRows.length === 1}
                            aria-label="Remove bonus row"
                            title="Remove bonus row"
                          >
                            ×
                          </button>
                        </div>
                      ))}
                    </div>

                    {addError && <p className={styles.formError}>{addError}</p>}

                    <div className={styles.addFormActions}>
                      <button
                        type="button"
                        className={styles.cancelBtn}
                        onClick={() => { setShowAddForm(false); setAddError(null); idempotencyKey.current = null; }}
                      >
                        Cancel
                      </button>
                      <button
                        type="submit"
                        className={styles.primaryBtn}
                        disabled={addSaving || addRows.some((row) => !row.driverId || !row.amount || !row.reason.trim())}
                      >
                        {addSaving ? 'Saving…' : `Save ${addRows.length} bonus${addRows.length === 1 ? '' : 'es'}`}
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
