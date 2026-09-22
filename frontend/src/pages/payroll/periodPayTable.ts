/**
 * Shared Period Pay presentation model (C1-3B) — used by both
 * CurrentPayrollReportsDialog and FinalizedPayrollLibraryDialog so the two
 * surfaces never build two independent interpretations of the same backend
 * financial contract.
 *
 * This module is presentation-only. It aligns backend-supplied values by
 * PayItemID and exposes backend fixed/footer totals verbatim. It never
 * computes, sums, or derives a monetary value — every money field here is
 * either a backend string passed through unchanged or `null` when the
 * backend contract did not supply one.
 */
import type { PayItemAmount, ReportColumn, ReportDriver } from '../../types/payroll';

export interface PeriodPayColumn {
  pay_item_id: number;
  label: string;
  code: string;
  unit: string | null;
}

export interface PeriodPayCell {
  pay_item_id: number;
  /** Backend amount string, or null when the backend did not supply one for this column. */
  amount: string | null;
}

export interface PeriodPayFixedValues {
  status_pay: string | null;
  gross_pay: string | null;
  minimum_adjustment: string | null;
  maximum_adjustment: string | null;
  bonus_total: string | null;
  total_pay: string | null;
}

export interface PeriodPayRow extends PeriodPayFixedValues {
  driver_id: number;
  driver_label: string;
  driver_code: string | null;
  /** Dense, ordered exactly like PeriodPayTableModel.columns. */
  items: PeriodPayCell[];
}

export interface PeriodPayFooter extends PeriodPayFixedValues {
  items: PeriodPayCell[];
}

export interface PeriodPayTableModel {
  columns: PeriodPayColumn[];
  rows: PeriodPayRow[];
  footer: PeriodPayFooter;
}

/** `driver.driver_name`, falling back to a stable `Driver #<id>` label. */
export function driverLabel(driver: { driver_id: number; driver_name: string | null }): string {
  return driver.driver_name ?? `Driver #${driver.driver_id}`;
}

function lookupAmount(amounts: PayItemAmount[] | null | undefined, payItemId: number): string | null {
  return amounts?.find((entry) => entry.pay_item_id === payItemId)?.amount ?? null;
}

function alignItems(columns: readonly PeriodPayColumn[], amounts: PayItemAmount[] | null | undefined): PeriodPayCell[] {
  return columns.map((column) => ({ pay_item_id: column.pay_item_id, amount: lookupAmount(amounts, column.pay_item_id) }));
}

export function buildPeriodPayTable(
  reportColumns: ReportColumn[],
  drivers: ReportDriver[],
  payItemTotals: PayItemAmount[] | null,
  payTotals: Record<string, string> | null,
): PeriodPayTableModel {
  const columns: PeriodPayColumn[] = reportColumns.map((column) => ({
    pay_item_id: column.pay_item_id,
    label: column.label,
    code: column.code,
    unit: column.unit,
  }));

  const rows: PeriodPayRow[] = drivers.map((driver) => {
    const pay = driver.pay;
    return {
      driver_id: driver.driver_id,
      driver_label: driverLabel(driver),
      driver_code: driver.driver_code,
      items: alignItems(columns, pay?.pay_item_amounts),
      status_pay: pay?.status_pay ?? null,
      gross_pay: pay?.gross_pay ?? null,
      minimum_adjustment: pay?.minimum_adjustment ?? null,
      maximum_adjustment: pay?.maximum_adjustment ?? null,
      bonus_total: pay?.bonus_total ?? null,
      total_pay: pay?.total_pay ?? null,
    };
  });

  const footer: PeriodPayFooter = {
    items: alignItems(columns, payItemTotals),
    status_pay: payTotals?.status_pay ?? null,
    gross_pay: payTotals?.gross_pay ?? null,
    minimum_adjustment: payTotals?.minimum_adjustment ?? null,
    maximum_adjustment: payTotals?.maximum_adjustment ?? null,
    bonus_total: payTotals?.bonus_total ?? null,
    total_pay: payTotals?.total_pay ?? null,
  };

  return { columns, rows, footer };
}
