import { useEffect, useReducer, useState, useCallback } from 'react';
import apiClient from '../../lib/apiClient';
import { transitionPeriodStatus } from '../../lib/payrollApi';
import { useAuth } from '../../store/authStore';
import { canCreatePeriod, canEntryPayroll, canFinalizePayroll } from '../../lib/permissions';
import type { Branch } from '../../types/core';
import type { PeriodSummary } from '../../types/payroll';
import { PeriodStatusBadge } from '../../components/StatusBadge';
import { CreatePeriodModal } from '../../components/CreatePeriodModal';
import { PayrollEntryDialog } from './PayrollEntryDialog';
import { DriversOffDialog } from './DriversOffDialog';
import { BonusDialog } from './BonusDialog';
import { FinalizationPreviewDialog } from './FinalizationPreviewDialog';
import styles from './PeriodsListPage.module.css';

// ── Current Payroll status policy ─────────────────────────────────────────────
// Active entry work: Draft and Open only.
// Approved: shown separately in "Ready to Finalize" — not mixed with entry work.
// InReview: belongs to the Review page, not Current Payroll.
// Cancelled / Locked / Archived: never shown on this page.
//
// The filterStatus dropdown is a client-side refinement within Draft + Open.
// No status is sent to the backend; we always fetch all, then split here.
// ─────────────────────────────────────────────────────────────────────────────

type ActiveFilter = '' | 'Draft' | 'Open';   // '' = both Draft and Open

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
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'PREPEND';     period: PeriodSummary };

function periodsReducer(s: PeriodsState, a: PeriodsAction): PeriodsState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true,  error: '' };
    case 'FETCH_OK':    return { periods: a.periods, loading: false, error: '' };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
    case 'PREPEND':     return { ...s, periods: [a.period, ...s.periods] };
    default:            return s;
  }
}

// ── Period card ───────────────────────────────────────────────────────────────

interface PeriodCardProps {
  period: PeriodSummary;
  onViewPayroll: () => void;
  onDriversOff: () => void;
  onBonus: () => void;
  onFinalize: () => void;
  onReload: () => void;
  canEntry: boolean;
  canFinalize: boolean;
}

function PeriodCard({
  period: p, onViewPayroll, onDriversOff, onBonus, onFinalize, onReload, canEntry, canFinalize,
}: PeriodCardProps) {
  const [transitioning,  setTransitioning]  = useState(false);
  const [transitionError, setTransitionError] = useState<string | null>(null);

  async function handleTransition(newStatus: string) {
    setTransitioning(true);
    setTransitionError(null);
    try {
      await transitionPeriodStatus(p.payroll_period_id, { status: newStatus });
      onReload();
    } catch (e: unknown) {
      const detail =
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setTransitionError(detail ?? `Failed to transition to ${newStatus}.`);
    } finally {
      setTransitioning(false);
    }
  }

  const isDraft    = p.status === 'Draft';
  const isOpen     = p.status === 'Open';
  const isApproved = p.status === 'Approved';
  // Entry controls apply to Open periods only (InReview excluded from this page)
  const isEnterable = isOpen;

  const nextAction = (() => {
    if (isDraft)    return { text: 'Open this period to begin payroll entry', style: styles.nextActionPrompt };
    if (isOpen)     return { text: 'Enter daily payroll, then submit for review when ready', style: styles.nextAction };
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
          {isEnterable ? 'Enter Payroll' : 'View Payroll'}
        </button>

        {/* Entry-only actions — require payroll.entry */}
        {canEntry && isEnterable && (
          <button className={styles.actionBtn} onClick={onDriversOff}>Drivers Off</button>
        )}
        {canEntry && isEnterable && (
          <button className={styles.actionBtn} onClick={onBonus}>Bonus</button>
        )}
        {canEntry && isDraft && (
          <button
            className={styles.workflowBtn}
            disabled={transitioning}
            onClick={() => void handleTransition('Open')}
          >
            {transitioning ? 'Opening…' : 'Open Period'}
          </button>
        )}
        {canEntry && isOpen && (
          <button
            className={styles.workflowBtn}
            disabled={transitioning}
            onClick={() => void handleTransition('InReview')}
          >
            {transitioning ? 'Submitting…' : 'Submit for Review'}
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

// ── Main page ────────────────────────────────────────────────────────────────

export function PeriodsListPage() {
  const { user } = useAuth();

  const isAllBranches   = user?.scope_type === 'AllCompanyBranches';
  const userCanCreate   = user ? canCreatePeriod(user)   : false;
  const userCanEntry    = user ? canEntryPayroll(user)   : false;
  const userCanFinalize = user ? canFinalizePayroll(user): false;

  const [branchesSt,  dispatchBranches] = useReducer(branchesReducer, { branches: [], loading: true });
  const [periodsSt,   dispatchPeriods]  = useReducer(periodsReducer,  { periods: [], loading: true, error: '' });

  // filterStatus is a client-side refinement within the Draft+Open active list.
  // '' = show both Draft and Open (default).
  const [filterStatus,    setFilterStatus]    = useState<ActiveFilter>('');
  const [filterBranchId,  setFilterBranchId]  = useState<string>('');
  const [showCreateModal, setShowCreateModal] = useState(false);

  // Dialog state — one period ID each, null = closed
  const [entryDialogPeriodId,      setEntryDialogPeriodId]      = useState<number | null>(null);
  const [driversOffDialogPeriodId, setDriversOffDialogPeriodId] = useState<number | null>(null);
  const [bonusDialogPeriodId,      setBonusDialogPeriodId]      = useState<number | null>(null);
  const [finalizeDialogPeriodId,   setFinalizeDialogPeriodId]   = useState<number | null>(null);

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
      // Current Payroll only cares about Draft, Open, and Approved.
      // InReview → Review page.  Cancelled / Locked / Archived → never shown here.
      const relevant = data.filter(
        (p) => p.status === 'Draft' || p.status === 'Open' || p.status === 'Approved'
      );
      dispatchPeriods({ type: 'FETCH_OK', periods: relevant });
    } catch {
      dispatchPeriods({ type: 'FETCH_ERROR', error: 'Failed to load payroll periods.' });
    }
  }, [filterBranchId]);

  useEffect(() => {
    void fetchPeriods();
  }, [fetchPeriods]);

  function handleCreated(period: PeriodSummary) {
    setShowCreateModal(false);
    dispatchPeriods({ type: 'PREPEND', period });
    setEntryDialogPeriodId(period.payroll_period_id);
  }

  const modalDefaultBranchId: number | null = isAllBranches
    ? (filterBranchId ? Number(filterBranchId) : null)
    : (user?.branch_ids?.[0] ?? null);

  const fixedBranch = branchesSt.branches.find(
    (b) => b.branch_id === user?.branch_ids?.[0]
  ) ?? null;

  function findPeriod(id: number | null) {
    if (id == null) return null;
    return periodsSt.periods.find((p) => p.payroll_period_id === id) ?? null;
  }

  // ── Client-side split ─────────────────────────────────────────────────────
  // activePeriods: Draft + Open, refined by filterStatus dropdown.
  // approvedPeriods: always shown in their own "Ready to Finalize" section.

  const activePeriods: PeriodSummary[] = periodsSt.periods.filter((p) => {
    if (p.status !== 'Draft' && p.status !== 'Open') return false;
    if (filterStatus === 'Draft') return p.status === 'Draft';
    if (filterStatus === 'Open')  return p.status === 'Open';
    return true; // '' = both
  });

  const approvedPeriods: PeriodSummary[] = periodsSt.periods.filter(
    (p) => p.status === 'Approved'
  );

  const hasAnyPeriods = activePeriods.length > 0 || approvedPeriods.length > 0;

  // ── Render ────────────────────────────────────────────────────────────────

  function renderCard(p: PeriodSummary) {
    return (
      <PeriodCard
        key={p.payroll_period_id}
        period={p}
        onViewPayroll={() => setEntryDialogPeriodId(p.payroll_period_id)}
        onDriversOff={() => setDriversOffDialogPeriodId(p.payroll_period_id)}
        onBonus={() => setBonusDialogPeriodId(p.payroll_period_id)}
        onFinalize={() => setFinalizeDialogPeriodId(p.payroll_period_id)}
        onReload={() => void fetchPeriods()}
        canEntry={userCanEntry}
        canFinalize={userCanFinalize}
      />
    );
  }

  return (
    <div className={styles.page}>
      {/* ── Header ───────────────────────────────────────────────────── */}
      <div className={styles.headerRow}>
        <h2 className={styles.pageTitle}>Current Payroll</h2>
        {userCanCreate && (
          <button
            className={styles.createBtn}
            onClick={() => setShowCreateModal(true)}
            disabled={branchesSt.loading}
          >
            + Create Period
          </button>
        )}
      </div>

      {/* ── Filters ──────────────────────────────────────────────────── */}
      <div className={styles.filters}>
        <label className={styles.filterLabel}>
          Show
          <select
            className={styles.filterSelect}
            value={filterStatus}
            onChange={(e) => setFilterStatus(e.target.value as ActiveFilter)}
          >
            <option value="">Draft &amp; Open</option>
            <option value="Draft">Draft only</option>
            <option value="Open">Open only</option>
          </select>
        </label>

        {isAllBranches ? (
          <label className={styles.filterLabel}>
            Branch
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

      {/* ── Content ──────────────────────────────────────────────────── */}
      {periodsSt.loading ? (
        <p className={styles.stateMsg}>Loading periods…</p>
      ) : periodsSt.error ? (
        <p className={styles.errorMsg}>{periodsSt.error}</p>
      ) : !hasAnyPeriods ? (
        <div className={styles.emptyState}>
          <p className={styles.emptyTitle}>No payroll periods found</p>
          <p className={styles.emptyHint}>
            {filterStatus || filterBranchId
              ? 'Try adjusting the filters.'
              : userCanCreate
                ? 'No active periods. Create one to get started.'
                : 'No active payroll periods for this branch.'}
          </p>
        </div>
      ) : (
        <>
          {/* Active entry work — Draft and Open */}
          {activePeriods.length > 0 && (
            <div className={styles.periodList}>
              {activePeriods.map(renderCard)}
            </div>
          )}

          {activePeriods.length === 0 && !periodsSt.loading && (
            <div className={styles.emptyState}>
              <p className={styles.emptyTitle}>
                No {filterStatus || 'Draft or Open'} periods
              </p>
              <p className={styles.emptyHint}>Try adjusting the filter.</p>
            </div>
          )}

          {/* Ready to Finalize — Approved periods in a distinct section */}
          {approvedPeriods.length > 0 && (
            <div className={styles.sectionBlock}>
              <div className={styles.sectionHeader}>
                <span className={styles.sectionTitle}>Ready to Finalize</span>
                <span className={styles.sectionHint}>
                  {approvedPeriods.length} approved period{approvedPeriods.length !== 1 ? 's' : ''} awaiting finalization
                </span>
              </div>
              <div className={styles.periodList}>
                {approvedPeriods.map(renderCard)}
              </div>
            </div>
          )}
        </>
      )}

      {/* ── Create modal ─────────────────────────────────────────────── */}
      {showCreateModal && (
        <CreatePeriodModal
          branches={branchesSt.branches}
          defaultBranchId={modalDefaultBranchId}
          isAllBranches={isAllBranches}
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

      {driversOffDialogPeriodId != null && (
        <DriversOffDialog
          periodId={driversOffDialogPeriodId}
          periodName={findPeriod(driversOffDialogPeriodId)?.period_name}
          onClose={() => setDriversOffDialogPeriodId(null)}
        />
      )}

      {bonusDialogPeriodId != null && (() => {
        const bp = findPeriod(bonusDialogPeriodId);
        return (
          <BonusDialog
            periodId={bonusDialogPeriodId}
            periodName={bp?.period_name}
            periodStatus={bp?.status ?? 'Draft'}
            onClose={() => setBonusDialogPeriodId(null)}
          />
        );
      })()}

      {finalizeDialogPeriodId != null && (
        <FinalizationPreviewDialog
          periodId={finalizeDialogPeriodId}
          periodName={findPeriod(finalizeDialogPeriodId)?.period_name}
          onClose={() => setFinalizeDialogPeriodId(null)}
          onFinalized={() => { void fetchPeriods(); }}
        />
      )}
    </div>
  );
}
