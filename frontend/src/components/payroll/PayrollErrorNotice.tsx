import styles from './PayrollErrorNotice.module.css';
import { friendlyError } from '../../lib/payrollSetupErrors';
import type { ApiErrorInfo } from '../../lib/payrollSetupErrors';

type PayrollErrorNoticeProps = {
  info: ApiErrorInfo;
  className?: string;
};

/**
 * Shows friendlyError(info).message prominently. Raw codes and the
 * server-authored detail (when it differs from the friendly message) are
 * tucked behind a collapsed "Technical details" panel, never shown in the
 * primary UI.
 */
export function PayrollErrorNotice({ info, className }: PayrollErrorNoticeProps) {
  const { message, code, detail } = friendlyError(info);
  const hasTechnicalDetails = code != null || detail != null;

  return (
    <div className={[styles.root, className].filter(Boolean).join(' ')} role="alert">
      <p className={styles.message}>{message}</p>
      {hasTechnicalDetails && (
        <details className={styles.details}>
          <summary className={styles.detailsSummary}>Technical details</summary>
          <div className={styles.detailsBody}>
            {code != null && (
              <p className={styles.detailsRow}>
                <span className={styles.detailsLabel}>Code:</span> {code}
              </p>
            )}
            {detail != null && (
              <p className={styles.detailsRow}>
                <span className={styles.detailsLabel}>Server message:</span> {detail}
              </p>
            )}
          </div>
        </details>
      )}
    </div>
  );
}
