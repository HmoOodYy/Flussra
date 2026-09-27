/**
 * Pure error-shape reader for /payroll-setup responses.
 *
 * PURE MODULE: no imports of React, apiClient, or axios, and no browser
 * globals. Callers pass whatever they caught (typically an AxiosError, but
 * this module never assumes that — it only duck-types the shape it needs).
 *
 * Never invents error codes or wording. Technical axios text
 * (`error.message`) is deliberately never read — only the backend-authored
 * `response.data.detail` is a trustworthy source of user-facing text; when it
 * is absent or unusable, the caller-supplied `fallback` is used instead.
 */
import { formatIsoLong } from './isoDate.ts';

export interface ApiErrorInfo {
  status: number | null;
  code: string | null;
  message: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function extractStatus(error: unknown): number | null {
  if (!isRecord(error) || !isRecord(error.response)) return null;
  const status = error.response.status;
  return typeof status === 'number' ? status : null;
}

function extractDetail(error: unknown): unknown {
  if (!isRecord(error) || !isRecord(error.response) || !isRecord(error.response.data)) {
    return undefined;
  }
  return error.response.data.detail;
}

/** Prefix a 422 validation message with its field name, e.g. "setup_code: <msg>". */
function fieldValidationMessage(entry: unknown): string | null {
  if (!isRecord(entry)) return null;
  const msg = entry.msg;
  if (typeof msg !== 'string' || msg.trim().length === 0) return null;

  const loc = entry.loc;
  if (Array.isArray(loc) && loc.length > 0) {
    const last = loc[loc.length - 1];
    if ((typeof last === 'string' || typeof last === 'number') && last !== 'body') {
      return `${last}: ${msg}`;
    }
  }
  return msg;
}

// ── Friendly copy for stable backend error codes ────────────────────────────
//
// Never invents wording for a code not in this table — friendlyPolicyMessage
// falls back to the caller's own text for anything unlisted.

export const FRIENDLY_POLICY_MESSAGES: Record<string, string> = {
  VERSION_NOT_FOUND: 'This policy does not have a published schedule for that payroll start date.',
  SUCCESSOR_BOUNDARY_INVALID: 'Choose a date when the new payroll schedule begins a new period.',
  PREDECESSOR_BOUNDARY_INVALID: 'The current payroll period has not ended on that date.',
  PERIOD_HISTORY_CONFLICT:
    'Payroll periods already exist for these dates, so this change would rewrite payroll history.',
  ONBOARDING_START_TOO_EARLY:
    'Payroll can start in the current payroll period or up to two periods earlier, not before.',
  ASSIGNMENT_OVERLAP: 'This branch already follows a payroll policy on that date.',
  ASSIGNMENT_GAP: 'This date would leave the branch without a payroll policy for a while.',
  ASSIGNMENT_NOT_FOUND: 'This branch has no payroll policy on that date to change from.',
  UNCHANGED_ASSIGNMENT: 'The branch already follows this policy on that date.',
  REPLACEMENT_REQUIRED: 'Another change already starts on this date. Confirm that you want to replace it.',
  REPLACEMENT_NOT_TERMINAL:
    'Another change already starts on this date. Confirm that you want to replace it.',
  VERSION_COVERAGE_GAP: 'A branch following this policy would have no payroll schedule before this date.',
  INVALID_NORMAL_DAYS_OFF: 'Choose at most two normal days off.',
  INVALID_SCHEDULE: 'The payroll schedule is incomplete or not valid.',
  INVALID_EFFECTIVE_DATE: "The change can't start before the schedule's first payroll period.",
  SETUP_NOT_ACTIVE: 'This payroll policy is archived.',
  SETUP_NOT_FOUND: 'This payroll policy no longer exists.',
  SETUP_CODE_CONFLICT: 'That code is already used by another payroll policy.',
  BRANCH_NOT_OPERATIONAL: 'This branch is not active.',
  BRANCH_NOT_FOUND: 'This branch no longer exists.',
  DEFAULT_SETUP_NOT_PUBLISHED: 'Publish a payroll schedule before setting this policy as the default.',
  DEFAULT_SETUP_IN_USE: 'This policy is the company default. Choose another default before archiving it.',
  SETUP_ASSIGNED: "Branches still follow this policy, so it can't be archived yet.",
  DRAFT_NOT_EDITABLE: 'This draft can no longer be changed.',
  DRAFT_NOT_FOUND: 'This draft no longer exists.',
  CONCURRENT_ASSIGNMENT_CHANGE: "Someone else changed this branch's payroll policy. Refresh and try again.",
  AUTHORITY_BOUNDARY_CROSSING: 'A payroll period would overlap a scheduled policy change.',
  INVALID_SCHEDULE_BOUNDARY: 'That date is not the start of a payroll period.',
  INVALID_SETUP_CODE: 'Codes starting with PPOL- are reserved for system-generated references.',
};

/** Own-property lookup only — never resolves via the prototype chain ('__proto__', 'constructor', ...). */
export function friendlyPolicyMessage(code: string | null, fallback: string): string {
  if (code != null && Object.prototype.hasOwnProperty.call(FRIENDLY_POLICY_MESSAGES, code)) {
    return FRIENDLY_POLICY_MESSAGES[code];
  }
  return fallback;
}

/**
 * Formats a server-authored chronology conflict without exposing its stable
 * code. The date-specific wording is used only when the backend supplies the
 * corresponding date, so the frontend never invents a conflicting boundary.
 */
export function friendlyBoundaryConflictMessage(
  code: string | null,
  reason: string,
  fallback: string,
): string {
  if (code === 'SUCCESSOR_BOUNDARY_INVALID') {
    const match = /^Existing scheduled update on (\d{4}-\d{2}-\d{2}) would not start on a valid boundary under this schedule$/.exec(reason);
    if (match) {
      return `An existing scheduled update on ${formatIsoLong(match[1])} would no longer start on a valid payroll-period boundary under this schedule.`;
    }
  }

  if (code === 'PREDECESSOR_BOUNDARY_INVALID') {
    const match = /^Effective date (\d{4}-\d{2}-\d{2}) splits the predecessor payroll period$/.exec(reason);
    if (match) {
      return `The proposed change on ${formatIsoLong(match[1])} would split the previous payroll period.`;
    }
  }

  return friendlyPolicyMessage(code, fallback);
}

/**
 * message = friendly copy when `info.code` is a known stable code, else
 * `info.message` unchanged. detail = the server-authored message, but only
 * when it differs from what is already shown as `message` (avoids a
 * "Technical details" panel that just repeats itself).
 */
export function friendlyError(info: ApiErrorInfo): {
  message: string;
  code: string | null;
  detail: string | null;
} {
  const message = friendlyBoundaryConflictMessage(info.code, info.message, info.message);
  const detail = info.message !== message ? info.message : null;
  return { message, code: info.code, detail };
}

export function readApiError(error: unknown, fallback: string): ApiErrorInfo {
  const status = extractStatus(error);
  const detail = extractDetail(error);

  // PolicyError envelope (and 410 legacy routes): { code: string, message: string }.
  if (isRecord(detail) && typeof detail.code === 'string') {
    const rawMessage = typeof detail.message === 'string' ? detail.message : '';
    const message = rawMessage.trim().length > 0 ? rawMessage : fallback;
    return { status, code: detail.code, message };
  }

  if (typeof detail === 'string' && detail.trim().length > 0) {
    return { status, code: null, message: detail };
  }

  // FastAPI 422 validation error array.
  if (Array.isArray(detail)) {
    const parts = detail
      .map(fieldValidationMessage)
      .filter((part): part is string => part !== null);
    return parts.length > 0
      ? { status, code: null, message: parts.join('; ') }
      : { status, code: null, message: fallback };
  }

  return { status, code: null, message: fallback };
}
