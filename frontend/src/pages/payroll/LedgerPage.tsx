/**
 * LedgerPage — CP-4
 *
 * Read-only view of finalized (Locked/Archived) payroll periods.
 * Accessible from the sidebar. Each period card shows final aggregates from
 * vw_PayrollPeriodList and opens either the P6A library or legacy FinalLines.
 *
 * Security: Driver/ODA access is blocked by the backend. P6A and FinalLines
 * retain their distinct permission contracts; frontend gating is UI-only.
 */
import { useEffect, useReducer, useState, useCallback, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import apiClient from '../../lib/apiClient';
import { getFinalizedPeriods } from '../../lib/payrollApi';
import type { Branch } from '../../types/core';
import type { FinalizedPeriodListItem, PeriodSummary } from '../../types/payroll';
import { PeriodStatusBadge } from '../../components/StatusBadge';
import { FinalSummaryDialog } from './FinalSummaryDialog';
import {
  FinalizedPayrollLibraryDialog,
  type FinalizedPayrollLibraryPeriodContext,
} from './FinalizedPayrollLibraryDialog';
import { discoverLedgerPeriods, type LedgerDiscoveryDeps, type LedgerDiscoveryPeriod } from './ledgerDiscovery';
import { resolvePreselectedPeriodId } from './finalizedNavigation';
import styles from './LedgerPage.module.css';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(v: string | null | undefined): string {
  if (v == null) return '-';
  const n = Number(v);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : String(v);
}

function libraryContextForPeriod(finalized: FinalizedPeriodListItem): FinalizedPayrollLibraryPeriodContext {
  return {
    payroll_period_id: finalized.period_id,
    period_name: finalized.period_name,
    period_code: finalized.period_code,
    branch_name: finalized.branch_name,
    status: finalized.period_status,
  };
}

// Module scope keeps this reference stable across renders (used as a useCallback dep).
const ledgerDiscoveryDeps: LedgerDiscoveryDeps = {
  fetchFinalized: getFinalizedPeriods,
  fetchOperational: async (status, branchId) => {
    const params: Record<string, string> = { status };
    if (branchId != null) params.branch_id = String(branchId);
    const response = await apiClient.get<PeriodSummary[]>('/payroll/periods', { params });
    return response.data;
  },
};

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

type PeriodsState = { periods: LedgerDiscoveryPeriod[]; loading: boolean; error: string };
type PeriodsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    periods: LedgerDiscoveryPeriod[] }
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
  period: LedgerDiscoveryPeriod;
  onOpenLibrary: (() => void) | null;
  onViewSummary: (() => void) | null;
}

function LedgerCard({ period: p, onOpenLibrary, onViewSummary }: LedgerCardProps) {
  // Finalized is the display authority when a period has both records — this
  // matches the presentation a ledger.view user saw before merged discovery.
  const status = p.finalized ? p.finalized.period_status : p.operational ? p.operational.status : null;
  const primary = p.finalized ?? p.operational;
  if (status == null || primary == null) return null;

  return (
    <div className={styles.card}>
      {/* Left: period info */}
      <div className={styles.cardInfo}>
        <div className={styles.cardNameRow}>
          <span className={styles.cardName}>{primary.period_name || primary.period_code}</span>
          <PeriodStatusBadge status={status} />
        </div>
        <div className={styles.cardMeta}>
          <span>{primary.branch_name}</span>
          <span className={styles.metaSep}>·</span>
          <span>{primary.period_type}</span>
          <span className={styles.metaSep}>·</span>
          <span>{primary.start_date} – {primary.end_date}</span>
          {primary.pay_date && (
            <>
              <span className={styles.metaSep}>·</span>
              <span>Pay: {primary.pay_date}</span>
            </>
          )}
        </div>
        {p.finalized ? (
          <div className={styles.cardStats}>
            <span className={styles.statChip}>
              <span className={styles.statLabel}>Finalized</span>
              <span className={styles.statValue}>{p.finalized.finalized_at_utc ? new Date(p.finalized.finalized_at_utc).toLocaleDateString() : 'Not provided'}</span>
            </span>
          </div>
        ) : p.operational ? (
          <div className={styles.cardStats}>
            <span className={styles.statChip}>
              <span className={styles.statLabel}>Final Gross</span>
              <span className={styles.statValue}>{fmt(p.operational.final_gross)}</span>
            </span>
            <span className={styles.statChip}>
              <span className={styles.statLabel}>Drivers</span>
              <span className={styles.statValue}>{p.operational.final_driver_count}</span>
            </span>
            <span className={styles.statChip}>
              <span className={styles.statLabel}>Lines</span>
              <span className={styles.statValue}>{p.operational.final_lines}</span>
            </span>
          </div>
        ) : null}
      </div>

      {/* Right: action */}
      <div className={styles.cardActions}>
        {onOpenLibrary && (
          <button className={styles.primaryBtn} onClick={onOpenLibrary} type="button">
            Open Finalized Payroll
          </button>
        )}
        {onViewSummary && (
          <button className={styles.secondaryBtn} onClick={onViewSummary} type="button">
            Final Lines
          </button>
        )}
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
  const [summaryPeriod,  setSummaryPeriod]  = useState<PeriodSummary | null>(null);
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
      const periods = await discoverLedgerPeriods(ledgerDiscoveryDeps, filterStatus, branchId);
      if (requestId === periodRequestId.current) {
        dispatchPeriods({ type: 'FETCH_OK', periods });
      }
    } catch {
      if (requestId === periodRequestId.current) {
        dispatchPeriods({ type: 'FETCH_ERROR', error: 'Failed to load ledger periods.' });
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
    const match = periodsSt.periods.find((p) => p.identity === preselectedId);
    if (match?.finalized) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time deep-link consumption, guarded by preselectConsumed above
      setLibraryPeriod(libraryContextForPeriod(match.finalized));
    } else if (match?.operational) {
      setSummaryPeriod(match.operational);
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
              setSummaryPeriod(null);
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
                setSummaryPeriod(null);
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
          {periodsSt.periods.map((p) => {
            const { finalized, operational } = p;
            return (
              <LedgerCard
                key={p.identity}
                period={p}
                onOpenLibrary={finalized
                  ? () => setLibraryPeriod(libraryContextForPeriod(finalized))
                  : null}
                onViewSummary={operational
                  ? () => setSummaryPeriod(operational)
                  : null}
              />
            );
          })}
        </div>
      )}

      {/* ── Final Summary dialog ───────────────────────────────────── */}
      {summaryPeriod != null && (
        <FinalSummaryDialog
          period={summaryPeriod}
          onClose={() => setSummaryPeriod(null)}
        />
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
