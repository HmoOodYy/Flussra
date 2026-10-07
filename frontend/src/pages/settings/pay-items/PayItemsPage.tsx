import { useCallback, useEffect, useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import apiClient from '../../../lib/apiClient';
import { useAuth } from '../../../store/authStore';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import {
  bulkUpdateBranchPayDefinition,
  copyPayDefinitionRequest,
  createPayDefinition,
  createPayDefinitionRequest,
  decidePayDefinitionRequest,
  listBranchPayDefinitionHistory,
  listBranchPayDefinitions,
  listPayDefinitionRequestEvents,
  listPayDefinitionRequests,
  listPayDefinitions,
  retirePayDefinition,
  submitPayDefinitionRequest,
  updateBranchPayDefinition,
  updatePayDefinitionRequest,
} from '../../../lib/compensationApi';
import {
  canConfigureAllBranchPayDefinitions,
  canConfigureBranchPayDefinitions,
  canCreatePayDefinitionDirectly,
  canGovernPayDefinitionsCompanyWide,
  canManagePayDefinitionsForBranch,
} from '../../../lib/permissions';
import { saveAndSubmitDraft, submitExistingDraft } from './requestDraftWorkflow';
import type { BranchAdmin } from '../../../types/settings';
import type {
  BranchConfigUpdatePayload,
  BranchConfigVersion,
  BranchPayDefinitionState,
  BulkBranchConfigResult,
  PayDefinitionDecisionAction,
  PayDefinitionInputType,
  PayDefinitionRequestEvent,
  PayDefinitionRequestStatus,
  PayDefinitionRequestSummary,
  PayDefinitionSummary,
} from '../../../types/compensation';
import styles from './PayItemsPage.module.css';

// ─── Constants & helpers ──────────────────────────────────────────────────────

const INPUT_TYPE_LABELS: Record<PayDefinitionInputType, string> = {
  Decimal: 'Decimal quantity',
  WholeNumber: 'Whole number',
};

const METHOD_LABELS: Record<string, string> = {
  PerUnit: 'Per unit',
  OrdinalTier: 'Ordinal tier (not yet available)',
};

type RequestFilter = PayDefinitionRequestStatus | 'all';

function apiError(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    const { message, branch_errors: branchErrors } = detail as {
      message?: string; branch_errors?: Array<{ branch_name: string; error: string }>;
    };
    if (branchErrors?.length) {
      return `${message ?? 'Validation failed.'} ${branchErrors.map(e => `${e.branch_name}: ${e.error}`).join('; ')}`;
    }
    if (message) return message;
  }
  return err instanceof Error ? err.message : 'Request failed.';
}

function fmtDate(value: string | null): string {
  if (!value) return '—';
  return new Date(value).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function todayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

type Coverage = 'all-active' | 'all-inactive' | 'mixed';

interface AggregateDefinition {
  pay_definition_id: number;
  definition_code: string;
  definition_name: string;
  input_type: PayDefinitionInputType;
  unit: string | null;
  calculation_method: string;
  activeBranches: number;
  totalBranches: number;
  coverage: Coverage;
}

function buildAggregates(
  byBranch: Record<number, BranchPayDefinitionState[]>,
): AggregateDefinition[] {
  const map = new Map<number, AggregateDefinition>();
  const branchCount = Object.keys(byBranch).length;
  for (const items of Object.values(byBranch)) {
    for (const item of items) {
      const existing = map.get(item.pay_definition_id) ?? {
        pay_definition_id: item.pay_definition_id,
        definition_code: item.definition_code,
        definition_name: item.definition_name,
        input_type: item.input_type,
        unit: item.unit,
        calculation_method: item.calculation_method,
        activeBranches: 0,
        totalBranches: branchCount,
        coverage: 'all-inactive' as Coverage,
      };
      if (item.is_active) existing.activeBranches += 1;
      map.set(item.pay_definition_id, existing);
    }
  }
  return [...map.values()].map(agg => ({
    ...agg,
    coverage: agg.activeBranches === 0 ? 'all-inactive'
      : agg.activeBranches === agg.totalBranches ? 'all-active' : 'mixed',
  }));
}

// ─── Main component ───────────────────────────────────────────────────────────

export function PayItemsPage() {
  const { user } = useAuth();
  const canAllBranches = canConfigureAllBranchPayDefinitions(user);
  const canDirectCreate = canCreatePayDefinitionDirectly(user);
  const canGovern = canGovernPayDefinitionsCompanyWide(user);

  const [branches, setBranches] = useState<BranchAdmin[]>([]);
  const [branchesError, setBranchesError] = useState('');
  const [branchMode, setBranchMode] = useState<'single' | 'all'>('single');
  const [selectedBranchId, setSelectedBranchId] = useState<number | null>(null);
  const [pageTab, setPageTab] = useState<'items' | 'requests'>('items');

  const [items, setItems] = useState<BranchPayDefinitionState[]>([]);
  const [byBranch, setByBranch] = useState<Record<number, BranchPayDefinitionState[]>>({});
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [reloadKey, setReloadKey] = useState(0);

  const [companyDefinitions, setCompanyDefinitions] = useState<Record<number, PayDefinitionSummary>>({});

  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'inactive' | 'mixed'>('all');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [history, setHistory] = useState<BranchConfigVersion[] | null>(null);
  const [toast, setToast] = useState('');

  const showToast = useCallback((message: string) => {
    setToast(message);
    setTimeout(() => setToast(''), 4000);
  }, []);

  // Edit-configuration modal
  const [editOpen, setEditOpen] = useState(false);
  const [editActive, setEditActive] = useState(true);
  const [editNotes, setEditNotes] = useState('');
  const [editUseDate, setEditUseDate] = useState(false);
  const [editDate, setEditDate] = useState(todayISO());
  const [editTarget, setEditTarget] = useState<'single' | 'all'>('single');
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState('');
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false);
  const [bulkResult, setBulkResult] = useState<BulkBranchConfigResult | null>(null);

  // Create modal
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState('');
  const [createCode, setCreateCode] = useState('');
  const [createInputType, setCreateInputType] = useState<PayDefinitionInputType>('Decimal');
  const [createUnit, setCreateUnit] = useState('');
  const [createNotes, setCreateNotes] = useState('');
  const [createSaving, setCreateSaving] = useState(false);
  const [createError, setCreateError] = useState('');

  // Retire
  const [retireOpen, setRetireOpen] = useState(false);
  const [retireWorking, setRetireWorking] = useState(false);

  // Requests
  const [requests, setRequests] = useState<PayDefinitionRequestSummary[]>([]);
  const [requestsLoading, setRequestsLoading] = useState(false);
  const [requestsError, setRequestsError] = useState('');
  const [requestFilter, setRequestFilter] = useState<RequestFilter>('PendingCompanyApproval');
  const [requestKey, setRequestKey] = useState(0);
  const [expandedRequestId, setExpandedRequestId] = useState<string | null>(null);
  const [requestEvents, setRequestEvents] = useState<PayDefinitionRequestEvent[]>([]);

  const [decideTarget, setDecideTarget] = useState<PayDefinitionRequestSummary | null>(null);
  const [decideAction, setDecideAction] = useState<PayDefinitionDecisionAction | null>(null);
  const [decideReason, setDecideReason] = useState('');
  const [decideSaving, setDecideSaving] = useState(false);
  const [decideError, setDecideError] = useState('');

  const [draftTarget, setDraftTarget] = useState<PayDefinitionRequestSummary | null>(null);
  const [draftName, setDraftName] = useState('');
  const [draftInputType, setDraftInputType] = useState<PayDefinitionInputType>('Decimal');
  const [draftUnit, setDraftUnit] = useState('');
  const [draftNotes, setDraftNotes] = useState('');
  const [draftSaving, setDraftSaving] = useState(false);
  const [draftError, setDraftError] = useState('');

  const branchEditable = selectedBranchId !== null && canConfigureBranchPayDefinitions(user, selectedBranchId);
  const canRequest = !canDirectCreate && selectedBranchId !== null
    && canManagePayDefinitionsForBranch(user, selectedBranchId);
  const showAddButton = canDirectCreate || canRequest;
  const showRequestsTab = canGovern || (selectedBranchId !== null
    && canManagePayDefinitionsForBranch(user, selectedBranchId));
  const activeBranches = branches.filter(b => b.status === 'Active');

  // ── Load branches ──────────────────────────────────────────────────────────
  useEffect(() => {
    apiClient.get<BranchAdmin[]>('/settings/branches')
      .then(({ data }) => {
        setBranches(data);
        setSelectedBranchId(current => current ?? (data.find(b => b.is_default) ?? data[0])?.branch_id ?? null);
      })
      .catch(e => setBranchesError(apiError(e)));
  }, []);

  // ── Load Company definitions (provenance, retirement) when permitted ───────
  useEffect(() => {
    if (!canGovern) return;
    listPayDefinitions()
      .then(list => setCompanyDefinitions(Object.fromEntries(list.map(d => [d.pay_definition_id, d]))))
      .catch(() => setCompanyDefinitions({}));
  }, [canGovern, reloadKey]);

  // ── Load applicability ─────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setLoadError('');
      setSelectedId(null);
      setHistory(null);
      try {
        if (branchMode === 'single') {
          if (selectedBranchId === null) return;
          const data = await listBranchPayDefinitions(selectedBranchId);
          if (!cancelled) setItems(data);
        } else {
          const results = await Promise.allSettled(
            activeBranches.map(b => listBranchPayDefinitions(b.branch_id)
              .then(data => ({ branchId: b.branch_id, data }))));
          if (cancelled) return;
          const next: Record<number, BranchPayDefinitionState[]> = {};
          let failed = 0;
          for (const r of results) {
            if (r.status === 'fulfilled') next[r.value.branchId] = r.value.data; else failed += 1;
          }
          if (Object.keys(next).length === 0 && failed > 0) {
            setLoadError('Failed to load pay items for all branches.');
          } else if (failed > 0) {
            setLoadError(`${failed} branch(es) could not be loaded; showing the rest.`);
          }
          setByBranch(next);
        }
      } catch (e) {
        if (!cancelled) setLoadError(apiError(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [branchMode, selectedBranchId, branches, reloadKey]);

  // ── Load requests ──────────────────────────────────────────────────────────
  const loadRequests = useCallback(async () => {
    setRequestsLoading(true);
    setRequestsError('');
    try {
      setRequests(await listPayDefinitionRequests({
        ...(requestFilter !== 'all' ? { status: requestFilter } : {}),
        ...(!canGovern && selectedBranchId ? { branch_id: selectedBranchId } : {}),
      }));
    } catch (e) {
      setRequestsError(apiError(e));
    } finally {
      setRequestsLoading(false);
    }
  }, [requestFilter, canGovern, selectedBranchId]);

  useEffect(() => {
    if (!showRequestsTab || pageTab !== 'requests') return;
    void (async () => { await loadRequests(); })();
  }, [pageTab, requestKey, showRequestsTab, loadRequests]);

  const aggregates = useMemo(
    () => (branchMode === 'all' ? buildAggregates(byBranch) : []), [branchMode, byBranch]);

  const q = search.trim().toLowerCase();
  const visibleItems = useMemo(() => items.filter(item => {
    if (statusFilter === 'active' && !item.is_active) return false;
    if (statusFilter === 'inactive' && item.is_active) return false;
    return !q || item.definition_name.toLowerCase().includes(q)
      || item.definition_code.toLowerCase().includes(q);
  }), [items, q, statusFilter]);
  const visibleAggregates = useMemo(() => aggregates.filter(agg => {
    if (statusFilter === 'active' && agg.coverage !== 'all-active') return false;
    if (statusFilter === 'inactive' && agg.coverage !== 'all-inactive') return false;
    if (statusFilter === 'mixed' && agg.coverage !== 'mixed') return false;
    return !q || agg.definition_name.toLowerCase().includes(q)
      || agg.definition_code.toLowerCase().includes(q);
  }), [aggregates, q, statusFilter]);

  const selectedItem = branchMode === 'single'
    ? items.find(i => i.pay_definition_id === selectedId) ?? null : null;
  const selectedAgg = branchMode === 'all'
    ? aggregates.find(a => a.pay_definition_id === selectedId) ?? null : null;
  const selectedCompany = selectedId !== null ? companyDefinitions[selectedId] : undefined;

  // ── Actions ────────────────────────────────────────────────────────────────
  function openEdit() {
    if (selectedItem) {
      setEditActive(selectedItem.is_active);
      setEditNotes(selectedItem.notes ?? '');
    } else if (selectedAgg) {
      setEditActive(selectedAgg.coverage === 'all-active');
      setEditNotes('');
    }
    setEditUseDate(false);
    setEditDate(todayISO());
    setEditTarget(branchMode === 'all' ? 'all' : 'single');
    setEditError('');
    setEditOpen(true);
  }

  async function executeSave() {
    if (selectedId === null) return;
    const payload: BranchConfigUpdatePayload = {
      is_active: editActive,
      notes: editNotes.trim() || null,
      effective_from: editUseDate ? (editDate || null) : null,
    };
    setEditSaving(true);
    setEditError('');
    try {
      if (editTarget === 'single') {
        if (selectedBranchId === null) return;
        await updateBranchPayDefinition(selectedBranchId, selectedId, payload);
        setEditOpen(false);
        setReloadKey(k => k + 1);
        showToast('Configuration saved.');
      } else {
        const result = await bulkUpdateBranchPayDefinition(selectedId, {
          target: 'AllBranches', branch_ids: null, ...payload,
        });
        setEditOpen(false);
        setBulkResult(result);
        setReloadKey(k => k + 1);
      }
    } catch (e) {
      setEditError(apiError(e));
    } finally {
      setEditSaving(false);
    }
  }

  async function loadHistory() {
    if (selectedId === null || selectedBranchId === null) return;
    try {
      setHistory(await listBranchPayDefinitionHistory(selectedBranchId, selectedId));
    } catch {
      setHistory([]);
    }
  }

  async function saveCreate(e: FormEvent) {
    e.preventDefault();
    if (!createName.trim()) {
      setCreateError('Pay item name is required.');
      return;
    }
    setCreateSaving(true);
    setCreateError('');
    const fields = {
      definition_code: createCode.trim() || null,
      definition_name: createName.trim(),
      input_type: createInputType,
      unit: createUnit.trim() || null,
      calculation_method: 'PerUnit' as const,
    };
    try {
      if (canDirectCreate) {
        await createPayDefinition(fields);
        showToast('Pay item created for the company. Activate it per branch to use it.');
        setReloadKey(k => k + 1);
      } else if (selectedBranchId !== null) {
        const draft = await createPayDefinitionRequest({
          requesting_branch_id: selectedBranchId, ...fields, notes: createNotes.trim() || null,
        });
        const result = await submitExistingDraft(
          { updatePayDefinitionRequest, submitPayDefinitionRequest }, draft);
        if (result.kind === 'submitted') {
          showToast('Request submitted for company approval.');
        } else {
          setPageTab('requests');
          setRequestFilter('Draft');
          showToast(`Draft saved but could not be submitted: ${apiError(result.error)}`);
        }
        setRequestKey(k => k + 1);
      } else {
        setCreateError('Select a branch before submitting a request.');
        return;
      }
      setCreateOpen(false);
      setCreateName(''); setCreateCode(''); setCreateUnit(''); setCreateNotes('');
      setCreateInputType('Decimal');
    } catch (err) {
      setCreateError(apiError(err));
    } finally {
      setCreateSaving(false);
    }
  }

  async function confirmRetire() {
    if (selectedId === null) return;
    setRetireWorking(true);
    try {
      const result = await retirePayDefinition(selectedId);
      setRetireOpen(false);
      setSelectedId(null);
      setReloadKey(k => k + 1);
      showToast(`"${result.definition_name}" retired.`);
    } catch (e) {
      showToast(apiError(e));
    } finally {
      setRetireWorking(false);
    }
  }

  function openDecide(request: PayDefinitionRequestSummary, action: PayDefinitionDecisionAction) {
    setDecideTarget(request);
    setDecideAction(action);
    setDecideReason('');
    setDecideError('');
  }

  async function executeDecide() {
    if (!decideTarget || !decideAction) return;
    if (!decideReason.trim()) {
      setDecideError('A reason is required before proceeding.');
      return;
    }
    setDecideSaving(true);
    setDecideError('');
    try {
      await decidePayDefinitionRequest(decideTarget.request_id, {
        action: decideAction, expected_revision: decideTarget.revision, reason: decideReason.trim(),
      });
      showToast(decideAction === 'Approve' ? 'Request approved.'
        : decideAction === 'Reject' ? 'Request rejected.' : 'Request returned to draft.');
      setDecideTarget(null);
      setRequestKey(k => k + 1);
      if (decideAction === 'Approve') setReloadKey(k => k + 1);
    } catch (e) {
      setDecideError(apiError(e));
    } finally {
      setDecideSaving(false);
    }
  }

  function openDraftEdit(request: PayDefinitionRequestSummary) {
    setDraftTarget(request);
    setDraftName(request.definition_name ?? '');
    setDraftInputType(request.input_type ?? 'Decimal');
    setDraftUnit(request.unit ?? '');
    setDraftNotes(request.notes ?? '');
    setDraftError('');
  }

  async function executeDraftEdit() {
    if (!draftTarget) return;
    if (!draftName.trim()) {
      setDraftError('Pay item name is required.');
      return;
    }
    setDraftSaving(true);
    setDraftError('');
    try {
      const result = await saveAndSubmitDraft(
        { updatePayDefinitionRequest, submitPayDefinitionRequest }, draftTarget,
        { definition_name: draftName.trim(), input_type: draftInputType,
          unit: draftUnit.trim(), notes: draftNotes.trim() });
      setDraftTarget(null);
      setRequestKey(k => k + 1);
      showToast(result.kind === 'submitted'
        ? 'Request submitted for company approval.'
        : `Draft saved but could not be submitted: ${apiError(result.error)}`);
    } catch (e) {
      setDraftError(apiError(e));
    } finally {
      setDraftSaving(false);
    }
  }

  async function copyRejected(request: PayDefinitionRequestSummary) {
    try {
      await copyPayDefinitionRequest(request.request_id);
      setRequestFilter('Draft');
      setRequestKey(k => k + 1);
      showToast('A new draft was created from the rejected request.');
    } catch (e) {
      showToast(apiError(e));
    }
  }

  async function toggleRequestHistory(request: PayDefinitionRequestSummary) {
    if (expandedRequestId === request.request_id) {
      setExpandedRequestId(null);
      return;
    }
    setExpandedRequestId(request.request_id);
    try {
      setRequestEvents(await listPayDefinitionRequestEvents(request.request_id));
    } catch {
      setRequestEvents([]);
    }
  }

  const branchName = (id: number) => branches.find(b => b.branch_id === id)?.branch_name ?? `Branch ${id}`;

  // ─────────────────────────────────────────────────────────────────────────
  return (
    <div className={styles.page}>
      {toast && <div className={styles.successAlert}>{toast}</div>}

      <div className={styles.branchBar}>
        <span className={styles.branchBarLabel}>View</span>
        {canAllBranches ? (
          <div className={styles.modeGroup}>
            {(['single', 'all'] as const).map(m => (
              <button key={m} type="button"
                className={`${styles.modeBtn}${branchMode === m ? ` ${styles.modeBtnActive}` : ''}`}
                onClick={() => { setBranchMode(m); setSearch(''); setStatusFilter('all'); }}>
                {m === 'single' ? 'Single Branch' : 'All Branches'}
              </button>
            ))}
          </div>
        ) : (
          <span className={styles.branchBarLabel}>Single Branch</span>
        )}
        <div className={styles.branchSep} />
        {branchMode === 'single' ? (
          <select className={styles.input} style={{ width: 'auto' }}
            value={selectedBranchId ?? ''} aria-label="Branch"
            onChange={e => setSelectedBranchId(Number(e.target.value))}>
            {branches.map(b => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
          </select>
        ) : (
          <span className={styles.branchTag}>All {activeBranches.length} active branches</span>
        )}
        <div style={{ flex: 1 }} />
        <p className={styles.pageSubtitle} style={{ margin: 0 }}>
          Pay items belong to the company; set which branches can use each one. Changes take effect from a chosen date.
        </p>
        <div className={styles.pageActions}>
          {!showAddButton && <span className={styles.readonlyNote}>Read-only</span>}
          {showAddButton && (
            <button type="button" className={styles.btnPrimary}
              onClick={() => { setCreateError(''); setCreateOpen(true); }}>
              + Add Pay Item
            </button>
          )}
        </div>
      </div>

      {branchesError && <div className={styles.errorAlert}>{branchesError}</div>}

      {showRequestsTab && (
        <div className={styles.pageTabs} role="tablist">
          {(['items', 'requests'] as const).map(tab => (
            <button key={tab} role="tab" type="button" aria-selected={pageTab === tab}
              className={`${styles.pageTabBtn}${pageTab === tab ? ` ${styles.pageTabBtnActive}` : ''}`}
              onClick={() => setPageTab(tab)}>
              {tab === 'items' ? 'Pay Items' : 'Requests'}
            </button>
          ))}
        </div>
      )}

      {(!showRequestsTab || pageTab === 'items') && (
        <div className={styles.body}>
          <div className={styles.listCard}>
            <div className={styles.listToolbar}>
              <div className={styles.statusFilters}>
                {(['all', 'active', 'inactive', ...(branchMode === 'all' ? ['mixed' as const] : [])] as const).map(f => (
                  <button key={f} type="button"
                    className={`${styles.filterPill}${statusFilter === f ? ` ${styles.filterPillActive}` : ''}`}
                    onClick={() => setStatusFilter(f)}>
                    {f.charAt(0).toUpperCase() + f.slice(1)}
                  </button>
                ))}
              </div>
              <div className={styles.searchWrap}>
                <input className={styles.searchInput} type="search" placeholder="Search pay items…"
                  value={search} onChange={e => setSearch(e.target.value)} />
              </div>
            </div>

            <div className={styles.tableWrap}>
              {loadError && <div className={styles.errorAlert} style={{ margin: '1rem' }}>{loadError}</div>}
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>Pay Item</th>
                    <th>Code</th>
                    <th className={styles.tdCenter}>Input</th>
                    <th className={styles.tdCenter}>{branchMode === 'all' ? 'Coverage' : 'Status'}</th>
                  </tr>
                </thead>
                <tbody>
                  {loading && [...Array(4)].map((_, i) => (
                    <tr key={i} className={styles.skeletonRow}>
                      {[...Array(4)].map((__, j) => <td key={j}><div className={styles.skeleton} /></td>)}
                    </tr>
                  ))}
                  {!loading && branchMode === 'single' && visibleItems.map(item => (
                    <tr key={item.pay_definition_id}
                      className={`${styles.tableRow}${selectedId === item.pay_definition_id ? ` ${styles.tableRowSelected}` : ''}${!item.is_active ? ` ${styles.tableRowInactive}` : ''}`}
                      onClick={() => { setSelectedId(item.pay_definition_id); setHistory(null); }}>
                      <td><span className={styles.itemName}>{item.definition_name}</span></td>
                      <td className={styles.rateCell}>{item.definition_code}</td>
                      <td className={styles.tdCenter}>{INPUT_TYPE_LABELS[item.input_type]}</td>
                      <td className={styles.tdCenter}>
                        <span className={item.is_active ? styles.badgeActive : styles.badgeInactive}>
                          {item.is_active ? 'Active' : item.is_configured ? 'Inactive' : 'Not configured'}
                        </span>
                      </td>
                    </tr>
                  ))}
                  {!loading && branchMode === 'all' && visibleAggregates.map(agg => (
                    <tr key={agg.pay_definition_id}
                      className={`${styles.tableRow}${selectedId === agg.pay_definition_id ? ` ${styles.tableRowSelected}` : ''}`}
                      onClick={() => setSelectedId(agg.pay_definition_id)}>
                      <td><span className={styles.itemName}>{agg.definition_name}</span></td>
                      <td className={styles.rateCell}>{agg.definition_code}</td>
                      <td className={styles.tdCenter}>{INPUT_TYPE_LABELS[agg.input_type]}</td>
                      <td className={styles.tdCenter}>
                        <span className={agg.coverage === 'all-active' ? styles.badgeActive
                          : agg.coverage === 'mixed' ? styles.badgeMixed : styles.badgeInactive}>
                          {agg.activeBranches}/{agg.totalBranches} branches
                        </span>
                      </td>
                    </tr>
                  ))}
                  {!loading && !loadError && (branchMode === 'single' ? visibleItems : visibleAggregates).length === 0 && (
                    <tr className={styles.emptyRow}>
                      <td colSpan={4}>
                        {search ? `No pay items match "${search}".` : 'No pay items yet. A company can have none.'}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className={styles.detailPanel}>
            {!selectedItem && !selectedAgg ? (
              <div className={styles.detailEmpty}>
                <p className={styles.detailEmptyTitle}>Choose a pay item</p>
                <p className={styles.detailEmptyText}>
                  Select a pay item to view its branch configuration, effective dates and history.
                </p>
              </div>
            ) : (
              <>
                <div className={styles.detailHeader}>
                  <div>
                    <div className={styles.detailItemName}>{(selectedItem ?? selectedAgg)!.definition_name}</div>
                    <div className={styles.detailCode}>{(selectedItem ?? selectedAgg)!.definition_code}</div>
                  </div>
                </div>
                <div className={styles.detailBody}>
                  <div className={styles.detailSection}>
                    <div className={styles.detailRow}>
                      <span className={styles.detailLabel}>Calculation</span>
                      <span className={styles.detailValue}>
                        {METHOD_LABELS[(selectedItem ?? selectedAgg)!.calculation_method] ?? (selectedItem ?? selectedAgg)!.calculation_method}
                      </span>
                    </div>
                    <div className={styles.detailRow}>
                      <span className={styles.detailLabel}>Input</span>
                      <span className={styles.detailValue}>{INPUT_TYPE_LABELS[(selectedItem ?? selectedAgg)!.input_type]}</span>
                    </div>
                    <div className={styles.detailRow}>
                      <span className={styles.detailLabel}>Unit</span>
                      <span className={styles.detailValue}>{(selectedItem ?? selectedAgg)!.unit ?? '—'}</span>
                    </div>
                    {selectedItem && (
                      <>
                        <div className={styles.detailRow}>
                          <span className={styles.detailLabel}>This branch</span>
                          <span className={styles.detailValue}>
                            {selectedItem.is_active ? 'Active' : selectedItem.is_configured ? 'Inactive' : 'Not configured'}
                          </span>
                        </div>
                        {selectedItem.current_config && (
                          <div className={styles.detailRow}>
                            <span className={styles.detailLabel}>Effective since</span>
                            <span className={styles.detailValue}>{selectedItem.current_config.effective_from}</span>
                          </div>
                        )}
                        {selectedItem.pending_config && (
                          <div className={styles.configBannerPending}>
                            Scheduled: {selectedItem.pending_config.is_active ? 'active' : 'inactive'} from {selectedItem.pending_config.effective_from}
                          </div>
                        )}
                        {selectedItem.notes && (
                          <div className={styles.detailRow}>
                            <span className={styles.detailLabel}>Notes</span>
                            <span className={styles.detailValue}>{selectedItem.notes}</span>
                          </div>
                        )}
                      </>
                    )}
                    {selectedAgg && (
                      <div className={styles.detailRow}>
                        <span className={styles.detailLabel}>Active in</span>
                        <span className={styles.detailValue}>{selectedAgg.activeBranches} of {selectedAgg.totalBranches} branches</span>
                      </div>
                    )}
                    {selectedCompany?.provenance && (
                      <div className={styles.detailRow}>
                        <span className={styles.detailLabel}>Created</span>
                        <span className={styles.detailValue}>
                          {selectedCompany.provenance.creation_mode === 'Request' ? 'From a branch request' : 'Directly by the company'}
                          {' · '}{fmtDate(selectedCompany.provenance.created_at_utc)}
                          {selectedCompany.provenance.requesting_branch_id !== null
                            && ` · ${branchName(selectedCompany.provenance.requesting_branch_id)}`}
                        </span>
                      </div>
                    )}
                  </div>

                  <div className={styles.detailActions}>
                    {(selectedAgg ? canAllBranches : branchEditable) && (
                      <button type="button" className={styles.btnPrimary} onClick={openEdit}>Edit Configuration</button>
                    )}
                    {selectedItem && (
                      <button type="button" className={styles.btnSecondary} onClick={() => void loadHistory()}>History</button>
                    )}
                    {selectedCompany && selectedCompany.status !== 'Retired' && canGovern && (
                      <button type="button" className={styles.btnDanger} onClick={() => setRetireOpen(true)}>Retire</button>
                    )}
                  </div>

                  {history && (
                    <div className={styles.historySection}>
                      <div className={styles.detailSectionTitle}>Configuration history</div>
                      {history.length === 0 && <div className={styles.cdpiEmpty}>No configuration yet.</div>}
                      {history.map(version => (
                        <div key={version.config_id} className={styles.historyItem}>
                          <span>{version.is_active ? 'Active' : 'Inactive'}</span>
                          <span className={styles.historyRange}>
                            {version.effective_from} – {version.effective_to ?? 'present'}
                          </span>
                          {version.notes && <span className={styles.historyMeta}>{version.notes}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {showRequestsTab && pageTab === 'requests' && (
        <div className={styles.cdpiSection}>
          <div className={styles.cdpiSectionHeader}>
            <h3 className={styles.cdpiSectionTitle}>Pay Item Requests</h3>
            <div className={styles.cdpiFilterRow}>
              {(['PendingCompanyApproval', 'Draft', 'Approved', 'Rejected', 'all'] as const).map(f => (
                <button key={f} type="button"
                  className={`${styles.filterPill}${requestFilter === f ? ` ${styles.filterPillActive}` : ''}`}
                  onClick={() => setRequestFilter(f)}>
                  {f === 'all' ? 'All' : f === 'PendingCompanyApproval' ? 'Pending' : f}
                </button>
              ))}
            </div>
          </div>
          {requestsLoading && <div className={styles.cdpiEmpty}>Loading requests…</div>}
          {requestsError && <div className={styles.errorAlert}>{requestsError}</div>}
          {!requestsLoading && !requestsError && requests.length === 0 && (
            <div className={styles.cdpiEmpty}>No requests found.</div>
          )}
          <div className={styles.cdpiRequestList}>
            {requests.map(req => (
              <div key={req.request_id} className={styles.cdpiRequestCard}>
                <div className={styles.cdpiCardRow}>
                  <span className={styles.cdpiStatusBadge}>
                    {req.status === 'PendingCompanyApproval' ? 'Pending' : req.status}
                  </span>
                  <span className={styles.cdpiCardName}>{req.definition_name ?? '(unnamed)'}</span>
                  <span className={styles.cdpiCardMeta}>
                    {branchName(req.requesting_branch_id)}
                    {' · '}{req.input_type ? INPUT_TYPE_LABELS[req.input_type] : '—'}
                    {req.unit ? ` · ${req.unit}` : ''}
                  </span>
                </div>
                {req.notes && <div className={styles.cdpiCardNotes}>{req.notes}</div>}
                <div className={styles.cdpiCardDates}>
                  rev {req.revision}
                  {req.submitted_at_utc && ` · submitted ${fmtDate(req.submitted_at_utc)}`}
                  {req.copied_from_request_id && ' · copied from a rejected request'}
                </div>
                <div className={styles.cdpiCardActions}>
                  {canGovern && req.status === 'PendingCompanyApproval' && (
                    <>
                      <button type="button" className={styles.btnPrimary} onClick={() => openDecide(req, 'Approve')}>Approve</button>
                      <button type="button" className={styles.btnSecondary} onClick={() => openDecide(req, 'ReturnToDraft')}>Return to Draft</button>
                      <button type="button" className={styles.btnDanger} onClick={() => openDecide(req, 'Reject')}>Reject</button>
                    </>
                  )}
                  {req.status === 'Draft' && canManagePayDefinitionsForBranch(user, req.requesting_branch_id) && (
                    <button type="button" className={styles.btnPrimary} onClick={() => openDraftEdit(req)}>Edit and submit</button>
                  )}
                  {req.status === 'Rejected' && canManagePayDefinitionsForBranch(user, req.requesting_branch_id) && (
                    <button type="button" className={styles.btnSecondary} onClick={() => void copyRejected(req)}>Copy to new draft</button>
                  )}
                  <button type="button" className={styles.btnGhost} onClick={() => void toggleRequestHistory(req)}>
                    {expandedRequestId === req.request_id ? 'Hide history' : 'History'}
                  </button>
                </div>
                {expandedRequestId === req.request_id && (
                  <div className={styles.historySection}>
                    {requestEvents.map(event => (
                      <div key={event.event_id} className={styles.historyItem}>
                        <span>{event.event_type}</span>
                        <span className={styles.historyRange}>{fmtDate(event.occurred_at_utc)}</span>
                        {event.reason && <span className={styles.historyMeta}>{event.reason}</span>}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Decide modal */}
      {decideTarget && decideAction && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget && !decideSaving) setDecideTarget(null); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>
                {decideAction === 'Approve' ? 'Approve Request' : decideAction === 'Reject' ? 'Reject Request' : 'Return to Draft'}
              </h2>
            </div>
            <div className={styles.modalBody}>
              <div className={styles.infoBanner}>
                <strong>{decideTarget.definition_name ?? '(unnamed)'}</strong> from {branchName(decideTarget.requesting_branch_id)}
              </div>
              <div className={styles.formGroup} style={{ marginTop: '0.75rem' }}>
                <label className={styles.label} htmlFor="decide-reason">Reason <span className={styles.required}>*</span></label>
                <textarea id="decide-reason" className={styles.textarea} maxLength={500}
                  value={decideReason} onChange={e => setDecideReason(e.target.value)} disabled={decideSaving} />
              </div>
              {decideError && <div className={styles.errorAlert}>{decideError}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button type="button" className={decideAction === 'Reject' ? styles.btnDanger : styles.btnPrimary}
                disabled={decideSaving || !decideReason.trim()} onClick={() => void executeDecide()}>
                {decideSaving ? 'Working…' : decideAction === 'Approve' ? 'Approve' : decideAction === 'Reject' ? 'Reject' : 'Return to Draft'}
              </button>
              <button type="button" className={styles.btnSecondary} disabled={decideSaving} onClick={() => setDecideTarget(null)}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* Draft edit modal */}
      {draftTarget && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget && !draftSaving) setDraftTarget(null); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}><h2 className={styles.modalTitle}>Edit Draft Request</h2></div>
            <div className={styles.modalBody}>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="draft-name">Pay item name <span className={styles.required}>*</span></label>
                <input id="draft-name" className={styles.input} maxLength={200} value={draftName}
                  onChange={e => setDraftName(e.target.value)} disabled={draftSaving} />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="draft-input">Input</label>
                <select id="draft-input" className={styles.input} value={draftInputType}
                  onChange={e => setDraftInputType(e.target.value as PayDefinitionInputType)} disabled={draftSaving}>
                  <option value="Decimal">{INPUT_TYPE_LABELS.Decimal}</option>
                  <option value="WholeNumber">{INPUT_TYPE_LABELS.WholeNumber}</option>
                </select>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="draft-unit">Unit <span className={styles.optional}>(optional)</span></label>
                <input id="draft-unit" className={styles.input} maxLength={50} value={draftUnit}
                  onChange={e => setDraftUnit(e.target.value)} disabled={draftSaving} />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="draft-notes">Notes <span className={styles.optional}>(optional)</span></label>
                <textarea id="draft-notes" className={styles.textarea} maxLength={500} value={draftNotes}
                  onChange={e => setDraftNotes(e.target.value)} disabled={draftSaving} />
              </div>
              {draftError && <div className={styles.errorAlert}>{draftError}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button type="button" className={styles.btnPrimary} disabled={draftSaving || !draftName.trim()}
                onClick={() => void executeDraftEdit()}>{draftSaving ? 'Working…' : 'Save and Submit'}</button>
              <button type="button" className={styles.btnSecondary} disabled={draftSaving} onClick={() => setDraftTarget(null)}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* Edit configuration modal */}
      {editOpen && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget) setEditOpen(false); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}><h2 className={styles.modalTitle}>Edit Configuration</h2></div>
            <div className={styles.modalBody}>
              {branchMode === 'all' && (
                <div className={styles.targetSelector}>
                  <span className={styles.targetSelectorLabel}>Apply to</span>
                  <div className={styles.targetOptions}>
                    <label className={styles.targetOption}>
                      <input type="radio" name="editTarget" checked={editTarget === 'all'} onChange={() => setEditTarget('all')} />
                      All active branches
                    </label>
                    <label className={styles.targetOption}>
                      <input type="radio" name="editTarget" checked={editTarget === 'single'} onChange={() => setEditTarget('single')} />
                      <select className={styles.input} style={{ width: 'auto' }} value={selectedBranchId ?? ''}
                        onChange={e => { setEditTarget('single'); setSelectedBranchId(Number(e.target.value)); }}>
                        {branches.map(b => <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>)}
                      </select>
                    </label>
                  </div>
                </div>
              )}
              <div className={styles.toggleRow}>
                <div className={styles.toggleInfo}>
                  <span className={styles.toggleLabel}>Active</span>
                  <span className={styles.toggleDesc}>Whether this pay item is available to the branch.</span>
                </div>
                <label className={styles.switch}>
                  <input type="checkbox" checked={editActive} onChange={e => setEditActive(e.target.checked)} />
                  <span className={styles.switchTrack} />
                </label>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label}>Effective date</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.82rem' }}>
                  <input type="checkbox" checked={editUseDate} onChange={e => setEditUseDate(e.target.checked)} />
                  Pick a specific date
                </label>
                {editUseDate ? (
                  <input type="date" className={styles.input} value={editDate} min={todayISO()}
                    onChange={e => setEditDate(e.target.value)} />
                ) : (
                  <div className={styles.inputNote}>Today, or after any open payroll period.</div>
                )}
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="edit-notes">Notes</label>
                <textarea id="edit-notes" className={styles.textarea} maxLength={500} value={editNotes}
                  onChange={e => setEditNotes(e.target.value)} />
              </div>
              {editError && <div className={styles.errorAlert}>{editError}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button type="button" className={styles.btnPrimary} disabled={editSaving}
                onClick={() => (editTarget === 'all' ? setBulkConfirmOpen(true) : void executeSave())}>
                {editSaving ? 'Saving…' : editTarget === 'all' ? 'Apply to All Branches…' : 'Save Configuration'}
              </button>
              <button type="button" className={styles.btnSecondary} disabled={editSaving} onClick={() => setEditOpen(false)}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={bulkConfirmOpen}
        title="Apply to all branches?"
        message="This updates the configuration across all active branches in one atomic operation: either every branch is updated or none."
        confirmLabel="Apply to All Branches"
        variant="primary"
        loading={editSaving}
        onConfirm={() => { setBulkConfirmOpen(false); void executeSave(); }}
        onCancel={() => setBulkConfirmOpen(false)}
      />

      {/* Create modal */}
      {createOpen && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget && !createSaving) setCreateOpen(false); }}>
          <form className={styles.modal} onSubmit={saveCreate}>
            <div className={styles.modalHeader}><h2 className={styles.modalTitle}>Add Pay Item</h2></div>
            <div className={styles.modalBody}>
              <div className={styles.infoBanner}>
                {canDirectCreate
                  ? 'The pay item is created for the whole company and starts inactive in every branch.'
                  : 'Your request is sent to the company for approval. When approved, the pay item becomes active in this branch.'}
              </div>
              <div className={styles.formGroup} style={{ marginTop: '0.75rem' }}>
                <label className={styles.label} htmlFor="create-name">Name <span className={styles.required}>*</span></label>
                <input id="create-name" className={styles.input} maxLength={200} autoFocus value={createName}
                  onChange={e => setCreateName(e.target.value)} disabled={createSaving} />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="create-code">Code <span className={styles.optional}>(optional)</span></label>
                <input id="create-code" className={styles.input} maxLength={50} value={createCode}
                  onChange={e => setCreateCode(e.target.value)} disabled={createSaving} />
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="create-input">Quantity entered as</label>
                <select id="create-input" className={styles.input} value={createInputType}
                  onChange={e => setCreateInputType(e.target.value as PayDefinitionInputType)} disabled={createSaving}>
                  <option value="Decimal">{INPUT_TYPE_LABELS.Decimal}</option>
                  <option value="WholeNumber">{INPUT_TYPE_LABELS.WholeNumber}</option>
                </select>
              </div>
              <div className={styles.formGroup}>
                <label className={styles.label} htmlFor="create-unit">Unit <span className={styles.optional}>(optional)</span></label>
                <input id="create-unit" className={styles.input} maxLength={50} value={createUnit}
                  onChange={e => setCreateUnit(e.target.value)} disabled={createSaving} />
              </div>
              <div className={styles.formGroup}>
                <span className={styles.label}>Pay method</span>
                <div className={styles.inputNote}>Each unit is paid at the driver&apos;s own rate.</div>
              </div>
              {!canDirectCreate && (
                <div className={styles.formGroup}>
                  <label className={styles.label} htmlFor="create-notes">Notes <span className={styles.optional}>(optional)</span></label>
                  <textarea id="create-notes" className={styles.textarea} maxLength={500} value={createNotes}
                    onChange={e => setCreateNotes(e.target.value)} disabled={createSaving} />
                </div>
              )}
              {createError && <div className={styles.errorAlert}>{createError}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button type="submit" className={styles.btnPrimary} disabled={createSaving || !createName.trim()}>
                {createSaving ? 'Working…' : canDirectCreate ? 'Create Pay Item' : 'Submit for Approval'}
              </button>
              <button type="button" className={styles.btnSecondary} disabled={createSaving} onClick={() => setCreateOpen(false)}>Cancel</button>
            </div>
          </form>
        </div>
      )}

      {/* Retire confirm */}
      <ConfirmDialog
        open={retireOpen}
        title="Retire pay item?"
        message={`"${selectedCompany?.definition_name ?? ''}" will be retired. It can no longer be activated for a branch or given new rates; its history, rates and provenance are preserved.`}
        confirmLabel="Retire"
        variant="danger"
        loading={retireWorking}
        onConfirm={() => void confirmRetire()}
        onCancel={() => setRetireOpen(false)}
      />

      {/* Bulk result */}
      {bulkResult && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget) setBulkResult(null); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}><h2 className={styles.modalTitle}>Configuration Applied</h2></div>
            <div className={styles.modalBody}>
              <div className={styles.infoBanner}>
                {bulkResult.updated_branch_count} of {bulkResult.requested_branch_count} branch(es) updated in one transaction.
              </div>
              <div className={styles.bulkResultList}>
                {bulkResult.results.map(r => (
                  <div key={r.branch_id} className={`${styles.bulkResultItem} ${styles.bulkResultSuccess}`}>
                    {r.branch_name} — <strong>{r.status}</strong> (effective {r.effective_from})
                  </div>
                ))}
              </div>
            </div>
            <div className={styles.modalFooter}>
              <button type="button" className={styles.btnPrimary} onClick={() => setBulkResult(null)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
