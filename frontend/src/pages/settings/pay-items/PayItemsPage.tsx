import React, { useEffect, useReducer, useState, useMemo, useCallback, useRef } from 'react';
import type { FormEvent, DragEvent } from 'react';
import apiClient from '../../../lib/apiClient';
import { useAuth } from '../../../store/authStore';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { createCdpiRequest, submitCdpiRequest, createDirectCdpiCompanyItem, listCdpiRequests, decideCdpiRequest } from '../../../lib/cdpiApi';
import { canManageCdpiForBranch, canDirectCreateCdpiCompanyItem } from '../../../lib/permissions';
import type {
  BranchAdmin,
  BranchPayItemState,
  BranchPayItemConfigVersion,
  PayItemConfigUpdate,
  BulkPayItemConfigUpdate,
  BulkPayItemConfigResult,
  CustomPayItemUsage,
  CustomPayItemDeleteResult,
  PayItemOrderUpdate,
  WizardValueType,
  WizardRateMethod,
  CdpiRequestSummary,
  CdpiDecideAction,
  CdpiStatus,
  CdpiRequestListParams,
} from '../../../types/settings';
import styles from './PayItemsPage.module.css';

// ─── Constants ────────────────────────────────────────────────────────────────

const RATE_BEHAVIOR_LABELS: Record<string, string> = {
  PerUnit: 'Per Unit', EnteredAmount: 'Entered Amount', Fixed: 'Fixed Amount',
  Calculated: 'Calculated', None: 'None', OrdinalTier: 'Ordinal Tier',
  RangeBracket: 'Range Bracket', RangeProgressive: 'Range Progressive', Block: 'Block',
};

// ─── Wizard constants ─────────────────────────────────────────────────────────

interface RateMethodDef {
  value:   WizardRateMethod;
  label:   string;
  desc:    string;
  example: string;
  // How many rate name inputs to start with (for fixed-count methods)
  // undefined means dynamic (user can add/remove, starting at 2)
  fixedCount?: number;
  rateLabels?: string[];   // labels for each input when fixed (OrdinalTier)
}

const RATE_METHODS: RateMethodDef[] = [
  {
    value:   'PerUnit',
    label:   'Same rate for every value',
    desc:    'Each unit is paid at one fixed rate.',
    example: 'e.g. $X per mile, $X per hour',
    fixedCount: 1,
  },
  {
    value:   'OrdinalTier',
    label:   'Different rate by item number',
    desc:    '1st item has one rate, 2nd has another, 3rd and beyond use the last rate.',
    example: 'e.g. 1st load $X, 2nd load $Y, 3rd+ load $Z',
    fixedCount:  3,
    rateLabels: ['1st item rate name', '2nd item rate name', '3rd+ item rate name'],
  },
  {
    value:   'Block',
    label:   'Pay by blocks',
    desc:    'Each block of quantity has its own rate.',
    example: 'e.g. first 10 at $X/unit, next 10 at $Y/unit',
  },
  {
    value:   'RangeBracket',
    label:   'One rate based on total range',
    desc:    'The total value falls into a bracket — that bracket\'s single rate applies to all.',
    example: 'e.g. 50–100 total → use the "Mid Range" rate for everything',
  },
  {
    value:   'RangeProgressive',
    label:   'Progressive range rates',
    desc:    'Each range is paid at its own rate; only that portion is paid at that rate.',
    example: 'e.g. first 50 at $X, next 50 at $Y, remainder at $Z',
  },
];

// ─── Step 2 display config (per value type) ──────────────────────────────────

interface MethodDisplayConfig {
  value:          WizardRateMethod;
  label:          string;
  popoverExpl:    string;
  popoverExample: string;
  popoverNote?:   string;
  badge?:         string;
}

interface Step2SectionConfig {
  question: string;
  hint:     string;
  normal:   MethodDisplayConfig[];
  advanced: {
    label:       string;
    supportText: string;
    methods:     MethodDisplayConfig[];
  };
}

const STEP2_TIME: Step2SectionConfig = {
  question: 'How should pay be calculated?',
  hint: 'This is immutable after creation — it defines how the calculation engine turns entered hours into pay.',
  normal: [
    {
      value:          'PerUnit',
      label:          'Same hourly rate',
      popoverExpl:    'Every entered hour is paid using the same hourly rate.',
      popoverExample: '1.5 hours × $20 per hour = $30',
    },
  ],
  advanced: {
    label:       'Advanced methods',
    supportText: 'Use these when the hourly rate changes based on the entered total or time ranges.',
    methods: [
      {
        value:          'RangeBracket',
        label:          'One rate based on total hours',
        popoverExpl:    'The total hours select one hourly rate, and that rate applies to all entered hours.',
        popoverExample: '0–4 hours: $18/hour\nMore than 4 hours: $20/hour\n5 hours × $20 = $100',
      },
      {
        value:          'RangeProgressive',
        label:          'Different rates by hour range',
        popoverExpl:    'Each portion of the entered hours is paid using the rate for its own range.',
        popoverExample: 'First 4 hours × $18 = $72\nNext 1 hour × $20 = $20\nTotal = $92',
      },
    ],
  },
};

const STEP2_NUMBER: Step2SectionConfig = {
  question: 'How should pay be calculated?',
  hint: 'This is immutable after creation — it defines how the calculation engine turns entered quantities into pay.',
  normal: [
    {
      value:          'PerUnit',
      label:          'Same rate for every unit',
      popoverExpl:    'Every entered unit is paid using the same rate.',
      popoverExample: '5 units × $20 = $100',
    },
    {
      value:          'OrdinalTier',
      label:          'Different rate by item number',
      popoverExpl:    'The first unit can earn one amount, the second another, and later units can use a different amount.',
      popoverExample: '1st unit = $35\n2nd unit = $30\n3rd and later = $25\n4 units = $35 + $30 + $25 + $25 = $115',
      badge:          'Whole numbers only',
    },
  ],
  advanced: {
    label:       'Advanced methods',
    supportText: 'Use these when payment depends on blocks, totals, or quantity ranges.',
    methods: [
      {
        value:          'Block',
        label:          'Fixed amount per block',
        popoverExpl:    'The quantity is divided into equal blocks, and each counted block pays one fixed amount.',
        popoverExample: 'Block size: 5\nAmount per block: $14\nQuantity entered: 10\n2 blocks × $14 = $28',
        popoverNote:    'Partial blocks are handled using the rounding rule configured later in Pay Rates.',
      },
      {
        value:          'RangeBracket',
        label:          'One rate based on total quantity',
        popoverExpl:    'The total quantity selects one rate, and that rate applies to the full quantity.',
        popoverExample: '0–100 units: $10 per unit\n101–200 units: $12 per unit\n150 units × $12 = $1,800',
      },
      {
        value:          'RangeProgressive',
        label:          'Different rates by quantity range',
        popoverExpl:    'Each portion of the quantity is paid using the rate for its own range.',
        popoverExample: 'First 100 units × $10 = $1,000\nNext 50 units × $12 = $600\nTotal = $1,600',
      },
    ],
  },
};

// Default rate names for each method
function defaultRateNames(method: WizardRateMethod): string[] {
  const def = RATE_METHODS.find(m => m.value === method);
  if (!def) return [''];
  if (def.fixedCount !== undefined) return Array(def.fixedCount).fill('');
  return ['', ''];  // dynamic: start with 2
}

// ─── Wizard state / reducer ───────────────────────────────────────────────────

interface WizardState {
  step:          1 | 2 | 3;
  scope:         'Daily' | 'Period' | null;
  value_type:    WizardValueType | null;
  rate_method:   WizardRateMethod | null;
  rate_names:    string[];
  item_name:     string;
  display_label: string;
  unit:          string;
  notes:         string;
}

const WIZARD_INITIAL: WizardState = {
  step: 1, scope: 'Daily', value_type: null, rate_method: null,
  rate_names: [], item_name: '', display_label: '', unit: '', notes: '',
};

type WizardAction =
  | { type: 'RESET' }
  | { type: 'SET_STEP';         step:   1 | 2 | 3 }
  | { type: 'SET_SCOPE';        scope:  'Daily' | 'Period' }
  | { type: 'SET_VALUE_TYPE';   vt:     WizardValueType }
  | { type: 'SET_RATE_METHOD';  method: WizardRateMethod }
  | { type: 'SET_RATE_NAME';    idx:    number; name: string }
  | { type: 'ADD_RATE_NAME' }
  | { type: 'REMOVE_RATE_NAME'; idx:    number }
  | { type: 'SET_ITEM_NAME';    name:   string }
  | { type: 'SET_DISPLAY_LABEL'; label: string }
  | { type: 'SET_UNIT';         unit:   string }
  | { type: 'SET_NOTES';        notes:  string };

function wizardReducer(s: WizardState, a: WizardAction): WizardState {
  switch (a.type) {
    case 'RESET': return { ...WIZARD_INITIAL };
    case 'SET_STEP':  return { ...s, step: a.step };
    case 'SET_SCOPE': return { ...s, scope: a.scope, value_type: null, rate_method: null, rate_names: [] };
    case 'SET_VALUE_TYPE': {
      let rate_method: WizardRateMethod | null = a.vt === 'Money' ? null : s.rate_method;
      // OrdinalTier and Block are not valid for Time/Hours
      if (a.vt === 'Time' && (rate_method === 'OrdinalTier' || rate_method === 'Block')) {
        rate_method = null;
      }
      const rate_names = rate_method ? s.rate_names : [];
      return { ...s, value_type: a.vt, rate_method, rate_names };
    }
    case 'SET_RATE_METHOD': {
      const names = defaultRateNames(a.method);
      return { ...s, rate_method: a.method, rate_names: names };
    }
    case 'SET_RATE_NAME': {
      const updated = [...s.rate_names];
      updated[a.idx] = a.name;
      return { ...s, rate_names: updated };
    }
    case 'ADD_RATE_NAME':    return { ...s, rate_names: [...s.rate_names, ''] };
    case 'REMOVE_RATE_NAME': return { ...s, rate_names: s.rate_names.filter((_, i) => i !== a.idx) };
    case 'SET_ITEM_NAME':     return { ...s, item_name: a.name };
    case 'SET_DISPLAY_LABEL': return { ...s, display_label: a.label };
    case 'SET_UNIT':          return { ...s, unit: a.unit };
    case 'SET_NOTES':         return { ...s, notes: a.notes };
    default: return s;
  }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function apiError(err: unknown): string {
  const d = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof d === 'string' && d.trim()) return d;
  // Standard FastAPI validation errors (array of { msg, loc, type })
  if (Array.isArray(d))
    return d.map((i: unknown) =>
      i && typeof i === 'object' && 'msg' in i ? String((i as { msg: unknown }).msg) : null
    ).filter(Boolean).join(' ');
  // Object-shaped detail: bulk validation failure returns { message, branch_errors }
  if (d !== null && typeof d === 'object' && !Array.isArray(d)) {
    const obj = d as {
      message?: unknown;
      branch_errors?: Array<{ branch_name?: unknown; error?: unknown }>;
    };
    const parts: string[] = [];
    if (typeof obj.message === 'string' && obj.message.trim()) parts.push(obj.message);
    if (Array.isArray(obj.branch_errors) && obj.branch_errors.length > 0) {
      obj.branch_errors.forEach(be => {
        const name = typeof be.branch_name === 'string' ? be.branch_name : '';
        const msg  = typeof be.error === 'string' ? be.error : '';
        if (name && msg) parts.push(`• ${name}: ${msg}`);
        else if (msg)   parts.push(`• ${msg}`);
      });
    }
    if (parts.length) return parts.join('\n');
  }
  return 'An unexpected error occurred.';
}

function fmtDate(s: string | null): string {
  if (!s) return '—';
  return new Date(s).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function todayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

// ─── Reducers ─────────────────────────────────────────────────────────────────

// Branches list
type BranchesState = { branches: BranchAdmin[]; loading: boolean; error: string };
type BranchesAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK'; branches: BranchAdmin[] }
  | { type: 'FETCH_ERROR'; error: string };

function branchesReducer(s: BranchesState, a: BranchesAction): BranchesState {
  switch (a.type) {
    case 'FETCH_START': return { branches: [], loading: true, error: '' };
    case 'FETCH_OK':    return { branches: a.branches, loading: false, error: '' };
    case 'FETCH_ERROR': return { branches: [], loading: false, error: a.error };
    default:            return s;
  }
}

// Single-branch items — separate pending orders per scope (null = use server order)
type ItemsState = {
  items:            BranchPayItemState[];
  loading:          boolean;
  error:            string;
  localOrderDaily:  BranchPayItemState[] | null;  // pending Daily reorder
  localOrderPeriod: BranchPayItemState[] | null;  // pending Period reorder
};
type ItemsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';         items:   BranchPayItemState[] }
  | { type: 'FETCH_ERROR';      error:   string }
  | { type: 'ITEM_UPDATED';     updated: BranchPayItemState }
  | { type: 'REORDER_DAILY';    ordered: BranchPayItemState[] }
  | { type: 'REORDER_PERIOD';   ordered: BranchPayItemState[] }
  | { type: 'CANCEL_ORDER_DAILY' }
  | { type: 'CANCEL_ORDER_PERIOD' };

function itemsReducer(s: ItemsState, a: ItemsAction): ItemsState {
  switch (a.type) {
    case 'FETCH_START':          return { items: [], loading: true,  error: '',      localOrderDaily: null, localOrderPeriod: null };
    case 'FETCH_OK':             return { items: a.items, loading: false, error: '', localOrderDaily: null, localOrderPeriod: null };
    case 'FETCH_ERROR':          return { items: [], loading: false, error: a.error, localOrderDaily: null, localOrderPeriod: null };
    case 'ITEM_UPDATED':         return { ...s, items: s.items.map(i => i.pay_item_id === a.updated.pay_item_id ? a.updated : i) };
    case 'REORDER_DAILY':        return { ...s, localOrderDaily:  a.ordered };
    case 'REORDER_PERIOD':       return { ...s, localOrderPeriod: a.ordered };
    case 'CANCEL_ORDER_DAILY':   return { ...s, localOrderDaily:  null };
    case 'CANCEL_ORDER_PERIOD':  return { ...s, localOrderPeriod: null };
    default:                     return s;
  }
}

// All-branches items — includes partial-load tracking
type AllBranchState = {
  byBranch: Record<number, BranchPayItemState[]>;
  loading: boolean;
  error: string;
  /** Non-null when some (but not all) branches failed to load. Coverage is partial. */
  partialWarning: string | null;
  failedBranches: Array<{ name: string; error: string }>;
};
type AllBranchAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';      byBranch: Record<number, BranchPayItemState[]> }
  | { type: 'FETCH_PARTIAL'; byBranch: Record<number, BranchPayItemState[]>; failed: Array<{ name: string; error: string }> }
  | { type: 'FETCH_ERROR';   error: string }
  | { type: 'BRANCH_ITEM_UPDATED'; branchId: number; updated: BranchPayItemState };

function allBranchReducer(s: AllBranchState, a: AllBranchAction): AllBranchState {
  switch (a.type) {
    case 'FETCH_START':
      return { byBranch: {}, loading: true, error: '', partialWarning: null, failedBranches: [] };
    case 'FETCH_OK':
      return { byBranch: a.byBranch, loading: false, error: '', partialWarning: null, failedBranches: [] };
    case 'FETCH_PARTIAL':
      return { byBranch: a.byBranch, loading: false, error: '', partialWarning: `${a.failed.length} branch(es) failed to load. Coverage shown is based on partial data.`, failedBranches: a.failed };
    case 'FETCH_ERROR':
      return { byBranch: {}, loading: false, error: a.error, partialWarning: null, failedBranches: [] };
    case 'BRANCH_ITEM_UPDATED': {
      const prev = s.byBranch[a.branchId] ?? [];
      return { ...s, byBranch: { ...s.byBranch, [a.branchId]: prev.map(i => i.pay_item_id === a.updated.pay_item_id ? a.updated : i) } };
    }
    default: return s;
  }
}

// Selection + history — combined so RESET is one atomic dispatch (no sync-setState-in-effect lint error)
type SelectionState = {
  selectedItem: BranchPayItemState | null;
  selectedAgg:  AggregateItem | null;
  showHistory:  boolean;
  history:      BranchPayItemConfigVersion[];
  historyLoading: boolean;
};
type SelectionAction =
  | { type: 'RESET' }
  | { type: 'SELECT_ITEM';    item: BranchPayItemState }
  | { type: 'UPDATE_ITEM';    item: BranchPayItemState }
  | { type: 'SELECT_AGG';     agg:  AggregateItem }
  | { type: 'HISTORY_START' }
  | { type: 'HISTORY_DONE';   history: BranchPayItemConfigVersion[] }
  | { type: 'HISTORY_HIDE' };

function selectionReducer(s: SelectionState, a: SelectionAction): SelectionState {
  switch (a.type) {
    case 'RESET':
      return { selectedItem: null, selectedAgg: null, showHistory: false, history: [], historyLoading: false };
    case 'SELECT_ITEM':
      return { selectedItem: a.item, selectedAgg: null, showHistory: false, history: [], historyLoading: false };
    case 'UPDATE_ITEM':
      return { ...s, selectedItem: s.selectedItem?.pay_item_id === a.item.pay_item_id ? a.item : s.selectedItem };
    case 'SELECT_AGG':
      return { selectedAgg: a.agg, selectedItem: null, showHistory: false, history: [], historyLoading: false };
    case 'HISTORY_START':
      return { ...s, historyLoading: true, showHistory: true };
    case 'HISTORY_DONE':
      return { ...s, history: a.history, historyLoading: false };
    case 'HISTORY_HIDE':
      return { ...s, showHistory: false };
    default:
      return s;
  }
}

// ─── Aggregate helpers (All Branches mode) ────────────────────────────────────

interface AggregateItem {
  pay_item_id: number;
  pay_item_code: string;
  pay_item_name: string;
  category: string;
  item_scope: 'Daily' | 'Period' | 'Summary';
  rate_behavior: string;
  requires_rate: boolean;
  appears_in_payroll_entry: boolean;
  is_system_standard: boolean;
  sort_order: number;
  totalBranches: number;
  activeBranches: number;
  coverage: 'all-active' | 'all-inactive' | 'mixed';
  perBranch: Array<{
    branch_id: number; branch_name: string;
    is_active: boolean; is_using_default: boolean;
    current_config: BranchPayItemConfigVersion | null;
  }>;
  sample: BranchPayItemState;
}

function buildAggregates(byBranch: Record<number, BranchPayItemState[]>, branches: BranchAdmin[]): AggregateItem[] {
  const map = new Map<number, AggregateItem>();
  for (const branch of branches) {
    const items = byBranch[branch.branch_id] ?? [];
    for (const item of items) {
      if (!map.has(item.pay_item_id)) {
        map.set(item.pay_item_id, {
          pay_item_id: item.pay_item_id, pay_item_code: item.pay_item_code,
          pay_item_name: item.pay_item_name, category: item.category,
          item_scope: item.item_scope, rate_behavior: item.rate_behavior,
          requires_rate: item.requires_rate, appears_in_payroll_entry: item.appears_in_payroll_entry,
          is_system_standard: item.is_system_standard, sort_order: item.sort_order,
          totalBranches: 0, activeBranches: 0, coverage: 'all-inactive', perBranch: [], sample: item,
        });
      }
      const agg = map.get(item.pay_item_id)!;
      agg.totalBranches++;
      if (item.is_active) agg.activeBranches++;
      agg.perBranch.push({ branch_id: branch.branch_id, branch_name: branch.branch_name,
        is_active: item.is_active, is_using_default: item.is_using_default, current_config: item.current_config });
    }
  }
  for (const agg of map.values()) {
    agg.coverage = agg.activeBranches === 0 ? 'all-inactive'
      : agg.activeBranches === agg.totalBranches ? 'all-active' : 'mixed';
  }
  return Array.from(map.values()).sort((a, b) =>
    a.sort_order - b.sort_order || a.pay_item_name.localeCompare(b.pay_item_name)
  );
}

// ─── CDPI request list state ──────────────────────────────────────────────────

type CdpiReqsState = { requests: CdpiRequestSummary[]; loading: boolean; error: string };
type CdpiReqsAction =
  | { type: 'FETCH_START' }
  | { type: 'FETCH_OK';    requests: CdpiRequestSummary[] }
  | { type: 'FETCH_ERROR'; error:    string };

function cdpiReqsReducer(s: CdpiReqsState, a: CdpiReqsAction): CdpiReqsState {
  switch (a.type) {
    case 'FETCH_START': return { requests: [], loading: true,  error: '' };
    case 'FETCH_OK':    return { requests: a.requests, loading: false, error: '' };
    case 'FETCH_ERROR': return { requests: [], loading: false, error: a.error };
    default:            return s;
  }
}

// ─── Main component ───────────────────────────────────────────────────────────

export function PayItemsPage() {
  const { user } = useAuth();
  // TODO: When /auth/me exposes explicit permission flags, prefer those over
  // role_code derivation. Currently has_setup_manage is derived in authStore.ts
  // from role_code === 'PAYROLL_ADMIN' on the AllCompanyBranches entry.
  const isAdmin = user?.scope_type === 'AllCompanyBranches' && user?.has_setup_manage === true;

  // ── Branch mode & selector ────────────────────────────────────────────────
  const [branchMode, setBranchMode] = useState<'single' | 'all'>('single');
  const [selectedBranchId, setSelectedBranchId] = useState<number | null>(null);

  // ── Data reducers ─────────────────────────────────────────────────────────
  const [branchSt, dispatchBranches] = useReducer(branchesReducer,
    { branches: [], loading: true, error: '' });
  const [singleSt, dispatchSingle] = useReducer(itemsReducer,
    { items: [], loading: false, error: '', localOrderDaily: null, localOrderPeriod: null });
  const [allSt, dispatchAll] = useReducer(allBranchReducer,
    { byBranch: {}, loading: false, error: '', partialWarning: null, failedBranches: [] });

  // Selection + history in one reducer (prevents sync-setState-in-effect lint errors)
  const [selSt, dispatchSel] = useReducer(selectionReducer,
    { selectedItem: null, selectedAgg: null, showHistory: false, history: [], historyLoading: false });

  // ── All-branches reload trigger (incremented after custom item create) ────
  const [allBranchKey, setAllBranchKey] = useState(0);

  // ── List filters ──────────────────────────────────────────────────────────
  const [search, setSearch]           = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'inactive' | 'mixed'>('all');
  const [branchDropOpen, setBranchDropOpen] = useState(false);

  // ── Edit config modal ─────────────────────────────────────────────────────
  const [editOpen, setEditOpen]               = useState(false);
  const [editIsActive, setEditIsActive]       = useState(true);
  const [editNotes, setEditNotes]             = useState('');
  const [editUseSpecificDate, setEditUseSpecificDate] = useState(false);
  const [editDate, setEditDate]               = useState<string>(() => todayISO());
  const [editTarget, setEditTarget]           = useState<'single' | 'all'>('single');
  const [editSaving, setEditSaving]           = useState(false);
  const [editError, setEditError]             = useState('');
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false);

  // ── Bulk result ───────────────────────────────────────────────────────────
  const [bulkResult, setBulkResult]         = useState<BulkPayItemConfigResult | null>(null);
  const [bulkResultOpen, setBulkResultOpen] = useState(false);

  // ── Create wizard ─────────────────────────────────────────────────────────
  const [createOpen, setCreateOpen]   = useState(false);
  const [createSaving, setCreateSaving] = useState(false);
  const [createError, setCreateError]   = useState('');
  const [wizard, dispatchWizard]        = useReducer(wizardReducer, WIZARD_INITIAL);

  // ── CDPI request review ───────────────────────────────────────────────────
  const [cdpiSt, dispatchCdpi] = useReducer(cdpiReqsReducer, { requests: [], loading: false, error: '' });
  const [cdpiFilter, setCdpiFilter]         = useState<CdpiStatus | 'all'>('PendingCompanyApproval');
  const [cdpiKey, setCdpiKey]               = useState(0);
  const [decideOpen, setDecideOpen]         = useState(false);
  const [decideTarget, setDecideTarget]     = useState<CdpiRequestSummary | null>(null);
  const [decideAction, setDecideAction]     = useState<CdpiDecideAction | null>(null);
  const [decideReason, setDecideReason]     = useState('');
  const [decideSaving, setDecideSaving]     = useState(false);
  const [decideError, setDecideError]       = useState('');

  // ── Delete flow ───────────────────────────────────────────────────────────
  const [usageData, setUsageData]         = useState<CustomPayItemUsage | null>(null);
  const [usageLoading, setUsageLoading]   = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [deleteWorking, setDeleteWorking] = useState(false);

  // ── Toast ─────────────────────────────────────────────────────────────────
  const [toast, setToast] = useState('');
  const showToast = useCallback((msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(''), 4000);
  }, []);

  // ── Drag-to-reorder state ──────────────────────────────────────────────────
  // localOrder is held in singleSt.localOrder (reducer) so FETCH_START resets it.
  // dragOverId and orderSaving are UI-only, never set inside effects.
  const [dragOverId, setDragOverId]       = useState<number | null>(null);
  const [orderSaving, setOrderSaving]     = useState<'Daily' | 'Period' | null>(null);
  const [isDraggingCursor, setIsDraggingCursor] = useState(false);
  const dragItemIdRef = useRef<number | null>(null);

  // ── Drag cursor (lifecycle-safe: DOM side-effect lives in an effect) ───────
  useEffect(() => {
    document.body.style.cursor = isDraggingCursor ? 'grabbing' : '';
    return () => { document.body.style.cursor = ''; };
  }, [isDraggingCursor]);

  // ── Load branches ─────────────────────────────────────────────────────────
  useEffect(() => {
    dispatchBranches({ type: 'FETCH_START' });
    apiClient.get<BranchAdmin[]>('/settings/branches')
      .then(({ data }) => {
        dispatchBranches({ type: 'FETCH_OK', branches: data });
        if (data.length > 0 && !selectedBranchId) {
          setSelectedBranchId((data.find(b => b.is_default) ?? data[0]).branch_id);
        }
      })
      .catch(e => dispatchBranches({ type: 'FETCH_ERROR', error: apiError(e) }));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Load single branch items ───────────────────────────────────────────────
  useEffect(() => {
    if (branchMode !== 'single' || !selectedBranchId) return;
    dispatchSingle({ type: 'FETCH_START' });   // also resets localOrder
    dispatchSel({ type: 'RESET' });            // single dispatch — no sync-setState-in-effect
    apiClient.get<BranchPayItemState[]>(`/settings/branches/${selectedBranchId}/pay-items`)
      .then(({ data }) => dispatchSingle({ type: 'FETCH_OK', items: data }))
      .catch(e => dispatchSingle({ type: 'FETCH_ERROR', error: apiError(e) }));
  }, [branchMode, selectedBranchId]);

  // ── Load all branches items ────────────────────────────────────────────────
  useEffect(() => {
    if (branchMode !== 'all' || branchSt.branches.length === 0) return;
    dispatchAll({ type: 'FETCH_START' });
    dispatchSel({ type: 'RESET' });   // single dispatch — no sync-setState-in-effect
    const active = branchSt.branches.filter(b => b.status === 'Active');
    Promise.allSettled(
      active.map(b =>
        apiClient.get<BranchPayItemState[]>(`/settings/branches/${b.branch_id}/pay-items`)
          .then(({ data }) => ({ branch_id: b.branch_id, items: data }))
      )
    ).then(results => {
      const byBranch: Record<number, BranchPayItemState[]> = {};
      const failed: Array<{ name: string; error: string }> = [];
      results.forEach((r, i) => {
        if (r.status === 'fulfilled') {
          byBranch[r.value.branch_id] = r.value.items;
        } else {
          failed.push({ name: active[i].branch_name, error: apiError(r.reason) });
        }
      });
      if (Object.keys(byBranch).length === 0) {
        dispatchAll({ type: 'FETCH_ERROR', error: 'Failed to load pay items for all branches.' });
      } else if (failed.length > 0) {
        dispatchAll({ type: 'FETCH_PARTIAL', byBranch, failed });
      } else {
        dispatchAll({ type: 'FETCH_OK', byBranch });
      }
    });
  }, [branchMode, branchSt.branches, allBranchKey]);

  // ── Load CDPI requests ────────────────────────────────────────────────────
  // Inline permission check because the derived constants are declared later in the
  // component body (they depend on selectedBranchId which is state).
  useEffect(() => {
    const canDirectCreate = canDirectCreateCdpiCompanyItem(user);
    const canBranchCreate = !canDirectCreate && selectedBranchId !== null
      && canManageCdpiForBranch(user, selectedBranchId);
    if (!canDirectCreate && !canBranchCreate) return;
    dispatchCdpi({ type: 'FETCH_START' });
    const params: CdpiRequestListParams = {};
    if (cdpiFilter !== 'all') params.status = cdpiFilter;
    // Branch users see only their own branch's requests.
    if (!canDirectCreate && selectedBranchId) params.branch_id = selectedBranchId;
    listCdpiRequests(params)
      .then(data => dispatchCdpi({ type: 'FETCH_OK', requests: data }))
      .catch(e => dispatchCdpi({ type: 'FETCH_ERROR', error: apiError(e) }));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cdpiFilter, cdpiKey, selectedBranchId]);
  // user is stable for the session; selectedBranchId covers userCanBranchCreate changes.

  // ── Aggregates ────────────────────────────────────────────────────────────
  const aggregates = useMemo(() => {
    if (branchMode !== 'all') return [];
    return buildAggregates(allSt.byBranch, branchSt.branches.filter(b => b.status === 'Active'));
  }, [branchMode, allSt.byBranch, branchSt.branches]);

  // ── Filtered items ────────────────────────────────────────────────────────

  // Base filter: search + status only (NOT scope tab).
  // Used to derive per-scope display lists so localOrderDaily/Period work correctly
  // regardless of which scope tab is active.
  const filteredBase = useMemo(() => {
    const q = search.trim().toLowerCase();
    return singleSt.items.filter(item => {
      if (item.item_scope !== 'Daily') return false;
      if (statusFilter === 'active'   && !item.is_active) return false;
      if (statusFilter === 'inactive' &&  item.is_active) return false;
      if (q && !item.pay_item_name.toLowerCase().includes(q) && !item.pay_item_code.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [singleSt.items, search, statusFilter]);

  // Display list — pending local order takes precedence over server order
  const displayDaily: BranchPayItemState[] = singleSt.localOrderDaily ?? filteredBase;

  const filteredAgg = useMemo(() => {
    const q = search.trim().toLowerCase();
    return aggregates.filter(agg => {
      if (agg.item_scope !== 'Daily') return false;
      if (statusFilter === 'active'   && agg.coverage !== 'all-active')   return false;
      if (statusFilter === 'inactive' && agg.coverage !== 'all-inactive') return false;
      if (statusFilter === 'mixed'    && agg.coverage !== 'mixed')        return false;
      if (q && !agg.pay_item_name.toLowerCase().includes(q) && !agg.pay_item_code.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [aggregates, search, statusFilter]);

  // ── Open edit modal ────────────────────────────────────────────────────────
  function openEdit() {
    const item = selSt.selectedItem ?? selSt.selectedAgg?.sample;
    if (!item) return;
    setEditIsActive(selSt.selectedItem ? selSt.selectedItem.is_active : selSt.selectedAgg!.coverage === 'all-active');
    setEditNotes(selSt.selectedItem?.notes ?? '');
    setEditUseSpecificDate(false);   // default: let backend schedule the safe date
    setEditDate(todayISO());
    setEditTarget(branchMode === 'all' ? 'all' : 'single');
    setEditError('');
    setEditOpen(true);
  }

  // ── Execute save (called directly for single, via confirm for bulk) ────────
  async function executeSave() {
    const itemId = selSt.selectedItem?.pay_item_id ?? selSt.selectedAgg?.pay_item_id;
    if (!itemId) return;

    const payload: PayItemConfigUpdate = {
      is_active: editIsActive,
      notes: editNotes.trim() || null,
      // If user did not pick a specific date, send null → backend schedules safely
      effective_from: editUseSpecificDate ? (editDate || null) : null,
    };

    setEditSaving(true);
    setEditError('');

    try {
      if (editTarget === 'single') {
        const branchId = selectedBranchId;
        if (!branchId) return;
        const { data } = await apiClient.patch<BranchPayItemState>(
          `/settings/branches/${branchId}/pay-items/${itemId}`, payload
        );
        dispatchSingle({ type: 'ITEM_UPDATED', updated: data });
        dispatchSel({ type: 'UPDATE_ITEM', item: data });
        setEditOpen(false);
        showToast('Configuration saved.');
      } else {
        // Atomic bulk update via dedicated backend endpoint.
        // Either ALL branches are updated or NONE (single transaction).
        const bulkPayload: BulkPayItemConfigUpdate = {
          target: 'AllBranches',
          branch_ids: null,
          is_active: editIsActive,
          notes: editNotes.trim() || null,
          effective_from: editUseSpecificDate ? (editDate || null) : null,
        };
        const { data: bulkRes } = await apiClient.patch<BulkPayItemConfigResult>(
          `/settings/branches/pay-items/${itemId}/bulk-config`, bulkPayload
        );
        // Refresh all-branch aggregate (incrementing key re-triggers the effect)
        setAllBranchKey(k => k + 1);
        dispatchSel({ type: 'RESET' });
        setEditOpen(false);
        setBulkResult(bulkRes);
        setBulkResultOpen(true);
      }
    } catch (e) {
      setEditError(apiError(e));
    } finally {
      setEditSaving(false);
    }
  }

  // ── Load history ───────────────────────────────────────────────────────────
  async function loadHistory() {
    if (!selSt.selectedItem || !selectedBranchId) return;
    dispatchSel({ type: 'HISTORY_START' });
    try {
      const { data } = await apiClient.get<BranchPayItemConfigVersion[]>(
        `/settings/branches/${selectedBranchId}/pay-items/${selSt.selectedItem.pay_item_id}/history`
      );
      dispatchSel({ type: 'HISTORY_DONE', history: data });
    } catch {
      dispatchSel({ type: 'HISTORY_DONE', history: [] });
    }
  }

  // ── Custom item: create (CDPI flow) ───────────────────────────────────────
  async function saveCreate(e: FormEvent) {
    e.preventDefault();
    if (!wizardStep3Complete) {
      setCreateError('Pay item name is required.');
      return;
    }
    if (wizard.rate_method !== 'PerUnit') {
      setCreateError('Only "Same rate" (PerUnit) is supported at this time. Please select it in Step 2.');
      return;
    }
    // input_type is always Time or Number in this wizard (Money card not shown in step 1)
    const input_type = wizard.value_type === 'Time' ? 'Time' as const : 'Number' as const;

    setCreateSaving(true);
    setCreateError('');

    try {
      if (userCanDirectCreate) {
        // ── Company-wide direct create ─────────────────────────────────────
        await createDirectCdpiCompanyItem({
          item_name:       wizard.item_name.trim(),
          input_type,
          calc_method_key: 'PerUnit',
          unit:            wizard.unit.trim() || null,
          notes:           wizard.notes.trim() || null,
        });
        setCreateOpen(false);
        dispatchWizard({ type: 'RESET' });
        showToast('Pay item created at company level. It starts inactive on all branches — activate per branch after setup.');
      } else {
        // ── Branch-scoped request flow ─────────────────────────────────────
        if (!selectedBranchId) {
          setCreateError('No branch selected. Please select a branch before submitting.');
          return;
        }
        // Step 1: create draft
        const draft = await createCdpiRequest({
          requesting_branch_id: selectedBranchId,
          item_name:            wizard.item_name.trim(),
          input_type,
          calc_method_key:      'PerUnit',
          unit:                 wizard.unit.trim() || null,
          notes:                wizard.notes.trim() || null,
        });
        // Step 2: submit for approval
        try {
          await submitCdpiRequest(draft.request_id, { expected_revision: draft.revision });
          setCreateOpen(false);
          dispatchWizard({ type: 'RESET' });
          showToast('Request submitted for company approval.');
        } catch (submitErr) {
          // Draft created but submit failed — inform user of partial state
          setCreateError(
            `Draft was saved (ref: ${draft.request_id.slice(0, 8)}…) but could not be submitted: ` +
            `${apiError(submitErr)}. You can submit it later once the issue is resolved.`
          );
        }
      }
    } catch (e) {
      setCreateError(apiError(e));
    } finally {
      setCreateSaving(false);
    }
  }

  // ── Custom item: delete ────────────────────────────────────────────────────
  async function startDelete() {
    if (!selSt.selectedItem) return;
    setUsageLoading(true);
    try {
      const { data } = await apiClient.get<CustomPayItemUsage>(
        `/settings/pay-items/${selSt.selectedItem.pay_item_id}/usage`
      );
      setUsageData(data);
      setDeleteConfirmOpen(true);
    } catch (e) { showToast(apiError(e)); }
    finally { setUsageLoading(false); }
  }

  async function confirmDelete() {
    if (!selSt.selectedItem) return;
    setDeleteWorking(true);
    try {
      const { data } = await apiClient.delete<CustomPayItemDeleteResult>(
        `/settings/pay-items/${selSt.selectedItem.pay_item_id}`
      );
      setDeleteConfirmOpen(false);
      dispatchSel({ type: 'RESET' });
      showToast(data.deletion_type === 'physical'
        ? `"${data.pay_item_code}" deleted permanently.`
        : `"${data.pay_item_code}" retired (it was used in payroll data).`
      );
      if (branchMode === 'single' && selectedBranchId) {
        dispatchSingle({ type: 'FETCH_START' });
        apiClient.get<BranchPayItemState[]>(`/settings/branches/${selectedBranchId}/pay-items`)
          .then(({ data: d }) => dispatchSingle({ type: 'FETCH_OK', items: d }))
          .catch(e => dispatchSingle({ type: 'FETCH_ERROR', error: apiError(e) }));
      }
    } catch (e) { showToast(apiError(e)); }
    finally { setDeleteWorking(false); }
  }

  // ── CDPI decide action ────────────────────────────────────────────────────
  function openDecide(req: CdpiRequestSummary, action: CdpiDecideAction) {
    setDecideTarget(req);
    setDecideAction(action);
    setDecideReason('');
    setDecideError('');
    setDecideOpen(true);
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
      await decideCdpiRequest(decideTarget.request_id, {
        action:            decideAction,
        expected_revision: decideTarget.revision,
        reason:            decideReason.trim(),
      });
      setDecideOpen(false);
      setDecideTarget(null);
      setDecideAction(null);
      setDecideReason('');
      const label = decideAction === 'Approve' ? 'approved'
        : decideAction === 'Reject'       ? 'rejected'
        : 'returned to draft';
      showToast(`Request ${label}.`);
      setCdpiKey(k => k + 1);
      // Refresh pay items list when a request is approved — the backend creates the PayItem.
      if (decideAction === 'Approve') {
        if (branchMode === 'single' && selectedBranchId) {
          dispatchSingle({ type: 'FETCH_START' });
          apiClient.get<BranchPayItemState[]>(`/settings/branches/${selectedBranchId}/pay-items`)
            .then(({ data }) => dispatchSingle({ type: 'FETCH_OK', items: data }))
            .catch(e2 => dispatchSingle({ type: 'FETCH_ERROR', error: apiError(e2) }));
        } else if (branchMode === 'all') {
          setAllBranchKey(k => k + 1);
        }
      }
    } catch (e) {
      setDecideError(apiError(e));
    } finally {
      setDecideSaving(false);
    }
  }

  // ─── Derived ───────────────────────────────────────────────────────────────

  // ── CDPI permission derivation ────────────────────────────────────────────
  // Company-wide direct create: AllCompanyBranches + payitems.edit (all assignments same scope).
  const userCanDirectCreate = canDirectCreateCdpiCompanyItem(user);
  // Branch-scoped request flow: mutually exclusive with direct create; requires a concrete branch.
  // selectedBranchId may be null while branches are loading — conservative: hide button until known.
  const userCanBranchCreate = !userCanDirectCreate && selectedBranchId !== null
    && canManageCdpiForBranch(user, selectedBranchId);
  // Show "+ Add Custom Item" when either CDPI path is available.
  const showAddButton = userCanDirectCreate || userCanBranchCreate;
  // Show CDPI request section to reviewers and branch-scoped submitters.
  const showCdpiRequests = userCanDirectCreate || userCanBranchCreate;

  const activeBranchCount = branchSt.branches.filter(b => b.status === 'Active').length;
  const loading   = branchMode === 'single' ? singleSt.loading : allSt.loading;
  const loadError = branchMode === 'single' ? singleSt.error   : allSt.error;

  // Wizard: can we advance from step 1 to step 2?
  const wizardStep1Complete = wizard.value_type !== null;
  // Wizard: can we advance from step 2 to step 3?
  const wizardStep2Complete = wizard.value_type !== null &&
    (wizard.value_type === 'Money' || wizard.rate_method !== null);
  // Wizard: can we submit?
  const wizardStep3Complete = wizard.item_name.trim().length > 0;

  // Drag-to-reorder: available in any scope tab when no search/status filter is active.
  // Scope boundary enforcement happens in handleDrop (Daily cannot be dropped onto Period and vice versa).
  const canReorder = isAdmin && branchMode === 'single' && !search && statusFilter === 'all' && !singleSt.loading;

  // ── Drag handlers ─────────────────────────────────────────────────────────
  function handleDragStart(e: DragEvent<HTMLTableRowElement>, id: number) {
    dragItemIdRef.current = id;
    e.dataTransfer.effectAllowed = 'move';
    setIsDraggingCursor(true);
    const reset = () => { setIsDraggingCursor(false); document.removeEventListener('mouseup', reset); };
    document.addEventListener('mouseup', reset);
  }

  function handleDragOver(e: DragEvent<HTMLTableRowElement>, id: number) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (dragOverId !== id) setDragOverId(id);
  }

  function handleDrop(e: DragEvent<HTMLTableRowElement>, targetId: number) {
    e.preventDefault();
    setDragOverId(null);
    const fromId = dragItemIdRef.current;
    dragItemIdRef.current = null;
    if (!fromId || fromId === targetId) return;

    const fromItem = displayDaily.find(i => i.pay_item_id === fromId);
    const toItem   = displayDaily.find(i => i.pay_item_id === targetId);
    if (!fromItem || !toItem) return;

    const fromIdx = displayDaily.findIndex(i => i.pay_item_id === fromId);
    const toIdx   = displayDaily.findIndex(i => i.pay_item_id === targetId);
    if (fromIdx === -1 || toIdx === -1) return;

    const next = [...displayDaily];
    const [moved] = next.splice(fromIdx, 1);
    next.splice(toIdx, 0, moved);
    dispatchSingle({ type: 'REORDER_DAILY', ordered: next });
  }

  function handleDragEnd() {
    dragItemIdRef.current = null;
    setDragOverId(null);
    setIsDraggingCursor(false);
  }

  // ── Save order (scope-specific) ────────────────────────────────────────────
  async function saveOrderForScope(scope: 'Daily' | 'Period') {
    const localOrder = scope === 'Daily' ? singleSt.localOrderDaily : singleSt.localOrderPeriod;
    if (!localOrder) return;
    setOrderSaving(scope);
    const payload: PayItemOrderUpdate = {
      items: localOrder.map((item, idx) => ({
        pay_item_id: item.pay_item_id,
        sort_order:  (idx + 1) * 10,
      })),
    };
    try {
      await apiClient.patch('/settings/pay-items/order', payload);
      // Reload from server to confirm saved order (FETCH_START clears both local orders).
      dispatchSingle({ type: 'FETCH_START' });
      const { data } = await apiClient.get<BranchPayItemState[]>(
        `/settings/branches/${selectedBranchId}/pay-items`
      );
      dispatchSingle({ type: 'FETCH_OK', items: data });
      showToast(`${scope === 'Daily' ? 'Daily' : 'Pay Period'} display order saved.`);
    } catch (e) {
      showToast(apiError(e));
    } finally {
      setOrderSaving(null);
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  return (
    <div className={styles.page}>

      {/* ── Toast ── */}
      {toast && <div className={styles.successAlert}><CheckIcon /> {toast}</div>}

      {/* ── Branch bar ── */}
      <div className={styles.branchBar}>
        <span className={styles.branchBarLabel}>View</span>
        {isAdmin ? (
          <div className={styles.modeGroup}>
            {(['single', 'all'] as const).map(m => (
              <button key={m}
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
          isAdmin ? (
            <div className={styles.branchDropWrap} onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setBranchDropOpen(false); }}>
              <button className={styles.branchDropBtn} onClick={() => setBranchDropOpen(o => !o)} disabled={branchSt.loading} type="button">
                <span>{branchSt.branches.find(b => b.branch_id === selectedBranchId)?.branch_name ?? '—'}</span>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className={branchDropOpen ? styles.branchDropChevronOpen : styles.branchDropChevron}><polyline points="6 9 12 15 18 9"/></svg>
              </button>
              {branchDropOpen && (
                <div className={styles.branchDropList}>
                  {branchSt.branches.map(b => (
                    <button key={b.branch_id} type="button"
                      className={`${styles.branchDropItem}${b.branch_id === selectedBranchId ? ` ${styles.branchDropItemActive}` : ''}`}
                      onClick={() => { setSelectedBranchId(b.branch_id); setBranchDropOpen(false); }}>
                      {b.branch_name}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <span className={styles.branchTag}>
              {branchSt.branches.find(b => b.branch_id === selectedBranchId)?.branch_name ?? '—'}
            </span>
          )
        ) : (
          <span className={styles.branchTag}>All {activeBranchCount} active branches</span>
        )}

        {branchMode === 'all' && !allSt.loading && aggregates.length > 0 && (
          <span className={styles.coverageSummary}>
            {aggregates.filter(a => a.coverage === 'all-active').length} fully active ·{' '}
            {aggregates.filter(a => a.coverage === 'mixed').length} mixed ·{' '}
            {aggregates.filter(a => a.coverage === 'all-inactive').length} inactive
          </span>
        )}

        <div style={{ flex: 1 }} />
        <p className={styles.pageSubtitle} style={{ margin: 0 }}>Set which pay items each branch can use. Changes take effect from a chosen date.</p>
        <div className={styles.pageActions}>
          {!showAddButton && <span className={styles.readonlyNote}><LockIcon /> Read-only</span>}
          {showAddButton && (
            <button className={styles.btnPrimary} onClick={() => { dispatchWizard({ type: 'RESET' }); setCreateError(''); setCreateOpen(true); }}>
              <PlusIcon /> Add Custom Item
            </button>
          )}
        </div>
      </div>

      {/* ── Partial-load warning (All Branches) ── */}
      {branchMode === 'all' && allSt.partialWarning && (
        <div className={styles.warnBannerPage}>
          <WarnIcon />
          <div>
            <strong>{allSt.partialWarning}</strong>
            {allSt.failedBranches.length > 0 && (
              <ul style={{ margin: '0.3rem 0 0 1rem', padding: 0 }}>
                {allSt.failedBranches.map((f, i) => (
                  <li key={i} style={{ fontSize: '0.76rem' }}>{f.name}: {f.error}</li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {/* ── Body ── */}
      <div className={styles.body}>

        {/* ── LEFT: Item list ── */}
        <div className={styles.listCard}>
          <div className={styles.listToolbar}>
            <div className={styles.statusFilters}>
              {(['all', 'active', 'inactive', ...(branchMode === 'all' ? ['mixed' as const] : [])] as const).map(f => (
                <button key={f}
                  className={`${styles.filterPill}${statusFilter === f ? ` ${styles.filterPillActive}` : ''}`}
                  onClick={() => setStatusFilter(f)}>
                  {f.charAt(0).toUpperCase() + f.slice(1)}
                </button>
              ))}
            </div>

            <div className={styles.searchWrap}>
              <SearchIcon />
              <input className={styles.searchInput} type="search" placeholder="Search items…"
                value={search} onChange={e => setSearch(e.target.value)} />
            </div>
          </div>

          {/* Order save/cancel bars — one per scope, shown when user has a pending reorder */}
          {singleSt.localOrderDaily && (
            <div className={styles.orderBar}>
              <span className={styles.orderBarMsg}><DragIcon /> Daily order changed — save to persist</span>
              <button className={styles.btnPrimary}
                onClick={() => void saveOrderForScope('Daily')} disabled={orderSaving !== null}>
                {orderSaving === 'Daily' ? <><SpinnerIcon /> Saving…</> : 'Save Daily Order'}
              </button>
              <button className={styles.btnSecondary}
                onClick={() => dispatchSingle({ type: 'CANCEL_ORDER_DAILY' })} disabled={orderSaving !== null}>
                Cancel
              </button>
            </div>
          )}
          {singleSt.localOrderPeriod && (
            <div className={styles.orderBar}>
              <span className={styles.orderBarMsg}><DragIcon /> Pay Period order changed — save to persist</span>
              <button className={styles.btnPrimary}
                onClick={() => void saveOrderForScope('Period')} disabled={orderSaving !== null}>
                {orderSaving === 'Period' ? <><SpinnerIcon /> Saving…</> : 'Save Period Order'}
              </button>
              <button className={styles.btnSecondary}
                onClick={() => dispatchSingle({ type: 'CANCEL_ORDER_PERIOD' })} disabled={orderSaving !== null}>
                Cancel
              </button>
            </div>
          )}

          <div className={styles.tableWrap}>
            {branchSt.error && (
              <div className={styles.errorAlert} style={{ margin: '1rem' }}>
                <AlertIcon /> {branchSt.error}
              </div>
            )}
            {loadError && !branchSt.error && (
              <div className={styles.errorAlert} style={{ margin: '1rem' }}>
                <AlertIcon /> {loadError}
              </div>
            )}

            {/* Drag hint when filters prevent reordering */}
            {isAdmin && branchMode === 'single' && !canReorder && !singleSt.localOrderDaily && !singleSt.localOrderPeriod && !singleSt.loading && displayDaily.length > 0 && (
              <div className={styles.dragHint}>
                Clear search and status filter to drag-reorder.
              </div>
            )}

            <table className={styles.table}>
              <colgroup>
                {/* # order column — narrow, only in single-branch mode */}
                {branchMode === 'single' && <col style={{ width: '2.4rem' }} />}
                <col style={{ width: '30%' }} /><col style={{ width: '20%' }} /><col style={{ width: '90px' }} /><col style={{ width: '110px' }} /><col style={{ width: '120px' }} />
              </colgroup>
              <thead>
                <tr>
                  {branchMode === 'single' && <th className={styles.tdCenter} title="Drag to reorder"></th>}
                  <th>Pay Item</th>
                  <th className={styles.tdCenter}>Payment Type</th>
                  <th className={styles.tdCenter}>Rate Required</th>
                  <th className={styles.tdCenter}>Payroll Entry</th>
                  <th className={styles.tdCenter}>{branchMode === 'all' ? 'Coverage' : 'Status'}</th>
                </tr>
              </thead>
              <tbody>
                {loading && [...Array(5)].map((_, i) => (
                  <tr key={i} className={styles.skeletonRow}>
                    {[...Array(branchMode === 'single' ? 6 : 5)].map((__, j) => (
                      <td key={j}><div className={styles.skeleton} style={{ width: `${[30,75,60,40,40,55][j]}%` }} /></td>
                    ))}
                  </tr>
                ))}


                {/* ── Daily item rows ── */}
                {!loading && branchMode === 'single' && displayDaily.map((item, idx) => {
                  const isSelected = selSt.selectedItem?.pay_item_id === item.pay_item_id;
                  const isDragOver = dragOverId === item.pay_item_id;
                  return (
                    <tr key={item.pay_item_id}
                      className={[
                        styles.tableRow,
                        isSelected ? styles.tableRowSelected : '',
                        isDragOver ? styles.tableRowDragOver : '',
                        canReorder ? styles.tableRowDraggable : '',
                        !item.is_active ? styles.tableRowInactive : '',
                      ].filter(Boolean).join(' ')}
                      draggable={canReorder}
                      onDragStart={canReorder ? e => handleDragStart(e, item.pay_item_id) : undefined}
                      onDragOver={canReorder ? e => handleDragOver(e, item.pay_item_id) : undefined}
                      onDrop={canReorder ? e => handleDrop(e, item.pay_item_id) : undefined}
                      onDragEnd={canReorder ? handleDragEnd : undefined}
                      onDragLeave={canReorder ? () => setDragOverId(null) : undefined}
                      onClick={() => dispatchSel({ type: 'SELECT_ITEM', item })}>
                      <td className={styles.orderCell}>
                        {canReorder
                          ? <span className={styles.dragHandle}><DragIcon /></span>
                          : <span className={styles.orderNum}>{idx + 1}</span>}
                      </td>
                      <td>
                        <div className={styles.itemNameCell}>
                          <span className={styles.itemName} title={item.pay_item_name}>{item.pay_item_name}</span>
                        </div>
                      </td>
                      <td className={styles.rateCell} title={RATE_BEHAVIOR_LABELS[item.rate_behavior] ?? item.rate_behavior}>{RATE_BEHAVIOR_LABELS[item.rate_behavior] ?? item.rate_behavior}</td>
                      <td className={styles.tdCenter}>{item.requires_rate ? <BoolYes /> : <BoolNo />}</td>
                      <td className={styles.tdCenter}>{item.appears_in_payroll_entry ? <BoolYes /> : <BoolNo />}</td>
                      <td className={styles.tdCenter}><StatusBadge active={item.is_active} isDefault={item.is_using_default} /></td>
                    </tr>
                  );
                })}

                {!loading && branchMode === 'all' && filteredAgg.map(agg => (
                  <tr key={agg.pay_item_id}
                    className={`${styles.tableRow}${selSt.selectedAgg?.pay_item_id === agg.pay_item_id ? ` ${styles.tableRowSelected}` : ''}`}
                    onClick={() => dispatchSel({ type: 'SELECT_AGG', agg })}>
                    <td>
                      <div className={styles.itemNameCell}>
                        <span className={styles.itemName}>{agg.pay_item_name}</span>
                      </div>
                    </td>
                    <td className={styles.rateCell}>{RATE_BEHAVIOR_LABELS[agg.rate_behavior] ?? agg.rate_behavior}</td>
                    <td className={styles.tdCenter}>{agg.requires_rate ? <BoolYes /> : <BoolNo />}</td>
                    <td className={styles.tdCenter}>{agg.appears_in_payroll_entry ? <BoolYes /> : <BoolNo />}</td>
                    <td><CoverageBadge coverage={agg.coverage} active={agg.activeBranches} total={agg.totalBranches} /></td>
                  </tr>
                ))}

                {!loading && branchMode === 'single' && displayDaily.length === 0 && !loadError && (
                  <tr className={styles.emptyRow}>
                    <td colSpan={6}>
                      {search ? `No items match "${search}".` : 'No pay items found for this filter.'}
                    </td>
                  </tr>
                )}
                {!loading && branchMode === 'all' && filteredAgg.length === 0 && !loadError && (
                  <tr className={styles.emptyRow}>
                    <td colSpan={5}>
                      {search ? `No items match "${search}".` : 'No pay items found for this filter.'}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* ── RIGHT: Detail panel ── */}
        <div className={styles.detailPanel}>
          {!selSt.selectedItem && !selSt.selectedAgg ? (
            <div className={styles.detailEmpty}>
              <div className={styles.detailEmptyIcon}><TagIcon size={40} /></div>
              <p className={styles.detailEmptyTitle}>Choose a pay item</p>
              <p className={styles.detailEmptyText}>
                Select an item to view configuration, branch coverage, effective dates, and history.
              </p>
            </div>
          ) : selSt.selectedItem ? (
            <SingleItemDetail
              item={selSt.selectedItem}
              isAdmin={isAdmin}
              onEdit={openEdit}
              onStartDelete={startDelete}
              usageLoading={usageLoading}
              showHistory={selSt.showHistory}
              history={selSt.history}
              historyLoading={selSt.historyLoading}
              onShowHistory={loadHistory}
              onHideHistory={() => dispatchSel({ type: 'HISTORY_HIDE' })}
            />
          ) : selSt.selectedAgg ? (
            <AggItemDetail agg={selSt.selectedAgg} isAdmin={isAdmin} onEdit={openEdit} />
          ) : null}
        </div>
      </div>

      {/* ── CDPI Request Review Section ── */}
      {showCdpiRequests && (
        <div className={styles.cdpiSection}>
          <div className={styles.cdpiSectionHeader}>
            <h3 className={styles.cdpiSectionTitle}>Custom Item Requests</h3>
            <div className={styles.cdpiFilterRow}>
              {(['PendingCompanyApproval', 'Draft', 'Approved', 'Rejected', 'all'] as const).map(f => (
                <button key={f}
                  className={`${styles.filterPill}${cdpiFilter === f ? ` ${styles.filterPillActive}` : ''}`}
                  onClick={() => setCdpiFilter(f)}>
                  {f === 'all' ? 'All'
                    : f === 'PendingCompanyApproval' ? 'Pending'
                    : f}
                </button>
              ))}
            </div>
          </div>

          {cdpiSt.loading && (
            <div className={styles.cdpiEmpty}><SpinnerIcon /> Loading requests…</div>
          )}
          {cdpiSt.error && !cdpiSt.loading && (
            <div className={styles.errorAlert} style={{ margin: '0.5rem 0' }}>
              <AlertIcon /> {cdpiSt.error}
            </div>
          )}
          {!cdpiSt.loading && !cdpiSt.error && cdpiSt.requests.length === 0 && (
            <div className={styles.cdpiEmpty}>No requests found.</div>
          )}

          {!cdpiSt.loading && cdpiSt.requests.length > 0 && (
            <div className={styles.cdpiRequestList}>
              {cdpiSt.requests.map(req => {
                const branchLabel = branchSt.branches.find(b => b.branch_id === req.requesting_branch_id)?.branch_name
                  ?? `Branch ${req.requesting_branch_id}`;
                return (
                  <div key={req.request_id} className={`${styles.cdpiRequestCard} ${styles[`cdpiStatus${req.status}`] ?? ''}`}>
                    <div className={styles.cdpiCardRow}>
                      <span className={`${styles.cdpiStatusBadge} ${styles[`cdpiStatusBadge${req.status}`] ?? ''}`}>
                        {req.status === 'PendingCompanyApproval' ? 'Pending' : req.status}
                      </span>
                      <span className={styles.cdpiCardName}>{req.item_name ?? '(unnamed)'}</span>
                      <span className={styles.cdpiCardMeta}>
                        {branchLabel}
                        {' · '}{req.input_type ?? '—'}
                        {' · '}{req.calc_method_key ?? '—'}
                        {req.unit ? ` · ${req.unit}` : ''}
                      </span>
                    </div>
                    {req.notes && (
                      <div className={styles.cdpiCardNotes}>{req.notes}</div>
                    )}
                    <div className={styles.cdpiCardDates}>
                      rev {req.revision}
                      {req.submitted_at_utc && ` · submitted ${fmtDate(req.submitted_at_utc)}`}
                      {!req.submitted_at_utc && req.updated_at_utc && ` · updated ${fmtDate(req.updated_at_utc)}`}
                    </div>
                    {/* Reviewer actions — only for company-wide reviewers on pending requests */}
                    {userCanDirectCreate && req.status === 'PendingCompanyApproval' && (
                      <div className={styles.cdpiCardActions}>
                        <button className={styles.btnPrimary}
                          onClick={() => openDecide(req, 'Approve')}>
                          Approve
                        </button>
                        <button className={styles.btnSecondary}
                          onClick={() => openDecide(req, 'ReturnToDraft')}>
                          Return to Draft
                        </button>
                        <button className={styles.btnDanger}
                          onClick={() => openDecide(req, 'Reject')}>
                          Reject
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* ── Decide modal (Approve / ReturnToDraft / Reject) ── */}
      {decideOpen && decideTarget && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget && !decideSaving) setDecideOpen(false); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>
                {decideAction === 'Approve' ? 'Approve Request'
                  : decideAction === 'Reject' ? 'Reject Request'
                  : 'Return to Draft'}
              </h2>
              <button className={styles.modalCloseBtn} onClick={() => setDecideOpen(false)} disabled={decideSaving}><CloseIcon /></button>
            </div>
            <div className={styles.modalBody}>
              <div className={styles.infoBanner}>
                <InfoIcon />
                <span>
                  <strong>{decideTarget.item_name ?? '(unnamed)'}</strong> from {
                    branchSt.branches.find(b => b.branch_id === decideTarget.requesting_branch_id)?.branch_name
                    ?? `Branch ${decideTarget.requesting_branch_id}`
                  }
                </span>
              </div>
              <div className={styles.formGroup} style={{ marginTop: '0.75rem' }}>
                <label className={styles.label}>
                  {decideAction === 'Approve' ? 'Approval note'
                    : decideAction === 'Reject' ? 'Reject reason'
                    : 'Return reason'} <span className={styles.required}>*</span>
                </label>
                <p className={styles.inputNote} style={{ marginBottom: '0.4rem' }}>
                  {decideAction === 'Approve'
                    ? 'Add a short approval note before approving this request.'
                    : decideAction === 'Reject'
                    ? 'Explain why this request is being rejected.'
                    : 'Explain what the branch should change before resubmitting.'}
                </p>
                <textarea className={styles.textarea} maxLength={500}
                  value={decideReason}
                  onChange={e => setDecideReason(e.target.value)}
                  placeholder={decideAction === 'Approve'
                    ? 'e.g. Approved — item meets company standards.'
                    : decideAction === 'Reject'
                    ? 'e.g. This item duplicates an existing company pay item.'
                    : 'e.g. Please clarify the unit and calculation method before resubmitting.'}
                  disabled={decideSaving} />
              </div>
              {decideError && <div className={styles.errorAlert}><AlertIcon /> {decideError}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button
                className={decideAction === 'Reject' ? styles.btnDanger : styles.btnPrimary}
                disabled={decideSaving || !decideReason.trim()}
                onClick={() => void executeDecide()}>
                {decideSaving ? <><SpinnerIcon /> Working…</>
                  : decideAction === 'Approve' ? 'Approve'
                  : decideAction === 'Reject'  ? 'Reject'
                  : 'Return to Draft'}
              </button>
              <button className={styles.btnSecondary} onClick={() => setDecideOpen(false)} disabled={decideSaving}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Edit config modal ── */}
      {editOpen && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget) setEditOpen(false); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>Edit Settings</h2>
              <button className={styles.modalCloseBtn} onClick={() => setEditOpen(false)}><CloseIcon /></button>
            </div>
            <div className={styles.modalBody}>
              {/* Bulk target selector */}
              {branchMode === 'all' && (
                <div className={styles.targetSelector}>
                  <span className={styles.targetSelectorLabel}>Apply to</span>
                  <div className={styles.targetOptions}>
                    <label className={styles.targetOption}>
                      <input type="radio" name="editTarget" value="all"
                        checked={editTarget === 'all'} onChange={() => setEditTarget('all')} />
                      All active branches
                    </label>
                    <label className={styles.targetOption}>
                      <input type="radio" name="editTarget" value="single"
                        checked={editTarget === 'single'} onChange={() => setEditTarget('single')} />
                      <select className={styles.select}
                        style={{ width: 'auto', padding: '0.15rem 0.4rem', fontSize: '0.8rem' }}
                        value={selectedBranchId ?? ''}
                        onChange={e => { setEditTarget('single'); setSelectedBranchId(Number(e.target.value)); }}>
                        {branchSt.branches.map(b => (
                          <option key={b.branch_id} value={b.branch_id}>{b.branch_name}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                </div>
              )}

              {/* Active toggle */}
              <div className={styles.toggleRow}>
                <div className={styles.toggleInfo}>
                  <span className={styles.toggleLabel}>Active</span>
                  <span className={styles.toggleDesc}>
                    Whether this pay item is enabled for payroll entry and calculations.
                  </span>
                </div>
                <label className={styles.switch}>
                  <input type="checkbox" checked={editIsActive}
                    onChange={e => setEditIsActive(e.target.checked)} />
                  <span className={styles.switchTrack} />
                </label>
              </div>

              {/* Effective date */}
              <div className={styles.formGroup}>
                <label className={styles.label}>Effective Date</label>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem',
                  cursor: 'pointer', fontSize: '0.82rem', marginBottom: '0.4rem', fontWeight: 500, color: '#374151' }}>
                  <input type="checkbox" checked={editUseSpecificDate}
                    onChange={e => setEditUseSpecificDate(e.target.checked)}
                    style={{ accentColor: '#2563eb' }} />
                  Pick a specific date
                </label>
                {editUseSpecificDate ? (
                  <>
                    <input type="date" className={styles.input} value={editDate}
                      min={todayISO()} onChange={e => setEditDate(e.target.value)} />
                    <span className={styles.inputNote}>Must be today or in the future. Cannot fall inside an open payroll period.</span>
                  </>
                ) : (
                  <div className={styles.inputNote}>
                    The backend will apply today's date, or automatically schedule after any active open payroll period.
                  </div>
                )}
              </div>

              {/* Notes */}
              <div className={styles.formGroup}>
                <label className={styles.label}>Notes</label>
                <textarea className={styles.textarea} value={editNotes}
                  onChange={e => setEditNotes(e.target.value)}
                  placeholder="Optional internal notes" maxLength={500} />
              </div>

              {/* Effective date / open period warning */}
              <div className={styles.warnBanner}>
                <WarnIcon />
                <span>
                  Changes take effect from the effective date and may affect open payroll periods.
                  {editTarget === 'all' && (
                    <> All branches are updated in one atomic transaction via the backend bulk endpoint.</>
                  )}
                </span>
              </div>

              {editError && <div className={styles.errorAlert}><AlertIcon /> {editError}</div>}
            </div>
            <div className={styles.modalFooter}>
              <button className={styles.btnPrimary} disabled={editSaving}
                onClick={() => {
                  if (editTarget === 'all') {
                    setBulkConfirmOpen(true);
                  } else {
                    void executeSave();
                  }
                }}>
                {editSaving ? <><SpinnerIcon /> Saving…</>
                  : editTarget === 'all' ? 'Apply to All Branches…' : 'Save Configuration'}
              </button>
              <button className={styles.btnSecondary} onClick={() => setEditOpen(false)} disabled={editSaving}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Bulk edit confirmation ── */}
      <ConfirmDialog
        open={bulkConfirmOpen}
        title="Apply to all branches?"
        message="This will update the pay item configuration across all active branches in one atomic operation. Either all branches are updated or none — the change is transactional."
        confirmLabel="Apply to All Branches"
        variant="primary"
        loading={editSaving}
        onConfirm={() => { setBulkConfirmOpen(false); void executeSave(); }}
        onCancel={() => setBulkConfirmOpen(false)}
      />

      {/* ── Create wizard ── */}
      {createOpen && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget && !createSaving) setCreateOpen(false); }}>
          <div className={`${styles.modal} ${styles.wizardModal}`}>
            {/* Header with step indicator */}
            <div className={styles.wizardHeader}>
              <div className={styles.wizardTitleRow}>
                <h2 className={styles.modalTitle}>Add Custom Pay Item</h2>
                <button className={styles.modalCloseBtn} onClick={() => setCreateOpen(false)} disabled={createSaving}><CloseIcon /></button>
              </div>
              <div className={styles.stepIndicator}>
                {([1, 2, 3] as const).map(n => (
                  <div key={n} className={styles.stepIndicatorItem}>
                    <div className={`${styles.stepDot} ${wizard.step === n ? styles.stepDotActive : wizard.step > n ? styles.stepDotDone : ''}`}>
                      {wizard.step > n ? <CheckIcon /> : n}
                    </div>
                    <span className={styles.stepLabel}>
                      {n === 1 ? 'Value Type' : n === 2 ? 'How It Works' : 'Name It'}
                    </span>
                    {n < 3 && <div className={`${styles.stepLine} ${wizard.step > n ? styles.stepLineDone : ''}`} />}
                  </div>
                ))}
              </div>
            </div>

            <form className={styles.wizardForm} onSubmit={saveCreate}>
              <div className={styles.wizardBody}>

                {/* ── Step 1: Value type ── */}
                {wizard.step === 1 && (
                  <WizardStep1 value_type={wizard.value_type} dispatch={dispatchWizard} />
                )}

                {/* ── Step 2: How does it work? ── */}
                {wizard.step === 2 && (
                  <WizardStep2
                    value_type={wizard.value_type}
                    rate_method={wizard.rate_method}
                    dispatch={dispatchWizard}
                  />
                )}

                {/* ── Step 3: Names ── */}
                {wizard.step === 3 && (
                  <WizardStep3
                    value_type={wizard.value_type!}
                    rate_method={wizard.rate_method}
                    item_name={wizard.item_name}
                    unit={wizard.unit}
                    notes={wizard.notes}
                    dispatch={dispatchWizard}
                  />
                )}

                {createError && <div className={styles.errorAlert} style={{ marginTop: '0.75rem' }}><AlertIcon /> {createError}</div>}
              </div>

              {/* Navigation footer */}
              <div className={styles.modalFooter}>
                {wizard.step > 1 && (
                  <button type="button" className={styles.btnSecondary}
                    onClick={() => dispatchWizard({ type: 'SET_STEP', step: (wizard.step - 1) as 1 | 2 | 3 })}
                    disabled={createSaving}>
                    ← Back
                  </button>
                )}
                <div style={{ flex: 1 }} />
                {wizard.step < 3 ? (
                  <button type="button" className={styles.btnPrimary}
                    onClick={() => { setCreateError(''); dispatchWizard({ type: 'SET_STEP', step: (wizard.step + 1) as 2 | 3 }); }}
                    disabled={wizard.step === 1 ? !wizardStep1Complete : !wizardStep2Complete}>
                    Next →
                  </button>
                ) : (
                  <button type="submit" className={styles.btnPrimary}
                    disabled={createSaving || !wizardStep3Complete}>
                    {createSaving
                      ? <><SpinnerIcon /> Submitting…</>
                      : userCanDirectCreate
                        ? 'Create Company Item'
                        : 'Submit for Approval'
                    }
                  </button>
                )}
                <button type="button" className={styles.btnSecondary}
                  onClick={() => setCreateOpen(false)} disabled={createSaving}>
                  Cancel
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* ── Delete confirm (with usage info) ── */}
      {deleteConfirmOpen && usageData && selSt.selectedItem && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget) setDeleteConfirmOpen(false); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>
                {usageData.deletion_would_retire ? 'Retire Pay Item' : 'Delete Pay Item'}
              </h2>
              <button className={styles.modalCloseBtn} onClick={() => setDeleteConfirmOpen(false)}><CloseIcon /></button>
            </div>
            <div className={styles.modalBody}>
              <div className={usageData.deletion_would_retire ? styles.warnBanner : styles.infoBanner}>
                {usageData.deletion_would_retire ? <WarnIcon /> : <InfoIcon />}
                <span>
                  {usageData.deletion_would_retire
                    ? `"${selSt.selectedItem.pay_item_name}" has been used in finalized or meaningful payroll data and cannot be physically deleted. It will be retired — the code is permanently locked and the item hidden from all UIs.`
                    : `"${selSt.selectedItem.pay_item_name}" has no meaningful usage and will be permanently deleted.`}
                </span>
              </div>
              <div className={styles.usageStats}>
                <div className={styles.usageStat}>
                  <span className={styles.usageStatLabel}>Finalized Lines</span>
                  <span className={styles.usageStatValue}>{usageData.final_line_count}</span>
                </div>
                <div className={styles.usageStat}>
                  <span className={styles.usageStatLabel}>Draft Lines</span>
                  <span className={styles.usageStatValue}>{usageData.meaningful_draft_line_count}</span>
                </div>
              </div>
            </div>
            <div className={styles.modalFooter}>
              <button className={styles.btnDanger} onClick={() => void confirmDelete()} disabled={deleteWorking}>
                {deleteWorking ? <><SpinnerIcon /> Working…</> : usageData.deletion_would_retire ? 'Retire Item' : 'Delete Permanently'}
              </button>
              <button className={styles.btnSecondary} onClick={() => setDeleteConfirmOpen(false)} disabled={deleteWorking}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Bulk result modal ── */}
      {bulkResultOpen && bulkResult && (
        <div className={styles.modalOverlay} onClick={e => { if (e.target === e.currentTarget) setBulkResultOpen(false); }}>
          <div className={styles.modal}>
            <div className={styles.modalHeader}>
              <h2 className={styles.modalTitle}>Bulk Update Applied</h2>
              <button className={styles.modalCloseBtn} onClick={() => setBulkResultOpen(false)}><CloseIcon /></button>
            </div>
            <div className={styles.modalBody}>
              <div className={styles.infoBanner}>
                <InfoIcon />
                <span>
                  {bulkResult.updated_branch_count} of {bulkResult.requested_branch_count} branch(es) updated
                  atomically. All changes were committed in one transaction.
                </span>
              </div>
              <div className={styles.bulkResultList}>
                {bulkResult.results.map(r => (
                  <div key={r.branch_id} className={`${styles.bulkResultItem} ${styles.bulkResultSuccess}`}>
                    <CheckIcon />
                    <span>
                      {r.branch_name} — <strong>{r.status}</strong> (effective {r.effective_from})
                    </span>
                  </div>
                ))}
              </div>
            </div>
            <div className={styles.modalFooter}>
              <button className={styles.btnPrimary} onClick={() => { setBulkResultOpen(false); setBulkResult(null); }}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}

// ─── Detail panel — single branch ────────────────────────────────────────────

function SingleItemDetail({
  item, isAdmin, onEdit, onStartDelete, usageLoading,
  showHistory, history, historyLoading, onShowHistory, onHideHistory,
}: {
  item: BranchPayItemState; isAdmin: boolean;
  onEdit: () => void; onStartDelete: () => void; usageLoading: boolean;
  showHistory: boolean; history: BranchPayItemConfigVersion[];
  historyLoading: boolean; onShowHistory: () => void; onHideHistory: () => void;
}) {
  return (
    <>
      <div className={styles.detailHeader}>
        <h2 className={styles.detailItemName}>{item.pay_item_name}</h2>
        <div className={styles.detailBadgeRow}>
          <ScopeBadge scope={item.item_scope} />
          <span style={{ fontSize: '0.72rem', color: '#94a3b8' }}>
            {item.is_system_standard ? 'Standard' : 'Custom'}
          </span>
        </div>
      </div>

      <div className={styles.detailBody}>
        {/*
          Config banners — display priority:
          1. pending_config (scheduled future change) — always shown if present
          2. current state (system default OR branch override)
        */}
        {item.pending_config && (
          <div className={`${styles.configBanner} ${styles.configBannerPending}`}>
            <WarnIcon />
            Scheduled: will be <strong>{item.pending_config.is_active ? 'activated' : 'deactivated'}</strong> on {fmtDate(item.pending_config.effective_from)}
          </div>
        )}
        {item.is_using_default ? (
          <div className={`${styles.configBanner} ${styles.configBannerDefault}`}>
            <InfoIcon /> Using company default — not customized for this branch
          </div>
        ) : (
          <div className={`${styles.configBanner} ${styles.configBannerCustom}`}>
            <CheckIcon /> Customized for this branch since {fmtDate(item.current_config?.effective_from ?? null)}
          </div>
        )}

        <div className={styles.detailSection}>
          <p className={styles.detailSectionTitle}>Branch Settings</p>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Status</span>
            <span className={styles.detailValue}><StatusBadge active={item.is_active} isDefault={item.is_using_default} /></span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Active Since</span>
            <span className={styles.detailValue}>{fmtDate(item.current_config?.effective_from ?? null)}</span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Notes</span>
            <span className={item.notes ? styles.detailValue : styles.detailValueMuted}>
              {item.notes ?? 'None'}
            </span>
          </div>
        </div>

        <div className={styles.detailSection}>
          <p className={styles.detailSectionTitle}>Item Details</p>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Payment Type</span>
            <span className={styles.detailValue}>{RATE_BEHAVIOR_LABELS[item.rate_behavior] ?? item.rate_behavior}</span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Rate Required</span>
            <span className={item.requires_rate ? styles.detailValueGood : styles.detailValue}>{item.requires_rate ? 'Yes' : 'No'}</span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Payroll Entry</span>
            <span className={item.appears_in_payroll_entry ? styles.detailValueGood : styles.detailValueMuted}>
              {item.appears_in_payroll_entry ? 'Yes' : 'No'}
            </span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>In Ledger</span>
            <span className={styles.detailValue}>{item.appears_in_ledger ? 'Yes' : 'No'}</span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>In Reports</span>
            <span className={styles.detailValue}>{item.appears_in_reports ? 'Yes' : 'No'}</span>
          </div>
          {item.unit && (
            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Measured In</span>
              <span className={styles.detailValue}>{item.unit}</span>
            </div>
          )}
          {item.line_type_mappings?.length > 0 && (
            <div className={styles.detailRow}>
              <span className={styles.detailLabel}>Line Categories</span>
              <span className={styles.detailValue}>{item.line_type_mappings.join(', ')}</span>
            </div>
          )}
        </div>

        {showHistory && (
          <div className={styles.detailSection}>
            <p className={styles.detailSectionTitle}>Change History</p>
            {historyLoading ? (
              <div className={styles.skeleton} style={{ width: '80%' }} />
            ) : history.length === 0 ? (
              <span className={styles.detailValueMuted}>No changes recorded yet.</span>
            ) : (
              <div className={styles.historySection}>
                {history.map(h => (
                  <div key={h.config_id} className={styles.historyItem}>
                    <span className={styles.historyRange}>
                      {fmtDate(h.effective_from)} → {h.effective_to ? fmtDate(h.effective_to) : 'Present'}
                      {' '}<StatusBadge active={h.is_active} />
                    </span>
                    {h.notes && <span className={styles.historyMeta}>{h.notes}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div className={styles.detailActions}>
        {isAdmin ? (
          <>
            <div className={styles.actionRow}>
              <button className={`${styles.btnPrimary} ${styles.btnFullWidth}`} onClick={onEdit}>
                <EditIcon /> Edit Settings
              </button>
            </div>
            <div className={styles.actionRow}>
              <button className={styles.btnGhost}
                onClick={() => { if (showHistory) onHideHistory(); else onShowHistory(); }}>
                <HistoryIcon /> {showHistory ? 'Hide History' : 'View History'}
              </button>
              {!item.is_system_standard && (
                <button className={styles.btnDanger} onClick={onStartDelete} disabled={usageLoading}>
                  {usageLoading ? <SpinnerIcon /> : <TrashIcon />}
                  {usageLoading ? 'Checking…' : 'Delete / Retire'}
                </button>
              )}
            </div>
          </>
        ) : (
          <div className={styles.actionRow}>
            <button className={styles.btnGhost}
              onClick={() => { if (showHistory) onHideHistory(); else onShowHistory(); }}>
              <HistoryIcon /> {showHistory ? 'Hide History' : 'View History'}
            </button>
          </div>
        )}
      </div>
    </>
  );
}

// ─── Detail panel — all branches ──────────────────────────────────────────────

function AggItemDetail({ agg, isAdmin, onEdit }: { agg: AggregateItem; isAdmin: boolean; onEdit: () => void }) {
  return (
    <>
      <div className={styles.detailHeader}>
        <h2 className={styles.detailItemName}>{agg.pay_item_name}</h2>
        <div className={styles.detailBadgeRow}>
          <ScopeBadge scope={agg.item_scope} />
          <span style={{ fontSize: '0.72rem', color: '#94a3b8' }}>
            {agg.is_system_standard ? 'Standard' : 'Custom'}
          </span>
        </div>
      </div>

      <div className={styles.detailBody}>
        <div className={styles.detailSection}>
          <p className={styles.detailSectionTitle}>Branch Coverage</p>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Overall</span>
            <span className={styles.detailValue}>
              <CoverageBadge coverage={agg.coverage} active={agg.activeBranches} total={agg.totalBranches} />
            </span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Active in</span>
            <span className={agg.activeBranches > 0 ? styles.detailValueGood : styles.detailValueMuted}>
              {agg.activeBranches} / {agg.totalBranches} branches
            </span>
          </div>
        </div>

        <div className={styles.detailSection}>
          <p className={styles.detailSectionTitle}>Per Branch</p>
          <table className={styles.branchCoverageTable}>
            <thead>
              <tr><th>Branch</th><th>Status</th><th>Since</th></tr>
            </thead>
            <tbody>
              {agg.perBranch.map(pb => (
                <tr key={pb.branch_id}>
                  <td>{pb.branch_name}</td>
                  <td><StatusBadge active={pb.is_active} isDefault={pb.is_using_default} /></td>
                  <td style={{ fontSize: '0.72rem', color: '#94a3b8' }}>
                    {pb.is_using_default ? 'Default' : pb.current_config ? fmtDate(pb.current_config.effective_from) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className={styles.detailSection}>
          <p className={styles.detailSectionTitle}>Item Properties</p>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Rate Behavior</span>
            <span className={styles.detailValue}>{RATE_BEHAVIOR_LABELS[agg.rate_behavior] ?? agg.rate_behavior}</span>
          </div>
          <div className={styles.detailRow}>
            <span className={styles.detailLabel}>Payroll Entry</span>
            <span className={agg.appears_in_payroll_entry ? styles.detailValueGood : styles.detailValueMuted}>
              {agg.appears_in_payroll_entry ? 'Visible' : 'Hidden'}
            </span>
          </div>
        </div>

        <div className={styles.infoBanner} style={{ fontSize: '0.76rem' }}>
          <InfoIcon />
          <span>
            History is per-branch. Switch to <strong>Single Branch</strong> mode to view detailed config history.
          </span>
        </div>
      </div>

      {isAdmin && (
        <div className={styles.detailActions}>
          <button className={`${styles.btnPrimary} ${styles.btnFullWidth}`} onClick={onEdit}>
            <EditIcon /> Edit Settings (Bulk)
          </button>
        </div>
      )}
    </>
  );
}

// ─── Wizard step components ───────────────────────────────────────────────────

function WizardStep1({
  value_type, dispatch,
}: {
  value_type: WizardValueType | null;
  dispatch: React.Dispatch<WizardAction>;
}) {
  const options = [
    {
      value: 'Time'   as WizardValueType,
      title: 'Time / Hours',
      helper: 'Accepts time formats like 1:00:00, 1hr 30min, or 1.5 hours. The system calculates pay using the rate you configure.',
    },
    {
      value: 'Number' as WizardValueType,
      title: 'Quantity / Number',
      helper: 'Any numeric value — stops, loads, miles, pallets, or any quantity your company uses. The system calculates pay using the rate you configure.',
    },
  ];

  return (
    <div className={styles.wizardStep}>
      <p className={styles.wizardQuestion}>What will be entered each day?</p>
      <div className={styles.scopeCards}>
        {options.map(opt => (
          <button key={opt.value} type="button"
            className={`${styles.scopeCard}${value_type === opt.value ? ` ${styles.scopeCardSelected}` : ''}`}
            onClick={() => dispatch({ type: 'SET_VALUE_TYPE', vt: opt.value })}>
            <span className={styles.scopeCardTitle}>{opt.title}</span>
            <span className={styles.scopeCardHelper}>{opt.helper}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

function WizardStep2({
  value_type, rate_method, dispatch,
}: {
  value_type: WizardValueType | null;
  rate_method: WizardRateMethod | null;
  dispatch: React.Dispatch<WizardAction>;
}) {
  const config = value_type === 'Time' ? STEP2_TIME : STEP2_NUMBER;
  const isAdvancedInitiallyOpen = value_type !== null && value_type !== 'Money'
    && rate_method !== null && config.advanced.methods.some(m => m.value === rate_method);
  const [advancedOpen, setAdvancedOpen] = useState(isAdvancedInitiallyOpen);

  if (!value_type || value_type === 'Money') return <div className={styles.wizardStep} />;

  return (
    <div className={styles.wizardStep}>
      <p className={styles.wizardQuestion}>{config.question}</p>
      <p className={styles.wizardHint}>{config.hint}</p>

      <div className={styles.rateMethods}>
        {config.normal.map(m => (
          <MethodCard
            key={m.value}
            config={m}
            selected={rate_method === m.value}
            onSelect={() => dispatch({ type: 'SET_RATE_METHOD', method: m.value })}
            disabled={m.value !== 'PerUnit'}
          />
        ))}
      </div>

      <div className={styles.advancedSection}>
        <button
          type="button"
          className={styles.advancedToggle}
          onClick={() => setAdvancedOpen(o => !o)}
          aria-expanded={advancedOpen}
        >
          <span className={styles.advancedToggleArrow}>{advancedOpen ? '▾' : '▸'}</span>
          {config.advanced.label}
        </button>
        {advancedOpen && (
          <>
            <p className={styles.advancedSupportText}>{config.advanced.supportText}</p>
            <div className={styles.rateMethods}>
              {config.advanced.methods.map(m => (
                <MethodCard
                  key={m.value}
                  config={m}
                  selected={false}
                  onSelect={() => {/* disabled — not selectable */}}
                  disabled
                />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function MethodCard({
  config, selected, onSelect, disabled,
}: {
  config: MethodDisplayConfig;
  selected: boolean;
  onSelect: () => void;
  disabled?: boolean;
}) {
  const [popoverOpen, setPopoverOpen] = useState(false);
  const wrapperRef  = useRef<HTMLDivElement>(null);
  const infoBtnRef  = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!popoverOpen) return;
    const close = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setPopoverOpen(false);
      }
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [popoverOpen]);

  return (
    <div
      ref={wrapperRef}
      className={styles.methodCardWrapper}
      onKeyDown={e => {
        if (e.key === 'Escape' && popoverOpen) {
          setPopoverOpen(false);
          infoBtnRef.current?.focus();
        }
      }}
    >
      {/* Card — div with role=button so info <button> can be nested inside */}
      <div
        role={disabled ? 'presentation' : 'button'}
        tabIndex={disabled ? -1 : 0}
        className={`${styles.rateMethodCard}${selected ? ` ${styles.rateMethodCardSelected}` : ''}${disabled ? ` ${styles.rateMethodCardDisabled}` : ''}`}
        onClick={disabled ? undefined : onSelect}
        onKeyDown={disabled ? undefined : e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(); } }}
        aria-disabled={disabled}
      >
        <div className={styles.rateMethodCardTop}>
          <div className={styles.rateMethodCardLeft}>
            <span className={styles.rateMethodLabel}>{config.label}</span>
            {disabled
              ? <span className={styles.rateMethodBadgeComingSoon}>Coming later</span>
              : config.badge && <span className={styles.rateMethodBadge}>{config.badge}</span>
            }
          </div>
          <div className={styles.rateMethodCardRight}>
            {selected && !disabled && <span className={styles.rateMethodCheck}><CheckIcon /></span>}
            {!disabled && (
              <button
                ref={infoBtnRef}
                type="button"
                className={styles.rateMethodInfoBtn}
                aria-label={`Learn more about ${config.label}`}
                aria-expanded={popoverOpen}
                onClick={e => { e.stopPropagation(); setPopoverOpen(o => !o); }}
              >
                <InfoCircleIcon />
              </button>
            )}
          </div>
        </div>
      </div>

      {popoverOpen && (
        <div className={styles.rateMethodPopover} role="region" aria-label={`Details for ${config.label}`}>
          <p className={styles.rateMethodPopoverExpl}>{config.popoverExpl}</p>
          <pre className={styles.rateMethodPopoverExample}>{config.popoverExample}</pre>
          {config.popoverNote && (
            <p className={styles.rateMethodPopoverNote}>{config.popoverNote}</p>
          )}
        </div>
      )}
    </div>
  );
}

function WizardStep3({
  value_type, rate_method, item_name, unit, notes, dispatch,
}: {
  value_type: WizardValueType;
  rate_method: WizardRateMethod | null;
  item_name: string;
  unit: string;
  notes: string;
  dispatch: React.Dispatch<WizardAction>;
}) {
  const methodDef = RATE_METHODS.find(m => m.value === rate_method);

  return (
    <div className={styles.wizardStep}>
      {/* Summary chip */}
      <div className={styles.wizardSummaryChip}>
        <span className={styles.wizardSummaryChipPart}>
          {value_type === 'Time' ? 'Time / Hours' : 'Quantity / Number'}
        </span>
        {rate_method && (
          <>
            <span className={styles.wizardSummaryChipSep}>·</span>
            <span className={styles.wizardSummaryChipPart}>{methodDef?.label ?? rate_method}</span>
          </>
        )}
      </div>

      {/* Item name */}
      <div className={styles.formGroup} style={{ marginTop: '1rem' }}>
        <label className={styles.label}>Pay Item Name <span className={styles.required}>*</span></label>
        <input className={styles.input} autoFocus maxLength={120}
          value={item_name}
          onChange={e => dispatch({ type: 'SET_ITEM_NAME', name: e.target.value })}
          placeholder="e.g. Loads Delivered Bonus" />
        <span className={styles.inputNote}>The official name shown in payroll reports and screens.</span>
      </div>

      {/* Unit — optional display metadata */}
      <div className={styles.formGroup}>
        <label className={styles.label}>Unit <span className={styles.optional}>(optional)</span></label>
        <input className={styles.input} maxLength={60}
          value={unit}
          onChange={e => dispatch({ type: 'SET_UNIT', unit: e.target.value })}
          placeholder={value_type === 'Time' ? 'e.g. hours' : 'e.g. loads, miles, stops'} />
        <span className={styles.inputNote}>Display label for what is being measured. Does not affect calculations.</span>
      </div>

      {/* Notes */}
      <div className={styles.formGroup}>
        <label className={styles.label}>Notes <span className={styles.optional}>(optional)</span></label>
        <textarea className={styles.textarea} maxLength={500}
          value={notes}
          onChange={e => dispatch({ type: 'SET_NOTES', notes: e.target.value })}
          placeholder="Internal notes about this pay item" />
      </div>

      <div className={styles.infoBanner} style={{ marginTop: '0.5rem' }}>
        <InfoIcon />
        <span>
          Pay rates will be configured in <strong>Pay Rates</strong> after this item is approved.
          The item starts <strong>inactive</strong> on all branches — your company admin will
          review and approve this request before it becomes available.
        </span>
      </div>
    </div>
  );
}


// ─── Badges ───────────────────────────────────────────────────────────────────

function ScopeBadge({ scope }: { scope: string }) {
  const cls = scope === 'Daily' ? styles.badgeScopeDaily
    : scope === 'Period' ? styles.badgeScopePeriod
    : styles.badgeScopeSummary;
  return <span className={`${styles.badgeScope} ${cls}`}>{scope}</span>;
}

function StatusBadge({ active, isDefault }: { active: boolean; isDefault?: boolean }) {
  if (isDefault && active) {
    return (
      <span className={`${styles.badgeStatus} ${styles.badgeDefault}`}>
        <span className={`${styles.dot} ${styles.dotActive}`} /> Active (default)
      </span>
    );
  }
  return (
    <span className={`${styles.badgeStatus} ${active ? styles.badgeActive : styles.badgeInactive}`}>
      <span className={`${styles.dot} ${active ? styles.dotActive : styles.dotInactive}`} />
      {active ? 'Active' : 'Inactive'}
    </span>
  );
}

function CoverageBadge({ coverage, active, total }: { coverage: string; active: number; total: number }) {
  const cls    = coverage === 'all-active' ? styles.badgeActive : coverage === 'mixed' ? styles.badgeMixed : styles.badgeInactive;
  const dotCls = coverage === 'all-active' ? styles.dotActive  : coverage === 'mixed' ? styles.dotMixed  : styles.dotInactive;
  const label  = coverage === 'all-active' ? 'All active' : coverage === 'all-inactive' ? 'All inactive' : `Mixed (${active}/${total})`;
  return (
    <span className={`${styles.badgeStatus} ${cls}`}>
      <span className={`${styles.dot} ${dotCls}`} /> {label}
    </span>
  );
}

function BoolYes() { return <span style={{ color: '#16a34a', fontWeight: 700, fontSize: '0.78rem' }}>✓</span>; }
function BoolNo()  { return <span style={{ color: '#94a3b8', fontSize: '0.78rem' }}>—</span>; }

// ─── Icons ────────────────────────────────────────────────────────────────────

function TagIcon({ size = 24 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>;
}
function PlusIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>;
}
function CloseIcon() {
  return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>;
}
function CheckIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }} aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>;
}
function AlertIcon() {
  return <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }} aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>;
}
function WarnIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }} aria-hidden="true"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>;
}
function InfoIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }} aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>;
}
function InfoCircleIcon() {
  return <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>;
}
function EditIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>;
}
function HistoryIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-3.38"/></svg>;
}
function TrashIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>;
}
function SearchIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={styles.searchIcon} aria-hidden="true"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>;
}
function LockIcon() {
  return <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }} aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>;
}
function SpinnerIcon() {
  return <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" className={styles.spinner} aria-hidden="true"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>;
}
function DragIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor" aria-hidden="true">
      <circle cx="3.5" cy="2"  r="1.1"/>
      <circle cx="8.5" cy="2"  r="1.1"/>
      <circle cx="3.5" cy="6"  r="1.1"/>
      <circle cx="8.5" cy="6"  r="1.1"/>
      <circle cx="3.5" cy="10" r="1.1"/>
      <circle cx="8.5" cy="10" r="1.1"/>
    </svg>
  );
}
