/**
 * ConfirmDialog — lightweight confirmation modal.
 *
 * Uses a portal-style fixed overlay so it stacks above any existing modal.
 * Never calls window.confirm().
 */
import styles from './ConfirmDialog.module.css';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  /** 'primary' for neutral actions, 'danger' for destructive ones */
  variant?: 'primary' | 'danger';
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  variant = 'primary',
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  if (!open) return null;

  return (
    <div
      className={styles.overlay}
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-dlg-title"
      onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}
    >
      <div className={styles.dialog}>
        <h2 id="confirm-dlg-title" className={styles.title}>{title}</h2>
        <p className={styles.message}>{message}</p>
        <div className={styles.actions}>
          <button
            className={styles.btnCancel}
            onClick={onCancel}
            disabled={loading}
          >
            Cancel
          </button>
          <button
            className={`${styles.btnConfirm} ${variant === 'danger' ? styles.btnDanger : styles.btnPrimary}`}
            onClick={onConfirm}
            disabled={loading}
            autoFocus
          >
            {loading ? 'Working…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
