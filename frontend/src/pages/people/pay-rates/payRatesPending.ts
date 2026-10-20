import type {
  DriverPayRateRow,
  DriverRateSummaryItem,
  ScheduleBrief,
  ScheduleSummary,
} from '../../../types/compensation.ts';
import { historyDefinitionName, isRetiredDefinition } from './payRatesHistory.ts';

/**
 * Pending ordinary schedules are discovered from the Driver's schedule summary and history
 * (read-scope filtered, so a past Branch never gets one and a full reader gets every one),
 * never from the Active-only authoring matrix: a Pending schedule of a retired PayDefinition
 * would otherwise be invisible and impossible to discard. The matrix only enriches rows of
 * Active definitions (current amount); it stays the one source of authoring choices.
 */

export interface PendingScheduleRow {
  scheduleId: number;
  rateDefinitionId: number;
  definitionName: string;
  definitionCode: string | null;
  unit: string | null;
  /** The PayDefinition is no longer Active: discard only, never approved or edited. */
  retired: boolean;
  effectiveFrom: string;
  effectiveTo: string | null;
  /** The Pending schedule's scalar amount; null while it has no value yet. */
  amount: string | null;
  /** True for a definition the authoring matrix lists (Active); false for a retired one. */
  inMatrix: boolean;
  /** The current rate from the matrix, when the definition is in it. */
  current: ScheduleBrief | null;
}

export function buildPendingScheduleRows(
  summary: DriverRateSummaryItem[],
  historyByDefinition: Record<number, ScheduleSummary[]>,
  matrixRows: DriverPayRateRow[],
): PendingScheduleRow[] {
  const matrix = new Map(matrixRows.map((row) => [row.rate_definition_id, row]));
  const rows: PendingScheduleRow[] = [];
  for (const item of summary) {
    if (item.pending_schedule_id == null) continue;
    const inMatrix = matrix.get(item.rate_definition_id);
    for (const schedule of historyByDefinition[item.rate_definition_id] ?? []) {
      if (schedule.status !== 'Pending') continue;
      rows.push({
        scheduleId: schedule.driver_rate_schedule_id,
        rateDefinitionId: item.rate_definition_id,
        definitionName: inMatrix?.definition_name ?? historyDefinitionName(item),
        definitionCode: inMatrix?.definition_code ?? item.definition_code,
        unit: inMatrix?.unit ?? item.unit,
        retired: isRetiredDefinition(item),
        effectiveFrom: schedule.effective_from,
        effectiveTo: schedule.effective_to,
        amount: schedule.values[0]?.amount ?? null,
        inMatrix: inMatrix !== undefined,
        current: inMatrix?.current ?? null,
      });
    }
  }
  return rows.sort((a, b) =>
    a.definitionName.localeCompare(b.definitionName) || a.effectiveFrom.localeCompare(b.effectiveFrom));
}

/**
 * Approving needs the existing edit gate (backend full history, a live tenure, edit
 * permission) and an Active definition: the backend refuses approval on a retired one, so
 * it is not offered. There is no editing of a retired definition's schedule either.
 */
export function canApprovePendingRow(row: PendingScheduleRow, canEditDriver: boolean): boolean {
  return canEditDriver && !row.retired;
}

/** Discarding follows the backend's discard authority alone, retired or orphaned or not. */
export function canDiscardPendingRow(canDiscardDriver: boolean): boolean {
  return canDiscardDriver;
}

/** Authoring choices come from the matrix only, so a retired definition is never among them. */
export function authoringRateDefinitionIds(matrixRows: DriverPayRateRow[]): number[] {
  return matrixRows.map((row) => row.rate_definition_id);
}
