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
import apiClient from '../../lib/apiClient';
import { getFinalizedPeriods, getPeriod } from '../../lib/payrollApi';
import { useAuth } from '../../store/authStore';
import { canViewFinalizedLibrary, canViewFinalLines } from '../../lib/permissions';
import type { Branch } from '../../types/core';
import type { FinalizedPeriodListItem, PeriodSummary } from '../../types/payroll';
import { PeriodStatusBadge } from '../../components/StatusBadge';
import { FinalSummaryDialog } from './FinalSummaryDialog';
import {
  FinalizedPayrollLibraryDialog,
  type FinalizedPayrollLibraryPeriodContext,
} from './FinalizedPayrollLibraryDialog';
import styles from './LedgerPage.module.css';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(v: string | null | undefined): string {
  if (v == null) return '-';
  const n = Number(v);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : String(v);
}

function libraryContextForPeriod(period: LedgerPeriod): FinalizedPayrollLibraryPeriodContext {
  if ('period_id' in period) {
    return {
      payroll_period_id: period.period_id,
      period_name: period.period_name,
      period_code: period.period_code,
      branch_name: period.branch_name,
      status: period.period_status,
    };
  }
  return {
    payroll_period_id: period.payroll_period_id,
    period_name: period.period_name,
    period_code: period.period_code,
    branch_name: period.branch_name,
    status: period.status,
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

type LedgerPeriod = PeriodSummary | FinalizedPeriodListItem;
type PeriodsState = { periods: LedgerPeriod[]; loading: boolean; error: string };
type PeriodsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    periods: LedgerPeriod[] }
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
  period: LedgerPeriod;
  onOpenLibrary: (() => void) | null;
  onViewSummary: (() => void) | null;
}

function LedgerCard({ period: p, onOpenLibrary, onViewSummary }: LedgerCardProps) {
  const isFinalizedLibraryItem = 'period_id' in p;
  return (
    <div className={styles.card}>
      {/* Left: period info */}
      <div className={styles.cardInfo}>
        <div className={styles.cardNameRow}>
          <span className={styles.cardName}>{p.period_name || p.period_code}</span>
          <PeriodStatusBadge status={isFinalizedLibraryItem ? p.period_status : p.status} />
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
        {isFinalizedLibraryItem ? (
          <div className={styles.cardStats}>
            <span className={styles.statChip}>
              <span className={styles.statLabel}>Finalized</span>
              <span className={styles.statValue}>{p.finalized_at_utc ? new Date(p.finalized_at_utc).toLocaleDateString() : 'Not provided'}</span>
            </span>
          </div>
        ) : (
          <div className={styles.cardStats}>
            <span className={styles.statChip}>
              <span className={styles.statLabel}>Final Gross</span>
              <span className={styles.statValue}>{fmt(p.final_gross)}</span>
            </span>
            <span className={styles.statChip}>
              <span className={styles.statLabel}>Drivers</span>
              <span className={styles.statValue}>{p.final_driver_count}</span>
            </span>
            <span className={styles.statChip}>
              <span className={styles.statLabel}>Lines</span>
              <span className={styles.statValue}>{p.final_lines}</span>
            </span>
          </div>
        )}
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
  const { user } = useAuth();
  const isAllBranches = user?.scope_type === 'AllCompanyBranches';
  const canOpenLibrary = user != null && canViewFinalizedLibrary(user);
  const canOpenFinalLines = user != null && canViewFinalLines(user);

  const [branchesSt, dispatchBranches] = useReducer(branchesReducer, { branches: [], loading: true });
  const [periodsSt,  dispatchPeriods]  = useReducer(periodsReducer,  { periods: [], loading: true, error: '' });
  const [filterStatus,   setFilterStatus]   = useState<LedgerStatus>('Locked');
  const [filterBranchId, setFilterBranchId] = useState<string>('');
  const [summaryPeriod,  setSummaryPeriod]  = useState<PeriodSummary | null>(null);
  const [libraryPeriod,  setLibraryPeriod]  = useState<FinalizedPayrollLibraryPeriodContext | null>(null);
  const [summaryLoadingId, setSummaryLoadingId] = useState<number | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const periodRequestId = useRef(0);

  // Load branches for AllCompanyBranches users
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
      const data = canOpenLibrary
        ? await getFinalizedPeriods(filterStatus, filterBranchId ? Number(filterBranchId) : undefined)
        : await (async () => {
            const params: Record<string, string> = { status: filterStatus };
            if (filterBranchId) params.branch_id = filterBranchId;
            const response = await apiClient.get<PeriodSummary[]>('/payroll/periods', { params });
            return response.data;
          })();
      if (requestId === periodRequestId.current) {
        dispatchPeriods({ type: 'FETCH_OK', periods: data });
      }
    } catch {
      if (requestId === periodRequestId.current) {
        dispatchPeriods({ type: 'FETCH_ERROR', error: 'Failed to load ledger periods.' });
      }
    }
  }, [canOpenLibrary, filterStatus, filterBranchId]);

  useEffect(() => { void fetchPeriods(); }, [fetchPeriods]);

  const openFinalLines = useCallback(async (period: LedgerPeriod) => {
    setSummaryError(null);
    if (!('period_id' in period)) {
      setSummaryPeriod(period);
      return;
    }
    setSummaryLoadingId(period.period_id);
    try {
      setSummaryPeriod(await getPeriod(period.period_id));
    } catch {
      setSummaryError('Failed to load the operational Final Lines view.');
    } finally {
      setSummaryLoadingId(null);
    }
  }, []);

  const fixedBranch = branchesSt.branches.find(
    (b) => b.branch_id === user?.branch_ids?.[0]
  ) ?? null;

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

        {isAllBranches ? (
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
          {periodsSt.periods.map((p) => (
            <LedgerCard
              key={'period_id' in p ? p.period_id : p.payroll_period_id}
              period={p}
              onOpenLibrary={canOpenLibrary
                ? () => setLibraryPeriod(libraryContextForPeriod(p))
                : null}
              onViewSummary={canOpenFinalLines
                ? () => { void openFinalLines(p); }
                : null}
            />
          ))}
        </div>
      )}

      {/* ── Final Summary dialog ───────────────────────────────────── */}
      {summaryPeriod != null && (
        <FinalSummaryDialog
          period={summaryPeriod}
          onClose={() => setSummaryPeriod(null)}
        />
      )}

      {summaryLoadingId != null && <p className={styles.stateMsg}>Loading final lines…</p>}
      {summaryError != null && <p className={styles.errorMsg}>{summaryError}</p>}

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
