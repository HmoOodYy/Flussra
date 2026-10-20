import type { DriverRateSummaryItem, ScheduleSummary } from '../../../types/compensation.ts';

/**
 * Rate history is discovered from the Driver's actual schedules (the backend's rate-schedule
 * summary, already limited to what the caller may read), never from the authoring matrix:
 * that matrix lists Active PayDefinitions only, so a retired definition's schedules would
 * vanish. A retired definition is labelled and its rows are read-only.
 */

export interface HistoryRow extends ScheduleSummary {
  definition_name: string;
  unit: string | null;
  /** The PayDefinition is no longer Active: shown for reference, never authorable. */
  retired: boolean;
}

export const RETIRED_LABEL = 'Retired';

export function isRetiredDefinition(item: Pick<DriverRateSummaryItem, 'pay_definition_status'>): boolean {
  return item.pay_definition_status != null && item.pay_definition_status !== 'Active';
}

/** The name to show: the definition's name, else its code, else the rate definition id. */
export function historyDefinitionName(item: DriverRateSummaryItem): string {
  return item.definition_name ?? item.definition_code ?? `Rate ${item.rate_definition_id}`;
}

/** One list of schedules per summary item (same order), newest start first. */
export function buildHistoryRows(
  items: DriverRateSummaryItem[],
  schedulesByItem: ScheduleSummary[][],
): HistoryRow[] {
  return items
    .flatMap((item, index) => schedulesByItem[index].map((schedule) => ({
      ...schedule,
      definition_name: historyDefinitionName(item),
      unit: item.unit,
      retired: isRetiredDefinition(item),
    })))
    .sort((a, b) => b.effective_from.localeCompare(a.effective_from));
}

/** A row can be voided only by someone who may edit, and never for a retired definition. */
export function canVoidHistoryRow(row: HistoryRow, canEdit: boolean): boolean {
  return canEdit && !row.retired && (row.status === 'Approved' || row.status === 'Superseded');
}
