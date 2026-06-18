import React from 'react';
import styles from './ErrorState.module.css';

type ErrorStateProps = {
  title?: string;
  message: string;
  action?: React.ReactNode;
};

export function ErrorState({ title = 'Something went wrong', message, action }: ErrorStateProps) {
  return (
    <div className={styles.root}>
      <p className={styles.title}>{title}</p>
      <p className={styles.message}>{message}</p>
      {action && <div className={styles.action}>{action}</div>}
    </div>
  );
}
