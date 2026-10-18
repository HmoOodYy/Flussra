import type { DriverTransferRequest } from '../../../types/transfer.ts';
import { TRANSFER_TERMINAL } from '../../../types/transfer.ts';
import { addIsoDays, formatIsoLong } from '../../../lib/isoDate.ts';

/**
 * Which lifecycle actions a request offers, from the server-reported fields only.
 *
 * `status` is the server's effective status: a Scheduled request is reported as
 * `Completed` once its EffectiveAt has passed, so the client never promotes or demotes
 * a status itself. The only clock use is the guard that hides Cancel/Reschedule on a
 * Scheduled request whose instant has already passed in the viewer's open page (the
 * backend remains the authority and rejects a stale action).
 */
export function isFutureScheduled(
  req: Pick<DriverTransferRequest, 'status' | 'effective_at'>,
  nowMs: number = Date.now(),
): boolean {
  if (req.status !== 'Scheduled' || !req.effective_at) return false;
  const effective = Date.parse(req.effective_at);
  return Number.isFinite(effective) && effective > nowMs;
}

/** Cancel is offered for any open request; a Scheduled one only while its instant is ahead. */
export function canCancelRequest(
  req: Pick<DriverTransferRequest, 'status' | 'effective_at'>,
  nowMs: number = Date.now(),
): boolean {
  if (TRANSFER_TERMINAL.has(req.status)) return false;
  if (req.status === 'Scheduled') return isFutureScheduled(req, nowMs);
  return true;
}

/** Reschedule moves the EffectiveAt of a Scheduled request that has not taken effect yet. */
export function canRescheduleRequest(
  req: Pick<DriverTransferRequest, 'status' | 'effective_at'>,
  nowMs: number = Date.now(),
): boolean {
  return isFutureScheduled(req, nowMs);
}

/** Complete (schedule or apply) is only for an Approved request. */
export function canCompleteRequest(req: Pick<DriverTransferRequest, 'status'>): boolean {
  return req.status === 'Approved';
}

/** The identity line for a request: Driver name, with the Driver number when known. */
export function driverLabel(
  req: Pick<DriverTransferRequest, 'driver_name' | 'driver_number'>,
): string {
  if (!req.driver_name) return req.driver_number ?? 'Driver';
  return req.driver_number ? `${req.driver_name} (${req.driver_number})` : req.driver_name;
}

/** The Branch-local calendar date (YYYY-MM-DD) of an instant in an IANA time zone. */
export function localIsoDate(instantIso: string, timeZoneId: string): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: timeZoneId, year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date(instantIso));
}

/**
 * Warning shown with a Scheduled transfer: a termination whose cutoff (the end of its
 * termination date in the source Branch) is at or before EffectiveAt cancels it. That is
 * every termination date up to the day before EffectiveAt's Branch-local date. The
 * backend stays authoritative; this only tells the user what to expect.
 */
export function terminationWarning(
  req: Pick<DriverTransferRequest, 'status' | 'effective_at'>,
  sourceTimeZoneId: string | null | undefined,
  nowMs: number = Date.now(),
): string | null {
  if (!sourceTimeZoneId || !req.effective_at || !isFutureScheduled(req, nowMs)) return null;
  const latest = addIsoDays(localIsoDate(req.effective_at, sourceTimeZoneId), -1);
  return `Terminating this employee with a termination date on or before ${formatIsoLong(latest)} `
    + 'will cancel this scheduled transfer.';
}
