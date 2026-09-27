/**
 * Company Payroll Policies page (Phase 6 U5a/U5b/U5c foundation + Unit A
 * terminology/UX pass + master-detail redesign Step 1 + Step 2).
 *
 * This file is the SHELL: header, tabs, the Policies tab's master (policy
 * list, now with search/filter and its own pinned "+ Add policy" footer) +
 * a detail column, the create-policy modal, and the Branch
 * Assignments tab mount.
 *
 * Everything that used to render in the detail column (company default,
 * selected-policy fields/actions, "Next steps", drafts list, publish panel,
 * policy-updates table, assigned-branches table, and their confirms/modals)
 * lives in PolicyDetailPanel.tsx. Step 3A mounts it with only the header +
 * Policy details section visible; the lower sections stay parked behind its
 * showLowerSections prop until their own steps.
 *
 * Step 2 replaces the master pane's flat status-badge list with a
 * search/filter list driven by policyMasterView.ts's derived usage state
 * (Active/Inactive/Archived — never the raw setup.status) and moves policy
 * creation into the pane's own pinned footer; the page-level header create
 * button is gone (the Welcome empty-state create button is unchanged).
 *
 * Never constructs a JS Date object or reads the wall clock — every date
 * shown here is an ISO string from the backend, rendered as-is or through
 * isoDate's pure calendar formatting.
 */
import { useCallback, useEffect, useMemo, useReducer, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '../../../store/authStore';
import {
  canAssignPayrollSetups,
  canManagePayrollSetups,
  canPublishPayrollSetups,
} from '../../../lib/permissions';
import { PageHeader, SectionCard } from '../../../components/ui';
import { PayrollErrorNotice } from '../../../components/payroll/PayrollErrorNotice';
import { readApiError } from '../../../lib/payrollSetupErrors';
import type { ApiErrorInfo } from '../../../lib/payrollSetupErrors';
import {
  createPayrollSetup,
  getDefaultPayrollSetup,
  listBranchPolicySummaries,
  listPayrollSetups,
} from '../../../lib/payrollSetupApi';
import type { BranchPolicySummaryResponse, SetupResponse } from '../../../types/payrollSetup';
import { normalizeDescription, validateSetupCreate } from './payrollSetupsView';
import type { PolicyFilter } from './policyMasterView';
import { futureStartLabel, policyUsage, visiblePolicies } from './policyMasterView';
import { resolveSelectedPolicyId } from './policyDetailView';
import { PolicyDetailPanel } from './PolicyDetailPanel';
import { BranchAssignmentsTab } from './BranchAssignmentsTab';
import styles from './PayrollSetupsPage.module.css';

// ── Tabs ─────────────────────────────────────────────────────────────────────

type Tab = 'policies' | 'assignments';

// ── Forms ────────────────────────────────────────────────────────────────────

interface CreateForm {
  setup_code: string;
  setup_name: string;
  description: string;
}

const EMPTY_CREATE_FORM: CreateForm = { setup_code: '', setup_name: '', description: '' };

// ── Async data-loading reducers ─────────────────────────────────────────────
//
// Using useReducer (a single dispatch per outcome) avoids calling multiple
// useState setters synchronously inside effect bodies
// (react-hooks/set-state-in-effect) — same pattern as StatusKeysPage.tsx.

interface SetupsState {
  setups: SetupResponse[];
  defaultSetupId: number | null;
  loading: boolean;
  error: ApiErrorInfo | null;
}
type SetupsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; setups: SetupResponse[]; defaultSetupId: number | null }
  | { type: 'FETCH_ERROR'; error: ApiErrorInfo };

function setupsReducer(s: SetupsState, a: SetupsAction): SetupsState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true, error: null };
    case 'FETCH_OK': return { setups: a.setups, defaultSetupId: a.defaultSetupId, loading: false, error: null };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
  }
}

interface SummariesState {
  summaries: BranchPolicySummaryResponse[];
  loading: boolean;
  error: ApiErrorInfo | null;
}
type SummariesAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; summaries: BranchPolicySummaryResponse[] }
  | { type: 'FETCH_ERROR'; error: ApiErrorInfo };

function summariesReducer(s: SummariesState, a: SummariesAction): SummariesState {
  switch (a.type) {
    case 'FETCH_START': return { ...s, loading: true, error: null };
    case 'FETCH_OK': return { summaries: a.summaries, loading: false, error: null };
    case 'FETCH_ERROR': return { ...s, loading: false, error: a.error };
  }
}

// ── Component ────────────────────────────────────────────────────────────────

export function PayrollSetupsPage() {
  const { user } = useAuth();
  const canManage = user !== null && canManagePayrollSetups(user);
  const canAssign = user !== null && canAssignPayrollSetups(user);
  const canPublish = user !== null && canPublishPayrollSetups(user);

  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState<Tab>('policies');

  // Success actions refresh the affected data and selection. Keep the child
  // callback for the existing component contract, but do not insert a
  // layout-shifting success banner into the page.
  const showToast = useCallback(() => {}, []);

  // ── Policies + company default ────────────────────────────────────────────
  const [setupsState, dispatchSetups] = useReducer(setupsReducer, {
    setups: [],
    defaultSetupId: null,
    loading: true,
    error: null,
  });
  const { setups, defaultSetupId, loading: setupsLoading, error: setupsError } = setupsState;

  const loadSetups = useCallback(async () => {
    dispatchSetups({ type: 'FETCH_START' });
    try {
      const [setupsList, defaultResp] = await Promise.all([
        listPayrollSetups(),
        getDefaultPayrollSetup(),
      ]);
      dispatchSetups({
        type: 'FETCH_OK',
        setups: setupsList,
        defaultSetupId: defaultResp.setup?.setup_id ?? null,
      });
    } catch (err) {
      dispatchSetups({ type: 'FETCH_ERROR', error: readApiError(err, 'Failed to load payroll policies.') });
    }
  }, []);

  useEffect(() => {
    loadSetups();
  }, [loadSetups]);

  // ── Branch policy summaries (Branch Assignments tab, master-pane usage
  // state, and PolicyDetailPanel) ────────────────────────────────────────────
  const [summariesState, dispatchSummaries] = useReducer(summariesReducer, {
    summaries: [],
    loading: true,
    error: null,
  });
  const { summaries, loading: summariesLoading, error: summariesError } = summariesState;
  const [summariesReloadKey, setSummariesReloadKey] = useState(0);

  useEffect(() => {
    let active = true;
    dispatchSummaries({ type: 'FETCH_START' });
    listBranchPolicySummaries()
      .then((rows) => {
        if (active) dispatchSummaries({ type: 'FETCH_OK', summaries: rows });
      })
      .catch((err) => {
        if (active) dispatchSummaries({ type: 'FETCH_ERROR', error: readApiError(err, 'Failed to load branches.') });
      });
    return () => {
      active = false;
    };
  }, [summariesReloadKey]);

  // ── Master pane: derived usage state + search/filter (Step 2) ────────────
  const [filter, setFilter] = useState<PolicyFilter>('All');
  const [search, setSearch] = useState('');
  const usage = useMemo(() => policyUsage(setups, summaries), [setups, summaries]);
  const visible = useMemo(
    () => visiblePolicies(setups, usage, filter, search),
    [setups, usage, filter, search],
  );

  // ── Selection (?setupId=) — resolved against the VISIBLE list ────────────
  // While summaries load, usage state is unreliable, so nothing is filtered
  // out yet. A selected policy hidden by search/filter falls back to the
  // first visible one; with none visible, selection (and the param) clears.
  const setupIdParam = searchParams.get('setupId');
  const selectableList = summariesLoading ? setups : visible;
  const selectedPolicyId = useMemo(
    () => resolveSelectedPolicyId(setupIdParam, selectableList),
    [setupIdParam, selectableList],
  );
  const selectedPolicy = setups.find((s) => s.setup_id === selectedPolicyId) ?? null;

  useEffect(() => {
    if (setupsLoading || setups.length === 0) return;
    const current = setupIdParam != null ? Number(setupIdParam) : null;
    if (current === selectedPolicyId) return;
    setSearchParams(
      selectedPolicyId != null ? { setupId: String(selectedPolicyId) } : {},
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setupsLoading, setups, setupIdParam, selectedPolicyId]);

  function selectPolicy(id: number) {
    setSearchParams({ setupId: String(id) }, { replace: true });
  }

  // ── Create policy modal ──────────────────────────────────────────────────
  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState<CreateForm>(EMPTY_CREATE_FORM);
  const [createError, setCreateError] = useState<ApiErrorInfo | null>(null);
  const [mutating, setMutating] = useState(false);

  function openCreateModal() {
    setCreateForm(EMPTY_CREATE_FORM);
    setCreateError(null);
    setCreateOpen(true);
  }

  function closeCreateModal() {
    setCreateOpen(false);
  }

  async function submitCreate() {
    const validationError = validateSetupCreate({
      setup_code: createForm.setup_code,
      setup_name: createForm.setup_name,
    });
    if (validationError) {
      setCreateError({ status: null, code: null, message: validationError });
      return;
    }
    setMutating(true);
    setCreateError(null);
    try {
      const created = await createPayrollSetup({
        setup_name: createForm.setup_name,
        description: normalizeDescription(createForm.description),
        ...(createForm.setup_code.trim() !== '' ? { setup_code: createForm.setup_code.trim() } : {}),
      });
      setCreateOpen(false);
      await loadSetups();
      setFilter('All');
      setSearch('');
      setSearchParams({ setupId: String(created.setup_id) }, { replace: true });
    } catch (err) {
      setCreateError(readApiError(err, 'Failed to create the policy.'));
    } finally {
      setMutating(false);
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  return (
    <div className={styles.page}>
      <PageHeader
        title="Payroll Policies"
        subtitle="Company payroll schedules. Branches follow a policy, and pick up its updates from their effective dates."
      />

      <div className={styles.tabs}>
        <button
          className={tab === 'policies' ? `${styles.tabBtn} ${styles.tabBtnActive}` : styles.tabBtn}
          onClick={() => setTab('policies')}
        >
          Policies
        </button>
        <button
          className={tab === 'assignments' ? `${styles.tabBtn} ${styles.tabBtnActive}` : styles.tabBtn}
          onClick={() => setTab('assignments')}
        >
          Branch Assignments
        </button>
      </div>

      {tab === 'policies' ? (
        setupsLoading ? (
          <p className={styles.mutedText}>Loading…</p>
        ) : setupsError ? (
          <PayrollErrorNotice info={setupsError} />
        ) : setups.length === 0 ? (
          <SectionCard>
            <div className={`${styles.detailEmpty} ${styles.welcomeCard}`}>
              <h2 className={styles.modalTitle}>Welcome to Payroll</h2>
              <p className={styles.defaultText}>Create your first payroll policy.</p>
              <p className={`${styles.mutedText} ${styles.welcomeBody}`}>
                A payroll policy defines how a group of branches runs payroll. You can update the
                policy over time, and branches following it will use those updates from their
                effective dates.
              </p>
              {canManage ? (
                <button className={styles.btnPrimary} onClick={openCreateModal}>
                  Create payroll policy
                </button>
              ) : (
                <p className={styles.mutedNote}>An administrator needs to create a payroll policy.</p>
              )}
            </div>
          </SectionCard>
        ) : (
          // ── Master-detail shell (search/filter + pinned add-policy footer on the
          // master pane; policy header + details in the detail pane) ────────
          <div className={styles.setupsBody}>
            {/* MASTER — all policies, active and archived, searchable/filterable */}
            <div className={styles.setupsListCard}>
              <div className={styles.setupsControls}>
                <input
                  type="search"
                  className={styles.policySearchInput}
                  placeholder="Search policies…"
                  aria-label="Search policies"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                <div className={styles.policyFilterRow}>
                  {(['All', 'Active', 'Inactive', 'Archived'] as const).map((f) => (
                    <button
                      key={f}
                      type="button"
                      className={
                        filter === f
                          ? `${styles.policyFilterBtn} ${styles.policyFilterBtnActive}`
                          : styles.policyFilterBtn
                      }
                      aria-pressed={filter === f}
                      onClick={() => setFilter(f)}
                    >
                      {f}
                    </button>
                  ))}
                </div>
              </div>

              <div className={styles.setupListScroll}>
                {summariesLoading ? (
                  <p className={`${styles.mutedText} ${styles.setupListMessage}`}>Loading…</p>
                ) : summariesError ? (
                  <PayrollErrorNotice info={summariesError} />
                ) : visible.length === 0 ? (
                  <p className={`${styles.mutedText} ${styles.setupListMessage}`}>
                    No policies match.{search.trim() !== '' ? ' Try a different search.' : ''}
                  </p>
                ) : (
                  <ul className={styles.setupList}>
                    {visible.map((s) => {
                      const isSelected = s.setup_id === selectedPolicyId;
                      const rowUsage = usage.get(s.setup_id);
                      const state = rowUsage?.state ?? 'Inactive';
                      return (
                        <li key={s.setup_id}>
                          <button
                            className={isSelected ? `${styles.setupRow} ${styles.setupRowActive}` : styles.setupRow}
                            onClick={() => selectPolicy(s.setup_id)}
                            aria-current={isSelected ? 'true' : undefined}
                          >
                            <span className={styles.setupRowMain}>
                              <span className={styles.setupRowName}>{s.setup_name}</span>
                              {state === 'Inactive' && rowUsage?.futureStart != null && (
                                <span className={styles.setupRowSecondary}>
                                  {futureStartLabel(rowUsage.futureStart)}
                                </span>
                              )}
                            </span>
                            <span className={styles.setupRowBadges}>
                              <span
                                className={
                                  state === 'Active'
                                    ? `${styles.badge} ${styles.badgeGreen}`
                                    : state === 'Archived'
                                      ? `${styles.badge} ${styles.badgeMuted}`
                                      : `${styles.badge} ${styles.badgeGray}`
                                }
                              >
                                {state}
                              </span>
                              {s.setup_id === defaultSetupId && (
                                <span className={`${styles.badge} ${styles.badgeBlue}`}>Default</span>
                              )}
                            </span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>

              {canManage && (
                <div className={styles.setupsPaneFooter}>
                  <button className={styles.addPolicyBtn} onClick={openCreateModal} disabled={mutating}>
                    + Add policy
                  </button>
                </div>
              )}
            </div>

            {/* DETAIL — Step 3A: header + policy details only (lower sections stay parked) */}
            <section
              className={styles.setupsDetailPane}
              aria-label="Policy details"
              data-testid="policy-detail-panel"
            >
              {selectedPolicy ? (
                <div className={styles.detailPaneInner}>
                  <PolicyDetailPanel
                    selectedPolicy={selectedPolicy}
                    usageState={usage.get(selectedPolicy.setup_id)?.state ?? 'Inactive'}
                    defaultSetupId={defaultSetupId}
                    summaries={summaries}
                    summariesLoading={summariesLoading}
                    summariesError={summariesError}
                    canManage={canManage}
                    canAssign={canAssign}
                    canPublish={canPublish}
                    mutating={mutating}
                    setMutating={setMutating}
                    onReloadSetups={loadSetups}
                    onGoToAssignments={() => setTab('assignments')}
                    showToast={showToast}
                  />
                </div>
              ) : (
                <div className={styles.detailPaneEmpty}>
                  <p className={styles.detailPaneEmptyTitle}>Select a payroll policy</p>
                  <p className={styles.detailPaneEmptyText}>Choose a policy on the left to view its details.</p>
                </div>
              )}
            </section>
          </div>
        )
      ) : (
        <SectionCard
          title="Branch Assignments"
          subtitle="The server decides which policy governs each payroll period."
        >
          <BranchAssignmentsTab
            summaries={summaries}
            summariesLoading={summariesLoading}
            summariesError={summariesError}
            activeSetups={setups.filter((s) => s.status === 'Active')}
            canAssign={canAssign}
            mutating={mutating}
            setMutating={setMutating}
            onReload={() => setSummariesReloadKey((k) => k + 1)}
            showToast={showToast}
          />
        </SectionCard>
      )}

      {/* ── Create policy modal ── */}
      {createOpen && (
        <div
          className={styles.modalOverlay}
          onClick={(e) => {
            if (e.target === e.currentTarget) closeCreateModal();
          }}
        >
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>Create payroll policy</h2>
              <button className={styles.modalCloseBtn} onClick={closeCreateModal}>
                ×
              </button>
            </div>
            <div className={styles.modalBody}>
              <div className={styles.formGroup}>
                <label className={styles.label}>Policy name</label>
                <input
                  className={styles.input}
                  value={createForm.setup_name}
                  maxLength={200}
                  onChange={(e) => setCreateForm((f) => ({ ...f, setup_name: e.target.value }))}
                  disabled={mutating}
                  autoFocus
                />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label}>Description</label>
                <textarea
                  className={styles.textarea}
                  value={createForm.description}
                  onChange={(e) => setCreateForm((f) => ({ ...f, description: e.target.value }))}
                  disabled={mutating}
                />
              </div>
              <details className={styles.formGroup}>
                <summary className={styles.label}>Advanced</summary>
                <div className={`${styles.formGroup} ${styles.sectionSpacerSmall}`}>
                  <label className={styles.label}>Integration code (optional)</label>
                  <p className={styles.fieldHint}>
                    Leave blank and Flussra creates a stable reference automatically.
                  </p>
                  <input
                    className={styles.input}
                    value={createForm.setup_code}
                    maxLength={50}
                    onChange={(e) => setCreateForm((f) => ({ ...f, setup_code: e.target.value }))}
                    disabled={mutating}
                  />
                </div>
              </details>
              {createError && <PayrollErrorNotice info={createError} />}
            </div>
            <div className={styles.modalFooter}>
              <button className={styles.btnPrimary} onClick={submitCreate} disabled={mutating}>
                {mutating ? 'Working…' : 'Create payroll policy'}
              </button>
              <button className={styles.btnSecondary} onClick={closeCreateModal} disabled={mutating}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
