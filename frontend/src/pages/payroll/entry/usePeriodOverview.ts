import { useCallback, useEffect, useState } from 'react';
import { getPeriodEntryOverview } from '../../../lib/payrollApi';
import type { PeriodEntryOverviewResponse } from '../../../types/payroll/entry';

/**
 * The saved entries of every day of the period, read beside the Day Grid for the day strip, the
 * Week view and the driver panel. It is extra information: when the read fails `overview` is
 * null and the page works without it. `refresh` reads it again, e.g. after a day was saved.
 */
export function usePeriodOverview(periodId: number) {
  const [overview, setOverview] = useState<PeriodEntryOverviewResponse | null>(null);
  const [reads, setReads] = useState(0);

  useEffect(() => {
    let current = true;
    getPeriodEntryOverview(periodId)
      .then((data) => { if (current) setOverview(data); })
      .catch(() => { if (current) setOverview(null); });
    return () => { current = false; };
  }, [periodId, reads]);

  const refresh = useCallback(() => setReads((count) => count + 1), []);

  return { overview, refresh };
}
