import styles from '../SettingsPlaceholder.module.css';

export function BranchesPage() {
  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1 className={styles.title}>Branches / Locations</h1>
        <p className={styles.subtitle}>Manage your company's branches and locations.</p>
      </div>
      <div className={styles.placeholder}>
        <BranchIcon />
        <span className={styles.placeholderTitle}>Branches — Coming Soon</span>
        <span className={styles.placeholderDesc}>
          Create, edit, and manage branch locations. Set default branches,
          view operational metrics, and configure per-branch payroll settings.
        </span>
      </div>
    </div>
  );
}

function BranchIcon() {
  return (
    <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"
      style={{ color: '#9ca3af' }} aria-hidden="true">
      <rect x="2" y="3" width="6" height="5" rx="1"/>
      <rect x="16" y="3" width="6" height="5" rx="1"/>
      <rect x="9" y="16" width="6" height="5" rx="1"/>
      <path d="M5 8v4h14V8M12 12v4"/>
    </svg>
  );
}
