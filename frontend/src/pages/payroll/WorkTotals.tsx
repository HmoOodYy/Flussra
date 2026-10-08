/**
 * Per-definition quantity totals for a calculation report.
 *
 * Totals are keyed by PayrollPeriodDefinitionID and labelled from the report's frozen
 * definition columns. Which totals appear depends only on the period's definitions;
 * no code or name is given any special meaning.
 */
import type { DefinitionQuantity, ReportColumn } from '../../types/payroll';

export function WorkTotals({
  totals,
  columns,
  className,
}: {
  totals: DefinitionQuantity[];
  columns: ReportColumn[];
  className?: string;
}) {
  const labels = new Map(columns.map((c) => [c.payroll_period_definition_id, c.label]));
  return (
    <div className={className}>
      <h4>Work totals</h4>
      {totals.length === 0 ? (
        <p>No values are currently available.</p>
      ) : (
        <dl>
          {totals.map((total) => (
            <div key={total.payroll_period_definition_id}>
              <dt>{labels.get(total.payroll_period_definition_id) ?? `Definition ${total.payroll_period_definition_id}`}</dt>
              <dd>{total.quantity}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}
