/**
 * LedgerPage — CP-4
 *
 * Read-only view of finalized (Locked/Archived) payroll periods.
 * Accessible from the sidebar.  Each period card shows final aggregates
 * from vw_PayrollPeriodList and allows opening the Final Summary dialog.
 *
 * Security: Ledger is operational/admin only.  Driver/ODA access is blocked
 * by the backend (403 on final-lines endpoint).  Frontend shows 403 cleanly.
 */
import { useEffect, useReducer, useState, useCallback, useRef } from 'react';
import apiClient from '../../lib/apiClient';
import { useAuth } from '../../store/authStore';
import type { Branch } from '../../types/core';
import type { PeriodSummary } from '../../types/payroll';
import { PeriodStatusBadge } from '../../components/StatusBadge';
import { FinalSummaryDialog } from './FinalSummaryDialog';
import styles from './LedgerPage.module.css';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(v: string | null | undefined): string {
  if (v == null) return '-';
  const n = Number(v);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : String(v);
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

type PeriodsState = { periods: PeriodSummary[]; loading: boolean; error: string };
type PeriodsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    periods: PeriodSummary[] }
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
  period: PeriodSummary;
  onViewSummary: () => void;
}

function LedgerCard({ period: p, onViewSummary }: LedgerCardProps) {
  return (
    <div className={styles.card}>
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
      </div>

      {/* Right: action */}
      <div className={styles.cardActions}>
        <button className={styles.viewBtn} onClick={onViewSummary}>
          View Final Summary
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
  const { user } = useAuth();
  const isAllBranches = user?.scope_type === 'AllCompanyBranches';

  const [branchesSt, dispatchBranches] = useReducer(branchesReducer, { branches: [], loading: true });
  const [periodsSt,  dispatchPeriods]  = useReducer(periodsReducer,  { periods: [], loading: true, error: '' });
  const [filterStatus,   setFilterStatus]   = useState<LedgerStatus>('Locked');
  const [filterBranchId, setFilterBranchId] = useState<string>('');
  const [summaryPeriod,  setSummaryPeriod]  = useState<PeriodSummary | null>(null);
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
      const params: Record<string, string> = { status: filterStatus };
      if (filterBranchId) params.branch_id = filterBranchId;
      const { data } = await apiClient.get<PeriodSummary[]>('/payroll/periods', { params });
      if (requestId === periodRequestId.current) {
        dispatchPeriods({ type: 'FETCH_OK', periods: data });
      }
    } catch {
      if (requestId === periodRequestId.current) {
        dispatchPeriods({ type: 'FETCH_ERROR', error: 'Failed to load ledger periods.' });
      }
    }
  }, [filterStatus, filterBranchId]);

  useEffect(() => { void fetchPeriods(); }, [fetchPeriods]);

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
              key={p.payroll_period_id}
              period={p}
              onViewSummary={() => setSummaryPeriod(p)}
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
    </div>
  );
}
