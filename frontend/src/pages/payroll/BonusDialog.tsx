/**
 * BonusDialog — period-level bonus management modal.
 *
 * Extracted from BonusPanel in PeriodDetailPage.tsx and promoted to a
 * standalone dialog. Fetches eligible drivers itself given a periodId.
 */
import { useEffect, useState, useCallback } from 'react';
import {
  getPeriodPayLines,
  addPeriodPayLine,
  voidPeriodPayLine,
  getEligibleDrivers,
} from '../../lib/payrollApi';
import type { PeriodPayLine, EligibleDriver } from '../../types/payroll';
import { useAuth } from '../../store/authStore';
import { canEntryPayroll } from '../../lib/permissions';
import styles from './BonusDialog.module.css';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const BONUS_EDITABLE_STATUSES = new Set(['Open']);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtAmount(v: string | null | undefined): string {
  if (v == null) return '—';
  const n = parseFloat(v);
  return isNaN(n) ? String(v) : `$${n.toFixed(2)}`;
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
  const [lines, setLines] = useState<PeriodPayLine[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [drivers, setDrivers] = useState<EligibleDriver[]>([]);

  const [showAddForm, setShowAddForm] = useState(false);
  const [addDriverId, setAddDriverId] = useState('');
  const [addAmount, setAddAmount] = useState('');
  const [addNotes, setAddNotes] = useState('');
  const [addSaving, setAddSaving] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const [voidingId, setVoidingId] = useState<number | null>(null);
  const [confirmVoidId, setConfirmVoidId] = useState<number | null>(null);

  // canEdit = period status allows bonus edits AND user has payroll.entry permission.
  const canEdit = BONUS_EDITABLE_STATUSES.has(periodStatus) && userCanEntry;

  const fetchLines = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const all = await getPeriodPayLines(periodId);
      setLines(all.filter((l) => l.line_type === 'BONUS' && l.status !== 'Void'));
    } catch {
      setLoadError('Failed to load bonus lines.');
    } finally {
      setLoading(false);
    }
  }, [periodId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void fetchLines();
    // Load eligible drivers for the add form
    getEligibleDrivers(periodId)
      .then(setDrivers)
      .catch(() => setDrivers([]));
  }, [fetchLines, periodId]);

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
    if (!addDriverId || !addAmount || !addNotes.trim()) return;
    setAddSaving(true);
    setAddError(null);
    try {
      await addPeriodPayLine(periodId, {
        driver_id: Number(addDriverId),
        line_type: 'BONUS',
        amount: addAmount,
        notes: addNotes.trim(),
      });
      setShowAddForm(false);
      setAddDriverId('');
      setAddAmount('');
      setAddNotes('');
      await fetchLines();
    } catch (err: unknown) {
      const msg =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Failed to add bonus.';
      setAddError(msg);
    } finally {
      setAddSaving(false);
    }
  }

  async function handleVoid(lineId: number) {
    setVoidingId(lineId);
    try {
      await voidPeriodPayLine(periodId, lineId);
      setConfirmVoidId(null);
      await fetchLines();
    } catch {
      await fetchLines();
    } finally {
      setVoidingId(null);
    }
  }

  const totalAmount = lines
    .filter((l) => l.calculated_amount != null)
    .reduce((sum, l) => sum + parseFloat(l.calculated_amount!), 0);

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
            {canEdit && (
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
                Total: <strong>{fmtAmount(totalAmount.toFixed(2))}</strong>
                &nbsp;|&nbsp;{lines.length} entr{lines.length === 1 ? 'y' : 'ies'}
              </div>

              {lines.length === 0 ? (
                <div className={styles.emptyMsg}>No bonus lines for this period.</div>
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
                    {lines.map((line) => (
                      <tr key={line.draft_line_id}>
                        <td className={styles.driverCell}>{line.driver_name}</td>
                        <td className={styles.amtCol}>{fmtAmount(line.calculated_amount)}</td>
                        <td>{line.notes ?? '—'}</td>
                        <td>{line.status}</td>
                        {canEdit && (
                          <td className={styles.actionsCell}>
                            {confirmVoidId === line.draft_line_id ? (
                              <span className={styles.voidConfirm}>
                                Void?{' '}
                                <button
                                  className={styles.dangerBtn}
                                  disabled={voidingId === line.draft_line_id}
                                  onClick={() => void handleVoid(line.draft_line_id)}
                                >
                                  {voidingId === line.draft_line_id ? '…' : 'Yes'}
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
                                onClick={() => setConfirmVoidId(line.draft_line_id)}
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
                        {drivers.map((d) => (
                          <option key={d.driver_id} value={String(d.driver_id)}>
                            {d.driver_name}
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
                      Reason / Notes
                      <input
                        className={styles.formInput}
                        type="text"
                        placeholder="e.g. Monthly bonus"
                        value={addNotes}
                        onChange={(e) => setAddNotes(e.target.value)}
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
                        disabled={addSaving || !addDriverId || !addAmount || !addNotes.trim()}
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
