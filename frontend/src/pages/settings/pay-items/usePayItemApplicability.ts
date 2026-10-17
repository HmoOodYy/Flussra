import { useEffect, useMemo, useState } from 'react';
import {
  listBranchPayDefinitionHistory,
  listBranchPayDefinitions,
} from '../../../lib/compensationApi';
import type { BranchConfigVersion, BranchPayDefinitionState } from '../../../types/compensation';
import type { BranchAdmin } from '../../../types/settings';
import { apiError, buildAggregates } from './payItemsModel';

interface UsePayItemApplicabilityArgs {
  branchMode: 'single' | 'all';
  selectedBranchId: number | null;
  branches: BranchAdmin[];
  activeBranches: BranchAdmin[];
  reloadKey: number;
  selectedId: number | null;
  setSelectedId(value: number | null): void;
}

export function usePayItemApplicability({
  branchMode, selectedBranchId, branches, activeBranches, reloadKey, selectedId, setSelectedId,
}: UsePayItemApplicabilityArgs) {
  const [items, setItems] = useState<BranchPayDefinitionState[]>([]);
  const [byBranch, setByBranch] = useState<Record<number, BranchPayDefinitionState[]>>({});
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'active' | 'inactive' | 'mixed'>('all');
  const [history, setHistory] = useState<BranchConfigVersion[] | null>(null);

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

  async function loadHistory() {
    if (selectedId === null || selectedBranchId === null) return;
    try {
      setHistory(await listBranchPayDefinitionHistory(selectedBranchId, selectedId));
    } catch {
      setHistory([]);
    }
  }

  return {
    items, loading, loadError, search, setSearch, statusFilter, setStatusFilter,
    history, setHistory, visibleItems, visibleAggregates,
    selectedItem, selectedAgg, loadHistory,
  };
}