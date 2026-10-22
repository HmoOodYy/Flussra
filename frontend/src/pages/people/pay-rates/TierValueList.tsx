import type { ScheduleValue } from '../../../types/compensation';
import { fmtAmount } from './payRatesView';
import { tierValueViews } from './payRatesTiers';
import styles from './PayRatesPage.module.css';

type TierValueListProps = {
  values: ScheduleValue[];
  unit: string | null;
  currencyCode: string | null;
  currencyDigits: number | null;
};

/** Every tier of an OrdinalTier schedule with its value, so no tier is hidden behind another. */
export function TierValueList({ values, unit, currencyCode, currencyDigits }: TierValueListProps) {
  return (
    <ul style={{ margin: 0, padding: 0, listStyle: 'none' }} aria-label="Tier values">
      {tierValueViews(values).map((tier) => (
        <li key={tier.label}>
          <span>{tier.label} → </span>
          {tier.amount === null
            ? <span className={styles.missingText}>Not set</span>
            : <strong>{fmtAmount(tier.amount, unit, currencyCode, currencyDigits)}</strong>}
        </li>
      ))}
    </ul>
  );
}
