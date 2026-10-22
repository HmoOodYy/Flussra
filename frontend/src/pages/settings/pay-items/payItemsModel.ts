import type {
  BranchPayDefinitionState,
  OrdinalTierInput,
  PayDefinitionDirectCreatePayload,
  PayDefinitionRequestCreatePayload,
  PayDefinitionCalculationMethod,
  PayDefinitionInputType,
  PayDefinitionRequestStatus,
  RateComponentSummary,
} from '../../../types/compensation';

export const INPUT_TYPE_LABELS: Record<PayDefinitionInputType, string> = {
  Decimal: 'Decimal quantity',
  WholeNumber: 'Whole number',
};

export const METHOD_LABELS: Record<string, string> = {
  PerUnit: 'Per unit',
  OrdinalTier: 'Tiered by quantity',
};

/** Validate the backend OrdinalTier topology contract before sending a create/request. */
export function ordinalTopologyError(tiers: OrdinalTierInput[]): string | null {
  if (tiers.length === 0) return 'Add at least one tier.';
  let expectedFrom = 1;
  let openEndedCount = 0;
  for (let index = 0; index < tiers.length; index += 1) {
    const tier = tiers[index];
    if (!Number.isInteger(tier.ordinal_from) || tier.ordinal_from < 1
      || tier.ordinal_from !== expectedFrom) return 'Tiers must use positive whole numbers in a continuous sequence starting at 1.';
    if (tier.ordinal_to === null) {
      openEndedCount += 1;
      if (index !== tiers.length - 1) return 'Only the final tier can be open-ended.';
    } else {
      if (!Number.isInteger(tier.ordinal_to) || tier.ordinal_to < tier.ordinal_from) {
        return 'Each tier end must be a whole number at or above its start.';
      }
      expectedFrom = tier.ordinal_to + 1;
    }
  }
  return openEndedCount === 1 && tiers[tiers.length - 1].ordinal_to === null
    ? null : 'The final tier must be open-ended.';
}

export function validatePayMethod(
  method: PayDefinitionCalculationMethod,
  inputType: PayDefinitionInputType,
  tiers: OrdinalTierInput[],
): string | null {
  if (method === 'OrdinalTier') {
    if (inputType !== 'WholeNumber') return 'Tiered pay requires a whole-number quantity.';
    return ordinalTopologyError(tiers);
  }
  return tiers.length > 0 ? 'Per-unit pay cannot include tiers.' : null;
}

export function ordinalTierLabel(tier: OrdinalTierInput): string {
  if (tier.ordinal_to === null) return `${tier.ordinal_from}+`;
  return tier.ordinal_from === tier.ordinal_to
    ? `${tier.ordinal_from}` : `${tier.ordinal_from}–${tier.ordinal_to}`;
}

export function buildPayDefinitionPayload(input: {
  definition_name: string;
  input_type: PayDefinitionInputType;
  unit: string | null;
  calculation_method: PayDefinitionCalculationMethod;
  ordinal_tiers: OrdinalTierInput[];
  definition_code?: string | null;
}): PayDefinitionDirectCreatePayload {
  return {
    ...(input.definition_code !== undefined ? { definition_code: input.definition_code } : {}),
    definition_name: input.definition_name,
    input_type: input.input_type,
    unit: input.unit,
    calculation_method: input.calculation_method,
    ...(input.calculation_method === 'OrdinalTier' ? { ordinal_tiers: input.ordinal_tiers } : {}),
  };
}

export function buildPayDefinitionRequestPayload(
  requestingBranchId: number,
  definition: PayDefinitionDirectCreatePayload,
  notes: string | null,
): PayDefinitionRequestCreatePayload {
  return { requesting_branch_id: requestingBranchId, ...definition, notes };
}

export type RequestFilter = PayDefinitionRequestStatus | 'all';
export type Coverage = 'all-active' | 'all-inactive' | 'mixed';

export interface AggregateDefinition {
  pay_definition_id: number;
  definition_code: string;
  definition_name: string;
  input_type: PayDefinitionInputType;
  unit: string | null;
  calculation_method: string;
  components: RateComponentSummary[];
  structure_locked_at_utc: string | null;
  activeBranches: number;
  totalBranches: number;
  coverage: Coverage;
}

export function apiError(err: unknown): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    const { message, branch_errors: branchErrors } = detail as {
      message?: string; branch_errors?: Array<{ branch_name: string; error: string }>;
    };
    if (branchErrors?.length) {
      return `${message ?? 'Validation failed.'} ${branchErrors.map(e => `${e.branch_name}: ${e.error}`).join('; ')}`;
    }
    if (message) return message;
  }
  return err instanceof Error ? err.message : 'Request failed.';
}

export function fmtDate(value: string | null): string {
  if (!value) return '—';
  return new Date(value).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export function buildAggregates(
  byBranch: Record<number, BranchPayDefinitionState[]>,
): AggregateDefinition[] {
  const map = new Map<number, AggregateDefinition>();
  const branchCount = Object.keys(byBranch).length;
  for (const items of Object.values(byBranch)) {
    for (const item of items) {
      const existing = map.get(item.pay_definition_id) ?? {
        pay_definition_id: item.pay_definition_id,
        definition_code: item.definition_code,
        definition_name: item.definition_name,
        input_type: item.input_type,
        unit: item.unit,
        calculation_method: item.calculation_method,
        components: item.components,
        structure_locked_at_utc: item.structure_locked_at_utc,
        activeBranches: 0,
        totalBranches: branchCount,
        coverage: 'all-inactive' as Coverage,
      };
      if (item.is_active) existing.activeBranches += 1;
      map.set(item.pay_definition_id, existing);
    }
  }
  return [...map.values()].map(agg => ({
    ...agg,
    coverage: agg.activeBranches === 0 ? 'all-inactive'
      : agg.activeBranches === agg.totalBranches ? 'all-active' : 'mixed',
  }));
}
