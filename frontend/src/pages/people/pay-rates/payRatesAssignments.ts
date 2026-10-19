import type { DriverAssignmentInfo } from '../../../types/core.ts';

/**
 * Status pay rates are the only compensation still owned by a DriverAssignment (a Driver's
 * home-Branch tenure), so the Status rates pane keeps an assignment picker. Current and
 * pending assignments are editable; ended ones are read-only history.
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
