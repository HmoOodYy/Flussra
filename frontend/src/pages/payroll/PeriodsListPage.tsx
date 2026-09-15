import { useEffect, useReducer, useState, useCallback } from 'react';
import apiClient from '../../lib/apiClient';
import { getCurrentPayrollHub, resubmitPeriod, submitPeriod } from '../../lib/payrollApi';
import { useAuth } from '../../store/authStore';
import { canCreatePeriod, canEntryPayroll, canFinalizePayroll, canViewPayrollReports } from '../../lib/permissions';
import type { Branch } from '../../types/core';
import type { CurrentPayrollHub, CurrentPayrollHubBranch, CurrentPayrollHubPeriodSlot, PeriodSummary } from '../../types/payroll';
import { PeriodStatusBadge } from '../../components/StatusBadge';
import { SectionCard } from '../../components/ui/SectionCard';
import { EmptyState } from '../../components/ui/EmptyState';
import { ErrorState } from '../../components/ui/ErrorState';
import { CreatePeriodModal } from '../../components/CreatePeriodModal';
import { PayrollEntryDialog } from './PayrollEntryDialog';
import { DriversOffDialog } from './DriversOffDialog';
import { BonusDialog } from './BonusDialog';
import { CalculationPreviewDialog } from './CalculationPreviewDialog';
import { FinalizationPreviewDialog } from './FinalizationPreviewDialog';
import { CurrentPayrollReportsDialog } from './CurrentPayrollReportsDialog';
import styles from './PeriodsListPage.module.css';

// ── Current Payroll status policy ─────────────────────────────────────────────
// Active entry work: Draft (Prepared), Open, and Returned.
// Approved: shown separately in "Ready to Finalize" — not mixed with entry work.
// InReview: belongs to the Review page, not Current Payroll.
// Cancelled / Locked / Archived: never shown on this page.
//
// The filterStatus dropdown is a client-side refinement within the active states.
// No status is sent to the backend; we always fetch all, then split here.
// ─────────────────────────────────────────────────────────────────────────────

type ActiveFilter = '' | 'Draft' | 'Open' | 'Returned';

function getWorkflowErrorDetail(error: unknown, fallback: string): string {
  const detail =
    (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return fallback;
}

// ── Reducers ──────────────────────────────────────────────────────────────────

type BranchesState = { branches: Branch[]; loading: boolean };
type BranchesAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';  branches: Branch[] }
  | { type: 'FETCH_ERROR' };

function branchesReducer(s: BranchesState, a: BranchesAction): BranchesState {
  switch (a.type) {
    case 'FETCH_START': return { branches: s.branches, loading: true  };
    case 'FETCH_OK':    return { branches: a.branches,  loading: false };
    case 'FETCH_ERROR': return { branches: [],           loading: false };
    default:            return s;
  }
}

type PeriodsState = { periods: PeriodSummary[]; loading: boolean; error: string };
type PeriodsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    periods: PeriodSummary[] }
  | { type: 'FETCH_ERROR'; error: string };

function periodsReducer(s: PeriodsState, a: PeriodsAction): PeriodsState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true,  error: '' };
    case 'FETCH_OK':    return { periods: a.periods, loading: false, error: '' };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
    default:            return s;
  }
}

type WorkflowState = { data: CurrentPayrollHub | null; loading: boolean; error: string };
type WorkflowAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; data: CurrentPayrollHub }
  | { type: 'FETCH_ERROR'; error: string };

function workflowReducer(state: WorkflowState, action: WorkflowAction): WorkflowState {
  switch (action.type) {
    case 'FETCH_START': return { ...state, loading: true, error: '' };
    case 'FETCH_OK': return { data: action.data, loading: false, error: '' };
    case 'FETCH_ERROR': return { ...state, loading: false, error: action.error };
  }
}

// ── Period card ───────────────────────────────────────────────────────────────

interface PeriodCardProps {
  period: PeriodSummary;
  onViewPayroll: () => void;
  onDriversOff: () => void;
  onBonus: () => void;
  onPreview: () => void;
  onFinalize: () => void;
  onReports: () => void;
  onReload: () => void;
  canEntry: boolean;
  canPreview: boolean;
  canFinalize: boolean;
  canViewReports: boolean;
}

function PeriodCard({
  period: p, onViewPayroll, onDriversOff, onBonus, onPreview, onFinalize, onReports, onReload, canEntry, canPreview, canFinalize, canViewReports,
}: PeriodCardProps) {
  const [transitioning,  setTransitioning]  = useState(false);
  const [transitionError, setTransitionError] = useState<string | null>(null);

  async function handleSubmit() {
    setTransitioning(true);
    setTransitionError(null);
    try {
      await submitPeriod(p.payroll_period_id);
      onReload();
    } catch (e: unknown) {
      setTransitionError(getWorkflowErrorDetail(e, 'Failed to submit this period for review.'));
    } finally {
      setTransitioning(false);
    }
  }

  async function handleResubmit() {
    setTransitioning(true);
    setTransitionError(null);
    try {
      await resubmitPeriod(p.payroll_period_id);
      onReload();
    } catch (e: unknown) {
      setTransitionError(getWorkflowErrorDetail(e, 'Failed to resubmit this period for review.'));
    } finally {
      setTransitioning(false);
    }
  }

  const isDraft    = p.status === 'Draft';
  const isOpen     = p.status === 'Open';
  const isReturned = p.status === 'Returned';
  const isApproved = p.status === 'Approved';
  const isEnterable = isOpen || isReturned;
  const isGridEnterable = isDraft || isEnterable;

  const nextAction = (() => {
    if (isDraft)    return { text: 'Prepared — backend workflow will promote this period when eligible', style: styles.nextActionPrompt };
    if (isOpen)     return { text: 'Enter daily payroll, then submit for review when ready', style: styles.nextAction };
    if (isReturned) return { text: 'Returned for correction — update payroll source entries, then resubmit for review', style: styles.nextAction };
    if (isApproved) return { text: 'Approved — awaiting finalization', style: styles.nextActionPrompt };
    return null;
  })();

  return (
    <div className={styles.periodCard}>
      {/* Left: period info */}
      <div className={styles.cardInfo}>
        <div className={styles.cardNameRow}>
          <span className={styles.cardName}>{p.period_name || p.period_code}</span>
          <PeriodStatusBadge status={p.status} />
        </div>
        <div className={styles.cardMeta}>
          <span>{p.branch_name}</span>
          <span className={styles.metaSep}>·</span>
          <span>{p.period_type}</span>
          <span className={styles.metaSep}>·</span>
          <span>{p.start_date} – {p.end_date}</span>
          {p.pay_date && (
            <>
              <span className={styles.metaSep}>·</span>
              <span>Pay: {p.pay_date}</span>
            </>
          )}
        </div>
        <div className={styles.cardCounts}>
          {p.draft_drivers > 0 && (
            <span className={styles.countBadge}>{p.draft_drivers} drivers</span>
          )}
          {p.draft_lines > 0 && (
            <span className={styles.countBadge}>{p.draft_lines} lines</span>
          )}
          {p.draft_lines_needing_attention > 0 && (
            <span className={`${styles.countBadge} ${styles.attnBadge}`}>
              &#9888; {p.draft_lines_needing_attention} need attention
            </span>
          )}
          {p.final_lines > 0 && (
            <span className={styles.countBadge}>{p.final_lines} final lines</span>
          )}
        </div>
        {nextAction && <div className={nextAction.style}>{nextAction.text}</div>}
      </div>

      {/* Right: action buttons */}
      <div className={styles.cardActions}>
        {/* View/Enter Payroll — always visible for non-finalized periods */}
        <button className={styles.primaryActionBtn} onClick={onViewPayroll}>
          {isDraft ? 'Prepare Payroll' : isGridEnterable ? 'Enter Payroll' : 'View Payroll'}
        </button>

        {canPreview && isEnterable && (
          <button className={styles.actionBtn} onClick={onPreview}>View Expected Payroll</button>
        )}

        {canViewReports && (isDraft || isOpen || isReturned || isApproved) && (
          <button className={styles.actionBtn} onClick={onReports}>Reports</button>
        )}

        {/* Entry-only actions — require payroll.entry */}
        {canEntry && isEnterable && (
          <button className={styles.actionBtn} onClick={onDriversOff}>Drivers Off</button>
        )}
        {canEntry && isEnterable && (
          <button className={styles.actionBtn} onClick={onBonus}>Bonus</button>
        )}
        {canEntry && isOpen && (
          <button
            className={styles.workflowBtn}
            disabled={transitioning}
            onClick={() => void handleSubmit()}
          >
            {transitioning ? 'Submitting…' : 'Submit for Review'}
          </button>
        )}
        {canEntry && isReturned && (
          <button
            className={styles.workflowBtn}
            disabled={transitioning}
            onClick={() => void handleResubmit()}
          >
            {transitioning ? 'Resubmitting…' : 'Resubmit for Review'}
          </button>
        )}

        {/* Finalize — requires payroll.finalize, only on Approved periods */}
        {canFinalize && isApproved && (
          <button className={styles.finalizeBtn} onClick={onFinalize}>
            Finalize Payroll
          </button>
        )}

        {transitionError && (
          <span className={styles.transitionError}>{transitionError}</span>
        )}
      </div>
    </div>
  );
}

function WorkflowSlot({
  title,
  period,
  emptyMessage,
}: {
  title: string;
  period: CurrentPayrollHubPeriodSlot | null;
  emptyMessage: string;
}) {
  return (
    <div className={styles.workflowSlot}>
      <span className={styles.workflowSlotLabel}>{title}</span>
      {period ? (
        <>
          <div className={styles.workflowSlotNameRow}>
            <strong>{period.period_name || period.period_code}</strong>
            <PeriodStatusBadge status={period.status} />
          </div>
          <span className={styles.workflowSlotMeta}>{period.start_date} - {period.end_date}</span>
          <div className={styles.workflowMetrics}>
            <span>Eligible <strong>{period.metrics.total_eligible_drivers}</strong></span>
            <span>Working <strong>{period.metrics.working_drivers}</strong></span>
            <span>Fully off <strong>{period.metrics.fully_off_drivers}</strong></span>
          </div>
          {period.financials_available && period.financial_summary ? (
            <div className={styles.workflowFinancials}>
              <span>Expected pay <strong>{formatMoney(period.financial_summary.total_expected_pay)}</strong></span>
              {period.financial_summary.has_blockers && (
                <span className={styles.workflowAlert}>Calculation blocked</span>
              )}
            </div>
          ) : (
            <span className={styles.workflowUnavailable}>Financial summary unavailable for this lifecycle state</span>
          )}
        </>
      ) : (
        <span className={styles.workflowSlotEmpty}>{emptyMessage}</span>
      )}
    </div>
  );
}

function formatMoney(value: string): string {
  const numeric = Number(value);
  return Number.isFinite(numeric)
    ? new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(numeric)
    : value;
}

function WorkflowBranch({ branch }: { branch: CurrentPayrollHubBranch }) {
  const { open, prepared, in_review: inReview, returned } = branch.slots;
  const creationCapability = branch.capabilities.can_create_open_candidate.allowed
    ? branch.capabilities.can_create_open_candidate
    : branch.capabilities.can_create_prepared_candidate;
  const workflowHint = open && prepared
    ? 'The prepared period automatically becomes Open after the current Open period is locked.'
    : open
      ? 'The next period can be prepared when backend workflow availability allows it.'
      : prepared
        ? 'This prepared period is waiting for backend workflow promotion.'
        : creationCapability.allowed
          ? 'No active period exists. Create a backend-approved payroll candidate to begin.'
          : creationCapability.reason_message ?? 'No payroll candidate is available for this branch.';

  const slots = [
    ['Current Payroll', open, 'No Open payroll period'],
    ['Next Payroll', prepared, 'No Prepared payroll period'],
    ['In Review', inReview, 'No payroll in review'],
    ['Returned', returned, 'No returned payroll'],
  ] as const;

  return (
    <div className={styles.workflowBranch}>
      <div className={styles.workflowBranchHeader}>
        <strong>{branch.branch_name}</strong>
        <span className={styles.workflowSetup}>Setup: {branch.setup_status}</span>
      </div>
      <div className={styles.workflowSlots}>
        {slots.map(([title, period, emptyMessage]) => (
          <WorkflowSlot key={title} title={title} period={period} emptyMessage={emptyMessage} />
        ))}
      </div>
      <p className={styles.workflowHint}>{workflowHint}</p>
      {branch.alerts.map((alert) => (
        <p key={alert.code} className={styles.workflowAlert}>{alert.message}</p>
      ))}
    </div>
  );
}

// ── Main page ────────────────────────────────────────────────────────────────

export function PeriodsListPage() {
  const { user } = useAuth();

  const isAllBranches   = user?.scope_type === 'AllCompanyBranches';
  const userCanPreview  = user ? user.active_permissions.some(
    (permission) => permission === 'payroll.view' || permission === 'payroll.entry',
  ) : false;
  const userCanViewReports = user ? canViewPayrollReports(user) : false;

  const [branchesSt,  dispatchBranches] = useReducer(branchesReducer, { branches: [], loading: true });
  const [periodsSt,   dispatchPeriods]  = useReducer(periodsReducer,  { periods: [], loading: true, error: '' });
  const [workflowSt,  dispatchWorkflow] = useReducer(workflowReducer, { data: null, loading: true, error: '' });

  // filterStatus is a client-side refinement within active work.
  const [filterStatus,    setFilterStatus]    = useState<ActiveFilter>('');
  const [filterBranchId,  setFilterBranchId]  = useState<string>('');
  const [showCreateModal, setShowCreateModal] = useState(false);

  // Dialog state — one period ID each, null = closed
  const [entryDialogPeriodId,      setEntryDialogPeriodId]      = useState<number | null>(null);
  const [driversOffDialogPeriodId, setDriversOffDialogPeriodId] = useState<number | null>(null);
  const [bonusDialogPeriodId,      setBonusDialogPeriodId]      = useState<number | null>(null);
  const [calculationPreviewPeriodId, setCalculationPreviewPeriodId] = useState<number | null>(null);
  const [finalizeDialogPeriodId,   setFinalizeDialogPeriodId]   = useState<number | null>(null);
  const [reportsDialogPeriodId, setReportsDialogPeriodId] = useState<number | null>(null);

  // ── Data fetching ─────────────────────────────────────────────────────────

  useEffect(() => {
    dispatchBranches({ type: 'FETCH_START' });
    apiClient
      .get<Branch[]>('/core/branches')
      .then((r) => dispatchBranches({ type: 'FETCH_OK', branches: r.data }))
      .catch(() => dispatchBranches({ type: 'FETCH_ERROR' }));
  }, []);

  const fetchPeriods = useCallback(async () => {
    dispatchPeriods({ type: 'FETCH_START' });
    try {
      const params: Record<string, string> = {};
      if (filterBranchId) params.branch_id = filterBranchId;
      const { data } = await apiClient.get<PeriodSummary[]>('/payroll/periods', { params });
      // Current Payroll surfaces Draft, Open, Returned, and Approved.
      // InReview → Review page.  Cancelled / Locked / Archived → never shown here.
      const relevant = data.filter(
        (p) => p.status === 'Draft' || p.status === 'Open' || p.status === 'Returned' || p.status === 'Approved'
      );
      dispatchPeriods({ type: 'FETCH_OK', periods: relevant });
    } catch {
      dispatchPeriods({ type: 'FETCH_ERROR', error: 'Failed to load payroll periods.' });
    }
  }, [filterBranchId]);

  useEffect(() => {
    void fetchPeriods();
  }, [fetchPeriods]);

  // Explicit filter always wins (only rendered for AllCompany users); otherwise
  // request the full accessible workflow set so multiple SpecificBranch
  // assignments are represented — do not pin to a single branch_ids[0].
  const workflowBranchId = filterBranchId ? Number(filterBranchId) : undefined;

  const fetchWorkflow = useCallback(async () => {
    dispatchWorkflow({ type: 'FETCH_START' });
    try {
      dispatchWorkflow({ type: 'FETCH_OK', data: await getCurrentPayrollHub(workflowBranchId) });
    } catch (error: unknown) {
      dispatchWorkflow({
        type: 'FETCH_ERROR',
        error: getWorkflowErrorDetail(error, 'Failed to load current payroll context.'),
      });
    }
  }, [workflowBranchId]);

  useEffect(() => {
    void fetchWorkflow();
  }, [fetchWorkflow]);

  const refreshHub = useCallback(() => {
    void fetchPeriods();
    void fetchWorkflow();
  }, [fetchPeriods, fetchWorkflow]);

  function handleCreated() {
    setShowCreateModal(false);
    refreshHub();
  }

  const creationAuthorizedBranches: Branch[] = user
    ? branchesSt.branches.filter((b) => canCreatePeriod(user, b.branch_id))
    : [];
  const userCanCreate = creationAuthorizedBranches.length > 0;
  const canSelectCreationBranch = creationAuthorizedBranches.length > 1;

  const filterSelectedBranchId = filterBranchId ? Number(filterBranchId) : null;
  const modalDefaultBranchId: number | null =
    filterSelectedBranchId !== null && creationAuthorizedBranches.some((b) => b.branch_id === filterSelectedBranchId)
      ? filterSelectedBranchId
      : creationAuthorizedBranches.length === 1
        ? creationAuthorizedBranches[0].branch_id
        : null;

  const fixedBranch = branchesSt.branches.find(
    (b) => b.branch_id === user?.branch_ids?.[0]
  ) ?? null;

  function findPeriod(id: number | null) {
    if (id == null) return null;
    return periodsSt.periods.find((p) => p.payroll_period_id === id) ?? null;
  }

  function findWorkingDrivers(periodId: number | null): number | null {
    if (periodId == null) return null;
    for (const branch of workflowBranches) {
      for (const slot of [branch.slots.open, branch.slots.returned]) {
        if (slot?.period_id === periodId) return slot.metrics.working_drivers;
      }
    }
    return null;
  }

  // ── Client-side split ─────────────────────────────────────────────────────
  // activePeriods: Draft + Open + Returned, refined by filterStatus dropdown.
  // approvedPeriods: always shown in their own "Ready to Finalize" section.

  const activePeriods: PeriodSummary[] = periodsSt.periods.filter((p) => {
    if (p.status !== 'Draft' && p.status !== 'Open' && p.status !== 'Returned') return false;
    if (filterStatus === 'Draft') return p.status === 'Draft';
    if (filterStatus === 'Open')  return p.status === 'Open';
    if (filterStatus === 'Returned') return p.status === 'Returned';
    return true;
  });

  const approvedPeriods: PeriodSummary[] = periodsSt.periods.filter(
    (p) => p.status === 'Approved'
  );
  const workflowBranches = workflowSt.data?.branches ?? [];
  const creationEligibleWorkflowBranches = user
    ? workflowBranches.filter((branch) => canCreatePeriod(user, branch.branch_id))
    : [];
  const workflowCanCreate = creationEligibleWorkflowBranches.some((branch) =>
    branch.capabilities.can_create_open_candidate.allowed
    || branch.capabilities.can_create_prepared_candidate.allowed,
  );
  const blockedCreateReason = creationEligibleWorkflowBranches.find((branch) =>
    !branch.capabilities.can_create_open_candidate.allowed
    && !branch.capabilities.can_create_prepared_candidate.allowed,
  )?.capabilities.can_create_open_candidate.reason_message;

  // ── Render ────────────────────────────────────────────────────────────────

  function renderCard(p: PeriodSummary) {
    return (
      <PeriodCard
        key={p.payroll_period_id}
        period={p}
        onViewPayroll={() => {
          setCalculationPreviewPeriodId(null);
          setEntryDialogPeriodId(p.payroll_period_id);
        }}
        onDriversOff={() => {
          setCalculationPreviewPeriodId(null);
          setDriversOffDialogPeriodId(p.payroll_period_id);
        }}
        onBonus={() => {
          setCalculationPreviewPeriodId(null);
          setBonusDialogPeriodId(p.payroll_period_id);
        }}
        onPreview={() => setCalculationPreviewPeriodId(p.payroll_period_id)}
        onFinalize={() => setFinalizeDialogPeriodId(p.payroll_period_id)}
        onReports={() => setReportsDialogPeriodId(p.payroll_period_id)}
        onReload={() => {
          setCalculationPreviewPeriodId(null);
          refreshHub();
        }}
        canEntry={user ? canEntryPayroll(user, p.branch_id) : false}
        canPreview={userCanPreview}
        canFinalize={user ? canFinalizePayroll(user, p.branch_id) : false}
        canViewReports={userCanViewReports}
      />
    );
  }

  const filterControls = (
    <div className={styles.filterControls}>
      <label className={styles.filterInline}>
        <span>Show</span>
        <select
          className={styles.filterSelect}
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value as ActiveFilter)}
        >
          <option value="">Prepared, Open &amp; Returned</option>
          <option value="Draft">Prepared only</option>
          <option value="Open">Open only</option>
          <option value="Returned">Returned only</option>
        </select>
      </label>

      {isAllBranches ? (
        <label className={styles.filterInline}>
          <span>Branch</span>
          <select
            className={styles.filterSelect}
            value={filterBranchId}
            onChange={(e) => setFilterBranchId(e.target.value)}
          >
            <option value="">All branches</option>
            {branchesSt.branches.map((b) => (
              <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>
            ))}
          </select>
        </label>
      ) : fixedBranch ? (
        <span className={styles.fixedBranch}>
          Branch: <strong>{fixedBranch.branch_name}</strong>
        </span>
      ) : null}
    </div>
  );

  return (
    <div className={styles.page}>
      {/* ── Header ───────────────────────────────────────────────────── */}
      {userCanCreate && (
        <div className={styles.headerRow}>
          <button
            className={styles.createBtn}
            onClick={() => setShowCreateModal(true)}
            disabled={branchesSt.loading || workflowSt.loading || !workflowCanCreate}
            title={!workflowCanCreate ? (blockedCreateReason ?? 'No payroll candidate is currently available.') : undefined}
          >
            + Create Payroll
          </button>
        </div>
      )}

      {/* ── Current Payroll Hub context ───────────────────────────────── */}
      <SectionCard
        title="Current Payroll"
        subtitle="Open, prepared, review, and returned payroll by branch."
        padded={false}
      >
        {workflowSt.loading ? (
          <p className={styles.stateMsg}>Loading current payroll context...</p>
        ) : workflowSt.error ? (
          <div className={styles.sectionPad}><ErrorState message={workflowSt.error} /></div>
        ) : workflowBranches.length === 0 ? (
          <p className={styles.stateMsg}>No accessible payroll workflow is available for this branch.</p>
        ) : (
          <div className={styles.workflowBranchList}>
            {workflowBranches.map((branch) => <WorkflowBranch key={branch.branch_id} branch={branch} />)}
          </div>
        )}
      </SectionCard>

      {/* ── Active Payroll Periods ────────────────────────────────────── */}
      <SectionCard
        title="Active Payroll Periods"
        actions={filterControls}
        padded={false}
      >
        {periodsSt.loading ? (
          <p className={styles.stateMsg}>Loading periods…</p>
        ) : periodsSt.error ? (
          <div className={styles.sectionPad}>
            <ErrorState message={periodsSt.error} />
          </div>
        ) : activePeriods.length === 0 ? (
          <div className={styles.sectionPad}>
            <EmptyState
              title={filterStatus ? `No ${filterStatus} periods` : 'No active payroll periods'}
              message={
                filterStatus || filterBranchId
                  ? 'Try adjusting the filters.'
                  : userCanCreate
                    ? 'No active periods. Create one to get started.'
                    : 'No active payroll periods for this branch.'
              }
            />
          </div>
        ) : (
          <div className={styles.periodListScroll}>
            {activePeriods.map(renderCard)}
          </div>
        )}
      </SectionCard>

      {/* ── Ready to Finalize ─────────────────────────────────────────── */}
      {!periodsSt.loading && !periodsSt.error && approvedPeriods.length > 0 && (
        <SectionCard
          title="Ready to Finalize"
          subtitle={`${approvedPeriods.length} approved period${approvedPeriods.length !== 1 ? 's' : ''} awaiting finalization`}
          padded={false}
        >
          <div className={styles.periodList}>
            {approvedPeriods.map(renderCard)}
          </div>
        </SectionCard>
      )}

      {/* ── Create modal ─────────────────────────────────────────────── */}
      {showCreateModal && (
        <CreatePeriodModal
          branches={creationAuthorizedBranches}
          defaultBranchId={modalDefaultBranchId}
          canSelectBranch={canSelectCreationBranch}
          onCreated={handleCreated}
          onClose={() => setShowCreateModal(false)}
        />
      )}

      {/* ── Dialogs ──────────────────────────────────────────────────── */}
      {entryDialogPeriodId != null && (
        <PayrollEntryDialog
          periodId={entryDialogPeriodId}
          onClose={() => setEntryDialogPeriodId(null)}
        />
      )}

      {driversOffDialogPeriodId != null && (() => {
        const period = findPeriod(driversOffDialogPeriodId);
        return period ? (
          <DriversOffDialog
            key={period.payroll_period_id}
            periodId={period.payroll_period_id}
            periodName={period.period_name}
            periodStartDate={period.start_date}
            periodEndDate={period.end_date}
            onClose={() => setDriversOffDialogPeriodId(null)}
          />
        ) : null;
      })()}

      {bonusDialogPeriodId != null && (() => {
        const bp = findPeriod(bonusDialogPeriodId);
        return (
          <BonusDialog
            periodId={bonusDialogPeriodId}
            periodName={bp?.period_name}
            onClose={() => setBonusDialogPeriodId(null)}
          />
        );
      })()}

      {calculationPreviewPeriodId != null && (
        <CalculationPreviewDialog
          periodId={calculationPreviewPeriodId}
          periodName={findPeriod(calculationPreviewPeriodId)?.period_name}
          workingDrivers={findWorkingDrivers(calculationPreviewPeriodId)}
          onClose={() => setCalculationPreviewPeriodId(null)}
          onLifecycleChanged={() => {
            setCalculationPreviewPeriodId(null);
            refreshHub();
          }}
        />
      )}

      {finalizeDialogPeriodId != null && (
        <FinalizationPreviewDialog
          periodId={finalizeDialogPeriodId}
          periodName={findPeriod(finalizeDialogPeriodId)?.period_name}
          onClose={() => setFinalizeDialogPeriodId(null)}
          onFinalized={refreshHub}
          onStateConflict={refreshHub}
        />
      )}

      {reportsDialogPeriodId != null && (
        <CurrentPayrollReportsDialog
          periodId={reportsDialogPeriodId}
          periodName={findPeriod(reportsDialogPeriodId)?.period_name}
          onClose={() => setReportsDialogPeriodId(null)}
        />
      )}
    </div>
  );
}
