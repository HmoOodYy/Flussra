import type {
  StatusKeyBranchLimits,
  StatusKeyBranchLimitsUpdate,
} from '../../../types/settings';

/**
 * Branch usage limits: each Status has up to four optional hard limits per Branch. The
 * editor holds each as an enabled flag and a text count; a Status with every limit
 * disabled has no limits in that Branch.
 */

export const LIMIT_SCOPES = ['per_period', 'per_driver', 'across_drivers', 'per_day'] as const;
export type LimitScope = (typeof LIMIT_SCOPES)[number];

export const LIMIT_LABELS: Record<LimitScope, string> = {
  per_period: 'Per period',
  per_driver: 'Per driver',
  across_drivers: 'Across all drivers',
  per_day: 'Per day',
};

export interface LimitsForm {
  per_period: { enabled: boolean; value: string };
  per_driver: { enabled: boolean; value: string };
  across_drivers: { enabled: boolean; value: string };
  per_day: { enabled: boolean; value: string };
}

function field(enabled: boolean, value: number | null): { enabled: boolean; value: string } {
  return { enabled, value: enabled && value != null ? String(value) : '' };
}

export function limitsToForm(limits: StatusKeyBranchLimits): LimitsForm {
  return {
    per_period: field(limits.limit_uses_per_period_enabled, limits.limit_uses_per_period),
    per_driver: field(limits.limit_uses_per_driver_enabled, limits.limit_uses_per_driver),
    across_drivers: field(limits.limit_uses_across_drivers_enabled, limits.limit_uses_across_drivers),
    per_day: field(limits.limit_uses_per_day_enabled, limits.limit_uses_per_day),
  };
}

function positiveInteger(value: string): number | null {
  const text = value.trim();
  if (!/^\d+$/.test(text)) return null;
  const n = Number(text);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/** An enabled limit needs a positive whole count; the first problem is returned. */
export function validateLimits(form: LimitsForm): string {
  for (const scope of LIMIT_SCOPES) {
    const { enabled, value } = form[scope];
    if (enabled && positiveInteger(value) === null) {
      return `${LIMIT_LABELS[scope]}: enter a whole number greater than 0.`;
    }
  }
  return '';
}

/** The whole replacement the server stores: a disabled limit carries no count. */
export function limitsToPayload(form: LimitsForm): StatusKeyBranchLimitsUpdate {
  const count = (scope: LimitScope) => (form[scope].enabled ? positiveInteger(form[scope].value) : null);
  return {
    limit_uses_per_period_enabled: form.per_period.enabled,
    limit_uses_per_period: count('per_period'),
    limit_uses_per_driver_enabled: form.per_driver.enabled,
    limit_uses_per_driver: count('per_driver'),
    limit_uses_across_drivers_enabled: form.across_drivers.enabled,
    limit_uses_across_drivers: count('across_drivers'),
    limit_uses_per_day_enabled: form.per_day.enabled,
    limit_uses_per_day: count('per_day'),
  };
}

/** Whether the edited form differs from the saved limits. */
export function limitsChanged(form: LimitsForm, saved: StatusKeyBranchLimits): boolean {
  const before = limitsToForm(saved);
  return LIMIT_SCOPES.some((scope) =>
    form[scope].enabled !== before[scope].enabled
    || (form[scope].enabled && form[scope].value.trim() !== before[scope].value));
}

/** A one-line summary of the enabled limits, or null when the Status has none in the Branch. */
export function limitsSummary(limits: StatusKeyBranchLimits): string | null {
  const form = limitsToForm(limits);
  const parts = LIMIT_SCOPES
    .filter((scope) => form[scope].enabled)
    .map((scope) => `${LIMIT_LABELS[scope]} ${form[scope].value}`);
  return parts.length === 0 ? null : parts.join(' · ');
}
