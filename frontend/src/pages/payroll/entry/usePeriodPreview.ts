import { useCallback, useEffect, useState } from 'react';
import { getCalculationPreview } from '../../../lib/payrollApi';
import type { CalculationPreviewResponse } from '../../../types/payroll/calculation';

/** Period statuses whose calculation preview the backend serves. */
const PREVIEW_STATUSES = new Set(['Open', 'Returned']);

/**
 * The live calculation preview of an Open or Returned period, read beside the Day Grid. It is
 * extra information: when the period has none or the read fails, `preview` is null and the page
 * shows its amounts as unavailable (`previewFailed` tells a failed read from one still under way).
 * `refresh` reads it again, e.g. after the day was saved.
 */
export function usePeriodPreview(periodId: number, periodStatus: string | null) {
  const [preview, setPreview] = useState<CalculationPreviewResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const [reads, setReads] = useState(0);
  const available = periodStatus !== null && PREVIEW_STATUSES.has(periodStatus);

  useEffect(() => {
    if (!available) return;
    let current = true;
    getCalculationPreview(periodId)
      .then((data) => { if (current) { setPreview(data); setFailed(false); } })
      .catch(() => { if (current) { setPreview(null); setFailed(true); } });
    return () => { current = false; };
  }, [periodId, available, reads]);

  const refresh = useCallback(() => setReads((count) => count + 1), []);

  const ready = available ? preview : null;
  return { preview: ready, previewFailed: available && failed, previewLoading: available && ready === null && !failed, refresh };
}
