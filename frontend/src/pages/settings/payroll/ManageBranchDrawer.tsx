/**
 * "Manage" drawer for a single branch's payroll policy (Phase 6 Unit A) —
 * current policy, a scheduled change (with cancel), changing the policy
 * (preview-first, same stale-safety contract as publish), and policy
 * history. Extracted from PayrollSetupsPage.tsx.
 *
 * Reassignment preview-first stale safety is unchanged: "Change policy"
 * only ever executes the stored preview's own inputs, and only when
 * canReassignFromPreview says so — see branchAssignmentPreview.ts, kept
 * intact. The mutation itself is never retried on failure; only the
 * read-only preview is asked again.
 */
import { useEffect, useRef, useState } from 'react';
import styles from './PayrollSetupsPage.module.css';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { PayrollBoundaryDateInput } from '../../../components/payroll/PayrollBoundaryDateInput';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import {
  getReassignmentChoices,
  listBranchPayrollSetupAssignments,
  previewReassignmentImpact,
  reassignPayrollSetup,
  withdrawPayrollSetupAssignment,
} from '../../../lib/payrollSetupApi';
import { readApiError, friendlyBoundaryConflictMessage } from '../../../lib/payrollSetupErrors';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { frequencyNoun, isChoicesCurrent } from '../../../lib/payrollBoundaryView';
import { formatIsoLong } from '../../../lib/isoDate';
import { buildAutoPreviewKey, useAutoPreview } from '../../../lib/useAutoPreview';
import { useBoundaryChoices } from '../../../lib/useBoundaryChoices';
import {
  buildReassignmentImpactRequest,
  buildReassignmentRequest,
  canReassignFromPreview,
} from './branchAssignmentPreview';
import type { ReassignInputs, StoredReassignPreview } from './branchAssignmentPreview';
import type { AssignmentResponse, BranchPolicySummaryResponse, SetupResponse } from '../../../types/payrollSetup';

type ManageBranchDrawerProps = {
  branchId: number;
  branchName: string;
  summary: BranchPolicySummaryResponse;
  activeSetups: readonly SetupResponse[];
  mutating: boolean;
  setMutating: (value: boolean) => void;
  onClose: () => void;
  onChanged: () => void;
  showToast: (message: string) => void;
};

function assignmentLine(a: AssignmentResponse): string {
  const span =
    a.effective_to_date != null
      ? `${formatIsoLong(a.effective_from_date)} – ${formatIsoLong(a.effective_to_date)}`
      : `${formatIsoLong(a.effective_from_date)} onward`;
  const cancelled = a.withdrawn_at_utc != null ? ' (cancelled)' : '';
  return `${a.setup_name} — ${span}${cancelled}`;
}

export function ManageBranchDrawer({
  branchId,
  branchName,
  summary,
  activeSetups,
  mutating,
  setMutating,
  onClose,
  onChanged,
  showToast,
}: ManageBranchDrawerProps) {
  const headingRef = useRef<HTMLHeadingElement | null>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  // ── Policy history (loaded once, when the drawer opens) ──────────────────
  const [history, setHistory] = useState<AssignmentResponse[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<ApiErrorInfo | null>(null);
  const [historyReloadKey, setHistoryReloadKey] = useState(0);

  useEffect(() => {
    let active = true;
    function markLoading() {
      setHistoryLoading(true);
    }
    markLoading();
    listBranchPayrollSetupAssignments(branchId)
      .then((rows) => {
        if (active) {
          setHistory(rows);
          setHistoryError(null);
          setHistoryLoading(false);
        }
      })
      .catch((err) => {
        if (active) {
          setHistoryError(readApiError(err, 'Failed to load policy history.'));
          setHistoryLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [branchId, historyReloadKey]);

  function afterMutation() {
    setHistoryReloadKey((k) => k + 1);
    onChanged();
  }

  // ── Cancel a scheduled change ─────────────────────────────────────────────
  const [cancelConfirmOpen, setCancelConfirmOpen] = useState(false);
  const [cancelError, setCancelError] = useState<ApiErrorInfo | null>(null);

  const cancelMessage = summary.scheduled_change
    ? `Cancel the change to ${summary.scheduled_change.setup_name} starting ` +
      `${formatIsoLong(summary.scheduled_change.effective_from_date)}? The branch keeps following ` +
      `${summary.current ? summary.current.setup_name : 'no policy'}.`
    : '';

  async function confirmCancelScheduledChange() {
    if (!summary.scheduled_change) {
      setCancelConfirmOpen(false);
      return;
    }
    setMutating(true);
    try {
      await withdrawPayrollSetupAssignment(summary.scheduled_change.assignment_id);
      showToast('Scheduled change cancelled.');
      setCancelConfirmOpen(false);
      afterMutation();
      onClose();
    } catch (err) {
      setCancelError(readApiError(err, 'Failed to cancel the scheduled change.'));
      setCancelConfirmOpen(false);
    } finally {
      setMutating(false);
    }
  }

  // ── Change policy (preview-first) ─────────────────────────────────────────
  const destinationOptions = activeSetups.filter((s) => s.setup_id !== summary.current?.setup_id);
  const [destinationSetupId, setDestinationSetupId] = useState<number | null>(null);
  const [reassignDate, setReassignDate] = useState('');
  const [reassignReason, setReassignReason] = useState('');
  const [reassignPreviewNonce, setReassignPreviewNonce] = useState(0);
  const [reassignConfirmOpen, setReassignConfirmOpen] = useState(false);
  const [reassignError, setReassignError] = useState<ApiErrorInfo | null>(null);

  const reassignChoicesKey = `${branchId}|${destinationSetupId ?? 'none'}`;
  const reassignChoicesFetcher =
    destinationSetupId != null
      ? (around?: string) => getReassignmentChoices(branchId, destinationSetupId, around)
      : null;
  const reassignBoundary = useBoundaryChoices(reassignChoicesFetcher, reassignChoicesKey, reassignDate);

  useEffect(() => {
    function applySuggestedDate() {
      if (reassignBoundary.choices?.suggested) setReassignDate(reassignBoundary.choices.suggested.date);
    }
    if (reassignDate === '' && reassignBoundary.choices?.suggested) {
      applySuggestedDate();
    }
  }, [reassignBoundary.choices, reassignDate]);

  function handleDestinationChange(value: string) {
    setDestinationSetupId(value === '' ? null : Number(value));
    setReassignDate('');
  }

  const currentReassignInputs: ReassignInputs | null =
    destinationSetupId != null
      ? { branchId, destinationSetupId, effectiveFromDate: reassignDate }
      : null;

  const reassignDateValid =
    reassignBoundary.choices != null && isChoicesCurrent(reassignBoundary.choices, reassignDate) &&
    reassignBoundary.choices.requested_valid;

  // The fetcher receives its own inputs snapshot from the hook (captured at
  // request time) — it must never close over `currentReassignInputs`
  // directly, or the snapshot would just be whatever is current at the
  // moment the closure was created, defeating the point of the snapshot.
  const reassignAutoFetcher = reassignDateValid
    ? (reassignInputs: ReassignInputs) => previewReassignmentImpact(branchId, buildReassignmentImpactRequest(reassignInputs))
    : null;
  const reassignRequestKey = buildAutoPreviewKey([
    branchId,
    destinationSetupId,
    reassignDate,
    reassignPreviewNonce,
  ]);
  // `currentReassignInputs` is only null when no destination is selected,
  // in which case reassignAutoFetcher above is already null too (no
  // destination -> no boundary choices -> reassignDateValid false) — this
  // placeholder is never actually sent to the fetcher.
  const reassignAutoPreview = useAutoPreview(
    reassignAutoFetcher,
    currentReassignInputs ?? { branchId, destinationSetupId: -1, effectiveFromDate: '' },
    reassignRequestKey,
  );

  // `stored.inputs` is the hook's own independent snapshot of the inputs
  // that were actually sent — never re-stamped with `currentReassignInputs`.
  // canReassignFromPreview's comparison of currentReassignInputs against
  // stored.inputs is what actually detects staleness; see useAutoPreview.ts.
  const storedReassignPreview: StoredReassignPreview | null = reassignAutoPreview.stored;
  const canChangePolicyNow =
    currentReassignInputs != null &&
    storedReassignPreview != null &&
    canReassignFromPreview(currentReassignInputs, storedReassignPreview) &&
    !reassignAutoPreview.loading;

  const destinationSetup = activeSetups.find((s) => s.setup_id === destinationSetupId) ?? null;
  const reassignConfirmMessage =
    storedReassignPreview && destinationSetup
      ? `Change ${branchName} from ${summary.current ? summary.current.setup_name : 'no policy'} to ` +
        `${destinationSetup.setup_name} starting ${formatIsoLong(storedReassignPreview.inputs.effectiveFromDate)}. ` +
        'Existing payroll history is not changed.'
      : '';

  async function confirmChangePolicy() {
    if (!storedReassignPreview || !canReassignFromPreview(currentReassignInputs!, storedReassignPreview)) {
      setReassignConfirmOpen(false);
      return;
    }
    setMutating(true);
    try {
      await reassignPayrollSetup(branchId, buildReassignmentRequest(storedReassignPreview.inputs, reassignReason));
      showToast('Policy changed.');
      setReassignConfirmOpen(false);
      setDestinationSetupId(null);
      setReassignDate('');
      setReassignReason('');
      afterMutation();
    } catch (err) {
      setReassignError(readApiError(err, 'Failed to change the policy.'));
      setReassignConfirmOpen(false);
      setReassignPreviewNonce((n) => n + 1);
    } finally {
      setMutating(false);
    }
  }

  return (
    <div
      className={styles.modalOverlay}
      role="dialog"
      aria-modal="true"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className={styles.modal}>
        <div className={styles.modalHeader}>
          <h2 className={styles.modalTitle} tabIndex={-1} ref={headingRef}>
            {`Manage — ${branchName}`}
          </h2>
          <button className={styles.modalCloseBtn} onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className={styles.modalBody}>
          {/* Current policy */}
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Current policy</span>
            <span className={styles.detailValue}>
              {summary.current
                ? `${summary.current.setup_name} — ${
                    summary.current.payroll_frequency != null
                      ? frequencyNoun(summary.current.payroll_frequency, summary.current.custom_interval_days)
                      : 'Unscheduled'
                  }`
                : 'None'}
            </span>
          </div>
          {summary.current && (
            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Since</span>
              <span className={styles.detailValue}>{formatIsoLong(summary.current.effective_from_date)}</span>
            </div>
          )}

          {/* Scheduled change */}
          {summary.scheduled_change && (
            <>
              <div className={styles.detailRow}>
                <span className={styles.detailLabel}>Scheduled change</span>
                <span className={styles.detailValue}>
                  {`${summary.scheduled_change.setup_name} — starts ${formatIsoLong(summary.scheduled_change.effective_from_date)}`}
                </span>
              </div>
              <div className={styles.detailActions}>
                <button className={styles.btnDanger} onClick={() => setCancelConfirmOpen(true)} disabled={mutating}>
                  Cancel scheduled change
                </button>
              </div>
              {cancelError && <PayrollErrorNotice info={cancelError} />}
            </>
          )}

          {/* Change policy */}
          <div className={`${styles.formGroup} ${styles.sectionSpacer}`}>
            <label className={styles.label}>Change policy</label>
            <select
              className={styles.input}
              value={destinationSetupId ?? ''}
              onChange={(e) => handleDestinationChange(e.target.value)}
              disabled={mutating}
            >
              <option value="">Select a new policy…</option>
              {destinationOptions.map((s) => (
                <option key={s.setup_id} value={s.setup_id}>
                  {s.setup_name}
                </option>
              ))}
            </select>
          </div>

          {destinationSetupId != null && (
            <>
              <PayrollBoundaryDateInput
                id="reassign-effective-date"
                label="Starts on"
                value={reassignDate}
                onChange={setReassignDate}
                choices={reassignBoundary.choices}
                loading={reassignBoundary.loading}
                error={reassignBoundary.error}
                disabled={mutating}
                context="reassignment"
              />

              <div className={styles.formGroup}>
                <label className={styles.label}>Reason (optional)</label>
                <textarea
                  className={styles.textarea}
                  value={reassignReason}
                  onChange={(e) => setReassignReason(e.target.value)}
                  disabled={mutating}
                />
              </div>

              {reassignAutoPreview.error && <PayrollErrorNotice info={reassignAutoPreview.error} />}
              {!reassignAutoPreview.error && !reassignAutoPreview.loading && storedReassignPreview && (
                <div className={styles.previewResult}>
                  <div className={styles.detailRow}>
                    <span className={styles.detailLabel}>Allowed</span>
                    <span className={styles.detailValue}>{storedReassignPreview.response.allowed ? 'Yes' : 'No'}</span>
                  </div>
                  {storedReassignPreview.response.conflicts.length > 0 && (
                    <>
                      <ul className={styles.draftList}>
                        {storedReassignPreview.response.conflicts.map((c, i) => (
                          <li key={i} className={styles.assignmentMeta}>
                            {friendlyBoundaryConflictMessage(c.code, c.reason, c.reason)}
                          </li>
                        ))}
                      </ul>
                      <details className={styles.mutedNote}>
                        <summary>Technical details</summary>
                        {storedReassignPreview.response.conflicts.map((c, i) => (
                          <p key={i}>{`${c.code}: ${c.reason}`}</p>
                        ))}
                      </details>
                    </>
                  )}
                </div>
              )}

              {reassignError && <PayrollErrorNotice info={reassignError} />}

              <div className={styles.detailActions}>
                <button
                  className={styles.btnPrimary}
                  onClick={() => setReassignConfirmOpen(true)}
                  disabled={!canChangePolicyNow || mutating}
                >
                  Change policy
                </button>
              </div>
            </>
          )}

          {/* Policy history */}
          <div className={`${styles.formGroup} ${styles.sectionSpacer}`}>
            <label className={styles.label}>Policy history</label>
            {historyError ? (
              <PayrollErrorNotice info={historyError} />
            ) : historyLoading ? (
              <p className={styles.mutedText}>Loading…</p>
            ) : history.length === 0 ? (
              <p className={styles.mutedText}>No policy history yet.</p>
            ) : (
              <ul className={styles.draftList}>
                {history.map((a) => (
                  <li key={a.assignment_id} className={styles.draftRow}>
                    <div className={styles.draftRowMain}>
                      <span className={styles.draftRowSchedule}>{assignmentLine(a)}</span>
                    </div>
                    {a.reason != null && <p className={styles.fieldHint}>{`Reason: ${a.reason}`}</p>}
                    {a.withdrawn_at_utc != null && a.withdrawal_reason != null && (
                      <p className={styles.fieldHint}>{`Cancellation reason: ${a.withdrawal_reason}`}</p>
                    )}
                    <details className={styles.mutedNote}>
                      <summary>Technical details</summary>
                      <p>{`Assignment #${a.assignment_id} · Policy #${a.setup_id}`}</p>
                    </details>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={cancelConfirmOpen}
        title="Cancel scheduled change?"
        message={cancelMessage}
        confirmLabel="Cancel change"
        variant="danger"
        loading={mutating}
        onConfirm={confirmCancelScheduledChange}
        onCancel={() => setCancelConfirmOpen(false)}
      />
      <ConfirmDialog
        open={reassignConfirmOpen}
        title="Change policy?"
        message={reassignConfirmMessage}
        confirmLabel="Change policy"
        loading={mutating}
        onConfirm={confirmChangePolicy}
        onCancel={() => setReassignConfirmOpen(false)}
      />
    </div>
  );
}
