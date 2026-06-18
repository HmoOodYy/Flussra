import React from 'react';
import styles from './ReadOnlyBanner.module.css';

type ReadOnlyBannerTone = 'info' | 'warning' | 'locked';

type ReadOnlyBannerProps = {
  title: string;
  message?: string;
  tone?: ReadOnlyBannerTone;
  actions?: React.ReactNode;
};

const toneClass: Record<ReadOnlyBannerTone, string> = {
  info: styles.toneInfo,
  warning: styles.toneWarning,
  locked: styles.toneLocked,
};

export function ReadOnlyBanner({ title, message, tone = 'info', actions }: ReadOnlyBannerProps) {
  return (
    <div className={[styles.root, toneClass[tone]].join(' ')}>
      <div className={styles.body}>
        <p className={styles.title}>{title}</p>
        {message && <p className={styles.message}>{message}</p>}
      </div>
      {actions && <div className={styles.actions}>{actions}</div>}
    </div>
  );
}
