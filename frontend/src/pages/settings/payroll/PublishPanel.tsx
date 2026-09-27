/**
 * Publish panel for the company-owned Payroll Policies page (Phase 6 Unit
 * A). Extracted from PayrollSetupsPage.tsx — owns the publish date, the
 * "replace an existing same-date change" checkbox, the automatic preview,
 * and the publish confirm/mutation.
 *
 * Preview-first stale safety is unchanged: publish only ever executes
 * `preview.inputs` (never the live form state directly), and only when
 * canPublishFromPreview(currentInputs, preview) says so — see
 * publishPreview.ts, kept intact. Publish is never retried on failure; only
 * the read-only preview is asked again (via a fresh auto-preview request).
 */
import { useEffect, useMemo, useState } from 'react';
import styles from './PayrollSetupsPage.module.css';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { PayrollBoundaryDateInput } from '../../../components/payroll/PayrollBoundaryDateInput';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import { getPublicationChoices, previewPublicationImpact, publishPayrollSetupDraft } from '../../../lib/payrollSetupApi';
import { readApiError, friendlyBoundaryConflictMessage } from '../../../lib/payrollSetupErrors';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { friendlyScheduleSummary } from '../../../lib/payrollSetupReadiness';
import { isValidIsoDate, formatIsoLong } from '../../../lib/isoDate';
import { buildAutoPreviewKey, useAutoPreview } from '../../../lib/useAutoPreview';
import { useBoundaryChoices } from '../../../lib/useBoundaryChoices';
import { isChoicesCurrent } from '../../../lib/payrollBoundaryView';
import { isDraftComplete, draftScheduleKey } from './draftEditor';
import { buildImpactRequest, buildPublishRequest, canPublishFromPreview } from './publishPreview';
import type { PreviewInputs, StoredPreview } from './publishPreview';
import type { DraftResponse, ScheduleResponse } from '../../../types/payrollSetup';

type BranchDirectoryEntry = { branch_id: number; branch_name: string };

type PublishPanelProps = {
  setupId: number;
  draft: DraftResponse;
  branches: readonly BranchDirectoryEntry[];
  mutating: boolean;
  setMutating: (value: boolean) => void;
  onClose: () => void;
  onPublished: (versionNumber: number) => void;
  showToast: (message: string) => void;
};

function branchName(branchId: number | null, branches: readonly BranchDirectoryEntry[]): string {
  if (branchId == null) return 'All branches';
  const found = branches.find((b) => b.branch_id === branchId);
  return found ? found.branch_name : `Branch #${branchId}`;
}

export function PublishPanel({
  setupId,
  draft,
  branches,
  mutating,
  setMutating,
  onClose,
  onPublished,
  showToast,
}: PublishPanelProps) {
  const [effectiveFromDate, setEffectiveFromDate] = useState('');
  const [replacesVersionId, setReplacesVersionId] = useState<number | null>(null);
  const [previewNonce, setPreviewNonce] = useState(0);
  const [publishConfirmOpen, setPublishConfirmOpen] = useState(false);
  const [publishError, setPublishError] = useState<ApiErrorInfo | null>(null);

  const choicesKey = `${setupId}|${draft.version_id}|${draftScheduleKey(draft)}`;
  const choicesFetcher = (around?: string) => getPublicationChoices(setupId, draft.version_id, around);
  const boundary = useBoundaryChoices(choicesFetcher, choicesKey, effectiveFromDate);

  // Apply the backend's own suggestion exactly once per key, and only while
  // the date is still blank — never overwrites something the user typed.
  useEffect(() => {
    function applySuggestedDate() {
      if (boundary.choices?.suggested) setEffectiveFromDate(boundary.choices.suggested.date);
    }
    if (effectiveFromDate === '' && boundary.choices?.suggested) {
      applySuggestedDate();
    }
  }, [boundary.choices, effectiveFromDate]);

  function handleDateChange(iso: string) {
    setEffectiveFromDate(iso);
    // A same-date replacement is specific to the date it was offered for —
    // changing the date must drop it so the next preview cannot send a
    // stale replaces_version_id.
    setReplacesVersionId(null);
  }

  const requestedChoice =
    boundary.choices && isChoicesCurrent(boundary.choices, effectiveFromDate) ? boundary.choices.requested : null;
  const replaceCandidateVersionId = requestedChoice?.replaces_version_id ?? null;
  const replaceCandidateVersionNumber = requestedChoice?.replaces_version_number ?? null;

  const currentPreviewInputs: PreviewInputs = useMemo(
    () => ({
      setupId,
      draftId: draft.version_id,
      draftScheduleKey: draftScheduleKey(draft),
      effectiveFromDate,
      replacesVersionId,
    }),
    [setupId, draft, effectiveFromDate, replacesVersionId],
  );

  const complete = isDraftComplete(draft);
  const dateValid = isValidIsoDate(effectiveFromDate);
  // The fetcher receives its own inputs snapshot from the hook (captured at
  // request time) — it must never close over `currentPreviewInputs`
  // directly, or the snapshot would just be whatever is current at the
  // moment the closure was created, defeating the point of the snapshot.
  const autoPreviewFetcher =
    complete && dateValid
      ? (previewInputs: PreviewInputs) =>
          previewPublicationImpact(setupId, draft.version_id, buildImpactRequest(previewInputs))
      : null;
  const requestKey = buildAutoPreviewKey([
    setupId,
    draft.version_id,
    draftScheduleKey(draft),
    effectiveFromDate,
    replacesVersionId,
    previewNonce,
  ]);
  const autoPreview = useAutoPreview(autoPreviewFetcher, currentPreviewInputs, requestKey);

  // `stored.inputs` is the hook's own independent snapshot of the inputs
  // that were actually sent — never re-stamped with `currentPreviewInputs`.
  // canPublishFromPreview's comparison of currentPreviewInputs against
  // stored.inputs is what actually detects staleness; see useAutoPreview.ts.
  const storedPreview: StoredPreview | null = autoPreview.stored;
  const canPublishNow =
    storedPreview != null && canPublishFromPreview(currentPreviewInputs, storedPreview) && !autoPreview.loading;

  async function confirmPublish() {
    if (!storedPreview || !canPublishFromPreview(currentPreviewInputs, storedPreview)) {
      setPublishConfirmOpen(false);
      return;
    }
    setMutating(true);
    try {
      // Always publish exactly what was previewed (storedPreview.inputs),
      // never the live currentPreviewInputs — even though
      // canPublishFromPreview just confirmed they match, the mutation
      // itself must be pinned to the reviewed snapshot.
      const resp = await publishPayrollSetupDraft(
        setupId,
        draft.version_id,
        buildPublishRequest(storedPreview.inputs),
      );
      showToast('Version published.');
      setPublishConfirmOpen(false);
      onPublished(resp.version_number);
    } catch (err) {
      setPublishError(readApiError(err, 'Failed to publish the schedule.'));
      setPublishConfirmOpen(false);
      // Never retry the publish itself — ask for a fresh read-only preview.
      setPreviewNonce((n) => n + 1);
    } finally {
      setMutating(false);
    }
  }

  const affectedLabels = storedPreview
    ? storedPreview.response.affected_branch_ids.map((id) => branchName(id, branches))
    : [];

  const publishConfirmMessage = storedPreview
    ? `This change starts ${formatIsoLong(storedPreview.inputs.effectiveFromDate)} and affects ` +
      `${affectedLabels.length} ${affectedLabels.length === 1 ? 'branch' : 'branches'}` +
      (replaceCandidateVersionNumber != null && replacesVersionId != null
        ? ' — it replaces the existing update that already starts on this date'
        : '') +
      '. Published policy history is never edited.'
    : '';

  const draftSchedule: ScheduleResponse | null = complete
    ? {
        payroll_frequency: draft.payroll_frequency as string,
        anchor_start_date: draft.anchor_start_date as string,
        custom_interval_days: draft.custom_interval_days,
        normal_days_off_mask: draft.normal_days_off_mask as number,
      }
    : null;

  return (
    <div className={styles.publishPanel}>
      <p className={styles.mutedText}>
        {draftSchedule ? friendlyScheduleSummary(draftSchedule) : 'Incomplete schedule'}
      </p>

      <PayrollBoundaryDateInput
        id="publish-effective-date"
        label="This change starts on"
        value={effectiveFromDate}
        onChange={handleDateChange}
        choices={boundary.choices}
        loading={boundary.loading}
        error={boundary.error}
        disabled={mutating}
        context="publication"
      />

      {replaceCandidateVersionNumber != null && (
        <>
          <label className={styles.correctionCheckboxRow}>
            <input
              type="checkbox"
              checked={replacesVersionId != null}
              onChange={(e) => setReplacesVersionId(e.target.checked ? replaceCandidateVersionId : null)}
              disabled={mutating}
            />
            <span>Replace the existing update already starting on this date</span>
          </label>
          <p className={styles.fieldHint}>Published policy history is never edited by a replacement.</p>
        </>
      )}

      {autoPreview.error && <PayrollErrorNotice info={autoPreview.error} />}

      {!autoPreview.error && !autoPreview.loading && storedPreview && (
        <div className={styles.previewResult}>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Branches affected</span>
            <span className={styles.detailValue}>
              {affectedLabels.length > 0 ? affectedLabels.join(', ') : 'None'}
            </span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>New schedule</span>
            <span className={styles.detailValue}>
              {friendlyScheduleSummary(storedPreview.response.successor_schedule)}
            </span>
          </div>
          {replaceCandidateVersionNumber != null && replacesVersionId != null && (
            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Replaces</span>
              <span className={styles.detailValue}>Existing update on this date</span>
            </div>
          )}
          {storedPreview.response.next_version_boundary != null && (
            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Next scheduled update</span>
              <span className={styles.detailValue}>
                {formatIsoLong(storedPreview.response.next_version_boundary)}
              </span>
            </div>
          )}
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Allowed</span>
            <span className={styles.detailValue}>{storedPreview.response.allowed ? 'Yes' : 'No'}</span>
          </div>

          {storedPreview.response.conflicts.length > 0 && (
            <>
              <ul className={styles.draftList}>
                {storedPreview.response.conflicts.map((c, i) => (
                  <li key={i} className={styles.assignmentMeta}>
                    {`${branchName(c.branch_id, branches)}: ${friendlyBoundaryConflictMessage(c.code, c.reason, c.reason)}`}
                  </li>
                ))}
              </ul>
              <details className={styles.mutedNote}>
                <summary>Technical details</summary>
                {storedPreview.response.conflicts.map((c, i) => (
                  <p key={i}>{`${c.code}: ${c.reason}`}</p>
                ))}
              </details>
            </>
          )}
        </div>
      )}

      {publishError && <PayrollErrorNotice info={publishError} />}

      <div className={styles.detailActions}>
        <button
          className={styles.btnPrimary}
          onClick={() => setPublishConfirmOpen(true)}
          disabled={!canPublishNow || mutating}
        >
          Publish
        </button>
        <button className={styles.btnSecondary} onClick={onClose} disabled={mutating}>
          Cancel
        </button>
      </div>

      <ConfirmDialog
        open={publishConfirmOpen}
        title="Publish this change?"
        message={publishConfirmMessage}
        confirmLabel="Publish"
        loading={mutating}
        onConfirm={confirmPublish}
        onCancel={() => setPublishConfirmOpen(false)}
      />
    </div>
  );
}
