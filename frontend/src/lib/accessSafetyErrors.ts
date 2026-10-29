/**
 * Plain wording for the access-safety refusals the backend returns when someone changes access:
 * their own, more than they hold, a role they hold, or a terminated person's.
 *
 * PURE MODULE: the backend stays authoritative; this only turns its stable error codes into text
 * a person can act on. Anything it does not recognise is left to the caller's usual error text.
 */
import { friendlyPermLabel } from './permissionLabels.ts';

export const ACCESS_SAFETY_ERROR_MESSAGES: Record<string, string> = {
  SELF_ACCESS_CHANGE_FORBIDDEN:
    'You cannot change your own role or extra permissions. Someone else needs to change your access.',
  OWN_ROLE_PERMISSIONS_EDIT_FORBIDDEN:
    'You hold this role, so you cannot change its permissions. Someone else needs to change them.',
  EMPLOYEE_TERMINATED:
    'This person has been terminated. They cannot be given sign-in or access, and their account stays linked so it cannot be switched back on.',
  COMPANY_OWNER_TERMINATION_FORBIDDEN:
    'This person is the Company Owner. Transfer ownership to someone else before terminating them.',
  OWN_EMPLOYEE_TERMINATION_FORBIDDEN:
    'You cannot terminate your own record. Someone else needs to do it.',
};

export const OWN_ACCESS_NOTICE = 'Someone else needs to change your own access.';

function grantCeilingMessage(permissionCodes: string[]): string {
  const base = 'You can only give access that you hold yourself.';
  if (permissionCodes.length === 0) return base;
  return `${base} You do not hold: ${permissionCodes.map(friendlyPermLabel).join(', ')}.`;
}

/** The friendly text for an access-safety refusal, or null when the error is something else. */
export function accessSafetyMessage(err: unknown): string | null {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null;
  const { code, permission_codes: codes } = detail as { code?: unknown; permission_codes?: unknown };
  if (typeof code !== 'string') return null;
  if (code === 'GRANT_CEILING_EXCEEDED') {
    return grantCeilingMessage(
      Array.isArray(codes) ? codes.filter((c): c is string => typeof c === 'string') : [],
    );
  }
  return ACCESS_SAFETY_ERROR_MESSAGES[code] ?? null;
}
