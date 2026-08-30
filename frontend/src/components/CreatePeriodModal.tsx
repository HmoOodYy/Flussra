import { useCallback, useEffect, useReducer, useState } from 'react';
import type { FormEvent } from 'react';
import {
  createPeriodFromCandidate,
  getCurrentWorkflow,
  getPeriodCandidate,
} from '../lib/payrollApi';
import type { PeriodCandidateMode, PeriodCandidatePreview } from '../types/payroll';
import type { Branch } from '../types/core';
import styles from './CreatePeriodModal.module.css';

function fmtDate(date: string): string {
  return new Date(`${date}T00:00:00`).toLocaleDateString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
  });
}

function frequencyLabel(periodType: string): string {
  const labels: Record<string, string> = {
    Week: 'Weekly', Biweek: 'Bi-weekly', Month: 'Monthly', Custom: 'Custom',
  };
  return labels[periodType] ?? periodType;
}

function errorDetail(error: unknown, fallback: string): string {
  const detail =
    (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return fallback;
}

type CandidateState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; candidate: PeriodCandidatePreview }
  | { status: 'blocked'; message: string }
  | { status: 'error'; message: string };

type CandidateAction =
  | { type: 'IDLE' }
  | { type: 'LOADING' }
  | { type: 'READY'; candidate: PeriodCandidatePreview }
  | { type: 'BLOCKED'; message: string }
  | { type: 'ERROR'; message: string };

function candidateReducer(_state: CandidateState, action: CandidateAction): CandidateState {
  switch (action.type) {
    case 'IDLE': return { status: 'idle' };
    case 'LOADING': return { status: 'loading' };
    case 'READY': return { status: 'ready', candidate: action.candidate };
    case 'BLOCKED': return { status: 'blocked', message: action.message };
    case 'ERROR': return { status: 'error', message: action.message };
  }
}

interface Props {
  branches: Branch[];
  defaultBranchId: number | null;
  isAllBranches: boolean;
  onCreated: () => void;
  onClose: () => void;
}

export function CreatePeriodModal({
  branches,
  defaultBranchId,
  isAllBranches,
  onCreated,
  onClose,
}: Props) {
  const initialBranchId = isAllBranches
    ? (defaultBranchId ?? 0)
    : (defaultBranchId ?? branches[0]?.branch_id ?? 0);
  const [branchId, setBranchId] = useState(initialBranchId);
  const [candidateState, dispatchCandidate] = useReducer(candidateReducer, { status: 'idle' });
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');

  const loadCandidate = useCallback(async (selectedBranchId: number) => {
    if (!selectedBranchId) {
      dispatchCandidate({ type: 'IDLE' });
      return;
    }

    dispatchCandidate({ type: 'LOADING' });
    try {
      const workflow = await getCurrentWorkflow(selectedBranchId);
      const branchWorkflow = workflow.branches.find(
        (branch) => branch.branch_id === selectedBranchId,
      );
      if (!branchWorkflow) {
        dispatchCandidate({ type: 'BLOCKED', message: 'This branch is not available for payroll creation.' });
        return;
      }

      const mode: PeriodCandidateMode | null = branchWorkflow.capabilities.can_create_open_candidate.allowed
        ? 'OPEN_CREATION'
        : branchWorkflow.capabilities.can_create_prepared_candidate.allowed
          ? 'PREPARED_CREATION'
          : null;
      if (!mode) {
        const reason = branchWorkflow.capabilities.can_create_open_candidate.reason_message
          ?? branchWorkflow.capabilities.can_create_prepared_candidate.reason_message
          ?? 'No payroll period can be created for this branch right now.';
        dispatchCandidate({ type: 'BLOCKED', message: reason });
        return;
      }

      const candidate = await getPeriodCandidate(selectedBranchId, mode);
      if (!candidate.selected.creatable) {
        dispatchCandidate({
          type: 'BLOCKED',
          message: candidate.selected.blocked_reason ?? 'This payroll candidate is not currently creatable.',
        });
        return;
      }
      dispatchCandidate({ type: 'READY', candidate });
    } catch (error: unknown) {
      dispatchCandidate({ type: 'ERROR', message: errorDetail(error, 'Failed to load payroll creation options.') });
    }
  }, []);

  useEffect(() => {
    void loadCandidate(branchId);
  }, [branchId, loadCandidate]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setSubmitError('');
    if (candidateState.status !== 'ready' || !branchId) return;

    setSubmitting(true);
    try {
      await createPeriodFromCandidate(branchId, candidateState.candidate.selected.candidate_key);
      onCreated();
    } catch (error: unknown) {
      setSubmitError(errorDetail(error, 'Failed to create payroll period. The candidate has been refreshed.'));
      void loadCandidate(branchId);
    } finally {
      setSubmitting(false);
    }
  }

  const fixedBranchName = !isAllBranches
    ? (branches.find((branch) => branch.branch_id === initialBranchId)?.branch_name ?? 'Your Branch')
    : null;
  const selected = candidateState.status === 'ready' ? candidateState.candidate.selected : null;
  const createsPrepared = selected?.target_status === 'Draft';

  return (
    <div className={styles.overlay} onClick={(event) => event.target === event.currentTarget && onClose()}>
      <div className={styles.modal}>
        <div className={styles.header}>
          <h2 className={styles.title}>Create Payroll Period</h2>
          <button className={styles.closeBtn} onClick={onClose} aria-label="Close">&#x2715;</button>
        </div>

        <form onSubmit={(event) => void handleSubmit(event)} className={styles.form}>
          {isAllBranches ? (
            <label className={styles.label}>
              Branch <span className={styles.required}>*</span>
              <select
                className={styles.select}
                value={branchId || ''}
                onChange={(event) => setBranchId(Number(event.target.value))}
                disabled={submitting}
                required
              >
                <option value="" disabled>-- Select a branch --</option>
                {branches.map((branch) => (
                  <option key={branch.branch_id} value={branch.branch_id}>{branch.branch_name}</option>
                ))}
              </select>
            </label>
          ) : (
            <div className={styles.label}>
              Branch
              <span className={styles.fixedValue}>{fixedBranchName}</span>
            </div>
          )}

          {candidateState.status === 'idle' && (
            <div className={styles.datesHint}>Select a branch to load its available payroll candidate.</div>
          )}
          {candidateState.status === 'loading' && (
            <div className={styles.datesLoading}><span className={styles.spinner} /> Loading payroll candidate...</div>
          )}
          {candidateState.status === 'blocked' && (
            <div className={styles.datesError}>{candidateState.message}</div>
          )}
          {candidateState.status === 'error' && (
            <div className={styles.datesError}>
              <strong>Could not load a payroll candidate.</strong><br />
              {candidateState.message}
            </div>
          )}
          {selected && (
            <div className={styles.datesCard}>
              <div className={styles.datesCardHeader}>
                <span className={styles.datesCardTitle}>{createsPrepared ? 'Next Payroll' : 'Current Payroll'}</span>
                <span className={styles.freqBadge}>{createsPrepared ? 'Prepared' : 'Open'} - {frequencyLabel(selected.period_type)}</span>
              </div>
              <strong>{selected.label}</strong>
              <div className={styles.datesRow}>
                <div className={styles.dateBlock}>
                  <span className={styles.dateBlockLabel}>Start Date</span>
                  <span className={styles.dateBlockValue}>{fmtDate(selected.start_date)}</span>
                </div>
                <div className={styles.dateSep}>to</div>
                <div className={styles.dateBlock}>
                  <span className={styles.dateBlockLabel}>End Date</span>
                  <span className={styles.dateBlockValue}>{fmtDate(selected.end_date)}</span>
                </div>
              </div>
              <div className={styles.datesSource}>
                {createsPrepared
                  ? 'This period is prepared for the upcoming payroll and becomes Open automatically when the current Open period closes.'
                  : 'Dates and lifecycle placement are supplied by Payroll Setup and the current backend workflow.'}
              </div>
            </div>
          )}

          {submitError && <div className={styles.error}>{submitError}</div>}

          <div className={styles.actions}>
            <button type="button" className={styles.cancelBtn} onClick={onClose}>Cancel</button>
            <button
              type="submit"
              className={styles.submitBtn}
              disabled={!selected || submitting}
              title={!selected ? 'A backend-approved candidate is required before creating a period' : undefined}
            >
              {submitting ? 'Creating...' : 'Create Payroll'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
