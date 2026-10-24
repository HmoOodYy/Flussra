import { driverInitials } from './dayEntryPageModel';
import styles from './PayrollEntryPage.module.css';

interface DriverIdentityProps {
  name: string;
  number: string | null;
  /** Whether the side panel currently shows this driver. */
  active: boolean;
  onOpen: () => void;
}

/** The driver cell of both tables: initials, the name (a button opening the side panel) and the number. */
export function DriverIdentity({ name, number, active, onOpen }: DriverIdentityProps) {
  return (
    <div className={styles.driverCell}>
      <span className={styles.avatar} aria-hidden="true">{driverInitials(name)}</span>
      <span className={styles.driverText}>
        <button
          type="button"
          className={`${styles.driverNameBtn} ${active ? styles.driverNameBtnActive : ''}`}
          aria-expanded={active}
          onClick={onOpen}
        >
          {name}
        </button>
        {number && <span className={styles.driverNumber}>{number}</span>}
      </span>
    </div>
  );
}
