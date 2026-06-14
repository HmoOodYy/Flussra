import { useEffect, useState, useReducer, useCallback, useMemo, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';
import apiClient from '../../../lib/apiClient';
import { useAuth } from '../../../store/authStore';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import type {
  BranchAdmin,
  PayrollSetup,
  PayrollSetupUpsert,
  StatusKey,
  StatusKeyCreate,
} from '../../../types/settings';
import styles from './PayrollSetupPage.module.css';

// ─── Constants ────────────────────────────────────────────────────────────────

const MAX_DAYS_OFF = 2;

const FREQ_OPTIONS = [
  { value: 'Week',   label: 'Weekly (every 7 days)' },
  { value: 'Biweek', label: 'Bi-weekly (every 14 days)' },
  { value: 'Month',  label: 'Monthly' },
  { value: 'Custom', label: 'Custom' },
] as const;

const DAYS = [
  { label: 'Sun', full: 'Sunday',    bit: 0 },
  { label: 'Mon', full: 'Monday',    bit: 1 },
  { label: 'Tue', full: 'Tuesday',   bit: 2 },
  { label: 'Wed', full: 'Wednesday', bit: 3 },
  { label: 'Thu', full: 'Thursday',  bit: 4 },
  { label: 'Fri', full: 'Friday',    bit: 5 },
  { label: 'Sat', full: 'Saturday',  bit: 6 },
];

const ALLOWANCE_CATEGORIES = [
  'Vacation', 'Sick', 'Bereavement', 'Jury Duty', 'Personal', 'Other',
] as const;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })
    ?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  if (Array.isArray(d) && d.length > 0)
    return d
      .map((i: unknown) =>
        i && typeof i === 'object' && 'msg' in i
          ? String((i as { msg: unknown }).msg)
          : null,
      )
      .filter(Boolean)
      .join(' ');
  return 'An unexpected error occurred.';
}

function fmtDate(dt: string | null | undefined): string {
  if (!dt) return '—';
  return new Date(dt.includes('T') ? dt : dt + 'T00:00:00')
    .toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function isDayBit(mask: number | null, bit: number): boolean {
  return mask !== null && (mask & (1 << bit)) !== 0;
}

function toggleDayBit(mask: number | null, bit: number): number {
  return ((mask ?? 0) ^ (1 << bit)) & 0x7f;
}

function getDaysOffNames(mask: number | null): string[] {
  if (!mask) return [];
  return DAYS.filter(d => isDayBit(mask, d.bit)).map(d => d.full);
}

function freqLabel(f: string): string {
  const map: Record<string, string> = {
    Week: 'Weekly', Biweek: 'Bi-weekly', Month: 'Monthly', Custom: 'Custom',
  };
  return map[f] ?? f;
}

function countBits(mask: number | null): number {
  if (!mask) return 0;
  let m = mask & 0x7f;
  let n = 0;
  while (m > 0) { n += m & 1; m >>= 1; }
  return n;
}

function isSetupComplete(setup: PayrollSetup | null): boolean {
  return !!setup?.payroll_frequency && !!setup?.anchor_start_date;
}

/** Returns array of "start → end" strings for the next `count` periods. */
function computeUpcomingPeriods(
  anchorStr: string,
  frequency: string,
  count = 3,
  customIntervalDays?: number | null,
): Array<{ start: string; end: string }> {
  const anchor = new Date(anchorStr + 'T00:00:00');
  const today  = new Date(); today.setHours(0, 0, 0, 0);
  const fmt    = (d: Date) => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  const addDays = (d: Date, n: number) => new Date(d.getTime() + n * 86_400_000);
  const results: Array<{ start: string; end: string }> = [];

  if (frequency === 'Week' || frequency === 'Biweek') {
    const days = frequency === 'Week' ? 7 : 14;
    let curr = new Date(anchor);
    while (curr < today) curr = addDays(curr, days);
    for (let i = 0; i < count; i++) {
      results.push({ start: fmt(curr), end: fmt(addDays(curr, days - 1)) });
      curr = addDays(curr, days);
    }
  } else if (frequency === 'Month') {
    let y = anchor.getFullYear(), m = anchor.getMonth();
    const day = anchor.getDate();
    while (new Date(y, m, day) < today) { m++; if (m > 11) { m = 0; y++; } }
    for (let i = 0; i < count; i++) {
      const start = new Date(y, m, day);
      // end = next-same-day − 1
      let ny = y, nm = m + 1; if (nm > 11) { nm = 0; ny++; }
      const nextSameDay = new Date(ny, nm, Math.min(day, new Date(ny, nm + 1, 0).getDate()));
      const end = addDays(nextSameDay, -1);
      results.push({ start: fmt(start), end: fmt(end) });
      m++; if (m > 11) { m = 0; y++; }
    }
  } else if (frequency === 'Custom' && customIntervalDays && customIntervalDays > 0) {
    const days = customIntervalDays;
    let curr = new Date(anchor);
    for (let i = 0; i < count; i++) {
      results.push({ start: fmt(curr), end: fmt(addDays(curr, days - 1)) });
      curr = addDays(curr, days);
    }
  }
  return results;
}

// ─── Form types ───────────────────────────────────────────────────────────────

interface ScheduleForm {
  payroll_frequency: string;
  anchor_start_date: string;
  normal_days_off_mask: number | null;
  notes: string;
  /** Only relevant when payroll_frequency === 'Custom'. YYYY-MM-DD */
  first_custom_end_date: string;
}

const EMPTY_SCHEDULE: ScheduleForm = {
  payroll_frequency: 'Week',
  anchor_start_date: '',
  normal_days_off_mask: null,
  notes: '',
  first_custom_end_date: '',
};

/** Convert days+anchor back to a YYYY-MM-DD end date string (or ''). */
function intervalToEndDate(anchor: string, days: number | null | undefined): string {
  if (!anchor || !days || days <= 0) return '';
  const d = new Date(anchor + 'T00:00:00');
  d.setDate(d.getDate() + days - 1);
  return d.toISOString().slice(0, 10);
}

function setupToForm(s: PayrollSetup): ScheduleForm {
  return {
    payroll_frequency:    s.payroll_frequency,
    anchor_start_date:    s.anchor_start_date,
    normal_days_off_mask: s.normal_days_off_mask,
    notes:                s.notes ?? '',
    first_custom_end_date: intervalToEndDate(s.anchor_start_date, s.custom_interval_days),
  };
}

interface KeyForm {
  key_name: string;
  hours_value: string;
  is_off_reason: boolean;
  deducts_from_yearly_allowance: boolean;
  allowance_category: string;
  // Usage limits
  limit_uses_per_period_enabled: boolean;
  limit_uses_per_period: string;
  limit_uses_per_driver_enabled: boolean;
  limit_uses_per_driver: string;
  limit_uses_across_drivers_enabled: boolean;
  limit_uses_across_drivers: string;
  limit_uses_per_day_enabled: boolean;
  limit_uses_per_day: string;
}

const EMPTY_KEY_FORM: KeyForm = {
  key_name:                          '',
  hours_value:                       '0',
  is_off_reason:                     true,
  deducts_from_yearly_allowance:     false,
  allowance_category:                '',
  limit_uses_per_period_enabled:     false,
  limit_uses_per_period:             '',
  limit_uses_per_driver_enabled:     false,
  limit_uses_per_driver:             '',
  limit_uses_across_drivers_enabled: false,
  limit_uses_across_drivers:         '',
  limit_uses_per_day_enabled:        false,
  limit_uses_per_day:                '',
};

function keyToForm(k: StatusKey): KeyForm {
  return {
    key_name:                          k.key_name,
    hours_value:                       String(k.hours_value),
    is_off_reason:                     k.is_off_reason,
    deducts_from_yearly_allowance:     k.deducts_from_yearly_allowance,
    allowance_category:                k.allowance_category ?? '',
    limit_uses_per_period_enabled:     k.limit_uses_per_period_enabled,
    limit_uses_per_period:             k.limit_uses_per_period != null ? String(k.limit_uses_per_period) : '',
    limit_uses_per_driver_enabled:     k.limit_uses_per_driver_enabled,
    limit_uses_per_driver:             k.limit_uses_per_driver != null ? String(k.limit_uses_per_driver) : '',
    limit_uses_across_drivers_enabled: k.limit_uses_across_drivers_enabled,
    limit_uses_across_drivers:         k.limit_uses_across_drivers != null ? String(k.limit_uses_across_drivers) : '',
    limit_uses_per_day_enabled:        k.limit_uses_per_day_enabled,
    limit_uses_per_day:                k.limit_uses_per_day != null ? String(k.limit_uses_per_day) : '',
  };
}

// ─── Reducers for async data loading ─────────────────────────────────────────
// Using useReducer avoids calling multiple setState functions synchronously
// inside effect bodies (react-hooks/set-state-in-effect).

// editing is included in SetupLoadState so FETCH_START can reset it
// in a single dispatch rather than a separate setState call in the effect.
type SetupLoadState = {
  data: PayrollSetup | null;
  loading: boolean;
  error: string;
  editing: boolean;
};
type SetupLoadAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    data: PayrollSetup }
  | { type: 'FETCH_EMPTY' }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'SAVED';       data: PayrollSetup }
  | { type: 'EDIT_START' }
  | { type: 'EDIT_CANCEL' };

function setupLoadReducer(s: SetupLoadState, a: SetupLoadAction): SetupLoadState {
  switch (a.type) {
    case 'FETCH_START':  return { data: null,   loading: true,  error: '',      editing: false };
    case 'FETCH_OK':     return { data: a.data, loading: false, error: '',      editing: s.editing };
    case 'FETCH_EMPTY':  return { data: null,   loading: false, error: '',      editing: false };
    case 'FETCH_ERROR':  return { data: null,   loading: false, error: a.error, editing: false };
    case 'SAVED':        return { data: a.data, loading: false, error: '',      editing: false };
    case 'EDIT_START':   return { ...s, editing: true };
    case 'EDIT_CANCEL':  return { ...s, editing: false };
  }
}

type KeysLoadState = { keys: StatusKey[]; loading: boolean; error: string; selectedKeyId: number | null };
type KeysLoadAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    keys: StatusKey[] }
  | { type: 'FETCH_ERROR'; error: string }
  | { type: 'SELECT';      id: number | null };

function keysLoadReducer(s: KeysLoadState, a: KeysLoadAction): KeysLoadState {
  switch (a.type) {
    case 'FETCH_START': return { keys: [],      loading: true,  error: '', selectedKeyId: null };
    case 'FETCH_OK':    return { keys: a.keys,  loading: false, error: '', selectedKeyId: s.selectedKeyId };
    case 'FETCH_ERROR': return { keys: [],      loading: false, error: a.error, selectedKeyId: null };
    case 'SELECT':      return { ...s, selectedKeyId: a.id };
  }
}

// ─── Main component ───────────────────────────────────────────────────────────

export function PayrollSetupPage() {
  const { user } = useAuth();
  // Backend requires AllCompanyBranches scope + setup.manage (PAYROLL_ADMIN role)
  const canEdit = user?.scope_type === 'AllCompanyBranches' && user?.has_setup_manage === true;

  // ── URL params (deep-link from Company & Branches "Missing" badge) ────────
  const [searchParams] = useSearchParams();

  // Read branchId param once at mount into a ref so it can be consumed by the
  // branch-load effect without becoming a reactive dependency (avoids re-runs).
  const urlBranchId = Number(searchParams.get('branchId'));
  const urlBranchIdRef = useRef<number | null>(urlBranchId > 0 ? urlBranchId : null);

  // ── Branches ──────────────────────────────────────────────────────────────
  const [branches, setBranches]             = useState<BranchAdmin[]>([]);
  const [branchesLoading, setBranchesLoading] = useState(true);
  const [selectedBranchId, setSelectedBranchId] = useState<number | null>(null);

  // ── Active tab — initialised from ?tab= param if present ─────────────────
  const [activeTab, setActiveTab] = useState<'schedule' | 'status-keys'>(() => {
    const t = searchParams.get('tab');
    return t === 'status-keys' ? 'status-keys' : 'schedule';
  });

  // ── Payroll Setup (loaded data via reducer; interaction state via useState) ─
  const [setupState, dispatchSetup] = useReducer(setupLoadReducer, {
    data: null, loading: false, error: '', editing: false,
  });
  const setup        = setupState.data;
  const setupLoading = setupState.loading;
  const setupError   = setupState.error;
  const editing      = setupState.editing;

  const [form, setForm]       = useState<ScheduleForm>(EMPTY_SCHEDULE);
  const [saving, setSaving]         = useState(false);
  const [saveErr, setSaveErr]       = useState('');
  const [daysOffErr, setDaysOffErr] = useState('');
  const [toast, setToast]           = useState('');

  // ── Confirmation dialog ───────────────────────────────────────────────────
  type ConfirmKind =
    | 'save-schedule'
    | 'leave-unsaved'
    | 'save-key'
    | 'deactivate-key'
    | 'reactivate-key';
  const [confirmKind,  setConfirmKind]  = useState<ConfirmKind | null>(null);
  const [confirming,   setConfirming]   = useState(false);
  const [pendingBranchId, setPendingBranchId] = useState<number | null>(null);

  // ── Status Keys (loaded data via reducer; modal state via useState) ────────
  const [keysState, dispatchKeys] = useReducer(keysLoadReducer, {
    keys: [], loading: false, error: '', selectedKeyId: null,
  });
  const statusKeys    = keysState.keys;
  const keysLoading   = keysState.loading;
  const keysError     = keysState.error;
  const selectedKeyId = keysState.selectedKeyId;

  const [keyStatusFilter, setKeyStatusFilter] = useState<'Active' | 'Inactive'>('Active');
  const [keyModal, setKeyModal]               = useState<'create' | 'edit' | null>(null);
  const [editingKey, setEditingKey]     = useState<StatusKey | null>(null);
  const [keyForm, setKeyForm]           = useState<KeyForm>(EMPTY_KEY_FORM);
  const [savingKey, setSavingKey]       = useState(false);
  const [keySaveErr, setKeySaveErr]     = useState('');
  const [deactivating, setDeactivating] = useState<number | null>(null);
  const [targetKey,    setTargetKey]    = useState<StatusKey | null>(null);

  // ── Toast ─────────────────────────────────────────────────────────────────
  const showToast = useCallback((msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(''), 4000);
  }, []);

  // ── Load branches ─────────────────────────────────────────────────────────
  useEffect(() => {
    apiClient.get<BranchAdmin[]>('/settings/branches')
      .then(({ data }) => {
        setBranches(data);
        setBranchesLoading(false);
        // If a branchId was supplied in the URL, select that branch;
        // fall back to the first branch if the param is missing or invalid.
        const urlId = urlBranchIdRef.current;
        const target = urlId ? (data.find(b => b.branch_id === urlId) ?? null) : null;
        setSelectedBranchId(target ? target.branch_id : (data[0]?.branch_id ?? null));
      })
      .catch(() => setBranchesLoading(false));
  }, []);

  // ── Load payroll setup when branch changes ────────────────────────────────
  // Single dispatchSetup call (no sync setState in effect body — lint-safe).
  useEffect(() => {
    if (!selectedBranchId) return;
    dispatchSetup({ type: 'FETCH_START' }); // resets editing + loading in one dispatch
    apiClient.get<PayrollSetup>(`/settings/branches/${selectedBranchId}/payroll-setup`)
      .then(({ data }) => dispatchSetup({ type: 'FETCH_OK', data }))
      .catch((e) => {
        const httpStatus = (e as { response?: { status?: number } })?.response?.status;
        if (httpStatus === 404) dispatchSetup({ type: 'FETCH_EMPTY' });
        else dispatchSetup({ type: 'FETCH_ERROR', error: apiError(e) });
      });
  }, [selectedBranchId]);

  // ── Load status keys ──────────────────────────────────────────────────────
  // Single dispatchKeys call (lint-safe).
  const includeInactiveKeys = keyStatusFilter === 'Inactive';

  useEffect(() => {
    if (!selectedBranchId) return;
    dispatchKeys({ type: 'FETCH_START' }); // also resets selectedKeyId via reducer
    apiClient.get<StatusKey[]>(`/settings/branches/${selectedBranchId}/status-keys`, {
      params: { include_inactive: includeInactiveKeys },
    })
      .then(({ data }) => dispatchKeys({ type: 'FETCH_OK', keys: data }))
      .catch((e) => dispatchKeys({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, [selectedBranchId, includeInactiveKeys]);

  // ── Save payroll setup ────────────────────────────────────────────────────
  /** Validate form fields — return error string or empty string if ok. */
  function validateScheduleForm(): string {
    if (!form.payroll_frequency) return 'Payroll frequency is required.';
    if (!form.anchor_start_date) return 'Anchor start date is required.';
    if (form.payroll_frequency === 'Custom') {
      if (!form.first_custom_end_date)
        return 'First period end date is required for Custom frequency.';
      const anchor = new Date(form.anchor_start_date + 'T00:00:00');
      const endDt  = new Date(form.first_custom_end_date + 'T00:00:00');
      if (endDt < anchor)
        return 'First period end date must be on or after the anchor start date.';
    }
    if (countBits(form.normal_days_off_mask) > MAX_DAYS_OFF)
      return `Normal days off cannot exceed ${MAX_DAYS_OFF} days. Please reduce your selection.`;
    return '';
  }

  /** Request confirmation before saving. Validates first so errors show without a dialog. */
  function requestSaveSchedule() {
    const err = validateScheduleForm();
    if (err) { setSaveErr(err); return; }
    setSaveErr('');
    setConfirmKind('save-schedule');
  }

  async function saveSetup() {
    if (!selectedBranchId) return;
    setSaving(true); setSaveErr('');
    try {
      const payload: PayrollSetupUpsert = {
        payroll_frequency:    form.payroll_frequency,
        anchor_start_date:    form.anchor_start_date,
        normal_days_off_mask: form.normal_days_off_mask,
        notes:                form.notes.trim() || null,
        ...(form.payroll_frequency === 'Custom' && form.first_custom_end_date
          ? { first_custom_end_date: form.first_custom_end_date }
          : {}),
      };
      const { data } = await apiClient.put<PayrollSetup>(
        `/settings/branches/${selectedBranchId}/payroll-setup`, payload);
      dispatchSetup({ type: 'SAVED', data });
      showToast('Payroll schedule saved.');
      apiClient.get<BranchAdmin[]>('/settings/branches')
        .then(({ data: bs }) => setBranches(bs)).catch(() => {});
    } catch (e) { setSaveErr(apiError(e)); }
    finally { setSaving(false); }
  }

  function startEdit() {
    setForm(setup ? setupToForm(setup) : { ...EMPTY_SCHEDULE });
    setSaveErr(''); setDaysOffErr('');
    dispatchSetup({ type: 'EDIT_START' });
  }

  function cancelEdit() {
    dispatchSetup({ type: 'EDIT_CANCEL' });
    setSaveErr(''); setDaysOffErr('');
  }

  // ── Branch change with unsaved guard ──────────────────────────────────────
  function selectBranch(id: number) {
    if (editing && id !== selectedBranchId) {
      setPendingBranchId(id);
      setConfirmKind('leave-unsaved');
    } else {
      setSelectedBranchId(id);
    }
  }

  // ── Unified confirm handler ───────────────────────────────────────────────
  async function handleConfirm() {
    if (!confirmKind) return;
    setConfirming(true);
    try {
      if (confirmKind === 'save-schedule') {
        await saveSetup();
      } else if (confirmKind === 'leave-unsaved') {
        dispatchSetup({ type: 'EDIT_CANCEL' });
        setSelectedBranchId(pendingBranchId);
        setPendingBranchId(null);
      } else if (confirmKind === 'save-key') {
        await saveKey();
      } else if (confirmKind === 'deactivate-key' && targetKey) {
        await deactivateKey(targetKey);
      } else if (confirmKind === 'reactivate-key' && targetKey) {
        await reactivateKey(targetKey);
      }
    } finally {
      setConfirming(false);
      setConfirmKind(null);
    }
  }

  // ── Status key CRUD ───────────────────────────────────────────────────────
  function openCreateKey() {
    setKeyForm(EMPTY_KEY_FORM); setKeySaveErr('');
    setEditingKey(null); setKeyModal('create');
  }

  function openEditKey(k: StatusKey) {
    setKeyForm(keyToForm(k)); setKeySaveErr('');
    setEditingKey(k); setKeyModal('edit');
  }

  function closeKeyModal() {
    setKeyModal(null); setEditingKey(null); setKeySaveErr('');
  }

  /** Validate key form then open confirm dialog (actual save via handleConfirm). */
  function requestSaveKey() {
    if (!keyForm.key_name.trim()) { setKeySaveErr('Key Name is required.'); return; }
    const hrs = parseFloat(keyForm.hours_value);
    if (isNaN(hrs) || hrs < 0 || hrs > 24) {
      setKeySaveErr('Hours value must be between 0 and 24.'); return;
    }
    if (keyForm.deducts_from_yearly_allowance && !keyForm.is_off_reason) {
      setKeySaveErr('Deducts from allowance requires "Can be used as leave/off reason" to be enabled.'); return;
    }
    if (keyForm.deducts_from_yearly_allowance && !keyForm.allowance_category) {
      setKeySaveErr('Allowance category is required when deducting from yearly allowance.'); return;
    }
    // Validate usage limits
    if (keyForm.limit_uses_per_period_enabled) {
      const v = parseInt(keyForm.limit_uses_per_period);
      if (isNaN(v) || v <= 0) { setKeySaveErr('Limit per period must be a positive number when enabled.'); return; }
    }
    if (keyForm.limit_uses_per_driver_enabled) {
      const v = parseInt(keyForm.limit_uses_per_driver);
      if (isNaN(v) || v <= 0) { setKeySaveErr('Limit per driver must be a positive number when enabled.'); return; }
    }
    if (keyForm.limit_uses_across_drivers_enabled) {
      const v = parseInt(keyForm.limit_uses_across_drivers);
      if (isNaN(v) || v <= 0) { setKeySaveErr('Limit across all drivers must be a positive number when enabled.'); return; }
    }
    if (keyForm.limit_uses_per_day_enabled) {
      const v = parseInt(keyForm.limit_uses_per_day);
      if (isNaN(v) || v <= 0) { setKeySaveErr('Limit per day must be a positive number when enabled.'); return; }
    }
    setKeySaveErr('');
    setConfirmKind('save-key');
  }

  async function saveKey() {
    if (!selectedBranchId) return;
    const hrs = parseFloat(keyForm.hours_value);
    setSavingKey(true); setKeySaveErr('');
    try {
      // Helper to parse a usage limit value string
      const parseLimitVal = (s: string): number | null => {
        const v = parseInt(s);
        return isNaN(v) || v <= 0 ? null : v;
      };

      if (keyModal === 'create') {
        const payload: StatusKeyCreate = {
          key_name:                          keyForm.key_name.trim(),
          hours_value:                       hrs,
          is_off_reason:                     keyForm.is_off_reason,
          deducts_from_yearly_allowance:     keyForm.deducts_from_yearly_allowance,
          allowance_category:                keyForm.deducts_from_yearly_allowance ? (keyForm.allowance_category || null) : null,
          is_active:                         true,
          limit_uses_per_period_enabled:     keyForm.limit_uses_per_period_enabled,
          limit_uses_per_period:             keyForm.limit_uses_per_period_enabled ? parseLimitVal(keyForm.limit_uses_per_period) : null,
          limit_uses_per_driver_enabled:     keyForm.limit_uses_per_driver_enabled,
          limit_uses_per_driver:             keyForm.limit_uses_per_driver_enabled ? parseLimitVal(keyForm.limit_uses_per_driver) : null,
          limit_uses_across_drivers_enabled: keyForm.limit_uses_across_drivers_enabled,
          limit_uses_across_drivers:         keyForm.limit_uses_across_drivers_enabled ? parseLimitVal(keyForm.limit_uses_across_drivers) : null,
          limit_uses_per_day_enabled:        keyForm.limit_uses_per_day_enabled,
          limit_uses_per_day:                keyForm.limit_uses_per_day_enabled ? parseLimitVal(keyForm.limit_uses_per_day) : null,
        };
        const { data } = await apiClient.post<StatusKey>(
          `/settings/branches/${selectedBranchId}/status-keys`, payload);
        dispatchKeys({
          type: 'FETCH_OK',
          keys: [...statusKeys, data].sort((a, b) => a.key_name.localeCompare(b.key_name)),
        });
        showToast(`Status key "${data.key_name}" created.`);
      } else if (editingKey) {
        const patchPayload: Record<string, unknown> = {
          key_name:                          keyForm.key_name.trim(),
          hours_value:                       hrs,
          is_off_reason:                     keyForm.is_off_reason,
          deducts_from_yearly_allowance:     keyForm.deducts_from_yearly_allowance,
          limit_uses_per_period_enabled:     keyForm.limit_uses_per_period_enabled,
          limit_uses_per_period:             keyForm.limit_uses_per_period_enabled ? parseLimitVal(keyForm.limit_uses_per_period) : null,
          limit_uses_per_driver_enabled:     keyForm.limit_uses_per_driver_enabled,
          limit_uses_per_driver:             keyForm.limit_uses_per_driver_enabled ? parseLimitVal(keyForm.limit_uses_per_driver) : null,
          limit_uses_across_drivers_enabled: keyForm.limit_uses_across_drivers_enabled,
          limit_uses_across_drivers:         keyForm.limit_uses_across_drivers_enabled ? parseLimitVal(keyForm.limit_uses_across_drivers) : null,
          limit_uses_per_day_enabled:        keyForm.limit_uses_per_day_enabled,
          limit_uses_per_day:                keyForm.limit_uses_per_day_enabled ? parseLimitVal(keyForm.limit_uses_per_day) : null,
        };
        if (keyForm.deducts_from_yearly_allowance) {
          patchPayload.allowance_category = keyForm.allowance_category || null;
        }
        const { data } = await apiClient.patch<StatusKey>(
          `/settings/branches/${selectedBranchId}/status-keys/${editingKey.status_key_id}`,
          patchPayload,
        );
        dispatchKeys({
          type: 'FETCH_OK',
          keys: statusKeys
            .map(k => k.status_key_id === data.status_key_id ? data : k)
            .sort((a, b) => a.key_name.localeCompare(b.key_name)),
        });
        showToast(`Status key "${data.key_name}" updated.`);
      }
      closeKeyModal();
    } catch (e) { setKeySaveErr(apiError(e)); }
    finally { setSavingKey(false); }
  }

  function requestDeactivate(k: StatusKey) { setTargetKey(k); setConfirmKind('deactivate-key'); }
  function requestReactivate(k: StatusKey) { setTargetKey(k); setConfirmKind('reactivate-key'); }

  async function deactivateKey(k: StatusKey) {
    if (!selectedBranchId) return;
    setDeactivating(k.status_key_id);
    try {
      const { data } = await apiClient.delete<StatusKey>(
        `/settings/branches/${selectedBranchId}/status-keys/${k.status_key_id}`);
      dispatchKeys({
        type: 'FETCH_OK',
        keys: includeInactiveKeys
          ? statusKeys.map(sk => sk.status_key_id === data.status_key_id ? data : sk)
          : statusKeys.filter(sk => sk.status_key_id !== data.status_key_id),
      });
      showToast(`"${k.key_name}" deactivated.`);
    } catch (e) { showToast(apiError(e)); }
    finally { setDeactivating(null); }
  }

  async function reactivateKey(k: StatusKey) {
    if (!selectedBranchId) return;
    setDeactivating(k.status_key_id);
    try {
      const { data } = await apiClient.patch<StatusKey>(
        `/settings/branches/${selectedBranchId}/status-keys/${k.status_key_id}`,
        { is_active: true });
      dispatchKeys({
        type: 'FETCH_OK',
        keys: statusKeys.map(sk => sk.status_key_id === data.status_key_id ? data : sk),
      });
      showToast(`"${k.key_name}" reactivated.`);
    } catch (e) { showToast(apiError(e)); }
    finally { setDeactivating(null); }
  }

  // ── Derived state ─────────────────────────────────────────────────────────
  const selectedBranch = useMemo(
    () => branches.find(b => b.branch_id === selectedBranchId) ?? null,
    [branches, selectedBranchId],
  );

  const selectedKey = useMemo(
    () => statusKeys.find(k => k.status_key_id === selectedKeyId) ?? null,
    [statusKeys, selectedKeyId],
  );

  // P5: when editing, preview reflects the live form — not the persisted setup.
  const previewFreq    = editing ? form.payroll_frequency : setup?.payroll_frequency ?? '';
  const previewAnchor  = editing ? form.anchor_start_date  : setup?.anchor_start_date ?? '';
  const previewMask    = editing ? form.normal_days_off_mask : setup?.normal_days_off_mask ?? null;
  // For Custom: derive interval from first_custom_end_date when editing; use saved value when reading.
  const previewCustomInterval: number | null = useMemo(() => {
    if (previewFreq !== 'Custom') return null;
    if (editing && form.first_custom_end_date && form.anchor_start_date) {
      const anchor = new Date(form.anchor_start_date + 'T00:00:00');
      const end    = new Date(form.first_custom_end_date + 'T00:00:00');
      const days   = Math.round((end.getTime() - anchor.getTime()) / 86_400_000) + 1;
      return days > 0 ? days : null;
    }
    return setup?.custom_interval_days ?? null;
  }, [previewFreq, editing, form.anchor_start_date, form.first_custom_end_date, setup]);

  const isCustomSetupComplete = previewFreq === 'Custom'
    ? (previewCustomInterval !== null && previewCustomInterval > 0)
    : true;

  const setupComplete = editing
    ? (!!form.payroll_frequency && !!form.anchor_start_date && isCustomSetupComplete)
    : isSetupComplete(setup) && (setup?.payroll_frequency !== 'Custom' || !!setup?.custom_interval_days);

  const hasPreviewData = !!previewFreq && !!previewAnchor;

  const daysOffNames = useMemo(() => getDaysOffNames(previewMask), [previewMask]);

  const upcomingPeriods = useMemo(
    () => hasPreviewData
      ? computeUpcomingPeriods(previewAnchor, previewFreq, 3, previewCustomInterval)
      : [],
    [hasPreviewData, previewAnchor, previewFreq, previewCustomInterval],
  );

  // ─────────────────────────────────────────────────────────────────────────
  return (
    <div className={styles.page}>

      {/* Page header */}
      <div className={styles.pageHeader}>
        <div>
          <h1 className={styles.pageTitle}>Payroll Setup</h1>
          <p className={styles.pageSubtitle}>
            Configure branch payroll schedule and payroll entry status keys.
          </p>
        </div>
      </div>

      {/* Toast */}
      {toast && <div className={styles.successAlert}><CheckIcon /> {toast}</div>}

      {/* Branch context card */}
      <div className={styles.branchCtx}>
        <div className={styles.branchCtxLeft}>
          <span className={styles.branchCtxLabel}><BranchIcon /> Branch</span>
          {branchesLoading ? (
            <span className={styles.branchCtxMuted}>Loading…</span>
          ) : branches.length === 0 ? (
            <span className={styles.branchCtxMuted}>No branches available</span>
          ) : canEdit && branches.length > 1 ? (
            <select
              className={styles.branchSelect}
              value={selectedBranchId ?? ''}
              onChange={e => selectBranch(Number(e.target.value))}
            >
              {branches.map(b => (
                <option key={b.branch_id} value={b.branch_id}>
                  {b.branch_name} ({b.branch_code})
                </option>
              ))}
            </select>
          ) : (
            <span className={styles.branchCtxValue}>
              {selectedBranch?.branch_name ?? '—'}
              {selectedBranch?.branch_code && (
                <span className={styles.branchCodePill}>{selectedBranch.branch_code}</span>
              )}
            </span>
          )}
        </div>
        <div className={styles.branchCtxRight}>
          {setupLoading ? (
            <span className={styles.chipNeutral}>Loading…</span>
          ) : !setup ? (
            <span className={styles.chipMissing}><WarnIcon /> Not Configured</span>
          ) : setupComplete ? (
            <span className={styles.chipComplete}><CheckIcon /> Setup Complete</span>
          ) : (
            <span className={styles.chipWarn}><WarnIcon /> Needs Attention</span>
          )}
          {setup?.updated_at_utc && (
            <span className={styles.lastUpdated}>Updated {fmtDate(setup.updated_at_utc)}</span>
          )}
        </div>
      </div>

      {/* Internal tab bar */}
      <div className={styles.tabs}>
        <button
          className={`${styles.tab}${activeTab === 'schedule' ? ` ${styles.tabActive}` : ''}`}
          onClick={() => setActiveTab('schedule')}
        >
          <CalendarTabIcon /> Pay Schedule
        </button>
        <button
          className={`${styles.tab}${activeTab === 'status-keys' ? ` ${styles.tabActive}` : ''}`}
          onClick={() => setActiveTab('status-keys')}
        >
          <KeyIcon /> Status Keys
          {!keysLoading && statusKeys.length > 0 && (
            <span className={styles.tabCount}>{statusKeys.length}</span>
          )}
        </button>
      </div>

      {/* ══ PAY SCHEDULE TAB ══ */}
      {activeTab === 'schedule' && (
        <div className={styles.tabContent}>
          {setupError ? (
            <div className={styles.errorAlert}><AlertIcon /> {setupError}</div>
          ) : setupLoading ? (
            <div className={styles.loadingCard}><SkeletonBlock /></div>
          ) : (
            <div className={styles.scheduleBody}>

              {/* LEFT — form / read view */}
              <div className={styles.formCard}>
                <div className={styles.formCardHeader}>
                  <h2 className={styles.cardTitle}>Schedule Configuration</h2>
                  <div className={styles.formCardActions}>
                    {!canEdit && <span className={styles.readonlyNote}><LockIcon /> Read-only</span>}
                    {canEdit && !editing && (
                      <button className={styles.btnGhost} onClick={startEdit}>
                        <EditIcon /> {setup ? 'Edit' : 'Configure'}
                      </button>
                    )}
                  </div>
                </div>

                {editing ? (
                  <div className={styles.formBody}>
                    <div className={styles.formGroup}>
                      <label className={styles.label}>
                        Payroll Frequency <span className={styles.required}>*</span>
                      </label>
                      <select
                        className={styles.select}
                        value={form.payroll_frequency}
                        onChange={e => setForm(f => ({ ...f, payroll_frequency: e.target.value }))}
                        disabled={saving}
                      >
                        {FREQ_OPTIONS.map(o => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </select>
                    </div>

                    <div className={styles.formGroup}>
                      <label className={styles.label}>
                        Anchor Start Date <span className={styles.required}>*</span>
                      </label>
                      <p className={styles.fieldHint}>
                        Reference date from which all periods are calculated.
                      </p>
                      <input
                        type="date"
                        className={styles.input}
                        value={form.anchor_start_date}
                        onChange={e => setForm(f => ({ ...f, anchor_start_date: e.target.value }))}
                        disabled={saving}
                      />
                    </div>

                    {form.payroll_frequency === 'Custom' && (
                      <div className={styles.formGroup}>
                        <label className={styles.label}>
                          First Period End Date <span className={styles.required}>*</span>
                        </label>
                        <p className={styles.fieldHint}>
                          The end date of the very first payroll period.
                          The system derives the cycle length from this date and repeats it automatically.
                        </p>
                        <input
                          type="date"
                          className={styles.input}
                          value={form.first_custom_end_date}
                          min={form.anchor_start_date || undefined}
                          onChange={e => setForm(f => ({ ...f, first_custom_end_date: e.target.value }))}
                          disabled={saving}
                        />
                        {previewCustomInterval !== null && previewCustomInterval > 0 && (
                          <p className={styles.fieldHint} style={{ marginTop: '0.35rem', color: '#2563eb' }}>
                            Cycle length: <strong>{previewCustomInterval} day{previewCustomInterval !== 1 ? 's' : ''}</strong>
                          </p>
                        )}
                      </div>
                    )}

                    <div className={styles.formGroup}>
                      <label className={styles.label}>
                        Normal Days Off
                        <span className={styles.fieldLimit}> (max {MAX_DAYS_OFF})</span>
                      </label>
                      <p className={styles.fieldHint}>
                        Standard rest days for this branch. Used in schedule previews.
                      </p>
                      <div className={styles.dayChips}>
                        {DAYS.map(d => {
                          const isOn = isDayBit(form.normal_days_off_mask, d.bit);
                          const wouldExceed = !isOn && countBits(form.normal_days_off_mask) >= MAX_DAYS_OFF;
                          return (
                            <button
                              key={d.bit}
                              type="button"
                              className={`${styles.dayChip}${isOn ? ` ${styles.dayChipActive}` : ''}${wouldExceed ? ` ${styles.dayChipDisabled}` : ''}`}
                              onClick={() => {
                                if (wouldExceed) {
                                  setDaysOffErr(`You can select up to ${MAX_DAYS_OFF} normal days off.`);
                                  return;
                                }
                                setDaysOffErr('');
                                setForm(f => ({
                                  ...f,
                                  normal_days_off_mask: toggleDayBit(f.normal_days_off_mask, d.bit),
                                }));
                              }}
                              disabled={saving}
                              title={wouldExceed ? `Max ${MAX_DAYS_OFF} days off` : undefined}
                            >
                              {d.label}
                            </button>
                          );
                        })}
                      </div>
                      {daysOffErr && (
                        <span className={styles.fieldError}>{daysOffErr}</span>
                      )}
                      {countBits(form.normal_days_off_mask) > MAX_DAYS_OFF && (
                        <div className={styles.daysOffWarn}>
                          <WarnIcon /> More than {MAX_DAYS_OFF} days are currently selected. Reduce to {MAX_DAYS_OFF} or fewer before saving.
                        </div>
                      )}
                    </div>

                    <div className={styles.formGroup}>
                      <label className={styles.label}>Notes</label>
                      <textarea
                        className={styles.textarea}
                        value={form.notes}
                        onChange={e => setForm(f => ({ ...f, notes: e.target.value }))}
                        disabled={saving}
                        placeholder="Optional internal notes"
                        rows={3}
                        maxLength={1000}
                      />
                    </div>

                    {saveErr && (
                      <div className={styles.errorAlert} style={{ marginTop: '0.5rem' }}>
                        <AlertIcon /> {saveErr}
                      </div>
                    )}

                    <div className={styles.formActions}>
                      <button className={styles.btnPrimary} onClick={requestSaveSchedule} disabled={saving}>
                        {saving ? <><SpinnerIcon /> Saving…</> : 'Save Schedule'}
                      </button>
                      <button className={styles.btnSecondary} onClick={cancelEdit} disabled={saving}>
                        Cancel
                      </button>
                    </div>
                  </div>

                ) : setup ? (
                  <div className={styles.readView}>
                    <ReadRow label="Payroll Frequency" value={freqLabel(setup.payroll_frequency)} />
                    <ReadRow label="Anchor Start Date"  value={fmtDate(setup.anchor_start_date)} />
                    {setup.payroll_frequency === 'Custom' && (
                      <ReadRow
                        label="Cycle Length"
                        value={setup.custom_interval_days
                          ? `${setup.custom_interval_days} day${setup.custom_interval_days !== 1 ? 's' : ''}`
                          : 'Not set'}
                      />
                    )}
                    <ReadRow
                      label="Normal Days Off"
                      value={daysOffNames.length > 0 ? daysOffNames.join(', ') : 'None set'}
                    />
                    {setup.notes && <ReadRow label="Notes" value={setup.notes} wide />}
                    <div className={styles.readMeta}>
                      <span>Configured {fmtDate(setup.created_at_utc)}</span>
                      {setup.updated_at_utc && (
                        <span>· Last saved {fmtDate(setup.updated_at_utc)}</span>
                      )}
                    </div>
                  </div>

                ) : (
                  <div className={styles.notConfigured}>
                    <CalendarTabIcon />
                    <span className={styles.notConfiguredTitle}>No schedule configured</span>
                    <span className={styles.notConfiguredDesc}>
                      Set up a payroll frequency and anchor date to enable period
                      generation for this branch.
                    </span>
                    {canEdit && (
                      <button
                        className={styles.btnPrimary}
                        onClick={startEdit}
                        style={{ marginTop: '1.25rem' }}
                      >
                        <PlusIcon /> Configure Schedule
                      </button>
                    )}
                  </div>
                )}
              </div>

              {/* RIGHT — preview card */}
              <aside className={styles.previewCard}>
                <div className={styles.previewHeader}>
                  <h2 className={styles.cardTitle}>Schedule Preview</h2>
                </div>
                <div className={styles.previewBody}>
                  {!hasPreviewData ? (
                    <div className={styles.previewEmpty}>
                      Configure a schedule to see a preview here.
                    </div>
                  ) : (
                    <>
                      <PreviewRow label="Frequency"   value={freqLabel(previewFreq)} />
                      <PreviewRow label="Anchor Date" value={fmtDate(previewAnchor)} />
                      {previewFreq === 'Custom' && (
                        <PreviewRow
                          label="Cycle Length"
                          value={previewCustomInterval && previewCustomInterval > 0
                            ? `${previewCustomInterval} day${previewCustomInterval !== 1 ? 's' : ''}`
                            : 'Enter first period end date'}
                        />
                      )}
                      <PreviewRow
                        label="Days Off"
                        value={daysOffNames.length > 0 ? daysOffNames.join(', ') : 'None'}
                      />

                      {upcomingPeriods.length > 0 && (
                        <div className={styles.previewSection}>
                          <span className={styles.previewSectionLabel}>
                            {previewFreq === 'Custom' ? 'Example Periods (preview only)' : 'Upcoming Periods'}
                          </span>
                          {upcomingPeriods.map((p, i) => (
                            <div key={i} className={styles.previewPeriodRow}>
                              <span className={styles.previewPeriodIdx}>#{i + 1}</span>
                              <span className={styles.previewPeriodDate}>{p.start} → {p.end}</span>
                            </div>
                          ))}
                          {previewFreq === 'Custom' && (
                            <p style={{ fontSize: '0.75rem', color: '#6b7280', marginTop: '0.5rem', lineHeight: 1.4 }}>
                              These are example dates for illustration only. Actual payroll periods are created one at a time from the Payroll module.
                            </p>
                          )}
                        </div>
                      )}

                      <div className={`${styles.previewStatus} ${setupComplete ? styles.previewStatusOk : styles.previewStatusWarn}`}>
                        {setupComplete
                          ? <><CheckIcon /> Setup complete</>
                          : <><WarnIcon /> Missing required fields</>}
                      </div>
                    </>
                  )}
                </div>
              </aside>

            </div>
          )}
        </div>
      )}

      {/* ══ STATUS KEYS TAB ══ */}
      {activeTab === 'status-keys' && (
        <div className={styles.statusKeysSection}>

          {/* Header row */}
          <div className={styles.sectionHeader}>
            <div className={styles.sectionLeft}>
              <h2 className={styles.sectionTitle}>Status Keys</h2>
              {!keysLoading && statusKeys.length > 0 && (
                <span className={styles.countChip}>{statusKeys.length}</span>
              )}
              {/* Segmented status filter replaces the old checkbox */}
              <div className={styles.segGroup}>
                {(['Active', 'Inactive'] as const).map(f => (
                  <button
                    key={f}
                    className={`${styles.segBtn}${keyStatusFilter === f ? ` ${styles.segBtnActive}` : ''}`}
                    onClick={() => setKeyStatusFilter(f)}
                  >
                    {f}
                  </button>
                ))}
              </div>
            </div>
            {canEdit && (
              <button className={styles.btnPrimary} onClick={openCreateKey}>
                <PlusIcon /> Add Status Key
              </button>
            )}
          </div>

          {/* Two-column body */}
          <div className={styles.keysBody}>

            {/* LEFT — list */}
            <div className={styles.keysListCard}>
              {keysError ? (
                <div className={styles.errorAlert}><AlertIcon /> {keysError}</div>
              ) : keysLoading ? (
                <div className={styles.loadingCard}><SkeletonBlock /></div>
              ) : statusKeys.length === 0 ? (
                <div className={styles.emptyCard}>
                  <KeyIcon />
                  <span className={styles.emptyTitle}>
                    {keyStatusFilter === 'Inactive' ? 'No inactive status keys' : 'No status keys yet'}
                  </span>
                  <span className={styles.emptyDesc}>
                    {keyStatusFilter === 'Inactive'
                      ? 'There are no deactivated status keys for this branch.'
                      : 'Status keys define payroll entry types such as Vacation, Sick Day, or On Leave.'}
                  </span>
                  {canEdit && keyStatusFilter === 'Active' && (
                    <button
                      className={styles.btnPrimary}
                      onClick={openCreateKey}
                      style={{ marginTop: '1.25rem' }}
                    >
                      <PlusIcon /> Add First Status Key
                    </button>
                  )}
                </div>
              ) : (
                <div className={styles.tableWrap}>
                  <table className={styles.table}>
                    <thead>
                      <tr>
                        <th>Key Name</th>
                        <th className={styles.numTh}>Hours</th>
                        <th>Leave/Off</th>
                        <th>Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {statusKeys.map(k => {
                        const isSelected = k.status_key_id === selectedKeyId;
                        return (
                          <tr
                            key={k.status_key_id}
                            className={`${styles.tableRow}${isSelected ? ` ${styles.tableRowSelected}` : ''}${!k.is_active ? ` ${styles.rowInactive}` : ''}`}
                            onClick={() => dispatchKeys({ type: 'SELECT', id: k.status_key_id })}
                          >
                            <td><span className={styles.statusCodeLabel}>{k.key_name}</span></td>
                            <td className={styles.numCell}>{k.hours_value}h</td>
                            <td>
                              {k.is_off_reason
                                ? <span className={`${styles.badge} ${styles.badgeBlue}`}>Yes</span>
                                : <span className={`${styles.badge} ${styles.badgeGray}`}>No</span>}
                            </td>
                            <td>
                              {k.is_active
                                ? <span className={`${styles.badge} ${styles.badgeGreen}`}>Active</span>
                                : <span className={`${styles.badge} ${styles.badgeRed}`}>Inactive</span>}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* RIGHT — detail panel */}
            <div className={styles.keysDetailPanel}>
              {!selectedKey ? (
                <div className={styles.keysDetailEmpty}>
                  <KeyIcon />
                  <p className={styles.keysDetailEmptyTitle}>Choose a status key to show details</p>
                  <p className={styles.keysDetailEmptyText}>
                    Select a key from the list to view its configuration and settings.
                  </p>
                </div>
              ) : (
                <StatusKeyDetail
                  statusKey={selectedKey}
                  canEdit={canEdit}
                  isDeactivating={deactivating === selectedKey.status_key_id}
                  onEdit={() => openEditKey(selectedKey)}
                  onDeactivate={() => requestDeactivate(selectedKey)}
                  onReactivate={() => requestReactivate(selectedKey)}
                  styles={styles}
                />
              )}
            </div>
          </div>
        </div>
      )}

      {/* Confirm dialog */}
      <ConfirmDialog
        open={confirmKind !== null}
        title={
          confirmKind === 'save-schedule'    ? 'Save payroll schedule?' :
          confirmKind === 'leave-unsaved'    ? 'Leave without saving?' :
          confirmKind === 'save-key'         ? (keyModal === 'create' ? 'Create status key?' : 'Save status key changes?') :
          confirmKind === 'deactivate-key'   ? 'Deactivate status key?' :
          confirmKind === 'reactivate-key'   ? 'Reactivate status key?' :
          'Confirm'
        }
        message={
          confirmKind === 'save-schedule'
            ? `Save payroll setup changes for "${selectedBranch?.branch_name ?? 'this branch'}"?`
            : confirmKind === 'leave-unsaved'
            ? 'You have unsaved schedule changes. Leave without saving?'
            : confirmKind === 'save-key'
            ? keyModal === 'create'
              ? `Create status key "${keyForm.key_name || '…'}"?`
              : `Save changes to "${editingKey?.key_name}"?`
            : confirmKind === 'deactivate-key'
            ? `Deactivate "${targetKey?.key_name}"? It will no longer appear in payroll entry.`
            : confirmKind === 'reactivate-key'
            ? `Reactivate "${targetKey?.key_name}"? It will appear in payroll entry again.`
            : ''
        }
        confirmLabel={
          confirmKind === 'leave-unsaved'  ? 'Leave' :
          confirmKind === 'deactivate-key' ? 'Deactivate' :
          confirmKind === 'reactivate-key' ? 'Reactivate' :
          'Save'
        }
        variant={confirmKind === 'deactivate-key' ? 'danger' : 'primary'}
        loading={confirming || saving || savingKey || deactivating !== null}
        onConfirm={handleConfirm}
        onCancel={() => { setConfirmKind(null); setPendingBranchId(null); }}
      />

      {/* Status Key modal */}
      {keyModal && (
        <div
          className={styles.modalOverlay}
          onClick={e => { if (e.target === e.currentTarget) closeKeyModal(); }}
        >
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>
                {keyModal === 'create' ? 'Add Status Key' : `Edit — ${editingKey?.key_name}`}
              </h2>
              <button className={styles.modalCloseBtn} onClick={closeKeyModal}>
                <CloseIcon />
              </button>
            </div>
            <div className={styles.modalBody}>
              <KeyFormFields
                form={keyForm}
                onChange={p => setKeyForm(f => ({ ...f, ...p }))}
                disabled={savingKey}
                s={styles}
              />
              {keySaveErr && (
                <div className={styles.errorAlert} style={{ marginTop: '0.75rem' }}>
                  <AlertIcon /> {keySaveErr}
                </div>
              )}
            </div>
            <div className={styles.modalFooter}>
              <button className={styles.btnPrimary} onClick={requestSaveKey} disabled={savingKey}>
                {savingKey
                  ? <><SpinnerIcon /> Saving…</>
                  : keyModal === 'create' ? 'Create Key' : 'Save Changes'}
              </button>
              <button className={styles.btnSecondary} onClick={closeKeyModal} disabled={savingKey}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}

// ─── Key form fields ──────────────────────────────────────────────────────────

function KeyFormFields({
  form, onChange, disabled, s,
}: {
  form: KeyForm;
  onChange: (p: Partial<KeyForm>) => void;
  disabled: boolean;
  s: Record<string, string>;
}) {
  return (
    <div className={s.formGrid}>
      {/* Key Name */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>
          Key Name <span className={s.required}>*</span>
        </label>
        <p className={s.fieldHint}>
          The label shown in payroll entry (e.g. "Vacation", "Sick Day", "On Leave").
        </p>
        <input
          className={s.input}
          value={form.key_name}
          onChange={e => onChange({ key_name: e.target.value })}
          disabled={disabled}
          maxLength={200}
          autoFocus
        />
      </div>

      {/* Hours Value */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.label}>Hours Value</label>
        <p className={s.fieldHint}>Hours counted for this status (0–24).</p>
        <input
          type="number"
          className={s.input}
          value={form.hours_value}
          onChange={e => onChange({ hours_value: e.target.value })}
          disabled={disabled}
          min="0" max="24" step="0.5"
          style={{ maxWidth: '120px' }}
        />
      </div>

      {/* Is Off Reason */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label className={s.checkboxLabel}>
          <input
            type="checkbox"
            checked={form.is_off_reason}
            onChange={e => {
              const checked = e.target.checked;
              onChange({
                is_off_reason: checked,
                ...(!checked && { deducts_from_yearly_allowance: false, allowance_category: '' }),
              });
            }}
            disabled={disabled}
          />
          <span>
            <strong>Can be used as a leave/off reason</strong>
            <span className={s.checkboxDesc}> — used for future leave/time-off tracking during a payroll period</span>
          </span>
        </label>
      </div>

      {/* Deducts Allowance */}
      <div className={`${s.formGroup} ${s.formGroupFull}`}>
        <label
          className={s.checkboxLabel}
          style={{ opacity: form.is_off_reason ? 1 : 0.4 }}
        >
          <input
            type="checkbox"
            checked={form.deducts_from_yearly_allowance}
            onChange={e => {
              const checked = e.target.checked;
              onChange({
                deducts_from_yearly_allowance: checked,
                ...(!checked && { allowance_category: '' }),
              });
            }}
            disabled={disabled || !form.is_off_reason}
          />
          <span>
            <strong>Deducts from yearly allowance</strong>
            <span className={s.checkboxDesc}> — when used, deducts from the driver's yearly allowance category</span>
          </span>
        </label>
      </div>

      {/* Allowance Category */}
      {form.deducts_from_yearly_allowance && (
        <div className={`${s.formGroup} ${s.formGroupFull}`}>
          <label className={s.label}>
            Allowance Category <span className={s.required}>*</span>
          </label>
          <p className={s.fieldHint}>
            Which yearly allowance category this key deducts from.
          </p>
          <select
            className={s.select}
            value={form.allowance_category}
            onChange={e => onChange({ allowance_category: e.target.value })}
            disabled={disabled}
          >
            <option value="">— Select category —</option>
            {ALLOWANCE_CATEGORIES.map(c => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>
      )}

      {/* Usage Limits */}
      <div className={`${s.formGroup} ${s.formGroupFull}`} style={{ borderTop: '1px solid #f1f5f9', paddingTop: '0.75rem', marginTop: '0.25rem' }}>
        <label className={s.label} style={{ marginBottom: '0.4rem' }}>Usage Limits</label>
        <p className={s.fieldHint} style={{ marginBottom: '0.75rem' }}>
          Optionally cap how many times this key can be used in various scopes.
        </p>
        {([
          { label: 'Limit uses per period',         enabledKey: 'limit_uses_per_period_enabled' as keyof KeyForm,         valueKey: 'limit_uses_per_period' as keyof KeyForm },
          { label: 'Limit uses per driver',         enabledKey: 'limit_uses_per_driver_enabled' as keyof KeyForm,         valueKey: 'limit_uses_per_driver' as keyof KeyForm },
          { label: 'Limit uses across all drivers', enabledKey: 'limit_uses_across_drivers_enabled' as keyof KeyForm,     valueKey: 'limit_uses_across_drivers' as keyof KeyForm },
          { label: 'Limit uses per day',            enabledKey: 'limit_uses_per_day_enabled' as keyof KeyForm,            valueKey: 'limit_uses_per_day' as keyof KeyForm },
        ] as const).map(({ label, enabledKey, valueKey }) => {
          const enabled = form[enabledKey] as boolean;
          return (
            <div key={enabledKey} className={s.usageLimitRow}>
              <input
                type="checkbox"
                checked={enabled}
                onChange={e => onChange({ [enabledKey]: e.target.checked, ...(!e.target.checked && { [valueKey]: '' }) })}
                disabled={disabled}
                aria-label={label}
              />
              <span style={{ flex: 1, fontSize: '0.83rem', color: '#374151' }}>{label}</span>
              <input
                type="number"
                className={s.usageLimitInput}
                value={form[valueKey] as string}
                onChange={e => onChange({ [valueKey]: e.target.value })}
                disabled={disabled || !enabled}
                placeholder={enabled ? 'e.g. 5' : '—'}
                min="1"
                aria-label={`${label} count`}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Status Key Detail Panel ──────────────────────────────────────────────────

function StatusKeyDetail({
  statusKey: k,
  canEdit,
  isDeactivating,
  onEdit,
  onDeactivate,
  onReactivate,
  styles: s,
}: {
  statusKey: StatusKey;
  canEdit: boolean;
  isDeactivating: boolean;
  onEdit: () => void;
  onDeactivate: () => void;
  onReactivate: () => void;
  styles: Record<string, string>;
}) {
  const usageLimitRows: { label: string; enabled: boolean; value: number | null }[] = [
    { label: 'Per period',         enabled: k.limit_uses_per_period_enabled,    value: k.limit_uses_per_period },
    { label: 'Per driver',         enabled: k.limit_uses_per_driver_enabled,    value: k.limit_uses_per_driver },
    { label: 'Across all drivers', enabled: k.limit_uses_across_drivers_enabled, value: k.limit_uses_across_drivers },
    { label: 'Per day',            enabled: k.limit_uses_per_day_enabled,       value: k.limit_uses_per_day },
  ];
  const anyLimitEnabled = usageLimitRows.some(r => r.enabled);

  return (
    <div className={s.keyDetailContent}>

      {/* Header */}
      <div className={s.keyDetailHeader}>
        <div className={s.keyDetailTitleBlock}>
          <span className={s.keyDetailCodeLabel}>{k.key_name}</span>
          <span className={k.is_active
            ? `${s.badge} ${s.badgeGreen}`
            : `${s.badge} ${s.badgeRed}`}>
            {k.is_active ? 'Active' : 'Inactive'}
          </span>
        </div>
        {canEdit && (
          <button className={s.btnGhost} onClick={onEdit}>
            <EditIconInline /> Edit
          </button>
        )}
      </div>

      {/* Body */}
      <div className={s.keyDetailBody}>

        {/* Key Details */}
        <div className={s.keyDetailSection}>
          <p className={s.keyDetailSectionTitle}>Key Details</p>
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Hours Value</span>
            <span className={s.keyDetailValue}>{k.hours_value}h</span>
          </div>
        </div>

        {/* Leave Settings */}
        <div className={s.keyDetailSection}>
          <p className={s.keyDetailSectionTitle}>Leave / Off Settings</p>
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Leave / off reason</span>
            <span className={s.keyDetailValue}>{k.is_off_reason ? 'Yes' : 'No'}</span>
          </div>
          {k.is_off_reason && (
            <p className={s.keyDetailHint}>
              Can be used for leave/time-off tracking during a payroll period.
            </p>
          )}
          <div className={s.keyDetailRow}>
            <span className={s.keyDetailLabel}>Deducts allowance</span>
            <span className={s.keyDetailValue}>{k.deducts_from_yearly_allowance ? 'Yes' : 'No'}</span>
          </div>
          {k.deducts_from_yearly_allowance && (
            <>
              <div className={s.keyDetailRow}>
                <span className={s.keyDetailLabel}>Allowance category</span>
                <span className={s.keyDetailValue}>{k.allowance_category ?? '—'}</span>
              </div>
              <p className={s.keyDetailHint}>
                When used, deducts from the driver's yearly <em>{k.allowance_category}</em> allowance.
              </p>
            </>
          )}
        </div>

        {/* Usage Limits */}
        <div className={s.keyDetailSection}>
          <div className={s.keyDetailSectionHeader}>
            <p className={s.keyDetailSectionTitle}>Usage Limits</p>
            {!anyLimitEnabled && <span className={s.plannedBadge}>Not configured</span>}
          </div>
          {!anyLimitEnabled ? (
            <p className={s.plannedNote}>
              No usage limits set. Edit this key to add limits.
              <br />
              <em>Note: limits are stored but not yet enforced at payroll entry time.</em>
            </p>
          ) : (
            <>
              {usageLimitRows.filter(r => r.enabled).map(row => (
                <div key={row.label} className={s.keyDetailRow}>
                  <span className={s.keyDetailLabel}>{row.label}</span>
                  <span className={s.keyDetailValue}>{row.value ?? '—'}</span>
                </div>
              ))}
              <p className={s.plannedNote} style={{ marginTop: '0.4rem' }}>
                <em>Usage limits are stored but not yet enforced at payroll entry time.</em>
              </p>
            </>
          )}
        </div>

      </div>

      {/* Actions */}
      {canEdit && (
        <div className={s.keyDetailActions}>
          {k.is_active ? (
            <button
              className={s.btnRowDeactivate}
              onClick={onDeactivate}
              disabled={isDeactivating}
              style={{ width: 'auto', height: 'auto', padding: '0.38rem 0.85rem', fontSize: '0.82rem', gap: '0.35rem' }}
            >
              {isDeactivating ? <SpinnerIcon /> : <DeactivateIcon />}
              Deactivate
            </button>
          ) : (
            <button
              className={s.btnRowReactivate}
              onClick={onReactivate}
              disabled={isDeactivating}
              style={{ width: 'auto', height: 'auto', padding: '0.38rem 0.85rem', fontSize: '0.82rem', gap: '0.35rem' }}
            >
              {isDeactivating ? <SpinnerIcon /> : <ReactivateIcon />}
              Reactivate
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// An inline version of EditIcon used inside StatusKeyDetail (avoids re-export issue)
function EditIconInline() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
    </svg>
  );
}

// ─── Small read-only display components ──────────────────────────────────────

function ReadRow({ label, value, wide }: { label: string; value: string; wide?: boolean }) {
  return (
    <div
      className={styles.readRow}
      style={wide ? { gridColumn: '1 / -1' } : undefined}
    >
      <span className={styles.readLabel}>{label}</span>
      <span className={styles.readValue}>{value}</span>
    </div>
  );
}

function PreviewRow({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.previewRow}>
      <span className={styles.previewLabel}>{label}</span>
      <span className={styles.previewValue}>{value}</span>
    </div>
  );
}

function SkeletonBlock() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.65rem', padding: '1.5rem' }}>
      {[55, 38, 72, 30].map((w, i) => (
        <div key={i} className={styles.skeleton} style={{ width: `${w}%` }} />
      ))}
    </div>
  );
}

// ─── Icons ────────────────────────────────────────────────────────────────────

function CalendarTabIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="18" height="18" rx="2"/>
      <line x1="16" y1="2" x2="16" y2="6"/>
      <line x1="8"  y1="2" x2="8"  y2="6"/>
      <line x1="3"  y1="10" x2="21" y2="10"/>
    </svg>
  );
}

function KeyIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="7.5" cy="15.5" r="5.5"/>
      <path d="M21 2l-9.6 9.6"/>
      <path d="M15.5 7.5l3 3L22 7l-3-3"/>
    </svg>
  );
}

function BranchIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>
      <polyline points="9 22 9 12 15 12 15 22"/>
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
      <line x1="12" y1="5" x2="12" y2="19"/>
      <line x1="5"  y1="12" x2="19" y2="12"/>
    </svg>
  );
}

function EditIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  );
}

function AlertIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <circle cx="12" cy="12" r="10"/>
      <line x1="12" y1="8" x2="12" y2="12"/>
      <line x1="12" y1="16" x2="12.01" y2="16"/>
    </svg>
  );
}

function WarnIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
      <line x1="12" y1="9"  x2="12"    y2="13"/>
      <line x1="12" y1="17" x2="12.01" y2="17"/>
    </svg>
  );
}

function LockIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ flexShrink: 0 }} aria-hidden="true">
      <rect x="5" y="11" width="14" height="10" rx="2"/>
      <path d="M8 11V7a4 4 0 0 1 8 0v4"/>
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
      <line x1="18" y1="6" x2="6"  y2="18"/>
      <line x1="6"  y1="6" x2="18" y2="18"/>
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2.5" strokeLinecap="round" className={styles.spinner} aria-hidden="true">
      <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
    </svg>
  );
}

function DeactivateIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10"/>
      <line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>
    </svg>
  );
}

function ReactivateIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="23 4 23 10 17 10"/>
      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
    </svg>
  );
}
