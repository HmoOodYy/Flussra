import { useState, useEffect, useCallback, useReducer } from 'react';
import type { FormEvent } from 'react';
import apiClient from '../lib/apiClient';
import { getNextPeriodDates } from '../lib/payrollApi';
import type { Branch } from '../types/core';
import type { PeriodSummary, PeriodCreate, NextPeriodDates } from '../types/payroll';
import styles from './CreatePeriodModal.module.css';

// ─── helpers ──────────────────────────────────────────────────────────────────

function fmtDate(d: string | null | undefined): string {
  if (!d) return '—';
  return new Date(d + 'T00:00:00').toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  });
}

function freqLabel(f: string): string {
  const map: Record<string, string> = {
    Week: 'Weekly', Biweek: 'Bi-weekly', Month: 'Monthly', Custom: 'Custom',
  };
  return map[f] ?? f;
}

/**
 * Parse a 422 overlap error from the backend into a readable message.
 * Backend format: "Overlap with existing period ID 7 (Status: Open, 2026-06-07 – 2026-06-13)"
 */
function formatOverlapError(raw: string): string {
  // Already readable — just surface it clearly.
  if (raw.toLowerCase().includes('overlap')) {
    return raw;
  }
  return raw;
}

// ─── date-load reducer (avoids setState-in-effect lint rule) ─────────────────

type DateLoadState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ok';               data: NextPeriodDates }
  | { status: 'no-setup' }
  | { status: 'incomplete-custom' }
  | { status: 'forbidden' }
  | { status: 'error';            message: string };

type DateLoadAction =
  | { type: 'LOADING' }
  | { type: 'OK';               data: NextPeriodDates }
  | { type: 'NO_SETUP' }
  | { type: 'INCOMPLETE_CUSTOM' }
  | { type: 'FORBIDDEN' }
  | { type: 'ERROR';            message: string }
  | { type: 'IDLE' };

function dateLoadReducer(_s: DateLoadState, a: DateLoadAction): DateLoadState {
  switch (a.type) {
    case 'LOADING':           return { status: 'loading' };
    case 'OK':                return { status: 'ok', data: a.data };
    case 'NO_SETUP':          return { status: 'no-setup' };
    case 'INCOMPLETE_CUSTOM': return { status: 'incomplete-custom' };
    case 'FORBIDDEN':         return { status: 'forbidden' };
    case 'ERROR':             return { status: 'error', message: a.message };
    case 'IDLE':              return { status: 'idle' };
  }
}

// ─── component ────────────────────────────────────────────────────────────────

interface Props {
  /** All branches the user can create periods for. */
  branches: Branch[];
  /**
   * Pre-selected branch_id.
   * Pass null to force the user to pick from the dropdown.
   */
  defaultBranchId: number | null;
  /** When true the branch dropdown is shown and required. */
  isAllBranches: boolean;
  onCreated: (period: PeriodSummary) => void;
  onClose: () => void;
}

export function CreatePeriodModal({
  branches,
  defaultBranchId,
  isAllBranches,
  onCreated,
  onClose,
}: Props) {
  // Determine initial branch ID
  const initialBranchId = isAllBranches
    ? (defaultBranchId ?? 0)
    : (defaultBranchId ?? branches[0]?.branch_id ?? 0);

  const [branchId, setBranchId] = useState<number>(initialBranchId);
  const [dateState, dispatchDate] = useReducer(dateLoadReducer, { status: 'idle' });

  // Optional manual fields
  const [periodName,  setPeriodName]  = useState('');

  // Submission
  const [submitting,   setSubmitting]   = useState(false);
  const [submitError,  setSubmitError]  = useState('');

  // ── fetch next-period-dates whenever branchId changes ───────────────────────
  // Uses dispatchDate (a reducer dispatch) — safe to call inside useEffect per
  // the react-hooks/set-state-in-effect lint rule.
  const fetchDates = useCallback(async (bid: number) => {
    if (!bid || bid === 0) {
      dispatchDate({ type: 'IDLE' });
      return;
    }
    dispatchDate({ type: 'LOADING' });
    try {
      const data = await getNextPeriodDates(bid);
      if (data.is_custom && (data.start_date === null || data.end_date === null)) {
        dispatchDate({ type: 'INCOMPLETE_CUSTOM' });
      } else {
        dispatchDate({ type: 'OK', data });
      }
    } catch (err: unknown) {
      const httpStatus = (err as { response?: { status?: number } })?.response?.status;
      if (httpStatus === 404) {
        dispatchDate({ type: 'NO_SETUP' });
      } else if (httpStatus === 403) {
        dispatchDate({ type: 'FORBIDDEN' });
      } else {
        const msg =
          (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
          'Failed to load next period dates.';
        dispatchDate({ type: 'ERROR', message: msg });
      }
    }
  }, []);

  useEffect(() => {
    void fetchDates(branchId);
  }, [branchId, fetchDates]);

  // ── submit ───────────────────────────────────────────────────────────────────
  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitError('');

    if (!branchId || branchId === 0) {
      setSubmitError('Please select a branch.');
      return;
    }
    if (dateState.status !== 'ok') return;

    const { data: nd } = dateState;
    if (!nd.start_date || !nd.end_date) return;

    const payload: PeriodCreate = {
      branch_id:   branchId,
      period_type: nd.period_type,
      start_date:  nd.start_date,
      end_date:    nd.end_date,
      ...(periodName ? { period_name: periodName } : {}),
    };

    setSubmitting(true);
    try {
      const { data } = await apiClient.post<PeriodSummary>('/payroll/periods', payload);
      onCreated(data);
    } catch (err: unknown) {
      const raw =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ??
        'Failed to create payroll period.';
      setSubmitError(formatOverlapError(raw));
    } finally {
      setSubmitting(false);
    }
  }

  // ── derived ───────────────────────────────────────────────────────────────────
  const canSubmit = dateState.status === 'ok' && !submitting;
  const fixedBranchName = !isAllBranches
    ? (branches.find(b => b.branch_id === initialBranchId)?.branch_name ?? 'Your Branch')
    : null;

  // ── render ────────────────────────────────────────────────────────────────────
  return (
    <div className={styles.overlay} onClick={e => e.target === e.currentTarget && onClose()}>
      <div className={styles.modal}>

        {/* Header */}
        <div className={styles.header}>
          <h2 className={styles.title}>Create Payroll Period</h2>
          <button className={styles.closeBtn} onClick={onClose} aria-label="Close">&#x2715;</button>
        </div>

        <form onSubmit={e => void handleSubmit(e)} className={styles.form}>

          {/* Branch selector */}
          {isAllBranches ? (
            <label className={styles.label}>
              Branch <span className={styles.required}>*</span>
              <select
                className={styles.select}
                value={branchId === 0 ? '' : branchId}
                onChange={e => setBranchId(Number(e.target.value))}
                required
              >
                <option value="" disabled>— Select a branch —</option>
                {branches.map(b => (
                  <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>
                ))}
              </select>
            </label>
          ) : (
            <div className={styles.label}>
              Branch
              <span className={styles.fixedValue}>{fixedBranchName}</span>
            </div>
          )}

          {/* Next period dates panel */}
          {dateState.status === 'idle' && branchId === 0 && (
            <div className={styles.datesHint}>
              Select a branch to load next payroll period dates.
            </div>
          )}

          {dateState.status === 'loading' && (
            <div className={styles.datesLoading}>
              <span className={styles.spinner} /> Loading next period dates…
            </div>
          )}

          {dateState.status === 'no-setup' && (
            <div className={styles.datesError}>
              <strong>Payroll setup is not configured for this branch.</strong>
              <br />
              Go to <em>Settings → Payroll Setup</em> to configure a payroll schedule before creating periods.
            </div>
          )}

          {dateState.status === 'incomplete-custom' && (
            <div className={styles.datesError}>
              <strong>Payroll setup is incomplete.</strong>
              <br />
              Complete the Custom payroll setup (set the first period end date) in
              <em> Settings → Payroll Setup</em> before creating a payroll period.
            </div>
          )}

          {dateState.status === 'forbidden' && (
            <div className={styles.datesError}>
              <strong>Access denied.</strong>
              <br />
              You do not have permission to create payroll periods for this branch.
            </div>
          )}

          {dateState.status === 'error' && (
            <div className={styles.datesError}>
              <strong>Could not load next period dates.</strong>
              <br />
              {dateState.message}
            </div>
          )}

          {dateState.status === 'ok' && (() => {
            const nd = dateState.data;
            return (
              <div className={styles.datesCard}>
                <div className={styles.datesCardHeader}>
                  <span className={styles.datesCardTitle}>Next Payroll Period</span>
                  <span className={styles.freqBadge}>{freqLabel(nd.period_type)}</span>
                </div>

                <div className={styles.datesRow}>
                  <div className={styles.dateBlock}>
                    <span className={styles.dateBlockLabel}>Start Date</span>
                    <span className={styles.dateBlockValue}>{fmtDate(nd.start_date)}</span>
                  </div>
                  <div className={styles.dateSep}>→</div>
                  <div className={styles.dateBlock}>
                    <span className={styles.dateBlockLabel}>End Date</span>
                    <span className={styles.dateBlockValue}>{fmtDate(nd.end_date)}</span>
                  </div>
                </div>

                {nd.is_custom && nd.custom_interval_days && (
                  <div className={styles.datesNote}>
                    Custom cycle: {nd.custom_interval_days} day{nd.custom_interval_days !== 1 ? 's' : ''}
                  </div>
                )}

                {nd.last_period_end_date && (
                  <div className={styles.datesNote}>
                    Follows period ending {fmtDate(nd.last_period_end_date)}
                  </div>
                )}

                <div className={styles.datesSource}>
                  Dates are auto-calculated from this branch's Payroll Setup.
                  They cannot be changed here.
                </div>
              </div>
            );
          })()}

          {/* Optional: Period Name */}
          {dateState.status === 'ok' && (
            <label className={styles.label}>
              Period Name{' '}
              <span className={styles.optional}>(optional — auto-generated if blank)</span>
              <input
                className={styles.input}
                type="text"
                placeholder="e.g. Week Jun 2026"
                value={periodName}
                onChange={e => setPeriodName(e.target.value)}
                disabled={submitting}
              />
            </label>
          )}

          {/* Submit error */}
          {submitError && (
            <div className={styles.error}>{submitError}</div>
          )}

          {/* Actions */}
          <div className={styles.actions}>
            <button type="button" className={styles.cancelBtn} onClick={onClose}>
              Cancel
            </button>
            <button
              type="submit"
              className={styles.submitBtn}
              disabled={!canSubmit}
              title={
                dateState.status !== 'ok'
                  ? 'Resolve the setup issue above before creating a period'
                  : undefined
              }
            >
              {submitting ? 'Creating…' : 'Create Period'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
