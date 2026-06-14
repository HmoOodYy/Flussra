/**
 * Generate a human-readable label from a permission code.
 *
 * Used when the backend Permission object's `permission_name` is unavailable
 * (e.g. a newly-added code that isn't yet in the fetched catalogue).
 *
 * Examples:
 *   users.edit       → Edit Members
 *   branches.create  → Create Branches
 *   payroll.approve  → Approve Payroll
 *   settings.manage  → Manage Settings
 */

const _MODULE_LABELS: Record<string, string> = {
  company:  'Company',
  branches: 'Branches',
  roles:    'Roles',
  users:    'Members',
  payroll:  'Payroll',
  payitems: 'Pay Items',
  payrates: 'Pay Rates',
  drivers:  'Drivers',
  dispatch: 'Dispatch',
  reports:  'Reports',
  settings: 'Settings',
};

const _ACTION_LABELS: Record<string, string> = {
  view:        'View',
  edit:        'Edit',
  create:      'Create',
  delete:      'Delete',
  approve:     'Approve',
  finalize:    'Finalize',
  manage:      'Manage',
  deactivate:  'Deactivate',
  entry:       'Enter',
  decide:      'Decide',
  approve_rate: 'Approve Rates',
};

export function friendlyPermLabel(code: string): string {
  const dot = code.indexOf('.');
  if (dot === -1) return code;
  const module = code.slice(0, dot);
  const action = code.slice(dot + 1);
  const moduleLabel = _MODULE_LABELS[module] ?? module;
  const actionLabel = _ACTION_LABELS[action] ?? action;
  return `${actionLabel} ${moduleLabel}`;
}
