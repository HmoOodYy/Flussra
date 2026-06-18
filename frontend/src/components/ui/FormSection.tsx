import React from 'react';
import styles from './FormSection.module.css';

type FormSectionProps = {
  title?: string;
  description?: string;
  children: React.ReactNode;
  actions?: React.ReactNode;
  className?: string;
};

export function FormSection({ title, description, children, actions, className }: FormSectionProps) {
  const hasHeader = title || description || actions;

  return (
    <div className={[styles.root, className].filter(Boolean).join(' ')}>
      {hasHeader && (
        <div className={styles.header}>
          <div className={styles.headerText}>
            {title && <h3 className={styles.title}>{title}</h3>}
            {description && <p className={styles.description}>{description}</p>}
          </div>
          {actions && <div className={styles.actions}>{actions}</div>}
        </div>
      )}
      <div className={styles.fields}>{children}</div>
    </div>
  );
}
