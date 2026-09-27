/**
 * Branch read-only Payroll Schedule page (Phase 6 unit U6).
 *
 * Strictly read-only: shows a Branch's persisted Payroll Setup authority —
 * its BranchAdmin readiness, the /effective authority for the backend-
 * supplied evaluation date, and the full assignment/version history. There
 * is no mutation path here (no create/edit/publish/assign/withdraw); those
 * remain on the company-owned Payroll Setups settings page.
 *
 * Never constructs a JS Date object or reads the wall clock — every date
 * shown here is an ISO string from the backend, rendered as-is. Never
 * derives readiness, the effective Setup/Version, or period boundaries
 * client-side; branchScheduleView.ts's pure helpers only format what the
 * backend already decided.
 */
import { useEffect, useReducer } from 'react';
import { useSearchParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import { useAuth } from '../../../store/authStore';
import { canViewBranchPayrollSchedule } from '../../../lib/permissions';
import { SectionCard, EmptyState, ErrorState } from '../../../components/ui';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import { readApiError } from '../../../lib/payrollSetupErrors';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import { formatIsoLong, formatIsoRange } from '../../../lib/isoDate';
import {
  getBranchEffectivePayrollSetup,
  getBranchPayrollSetupHistory,
} from '../../../lib/payrollSetupApi';
import type { BranchHistoryResponse, EffectiveAuthorityResponse } from '../../../types/payrollSetup';
import type { BranchAdmin } from '../../../types/settings';
import {
  annotateHistory,
  assignmentHistoryLine,
  branchDisplay,
  effectiveRequestDate,
  fullDaysOffLine,
  parseBranchIdParam,
  periodsStartLine,
  readinessView,
  resolveBranchSelection,
  scheduleFrequencyNoun,
  upcomingChange,
  upcomingChangeText,
  versionHistoryLine,
} from './branchScheduleView';
import styles from './BranchPayrollSchedulePage.module.css';

// ── Branch list load ──────────────────────────────────────────────────────────

interface BranchListState {
  branches: BranchAdmin[];
  loading: boolean;
  error: ApiErrorInfo | null;
}
type BranchListAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; branches: BranchAdmin[] }
  | { type: 'FETCH_ERROR'; error: ApiErrorInfo };

function branchListReducer(s: BranchListState, a: BranchListAction): BranchListState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true, error: null };
    case 'FETCH_OK': return { branches: a.branches, loading: false, error: null };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
  }
}

// ── Readiness load ────────────────────────────────────────────────────────────
//
// Each action carries the branchId the request was made for, and the
// reducer stores it alongside the data/loading/error — this lets render
// derive whether the state on hand actually belongs to the CURRENTLY
// selected branch (see readinessCurrent below), instead of trusting a
// stale previous branch's data during the one render between a branch
// switch and that branch's own FETCH_START.

interface ReadinessState {
  branchId: number | null;
  data: BranchAdmin | null;
  loading: boolean;
  error: ApiErrorInfo | null;
}
type ReadinessAction =
  | { type: 'FETCH_START'; branchId: number }
  | { type: 'FETCH_OK'; branchId: number; data: BranchAdmin }
  | { type: 'FETCH_ERROR'; branchId: number; error: ApiErrorInfo };

function readinessReducer(_s: ReadinessState, a: ReadinessAction): ReadinessState {
  switch (a.type) {
    case 'FETCH_START': return { branchId: a.branchId, data: null, loading: true, error: null };
    case 'FETCH_OK': return { branchId: a.branchId, data: a.data, loading: false, error: null };
    case 'FETCH_ERROR': return { branchId: a.branchId, data: null, loading: false, error: a.error };
  }
}

// ── History load ───────────────────────────────────────────────────────────────
//
// Same branch-tagging pattern as readiness, above.

interface HistoryState {
  branchId: number | null;
  data: BranchHistoryResponse | null;
  loading: boolean;
  error: ApiErrorInfo | null;
}
type HistoryAction =
  | { type: 'FETCH_START'; branchId: number }
  | { type: 'FETCH_OK'; branchId: number; data: BranchHistoryResponse }
  | { type: 'FETCH_ERROR'; branchId: number; error: ApiErrorInfo };

function historyReducer(_s: HistoryState, a: HistoryAction): HistoryState {
  switch (a.type) {
    case 'FETCH_START': return { branchId: a.branchId, data: null, loading: true, error: null };
    case 'FETCH_OK': return { branchId: a.branchId, data: a.data, loading: false, error: null };
    case 'FETCH_ERROR': return { branchId: a.branchId, data: null, loading: false, error: a.error };
  }
}

// ── Effective authority load ──────────────────────────────────────────────────
//
// Tagged by a request key (`${branchId}|${effectiveDate}`) rather than a
// status enum with a distinct "not requested" action — render derives
// "not-requested" itself by comparing the current key to the one the
// state was last fetched for (see effectiveStatus below), so a render
// during which branchId/effectiveDate have already moved on never shows
// the previous request's data/error as if it were current.

interface EffectiveState {
  key: string | null;
  status: 'loading' | 'error' | 'ok';
  data: EffectiveAuthorityResponse | null;
  error: ApiErrorInfo | null;
}
type EffectiveAction =
  | { type: 'FETCH_START'; key: string }
  | { type: 'FETCH_OK'; key: string; data: EffectiveAuthorityResponse }
  | { type: 'FETCH_ERROR'; key: string; error: ApiErrorInfo };

function effectiveReducer(_s: EffectiveState, a: EffectiveAction): EffectiveState {
  switch (a.type) {
    case 'FETCH_START': return { key: a.key, status: 'loading', data: null, error: null };
    case 'FETCH_OK': return { key: a.key, status: 'ok', data: a.data, error: null };
    case 'FETCH_ERROR': return { key: a.key, status: 'error', data: null, error: a.error };
  }
}

// ── Component ────────────────────────────────────────────────────────────────

export function BranchPayrollSchedulePage() {
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();

  // ── Branch options ──────────────────────────────────────────────────────────
  const [branchListState, dispatchBranchList] = useReducer(branchListReducer, {
    branches: [],
    loading: true,
    error: null,
  });
  const { branches, loading: branchListLoading, error: branchListError } = branchListState;

  useEffect(() => {
    let cancelled = false;
    dispatchBranchList({ type: 'FETCH_START' });
    apiClient
      .get<BranchAdmin[]>('/settings/branches')
      .then((resp) => {
        if (!cancelled) dispatchBranchList({ type: 'FETCH_OK', branches: resp.data });
      })
      .catch((err) => {
        if (!cancelled) {
          dispatchBranchList({ type: 'FETCH_ERROR', error: readApiError(err, 'Failed to load Branches.') });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const viewable = branches.filter((b) => user !== null && canViewBranchPayrollSchedule(user, b.branch_id));
  const viewableBranchIds = viewable.map((b) => b.branch_id);

  // ── Selection ────────────────────────────────────────────────────────────────
  const branchIdParam = searchParams.get('branchId');
  const selection = branchListLoading
    ? null
    : resolveBranchSelection(parseBranchIdParam(branchIdParam), viewableBranchIds);

  const redirectBranchId = selection !== null && selection.kind === 'redirect' ? selection.branchId : null;

  useEffect(() => {
    if (redirectBranchId !== null) {
      setSearchParams({ branchId: String(redirectBranchId) }, { replace: true });
    }
  }, [redirectBranchId, setSearchParams]);

  const branchId = selection !== null && selection.kind === 'selected' ? selection.branchId : null;

  function handlePickerChange(value: string) {
    if (value === '') return;
    setSearchParams({ branchId: value });
  }

  // ── Readiness load ────────────────────────────────────────────────────────
  const [readinessState, dispatchReadiness] = useReducer(readinessReducer, {
    branchId: null,
    data: null,
    loading: false,
    error: null,
  });

  useEffect(() => {
    if (branchId === null) return;
    let cancelled = false;
    dispatchReadiness({ type: 'FETCH_START', branchId });
    apiClient
      .get<BranchAdmin>(`/settings/branches/${branchId}`)
      .then((resp) => {
        if (!cancelled) dispatchReadiness({ type: 'FETCH_OK', branchId, data: resp.data });
      })
      .catch((err) => {
        if (!cancelled) {
          dispatchReadiness({
            type: 'FETCH_ERROR',
            branchId,
            error: readApiError(err, 'Failed to load Branch readiness.'),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [branchId]);

  // readinessCurrent is true only once the readiness state on hand was
  // actually fetched for the currently selected branchId — during the one
  // render between a branch switch and that branch's own FETCH_START, the
  // previous branch's readinessState.branchId still doesn't match, so the
  // derived values below correctly read as null/loading instead of stale.
  const readinessCurrent = branchId !== null && readinessState.branchId === branchId;
  const readinessData = readinessCurrent ? readinessState.data : null;
  const readinessError = readinessCurrent ? readinessState.error : null;
  const readinessLoading = branchId !== null && (!readinessCurrent || readinessState.loading);

  // ── History load — depends ONLY on branchId ─────────────────────────────────
  const [historyState, dispatchHistory] = useReducer(historyReducer, {
    branchId: null,
    data: null,
    loading: false,
    error: null,
  });

  useEffect(() => {
    if (branchId === null) return;
    let cancelled = false;
    dispatchHistory({ type: 'FETCH_START', branchId });
    getBranchPayrollSetupHistory(branchId)
      .then((data) => {
        if (!cancelled) dispatchHistory({ type: 'FETCH_OK', branchId, data });
      })
      .catch((err) => {
        if (!cancelled) {
          dispatchHistory({
            type: 'FETCH_ERROR',
            branchId,
            error: readApiError(err, 'Failed to load Assignment history.'),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [branchId]);

  const historyCurrent = branchId !== null && historyState.branchId === branchId;
  const historyData = historyCurrent ? historyState.data : null;
  const historyError = historyCurrent ? historyState.error : null;
  const historyLoading = branchId !== null && (!historyCurrent || historyState.loading);

  // ── Effective authority load ────────────────────────────────────────────────
  // effectiveDate is derived from readinessData (the CURRENT branch's
  // readiness only) — never from the raw readinessState, which could still
  // hold the previous branch's data/date for one render after a switch.
  const effectiveDate = readinessData ? effectiveRequestDate(readinessData) : null;

  const [effectiveState, dispatchEffective] = useReducer(effectiveReducer, {
    key: null,
    status: 'loading',
    data: null,
    error: null,
  });

  useEffect(() => {
    if (branchId === null || effectiveDate === null) return;
    const key = `${branchId}|${effectiveDate}`;
    let cancelled = false;
    dispatchEffective({ type: 'FETCH_START', key });
    getBranchEffectivePayrollSetup(branchId, effectiveDate)
      .then((data) => {
        if (!cancelled) dispatchEffective({ type: 'FETCH_OK', key, data });
      })
      .catch((err) => {
        if (!cancelled) {
          dispatchEffective({
            type: 'FETCH_ERROR',
            key,
            error: readApiError(err, 'Failed to load the effective Payroll Setup authority.'),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [branchId, effectiveDate]);

  // effectiveKey is this render's own request identity; effectiveStatus
  // compares it against the key effectiveState was last fetched/dispatched
  // for. If they differ — e.g. the branch or effectiveDate just changed and
  // the effect above hasn't dispatched FETCH_START yet — this render reads
  // as 'loading' rather than showing the previous request's stale data,
  // error, or "not requested" text under the new selection.
  const effectiveKey = branchId !== null && effectiveDate !== null ? `${branchId}|${effectiveDate}` : null;
  const effectiveStatus: 'not-requested' | 'loading' | 'error' | 'ok' =
    effectiveKey === null ? 'not-requested' : effectiveState.key !== effectiveKey ? 'loading' : effectiveState.status;
  const effectiveData = effectiveStatus === 'ok' ? effectiveState.data : null;
  const effectiveError = effectiveStatus === 'error' ? effectiveState.error : null;

  // ── Render ───────────────────────────────────────────────────────────────────

  if (branchListError) {
    return (
      <div className={styles.page}>
        <ErrorState
          title="Failed to load Branches"
          message={`${branchListError.message}${branchListError.code != null ? ` (${branchListError.code})` : ''}${
            branchListError.status != null ? ` — HTTP ${branchListError.status}` : ''
          }`}
        />
      </div>
    );
  }

  const picker = !branchListLoading && (
    <div className={styles.pickerRow}>
      <label className={styles.pickerLabel} htmlFor="branch-schedule-picker">
        Branch
      </label>
      <select
        id="branch-schedule-picker"
        className={styles.pickerSelect}
        value={branchId !== null ? String(branchId) : ''}
        onChange={(e) => handlePickerChange(e.target.value)}
      >
        {branchId === null && (
          <option value="" disabled>
            Select a Branch
          </option>
        )}
        {viewable.map((b) => (
          <option key={b.branch_id} value={b.branch_id}>
            {branchDisplay(b)}
          </option>
        ))}
      </select>
    </div>
  );

  return (
    <div className={styles.page}>
      <p className={styles.introLine}>
        Read-only view of this Branch&apos;s persisted Payroll Setup authority. Changes are made by company
        administrators.
      </p>

      {picker}

      {branchListLoading ? (
        <p className={styles.mutedText}>Loading Branches…</p>
      ) : selection === null || selection.kind === 'none' ? (
        <EmptyState
          title="No Branches available"
          message="You do not have payroll.view on any Branch you can access."
        />
      ) : selection.kind === 'invalid' ? (
        <p className={styles.noticeText}>{`"${selection.raw}" is not a valid Branch id. Choose a Branch below.`}</p>
      ) : selection.kind === 'unavailable' ? (
        <p className={styles.noticeText}>
          {`Branch #${selection.branchId} is not available to you, or does not exist. Choose a Branch below.`}
        </p>
      ) : selection.kind === 'redirect' ? (
        <p className={styles.mutedText}>Loading Branch…</p>
      ) : (
        <>
          {/* ── Readiness ── */}
          <SectionCard title="Readiness">
            {readinessLoading ? (
              <p className={styles.mutedText}>Loading readiness…</p>
            ) : readinessError ? (
              <PayrollErrorNotice info={readinessError} />
            ) : readinessData ? (
              <div className={styles.readinessBody}>
                <p className={styles.branchName}>{branchDisplay(readinessData)}</p>
                {(() => {
                  const view = readinessView(readinessData.schedule_readiness_reason);
                  if (view.kind === 'unavailable') {
                    return <p className={styles.mutedText}>Readiness is not available for this Branch.</p>;
                  }
                  if (view.kind === 'ready') {
                    return (
                      <div className={styles.readinessLine}>
                        <span className={styles.readinessLabel}>{view.label}</span>
                        {view.description && <span className={styles.mutedText}>{view.description}</span>}
                        <details className={styles.mutedNote}>
                          <summary>Technical details</summary>
                          <p>{`Readiness code: ${view.code}`}</p>
                        </details>
                      </div>
                    );
                  }
                  return (
                    <div className={styles.readinessLine}>
                      <span className={styles.readinessLabel}>{view.label}</span>
                      <span className={styles.mutedText}>{view.explanation}</span>
                      <details className={styles.mutedNote}>
                        <summary>Technical details</summary>
                        <p>{`Readiness code: ${view.code}`}</p>
                      </details>
                    </div>
                  );
                })()}
                <p className={styles.mutedText}>
                  {(() => {
                    const readinessDate = readinessData.schedule_readiness_date;
                    return readinessDate !== null
                      ? `Evaluated for period starting: ${formatIsoLong(readinessDate)}`
                      : 'Evaluation date: not supplied.';
                  })()}
                </p>
              </div>
            ) : null}
          </SectionCard>

          {/* ── Next payroll period ── */}
          <SectionCard title="Next payroll period">
            {readinessLoading ? (
              <p className={styles.mutedText}>Waiting for readiness…</p>
            ) : readinessError ? (
              <p className={styles.mutedText}>Not requested — readiness could not be loaded.</p>
            ) : effectiveStatus === 'not-requested' ? (
              (() => {
                const view = readinessData ? readinessView(readinessData.schedule_readiness_reason) : null;
                if (view === null || view.kind === 'unavailable') {
                  return <p className={styles.mutedText}>Not requested — readiness is not available.</p>;
                }
                return <p className={styles.mutedText}>{`Not requested — readiness is ${view.label}.`}</p>;
              })()
            ) : effectiveStatus === 'loading' ? (
              <p className={styles.mutedText}>Loading effective authority…</p>
            ) : effectiveStatus === 'error' && effectiveError ? (
              <PayrollErrorNotice info={effectiveError} />
            ) : effectiveStatus === 'ok' && effectiveData ? (
              <div className={styles.effectiveBody}>
                <p className={styles.effectiveLine}>
                  {`Next payroll period: ${formatIsoRange(effectiveData.period_start_date, effectiveData.period_end_date)}`}
                </p>
                <p className={styles.effectiveLine}>{`Payroll policy: ${effectiveData.setup_name}`}</p>
                <div className={styles.scheduleBlock}>
                  <p className={styles.scheduleValue}>{scheduleFrequencyNoun(effectiveData.schedule)}</p>
                  <p className={styles.scheduleValue}>
                    {periodsStartLine(effectiveData.schedule, effectiveData.period_start_date)}
                  </p>
                  <p className={styles.scheduleValue}>{fullDaysOffLine(effectiveData.schedule.normal_days_off_mask)}</p>
                </div>
                <p className={styles.effectiveLine}>{`Policy version: Version ${effectiveData.version_number}`}</p>
                <p className={styles.effectiveLine}>
                  {upcomingChangeText(upcomingChange(effectiveData, historyData))}
                </p>
                <details className={styles.mutedNote}>
                  <summary>Technical details</summary>
                  <p>{`Setup code: ${effectiveData.setup_code} (Setup #${effectiveData.setup_id})`}</p>
                  <p>{`Assignment #${effectiveData.assignment_id}`}</p>
                  <p>{`Version #${effectiveData.version_id}`}</p>
                  <p>{`Anchor start date: ${effectiveData.schedule.anchor_start_date}`}</p>
                  <p>{`Normal days off mask: ${effectiveData.schedule.normal_days_off_mask}`}</p>
                  <p>
                    {'Config hash: '}
                    <span className={styles.monospace}>{effectiveData.config_hash}</span>
                  </p>
                  <p>{`Next boundary date: ${effectiveData.next_boundary_date ?? '—'}`}</p>
                  <p>{`Next boundary kind: ${effectiveData.next_boundary_kind ?? '—'}`}</p>
                </details>
              </div>
            ) : null}
          </SectionCard>

          {/* ── Policy history ── */}
          <SectionCard title="Policy history">
            {historyLoading ? (
              <p className={styles.mutedText}>Loading history…</p>
            ) : historyError ? (
              <PayrollErrorNotice info={historyError} />
            ) : historyData ? (
              historyData.assignments.length === 0 ? (
                <p className={styles.mutedText}>No policy history is recorded for this branch.</p>
              ) : (
                <>
                  {(() => {
                    const readinessDate = readinessData?.schedule_readiness_date;
                    return readinessDate === null || readinessDate === undefined;
                  })() && (
                    <p className={styles.mutedNote}>
                      No readiness evaluation date — history is shown without scheduled tags.
                    </p>
                  )}
                  <ul className={styles.assignmentList}>
                    {annotateHistory(
                      historyData,
                      effectiveData,
                      readinessData?.schedule_readiness_date ?? null,
                    ).map((row) => (
                      <li
                        key={row.assignment.assignment_id}
                        className={row.withdrawn ? `${styles.assignmentRow} ${styles.muted}` : styles.assignmentRow}
                      >
                        <p className={styles.assignmentTitle}>{assignmentHistoryLine(row)}</p>
                        <div className={styles.tagRow}>
                          {row.governing && <span className={styles.tag}>In effect</span>}
                          {row.scheduled && <span className={styles.tag}>Scheduled</span>}
                        </div>
                        {row.assignment.reason != null && (
                          <p className={styles.assignmentLine}>{`Reason: ${row.assignment.reason}`}</p>
                        )}
                        {row.withdrawn && row.assignment.withdrawal_reason != null && (
                          <p className={styles.assignmentLine}>
                            {`Cancellation reason: ${row.assignment.withdrawal_reason}`}
                          </p>
                        )}
                        <details className={styles.mutedNote}>
                          <summary>Technical details</summary>
                          <p>{`Assignment #${row.assignment.assignment_id} · Setup #${row.assignment.setup_id} (${row.assignment.setup_code})`}</p>
                        </details>

                        {row.versions.length > 0 && (
                          <ul className={styles.versionList}>
                            {row.versions.map((v) => (
                              <li key={v.segment.version_id} className={styles.versionRow}>
                                <p className={styles.versionLine}>{versionHistoryLine(v.segment)}</p>
                                <div className={styles.tagRow}>
                                  {v.governing && <span className={styles.tag}>In effect</span>}
                                  {v.scheduled && <span className={styles.tag}>Scheduled</span>}
                                </div>
                                <details className={styles.mutedNote}>
                                  <summary>Technical details</summary>
                                  <p>{`Version #${v.segment.version_id}`}</p>
                                  <p>
                                    {'Config hash: '}
                                    <span className={styles.monospace}>{v.segment.config_hash}</span>
                                  </p>
                                </details>
                              </li>
                            ))}
                          </ul>
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )
            ) : null}
          </SectionCard>
        </>
      )}
    </div>
  );
}
