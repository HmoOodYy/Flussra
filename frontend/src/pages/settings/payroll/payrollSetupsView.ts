/**
 * Pure view/formatting/validation helpers for the company-owned Payroll
 * Policies page (Phase 6 Unit A; internal API/type names still say
 * "Setup" — see backend/app/payroll_setup/schemas.py — this module never
 * renames them, only the user-facing copy built from them).
 *
 * PURE MODULE: no React, no apiClient, `import type` only for API types.
 * Never constructs a JS Date object or reads the wall clock — ISO date
 * strings from the backend are rendered/compared as strings only.
 */
import { formatIsoLong, isValidIsoDate } from '../../../lib/isoDate.ts';
import type { BranchPolicySummaryResponse, VersionResponse } from '../../../types/payrollSetup';

// ── Legacy Status Keys redirect ─────────────────────────────────────────────

/**
 * The legacy /settings/payroll?tab=status-keys[&branchId=] URL must keep
 * working after the legacy page is deleted. Any other tab value (or no tab
 * at all — e.g. the old branch-schedule tab, or ?setupId=4) is left alone
 * (returns null) so normal /settings/payroll traffic reaches the new
 * Payroll Setups page.
 */
export function legacyPayrollSettingsRedirect(search: string): string | null {
  const qs = search.startsWith('?') ? search.slice(1) : search;
  const params = new URLSearchParams(qs);
  if (params.get('tab') !== 'status-keys') return null;

  const branchId = params.get('branchId');
  if (branchId != null && branchId !== '') {
    return `/settings/status-keys?branchId=${encodeURIComponent(branchId)}`;
  }
  return '/settings/status-keys';
}

// ── Setup metadata validation (mirrors backend constraints only) ───────────

export const SETUP_CODE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

/**
 * `setup_code` (surfaced to the user as the optional "Integration code") is
 * now optional — mirrors POST /payroll-setup/setups, where the server
 * generates a stable code from the name when it is omitted. When the user
 * does supply one, it is validated with the exact same pattern the backend
 * enforces, so a rejection never reaches the server.
 */
export function validateSetupCreate({
  setup_code,
  setup_name,
}: {
  setup_code: string;
  setup_name: string;
}): string | null {
  if (setup_code.length > 0) {
    if (setup_code.length > 50) return 'Setup code must be 50 characters or fewer.';
    if (!SETUP_CODE_PATTERN.test(setup_code)) {
      return "Integration code must start with a letter or digit and contain only letters, digits, '_', '.', or '-'.";
    }
  }
  if (setup_name.length === 0) return 'Policy name is required.';
  if (setup_name.length > 200) return 'Policy name must be 200 characters or fewer.';
  return null;
}

export function validateSetupUpdate({
  setup_name,
}: {
  setup_name: string;
}): string | null {
  if (setup_name.length === 0) return 'Policy name is required.';
  if (setup_name.length > 200) return 'Policy name must be 200 characters or fewer.';
  return null;
}

/** Empty/whitespace-only description becomes null; otherwise sent as-is (no trimming). */
export function normalizeDescription(text: string): string | null {
  return text.trim() === '' ? null : text;
}

// ── Archive eligibility (client-side fact only; server remains authoritative) ─

/**
 * The ONLY client-side archive check. Everything else about archive
 * eligibility (SETUP_ASSIGNED, etc.) is decided by the server and rendered
 * from its PolicyError.
 */
export function archiveBlockedReason(
  setupId: number,
  defaultSetupId: number | null,
): string | null {
  return setupId === defaultSetupId
    ? 'This policy is the company default. Clear or change the company default before archiving.'
    : null;
}

// ── Version display helpers ─────────────────────────────────────────────────

export function shortHash(hash: string): string {
  return hash.slice(0, 12);
}

function versionNumberOrFallback(
  versionId: number,
  versions: readonly VersionResponse[],
): number | string {
  const found = versions.find((v) => v.version_id === versionId);
  return found ? found.version_number : `#${versionId}`;
}

/**
 * Describes how a published Version relates to its neighbors, using only
 * the ids the backend already gives it — no date comparison, no "current"
 * or "future" inference.
 */
export function versionRelationship(
  version: VersionResponse,
  versions: readonly VersionResponse[],
): string | null {
  if (version.replaced_by_version_id != null) {
    return `Replaced by Version ${versionNumberOrFallback(version.replaced_by_version_id, versions)}`;
  }
  if (version.replaces_version_id != null) {
    return `Replaces Version ${versionNumberOrFallback(version.replaces_version_id, versions)}`;
  }
  return null;
}

// ── Draft row label (no raw draft/version ids in primary UI) ───────────────

/**
 * "Draft created <formatted date>" using only the ISO date part of a
 * `created_at_utc` timestamp (e.g. "2026-01-04T00:00:00Z" -> "2026-01-04")
 * — plain string slicing, never a JS `Date`.
 */
export function draftCreatedLabel(createdAtUtc: string): string {
  const datePart = createdAtUtc.slice(0, 10);
  return isValidIsoDate(datePart) ? `Draft created ${formatIsoLong(datePart)}` : 'Draft created';
}

// ── Assigned Branches (derived from branch-summaries — no per-branch history) ─
//
// Phase 6 Unit A replaces the old per-branch assignment-history composition
// (one GET per branch) with a single GET /payroll-setup/branch-summaries
// call shared by the policy detail's "Assigned branches" section and the
// Branch Assignments tab (see branchPolicyView.ts). Pure grouping/sorting
// only — no date-boundary logic, no wall-clock use.

export interface PolicyBranchRow {
  branch_id: number;
  branch_name: string;
  branch_code: string;
  relation: 'current' | 'scheduled';
  since_date: string;
}

export function assignedPolicyBranchRows(
  summaries: readonly BranchPolicySummaryResponse[],
  setupId: number,
): PolicyBranchRow[] {
  const rows: PolicyBranchRow[] = [];

  for (const s of summaries) {
    if (s.current != null && s.current.setup_id === setupId) {
      rows.push({
        branch_id: s.branch_id,
        branch_name: s.branch_name,
        branch_code: s.branch_code,
        relation: 'current',
        since_date: s.current.effective_from_date,
      });
    } else if (s.scheduled_change != null && s.scheduled_change.setup_id === setupId) {
      rows.push({
        branch_id: s.branch_id,
        branch_name: s.branch_name,
        branch_code: s.branch_code,
        relation: 'scheduled',
        since_date: s.scheduled_change.effective_from_date,
      });
    }
  }

  rows.sort((a, b) => a.branch_name.localeCompare(b.branch_name));
  return rows;
}

// ── "Next steps" onboarding guide (policy detail, empty/incomplete states) ──
//
// Purely derived from already-loaded server data (drafts, versions, the
// company default, and branch summaries) — never computes readiness or
// eligibility itself. Hidden entirely once its list is empty ("complete").

export type PolicyNextStepKind = 'add-draft' | 'publish' | 'set-default' | 'assign-branches';

export function policyNextSteps(input: {
  hasDrafts: boolean;
  hasPublishedVersions: boolean;
  /** Whether ANY policy is currently the company default — not specifically this one. */
  hasCompanyDefault: boolean;
  /** Whether any branch (per branch-summaries) currently follows or is scheduled to follow this policy. */
  hasAnyBranchFollowing: boolean;
  canAssign: boolean;
}): PolicyNextStepKind[] {
  const steps: PolicyNextStepKind[] = [];
  if (!input.hasDrafts && !input.hasPublishedVersions) steps.push('add-draft');
  if (input.hasDrafts && !input.hasPublishedVersions) steps.push('publish');
  if (input.hasPublishedVersions && !input.hasCompanyDefault && input.canAssign) steps.push('set-default');
  if (input.hasPublishedVersions && !input.hasAnyBranchFollowing) steps.push('assign-branches');
  return steps;
}
