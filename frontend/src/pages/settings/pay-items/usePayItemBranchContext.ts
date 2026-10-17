import { useEffect, useState } from 'react';
import { getSettingsBranches } from '../../../lib/settingsApi';
import type { BranchAdmin } from '../../../types/settings';
import { apiError } from './payItemsModel';

export function usePayItemBranchContext() {
  const [branches, setBranches] = useState<BranchAdmin[]>([]);
  const [branchesError, setBranchesError] = useState('');
  const [branchMode, setBranchMode] = useState<'single' | 'all'>('single');
  const [selectedBranchId, setSelectedBranchId] = useState<number | null>(null);

  useEffect(() => {
    getSettingsBranches()
      .then(({ data }) => {
        setBranches(data);
        setSelectedBranchId(current => current ?? (data.find(b => b.is_default) ?? data[0])?.branch_id ?? null);
      })
      .catch(e => setBranchesError(apiError(e)));
  }, []);

  const activeBranches = branches.filter(b => b.status === 'Active');
  const branchName = (id: number) => branches.find(b => b.branch_id === id)?.branch_name ?? `Branch ${id}`;

  return {
    branches, branchesError, branchMode, setBranchMode,
    selectedBranchId, setSelectedBranchId, activeBranches, branchName,
  };
}