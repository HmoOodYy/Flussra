/**
 * PayRatesPage — /people/pay-rates
 *
 * Two-panel layout:
 *  Left : filterable driver list (with bulk summary badges)
 *  Right: selected driver panel with tabs
 *         Current Rates | Pending Changes | History | Pay Rules
 */
import { useEffect, useState, useCallback } from 'react';
import { useSearchParams, useNavigate, Link } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import { useAuth } from '../../../store/authStore';
import { canEditPayRates, canManageSettingsAdmin } from '../../../lib/permissions';
import type { DriverSummary, Branch } from '../../../types/core';
import styles from './PayRatesPage.module.css';

// ── Types ─────────────────────────────────────────────────────────────────────

interface RateMatrixCurrentRate {
  driver_rate_id: number;
  amount: string;
  effective_from: string;
  effective_to: string | null;
  status: string;
}

interface RateMatrixGroup {
  group_key: string;
  pay_item_id: number;
  pay_item_name: string;
  item_scope: string;
  rate_behavior: string;
  rate_type_id: number;
  rate_code: string;
  rate_name: string;
  unit_name: string;
  current_rate: RateMatrixCurrentRate | null;
  pending_rate: RateMatrixCurrentRate | null;
  is_required: boolean;
  is_missing: boolean;
  pay_item_effective_from: string | null;
}

interface DriverRateMatrix {
  driver_id: number;
  driver_name: string;
  driver_code: string | null;
  branch_id: number;
  branch_name: string;
  as_of: string;
  groups: RateMatrixGroup[];
}

/** One rate record — used for pending list and history list */
interface DriverRateRecord {
  driver_rate_id: number;
  driver_id: number;
  rate_type_id: number;
  rate_code: string;
  rate_name: string;
  unit_name: string;
  amount: string;
  effective_from: string;
  effective_to: string | null;
  status: string;
  notes: string | null;
  created_at_utc: string;
  approved_at_utc: string | null;
}

/** Summary badge counts returned by /rates/summary */
interface DriverRatesSummary {
  driver_id: number;
  pending_count: number;
  future_approved_count: number;
  missing_required_count: number | null;
}

interface EditRow {
  pay_item_id: number;
  rate_type_id: number;
  amount: string;
  effective_from: string;
}

interface OriginalValues {
  amount: string;
  effective_from: string;
}

type ActiveTab = 'current' | 'pending' | 'history' | 'pay-rules';

/** One DriverPayRule row from /payroll/drivers/{id}/pay-rules */
interface DriverPayRule {
  driver_pay_rule_id: number;
  driver_id: number;
  rule_type: string;       // 'MinimumPay' | 'MaximumPay'
  amount: string;
  effective_from: string;
  effective_to: string | null;
  status: string;          // 'Active' | 'Ended' | 'Voided'
  notes: string | null;
  created_at_utc: string;
}

/** Result from copy-from endpoint */
interface CopyRatesResult {
  target_driver_id: number;
  source_driver_id: number;
  effective_from: string;
  allow_self_approval: boolean;
  rates_copied: number;
  rates_approved: number;
  rates_pending: number;
  pay_rules_copied: number;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d) && d.length > 0) {
    const first = d[0];
    if (first?.msg) return first.msg;
  }
  return 'An unexpected error occurred.';
}

function fmtAmount(amount: string, unitName: string): string {
  const n = parseFloat(amount);
  return `$${n.toFixed(4)}/${unitName}`;
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function statusBadgeClass(status: string): string {
  switch (status) {
    case 'Approved':        return styles.badgeApproved;
    case 'Superseded':      return styles.badgeSuperseded;
    case 'PendingApproval': return styles.badgePending;
    case 'Voided':          return styles.badgeVoided;
    default:                return styles.statusBadge;
  }
}

function isFutureApproved(row: DriverRateRecord): boolean {
  return row.status === 'Approved' && row.effective_from > today();
}

// ── Component ─────────────────────────────────────────────────────────────────

export function PayRatesPage() {
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();

  // ── Driver list state ──────────────────────────────────────────────────────
  const [drivers, setDrivers] = useState<DriverSummary[]>([]);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [loadingDrivers, setLoadingDrivers] = useState(true);
  const [search, setSearch] = useState('');
  const [branchFilter, setBranchFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('Active');

  // ── Selected driver — initialised from URL on first render ────────────────
  const initialDriverId = searchParams.get('driverId')
    ? parseInt(searchParams.get('driverId')!, 10)
    : null;
  const [selectedDriverId, setSelectedDriverId] = useState<number | null>(initialDriverId);

  // ── Tab state ──────────────────────────────────────────────────────────────
  const [activeTab, setActiveTab] = useState<ActiveTab>('current');

  // ── Current Rates (matrix) state ───────────────────────────────────────────
  const [matrix, setMatrix] = useState<DriverRateMatrix | null>(null);
  const [loadingMatrix, setLoadingMatrix] = useState(false);
  const [matrixError, setMatrixError] = useState('');

  // ── Edit state ─────────────────────────────────────────────────────────────
  const [editMode, setEditMode] = useState(false);
  const [editRows, setEditRows] = useState<Record<string, EditRow>>({});
  const [originalValues, setOriginalValues] = useState<Record<string, OriginalValues>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');

  // ── Pending Changes state ──────────────────────────────────────────────────
  const [pendingRates, setPendingRates] = useState<DriverRateRecord[]>([]);
  const [loadingPending, setLoadingPending] = useState(false);
  const [actioningRateId, setActioningRateId] = useState<number | null>(null);
  const [pendingActionError, setPendingActionError] = useState('');

  // ── History state ──────────────────────────────────────────────────────────
  const [historyRows, setHistoryRows] = useState<DriverRateRecord[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);

  // ── Summary badges ─────────────────────────────────────────────────────────
  const [summary, setSummary] = useState<DriverRatesSummary | null>(null);

  // ── Pay Rules state ────────────────────────────────────────────────────────
  const [payRules, setPayRules] = useState<DriverPayRule[]>([]);
  const [loadingPayRules, setLoadingPayRules] = useState(false);
  const [payRulesLoaded, setPayRulesLoaded] = useState(false);
  const [payRulesError, setPayRulesError] = useState('');
  // Inline form for creating or ending a rule
  const [ruleForm, setRuleForm] = useState<{
    ruleType: 'MinimumPay' | 'MaximumPay';
    action: 'add' | 'end';
    amount: string;
    effectiveFrom: string;
    effectiveTo: string;
    notes: string;
  } | null>(null);
  const [savingRule, setSavingRule] = useState(false);
  const [ruleFormError, setRuleFormError] = useState('');

  // ── Copy Rates state ───────────────────────────────────────────────────────
  const [copyModalOpen, setCopyModalOpen] = useState(false);
  const [copySourceDriverId, setCopySourceDriverId] = useState<number | null>(null);
  const [copyEffectiveFrom, setCopyEffectiveFrom] = useState(today());
  const [copyIncludeRules, setCopyIncludeRules] = useState(false);
  const [copying, setCopying] = useState(false);
  const [copyError, setCopyError] = useState('');
  const [copyResult, setCopyResult] = useState<CopyRatesResult | null>(null);

  // ── Bulk driver summary badges ─────────────────────────────────────────────
  const [bulkSummary, setBulkSummary] = useState<Record<number, DriverRatesSummary>>({});

  // ── No-driver-profile state ────────────────────────────────────────────────
  const [noProfile, setNoProfile] = useState(false);

  // ── driverUserId lookup (runs once on mount if param present) ─────────────
  useEffect(() => {
    const driverUserIdParam = searchParams.get('driverUserId');
    if (!driverUserIdParam) return;
    void apiClient
      .get(`/admin/users/${driverUserIdParam}/driver`)
      .then((res) => {
        const data = res.data as { has_driver_profile: boolean; driver_id: number | null };
        if (!data.has_driver_profile || data.driver_id == null) {
          setNoProfile(true);
        } else {
          const newParams = new URLSearchParams(searchParams);
          newParams.delete('driverUserId');
          newParams.set('driverId', String(data.driver_id));
          navigate({ search: newParams.toString() }, { replace: true });
          setSelectedDriverId(data.driver_id);
        }
      })
      .catch(() => setNoProfile(true));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Load drivers ───────────────────────────────────────────────────────────
  useEffect(() => {
    if (!user?.company_id) return;
    void (async () => {
      setLoadingDrivers(true);
      try {
        const res = await apiClient.get('/core/drivers', { params: { company_id: user.company_id } });
        setDrivers(res.data as DriverSummary[]);
        // Load bulk summary badges in the background after drivers are available
        void loadBulkSummary();
      } catch {
        // silent
      } finally {
        setLoadingDrivers(false);
      }
    })();
  }, [user?.company_id]);

  // ── Load branches ──────────────────────────────────────────────────────────
  useEffect(() => {
    if (!user?.company_id) return;
    void apiClient
      .get('/core/branches', { params: { company_id: user.company_id } })
      .then((res) => setBranches(res.data as Branch[]))
      .catch(() => {});
  }, [user?.company_id]);

  // ── Load matrix ────────────────────────────────────────────────────────────
  const loadMatrix = useCallback(async (driverId: number) => {
    setLoadingMatrix(true);
    setMatrixError('');
    setMatrix(null);
    setEditMode(false);
    setEditRows({});
    try {
      const res = await apiClient.get(`/payroll/drivers/${driverId}/rate-matrix`);
      setMatrix(res.data as DriverRateMatrix);
    } catch (err) {
      setMatrixError(apiError(err));
    } finally {
      setLoadingMatrix(false);
    }
  }, []);

  // ── Load pending rates ─────────────────────────────────────────────────────
  const loadPending = useCallback(async (driverId: number) => {
    setLoadingPending(true);
    setPendingActionError('');
    try {
      const res = await apiClient.get(`/payroll/drivers/${driverId}/rates/pending`);
      setPendingRates(res.data as DriverRateRecord[]);
    } catch {
      setPendingRates([]);
    } finally {
      setLoadingPending(false);
    }
  }, []);

  // ── Load history ───────────────────────────────────────────────────────────
  const loadHistory = useCallback(async (driverId: number) => {
    setLoadingHistory(true);
    try {
      const res = await apiClient.get(`/payroll/drivers/${driverId}/rates/history`);
      setHistoryRows(res.data as DriverRateRecord[]);
      setHistoryLoaded(true);
    } catch {
      setHistoryRows([]);
      setHistoryLoaded(true);
    } finally {
      setLoadingHistory(false);
    }
  }, []);

  // ── Load summary ───────────────────────────────────────────────────────────
  const loadSummary = useCallback(async (driverId: number) => {
    try {
      const res = await apiClient.get(`/payroll/drivers/${driverId}/rates/summary`);
      setSummary(res.data as DriverRatesSummary);
    } catch {
      setSummary(null);
    }
  }, []);

  // ── Load pay rules ─────────────────────────────────────────────────────────
  const loadPayRules = useCallback(async (driverId: number) => {
    setLoadingPayRules(true);
    setPayRulesError('');
    try {
      const res = await apiClient.get(`/payroll/drivers/${driverId}/pay-rules`);
      setPayRules(res.data as DriverPayRule[]);
      setPayRulesLoaded(true);
    } catch (err) {
      setPayRulesError(apiError(err));
      setPayRules([]);
      setPayRulesLoaded(true);
    } finally {
      setLoadingPayRules(false);
    }
  }, []);

  // ── Load bulk summary for driver list badges ───────────────────────────────
  // Not wrapped in useCallback — stable API call with no component dependencies.
  async function loadBulkSummary() {
    try {
      const res = await apiClient.get('/payroll/drivers/rates-summary');
      const list = res.data as DriverRatesSummary[];
      const map: Record<number, DriverRatesSummary> = {};
      for (const s of list) map[s.driver_id] = s;
      setBulkSummary(map);
    } catch {
      // Bulk summary badges are non-critical — fail silently
    }
  }

  // ── Reload all driver data (after an action) ───────────────────────────────
  const reloadAll = useCallback(async (driverId: number) => {
    await Promise.all([
      loadMatrix(driverId),
      loadPending(driverId),
      loadSummary(driverId),
      ...(historyLoaded ? [loadHistory(driverId)] : []),
      ...(payRulesLoaded ? [loadPayRules(driverId)] : []),
    ]);
    void loadBulkSummary();
  }, [loadMatrix, loadPending, loadSummary, loadHistory, historyLoaded,
      loadPayRules, payRulesLoaded]);

  // ── When selected driver changes: reset state + load matrix/pending/summary ─
  useEffect(() => {
    if (selectedDriverId == null) return;
    let cancelled = false;

    void (async () => {
      // Reset all driver-specific state before fetching
      if (!cancelled) {
        setMatrix(null);
        setPendingRates([]);
        setHistoryRows([]);
        setHistoryLoaded(false);
        setSummary(null);
        setEditMode(false);
        setEditRows({});
        setMatrixError('');
        setPendingActionError('');
        setPayRules([]);
        setPayRulesLoaded(false);
        setPayRulesError('');
        setRuleForm(null);
        setRuleFormError('');
        setCopyResult(null);
        setCopyError('');
        setCopyModalOpen(false);
      }
      if (cancelled) return;
      await Promise.all([
        loadMatrix(selectedDriverId),
        loadPending(selectedDriverId),
        loadSummary(selectedDriverId),
      ]);
    })();

    return () => { cancelled = true; };
  }, [selectedDriverId, loadMatrix, loadPending, loadSummary]);

  // ── Load history lazily when History tab is first opened ──────────────────
  useEffect(() => {
    if (activeTab !== 'history' || selectedDriverId == null || historyLoaded || loadingHistory) return;
    const driverId = selectedDriverId;
    void (async () => { await loadHistory(driverId); })();
  }, [activeTab, selectedDriverId, historyLoaded, loadingHistory, loadHistory]);

  // ── Load pay rules lazily when Pay Rules tab is first opened ──────────────
  useEffect(() => {
    if (activeTab !== 'pay-rules' || selectedDriverId == null || payRulesLoaded || loadingPayRules) return;
    const driverId = selectedDriverId;
    void (async () => { await loadPayRules(driverId); })();
  }, [activeTab, selectedDriverId, payRulesLoaded, loadingPayRules, loadPayRules]);

  // ── Select driver ──────────────────────────────────────────────────────────
  function selectDriver(driverId: number) {
    setSelectedDriverId(driverId);
    setActiveTab('current');
    const newParams = new URLSearchParams(searchParams);
    newParams.set('driverId', String(driverId));
    setSearchParams(newParams, { replace: true });
    setNoProfile(false);
  }

  // ── Filter drivers ─────────────────────────────────────────────────────────
  const filteredDrivers = drivers.filter((d) => {
    const q = search.toLowerCase();
    const matchSearch =
      !q ||
      d.full_name.toLowerCase().includes(q) ||
      (d.driver_code ?? '').toLowerCase().includes(q);
    const matchBranch = !branchFilter || String(d.branch_id) === branchFilter;
    const matchStatus = !statusFilter || d.driver_status === statusFilter;
    return matchSearch && matchBranch && matchStatus;
  });

  // ── Edit helpers ───────────────────────────────────────────────────────────
  function groupKey(g: RateMatrixGroup): string {
    return g.group_key ?? `${g.pay_item_id}:${g.rate_type_id}`;
  }

  function enterEditMode() {
    if (!matrix) return;
    const initial: Record<string, EditRow> = {};
    const originals: Record<string, OriginalValues> = {};
    for (const g of matrix.groups) {
      const key = groupKey(g);
      const amount = g.current_rate?.amount ?? '';
      const effectiveFrom = today();
      initial[key] = { pay_item_id: g.pay_item_id, rate_type_id: g.rate_type_id, amount, effective_from: effectiveFrom };
      originals[key] = { amount, effective_from: effectiveFrom };
    }
    setEditRows(initial);
    setOriginalValues(originals);
    setEditMode(true);
    setSaveError('');
  }

  function cancelEdit() {
    setEditMode(false);
    setEditRows({});
    setOriginalValues({});
    setSaveError('');
  }

  function isDirty(key: string): boolean {
    const cur = editRows[key];
    const orig = originalValues[key];
    if (!cur || !orig) return false;
    return cur.amount !== orig.amount || cur.effective_from !== orig.effective_from;
  }

  const dirtyCount = Object.keys(editRows).filter((k) => isDirty(k)).length;

  async function saveRates() {
    if (!matrix) return;
    setSaving(true);
    setSaveError('');
    try {
      const dirtyRows = Object.entries(editRows).filter(([key, row]) => {
        if (!isDirty(key)) return false;
        if (!row.amount || parseFloat(row.amount) <= 0) return false;
        return true;
      });
      if (dirtyRows.length === 0) return;

      const effectiveDates = new Set(dirtyRows.map(([, row]) => row.effective_from));
      if (effectiveDates.size > 1) {
        setSaveError(
          'All changed rates must have the same effective date for a batch save. ' +
          'Please set the same effective date on all changed rows, then try again.'
        );
        return;
      }

      const effectiveFrom = dirtyRows[0][1].effective_from;
      const changes = dirtyRows.map(([, row]) => ({
        pay_item_id: row.pay_item_id,
        rate_type_id: row.rate_type_id,
        amount: row.amount,
      }));

      await apiClient.post(
        `/payroll/drivers/${matrix.driver_id}/rates/batch`,
        { effective_from: effectiveFrom, changes }
      );

      setEditMode(false);
      setEditRows({});
      setOriginalValues({});
      await reloadAll(matrix.driver_id);
    } catch (err) {
      setSaveError(apiError(err));
    } finally {
      setSaving(false);
    }
  }

  // ── Pending actions ────────────────────────────────────────────────────────
  async function approveRate(rateId: number) {
    if (!selectedDriverId) return;
    setActioningRateId(rateId);
    setPendingActionError('');
    try {
      await apiClient.post(`/payroll/rates/${rateId}/approve`);
      await reloadAll(selectedDriverId);
    } catch (err) {
      setPendingActionError(apiError(err));
    } finally {
      setActioningRateId(null);
    }
  }

  async function voidRate(rateId: number) {
    if (!selectedDriverId) return;
    setActioningRateId(rateId);
    setPendingActionError('');
    try {
      await apiClient.delete(`/payroll/rates/${rateId}`);
      await reloadAll(selectedDriverId);
    } catch (err) {
      setPendingActionError(apiError(err));
    } finally {
      setActioningRateId(null);
    }
  }

  // ── Void pay rule ──────────────────────────────────────────────────────────
  async function voidPayRule(ruleId: number) {
    if (!selectedDriverId) return;
    setSavingRule(true);
    setRuleFormError('');
    try {
      await apiClient.post(`/payroll/driver-pay-rules/${ruleId}/void`);
      await loadPayRules(selectedDriverId);
    } catch (err) {
      setPayRulesError(apiError(err));
    } finally {
      setSavingRule(false);
    }
  }

  // ── Create pay rule ────────────────────────────────────────────────────────
  async function createPayRule() {
    if (!selectedDriverId || !ruleForm || ruleForm.action !== 'add') return;
    setSavingRule(true);
    setRuleFormError('');
    try {
      await apiClient.post('/payroll/driver-pay-rules', {
        driver_id: selectedDriverId,
        rule_type: ruleForm.ruleType,
        amount: ruleForm.amount,
        effective_from: ruleForm.effectiveFrom,
        notes: ruleForm.notes || null,
      });
      setRuleForm(null);
      await loadPayRules(selectedDriverId);
    } catch (err) {
      setRuleFormError(apiError(err));
    } finally {
      setSavingRule(false);
    }
  }

  // ── End pay rule ───────────────────────────────────────────────────────────
  async function endPayRule(ruleId: number) {
    if (!selectedDriverId || !ruleForm || ruleForm.action !== 'end') return;
    setSavingRule(true);
    setRuleFormError('');
    try {
      await apiClient.post(`/payroll/driver-pay-rules/${ruleId}/end`, {
        effective_to: ruleForm.effectiveTo,
      });
      setRuleForm(null);
      await loadPayRules(selectedDriverId);
    } catch (err) {
      setRuleFormError(apiError(err));
    } finally {
      setSavingRule(false);
    }
  }

  // ── Copy rates from driver ─────────────────────────────────────────────────
  async function copyFromDriver() {
    if (!selectedDriverId || !copySourceDriverId) return;
    setCopying(true);
    setCopyError('');
    setCopyResult(null);
    try {
      const res = await apiClient.post(
        `/payroll/drivers/${selectedDriverId}/rates/copy-from/${copySourceDriverId}`,
        { effective_from: copyEffectiveFrom, include_pay_rules: copyIncludeRules }
      );
      setCopyResult(res.data as CopyRatesResult);
      await reloadAll(selectedDriverId);
    } catch (err) {
      setCopyError(apiError(err));
    } finally {
      setCopying(false);
    }
  }

  // ── Derived counts ─────────────────────────────────────────────────────────
  const missingCount = summary?.missing_required_count
    ?? (matrix?.groups.filter((g) => g.is_missing).length ?? 0);
  const pendingCount = summary?.pending_count ?? pendingRates.length;
  const futureCount  = summary?.future_approved_count ?? 0;

  // ── Derived permissions — branch-aware, keyed off the selected driver ─────
  const canEditMatrix = user && matrix ? canEditPayRates(user, matrix.branch_id) : false;
  const copySourceDriver = drivers.find((d) => d.driver_id === copySourceDriverId);
  const canCopyRates =
    canEditMatrix && !!copySourceDriver && !!user && canEditPayRates(user, copySourceDriver.branch_id);

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className={styles.page}>
      {/* Header */}
      <div className={styles.header}>
        <div>
          <p className={styles.title}>Pay Rates</p>
          <p className={styles.sub}>Configure driver pay rates by rate type.</p>
        </div>
      </div>

      <div className={styles.body}>
        {/* Left panel */}
        <div className={styles.left}>
          <div className={styles.filters}>
            <input
              className={styles.search}
              type="text"
              placeholder="Search drivers..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <div className={styles.filterRow}>
              <select className={styles.sel} value={branchFilter} onChange={(e) => setBranchFilter(e.target.value)}>
                <option value="">All branches</option>
                {branches.map((b) => (
                  <option key={b.branch_id} value={String(b.branch_id)}>{b.branch_name}</option>
                ))}
              </select>
              <select className={styles.sel} value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                <option value="">All statuses</option>
                <option value="Active">Active</option>
                <option value="Inactive">Inactive</option>
                <option value="Terminated">Terminated</option>
              </select>
            </div>
          </div>

          <div className={styles.list}>
            {loadingDrivers ? (
              <div className={styles.emptyList}>Loading drivers...</div>
            ) : filteredDrivers.length === 0 ? (
              <div className={styles.emptyList}>No drivers found.</div>
            ) : (
              filteredDrivers.map((d) => (
                <button
                  key={d.driver_id}
                  className={`${styles.item} ${selectedDriverId === d.driver_id ? styles.itemActive : ''}`}
                  onClick={() => selectDriver(d.driver_id)}
                >
                  <div className={styles.itemBody}>
                    <div className={styles.itemName}>{d.full_name}</div>
                    <div className={styles.itemMeta}>
                      {d.driver_code ?? 'No code'} &middot; {d.branch_name}
                    </div>
                    {/* Bulk summary badges */}
                    {bulkSummary[d.driver_id] && (
                      <div className={styles.listBadgeRow}>
                        {(bulkSummary[d.driver_id].pending_count ?? 0) > 0 && (
                          <span className={styles.listBadgePending}>
                            {bulkSummary[d.driver_id].pending_count} pending
                          </span>
                        )}
                        {(bulkSummary[d.driver_id].future_approved_count ?? 0) > 0 && (
                          <span className={styles.listBadgeFuture}>
                            {bulkSummary[d.driver_id].future_approved_count} future
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                  <span className={d.driver_status === 'Active' ? styles.okChip : styles.statusBadge}>
                    {d.driver_status}
                  </span>
                </button>
              ))
            )}
          </div>

          <div className={styles.listFoot}>
            {filteredDrivers.length} driver{filteredDrivers.length !== 1 ? 's' : ''}
          </div>
        </div>

        {/* Right panel */}
        <div className={styles.right}>
          {noProfile ? (
            <div className={styles.emptyRight}>
              <div className={styles.emptyRightCard}>
                <div className={styles.emptyRightIcon}>
                  <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" strokeWidth="1.5">
                    <circle cx="12" cy="8" r="4" /><path d="M6 20v-2a6 6 0 0 1 12 0v2" />
                  </svg>
                </div>
                <p className={styles.emptyRightTitle}>No driver profile</p>
                <p className={styles.emptyRightMsg}>This user does not have a driver profile yet.</p>
                <Link to="/people" style={{ fontSize: '0.83rem', color: '#6366f1' }}>Back to People</Link>
              </div>
            </div>
          ) : selectedDriverId == null ? (
            <div className={styles.emptyRight}>
              <div className={styles.emptyRightCard}>
                <div className={styles.emptyRightIcon}>
                  <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#9ca3af" strokeWidth="1.5">
                    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                    <circle cx="9" cy="7" r="4" />
                    <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
                    <path d="M16 3.13a4 4 0 0 1 0 7.75" />
                  </svg>
                </div>
                <p className={styles.emptyRightTitle}>Select a driver</p>
                <p className={styles.emptyRightMsg}>
                  Choose a driver from the list to view and manage their pay rates.
                </p>
              </div>
            </div>
          ) : loadingMatrix && !matrix ? (
            <div className={styles.loading}>Loading rates...</div>
          ) : matrixError && !matrix ? (
            <div className={styles.detail}>
              <div className={styles.errBanner}>{matrixError}</div>
            </div>
          ) : matrix ? (
            <div className={styles.detailTabbed}>
              {/* Driver header card */}
              <div className={styles.dHeaderCard} style={{ marginBottom: '0.75rem' }}>
                <div className={styles.dHeaderTop}>
                  <div>
                    <p className={styles.dName}>{matrix.driver_name}</p>
                    <p className={styles.dMeta}>
                      {matrix.driver_code ?? 'No code'} &middot; {matrix.branch_name}
                    </p>
                    {/* Summary badges */}
                    <div className={styles.summaryBadges}>
                      {missingCount > 0 && (
                        <span className={styles.missingChip}>{missingCount} missing</span>
                      )}
                      {pendingCount > 0 && (
                        <span className={styles.pendingBadge}>{pendingCount} pending</span>
                      )}
                      {futureCount > 0 && (
                        <span className={styles.futureBadge}>{futureCount} future approved</span>
                      )}
                    </div>
                  </div>
                  <div className={styles.dActions}>
                    {canEditMatrix && !editMode && activeTab === 'current' && (
                      <button className={styles.btnPrimary} onClick={enterEditMode}>
                        Edit Rates
                      </button>
                    )}
                    {canEditMatrix && !editMode && (
                      <button
                        className={styles.btnSecondary}
                        onClick={() => {
                          setCopyModalOpen(true);
                          setCopySourceDriverId(null);
                          setCopyEffectiveFrom(today());
                          setCopyIncludeRules(false);
                          setCopyError('');
                          setCopyResult(null);
                        }}
                      >
                        Copy from…
                      </button>
                    )}
                    {editMode && (
                      <button className={styles.btnSecondary} onClick={cancelEdit}>
                        Cancel
                      </button>
                    )}
                  </div>
                </div>
              </div>

              {/* Tab navigation */}
              <div className={styles.tabs}>
                <button
                  className={`${styles.tab} ${activeTab === 'current' ? styles.tabActive : ''}`}
                  onClick={() => { setActiveTab('current'); if (editMode) cancelEdit(); }}
                >
                  Current Rates
                </button>
                <button
                  className={`${styles.tab} ${activeTab === 'pending' ? styles.tabActive : ''}`}
                  onClick={() => { setActiveTab('pending'); if (editMode) cancelEdit(); }}
                >
                  Pending Changes
                  {pendingCount > 0 && (
                    <span className={`${styles.tabCount} ${styles.tabCountWarn}`}>
                      {pendingCount}
                    </span>
                  )}
                </button>
                <button
                  className={`${styles.tab} ${activeTab === 'history' ? styles.tabActive : ''}`}
                  onClick={() => { setActiveTab('history'); if (editMode) cancelEdit(); }}
                >
                  History
                </button>
                <button
                  className={`${styles.tab} ${activeTab === 'pay-rules' ? styles.tabActive : ''}`}
                  onClick={() => { setActiveTab('pay-rules'); if (editMode) cancelEdit(); }}
                >
                  Pay Rules
                </button>
              </div>

              {/* Tab panels */}
              <div className={styles.tabPanel}>

                {/* ── Current Rates tab ─────────────────────────────────── */}
                {activeTab === 'current' && (
                  <>
                    {/* Future approved notice */}
                    {futureCount > 0 && !editMode && (
                      <div className={styles.section}>
                        <div className={styles.sectionHead}>
                          <span className={styles.sectionTitle}>Future Approved Rates</span>
                          <span className={styles.futureBadge}>{futureCount} scheduled</span>
                        </div>
                        <FutureApprovedPanel driverId={matrix.driver_id} />
                      </div>
                    )}

                    {/* Required rates matrix */}
                    {matrix.groups.length > 0 ? (
                      <div className={styles.section}>
                        <div className={styles.sectionHead}>
                          <span className={styles.sectionTitle}>Required Rates</span>
                        </div>
                        {saveError && (
                          <div className={styles.errBanner} style={{ margin: '0.75rem 1.25rem 0' }}>
                            {saveError}
                          </div>
                        )}
                        <table className={styles.rateTable}>
                          <thead>
                            <tr>
                              <th>Rate</th>
                              <th>Amount</th>
                              <th>Effective From</th>
                              <th>Status</th>
                            </tr>
                          </thead>
                          <tbody>
                            {matrix.groups.map((g) => {
                              const gKey = groupKey(g);
                              return (
                                <tr key={gKey}>
                                  <td>
                                    <strong>{g.rate_name}</strong>
                                    <div style={{ fontSize: '0.74rem', color: '#6b7280' }}>{g.pay_item_name}</div>
                                    {g.pay_item_effective_from && g.pay_item_effective_from > new Date().toISOString().slice(0, 10) && (
                                      <div style={{ fontSize: '0.72rem', color: '#d97706', marginTop: '2px' }}>
                                        Payroll active from {new Date(g.pay_item_effective_from + 'T00:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}
                                      </div>
                                    )}
                                    {g.rate_behavior !== 'Flat' && g.rate_behavior !== 'EnteredAmount' && (
                                      <span className={styles.advancedBadge} title={`Rate behavior: ${g.rate_behavior}`}>
                                        {g.rate_behavior === 'Block' ? 'Block' :
                                         g.rate_behavior === 'OrdinalTier' ? 'Tiered' :
                                         g.rate_behavior === 'RangeBracket' ? 'Bracket' :
                                         g.rate_behavior === 'RangeProgressive' ? 'Progressive' : 'Advanced'}
                                      </span>
                                    )}
                                  </td>
                                  <td>
                                    {editMode ? (
                                      <input
                                        className={styles.editInput}
                                        type="number"
                                        step="0.0001"
                                        min="0"
                                        placeholder="0.0000"
                                        value={editRows[gKey]?.amount ?? ''}
                                        onChange={(e) =>
                                          setEditRows((prev) => ({
                                            ...prev,
                                            [gKey]: { ...prev[gKey], amount: e.target.value },
                                          }))
                                        }
                                      />
                                    ) : g.current_rate ? (
                                      fmtAmount(g.current_rate.amount, g.unit_name)
                                    ) : (
                                      <span className={styles.missingText}>No rate set</span>
                                    )}
                                  </td>
                                  <td>
                                    {editMode ? (
                                      <input
                                        className={styles.editDateInput}
                                        type="date"
                                        value={editRows[gKey]?.effective_from ?? today()}
                                        onChange={(e) =>
                                          setEditRows((prev) => ({
                                            ...prev,
                                            [gKey]: { ...prev[gKey], effective_from: e.target.value },
                                          }))
                                        }
                                      />
                                    ) : g.current_rate ? (
                                      g.current_rate.effective_from
                                    ) : (
                                      '—'
                                    )}
                                  </td>
                                  <td>
                                    {g.pending_rate && !editMode && (
                                      <span className={styles.pendingBadge} style={{ marginRight: '0.4rem' }}>
                                        Pending
                                      </span>
                                    )}
                                    {!editMode && (
                                      g.current_rate ? (
                                        <span className={styles.okIcon}>&#10003;</span>
                                      ) : (
                                        <span className={styles.missingIcon}>&#9888;</span>
                                      )
                                    )}
                                    {editMode && isDirty(gKey) && (
                                      <span className={styles.pendingBadge}>Edited</span>
                                    )}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>

                        {editMode && (
                          <div className={styles.saveBar}>
                            <button
                              className={styles.btnPrimary}
                              onClick={saveRates}
                              disabled={saving || dirtyCount === 0}
                            >
                              {saving ? 'Saving...' : `Save ${dirtyCount} Change${dirtyCount !== 1 ? 's' : ''}`}
                            </button>
                            <button className={styles.btnSecondary} onClick={cancelEdit}>
                              Cancel
                            </button>
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className={styles.section}>
                        <div className={styles.emptySection}>
                          <p>This branch has no active rate items configured for driver rates.</p>
                          {user !== null && canManageSettingsAdmin(user) ? (
                            <p className={styles.emptySectionHint}>
                              Go to <strong>Settings &gt; Pay Items</strong> to enable rate items for this branch.
                            </p>
                          ) : (
                            <p className={styles.emptySectionHint}>
                              Ask an admin to enable rate items for this branch.
                            </p>
                          )}
                        </div>
                      </div>
                    )}
                  </>
                )}

                {/* ── Pending Changes tab ───────────────────────────────── */}
                {activeTab === 'pending' && (
                  <div className={styles.section}>
                    <div className={styles.sectionHead}>
                      <span className={styles.sectionTitle}>Pending Changes</span>
                    </div>
                    {pendingActionError && (
                      <div className={styles.actionErr}>{pendingActionError}</div>
                    )}
                    {loadingPending ? (
                      <div className={styles.loading}>Loading pending rates...</div>
                    ) : pendingRates.length === 0 ? (
                      <div className={styles.emptySection}>
                        No pending rate changes for this driver.
                      </div>
                    ) : (
                      <table className={styles.rateTable}>
                        <thead>
                          <tr>
                            <th>Rate Type</th>
                            <th>Current</th>
                            <th>Pending Amount</th>
                            <th>Effective From</th>
                            {canEditMatrix && <th>Actions</th>}
                          </tr>
                        </thead>
                        <tbody>
                          {pendingRates.map((r) => {
                            // Look up current approved rate from matrix
                            const matrixGroup = matrix.groups.find(
                              (g) => g.rate_type_id === r.rate_type_id
                            );
                            const currentAmount = matrixGroup?.current_rate?.amount;
                            const busy = actioningRateId === r.driver_rate_id;
                            return (
                              <tr key={r.driver_rate_id}>
                                <td>
                                  <strong>{r.rate_name}</strong>
                                  {r.notes && (
                                    <div style={{ fontSize: '0.74rem', color: '#6b7280' }}>{r.notes}</div>
                                  )}
                                </td>
                                <td>
                                  {currentAmount
                                    ? fmtAmount(currentAmount, r.unit_name)
                                    : <span className={styles.missingText}>No current rate</span>}
                                </td>
                                <td>
                                  <strong>{fmtAmount(r.amount, r.unit_name)}</strong>
                                </td>
                                <td>{r.effective_from}</td>
                                {canEditMatrix && (
                                  <td>
                                    <div className={styles.actionBtns}>
                                      <button
                                        className={styles.btnApprove}
                                        disabled={busy || actioningRateId != null}
                                        onClick={() => void approveRate(r.driver_rate_id)}
                                      >
                                        {busy ? '…' : 'Approve'}
                                      </button>
                                      <button
                                        className={styles.btnVoid}
                                        disabled={busy || actioningRateId != null}
                                        onClick={() => void voidRate(r.driver_rate_id)}
                                      >
                                        {busy ? '…' : 'Void'}
                                      </button>
                                    </div>
                                  </td>
                                )}
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    )}
                  </div>
                )}

                {/* ── History tab ───────────────────────────────────────── */}
                {activeTab === 'history' && (
                  <div className={styles.section}>
                    <div className={styles.sectionHead}>
                      <span className={styles.sectionTitle}>Rate History</span>
                    </div>
                    {loadingHistory ? (
                      <div className={styles.loading}>Loading history...</div>
                    ) : historyRows.length === 0 ? (
                      <div className={styles.emptySection}>
                        No rate history for this driver.
                      </div>
                    ) : (
                      <table className={styles.rateTable}>
                        <thead>
                          <tr>
                            <th>Rate Type</th>
                            <th>Amount</th>
                            <th>Effective</th>
                            <th>Status</th>
                          </tr>
                        </thead>
                        <tbody>
                          {historyRows.map((r) => (
                            <tr key={r.driver_rate_id}>
                              <td>
                                <strong>{r.rate_name}</strong>
                                {r.notes && (
                                  <div style={{ fontSize: '0.74rem', color: '#6b7280' }}>{r.notes}</div>
                                )}
                              </td>
                              <td>${parseFloat(r.amount).toFixed(4)}/{r.unit_name}</td>
                              <td>
                                {r.effective_from}
                                {r.effective_to ? ` – ${r.effective_to}` : ''}
                              </td>
                              <td>
                                {isFutureApproved(r) ? (
                                  <span className={styles.badgeFuture}>Future Approved</span>
                                ) : (
                                  <span className={statusBadgeClass(r.status)}>{r.status}</span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                )}

                {/* ── Pay Rules tab ──────────────────────────────────── */}
                {activeTab === 'pay-rules' && (
                  <div className={styles.section}>
                    <div className={styles.sectionHead}>
                      <span className={styles.sectionTitle}>Pay Rules</span>
                      <span className={styles.sectionNote}>Minimum &amp; Maximum Pay per period</span>
                    </div>
                    <div className={styles.payRulesInfo}>
                      Pay Rules set a floor (Minimum Pay) or ceiling (Maximum Pay) applied
                      during finalization. They are separate from the rate matrix.
                    </div>
                    {payRulesError && (
                      <div className={styles.errBanner} style={{ margin: '0 1.25rem 0.5rem' }}>
                        {payRulesError}
                      </div>
                    )}
                    {loadingPayRules ? (
                      <div className={styles.loading}>Loading pay rules...</div>
                    ) : (
                      <>
                        {(['MinimumPay', 'MaximumPay'] as const).map((ruleType) => {
                          const active = payRules.find(r => r.rule_type === ruleType && r.status === 'Active');
                          const ended  = payRules.filter(r => r.rule_type === ruleType && r.status === 'Ended');
                          const voided = payRules.filter(r => r.rule_type === ruleType && r.status === 'Voided');
                          const label  = ruleType === 'MinimumPay' ? 'Minimum Pay' : 'Maximum Pay';
                          const isFormOpenForThis = ruleForm?.ruleType === ruleType;
                          const isAddForm = isFormOpenForThis && ruleForm?.action === 'add';
                          const isEndForm = isFormOpenForThis && ruleForm?.action === 'end';

                          return (
                            <div key={ruleType} className={styles.payRuleCard}>
                              {/* ── Header ── */}
                              <div className={styles.payRuleHeader}>
                                <strong>{label}</strong>
                                {active ? (
                                  <span className={styles.badgeApproved}>Active</span>
                                ) : (
                                  <span className={styles.badgeVoided}>Not set</span>
                                )}
                                {/* Actions */}
                                {canEditMatrix && !isFormOpenForThis && (
                                  <div className={styles.payRuleActions}>
                                    {!active && (
                                      <button
                                        className={styles.btnApprove}
                                        onClick={() => {
                                          setRuleForm({
                                            ruleType,
                                            action: 'add',
                                            amount: '',
                                            effectiveFrom: today(),
                                            effectiveTo: '',
                                            notes: '',
                                          });
                                          setRuleFormError('');
                                        }}
                                      >
                                        + Add
                                      </button>
                                    )}
                                    {active && (
                                      <>
                                        <button
                                          className={styles.btnSecondary}
                                          style={{ fontSize: '0.75rem', padding: '0.2rem 0.55rem' }}
                                          onClick={() => {
                                            setRuleForm({
                                              ruleType,
                                              action: 'end',
                                              amount: '',
                                              effectiveFrom: '',
                                              effectiveTo: today(),
                                              notes: '',
                                            });
                                            setRuleFormError('');
                                          }}
                                        >
                                          End
                                        </button>
                                        <button
                                          className={styles.btnVoid}
                                          style={{ fontSize: '0.75rem', padding: '0.2rem 0.55rem' }}
                                          disabled={savingRule}
                                          onClick={() => void voidPayRule(active.driver_pay_rule_id)}
                                        >
                                          Void
                                        </button>
                                      </>
                                    )}
                                  </div>
                                )}
                              </div>

                              {/* ── Current active rule display ── */}
                              {active && !isEndForm && (
                                <div className={styles.payRuleRow}>
                                  <span className={styles.payRuleAmount}>
                                    ${parseFloat(active.amount).toFixed(2)}<span className={styles.payRuleUnit}>/period</span>
                                  </span>
                                  <span className={styles.payRuleMeta}>
                                    From {active.effective_from}
                                    {active.effective_to ? ` to ${active.effective_to}` : ' (open-ended)'}
                                  </span>
                                </div>
                              )}
                              {active?.notes && !isEndForm && (
                                <div className={styles.payRuleNotes}>{active.notes}</div>
                              )}

                              {/* ── Add form ── */}
                              {isAddForm && (
                                <div className={styles.payRuleForm}>
                                  <div className={styles.payRuleFormRow}>
                                    <div className={styles.payRuleFormGroup}>
                                      <label className={styles.formLabel}>Amount ($/period)</label>
                                      <input
                                        className={styles.editInput}
                                        type="number"
                                        step="0.01"
                                        min="0.01"
                                        placeholder="e.g. 150.00"
                                        value={ruleForm.amount}
                                        onChange={e => setRuleForm(f => f ? { ...f, amount: e.target.value } : f)}
                                      />
                                    </div>
                                    <div className={styles.payRuleFormGroup}>
                                      <label className={styles.formLabel}>Effective from</label>
                                      <input
                                        className={styles.editDateInput}
                                        type="date"
                                        value={ruleForm.effectiveFrom}
                                        onChange={e => setRuleForm(f => f ? { ...f, effectiveFrom: e.target.value } : f)}
                                      />
                                    </div>
                                  </div>
                                  <div className={styles.payRuleFormGroup}>
                                    <label className={styles.formLabel}>Notes (optional)</label>
                                    <input
                                      className={styles.editInput}
                                      style={{ width: '100%' }}
                                      type="text"
                                      placeholder="Optional note"
                                      value={ruleForm.notes}
                                      onChange={e => setRuleForm(f => f ? { ...f, notes: e.target.value } : f)}
                                    />
                                  </div>
                                  {ruleFormError && (
                                    <div className={styles.actionErr} style={{ borderRadius: 6, margin: '0.25rem 0' }}>
                                      {ruleFormError}
                                    </div>
                                  )}
                                  <div className={styles.payRuleFormButtons}>
                                    <button
                                      className={styles.btnPrimary}
                                      disabled={savingRule || !ruleForm.amount || !ruleForm.effectiveFrom}
                                      onClick={() => void createPayRule()}
                                    >
                                      {savingRule ? 'Saving…' : `Add ${label}`}
                                    </button>
                                    <button
                                      className={styles.btnSecondary}
                                      disabled={savingRule}
                                      onClick={() => { setRuleForm(null); setRuleFormError(''); }}
                                    >
                                      Cancel
                                    </button>
                                  </div>
                                </div>
                              )}

                              {/* ── End form ── */}
                              {isEndForm && active && (
                                <div className={styles.payRuleForm}>
                                  <p className={styles.payRuleFormDesc}>
                                    Ending this rule will close it at the date you specify.
                                    The current amount is <strong>${parseFloat(active.amount).toFixed(2)}</strong>.
                                    Historical finalized periods that relied on this rule will not be affected.
                                  </p>
                                  <div className={styles.payRuleFormRow}>
                                    <div className={styles.payRuleFormGroup}>
                                      <label className={styles.formLabel}>End date (effective to)</label>
                                      <input
                                        className={styles.editDateInput}
                                        type="date"
                                        value={ruleForm.effectiveTo}
                                        onChange={e => setRuleForm(f => f ? { ...f, effectiveTo: e.target.value } : f)}
                                      />
                                    </div>
                                  </div>
                                  {ruleFormError && (
                                    <div className={styles.actionErr} style={{ borderRadius: 6, margin: '0.25rem 0' }}>
                                      {ruleFormError}
                                    </div>
                                  )}
                                  <div className={styles.payRuleFormButtons}>
                                    <button
                                      className={styles.btnPrimary}
                                      disabled={savingRule || !ruleForm.effectiveTo}
                                      onClick={() => void endPayRule(active.driver_pay_rule_id)}
                                    >
                                      {savingRule ? 'Saving…' : 'End Rule'}
                                    </button>
                                    <button
                                      className={styles.btnSecondary}
                                      disabled={savingRule}
                                      onClick={() => { setRuleForm(null); setRuleFormError(''); }}
                                    >
                                      Cancel
                                    </button>
                                  </div>
                                </div>
                              )}

                              {/* ── Empty state ── */}
                              {!active && !isAddForm && payRules.filter(r => r.rule_type === ruleType).length === 0 && (
                                <div className={styles.emptySection} style={{ padding: '0.5rem 0' }}>
                                  No {label.toLowerCase()} rule set.
                                </div>
                              )}

                              {/* ── History ── */}
                              {(ended.length > 0 || voided.length > 0) && (
                                <details className={styles.payRuleHistory}>
                                  <summary style={{ cursor: 'pointer', fontSize: '0.78rem', color: '#6b7280', padding: '0.3rem 0' }}>
                                    {ended.length + voided.length} historical record{ended.length + voided.length !== 1 ? 's' : ''}
                                  </summary>
                                  <table className={styles.rateTable} style={{ marginTop: '0.5rem' }}>
                                    <thead>
                                      <tr><th>Amount</th><th>Period</th><th>Status</th></tr>
                                    </thead>
                                    <tbody>
                                      {[...ended, ...voided].map(r => (
                                        <tr key={r.driver_pay_rule_id}>
                                          <td>${parseFloat(r.amount).toFixed(2)}</td>
                                          <td>{r.effective_from}{r.effective_to ? ` – ${r.effective_to}` : ''}</td>
                                          <td>
                                            <span className={r.status === 'Ended' ? styles.badgeSuperseded : styles.badgeVoided}>
                                              {r.status}
                                            </span>
                                          </td>
                                        </tr>
                                      ))}
                                    </tbody>
                                  </table>
                                </details>
                              )}
                            </div>
                          );
                        })}
                      </>
                    )}
                  </div>
                )}

              </div>

              {/* ── Copy Rates modal ────────────────────────────────────── */}
              {copyModalOpen && (
                <div className={styles.modalOverlay} onClick={() => setCopyModalOpen(false)}>
                  <div className={styles.modal} onClick={e => e.stopPropagation()}>
                    <div className={styles.modalHead}>
                      <strong>Copy rates from another driver</strong>
                      <button className={styles.modalClose} onClick={() => setCopyModalOpen(false)}>✕</button>
                    </div>
                    <div className={styles.modalBody}>
                      <p className={styles.modalDesc}>
                        Copies current <strong>Approved</strong> rates from the selected source driver
                        to <strong>{matrix.driver_name}</strong>. Only rates valid for this driver&apos;s
                        branch are copied.
                      </p>

                      <label className={styles.formLabel}>Source driver</label>
                      <select
                        className={styles.sel}
                        value={copySourceDriverId ?? ''}
                        onChange={e => setCopySourceDriverId(e.target.value ? Number(e.target.value) : null)}
                      >
                        <option value="">— Select a driver —</option>
                        {drivers
                          .filter(d => d.driver_id !== matrix.driver_id)
                          .map(d => (
                            <option key={d.driver_id} value={d.driver_id}>
                              {d.full_name} ({d.driver_code ?? 'no code'}) · {d.branch_name}
                            </option>
                          ))}
                      </select>

                      <label className={styles.formLabel} style={{ marginTop: '0.75rem' }}>Effective from</label>
                      <input
                        type="date"
                        className={styles.editDateInput}
                        value={copyEffectiveFrom}
                        onChange={e => setCopyEffectiveFrom(e.target.value)}
                      />

                      <label className={styles.formCheckLabel} style={{ marginTop: '0.75rem' }}>
                        <input
                          type="checkbox"
                          checked={copyIncludeRules}
                          onChange={e => setCopyIncludeRules(e.target.checked)}
                        />
                        &nbsp;Include MinimumPay / MaximumPay rules
                      </label>

                      {copyError && <div className={styles.actionErr}>{copyError}</div>}

                      {copyResult && (
                        <div className={styles.copySuccess}>
                          ✓ Copied {copyResult.rates_copied} rate{copyResult.rates_copied !== 1 ? 's' : ''}
                          {copyResult.rates_approved > 0 && ` (${copyResult.rates_approved} approved)`}
                          {copyResult.rates_pending > 0 && ` (${copyResult.rates_pending} pending approval)`}
                          {copyResult.pay_rules_copied > 0 && `, ${copyResult.pay_rules_copied} rule${copyResult.pay_rules_copied !== 1 ? 's' : ''}`}
                        </div>
                      )}
                    </div>
                    <div className={styles.modalFoot}>
                      <button
                        className={styles.btnPrimary}
                        disabled={!copySourceDriverId || !copyEffectiveFrom || copying || !canCopyRates}
                        onClick={() => void copyFromDriver()}
                      >
                        {copying ? 'Copying…' : 'Copy Rates'}
                      </button>
                      <button className={styles.btnSecondary} onClick={() => setCopyModalOpen(false)}>
                        {copyResult ? 'Close' : 'Cancel'}
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// ── Future Approved Panel ─────────────────────────────────────────────────────

/**
 * Loads the driver's history and shows only future-dated Approved rates.
 * Used in the Current Rates tab when futureCount > 0.
 */
function FutureApprovedPanel({ driverId }: { driverId: number }) {
  const [rows, setRows] = useState<DriverRateRecord[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    void apiClient
      .get(`/payroll/drivers/${driverId}/rates/history`)
      .then((res) => {
        const data = res.data as DriverRateRecord[];
        setRows(data.filter(isFutureApproved));
      })
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [driverId]);

  if (loading) return <div className={styles.loading}>Loading...</div>;
  if (rows.length === 0) return null;

  return (
    <table className={styles.rateTable}>
      <thead>
        <tr>
          <th>Rate Type</th>
          <th>Amount</th>
          <th>Effective From</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.driver_rate_id}>
            <td><strong>{r.rate_name}</strong></td>
            <td>${parseFloat(r.amount).toFixed(4)}/{r.unit_name}</td>
            <td>{r.effective_from}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
