import { isAxiosError } from 'axios';

/**
 * The Ledger lists finalized periods from one authority (the Finalized Payroll Library).
 * A 403 means that authority grants nothing for the requested branch scope; every other
 * failure (network, 401, 404, 5xx) is a real load failure and is reported as one.
 */
export function ledgerLoadErrorMessage(error: unknown): string {
  if (isAxiosError(error) && error.response?.status === 403) {
    return 'You do not have access to finalized payroll for this branch scope.';
  }
  return 'Failed to load ledger periods.';
}
