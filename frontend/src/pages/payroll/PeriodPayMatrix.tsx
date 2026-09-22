/**
 * PeriodPayMatrix — the one table both CurrentPayrollReportsDialog and
 * FinalizedPayrollLibraryDialog render for their `period-pay` view, so the
 * two surfaces never drift into two different table layouts for the same
 * backend financial contract.
 *
 * Presentation only: money formatting is supplied by the caller via
 * `formatCell`, so this component never decides how a value is displayed,
 * only where it goes.
 */
import type { ReactNode } from 'react';
import type { PeriodPayTableModel } from './periodPayTable';
import styles from './PeriodPayMatrix.module.css';

export interface PeriodPayMatrixProps {
  table: PeriodPayTableModel;
  emptyState: ReactNode;
  formatCell: (value: string | null) => string;
}

export function PeriodPayMatrix({ table, emptyState, formatCell }: PeriodPayMatrixProps) {
  if (table.rows.length === 0) return <>{emptyState}</>;
  return (
    <div className={styles.periodPayWrap}>
      <table className={styles.periodPayTable} aria-label="Period Pay by Daily Pay Item">
        <thead>
          <tr>
            <th scope="col" className={styles.periodPayDriverCol}>Driver</th>
            {table.columns.map((column) => (
              <th scope="col" key={column.pay_item_id}>
                {column.label}
                <small>{column.code}{column.unit ? ` · ${column.unit}` : ''}</small>
              </th>
            ))}
            <th scope="col">Status Pay</th>
            <th scope="col">Gross Pay</th>
            <th scope="col">Minimum Adjustment</th>
            <th scope="col">Maximum Adjustment</th>
            <th scope="col">Bonus</th>
            <th scope="col" className={styles.periodPayTotalCol}>Total Pay</th>
          </tr>
        </thead>
        <tbody>
          {table.rows.map((row) => (
            <tr key={row.driver_id}>
              <th scope="row" className={styles.periodPayDriverCol}>
                <strong>{row.driver_label}</strong>
                {row.driver_code && <span>{row.driver_code}</span>}
              </th>
              {row.items.map((item) => (
                <td key={item.pay_item_id} className={styles.numeric}>{formatCell(item.amount)}</td>
              ))}
              <td className={styles.numeric}>{formatCell(row.status_pay)}</td>
              <td className={styles.numeric}>{formatCell(row.gross_pay)}</td>
              <td className={styles.numeric}>{formatCell(row.minimum_adjustment)}</td>
              <td className={styles.numeric}>{formatCell(row.maximum_adjustment)}</td>
              <td className={styles.numeric}>{formatCell(row.bonus_total)}</td>
              <td className={`${styles.numeric} ${styles.periodPayTotalCol}`}>{formatCell(row.total_pay)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className={styles.periodPayFooterRow}>
            <th scope="row" className={styles.periodPayDriverCol}>TOTAL</th>
            {table.footer.items.map((item) => (
              <td key={item.pay_item_id} className={styles.numeric}>{formatCell(item.amount)}</td>
            ))}
            <td className={styles.numeric}>{formatCell(table.footer.status_pay)}</td>
            <td className={styles.numeric}>{formatCell(table.footer.gross_pay)}</td>
            <td className={styles.numeric}>{formatCell(table.footer.minimum_adjustment)}</td>
            <td className={styles.numeric}>{formatCell(table.footer.maximum_adjustment)}</td>
            <td className={styles.numeric}>{formatCell(table.footer.bonus_total)}</td>
            <td className={`${styles.numeric} ${styles.periodPayTotalCol}`}>{formatCell(table.footer.total_pay)}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
