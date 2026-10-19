import type { DriverAssignmentInfo, DriverAssignmentRosterRow } from '../../../types/core.ts';

/**
 * Pay Rates is assignment-centric in Stage 1: rates and pay rules belong to one
 * DriverAssignment (a Driver's home-Branch tenure). The roster lists every assignment the
 * user may read, so a manager of the old Branch still reaches a transferred Driver's ended
 * assignment. Current and pending assignments are editable; ended ones are read-only history.
 */

const dayFormat: Intl.DateTimeFormatOptions = {
  year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC',
};

function day(iso: string): string {
  return new Date(iso).toLocaleDateString('en-US', dayFormat);
}

/** "Jan 2, 2026 – Mar 3, 2026 (Transferred)"; open windows end at "present". */
export function assignmentWindow(a: DriverAssignmentInfo): string {
  const end = a.effective_to_at ? day(a.effective_to_at) : 'present';
  const reason = a.end_reason ? ` (${a.end_reason})` : '';
  return `${day(a.effective_from_at)} – ${end}${reason}`;
}

/** "North · Jan 2, 2026 – Mar 3, 2026 (Transferred)"; a pending one is marked "Upcoming". */
export function assignmentLabel(a: DriverAssignmentInfo): string {
  const prefix = a.state === 'pending' ? 'Upcoming · ' : '';
  return `${prefix}${a.branch_name} · ${assignmentWindow(a)}`;
}

/** Ended assignments are shown for reference only; rates there are never edited. */
export function isReadOnlyAssignment(a: DriverAssignmentInfo | null | undefined): boolean {
  return a != null && a.state === 'ended';
}

export function stateBadgeLabel(state: DriverAssignmentInfo['state']): string {
  switch (state) {
    case 'current': return 'Current';
    case 'pending': return 'Pending';
    case 'ended': return 'Ended';
  }
}

/** Identity of a roster row: the permanent Driver number with the Branch it belongs to. */
export function rosterIdentity(row: DriverAssignmentRosterRow): string {
  return `${row.driver_number ?? 'No number'} · ${row.branch_name}`;
}

export type AssignmentFilter = 'open' | 'ended' | 'all';

export const ASSIGNMENT_FILTER_LABELS: Record<AssignmentFilter, string> = {
  open: 'Current & pending',
  ended: 'Ended',
  all: 'All assignments',
};

export interface RosterFilters {
  search: string;
  branchId: string;
  driverStatus: string;
  assignment: AssignmentFilter;
}

/** Default view: what can be edited. Ended assignments are one filter choice away. */
export const DEFAULT_ASSIGNMENT_FILTER: AssignmentFilter = 'open';

export function filterRoster(
  rows: DriverAssignmentRosterRow[],
  filters: RosterFilters,
): DriverAssignmentRosterRow[] {
  const q = filters.search.trim().toLowerCase();
  return rows.filter((row) => {
    if (filters.assignment === 'open' && row.state === 'ended') return false;
    if (filters.assignment === 'ended' && row.state !== 'ended') return false;
    if (filters.branchId && String(row.branch_id) !== filters.branchId) return false;
    if (filters.driverStatus && row.driver_status !== filters.driverStatus) return false;
    if (!q) return true;
    return row.full_name.toLowerCase().includes(q)
      || (row.driver_number ?? '').toLowerCase().includes(q);
  });
}

/** Every readable assignment of one permanent Driver (the picker's choices). */
export function driverAssignmentsOf(
  rows: DriverAssignmentRosterRow[],
  driverId: number | null,
): DriverAssignmentRosterRow[] {
  if (driverId == null) return [];
  return rows.filter((row) => row.driver_id === driverId);
}
