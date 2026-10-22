import type { OrdinalTierInput } from '../../../types/compensation';
import { ordinalTierLabel, ordinalTopologyError } from './payItemsModel';
import styles from './PayItemsPage.module.css';

interface OrdinalTierEditorProps {
  tiers: OrdinalTierInput[];
  setTiers(tiers: OrdinalTierInput[]): void;
  disabled?: boolean;
}

export function OrdinalTierEditor({ tiers, setTiers, disabled = false }: OrdinalTierEditorProps) {
  function updateEnd(index: number, value: string) {
    const end = value === '' ? Number.NaN : Number(value);
    let nextFrom = 1;
    setTiers(tiers.map((tier, position) => {
      const ordinalFrom = nextFrom;
      const ordinalTo = position === index ? end : tier.ordinal_to;
      if (ordinalTo !== null) nextFrom = ordinalTo + 1;
      return { ordinal_from: ordinalFrom, ordinal_to: ordinalTo };
    }));
  }

  function addTier() {
    const last = tiers[tiers.length - 1];
    const nextFrom = last.ordinal_from + 1;
    setTiers([
      ...tiers.slice(0, -1),
      { ...last, ordinal_to: last.ordinal_from },
      { ordinal_from: nextFrom, ordinal_to: null },
    ]);
  }

  function removeLastTier() {
    if (tiers.length < 2) return;
    const remaining = tiers.slice(0, -1);
    remaining[remaining.length - 1] = { ...remaining[remaining.length - 1], ordinal_to: null };
    setTiers(remaining);
  }

  const error = ordinalTopologyError(tiers);
  return (
    <div className={styles.formGroup}>
      <span className={styles.label}>Quantity tiers</span>
      <div className={styles.inputNote}>
        Each quantity unit is paid at the Driver&apos;s rate for its tier. For example, 1–2, 3–5, 6+.
      </div>
      <div className={styles.tierEditor}>
        {tiers.map((tier, index) => (
          <div className={styles.tierEditorRow} key={index}>
            <span className={styles.tierEditorRange}>{ordinalTierLabel(tier)}</span>
            {tier.ordinal_to !== null && (
              <label className={styles.tierEditorEnd}>
                through
                <input className={styles.input} type="number" min={tier.ordinal_from}
                  step="1" value={Number.isNaN(tier.ordinal_to) ? '' : tier.ordinal_to}
                  aria-label={`Tier ${index + 1} end`} disabled={disabled}
                  onChange={(event) => updateEnd(index, event.target.value)} />
              </label>
            )}
            {index === tiers.length - 1 && tiers.length > 1 && (
              <button type="button" className={styles.btnGhost} disabled={disabled} onClick={removeLastTier}>
                Remove final tier
              </button>
            )}
          </div>
        ))}
      </div>
      {error && <div className={styles.errorAlert} role="alert">{error}</div>}
      <button type="button" className={styles.btnSecondary} disabled={disabled || Boolean(error)} onClick={addTier}>
        Add tier
      </button>
    </div>
  );
}
