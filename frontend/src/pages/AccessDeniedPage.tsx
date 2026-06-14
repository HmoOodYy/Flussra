import { useNavigate } from 'react-router-dom';
import styles from './AccessDeniedPage.module.css';

export function AccessDeniedPage() {
  const navigate = useNavigate();
  return (
    <div className={styles.container}>
      <div className={styles.card}>
        <div className={styles.icon} aria-hidden="true">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <line x1="4.93" y1="4.93" x2="19.07" y2="19.07" />
          </svg>
        </div>
        <h1 className={styles.title}>Access Denied</h1>
        <p className={styles.message}>
          You don't have permission to view this page.
          Contact your administrator if you believe this is a mistake.
        </p>
        <button className={styles.btn} onClick={() => navigate('/dashboard')}>
          Go to Dashboard
        </button>
      </div>
    </div>
  );
}
