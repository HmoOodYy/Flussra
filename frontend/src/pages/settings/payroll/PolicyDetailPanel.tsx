/**
 * Policy detail content for the company-owned Payroll Policies page
 * (Phase 6 — master-detail redesign, Step 1 extraction).
 *
 * This holds ALL of the business/API logic that used to render in
 * PayrollSetupsPage.tsx's detail column: the company-default card, the
 * selected policy's fields + actions (edit/mark-default/archive), the
 * "Next steps" guide, the draft schedules list + draft editor modal, the
 * publish panel mount, the read-only policy-updates table, the assigned-
 * branches table, and their confirms/modals.
 *
 * Since Step 3A the page mounts this component with only the header +
 * "Policy details" section visible. Every lower section (Next steps/archive,
 * Draft schedules, Publish, Policy updates, Assigned branches) stays here,
 * fully wired and type-checked, behind the `showLowerSections` prop (default
 * false) until its own step mounts it.
 *
 * Moved, not rewritten: every handler/state/derivation below is the same
 * logic that lived in the shell, adapted only where crossing the component
 * boundary requires it (documented inline where that happens).
 *
 * Never constructs a JS Date object or reads the wall clock — every date
 * shown here is an ISO string from the backend, rendered as-is or through
 * isoDate's pure calendar formatting.
 */
import { useEffect, useMemo, useReducer, useState } from 'react';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { SectionCard } from '../../../components/ui';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import {
  archivePayrollSetup,
  clearDefaultPayrollSetup,
  discardPayrollSetupDraft,
  listPayrollSetupDrafts,
  listPayrollSetupVersions,
  setDefaultPayrollSetup,
  updatePayrollSetup,
} from '../../../lib/payrollSetupApi';
import { readApiError } from '../../../lib/payrollSetupErrors';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { friendlyScheduleSummary } from '../../../lib/payrollSetupReadiness';
import {
  currentVersion,
  effectiveDisplayVersionNumbers,
  hasPublishedVersion,
  policyScheduleFields,
  timelineDateRange,
  timelinePublishedVersions,
  timelineScheduleSummary,
  usageStateLabel,
} from './policyDetailView';
import type { PolicyUsageState } from './policyMasterView';
import { formatIsoLong } from '../../../lib/isoDate';
import type {
  BranchPolicySummaryResponse,
  DraftResponse,
  SetupResponse,
  VersionResponse,
} from '../../../types/payrollSetup';
import {
  archiveBlockedReason,
  assignedPolicyBranchRows,
  draftCreatedLabel,
  normalizeDescription,
  policyNextSteps,
  validateSetupUpdate,
  versionRelationship,
} from './payrollSetupsView';
import { draftFormFromDraft, emptyDraftForm, isDraftComplete } from './draftEditor';
import type { DraftForm } from './draftEditor';
import { DraftEditorModal } from './DraftEditorModal';
import { PublishPanel } from './PublishPanel';
import { VersionPlanningSection } from './VersionPlanningSection';
import { upcomingPublishedVersions } from './versionPlanningView';
import styles from './PayrollSetupsPage.module.css';

// ── Confirm actions ──────────────────────────────────────────────────────────

type ConfirmKind = 'clear-default' | 'set-default' | 'archive';

interface ActionError {
  kind: 'default' | 'archive';
  /** The policy this error belongs to; null for clear-default (not tied to any policy). */
  setupId: number | null;
  info: ApiErrorInfo;
}

interface EditForm {
  setup_name: string;
  description: string;
}

interface DraftActionError {
  kind: 'discard';
  setupId: number;
  draftId: number;
  info: ApiErrorInfo;
}

// ── Async data-loading reducers (Policy updates / Drafts of the selected policy) ─

interface VersionsState {
  versions: VersionResponse[];
  loading: boolean;
  error: ApiErrorInfo | null;
}
type VersionsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; versions: VersionResponse[] }
  | { type: 'FETCH_ERROR'; error: ApiErrorInfo }
  | { type: 'CLEAR' };

function versionsReducer(s: VersionsState, a: VersionsAction): VersionsState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true, error: null };
    case 'FETCH_OK': return { versions: a.versions, loading: false, error: null };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
    case 'CLEAR': return { versions: [], loading: false, error: null };
  }
}

interface DraftsState {
  drafts: DraftResponse[];
  loading: boolean;
  error: ApiErrorInfo | null;
}
type DraftsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; drafts: DraftResponse[] }
  | { type: 'FETCH_ERROR'; error: ApiErrorInfo }
  | { type: 'CLEAR' };

function draftsReducer(s: DraftsState, a: DraftsAction): DraftsState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true, error: null };
    case 'FETCH_OK': return { drafts: a.drafts, loading: false, error: null };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
    case 'CLEAR': return { drafts: [], loading: false, error: null };
  }
}

type VersionTimelineSectionProps = {
  versions: readonly VersionResponse[];
  loading: boolean;
  error: ApiErrorInfo | null;
};

function VersionTimelineSection({ versions, loading, error }: VersionTimelineSectionProps) {
  const timelineVersions = useMemo(() => timelinePublishedVersions(versions), [versions]);
  const displayNumbers = useMemo(() => effectiveDisplayVersionNumbers(versions), [versions]);

  return (
    <section className={styles.versionTimeline} aria-labelledby="version-timeline-title">
      <div className={styles.versionTimelineHeader}>
        <div>
          <h3 id="version-timeline-title" className={styles.versionTimelineTitle}>Version Timeline</h3>
          <p className={styles.versionTimelineSubtitle}>See the published policy changes that have taken effect.</p>
        </div>
      </div>
      {error ? (
        <div className={styles.versionTimelineMessage}>
          <PayrollErrorNotice info={error} />
        </div>
      ) : loading ? (
        <p className={styles.versionTimelineMessage}>Loading published versions…</p>
      ) : timelineVersions.length === 0 ? (
        <p className={styles.versionTimelineMessage}>No version has taken effect yet.</p>
      ) : (
        <ol className={styles.versionTimelineList}>
          {timelineVersions.map((version) => (
            <li key={version.version_id} className={styles.versionTimelineItem}>
              <span className={styles.versionTimelineNode} aria-hidden="true" />
              <article className={styles.versionTimelineCard}>
                <div className={styles.versionTimelineCardHeader}>
                  <div className={styles.versionTimelineVersion}>
                    <strong>{`Version ${displayNumbers.get(version.version_id) ?? '—'}`}</strong>
                    {version.is_current && <span className={styles.versionTimelineCurrent}>Current</span>}
                  </div>
                  <span className={styles.versionTimelineDate}>{timelineDateRange(version)}</span>
                </div>
                <p className={styles.versionTimelineSummary}>{timelineScheduleSummary(version)}</p>
              </article>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

// ── Component ────────────────────────────────────────────────────────────────

type PolicyDetailPanelProps = {
  selectedPolicy: SetupResponse | null;
  /** Derived usage state (policyMasterView.policyUsage) — never the raw setup.status. */
  usageState: PolicyUsageState;
  defaultSetupId: number | null;
  summaries: readonly BranchPolicySummaryResponse[];
  summariesLoading: boolean;
  summariesError: ApiErrorInfo | null;
  canManage: boolean;
  canAssign: boolean;
  canPublish: boolean;
  mutating: boolean;
  setMutating: (value: boolean) => void;
  /** Refreshes the shell's setups list + company default (e.g. after archive/edit/default changes). */
  onReloadSetups: () => Promise<void>;
  /** Switches the shell to the Branch Assignments tab (the "Assign branches" next step). */
  onGoToAssignments: () => void;
  showToast: (message: string) => void;
  /** Mounts the not-yet-designed lower sections. Off until their steps land. */
  showLowerSections?: boolean;
};

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
  const [setupDataReloadKey, setSetupDataReloadKey] = useState(0);

  const [versionsState, dispatchVersions] = useReducer(versionsReducer, {
    versions: [],
    loading: false,
    error: null,
  });
  const { versions, loading: versionsLoading, error: versionsError } = versionsState;
  const displayVersionNumbers = useMemo(() => effectiveDisplayVersionNumbers(versions), [versions]);

  useEffect(() => {
    if (selectedPolicyId == null) {
      dispatchVersions({ type: 'CLEAR' });
      return;
    }
    let active = true;
    dispatchVersions({ type: 'FETCH_START' });
    listPayrollSetupVersions(selectedPolicyId)
      .then((v) => {
        if (active) dispatchVersions({ type: 'FETCH_OK', versions: v });
      })
      .catch((err) => {
        if (active) dispatchVersions({ type: 'FETCH_ERROR', error: readApiError(err, 'Failed to load policy updates.') });
      });
    return () => {
      active = false;
    };
  }, [selectedPolicyId, setupDataReloadKey]);

  const [draftsState, dispatchDrafts] = useReducer(draftsReducer, {
    drafts: [],
    loading: false,
    error: null,
  });
  const { drafts, loading: draftsLoading, error: draftsError } = draftsState;

  useEffect(() => {
    if (selectedPolicyId == null) {
      dispatchDrafts({ type: 'CLEAR' });
      return;
    }
    let active = true;
    dispatchDrafts({ type: 'FETCH_START' });
    listPayrollSetupDrafts(selectedPolicyId)
      .then((d) => {
        if (active) dispatchDrafts({ type: 'FETCH_OK', drafts: d });
      })
      .catch((err) => {
        if (active) dispatchDrafts({ type: 'FETCH_ERROR', error: readApiError(err, 'Failed to load draft schedules.') });
      });
    return () => {
      active = false;
    };
  }, [selectedPolicyId, setupDataReloadKey]);

  // ── Draft editor modal (create + edit share it) ─────────────────────────
  type DraftEditorMode = 'create' | 'edit';
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
      {/* ── Header: name, usage state, company default, edit ── */}
      {selectedPolicy && (
        <div className={styles.policyHeader}>
          <div className={styles.policyHeaderTop}>
            <div className={styles.policyHeaderMain}>
              <div className={styles.policyTitleRow}>
                <h2 className={styles.policyTitle}>{selectedPolicy.setup_name}</h2>
                <span
                  className={
                    usageState === 'Active'
                      ? `${styles.badge} ${styles.badgeGreen}`
                      : usageState === 'Archived'
                        ? `${styles.badge} ${styles.badgeMuted}`
                        : `${styles.badge} ${styles.badgeGray}`
                  }
                >
                  {usageStateLabel(usageState)}
                </span>
                {selectedPolicy.setup_id === defaultSetupId && (
                  <span className={`${styles.badge} ${styles.badgeBlue}`}>Default for new branches</span>
                )}
              </div>
              {selectedPolicy.description && <p className={styles.policyDescription}>{selectedPolicy.description}</p>}
            </div>
            <div className={styles.policyHeaderActions}>
              <div className={styles.policyActionRow}>
                {canAssign && selectedPolicy.status === 'Active' && selectedPolicy.setup_id !== defaultSetupId && (
                  <button
                    className={styles.btnSecondary}
                    onClick={() => openConfirm('set-default')}
                    disabled={mutating || !defaultPrerequisiteMet}
                    aria-describedby={defaultBlockedByNoPublishedVersion ? 'policy-default-hint' : undefined}
                  >
                    Set as default
                  </button>
                )}
                {selectedPolicy.setup_id === defaultSetupId && canAssign && (
                  <button className={styles.btnDanger} onClick={() => openConfirm('clear-default')} disabled={mutating}>
                    Clear company default
                  </button>
                )}
                {canManage && selectedPolicy.status === 'Active' && (
                  <button className={styles.btnSecondary} onClick={openEditModal} disabled={mutating}>
                    Edit details
                  </button>
                )}
              </div>
              {defaultBlockedByNoPublishedVersion &&
                canAssign &&
                selectedPolicy.status === 'Active' &&
                selectedPolicy.setup_id !== defaultSetupId && (
                  <p id="policy-default-hint" className={styles.policyActionHint}>
                    Publish a payroll schedule before setting this policy as the default.
                  </p>
                )}
            </div>
          </div>
          {actionError?.kind === 'default' &&
            (actionError.setupId === null || actionError.setupId === selectedPolicyId) && (
              <div className={styles.policyHeaderNotice}>
                <PayrollErrorNotice info={actionError.info} />
              </div>
            )}

          {/* ── Payroll schedule: the currently effective published Version ── */}
          <section className={styles.policyDetailsCard} aria-label="Payroll schedule">
          <div className={styles.policyDetailsHead}>
              <div className={styles.policyDetailsHeading}>
                <span className={styles.policyDetailsIcon} aria-hidden="true">▤</span>
                <div>
                  <h3 className={styles.policyDetailsTitle}>Payroll schedule</h3>
                  <p className={styles.policyDetailsSubtitle}>The schedule currently in effect for this policy.</p>
                </div>
              </div>
            </div>
          {versionsError ? (
            <div className={styles.policyDetailsBody}>
              <PayrollErrorNotice info={versionsError} />
            </div>
          ) : versionsLoading ? (
            <div className={styles.policyDetailsBody}>
              <p className={styles.mutedText}>Loading…</p>
            </div>
          ) : scheduleFields === null ? (
            <div className={styles.scheduleEmpty}>
              <p className={styles.scheduleEmptyTitle}>No payroll schedule is active yet</p>
              <p className={styles.scheduleEmptyText}>Publish a schedule to start using this policy.</p>
            </div>
          ) : (
            <dl className={styles.policyDetailsGrid}>
              <div className={styles.policyDetailsItem}>
                <dt>Payroll frequency</dt>
                <dd>{scheduleFields.frequency}</dd>
              </div>
              <div className={styles.policyDetailsItem}>
                <dt>First payroll period starts on</dt>
                <dd>{scheduleFields.firstPeriodStart}</dd>
              </div>
              {scheduleFields.interval !== null && (
                <div className={styles.policyDetailsItem}>
                  <dt>Custom interval</dt>
                  <dd>{scheduleFields.interval}</dd>
                </div>
              )}
              <div className={styles.policyDetailsItem}>
                <dt>Regular days off</dt>
                <dd>{scheduleFields.daysOff}</dd>
              </div>
            </dl>
          )}
          </section>
        </div>
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
        <>
      {/* ── Parked: policy detail lower block (next steps, archive) ── */}
      {selectedPolicy && (
        <div className={styles.setupsDetailPanel}>
          <div className={styles.detailContent}>
            {nextSteps.length > 0 && (
              <div className={styles.previewResult}>
                <p className={styles.label}>Next steps</p>
                <ul className={styles.draftList}>
                  {nextSteps.map((kind) => (
                    <li key={kind} className={styles.assignmentMeta}>
                      {NEXT_STEP_TEXT[kind]}
                      {nextStepPermitted[kind] && (
                        <button
                          className={`${styles.btnSecondary} ${styles.nextStepAction}`}
                          onClick={() => handleNextStepAction(kind)}
                          disabled={mutating}
                        >
                          Go
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {selectedPolicy.status === 'Active' ? (
              <div className={styles.detailActions}>
                {canManage && (
                  archiveReason ? (
                    <span className={styles.archiveBlocked}>
                      <button className={styles.btnDanger} disabled>
                        Archive
                      </button>
                      <span className={styles.archiveBlockedReason}>{archiveReason}</span>
                    </span>
                  ) : (
                    <button className={styles.btnDanger} onClick={() => openConfirm('archive')} disabled={mutating}>
                      Archive
                    </button>
                  )
                )}
              </div>
            ) : (
              <p className={styles.mutedNote}>Archived policies are read-only.</p>
            )}

            {actionError?.kind === 'archive' && actionError.setupId === selectedPolicyId && (
              <PayrollErrorNotice info={actionError.info} />
            )}

            <details className={styles.mutedNote}>
              <summary>Integration details</summary>
              <p>{`Reference code: ${selectedPolicy.setup_code}`}</p>
            </details>
          </div>
        </div>
      )}

      {/* ── Drafts ── */}
      {selectedPolicy && (
        <SectionCard
          title="Draft schedules"
          actions={
            selectedPolicy.status === 'Active' && canManage ? (
              <button className={styles.btnPrimary} onClick={openCreateDraftModal} disabled={mutating}>
                + Add draft schedule
              </button>
            ) : undefined
          }
        >
          {draftsError ? (
            <PayrollErrorNotice info={draftsError} />
          ) : draftsLoading ? (
            <p className={styles.mutedText}>Loading…</p>
          ) : drafts.length === 0 ? (
            <p className={styles.mutedText}>No draft schedules.</p>
          ) : (
            <ul className={styles.draftList}>
              {drafts.map((d) => {
                const complete = isDraftComplete(d);
                return (
                  <li key={d.version_id} className={styles.draftRow}>
                    <div className={styles.draftRowMain}>
                      <span className={styles.draftRowTitle}>{draftCreatedLabel(d.created_at_utc)}</span>
                      <span className={styles.draftRowSchedule}>
                        {complete
                          ? friendlyScheduleSummary({
                              payroll_frequency: d.payroll_frequency as string,
                              anchor_start_date: d.anchor_start_date as string,
                              custom_interval_days: d.custom_interval_days,
                              normal_days_off_mask: d.normal_days_off_mask as number,
                            })
                          : 'Incomplete schedule'}
                      </span>
                    </div>

                    {selectedPolicy.status === 'Active' && (
                      <div className={styles.draftRowActions}>
                        {canManage && (
                          <button
                            className={styles.btnSecondary}
                            onClick={() => openEditDraftModal(d)}
                            disabled={mutating}
                          >
                            Edit
                          </button>
                        )}
                        {canManage && (
                          <button
                            className={styles.btnDanger}
                            onClick={() => openDiscardConfirm(d)}
                            disabled={mutating}
                          >
                            Discard
                          </button>
                        )}
                        {canPublish && (
                          <span className={styles.archiveBlocked}>
                            <button
                              className={styles.btnSecondary}
                              onClick={() => setPublishDraft(d)}
                              disabled={mutating || !complete}
                            >
                              Publish…
                            </button>
                            {!complete && (
                              <span className={styles.archiveBlockedReason}>
                                Complete the schedule before publishing.
                              </span>
                            )}
                          </span>
                        )}
                      </div>
                    )}

                    {draftActionError?.kind === 'discard' &&
                      draftActionError.setupId === selectedPolicy.setup_id &&
                      draftActionError.draftId === d.version_id && (
                        <PayrollErrorNotice info={draftActionError.info} />
                      )}
                  </li>
                );
              })}
            </ul>
          )}
        </SectionCard>
      )}

      {/* ── Publish panel (preview-first) ── */}
      {selectedPolicy && selectedPolicy.status === 'Active' && canPublish && publishDraft && currentPublishDraft && (
        <SectionCard title="Publish">
          <PublishPanel
            key={currentPublishDraft.version_id}
            setupId={selectedPolicy.setup_id}
            draft={currentPublishDraft}
            branches={branchDirectory}
            mutating={mutating}
            setMutating={setMutating}
            onClose={() => setPublishDraft(null)}
            onPublished={() => {
              setPublishDraft(null);
              setSetupDataReloadKey((k) => k + 1);
            }}
            showToast={showToast}
          />
        </SectionCard>
      )}

      {/* ── Policy updates (read-only) ── */}
      <SectionCard title="Policy updates">
        {!selectedPolicy ? (
          <p className={styles.mutedText}>Select a policy to view its updates.</p>
        ) : versionsError ? (
          <PayrollErrorNotice info={versionsError} />
        ) : versionsLoading ? (
          <p className={styles.mutedText}>Loading…</p>
        ) : versions.length === 0 ? (
          <p className={styles.mutedText}>No published updates yet.</p>
        ) : (
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Starts on</th>
                  <th>Next change</th>
                  <th>Payroll schedule</th>
                  <th>Relationship</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.version_id}>
                    <td>
                      {displayVersionNumbers.has(v.version_id)
                        ? `Version ${displayVersionNumbers.get(v.version_id)}`
                        : v.is_terminal
                          ? 'Scheduled update'
                          : 'Published correction'}
                    </td>
                    <td>{formatIsoLong(v.effective_from_date)}</td>
                    <td>{v.effective_to_date ? formatIsoLong(v.effective_to_date) : 'No scheduled policy change'}</td>
                    <td>{friendlyScheduleSummary(v.schedule)}</td>
                    <td>{versionRelationship(v, versions) ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

      {/* ── Assigned branches ── */}
      <SectionCard title="Assigned branches">
        {!selectedPolicy ? (
          <p className={styles.mutedText}>Select a policy to view its assigned branches.</p>
        ) : summariesError ? (
          <PayrollErrorNotice info={summariesError} />
        ) : summariesLoading ? (
          <p className={styles.mutedText}>Loading…</p>
        ) : assignedBranchRows.length === 0 ? (
          <p className={styles.mutedText}>No branches follow this policy yet.</p>
        ) : (
          <div className={styles.tableWrap}>
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>Branch</th>
                  <th>Relation</th>
                  <th>Since</th>
                </tr>
              </thead>
              <tbody>
                {assignedBranchRows.map((row) => (
                  <tr key={`${row.branch_id}-${row.relation}`}>
                    <td>
                      {row.branch_name}
                      {row.branch_code && ` (${row.branch_code})`}
                    </td>
                    <td>{row.relation === 'current' ? 'Current' : 'Scheduled'}</td>
                    <td>{formatIsoLong(row.since_date)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </SectionCard>

        </>
      )}

      {/* ── Confirm dialogs (policy-level) ── */}
      <ConfirmDialog
        open={confirmKind === 'clear-default'}
        title="Clear company default?"
        message="New branches will no longer have a default payroll policy available for automatic onboarding until another default is selected. Existing branch assignments are unchanged."
        confirmLabel="Clear default"
        variant="danger"
        loading={mutating}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmKind(null)}
      />
      <ConfirmDialog
        open={confirmKind === 'set-default'}
        title="Set company default?"
        message="Affects only branches created later; existing branch assignments never change."
        confirmLabel="Set default"
        loading={mutating}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmKind(null)}
      />
      <ConfirmDialog
        open={confirmKind === 'archive'}
        title="Archive payroll policy?"
        message={archiveMessage}
        confirmLabel="Archive"
        variant="danger"
        loading={mutating}
        onConfirm={handleConfirm}
        onCancel={() => setConfirmKind(null)}
      />
      <ConfirmDialog
        open={discardTarget != null}
        title="Discard draft schedule?"
        message={
          discardTarget
            ? `Discard ${draftCreatedLabel(discardTarget.created_at_utc)}? Published policy updates are not changed.`
            : ''
        }
        confirmLabel="Discard"
        variant="danger"
        loading={mutating}
        onConfirm={confirmDiscardDraft}
        onCancel={() => setDiscardTarget(null)}
      />

      {/* ── Edit policy modal ── */}
      {editOpen && selectedPolicy && (
        <div
          className={styles.modalOverlay}
          onClick={(e) => {
            if (e.target === e.currentTarget) closeEditModal();
          }}
        >
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>{`Edit — ${selectedPolicy.setup_name}`}</h2>
              <button className={styles.modalCloseBtn} onClick={closeEditModal}>
                ×
              </button>
            </div>
            <div className={styles.modalBody}>
              <div className={styles.formGroup}>
                <label className={styles.label}>Policy name</label>
                <input
                  className={styles.input}
                  value={editForm.setup_name}
                  maxLength={200}
                  onChange={(e) => setEditForm((f) => ({ ...f, setup_name: e.target.value }))}
                  disabled={mutating}
                />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label}>Description</label>
                <textarea
                  className={styles.textarea}
                  value={editForm.description}
                  onChange={(e) => setEditForm((f) => ({ ...f, description: e.target.value }))}
                  disabled={mutating}
                />
              </div>
              {editError && <PayrollErrorNotice info={editError} />}
            </div>
            <div className={styles.modalFooter}>
              <button className={styles.btnPrimary} onClick={submitEdit} disabled={mutating}>
                {mutating ? 'Working…' : 'Save changes'}
              </button>
              <button className={styles.btnSecondary} onClick={closeEditModal} disabled={mutating}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Draft editor modal (create + edit) ── */}
      {draftEditorOpen && selectedPolicy && (
        <DraftEditorModal
          setupId={selectedPolicy.setup_id}
          mode={draftEditorMode}
          editingDraftId={editingDraftId}
          initialForm={draftEditorInitialForm}
          versions={versions}
          canPublish={canPublish}
          mutating={mutating}
          setMutating={setMutating}
          onClose={closeDraftEditorModal}
          onSaved={() => {
            setDraftEditorOpen(false);
            setSetupDataReloadKey((k) => k + 1);
          }}
          onDraftSaved={() => setSetupDataReloadKey((k) => k + 1)}
          onPublished={() => {
            setDraftEditorOpen(false);
            setSetupDataReloadKey((k) => k + 1);
          }}
          showToast={showToast}
        />
      )}
    </>
  );
}
