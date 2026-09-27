/**
 * "Assign policy" modal for a single, already-known branch (Phase 6 Unit A)
 * — opened from a Branch Assignments row for a branch with no policy yet.
 * Extracted from PayrollSetupsPage.tsx.
 *
 * Never computes which dates are valid itself: the boundary choices come
 * from GET /payroll-setup/branches/{id}/assignment-choices via
 * useBoundaryChoices, and "Assign policy" is only enabled when the backend
 * says the chosen date is current and valid.
 */
import { useEffect, useState } from 'react';
import styles from './PayrollSetupsPage.module.css';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { PayrollBoundaryDateInput } from '../../../components/payroll/PayrollBoundaryDateInput';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import { assignPayrollSetup, getAssignmentChoices } from '../../../lib/payrollSetupApi';
import { readApiError } from '../../../lib/payrollSetupErrors';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { formatIsoLong, isValidIsoDate } from '../../../lib/isoDate';
import { isChoicesCurrent } from '../../../lib/payrollBoundaryView';
import { useBoundaryChoices } from '../../../lib/useBoundaryChoices';
import type { SetupResponse } from '../../../types/payrollSetup';

type AssignPolicyModalProps = {
  branchId: number;
  branchName: string;
  activeSetups: readonly SetupResponse[];
  mutating: boolean;
  setMutating: (value: boolean) => void;
  onClose: () => void;
  onAssigned: () => void;
  showToast: (message: string) => void;
};

export function AssignPolicyModal({
  branchId,
  branchName,
  activeSetups,
  mutating,
  setMutating,
  onClose,
  onAssigned,
  showToast,
}: AssignPolicyModalProps) {
  const [setupId, setSetupId] = useState<number | null>(null);
  const [effectiveFromDate, setEffectiveFromDate] = useState('');
  const [reason, setReason] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState<ApiErrorInfo | null>(null);

  const choicesKey = `${branchId}|${setupId ?? 'none'}`;
  const choicesFetcher = setupId != null ? (around?: string) => getAssignmentChoices(branchId, setupId, around) : null;
  const boundary = useBoundaryChoices(choicesFetcher, choicesKey, effectiveFromDate);

  useEffect(() => {
    function applySuggestedDate() {
      if (boundary.choices?.suggested) setEffectiveFromDate(boundary.choices.suggested.date);
    }
    if (effectiveFromDate === '' && boundary.choices?.suggested) {
      applySuggestedDate();
    }
  }, [boundary.choices, effectiveFromDate]);

  function handleSetupChange(value: string) {
    setSetupId(value === '' ? null : Number(value));
    setEffectiveFromDate('');
  }

  const choicesReady =
    boundary.choices != null && isChoicesCurrent(boundary.choices, effectiveFromDate) && boundary.choices.requested_valid;
  const canAssign = setupId != null && choicesReady;
  const selectedSetup = activeSetups.find((s) => s.setup_id === setupId) ?? null;

  const confirmMessage =
    selectedSetup != null && isValidIsoDate(effectiveFromDate)
      ? `Assign ${selectedSetup.setup_name} to ${branchName}, starting ${formatIsoLong(effectiveFromDate)}?`
      : '';

  async function confirmAssign() {
    if (!canAssign || setupId == null) {
      setConfirmOpen(false);
      return;
    }
    setMutating(true);
    try {
      await assignPayrollSetup(branchId, {
        setup_id: setupId,
        effective_from_date: effectiveFromDate,
        reason: reason.trim() === '' ? null : reason,
      });
      showToast('Policy assigned.');
      setConfirmOpen(false);
      onAssigned();
    } catch (err) {
      setError(readApiError(err, 'Failed to assign the policy.'));
      setConfirmOpen(false);
    } finally {
      setMutating(false);
    }
  }

  return (
    <div
      className={styles.modalOverlay}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className={styles.modal}>
        <div className={styles.modalHeader}>
          <h2 className={styles.modalTitle}>{`Assign policy to ${branchName}`}</h2>
          <button className={styles.modalCloseBtn} onClick={onClose}>
            ×
          </button>
        </div>
        <div className={styles.modalBody}>
          <div className={styles.formGroup}>
            <label className={styles.label}>Policy</label>
            <select
              className={styles.input}
              value={setupId ?? ''}
              onChange={(e) => handleSetupChange(e.target.value)}
              disabled={mutating}
            >
              <option value="">Select…</option>
              {activeSetups.map((s) => (
                <option key={s.setup_id} value={s.setup_id}>
                  {s.setup_name}
                </option>
              ))}
            </select>
          </div>

          {setupId != null && (
            <PayrollBoundaryDateInput
              id="assign-effective-date"
              label="Starts on"
              value={effectiveFromDate}
              onChange={setEffectiveFromDate}
              choices={boundary.choices}
              loading={boundary.loading}
              error={boundary.error}
              disabled={mutating}
              context="assignment"
            />
          )}

          <div className={styles.formGroup}>
            <label className={styles.label}>Reason (optional)</label>
            <textarea
              className={styles.textarea}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              disabled={mutating}
            />
          </div>

          {error && <PayrollErrorNotice info={error} />}
        </div>
        <div className={styles.modalFooter}>
          <button
            className={styles.btnPrimary}
            onClick={() => setConfirmOpen(true)}
            disabled={!canAssign || mutating}
          >
            Assign policy
          </button>
          <button className={styles.btnSecondary} onClick={onClose} disabled={mutating}>
            Cancel
          </button>
        </div>
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title="Assign policy?"
        message={confirmMessage}
        confirmLabel="Assign"
        loading={mutating}
        onConfirm={confirmAssign}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}
