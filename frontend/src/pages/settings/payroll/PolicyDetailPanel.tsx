/**
 * Selected Payroll Policy details, actions, Version Planning, and Timeline.
 * Additional lower sections remain controlled by showLowerSections.
 *
 * Never constructs a JS Date object or reads the wall clock — every date
 * shown here is an ISO string from the backend, rendered as-is or through
 * isoDate's pure calendar formatting.
 */
import { useMemo, useState } from 'react';
import { archivePayrollSetup, clearDefaultPayrollSetup, discardPayrollSetupDraft, setDefaultPayrollSetup, updatePayrollSetup } from '../../../lib/payrollSetupApi';
import { readApiError } from '../../../lib/payrollSetupErrors';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { currentVersion, effectiveDisplayVersionNumbers, hasPublishedVersion, policyScheduleFields } from './policyDetailView';
import type { DraftResponse } from '../../../types/payrollSetup';
import { archiveBlockedReason, assignedPolicyBranchRows, normalizeDescription, policyNextSteps, validateSetupUpdate } from './payrollSetupsView';
import { draftFormFromDraft, emptyDraftForm } from './draftEditor';
import type { DraftForm } from './draftEditor';
import { VersionPlanningSection } from './VersionPlanningSection';
import { upcomingPublishedVersions } from './versionPlanningView';
import { usePolicyDetailData } from './usePolicyDetailData';
import type { ActionError, ConfirmKind, DraftActionError, EditForm, PolicyDetailPanelProps, DraftEditorMode } from './policyDetailTypes';
import { PolicyDetailHeader } from './PolicyDetailHeader';
import { VersionTimelineSection } from './VersionTimelineSection';
import { PolicyDetailLowerSections } from './PolicyDetailLowerSections';
import { PolicyDetailDialogs } from './PolicyDetailDialogs';

// ── Component ────────────────────────────────────────────────────────────────

export function PolicyDetailPanel({
  selectedPolicy,
  usageState,
  defaultSetupId,
  summaries,
  summariesLoading,
  summariesError,
  canManage,
  canAssign,
  canPublish,
  mutating,
  setMutating,
  onReloadSetups,
  onGoToAssignments,
  showToast,
  showLowerSections = false,
}: PolicyDetailPanelProps) {
  const selectedPolicyId = selectedPolicy?.setup_id ?? null;

  // A lightweight branch directory (id -> name), used only to label a
  // branch id (publish/preview conflict tables).
  const branchDirectory = useMemo(
    () => summaries.map((s) => ({ branch_id: s.branch_id, branch_name: s.branch_name })),
    [summaries],
  );

  // ── Mutations: confirm-gated (clear default / set default / archive) ──────
  const [confirmKind, setConfirmKind] = useState<ConfirmKind | null>(null);
  const [actionError, setActionError] = useState<ActionError | null>(null);

  function openConfirm(kind: ConfirmKind) {
    setActionError(null);
    setConfirmKind(kind);
  }

  async function handleConfirm() {
    if (!confirmKind) return;
    if (confirmKind !== 'clear-default' && !selectedPolicy) {
      setConfirmKind(null);
      return;
    }
    // Capture which policy this action targets before awaiting — the user
    // may switch the selected policy while the request is in flight, and
    // the resulting error must stay attached to the policy it actually
    // happened on, not whichever policy happens to be selected when the
    // catch runs.
    const targetSetupId = confirmKind === 'clear-default' ? null : selectedPolicy?.setup_id ?? null;
    setMutating(true);
    try {
      if (confirmKind === 'clear-default') {
        await clearDefaultPayrollSetup();
        showToast('Company default cleared.');
        await onReloadSetups();
      } else if (confirmKind === 'set-default' && selectedPolicy) {
        await setDefaultPayrollSetup(selectedPolicy.setup_id);
        showToast('Company default updated.');
        await onReloadSetups();
      } else if (confirmKind === 'archive' && selectedPolicy) {
        await archivePayrollSetup(selectedPolicy.setup_id);
        showToast('Policy archived.');
        await onReloadSetups();
      }
      setConfirmKind(null);
    } catch (err) {
      const kind: ActionError['kind'] = confirmKind === 'archive' ? 'archive' : 'default';
      setActionError({ kind, setupId: targetSetupId, info: readApiError(err, 'The request failed.') });
      setConfirmKind(null);
    } finally {
      setMutating(false);
    }
  }

  // ── Edit policy metadata modal ───────────────────────────────────────────
  const [editOpen, setEditOpen] = useState(false);
  const [editForm, setEditForm] = useState<EditForm>({ setup_name: '', description: '' });
  const [editError, setEditError] = useState<ApiErrorInfo | null>(null);

  function openEditModal() {
    if (!selectedPolicy) return;
    setEditForm({ setup_name: selectedPolicy.setup_name, description: selectedPolicy.description ?? '' });
    setEditError(null);
    setEditOpen(true);
  }

  function closeEditModal() {
    setEditOpen(false);
  }

  async function submitEdit() {
    if (!selectedPolicy) return;
    const validationError = validateSetupUpdate({ setup_name: editForm.setup_name });
    if (validationError) {
      setEditError({ status: null, code: null, message: validationError });
      return;
    }
    setMutating(true);
    setEditError(null);
    try {
      await updatePayrollSetup(selectedPolicy.setup_id, {
        setup_name: editForm.setup_name,
        description: normalizeDescription(editForm.description),
      });
      setEditOpen(false);
      showToast('Policy details saved.');
      await onReloadSetups();
    } catch (err) {
      setEditError(readApiError(err, 'Failed to save policy details.'));
    } finally {
      setMutating(false);
    }
  }

  // ── Policy updates ("Versions") + Drafts of the selected policy ──────────
  // Share one reload key so publish (which changes both) can refresh both
  // from the backend without synthesizing local state.
  const { versions, versionsLoading, versionsError, drafts, draftsLoading, draftsError, setSetupDataReloadKey } = usePolicyDetailData(selectedPolicyId);
  const displayVersionNumbers = useMemo(() => effectiveDisplayVersionNumbers(versions), [versions]);

  // ── Draft editor modal (create + edit share it) ─────────────────────────
  const [draftEditorOpen, setDraftEditorOpen] = useState(false);
  const [draftEditorMode, setDraftEditorMode] = useState<DraftEditorMode>('create');
  const [editingDraftId, setEditingDraftId] = useState<number | null>(null);
  const [draftEditorInitialForm, setDraftEditorInitialForm] = useState<DraftForm>(emptyDraftForm());

  function openCreateDraftModal() {
    setDraftEditorMode('create');
    setEditingDraftId(null);
    setDraftEditorInitialForm(emptyDraftForm());
    setDraftEditorOpen(true);
  }

  function openEditDraftModal(draft: DraftResponse) {
    setDraftEditorMode('edit');
    setEditingDraftId(draft.version_id);
    setDraftEditorInitialForm(draftFormFromDraft(draft));
    setDraftEditorOpen(true);
  }

  function closeDraftEditorModal() {
    setDraftEditorOpen(false);
  }

  // ── Discard Draft confirm ────────────────────────────────────────────────
  const [discardTarget, setDiscardTarget] = useState<DraftResponse | null>(null);
  const [draftActionError, setDraftActionError] = useState<DraftActionError | null>(null);

  function openDiscardConfirm(draft: DraftResponse) {
    setDraftActionError(null);
    setDiscardTarget(draft);
  }

  async function confirmDiscardDraft() {
    if (!selectedPolicy || !discardTarget) return;
    // Capture the target — and whether the Publish panel is currently open
    // on it — before awaiting: the user may change what's selected/open
    // while the request is in flight.
    const targetSetupId = selectedPolicy.setup_id;
    const targetDraftId = discardTarget.version_id;
    const publishPanelOpenOnThisDraft = openPublishDraftId === targetDraftId;
    setMutating(true);
    try {
      await discardPayrollSetupDraft(targetSetupId, targetDraftId);
      showToast('Draft schedule discarded.');
      setDiscardTarget(null);
      if (publishPanelOpenOnThisDraft) {
        setPublishDraft(null);
      }
      setSetupDataReloadKey((k) => k + 1);
    } catch (err) {
      setDraftActionError({
        kind: 'discard',
        setupId: targetSetupId,
        draftId: targetDraftId,
        info: readApiError(err, 'Failed to discard the draft schedule.'),
      });
      setDiscardTarget(null);
    } finally {
      setMutating(false);
    }
  }

  // ── Publish panel (preview-first — see PublishPanel.tsx) ─────────────────
  const [publishDraft, setPublishDraft] = useState<DraftResponse | null>(null);
  // Read once, alongside the state declaration, rather than inline at each
  // comparison site — reading publishDraft directly next to a conditional
  // call to its own setter elsewhere defeats React Compiler's memoization
  // analysis for other hooks in this component.
  const openPublishDraftId = publishDraft?.version_id ?? null;

  // The Draft as currently loaded from the backend (not a stale snapshot
  // from when the panel was opened).
  const currentPublishDraft = useMemo(() => {
    if (!publishDraft) return null;
    return drafts.find((d) => d.version_id === publishDraft.version_id) ?? publishDraft;
  }, [drafts, publishDraft]);

  // Adaptation required by the component boundary: the shell's old
  // selectSetup() cleared actionError/draftActionError/publishDraft inline,
  // in the same click handler that changed the URL-backed selection. Now
  // that those three states live here (not in the shell), the equivalent
  // reset happens by comparing the selected policy id against a
  // tracked-in-state previous value during render (React's documented
  // "adjusting state when a prop changes" pattern) — same states cleared,
  // same trigger (the selection changing), just expressed across the
  // component boundary instead of inline in the click handler, and without
  // the extra render tick an effect-based reset would cost.
  const [trackedPolicyId, setTrackedPolicyId] = useState(selectedPolicyId);
  if (trackedPolicyId !== selectedPolicyId) {
    setTrackedPolicyId(selectedPolicyId);
    setActionError(null);
    setDraftActionError(null);
    setPublishDraft(null);
  }

  // ── Derived: assigned branches for the selected policy (from summaries) ──
  const assignedBranchRows = useMemo(() => {
    if (!selectedPolicy) return [];
    return assignedPolicyBranchRows(summaries, selectedPolicy.setup_id);
  }, [summaries, selectedPolicy]);

  const archiveReason = selectedPolicy
    ? archiveBlockedReason(selectedPolicy.setup_id, defaultSetupId)
    : null;

  const archiveMessage = selectedPolicy
    ? `Archive "${selectedPolicy.setup_name}"? The server rejects archiving while any assignment could still govern new payroll periods.` +
      (!summariesLoading && !summariesError && assignedBranchRows.length > 0
        ? ` Currently followed by: ${assignedBranchRows.map((r) => r.branch_name).join(', ')}.`
        : '')
    : '';

  // ── "Next steps" onboarding guide for the selected policy ────────────────
  const nextSteps = useMemo(() => {
    if (!selectedPolicy) return [];
    return policyNextSteps({
      hasDrafts: drafts.length > 0,
      hasPublishedVersions: versions.length > 0,
      hasCompanyDefault: defaultSetupId != null,
      hasAnyBranchFollowing: assignedBranchRows.length > 0,
      canAssign,
    });
  }, [selectedPolicy, drafts, versions, defaultSetupId, assignedBranchRows, canAssign]);

  const NEXT_STEP_TEXT: Record<string, string> = {
    'add-draft': 'Add a draft schedule.',
    publish: 'Publish the schedule.',
    'set-default': 'Make it the company default.',
    'assign-branches': 'Assign branches to this policy.',
  };

  function handleNextStepAction(kind: string) {
    if (kind === 'add-draft' && canManage) openCreateDraftModal();
    else if (kind === 'publish' && canPublish && drafts[0]) setPublishDraft(drafts[0]);
    else if (kind === 'set-default' && canAssign) openConfirm('set-default');
    else if (kind === 'assign-branches') onGoToAssignments();
  }

  const nextStepPermitted: Record<string, boolean> = {
    'add-draft': canManage,
    publish: canPublish,
    'set-default': canAssign,
    'assign-branches': true,
  };

  // Company Default prerequisite: at least one Published Version (the server enforces it
  // too). Unknown while versions load or fail to load, so the action stays disabled then.
  const versionsLoaded = !versionsLoading && !versionsError;
  const defaultBlockedByNoPublishedVersion =
    selectedPolicy != null && versionsLoaded && !hasPublishedVersion(versions, selectedPolicy.setup_id);
  const defaultPrerequisiteMet =
    selectedPolicy != null && versionsLoaded && hasPublishedVersion(versions, selectedPolicy.setup_id);

  const scheduleFields = policyScheduleFields(
    selectedPolicy ? currentVersion(versions, selectedPolicy.setup_id) : null,
  );
  const upcomingVersions = useMemo(() => upcomingPublishedVersions(versions), [versions]);
  const canCreateVersion = selectedPolicy != null && selectedPolicy.status === 'Active' && canManage;

  // ─────────────────────────────────────────────────────────────────────────
  return (
    <>
      {selectedPolicy && (
        <PolicyDetailHeader
          selectedPolicy={selectedPolicy}
          usageState={usageState}
          defaultSetupId={defaultSetupId}
          openConfirm={openConfirm}
          openEditModal={openEditModal}
          mutating={mutating}
          defaultPrerequisiteMet={defaultPrerequisiteMet}
          defaultBlockedByNoPublishedVersion={defaultBlockedByNoPublishedVersion}
          canAssign={canAssign}
          canManage={canManage}
          actionError={actionError}
          selectedPolicyId={selectedPolicyId}
          versionsError={versionsError}
          versionsLoading={versionsLoading}
          scheduleFields={scheduleFields}
        />
      )}
      {selectedPolicy && (
        <VersionPlanningSection
          upcomingVersions={upcomingVersions}
          drafts={drafts}
          versionsLoading={versionsLoading}
          versionsError={versionsError}
          draftsLoading={draftsLoading}
          draftsError={draftsError}
          canCreate={canCreateVersion}
          mutating={mutating}
          draftActionError={
            draftActionError
              ? { draftId: draftActionError.draftId, info: draftActionError.info }
              : null
          }
          onCreate={openCreateDraftModal}
          onEditDraft={openEditDraftModal}
          onDiscardDraft={openDiscardConfirm}
        />
      )}

      {selectedPolicy && (
        <VersionTimelineSection
          versions={versions}
          loading={versionsLoading}
          error={versionsError}
        />
      )}

      {showLowerSections && (
        <PolicyDetailLowerSections
          selectedPolicy={selectedPolicy}
          selectedPolicyId={selectedPolicyId}
          nextSteps={nextSteps}
          nextStepText={NEXT_STEP_TEXT}
          nextStepPermitted={nextStepPermitted}
          handleNextStepAction={handleNextStepAction}
          openConfirm={openConfirm}
          mutating={mutating}
          canManage={canManage}
          canPublish={canPublish}
          archiveReason={archiveReason}
          actionError={actionError}
          drafts={drafts}
          draftsError={draftsError}
          draftsLoading={draftsLoading}
          openCreateDraftModal={openCreateDraftModal}
          openEditDraftModal={openEditDraftModal}
          openDiscardConfirm={openDiscardConfirm}
          draftActionError={draftActionError}
          publishDraft={publishDraft}
          currentPublishDraft={currentPublishDraft}
          setPublishDraft={setPublishDraft}
          setSetupDataReloadKey={setSetupDataReloadKey}
          branchDirectory={branchDirectory}
          setMutating={setMutating}
          showToast={showToast}
          versions={versions}
          displayVersionNumbers={displayVersionNumbers}
          versionsError={versionsError}
          versionsLoading={versionsLoading}
          summariesError={summariesError}
          summariesLoading={summariesLoading}
          assignedBranchRows={assignedBranchRows}
        />
      )}

      <PolicyDetailDialogs
        confirmKind={confirmKind}
        setConfirmKind={setConfirmKind}
        mutating={mutating}
        handleConfirm={handleConfirm}
        discardTarget={discardTarget}
        setDiscardTarget={setDiscardTarget}
        archiveMessage={archiveMessage}
        confirmDiscardDraft={confirmDiscardDraft}
        editOpen={editOpen}
        selectedPolicy={selectedPolicy}
        closeEditModal={closeEditModal}
        editForm={editForm}
        setEditForm={setEditForm}
        editError={editError}
        submitEdit={submitEdit}
        draftEditorOpen={draftEditorOpen}
        draftEditorMode={draftEditorMode}
        editingDraftId={editingDraftId}
        draftEditorInitialForm={draftEditorInitialForm}
        versions={versions}
        canPublish={canPublish}
        setMutating={setMutating}
        closeDraftEditorModal={closeDraftEditorModal}
        setDraftEditorOpen={setDraftEditorOpen}
        setSetupDataReloadKey={setSetupDataReloadKey}
        showToast={showToast}
      />

    </>
  );
}
