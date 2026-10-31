/**
 * Warning (never a blocker) listing Driver minimum/maximum pay rules that would stop fitting
 * a calendar change. Shown inside the publish and change-policy previews.
 */
import styles from './PayrollSetupsPage.module.css';
import { buildPayRuleImpactView } from './payRuleImpactView';
import type { AffectedPayRuleResponse } from '../../../types/payrollSetup';

export function PayRuleImpactNotice({ rules }: { rules: readonly AffectedPayRuleResponse[] | undefined }) {
  const view = buildPayRuleImpactView(rules);
  if (!view) return null;
  return (
    <div className={styles.payRuleWarning} role="note">
      <p className={styles.payRuleWarningText}>{view.message}</p>
      <ul className={styles.payRuleWarningList}>
        {view.rows.map((row) => (
          <li key={row.key}>
            <strong>{row.driver}</strong>
            {` — ${row.branch} · ${row.ruleType} · set for a ${row.calendar}`}
          </li>
        ))}
        {view.hiddenCount > 0 && <li>{`and ${view.hiddenCount} more`}</li>}
      </ul>
    </div>
  );
}
