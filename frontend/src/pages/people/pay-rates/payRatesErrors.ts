/**
 * Error messages for Driver-owned compensation writes. The backend stays authoritative; this
 * only turns its structured refusals into text a user can act on.
 */

export interface CompensationErrorDetail {
  code: string | null;
  message: string | null;
  payrollPeriodIds: number[];
}

export const COMPENSATION_ERROR_MESSAGES: Record<string, string> = {
  DRIVER_COMPENSATION_HISTORICAL:
    'These dates fall inside a tenure that has already ended, so they can no longer be changed.',
  PAY_RULE_CALENDAR_UNAVAILABLE:
    "No Payroll Setup applies to this Driver's Branch on that date, so the pay calendar cannot be "
    + 'recorded. Publish a Payroll Setup for the Branch first.',
  SCHEDULE_CHANGED: 'This rate was changed by someone else. Reload and try again.',
  SCHEDULE_WINDOW_OVERLAP:
    'These dates overlap another rate for this pay item. Choose a start date after it.',
  SCHEDULE_NOT_PENDING: 'Only a Pending rate can be edited, approved or discarded.',
  SCHEDULE_INCOMPLETE: 'The rate has no amount yet. Enter every amount before approving.',
  SCHEDULE_IMMUTABLE: 'An approved rate cannot be edited. Create a new rate or void this one.',
  SCHEDULE_VALUES_IMMUTABLE: 'The amounts of this rate can no longer be changed.',
  SCHEDULE_INVALID_TRANSITION: 'This rate is not in a state where that action is possible.',
  SCHEDULE_NOT_FOUND: 'This rate no longer exists. Reload the page.',
  RATE_SCHEDULE_SELF_APPROVAL_DISALLOWED:
    'Your company does not allow you to approve a pay rate change that you created or last edited. '
    + 'Ask another user with pay rate approval access to approve it.',
};

const PERMISSION_MESSAGE = "You do not have permission to change this Driver's pay rates.";

function finalizedMessage(ids: number[]): string {
  const periods = ids.length > 0
    ? ` (${ids.length} payroll period${ids.length === 1 ? '' : 's'})`
    : '';
  return `This change touches finalized payroll${periods} and cannot be made. `
    + 'Choose dates that do not reach a finalized period.';
}

/** The structured parts of an API error: detail may be a string, an object or a validation list. */
export function compensationErrorDetail(err: unknown): CompensationErrorDetail {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return { code: null, message: detail.trim() || null, payrollPeriodIds: [] };
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const body = detail as { code?: unknown; message?: unknown; payroll_period_ids?: unknown };
    return {
      code: typeof body.code === 'string' ? body.code : null,
      message: typeof body.message === 'string' ? body.message : null,
      payrollPeriodIds: Array.isArray(body.payroll_period_ids)
        ? body.payroll_period_ids.filter((id): id is number => typeof id === 'number') : [],
    };
  }
  if (Array.isArray(detail) && typeof (detail[0] as { msg?: unknown } | undefined)?.msg === 'string') {
    return { code: null, message: (detail[0] as { msg: string }).msg, payrollPeriodIds: [] };
  }
  return { code: null, message: null, payrollPeriodIds: [] };
}

export function compensationErrorMessage(err: unknown): string {
  const detail = compensationErrorDetail(err);
  if (detail.code === 'DRIVER_COMPENSATION_IN_FINALIZED_PERIOD') {
    return finalizedMessage(detail.payrollPeriodIds);
  }
  if (detail.code && COMPENSATION_ERROR_MESSAGES[detail.code]) {
    return COMPENSATION_ERROR_MESSAGES[detail.code];
  }
  if (detail.message) return detail.message;
  const status = (err as { response?: { status?: number } })?.response?.status;
  if (status === 403) return PERMISSION_MESSAGE;
  return 'An unexpected error occurred.';
}
