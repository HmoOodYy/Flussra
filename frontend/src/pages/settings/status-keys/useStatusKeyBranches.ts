import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { getSettingsBranches } from '../../../lib/settingsApi';
import type { BranchAdmin } from '../../../types/settings';

export function useStatusKeyBranches() {
  // ── URL params (deep-link from Company & Branches "Missing" badge) ────────
  const [searchParams] = useSearchParams();

  // Read branchId param once at mount into a ref so it can be consumed by the
  // branch-load effect without becoming a reactive dependency (avoids re-runs).
  const urlBranchId = Number(searchParams.get('branchId'));
  const urlBranchIdRef = useRef<number | null>(urlBranchId > 0 ? urlBranchId : null);

  // ── Branches ──────────────────────────────────────────────────────────────
  const [branches, setBranches] = useState<BranchAdmin[]>([]);
  const [branchesLoading, setBranchesLoading] = useState(true);
  const [selectedBranchId, setSelectedBranchId] = useState<number | null>(null);

  useEffect(() => {
    getSettingsBranches()
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

  const selectedBranch = useMemo(
    () => branches.find(b => b.branch_id === selectedBranchId) ?? null,
    [branches, selectedBranchId],
  );

  return { branches, branchesLoading, selectedBranchId, setSelectedBranchId, selectedBranch };
}
