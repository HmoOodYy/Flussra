/**
 * Ledger discovery — merges the two independently-authoritative backend
 * sources (finalized library vs. operational periods) into one list without
 * ever deriving capability from active_permissions or role codes.
 *
 * A 403 from either authority means that authority grants nothing for the
 * requested branch scope; any other failure (network, 401, 404, 422, 5xx) is
 * a real load failure and must propagate.
 */
import { isAxiosError } from 'axios';
import type { FinalizedPeriodListItem, PeriodSummary } from '../../types/payroll';

export type LedgerDiscoveryStatus = 'Locked' | 'Archived';

export interface LedgerDiscoveryDeps {
  fetchFinalized: (status: LedgerDiscoveryStatus, branchId?: number) => Promise<FinalizedPeriodListItem[]>;
  fetchOperational: (status: LedgerDiscoveryStatus, branchId?: number) => Promise<PeriodSummary[]>;
}

/**
 * A merged payroll period. `finalized` is set only when the finalized-library
 * authority returned this period (unlocks the library action); `operational`
 * is set only when the operational authority returned it (unlocks Final
 * Lines). Both may be set when the same period is authoritative in both.
 */
export interface LedgerDiscoveryPeriod {
  identity: number;
  finalized: FinalizedPeriodListItem | null;
  operational: PeriodSummary | null;
}

export function isDiscoveryForbidden(error: unknown): boolean {
  return isAxiosError(error) && error.response?.status === 403;
}

interface LedgerSortFields {
  startDate: string;
  branchName: string;
}

function sortFieldsOf(p: LedgerDiscoveryPeriod): LedgerSortFields | null {
  if (p.finalized) return { startDate: p.finalized.start_date, branchName: p.finalized.branch_name };
  if (p.operational) return { startDate: p.operational.start_date, branchName: p.operational.branch_name };
  return null;
}

// Mirrors the backend's canonical ledger ordering (startdate DESC, branchname,
// identity DESC as the tiebreak — see finalized_library_read_model.py) so
// merging two sources never regroups rows by which authority found them.
function compareLedgerPeriods(a: LedgerDiscoveryPeriod, b: LedgerDiscoveryPeriod): number {
  const af = sortFieldsOf(a);
  const bf = sortFieldsOf(b);
  if (af == null || bf == null) return 0;
  if (af.startDate !== bf.startDate) return af.startDate > bf.startDate ? -1 : 1;
  if (af.branchName !== bf.branchName) return af.branchName < bf.branchName ? -1 : 1;
  return b.identity - a.identity;
}

async function discoverAuthority<T>(fetcher: () => Promise<T[]>): Promise<T[]> {
  try {
    return await fetcher();
  } catch (error) {
    if (isDiscoveryForbidden(error)) return [];
    throw error;
  }
}

export async function discoverLedgerPeriods(
  deps: LedgerDiscoveryDeps,
  status: LedgerDiscoveryStatus,
  branchId?: number,
): Promise<LedgerDiscoveryPeriod[]> {
  const [finalizedItems, operationalItems] = await Promise.all([
    discoverAuthority(() => deps.fetchFinalized(status, branchId)),
    discoverAuthority(() => deps.fetchOperational(status, branchId)),
  ]);

  const byIdentity = new Map<number, LedgerDiscoveryPeriod>();

  for (const item of finalizedItems) {
    byIdentity.set(item.period_id, { identity: item.period_id, finalized: item, operational: null });
  }
  for (const item of operationalItems) {
    const existing = byIdentity.get(item.payroll_period_id);
    if (existing) {
      existing.operational = item;
    } else {
      byIdentity.set(item.payroll_period_id, {
        identity: item.payroll_period_id,
        finalized: null,
        operational: item,
      });
    }
  }

  return Array.from(byIdentity.values()).sort(compareLedgerPeriods);
}
