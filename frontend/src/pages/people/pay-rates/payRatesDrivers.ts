import type { DriverAssignmentRosterRow } from '../../../types/core.ts';
import type { DriverPayRateRow } from '../../../types/compensation.ts';

/**
 * Pay Rates is Driver-centric: ordinary rates and Min/Max pay rules belong to the permanent
 * Driver, so the roster shows one row per Driver the user can read. The rows are derived from
 * the assignment roster (GET /core/driver-assignments) by grouping on driver_id; the
 * assignments themselves are still needed for Status pay rates, which stay owned by a Branch
 * tenure.
 */

export interface PayRateDriver {
  driverId: number;
  fullName: string;
  driverNumber: string | null;
  driverStatus: string;
  /** Every tenure the user can read, newest first. */
  tenures: DriverAssignmentRosterRow[];
  current: DriverAssignmentRosterRow | null;
  pending: DriverAssignmentRosterRow | null;
  /** The tenure that represents the Driver: current, else pending, else the latest ended one. */
  home: DriverAssignmentRosterRow;
  /** The user only reads ended tenures of this Driver (a past Branch, or a Driver who left). */
  pastOnly: boolean;
}

export function groupRosterByDriver(rows: DriverAssignmentRosterRow[]): PayRateDriver[] {
  const byDriver = new Map<number, DriverAssignmentRosterRow[]>();
  for (const row of rows) {
    const list = byDriver.get(row.driver_id);
    if (list) list.push(row);
    else byDriver.set(row.driver_id, [row]);
  }
  const drivers: PayRateDriver[] = [];
  for (const [driverId, group] of byDriver) {
    const tenures = [...group].sort((a, b) => b.effective_from_at.localeCompare(a.effective_from_at));
    const current = tenures.find((t) => t.state === 'current') ?? null;
    const pending = [...tenures].reverse().find((t) => t.state === 'pending') ?? null;
    const home = current ?? pending ?? tenures[0];
    drivers.push({
      driverId,
      fullName: home.full_name,
      driverNumber: home.driver_number,
      driverStatus: home.driver_status,
      tenures,
      current,
      pending,
      home,
      pastOnly: current == null && pending == null,
    });
  }
  return drivers.sort((a, b) => a.fullName.localeCompare(b.fullName) || a.driverId - b.driverId);
}

export type DriverTenureFilter = 'open' | 'ended' | 'all';

export const DRIVER_TENURE_FILTER_LABELS: Record<DriverTenureFilter, string> = {
  open: 'Current & pending',
  ended: 'With a past Branch',
  all: 'All drivers',
};

export const DEFAULT_DRIVER_TENURE_FILTER: DriverTenureFilter = 'open';

export interface DriverFilters {
  search: string;
  branchId: string;
  driverStatus: string;
  tenure: DriverTenureFilter;
}

/** The Branch filter matches any readable tenure; the state filter reads the tenure states. */
export function filterDrivers(drivers: PayRateDriver[], filters: DriverFilters): PayRateDriver[] {
  const q = filters.search.trim().toLowerCase();
  return drivers.filter((d) => {
    if (filters.tenure === 'open' && d.pastOnly) return false;
    if (filters.tenure === 'ended' && !d.tenures.some((t) => t.state === 'ended')) return false;
    if (filters.branchId && !d.tenures.some((t) => String(t.branch_id) === filters.branchId)) return false;
    if (filters.driverStatus && d.driverStatus !== filters.driverStatus) return false;
    if (!q) return true;
    return d.fullName.toLowerCase().includes(q) || (d.driverNumber ?? '').toLowerCase().includes(q);
  });
}

/** "North"; "South (was North)" after a transfer; "North (past Branch)" when only history is readable. */
export function driverBranchSummary(d: PayRateDriver): string {
  if (d.pastOnly) return `${d.home.branch_name} (past Branch)`;
  const shown = d.current ?? d.pending!;
  const earlier = d.tenures.find((t) => t.state === 'ended' && t.branch_id !== shown.branch_id);
  const prefix = shown.state === 'pending' ? 'Upcoming: ' : '';
  return earlier ? `${prefix}${shown.branch_name} (was ${earlier.branch_name})` : `${prefix}${shown.branch_name}`;
}

export const PAST_BRANCH_BANNER = "History from your Branch's tenure — read-only";

/**
 * Whether the Driver-owned panes (ordinary rates, Min/Max) may offer edit controls. Authority
 * follows the Driver's current (else upcoming) Branch; a user who only reads ended tenures
 * never edits. The backend remains authoritative.
 */
export function canEditDriverCompensation(
  d: PayRateDriver | null,
  canEditAtBranch: (branchId: number) => boolean,
): boolean {
  if (d == null) return false;
  const authority = d.current ?? d.pending;
  return authority != null && canEditAtBranch(authority.branch_id);
}

export type PayRateTab = 'current' | 'pending' | 'history' | 'pay-rules';

/** Pending rows are never visible from a past Branch, so that tab is not offered there. */
export function visibleTabs(d: PayRateDriver | null): PayRateTab[] {
  const tabs: PayRateTab[] = ['current', 'pending', 'history', 'pay-rules'];
  return d?.pastOnly ? tabs.filter((tab) => tab !== 'pending') : tabs;
}

/** The tenure whose Status pay rates are shown first: current, else upcoming, else the latest ended. */
export function defaultStatusAssignment(d: PayRateDriver | null): DriverAssignmentRosterRow | null {
  return d?.home ?? null;
}

/** Status rates are owned by one assignment: only a current or upcoming one is writable. */
export function canEditStatusRates(
  assignment: DriverAssignmentRosterRow | null,
  canEditAtBranch: (branchId: number) => boolean,
): boolean {
  return assignment != null && assignment.state !== 'ended' && canEditAtBranch(assignment.branch_id);
}

export const NOT_APPLICABLE_FLAG = 'Not used in this Branch on that date';

/** A definition the Driver's context Branch does not use is flagged; null (no tenure) and true are not. */
export function applicabilityFlag(row: Pick<DriverPayRateRow, 'applicable_in_context_branch'>): string | null {
  return row.applicable_in_context_branch === false ? NOT_APPLICABLE_FLAG : null;
}
