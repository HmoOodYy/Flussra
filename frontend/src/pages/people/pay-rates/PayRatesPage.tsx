/**
 * PayRatesPage — /people/pay-rates
 *
 * Two-panel layout:
 *  Left : filterable driver list
 *  Right: selected driver panel with tabs
 *         Current Rates | Pending Changes | History | Pay Rules
 *
 * Ordinary PayDefinition rates are target DriverRateAssignments. The Status pay
 * rate section below them still uses the temporary Status-only rate path until
 * Status compensation is cut over.
 */
import { useEffect, useState, useCallback } from 'react';
import { useSearchParams, useNavigate, Link } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import {
  approveAssignment,
  createAssignment,
  discardAssignment,
  listAssignmentHistory,
  listDriverPayRates,
  replaceAssignmentValues,
  updateAssignment,
  voidAssignment,
} from '../../../lib/compensationApi';
import { useAuth } from '../../../store/authStore';
import { canEditPayRates } from '../../../lib/permissions';
import { formatMoney, formatRate } from '../../../lib/money';
import type { DriverSummary, Branch } from '../../../types/core';
import type { AssignmentSummary, DriverPayRateRow } from '../../../types/compensation';
import styles from './PayRatesPage.module.css';

// ── Types ─────────────────────────────────────────────────────────────────────

/** A Status pay rate (temporary Status-only rate path). */
interface StatusRate {
  driver_rate_id: number;
  amount: string;
  effective_from: string;
  effective_to: string | null;
  status: string;
}

interface StatusRateGroup {
  group_key: string;
  status_rate_column_id: number;
  rate_type_id: number;
  rate_name: string;
  unit_name: string;
  current_rate: StatusRate | null;
  pending_rate: StatusRate | null;
  is_missing: boolean;
}

interface StatusRateMatrix {
  driver_id: number;
  groups: StatusRateGroup[];
}

interface EditRow {
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

interface HistoryRow extends AssignmentSummary {
  definition_name: string;
  unit: string | null;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (d && typeof d === 'object' && typeof (d as { message?: unknown }).message === 'string') {
    return (d as { message: string }).message;
  }
  if (Array.isArray(d) && d.length > 0) {
    const first = d[0];
    if (first?.msg) return first.msg;
  }
  return 'An unexpected error occurred.';
}

function fmtAmount(amount: string | null, unit: string | null, code: string | null, digits: number | null): string {
  if (amount === null) return '—';
  const text = formatRate(amount, code, digits);
  return unit ? `${text}/${unit}` : text;
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function statusBadgeClass(status: string): string {
  switch (status) {
    case 'Approved':   return styles.badgeApproved;
    case 'Superseded': return styles.badgeSuperseded;
    case 'Pending':    return styles.badgePending;
    case 'Voided':     return styles.badgeVoided;
    default:           return styles.statusBadge;
  }
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

  const initialDriverId = searchParams.get('driverId')
    ? parseInt(searchParams.get('driverId')!, 10)
    : null;
  const [selectedDriverId, setSelectedDriverId] = useState<number | null>(initialDriverId);
  const [activeTab, setActiveTab] = useState<ActiveTab>('current');

  // ── Target rates ───────────────────────────────────────────────────────────
  const [rows, setRows] = useState<DriverPayRateRow[]>([]);
  const [loadingRows, setLoadingRows] = useState(false);
  const [rowsError, setRowsError] = useState('');
  const [statusMatrix, setStatusMatrix] = useState<StatusRateMatrix | null>(null);

  // ── Edit state ─────────────────────────────────────────────────────────────
  // Pay Item rates are edited and saved one RateDefinition (row) at a time.
  const [targetDrafts, setTargetDrafts] = useState<Record<number, EditRow>>({});
  const [targetSavingId, setTargetSavingId] = useState<number | null>(null);
  const [targetErrors, setTargetErrors] = useState<Record<number, string>>({});
  // Status pay rates keep their temporary batch path (until P5), separately.
  const [statusEditMode, setStatusEditMode] = useState(false);
  const [statusDrafts, setStatusDrafts] = useState<Record<string, EditRow>>({});
  const [statusOriginal, setStatusOriginal] = useState<Record<string, EditRow>>({});
  const [statusSaving, setStatusSaving] = useState(false);
  const [statusSaveError, setStatusSaveError] = useState('');

  // ── Pending actions ────────────────────────────────────────────────────────
  const [actioningId, setActioningId] = useState<number | null>(null);
  const [pendingActionError, setPendingActionError] = useState('');

  // ── History ────────────────────────────────────────────────────────────────
  const [historyRows, setHistoryRows] = useState<HistoryRow[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [voidTarget, setVoidTarget] = useState<HistoryRow | null>(null);
  const [voidReason, setVoidReason] = useState('');
  const [voidError, setVoidError] = useState('');
  const [voiding, setVoiding] = useState(false);

  // ── Pay Rules state ────────────────────────────────────────────────────────
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

  // ── Load target rates + Status pay rates ───────────────────────────────────
  const loadRates = useCallback(async (driverId: number) => {
    setLoadingRows(true);
    setRowsError('');
    setRows([]);
    setStatusMatrix(null);
    try {
      setRows(await listDriverPayRates(driverId));
    } catch (err) {
      setRowsError(apiError(err));
    }
    try {
      const res = await apiClient.get(`/payroll/drivers/${driverId}/rate-matrix`);
      setStatusMatrix(res.data as StatusRateMatrix);
    } catch {
      setStatusMatrix(null);
    } finally {
      setLoadingRows(false);
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

  const loadHistory = useCallback(async (driverId: number, current: DriverPayRateRow[]) => {
    setLoadingHistory(true);
    try {
      const lists = await Promise.all(current.map(async (row) => {
        const items = await listAssignmentHistory(driverId, row.rate_definition_id);
        return items.map((item) => ({
          ...item, definition_name: row.definition_name, unit: row.unit,
        }));
      }));
      setHistoryRows(lists.flat().sort((a, b) => b.effective_from.localeCompare(a.effective_from)));
    } catch {
      setHistoryRows([]);
    } finally {
      setHistoryLoaded(true);
      setLoadingHistory(false);
    }
  }, []);

  const reloadAll = useCallback(async (driverId: number) => {
    await loadRates(driverId);
    setHistoryLoaded(false);
    if (payRulesLoaded) await loadPayRules(driverId);
  }, [loadRates, loadPayRules, payRulesLoaded]);

  // ── When selected driver changes: reset state + load ──────────────────────
  useEffect(() => {
    if (selectedDriverId == null) return;
    let cancelled = false;
    void (async () => {
      if (cancelled) return;
      setHistoryRows([]);
      setHistoryLoaded(false);
      setPendingActionError('');
      setTargetDrafts({});
      setTargetErrors({});
      setStatusEditMode(false);
      setStatusDrafts({});
      setStatusOriginal({});
      setStatusSaveError('');
      setPayRules([]);
      setPayRulesLoaded(false);
      setPayRulesError('');
      setRuleForm(null);
      setRuleFormError('');
      await loadRates(selectedDriverId);
    })();
    return () => { cancelled = true; };
  }, [selectedDriverId, loadRates]);

  // ── Lazy tabs ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (activeTab !== 'history' || selectedDriverId == null || historyLoaded || loadingHistory || loadingRows) return;
    const driverId = selectedDriverId;
    void (async () => { await loadHistory(driverId, rows); })();
  }, [activeTab, selectedDriverId, historyLoaded, loadingHistory, loadingRows, rows, loadHistory]);

  useEffect(() => {
    if (activeTab !== 'pay-rules' || selectedDriverId == null || payRulesLoaded || loadingPayRules) return;
    const driverId = selectedDriverId;
    void (async () => { await loadPayRules(driverId); })();
  }, [activeTab, selectedDriverId, payRulesLoaded, loadingPayRules, loadPayRules]);

  function selectDriver(driverId: number) {
    setSelectedDriverId(driverId);
    setActiveTab('current');
    const newParams = new URLSearchParams(searchParams);
    newParams.set('driverId', String(driverId));
    setSearchParams(newParams, { replace: true });
    setNoProfile(false);
  }

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

  const selectedDriver = drivers.find((d) => d.driver_id === selectedDriverId) ?? null;

  // ── Edit helpers ───────────────────────────────────────────────────────────
  const statusKey = (group: StatusRateGroup) => `s:${group.status_rate_column_id}`;

  function cancelAllEdits() {
    setTargetDrafts({});
    setTargetErrors({});
    setStatusEditMode(false);
    setStatusDrafts({});
    setStatusOriginal({});
    setStatusSaveError('');
  }

  function startTargetEdit(row: DriverPayRateRow) {
    setTargetErrors((prev) => { const next = { ...prev }; delete next[row.rate_definition_id]; return next; });
    setTargetDrafts((prev) => ({
      ...prev,
      [row.rate_definition_id]: {
        amount: row.pending?.amount ?? row.current?.amount ?? '',
        effective_from: row.pending?.effective_from ?? today(),
      },
    }));
  }

  function cancelTargetEdit(rateDefinitionId: number) {
    setTargetDrafts((prev) => { const next = { ...prev }; delete next[rateDefinitionId]; return next; });
    setTargetErrors((prev) => { const next = { ...prev }; delete next[rateDefinitionId]; return next; });
  }

  function setTargetField(rateDefinitionId: number, field: keyof EditRow, value: string) {
    setTargetDrafts((prev) => ({ ...prev, [rateDefinitionId]: { ...prev[rateDefinitionId], [field]: value } }));
  }

  /** Refresh the Driver's target rate state without blanking the page. */
  async function refreshTargetRows(driverId: number) {
    try {
      setRows(await listDriverPayRates(driverId));
      setHistoryLoaded(false);
    } catch (err) {
      setRowsError(apiError(err));
    }
  }

  /**
   * Save ONE Pay Item rate as one complete assignment, optionally approving it.
   * The backend has no multi-row transaction, so exactly one RateDefinition is
   * mutated per action. A Pending assignment that survives an approval failure
   * stays visible and recoverable (Pending Changes tab).
   */
  async function saveTargetRow(row: DriverPayRateRow, approve: boolean) {
    const id = row.rate_definition_id;
    const draft = targetDrafts[id];
    if (!selectedDriverId || !draft || draft.amount === '') return;
    const driverId = selectedDriverId;
    setTargetSavingId(id);
    setTargetErrors((prev) => { const next = { ...prev }; delete next[id]; return next; });
    let phase: 'save' | 'approve' = 'save';
    try {
      let assignmentId: number;
      if (row.pending) {
        assignmentId = row.pending.driver_rate_assignment_id;
        if (row.pending.effective_from !== draft.effective_from) {
          await updateAssignment(assignmentId, { effective_from: draft.effective_from });
        }
      } else {
        const created = await createAssignment({
          driver_id: driverId, rate_definition_id: id, effective_from: draft.effective_from,
        });
        assignmentId = created.driver_rate_assignment_id;
      }
      await replaceAssignmentValues(assignmentId, [{
        rate_component_definition_id: row.rate_component_definition_id, amount: draft.amount,
      }]);
      if (approve) {
        phase = 'approve';
        await approveAssignment(assignmentId);
      }
      cancelTargetEdit(id);
    } catch (err) {
      if (phase === 'approve') {
        setTargetErrors((prev) => ({
          ...prev,
          [id]: `${row.definition_name}: the change was saved as Pending, but approval failed — ` +
            `${apiError(err)} It remains under Pending Changes and can be approved or discarded there.`,
        }));
        setTargetDrafts((prev) => { const next = { ...prev }; delete next[id]; return next; });
      } else {
        setTargetErrors((prev) => ({ ...prev, [id]: `${row.definition_name}: ${apiError(err)}` }));
      }
    } finally {
      await refreshTargetRows(driverId);
      setTargetSavingId(null);
    }
  }

  // ── Status pay rates: temporary batch path until P5 ────────────────────────
  function startStatusEdit() {
    const initial: Record<string, EditRow> = {};
    for (const group of statusMatrix?.groups ?? []) {
      initial[statusKey(group)] = { amount: group.current_rate?.amount ?? '', effective_from: today() };
    }
    setStatusDrafts(initial);
    setStatusOriginal(initial);
    setStatusEditMode(true);
    setStatusSaveError('');
  }

  function cancelStatusEdit() {
    setStatusEditMode(false);
    setStatusDrafts({});
    setStatusOriginal({});
    setStatusSaveError('');
  }

  function isStatusDirty(key: string): boolean {
    const cur = statusDrafts[key];
    const orig = statusOriginal[key];
    if (!cur || !orig) return false;
    return cur.amount !== orig.amount || cur.effective_from !== orig.effective_from;
  }

  const dirtyStatusKeys = Object.keys(statusDrafts).filter((k) => isStatusDirty(k) && statusDrafts[k].amount !== '');

  function setStatusField(key: string, field: keyof EditRow, value: string) {
    setStatusDrafts((prev) => ({ ...prev, [key]: { ...prev[key], [field]: value } }));
  }

  async function saveStatusRates() {
    if (!selectedDriverId) return;
    const driverId = selectedDriverId;
    setStatusSaving(true);
    setStatusSaveError('');
    try {
      const statusChanges = (statusMatrix?.groups ?? []).filter((g) => dirtyStatusKeys.includes(statusKey(g)));
      const dates = new Set(statusChanges.map((g) => statusDrafts[statusKey(g)].effective_from));
      if (dates.size > 1) {
        throw new Error('All changed Status pay rates must share one effective date.');
      }
      await apiClient.post(`/payroll/drivers/${driverId}/rates/batch`, {
        effective_from: [...dates][0],
        changes: statusChanges.map((g) => ({
          status_rate_column_id: g.status_rate_column_id,
          rate_type_id: g.rate_type_id,
          amount: statusDrafts[statusKey(g)].amount,
        })),
      });
      cancelStatusEdit();
      await loadRates(driverId);
    } catch (err) {
      setStatusSaveError(err instanceof Error ? err.message : apiError(err));
      await loadRates(driverId);
    } finally {
      setStatusSaving(false);
    }
  }

  // ── Pending actions ────────────────────────────────────────────────────────
  async function runPendingAction(id: number, action: () => Promise<unknown>) {
    if (!selectedDriverId) return;
    setActioningId(id);
    setPendingActionError('');
    try {
      await action();
      await reloadAll(selectedDriverId);
    } catch (err) {
      setPendingActionError(apiError(err));
    } finally {
      setActioningId(null);
    }
  }

  async function executeVoid() {
    if (!voidTarget || !selectedDriverId) return;
    if (!voidReason.trim()) {
      setVoidError('A reason is required.');
      return;
    }
    setVoiding(true);
    setVoidError('');
    try {
      await voidAssignment(voidTarget.driver_rate_assignment_id, voidReason.trim());
      setVoidTarget(null);
      setVoidReason('');
      await reloadAll(selectedDriverId);
    } catch (err) {
      setVoidError(apiError(err));
    } finally {
      setVoiding(false);
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

  // ── Derived ────────────────────────────────────────────────────────────────
  const currencyCode = user?.currency_code ?? null;
  const currencyDigits = user?.currency_minor_unit_digits ?? null;
  const missingCount = rows.filter((r) => r.current === null).length;
  const pendingRows = rows.filter((r) => r.pending !== null);
  const statusPending = (statusMatrix?.groups ?? []).filter((g) => g.pending_rate !== null);
  const pendingCount = pendingRows.length + statusPending.length;
  const futureCount = rows.filter((r) => r.future !== null).length;
  const canEditMatrix = user && selectedDriver ? canEditPayRates(user, selectedDriver.branch_id) : false;

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <p className={styles.title}>Pay Rates</p>
          <p className={styles.sub}>Set each driver&apos;s rate for the pay items active in their branch.</p>
        </div>
      </div>

      <div className={styles.body}>
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

        <div className={styles.right}>
          {noProfile ? (
            <div className={styles.emptyRight}>
              <div className={styles.emptyRightCard}>
                <p className={styles.emptyRightTitle}>No driver profile</p>
                <p className={styles.emptyRightMsg}>This user does not have a driver profile yet.</p>
                <Link to="/people" style={{ fontSize: '0.83rem', color: '#6366f1' }}>Back to People</Link>
              </div>
            </div>
          ) : selectedDriverId == null ? (
            <div className={styles.emptyRight}>
              <div className={styles.emptyRightCard}>
                <p className={styles.emptyRightTitle}>Select a driver</p>
                <p className={styles.emptyRightMsg}>
                  Choose a driver from the list to view and manage their pay rates.
                </p>
              </div>
            </div>
          ) : loadingRows && rows.length === 0 && !statusMatrix ? (
            <div className={styles.loading}>Loading rates...</div>
          ) : rowsError ? (
            <div className={styles.detail}>
              <div className={styles.errBanner}>{rowsError}</div>
            </div>
          ) : (
            <div className={styles.detailTabbed}>
              <div className={styles.dHeaderCard} style={{ marginBottom: '0.75rem' }}>
                <div className={styles.dHeaderTop}>
                  <div>
                    <p className={styles.dName}>{selectedDriver?.full_name ?? `Driver ${selectedDriverId}`}</p>
                    <p className={styles.dMeta}>
                      {selectedDriver?.driver_code ?? 'No code'} &middot; {selectedDriver?.branch_name ?? ''}
                    </p>
                    <div className={styles.summaryBadges}>
                      {missingCount > 0 && <span className={styles.missingChip}>{missingCount} missing</span>}
                      {pendingCount > 0 && <span className={styles.pendingBadge}>{pendingCount} pending</span>}
                      {futureCount > 0 && <span className={styles.futureBadge}>{futureCount} future approved</span>}
                    </div>
                  </div>
                  <div className={styles.dActions}>
                  </div>
                </div>
              </div>

              <div className={styles.tabs}>
                <button className={`${styles.tab} ${activeTab === 'current' ? styles.tabActive : ''}`}
                  onClick={() => { setActiveTab('current'); cancelAllEdits(); }}>Current Rates</button>
                <button className={`${styles.tab} ${activeTab === 'pending' ? styles.tabActive : ''}`}
                  onClick={() => { setActiveTab('pending'); cancelAllEdits(); }}>
                  Pending Changes
                  {pendingCount > 0 && (
                    <span className={`${styles.tabCount} ${styles.tabCountWarn}`}>{pendingCount}</span>
                  )}
                </button>
                <button className={`${styles.tab} ${activeTab === 'history' ? styles.tabActive : ''}`}
                  onClick={() => { setActiveTab('history'); cancelAllEdits(); }}>History</button>
                <button className={`${styles.tab} ${activeTab === 'pay-rules' ? styles.tabActive : ''}`}
                  onClick={() => { setActiveTab('pay-rules'); cancelAllEdits(); }}>Pay Rules</button>
              </div>

              <div className={styles.tabPanel}>

                {activeTab === 'current' && (
                  <>
                    <div className={styles.section}>
                      <div className={styles.sectionHead}>
                        <span className={styles.sectionTitle}>Pay Item Rates</span>
                      </div>
                      {rows.length > 0 ? (
                        <table className={styles.rateTable}>
                          <thead>
                            <tr>
                              <th>Pay Item</th><th>Amount</th><th>Effective From</th><th>Status</th>
                              {canEditMatrix && <th>Actions</th>}
                            </tr>
                          </thead>
                          <tbody>
                            {rows.map((row) => {
                              const id = row.rate_definition_id;
                              const draft = targetDrafts[id];
                              const editing = draft !== undefined;
                              const busy = targetSavingId === id;
                              return (
                                <tr key={id}>
                                  <td>
                                    <strong>{row.definition_name}</strong>
                                    <div style={{ fontSize: '0.74rem', color: '#6b7280' }}>{row.definition_code}</div>
                                    {row.future && (
                                      <div style={{ fontSize: '0.72rem', color: '#d97706', marginTop: '2px' }}>
                                        Next: {fmtAmount(row.future.amount, row.unit, currencyCode, currencyDigits)} from {row.future.effective_from}
                                      </div>
                                    )}
                                    {targetErrors[id] && (
                                      <div className={styles.actionErr} style={{ marginTop: '0.35rem' }}>{targetErrors[id]}</div>
                                    )}
                                  </td>
                                  <td>
                                    {editing ? (
                                      <input
                                        className={styles.editInput}
                                        type="number" step="0.0001" min="0" placeholder="0.0000"
                                        value={draft.amount}
                                        onChange={(e) => setTargetField(id, 'amount', e.target.value)}
                                      />
                                    ) : row.current ? (
                                      fmtAmount(row.current.amount, row.unit, currencyCode, currencyDigits)
                                    ) : (
                                      <span className={styles.missingText}>No rate set</span>
                                    )}
                                  </td>
                                  <td>
                                    {editing ? (
                                      <input
                                        className={styles.editDateInput} type="date"
                                        value={draft.effective_from}
                                        onChange={(e) => setTargetField(id, 'effective_from', e.target.value)}
                                      />
                                    ) : row.current ? row.current.effective_from : '—'}
                                  </td>
                                  <td>
                                    {row.pending && !editing && (
                                      <span className={styles.pendingBadge} style={{ marginRight: '0.4rem' }}>Pending</span>
                                    )}
                                    {!editing && (row.current
                                      ? <span className={styles.okIcon}>&#10003;</span>
                                      : <span className={styles.missingIcon}>&#9888;</span>)}
                                    {editing && <span className={styles.pendingBadge}>Editing</span>}
                                  </td>
                                  {canEditMatrix && (
                                    <td>
                                      {editing ? (
                                        <div className={styles.actionBtns}>
                                          <button className={styles.btnApprove}
                                            disabled={busy || targetSavingId != null || draft.amount === ''}
                                            onClick={() => void saveTargetRow(row, true)}>
                                            {busy ? 'Saving…' : 'Save & Approve'}
                                          </button>
                                          <button className={styles.btnSecondary}
                                            disabled={busy || targetSavingId != null || draft.amount === ''}
                                            onClick={() => void saveTargetRow(row, false)}>
                                            Save as Pending
                                          </button>
                                          <button className={styles.btnSecondary}
                                            disabled={busy}
                                            onClick={() => cancelTargetEdit(id)}>
                                            Cancel
                                          </button>
                                        </div>
                                      ) : (
                                        <button className={styles.btnSecondary}
                                          disabled={targetSavingId != null}
                                          onClick={() => startTargetEdit(row)}>
                                          Edit rate
                                        </button>
                                      )}
                                    </td>
                                  )}
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      ) : (
                        <div className={styles.emptySection}>
                          <p>This branch has no active pay items that need a rate.</p>
                          <p className={styles.emptySectionHint}>
                            Pay items are activated per branch under <strong>Settings &gt; Pay Items</strong>.
                          </p>
                        </div>
                      )}
                    </div>

                    {(statusMatrix?.groups.length ?? 0) > 0 && (
                      <div className={styles.section}>
                        <div className={styles.sectionHead}>
                          <span className={styles.sectionTitle}>Status Pay Rates</span>
                          {canEditMatrix && !statusEditMode && (
                            <button className={styles.btnSecondary} onClick={startStatusEdit}>
                              Edit Status Rates
                            </button>
                          )}
                        </div>
                        {statusSaveError && (
                          <div className={styles.errBanner} style={{ margin: '0.75rem 1.25rem 0' }}>{statusSaveError}</div>
                        )}
                        <table className={styles.rateTable}>
                          <thead>
                            <tr><th>Rate</th><th>Amount</th><th>Effective From</th><th>Status</th></tr>
                          </thead>
                          <tbody>
                            {statusMatrix!.groups.map((g) => {
                              const key = statusKey(g);
                              return (
                                <tr key={g.group_key}>
                                  <td><strong>{g.rate_name}</strong></td>
                                  <td>
                                    {statusEditMode ? (
                                      <input
                                        className={styles.editInput}
                                        type="number" step="0.0001" min="0" placeholder="0.0000"
                                        value={statusDrafts[key]?.amount ?? ''}
                                        onChange={(e) => setStatusField(key, 'amount', e.target.value)}
                                      />
                                    ) : g.current_rate ? (
                                      fmtAmount(g.current_rate.amount, g.unit_name, currencyCode, currencyDigits)
                                    ) : (
                                      <span className={styles.missingText}>No rate set</span>
                                    )}
                                  </td>
                                  <td>
                                    {statusEditMode ? (
                                      <input
                                        className={styles.editDateInput} type="date"
                                        value={statusDrafts[key]?.effective_from ?? today()}
                                        onChange={(e) => setStatusField(key, 'effective_from', e.target.value)}
                                      />
                                    ) : g.current_rate ? g.current_rate.effective_from : '—'}
                                  </td>
                                  <td>
                                    {g.pending_rate && !statusEditMode && (
                                      <span className={styles.pendingBadge} style={{ marginRight: '0.4rem' }}>Pending</span>
                                    )}
                                    {!statusEditMode && (g.current_rate
                                      ? <span className={styles.okIcon}>&#10003;</span>
                                      : <span className={styles.missingIcon}>&#9888;</span>)}
                                    {statusEditMode && isStatusDirty(key) && <span className={styles.pendingBadge}>Edited</span>}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                        {statusEditMode && (
                          <div className={styles.saveBar}>
                            <button className={styles.btnPrimary} onClick={() => void saveStatusRates()}
                              disabled={statusSaving || dirtyStatusKeys.length === 0}>
                              {statusSaving ? 'Saving...' : `Save ${dirtyStatusKeys.length} Status Rate Change${dirtyStatusKeys.length !== 1 ? 's' : ''}`}
                            </button>
                            <button className={styles.btnSecondary} onClick={cancelStatusEdit} disabled={statusSaving}>
                              Cancel
                            </button>
                            <span style={{ fontSize: '0.74rem', color: '#6b7280' }}>
                              Status rates are saved together on the temporary Status rate path, separately from Pay Item rates.
                            </span>
                          </div>
                        )}
                      </div>
                    )}
                  </>
                )}

                {activeTab === 'pending' && (
                  <div className={styles.section}>
                    <div className={styles.sectionHead}>
                      <span className={styles.sectionTitle}>Pending Changes</span>
                    </div>
                    {pendingActionError && <div className={styles.actionErr}>{pendingActionError}</div>}
                    {pendingCount === 0 ? (
                      <div className={styles.emptySection}>No pending rate changes for this driver.</div>
                    ) : (
                      <table className={styles.rateTable}>
                        <thead>
                          <tr>
                            <th>Pay Item</th><th>Current</th><th>Pending Amount</th><th>Effective From</th>
                            {canEditMatrix && <th>Actions</th>}
                          </tr>
                        </thead>
                        <tbody>
                          {pendingRows.map((row) => {
                            const pending = row.pending!;
                            const busy = actioningId === pending.driver_rate_assignment_id;
                            return (
                              <tr key={pending.driver_rate_assignment_id}>
                                <td><strong>{row.definition_name}</strong></td>
                                <td>
                                  {row.current
                                    ? fmtAmount(row.current.amount, row.unit, currencyCode, currencyDigits)
                                    : <span className={styles.missingText}>No current rate</span>}
                                </td>
                                <td>
                                  {pending.amount === null
                                    ? <span className={styles.missingText}>Not set</span>
                                    : <strong>{fmtAmount(pending.amount, row.unit, currencyCode, currencyDigits)}</strong>}
                                </td>
                                <td>{pending.effective_from}</td>
                                {canEditMatrix && (
                                  <td>
                                    <div className={styles.actionBtns}>
                                      <button className={styles.btnApprove}
                                        disabled={busy || actioningId != null || pending.amount === null}
                                        onClick={() => void runPendingAction(
                                          pending.driver_rate_assignment_id,
                                          () => approveAssignment(pending.driver_rate_assignment_id))}>
                                        {busy ? '…' : 'Approve'}
                                      </button>
                                      <button className={styles.btnVoid}
                                        disabled={busy || actioningId != null}
                                        onClick={() => void runPendingAction(
                                          pending.driver_rate_assignment_id,
                                          () => discardAssignment(pending.driver_rate_assignment_id))}>
                                        {busy ? '…' : 'Discard'}
                                      </button>
                                    </div>
                                  </td>
                                )}
                              </tr>
                            );
                          })}
                          {statusPending.map((g) => {
                            const rate = g.pending_rate!;
                            const busy = actioningId === rate.driver_rate_id;
                            return (
                              <tr key={`s${rate.driver_rate_id}`}>
                                <td><strong>{g.rate_name}</strong> <span className={styles.statusBadge}>Status pay</span></td>
                                <td>
                                  {g.current_rate
                                    ? fmtAmount(g.current_rate.amount, g.unit_name, currencyCode, currencyDigits)
                                    : <span className={styles.missingText}>No current rate</span>}
                                </td>
                                <td><strong>{fmtAmount(rate.amount, g.unit_name, currencyCode, currencyDigits)}</strong></td>
                                <td>{rate.effective_from}</td>
                                {canEditMatrix && (
                                  <td>
                                    <div className={styles.actionBtns}>
                                      <button className={styles.btnApprove} disabled={busy || actioningId != null}
                                        onClick={() => void runPendingAction(
                                          rate.driver_rate_id,
                                          () => apiClient.post(`/payroll/rates/${rate.driver_rate_id}/approve`))}>
                                        {busy ? '…' : 'Approve'}
                                      </button>
                                      <button className={styles.btnVoid} disabled={busy || actioningId != null}
                                        onClick={() => void runPendingAction(
                                          rate.driver_rate_id,
                                          () => apiClient.delete(`/payroll/rates/${rate.driver_rate_id}`))}>
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

                {activeTab === 'history' && (
                  <div className={styles.section}>
                    <div className={styles.sectionHead}>
                      <span className={styles.sectionTitle}>Rate History</span>
                    </div>
                    {loadingHistory ? (
                      <div className={styles.loading}>Loading history...</div>
                    ) : historyRows.length === 0 ? (
                      <div className={styles.emptySection}>No rate history for this driver.</div>
                    ) : (
                      <table className={styles.rateTable}>
                        <thead>
                          <tr>
                            <th>Pay Item</th><th>Amount</th><th>Effective</th><th>Status</th>
                            {canEditMatrix && <th>Actions</th>}
                          </tr>
                        </thead>
                        <tbody>
                          {historyRows.map((r) => (
                            <tr key={r.driver_rate_assignment_id}>
                              <td>
                                <strong>{r.definition_name}</strong>
                                {r.void_reason && (
                                  <div style={{ fontSize: '0.74rem', color: '#6b7280' }}>{r.void_reason}</div>
                                )}
                              </td>
                              <td>{fmtAmount(r.values[0]?.amount ?? null, r.unit, currencyCode, currencyDigits)}</td>
                              <td>{r.effective_from}{r.effective_to ? ` – ${r.effective_to}` : ''}</td>
                              <td><span className={statusBadgeClass(r.status)}>{r.status}</span></td>
                              {canEditMatrix && (
                                <td>
                                  {(r.status === 'Approved' || r.status === 'Superseded') && (
                                    <button className={styles.btnVoid}
                                      onClick={() => { setVoidTarget(r); setVoidReason(''); setVoidError(''); }}>
                                      Void
                                    </button>
                                  )}
                                </td>
                              )}
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
                                    {formatMoney(active.amount, user?.currency_code, user?.currency_minor_unit_digits)}<span className={styles.payRuleUnit}>/period</span>
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
                                      <label className={styles.formLabel}>Amount per period</label>
                                      <input
                                        className={styles.editInput}
                                        type="number"
                                        step="0.0001"
                                        min="0.0001"
                                        placeholder="e.g. 150.0000"
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
                                    The current amount is <strong>{formatMoney(active.amount, user?.currency_code, user?.currency_minor_unit_digits)}</strong>.
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
                                          <td>{formatMoney(r.amount, user?.currency_code, user?.currency_minor_unit_digits)}</td>
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

              {voidTarget && (
                <div className={styles.modalOverlay} onClick={() => { if (!voiding) setVoidTarget(null); }}>
                  <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                    <div className={styles.modalHead}>
                      <strong>Void rate</strong>
                      <button className={styles.modalClose} onClick={() => setVoidTarget(null)} disabled={voiding}>✕</button>
                    </div>
                    <div className={styles.modalBody}>
                      <p className={styles.modalDesc}>
                        Voiding the {voidTarget.status} rate for <strong>{voidTarget.definition_name}</strong>{' '}
                        ({voidTarget.effective_from}) removes it from rate resolution. It is kept as history.
                      </p>
                      <label className={styles.formLabel} htmlFor="void-reason">Reason</label>
                      <input id="void-reason" className={styles.editInput} style={{ width: '100%' }} type="text"
                        value={voidReason} onChange={(e) => setVoidReason(e.target.value)} disabled={voiding} />
                      {voidError && <div className={styles.actionErr}>{voidError}</div>}
                    </div>
                    <div className={styles.modalFoot}>
                      <button className={styles.btnPrimary} disabled={voiding || !voidReason.trim()}
                        onClick={() => void executeVoid()}>{voiding ? 'Voiding…' : 'Void Rate'}</button>
                      <button className={styles.btnSecondary} onClick={() => setVoidTarget(null)} disabled={voiding}>Cancel</button>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
