import React from 'react';
import styles from './SectionCard.module.css';

type SectionCardProps = {
  title?: string;
  subtitle?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  padded?: boolean;
  className?: string;
};

export function SectionCard({
  title,
  subtitle,
  actions,
  children,
  padded = true,
  className,
}: SectionCardProps) {
  const hasHeader = title || subtitle || actions;

  return (
    <div className={[styles.root, className].filter(Boolean).join(' ')}>
      {hasHeader && (
        <div className={styles.header}>
          <div className={styles.headerText}>
            {title && <h2 className={styles.title}>{title}</h2>}
            {subtitle && <p className={styles.subtitle}>{subtitle}</p>}
          </div>
          {actions && <div className={styles.actions}>{actions}</div>}
        </div>
      )}
      <div className={[styles.body, padded ? styles.padded : ''].filter(Boolean).join(' ')}>
        {children}
      </div>
    </div>
  );
}
