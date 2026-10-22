/**
 * LedgerPage
 *
 * Read-only list of finalized (Locked/Archived) payroll periods. The Finalized Payroll
 * Library is the one finalized-payroll presentation: every card opens it, and every amount,
 * count and explanation shown there is the backend's frozen evidence.
 *
 * Security: ledger.view is the one authority (see canViewLedger); DRIVER/Self access is
 * blocked by the backend. Frontend gating is UI-only.
 */
import { useEffect, useReducer, useState, useCallback, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import apiClient from '../../lib/apiClient';
import { getFinalizedPeriods } from '../../lib/payrollApi';
import type { Branch } from '../../types/core';
import type { FinalizedPeriodListItem } from '../../types/payroll/finalized';
import { PeriodStatusBadge } from '../../components/StatusBadge';
import {
  FinalizedPayrollLibraryDialog,
  type FinalizedPayrollLibraryPeriodContext,
} from './FinalizedPayrollLibraryDialog';
import { ledgerLoadErrorMessage } from './ledgerLoadError';
import { resolvePreselectedPeriodId } from './finalizedNavigation';
import styles from './LedgerPage.module.css';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function libraryContextForPeriod(finalized: FinalizedPeriodListItem): FinalizedPayrollLibraryPeriodContext {
  return {
    payroll_period_id: finalized.period_id,
    period_name: finalized.period_name,
    period_code: finalized.period_code,
    branch_name: finalized.branch_name,
    status: finalized.period_status,
  };
}

// ---------------------------------------------------------------------------
// Reducers
// ---------------------------------------------------------------------------

type BranchesState = { branches: Branch[]; loading: boolean };
type BranchesAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; branches: Branch[] }
  | { type: 'FETCH_ERROR' };

function branchesReducer(s: BranchesState, a: BranchesAction): BranchesState {
  switch (a.type) {
    case 'FETCH_START': return { branches: s.branches, loading: true };
    case 'FETCH_OK':    return { branches: a.branches,  loading: false };
    case 'FETCH_ERROR': return { branches: [],           loading: false };
    default:            return s;
  }
}

type PeriodsState = { periods: FinalizedPeriodListItem[]; loading: boolean; error: string };
type PeriodsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    periods: FinalizedPeriodListItem[] }
  | { type: 'FETCH_ERROR'; error: string };

function periodsReducer(s: PeriodsState, a: PeriodsAction): PeriodsState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true, error: '' };
    case 'FETCH_OK':    return { periods: a.periods, loading: false, error: '' };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
    default:            return s;
  }
}

// ---------------------------------------------------------------------------
// Period card
// ---------------------------------------------------------------------------

interface LedgerCardProps {
  period: FinalizedPeriodListItem;
  onOpenLibrary: () => void;
}

function LedgerCard({ period: p, onOpenLibrary }: LedgerCardProps) {
  return (
    <div className={styles.card}>
      {/* Left: period info */}
      <div className={styles.cardInfo}>
        <div className={styles.cardNameRow}>
          <span className={styles.cardName}>{p.period_name || p.period_code}</span>
          <PeriodStatusBadge status={p.period_status} />
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
        <div className={styles.cardStats}>
          <span className={styles.statChip}>
            <span className={styles.statLabel}>Finalized</span>
            <span className={styles.statValue}>{p.finalized_at_utc ? new Date(p.finalized_at_utc).toLocaleDateString() : 'Not provided'}</span>
          </span>
        </div>
      </div>

      {/* Right: action */}
      <div className={styles.cardActions}>
        <button className={styles.primaryBtn} onClick={onOpenLibrary} type="button">
          Open Finalized Payroll
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

const LEDGER_STATUSES = ['Locked', 'Archived'] as const;
type LedgerStatus = (typeof LEDGER_STATUSES)[number];

export function LedgerPage() {
  const [branchesSt, dispatchBranches] = useReducer(branchesReducer, { branches: [], loading: true });
  const [periodsSt,  dispatchPeriods]  = useReducer(periodsReducer,  { periods: [], loading: true, error: '' });
  const [filterStatus,   setFilterStatus]   = useState<LedgerStatus>('Locked');
  const [filterBranchId, setFilterBranchId] = useState<string>('');
  const [libraryPeriod,  setLibraryPeriod]  = useState<FinalizedPayrollLibraryPeriodContext | null>(null);
  const periodRequestId = useRef(0);
  const [searchParams, setSearchParams] = useSearchParams();
  const preselectConsumed = useRef(false);

  // /core/branches already scopes results to what this user can access.
  useEffect(() => {
    dispatchBranches({ type: 'FETCH_START' });
    apiClient
      .get<Branch[]>('/core/branches')
      .then((r) => dispatchBranches({ type: 'FETCH_OK', branches: r.data }))
      .catch(() => dispatchBranches({ type: 'FETCH_ERROR' }));
  }, []);

  const fetchPeriods = useCallback(async () => {
    const requestId = ++periodRequestId.current;
    dispatchPeriods({ type: 'FETCH_START' });
    try {
      const branchId = filterBranchId ? Number(filterBranchId) : undefined;
      const periods = await getFinalizedPeriods(filterStatus, branchId);
      if (requestId === periodRequestId.current) {
        dispatchPeriods({ type: 'FETCH_OK', periods });
      }
    } catch (error) {
      if (requestId === periodRequestId.current) {
        dispatchPeriods({ type: 'FETCH_ERROR', error: ledgerLoadErrorMessage(error) });
      }
    }
  }, [filterStatus, filterBranchId]);

  useEffect(() => { void fetchPeriods(); }, [fetchPeriods]);

  // Deep-link preselection (e.g. from "View Finalized Payroll" right after
  // finalizing a period): open the matching card's dialog once the default
  // Locked list has loaded, then drop the query param so it doesn't re-fire
  // on a later refetch (filter change, dialog close, etc.). A period that
  // isn't found (wrong filter, not yet visible, or a bad id) is a silent
  // no-op — the page still shows the full ledger list to pick from manually.
  // This is a genuine one-time reaction to external navigation state (the
  // URL), guarded by preselectConsumed so it never re-fires on a later
  // refetch — not per-render derived state, so the direct setState calls
  // below are intentional and explicitly acknowledged rather than hidden.
  useEffect(() => {
    if (preselectConsumed.current) return;
    if (periodsSt.loading || periodsSt.error) return;
    const preselectedId = resolvePreselectedPeriodId(searchParams);
    if (preselectedId == null) return;

    preselectConsumed.current = true;
    const match = periodsSt.periods.find((p) => p.period_id === preselectedId);
    if (match) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time deep-link consumption, guarded by preselectConsumed above
      setLibraryPeriod(libraryContextForPeriod(match));
    }
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete('period_id');
      return next;
    }, { replace: true });
  }, [periodsSt, searchParams, setSearchParams]);

  const hasMultipleBranches = branchesSt.branches.length > 1;
  const fixedBranch = branchesSt.branches.length === 1 ? branchesSt.branches[0] : null;

  return (
    <div className={styles.page}>
      {/* ── Header ────────────────────────────────────────────────── */}
      <div className={styles.headerRow}>
        <h2 className={styles.pageTitle}>Ledger</h2>
        <span className={styles.readOnlyTag}>Read-only — finalized payroll</span>
      </div>

      {/* ── Filters ───────────────────────────────────────────────── */}
      <div className={styles.filters}>
        <label className={styles.filterLabel}>
          Status
          <select
            className={styles.filterSelect}
            value={filterStatus}
            onChange={(e) => {
              setLibraryPeriod(null);
              setFilterStatus(e.target.value as LedgerStatus);
            }}
          >
            {LEDGER_STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>

        {hasMultipleBranches ? (
          <label className={styles.filterLabel}>
            Branch
            <select
              className={styles.filterSelect}
              value={filterBranchId}
              onChange={(e) => {
                setLibraryPeriod(null);
                setFilterBranchId(e.target.value);
              }}
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

      {/* ── Content ───────────────────────────────────────────────── */}
      {periodsSt.loading ? (
        <p className={styles.stateMsg}>Loading ledger…</p>
      ) : periodsSt.error ? (
        <p className={styles.errorMsg}>{periodsSt.error}</p>
      ) : periodsSt.periods.length === 0 ? (
        <div className={styles.emptyState}>
          <p className={styles.emptyTitle}>No {filterStatus.toLowerCase()} periods found</p>
          <p className={styles.emptyHint}>
            {filterStatus === 'Locked'
              ? 'Finalized periods will appear here after a payroll is finalized.'
              : 'Archived periods will appear here.'}
          </p>
        </div>
      ) : (
        <div className={styles.cardList}>
          {periodsSt.periods.map((p) => (
            <LedgerCard
              key={p.period_id}
              period={p}
              onOpenLibrary={() => setLibraryPeriod(libraryContextForPeriod(p))}
            />
          ))}
        </div>
      )}

      {libraryPeriod != null && (
        <FinalizedPayrollLibraryDialog
          key={libraryPeriod.payroll_period_id}
          period={libraryPeriod}
          onClose={() => setLibraryPeriod(null)}
        />
      )}
    </div>
  );
}
