import type {
  BranchPayDefinitionState,
  PayDefinitionInputType,
  PayDefinitionRequestStatus,
} from '../../../types/compensation';

export const INPUT_TYPE_LABELS: Record<PayDefinitionInputType, string> = {
  Decimal: 'Decimal quantity',
  WholeNumber: 'Whole number',
};

export const METHOD_LABELS: Record<string, string> = {
  PerUnit: 'Per unit',
  OrdinalTier: 'Ordinal tier (not yet available)',
};

export type RequestFilter = PayDefinitionRequestStatus | 'all';
export type Coverage = 'all-active' | 'all-inactive' | 'mixed';

export interface AggregateDefinition {
  pay_definition_id: number;
  definition_code: string;
  definition_name: string;
  input_type: PayDefinitionInputType;
  unit: string | null;
  calculation_method: string;
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
