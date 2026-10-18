import type { DriverAssignmentInfo } from '../../../types/core.ts';

/**
 * Pay Rates addresses one DriverAssignment at a time. A Driver can have several (one per
 * home-Branch tenure): the current or pending one is editable, ended ones are read-only
 * history that stays reachable after a transfer.
 */

const dayFormat: Intl.DateTimeFormatOptions = {
  year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC',
};

function day(iso: string): string {
  return new Date(iso).toLocaleDateString('en-US', dayFormat);
}

/** "North · Jan 2, 2026 – Mar 3, 2026 (Transferred)"; open windows end at "present". */
export function assignmentLabel(a: DriverAssignmentInfo): string {
  const end = a.effective_to_at ? day(a.effective_to_at) : 'present';
  const reason = a.end_reason ? ` (${a.end_reason})` : '';
  const prefix = a.state === 'pending' ? 'Upcoming · ' : '';
  return `${prefix}${a.branch_name} · ${day(a.effective_from_at)} – ${end}${reason}`;
}

/** Ended assignments are shown for reference only; rates there are never edited. */
export function isReadOnlyAssignment(a: DriverAssignmentInfo | null | undefined): boolean {
  return a != null && a.state === 'ended';
}

/** The assignment to open first: the Driver's visible one when the user may read it. */
export function defaultAssignmentId(
  assignments: DriverAssignmentInfo[],
  visibleAssignmentId: number | null,
): number | null {
  if (visibleAssignmentId != null
      && assignments.some((a) => a.driver_assignment_id === visibleAssignmentId)) {
    return visibleAssignmentId;
  }
  const current = assignments.find((a) => a.state === 'current')
    ?? assignments.find((a) => a.state === 'pending');
  return (current ?? assignments[0])?.driver_assignment_id ?? visibleAssignmentId;
}

/** The assignment being viewed: an explicit pick that still exists, else the default. */
export function viewedAssignmentId(
  assignments: DriverAssignmentInfo[],
  picked: number | null,
  visibleAssignmentId: number | null,
): number | null {
  if (picked != null && assignments.some((a) => a.driver_assignment_id === picked)) return picked;
  return defaultAssignmentId(assignments, visibleAssignmentId);
}
