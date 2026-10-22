import { useEffect, useState } from 'react';
import { getBranchStatusKeyLimits, putBranchStatusKeyLimits } from '../../../lib/settingsApi';
import type { StatusKeyBranchLimits } from '../../../types/settings';
import { limitsToPayload, validateLimits, type LimitsForm } from './statusLimitsModel';
import { apiError } from './statusKeysModel';

/** The usage limits of every active Status in one Branch, and the writer of one Status. */
export function useBranchStatusLimits(branchId: number | null) {
  const [loaded, setLoaded] = useState<{ branchId: number; limits: StatusKeyBranchLimits[]; error: string } | null>(null);
  const [savingKeyId, setSavingKeyId] = useState<number | null>(null);
  const [saveError, setSaveError] = useState<{ keyId: number; message: string } | null>(null);

  useEffect(() => {
    if (branchId == null) return;
    let cancelled = false;
    getBranchStatusKeyLimits(branchId)
      .then(({ data }) => { if (!cancelled) setLoaded({ branchId, limits: data, error: '' }); })
      .catch((e) => { if (!cancelled) setLoaded({ branchId, limits: [], error: apiError(e) }); });
    return () => { cancelled = true; };
  }, [branchId]);

  // Rows of another Branch are never shown while this one is loading.
  const current = loaded !== null && loaded.branchId === branchId ? loaded : null;

  async function save(statusKeyId: number, form: LimitsForm): Promise<StatusKeyBranchLimits | null> {
    if (branchId == null) return null;
    const problem = validateLimits(form);
    if (problem) { setSaveError({ keyId: statusKeyId, message: problem }); return null; }
    setSavingKeyId(statusKeyId); setSaveError(null);
    try {
      const { data } = await putBranchStatusKeyLimits(branchId, statusKeyId, limitsToPayload(form));
      setLoaded((prev) => prev && prev.branchId === branchId
        ? { ...prev, limits: prev.limits.map((l) => l.status_key_id === data.status_key_id ? data : l) }
        : prev);
      return data;
    } catch (e) {
      setSaveError({ keyId: statusKeyId, message: apiError(e) });
      return null;
    } finally { setSavingKeyId(null); }
  }

  return {
    loading: branchId != null && current === null,
    limits: current?.limits ?? [],
    error: current?.error ?? '',
    savingKeyId,
    saveError,
    clearSaveError: () => setSaveError(null),
    save,
  };
}
