/**
 * Draft editor modal (create + edit) for the company-owned Payroll Policies
 * page (Phase 6 Unit A). Extracted from PayrollSetupsPage.tsx.
 *
 * The 2-normal-days-off cap enforced here (disabling the other day toggles,
 * and validateDraftForm's matching check) is UI guidance only — the backend
 * (INVALID_NORMAL_DAYS_OFF) remains the only real enforcement point. A
 * prefilled source with more than 2 days off (should not exist) is shown
 * as-is, with the note, and is never silently changed.
 */
import { useMemo, useState } from 'react';
import styles from './PayrollSetupsPage.module.css';
import { DateInput } from '../../../components/ui/DateInput';
import { PayrollBoundaryDateInput } from '../../../components/payroll/PayrollBoundaryDateInput';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import {
  createPayrollSetupDraft,
  getInlinePublicationChoices,
  getPublicationChoices,
  previewInlinePublicationImpact,
  previewPublicationImpact,
  publishInlinePayrollSetup,
  publishPayrollSetupDraft,
  updatePayrollSetupDraft,
} from '../../../lib/payrollSetupApi';
import { readApiError } from '../../../lib/payrollSetupErrors';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { WEEKDAYS, canAddDayOff, frequencyLabel, friendlyScheduleSummary, toggleDay } from '../../../lib/payrollSetupReadiness';
import { formatIsoLong, isValidIsoDate } from '../../../lib/isoDate';
import { buildAutoPreviewKey, useAutoPreview } from '../../../lib/useAutoPreview';
import { useBoundaryChoices } from '../../../lib/useBoundaryChoices';
import { isChoicesCurrent } from '../../../lib/payrollBoundaryView';
import {
  draftFormFromSchedule,
  formAfterAnchorChange,
  formAfterFrequencyChange,
  formFromStartingPoint,
  toDraftPayload,
  toInlineSchedulePayload,
  validateReviewForm,
  validateSaveForLater,
  isScheduleFormComplete,
  frequencyChangeNeedsConfirmation,
  draftFormScheduleKey,
} from './draftEditor';
import type { DraftForm } from './draftEditor';
import {
  buildImpactRequest,
  buildInlinePublicationRequest,
  buildPublishRequest,
  canPublishFromPreview,
  canPublishInlineFromPreview,
  type InlinePreviewInputs,
  type PreviewInputs,
} from './publishPreview';
import type { VersionResponse } from '../../../types/payrollSetup';
import { startingPointLabel } from './policyDetailView';

const BLANK_START_FROM = 'blank';

const FREQUENCY_OPTIONS = [
  { value: 'Week', label: 'Weekly', description: 'Every week' },
  { value: 'Biweek', label: 'Biweekly', description: 'Every two weeks' },
  { value: 'Month', label: 'Monthly', description: 'Once each month' },
  { value: 'Custom', label: 'Custom', description: 'Set a specific first period' },
] as const;

type FrequencyValue = DraftForm['payroll_frequency'];

type FrequencySelectorProps = {
  value: FrequencyValue;
  disabled: boolean;
  onChange: (value: FrequencyValue) => void;
};

function FrequencySelector({ value, disabled, onChange }: FrequencySelectorProps) {
  return (
    <div className={styles.frequencySegmented} role="radiogroup" aria-label="Payroll frequency">
      {FREQUENCY_OPTIONS.map((option, index) => (
        <button
          key={option.value}
          type="button"
          className={[
            styles.frequencySegment,
            value === option.value ? styles.frequencySegmentSelected : '',
          ].filter(Boolean).join(' ')}
          role="radio"
          aria-checked={value === option.value}
          disabled={disabled}
          onClick={() => onChange(option.value)}
          onKeyDown={(event) => {
            if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
              event.preventDefault();
              onChange(FREQUENCY_OPTIONS[(index + 1) % FREQUENCY_OPTIONS.length].value);
            } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
              event.preventDefault();
              onChange(FREQUENCY_OPTIONS[(index - 1 + FREQUENCY_OPTIONS.length) % FREQUENCY_OPTIONS.length].value);
            }
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

type DraftEditorModalProps = {
  setupId: number;
  mode: 'create' | 'edit';
  editingDraftId: number | null;
  initialForm: DraftForm;
  versions: readonly VersionResponse[];
  canPublish: boolean;
  mutating: boolean;
  setMutating: (value: boolean) => void;
  onClose: () => void;
  onSaved: () => void;
  onDraftSaved: () => void;
  onPublished: (versionNumber: number) => void;
  showToast: (message: string) => void;
};

export function DraftEditorModal({
  setupId,
  mode,
  editingDraftId,
  initialForm,
  versions,
  canPublish,
  mutating,
  setMutating,
  onClose,
  onSaved,
  onDraftSaved,
  onPublished,
  showToast,
}: DraftEditorModalProps) {
  type EditorView = 'configure' | 'review';
  type EditorPreviewInputs = PreviewInputs | InlinePreviewInputs;

  const terminalVersions = useMemo(
    () => versions.filter((version) => version.lifecycle_state === 'Published' && version.is_terminal),
    [versions],
  );
  const defaultSource = terminalVersions.find((version) => version.is_current) ?? terminalVersions[terminalVersions.length - 1];
  const editorInitialForm: DraftForm =
    mode === 'create' && defaultSource
      ? {
          ...draftFormFromSchedule(defaultSource.schedule),
          // A source Version is a schedule template only. Its historical
          // effective date never becomes the new Version's effective date.
          planned_effective_from_date: initialForm.planned_effective_from_date,
        }
      : initialForm;
  const [form, setForm] = useState<DraftForm>(editorInitialForm);
  const [startFrom, setStartFrom] = useState<string>(
    mode === 'create' && defaultSource ? String(defaultSource.version_id) : BLANK_START_FROM,
  );
  const [view, setView] = useState<EditorView>('configure');
  const [error, setError] = useState<ApiErrorInfo | null>(null);
  const [replacesVersionId, setReplacesVersionId] = useState<number | null>(null);
  const [previewNonce, setPreviewNonce] = useState(0);
  const [publishConfirmOpen, setPublishConfirmOpen] = useState(false);
  const [pendingFrequency, setPendingFrequency] = useState<FrequencyValue | null>(null);
  const [pendingStartingPoint, setPendingStartingPoint] = useState<string | null>(null);
  const [templateBaselineKey, setTemplateBaselineKey] = useState(() => draftFormScheduleKey(editorInitialForm));
  const [effectiveDateOwnership, setEffectiveDateOwnership] = useState<'auto' | 'manual'>(
    () => (editorInitialForm.planned_effective_from_date === '' ? 'auto' : 'manual'),
  );

  const scheduleDirtySinceTemplate = draftFormScheduleKey(form) !== templateBaselineKey;

  function applyStartFrom(value: string) {
    const nextForm = formFromStartingPoint(form, value, versions);

    if (nextForm === null) return;
    setStartFrom(value);
    setForm(nextForm);
    setTemplateBaselineKey(draftFormScheduleKey(nextForm));
    setReplacesVersionId(null);
    setPendingStartingPoint(null);
  }

  function requestStartingPointChange(value: string) {
    if (value === startFrom) return;
    if (scheduleDirtySinceTemplate) {
      setPendingStartingPoint(value);
      return;
    }
    applyStartFrom(value);
  }

  const scheduleReady = isScheduleFormComplete(form);
  const scheduleKey = JSON.stringify([
    form.payroll_frequency,
    form.anchor_start_date,
    form.custom_period_end_date,
    form.normal_days_off_mask,
  ]);
  const boundaryKey = `${setupId}|${mode}|${editingDraftId ?? 'inline'}|${scheduleKey}`;
  const boundaryFetcher = scheduleReady
    ? (around?: string) =>
        mode === 'edit' && editingDraftId != null
          ? getPublicationChoices(setupId, editingDraftId, around)
          : getInlinePublicationChoices(setupId, toInlineSchedulePayload(form), around)
    : null;
  const boundary = useBoundaryChoices(boundaryFetcher, boundaryKey, form.planned_effective_from_date);

  function handleEffectiveDateChange(iso: string) {
    setForm((current) => ({ ...current, planned_effective_from_date: iso }));
    setEffectiveDateOwnership('manual');
    setReplacesVersionId(null);
  }

  function handleAnchorDateChange(iso: string) {
    setForm((current) => formAfterAnchorChange(current, iso, effectiveDateOwnership));
    setReplacesVersionId(null);
  }

  function applyFrequencyChange(nextFrequency: FrequencyValue) {
    setForm((current) => formAfterFrequencyChange(current, nextFrequency));
    setReplacesVersionId(null);
  }

  function requestFrequencyChange(nextFrequency: FrequencyValue) {
    if (nextFrequency === form.payroll_frequency) return;
    if (
      frequencyChangeNeedsConfirmation(
        form.payroll_frequency,
        nextFrequency,
        form.custom_period_end_date,
        form.custom_interval_days,
      )
    ) {
      setPendingFrequency(nextFrequency);
      return;
    }
    applyFrequencyChange(nextFrequency);
  }

  const requestedChoice =
    boundary.choices && isChoicesCurrent(boundary.choices, form.planned_effective_from_date)
      ? boundary.choices.requested
      : null;
  const replacementCandidateId = requestedChoice?.replaces_version_id ?? null;
  const replacementCandidateNumber = requestedChoice?.replaces_version_number ?? null;

  const currentPreviewInputs: EditorPreviewInputs = useMemo(
    () =>
      mode === 'create'
        ? {
            setupId,
            schedule: toInlineSchedulePayload(form),
            scheduleKey,
            effectiveFromDate: form.planned_effective_from_date,
            replacesVersionId,
          }
        : {
            setupId,
            draftId: editingDraftId ?? 0,
            draftScheduleKey: scheduleKey,
            effectiveFromDate: form.planned_effective_from_date,
            replacesVersionId,
          },
    [editingDraftId, form, mode, replacesVersionId, scheduleKey, setupId],
  );

  const requestKey = buildAutoPreviewKey([
    view,
    setupId,
    mode,
    editingDraftId,
    scheduleKey,
    form.planned_effective_from_date,
    replacesVersionId,
    previewNonce,
  ]);
  const autoPreviewFetcher =
    view === 'review' && canPublish && scheduleReady && isValidIsoDate(form.planned_effective_from_date)
      ? (inputs: EditorPreviewInputs) =>
          'schedule' in inputs
            ? previewInlinePublicationImpact(setupId, buildInlinePublicationRequest(inputs))
            : previewPublicationImpact(setupId, inputs.draftId, buildImpactRequest(inputs))
      : null;
  const autoPreview = useAutoPreview(autoPreviewFetcher, currentPreviewInputs, requestKey);
  const storedPreview = autoPreview.stored;
  const canPublishNow =
    storedPreview != null &&
    ('schedule' in currentPreviewInputs
      ? canPublishInlineFromPreview(currentPreviewInputs, storedPreview as { inputs: InlinePreviewInputs; response: typeof storedPreview.response })
      : canPublishFromPreview(currentPreviewInputs, storedPreview as { inputs: PreviewInputs; response: typeof storedPreview.response })) &&
    !autoPreview.loading;

  async function saveForLater() {
    const validationError = validateSaveForLater(form);
    if (validationError) {
      setError({ status: null, code: null, message: validationError });
      return;
    }
    setMutating(true);
    setError(null);
    try {
      if (mode === 'create') {
        await createPayrollSetupDraft(setupId, toDraftPayload(form));
        showToast('Version saved for later.');
      } else if (editingDraftId != null) {
        await updatePayrollSetupDraft(setupId, editingDraftId, toDraftPayload(form));
        showToast('Changes saved.');
      }
      onSaved();
    } catch (err) {
      setError(readApiError(err, 'Failed to save your changes.'));
    } finally {
      setMutating(false);
    }
  }

  async function reviewVersion() {
    const validationError = validateReviewForm(form);
    if (validationError) {
      setError({ status: null, code: null, message: validationError });
      return;
    }
    if (!canPublish) {
      setError({ status: null, code: null, message: 'You do not have permission to publish versions.' });
      return;
    }
    setMutating(true);
    setError(null);
    try {
      if (mode === 'edit' && editingDraftId != null) {
        await updatePayrollSetupDraft(setupId, editingDraftId, toDraftPayload(form));
        onDraftSaved();
      }
      setView('review');
    } catch (err) {
      setError(readApiError(err, 'Failed to save your changes before review.'));
    } finally {
      setMutating(false);
    }
  }

  async function confirmPublish() {
    if (!storedPreview || !canPublishNow) {
      setPublishConfirmOpen(false);
      return;
    }
    setMutating(true);
    setError(null);
    try {
      const response =
        'schedule' in storedPreview.inputs
          ? await publishInlinePayrollSetup(setupId, buildInlinePublicationRequest(storedPreview.inputs))
          : await publishPayrollSetupDraft(
              setupId,
              storedPreview.inputs.draftId,
              buildPublishRequest(storedPreview.inputs),
            );
      setPublishConfirmOpen(false);
      showToast('Version published.');
      onPublished(response.version_number);
    } catch (err) {
      setPublishConfirmOpen(false);
      setError(readApiError(err, 'Failed to publish the version.'));
      setPreviewNonce((nonce) => nonce + 1);
    } finally {
      setMutating(false);
    }
  }

  const completeSchedule = scheduleReady
    ? toInlineSchedulePayload(form)
    : null;
  const scheduleSummary = completeSchedule ? friendlyScheduleSummary(completeSchedule) : 'Incomplete schedule';
  const affectedBranchCount = storedPreview?.response.affected_branch_ids.length ?? 0;
  const publishConfirmationDate = form.planned_effective_from_date
    ? formatIsoLong(form.planned_effective_from_date)
    : 'the selected date';

  const daysSelectedCount = WEEKDAYS.filter((day) => (form.normal_days_off_mask & (1 << day.bit)) !== 0).length;

  return (
    <>
      <div
        className={styles.modalOverlay}
        onClick={(e) => {
          if (e.target === e.currentTarget) onClose();
        }}
      >
        <div className={styles.modal}>
          <div className={styles.modalHeader}>
            <div>
              <h2 className={styles.modalTitle}>
                {view === 'review' ? 'Review version' : mode === 'create' ? 'Create new version' : 'Continue version'}
              </h2>
              <p className={styles.modalSubtitle}>
                {view === 'review' ? 'Check this change before publishing it.' : 'Configure the payroll policy change.'}
              </p>
            </div>
            <button className={styles.modalCloseBtn} onClick={onClose} disabled={mutating}>
              ×
            </button>
          </div>

          <div className={styles.modalBody}>
            <div className={styles.modalFormColumn}>
            {view === 'configure' ? (
              <>
                {mode === 'create' && (
                  <div className={styles.formGroup}>
                    <label className={styles.label}>Starting point</label>
                    <select
                      className={styles.input}
                      value={startFrom}
                      onChange={(e) => requestStartingPointChange(e.target.value)}
                      disabled={mutating}
                    >
                      {terminalVersions.map((version) => (
                        <option key={version.version_id} value={String(version.version_id)}>
                          {startingPointLabel(version, versions)}
                        </option>
                      ))}
                      <option value={BLANK_START_FROM}>Start from scratch</option>
                    </select>
                    {startFrom !== BLANK_START_FROM && (
                      <p className={styles.fieldHint}>
                        Schedule values are copied into this new version. The published policy is not changed.
                      </p>
                    )}
                  </div>
                )}

                <div className={styles.formGroup}>
                  <label id="draft-frequency-label" className={styles.label}>Payroll frequency</label>
                  <FrequencySelector value={form.payroll_frequency} disabled={mutating} onChange={requestFrequencyChange} />
                </div>

                {form.payroll_frequency !== '' && (
                  <DateInput
                    id="draft-anchor-date"
                    label="First payroll period starts on"
                    value={form.anchor_start_date}
                    onChange={handleAnchorDateChange}
                    disabled={mutating}
                    required
                  />
                )}

                {form.payroll_frequency === 'Custom' && form.anchor_start_date !== '' && (
                  <>
                    <DateInput
                      id="draft-custom-period-end"
                      label="First payroll period ends on"
                      value={form.custom_period_end_date}
                      onChange={(iso) => setForm((current) => ({ ...current, custom_period_end_date: iso }))}
                      disabled={mutating}
                      required
                    />
                    {form.custom_period_end_date !== '' && completeSchedule?.custom_interval_days != null && (
                      <p className={styles.derivedField}>Cycle length: {completeSchedule.custom_interval_days} days</p>
                    )}
                  </>
                )}

                {form.payroll_frequency !== '' && (
                  <div className={styles.formGroup}>
                    <label className={styles.label}>Regular days off</label>
                    <div className={styles.dayToggleRow}>
                      {WEEKDAYS.map((day) => {
                        const active = (form.normal_days_off_mask & (1 << day.bit)) !== 0;
                        const disabled = mutating || (!active && !canAddDayOff(form.normal_days_off_mask));
                        return (
                          <button
                            key={day.bit}
                            type="button"
                            className={[
                              styles.dayToggle,
                              active ? styles.dayToggleActive : '',
                              disabled && !active ? styles.dayToggleDisabled : '',
                            ].filter(Boolean).join(' ')}
                            onClick={() =>
                              setForm((current) => ({
                                ...current,
                                normal_days_off_mask: toggleDay(current.normal_days_off_mask, day.bit),
                              }))
                            }
                            disabled={disabled}
                            aria-disabled={disabled}
                            title={disabled && !active && !mutating ? 'You can choose up to two normal days off.' : undefined}
                          >
                            {day.short}
                          </button>
                        );
                      })}
                    </div>
                    <p className={styles.fieldHint}>
                      {daysSelectedCount >= 2
                        ? 'You can choose up to two normal days off.'
                        : 'Choose up to two normal days off.'}
                    </p>
                  </div>
                )}

                {scheduleReady && (
                  <PayrollBoundaryDateInput
                    id="draft-effective-date"
                    label="This version takes effect on"
                    value={form.planned_effective_from_date}
                    onChange={handleEffectiveDateChange}
                    choices={boundary.choices}
                    loading={boundary.loading}
                    error={boundary.error}
                    disabled={mutating}
                    hint="Choose the payroll period when this version should begin."
                    showTechnicalDetails={false}
                    context="publication"
                  />
                )}

                {replacementCandidateNumber != null && (
                  <>
                    <label className={styles.correctionCheckboxRow}>
                      <input
                        type="checkbox"
                        checked={replacesVersionId != null}
                        onChange={(e) => setReplacesVersionId(e.target.checked ? replacementCandidateId : null)}
                        disabled={mutating}
                      />
                      <span>Replace the existing update already starting on this date</span>
                    </label>
                    <p className={styles.fieldHint}>Published policy history is never edited by a replacement.</p>
                  </>
                )}
              </>
            ) : (
              <div className={styles.reviewContent}>
                <div className={styles.reviewSummary}>
                  <div className={styles.detailRow}>
                    <span className={styles.detailLabel}>Payroll frequency</span>
                    <span className={styles.detailValue}>{completeSchedule ? frequencyLabel(completeSchedule.payroll_frequency) : '—'}</span>
                  </div>
                  <div className={styles.detailRow}>
                    <span className={styles.detailLabel}>First payroll period starts on</span>
                    <span className={styles.detailValue}>{form.anchor_start_date ? formatIsoLong(form.anchor_start_date) : '—'}</span>
                  </div>
                  {form.payroll_frequency === 'Custom' && (
                    <div className={styles.detailRow}>
                      <span className={styles.detailLabel}>First payroll period ends on</span>
                      <span className={styles.detailValue}>{form.custom_period_end_date ? formatIsoLong(form.custom_period_end_date) : '—'}</span>
                    </div>
                  )}
                  <div className={styles.detailRow}>
                    <span className={styles.detailLabel}>Payroll schedule</span>
                    <span className={styles.detailValue}>{scheduleSummary}</span>
                  </div>
                  <div className={styles.detailRow}>
                    <span className={styles.detailLabel}>This version takes effect on</span>
                    <span className={styles.detailValue}>{formatIsoLong(form.planned_effective_from_date)}</span>
                  </div>
                  {replacementCandidateNumber != null && replacesVersionId != null && (
                    <div className={styles.detailRow}>
                      <span className={styles.detailLabel}>Replacement</span>
                    <span className={styles.detailValue}>Replaces the existing update on this date</span>
                    </div>
                  )}
                </div>

                {autoPreview.error && <PayrollErrorNotice info={autoPreview.error} />}
                {!autoPreview.error && !autoPreview.loading && storedPreview && (
                  <div className={styles.previewResult}>
                    <div className={styles.detailRow}>
                      <span className={styles.detailLabel}>Branches affected</span>
                      <span className={styles.detailValue}>{affectedBranchCount || 'None'}</span>
                    </div>
                    {storedPreview.response.next_version_boundary != null && (
                      <div className={styles.detailRow}>
                        <span className={styles.detailLabel}>Next scheduled update</span>
                        <span className={styles.detailValue}>{formatIsoLong(storedPreview.response.next_version_boundary)}</span>
                      </div>
                    )}
                    {storedPreview.response.conflicts.length > 0 && (
                      <PayrollErrorNotice
                        info={{
                          status: null,
                          code: storedPreview.response.conflicts[0].code,
                          message: storedPreview.response.conflicts[0].reason,
                        }}
                      />
                    )}
                  </div>
                )}
              </div>
            )}

            {error && <PayrollErrorNotice info={error} />}
            </div>
          </div>

          <div className={styles.modalFooter}>
            {view === 'review' ? (
              <>
                <button className={styles.btnSecondary} onClick={() => setView('configure')} disabled={mutating}>
                  Back
                </button>
                <button
                  className={styles.btnPrimary}
                  onClick={() => setPublishConfirmOpen(true)}
                  disabled={mutating || !canPublishNow}
                >
                  {mutating ? 'Working…' : 'Publish version'}
                </button>
              </>
            ) : (
              <>
                <button className={styles.btnSecondary} onClick={onClose} disabled={mutating}>
                  Cancel
                </button>
                <button className={styles.btnSecondary} onClick={saveForLater} disabled={mutating}>
                  {mutating ? 'Working…' : mode === 'create' ? 'Save for later' : 'Save changes'}
                </button>
                <button className={styles.btnPrimary} onClick={reviewVersion} disabled={mutating || !canPublish}>
                  Review & publish
                </button>
              </>
            )}
          </div>
        </div>
      </div>
      <ConfirmDialog
        open={pendingStartingPoint !== null}
        title="Change starting point?"
        message={
          pendingStartingPoint === BLANK_START_FROM
            ? 'Your current schedule edits will be cleared.'
            : 'Your current schedule edits will be replaced with the selected starting point.'
        }
        confirmLabel={pendingStartingPoint === BLANK_START_FROM ? 'Start from scratch' : 'Use selected version'}
        loading={mutating}
        onConfirm={() => {
          if (pendingStartingPoint !== null) applyStartFrom(pendingStartingPoint);
        }}
        onCancel={() => setPendingStartingPoint(null)}
      />
      <ConfirmDialog
        open={pendingFrequency !== null}
        title="Change payroll frequency?"
        message="Custom period details will be cleared and will need to be entered again."
        confirmLabel="Change frequency"
        loading={mutating}
        onConfirm={() => {
          if (pendingFrequency === null) return;
          applyFrequencyChange(pendingFrequency);
          setPendingFrequency(null);
        }}
        onCancel={() => setPendingFrequency(null)}
      />
      <ConfirmDialog
        open={publishConfirmOpen}
        title="Publish version?"
        message={`This version takes effect ${publishConfirmationDate} and affects ${affectedBranchCount} ${affectedBranchCount === 1 ? 'branch' : 'branches'}. Published policy history is never edited.`}
        confirmLabel="Publish version"
        loading={mutating}
        onConfirm={confirmPublish}
        onCancel={() => setPublishConfirmOpen(false)}
      />
    </>
  );
}
