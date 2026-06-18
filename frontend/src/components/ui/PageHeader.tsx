import React from 'react';
import styles from './PageHeader.module.css';

type PageHeaderProps = {
  title: string;
  subtitle?: string;
  eyebrow?: string;
  actions?: React.ReactNode;
  children?: React.ReactNode;
};

export function PageHeader({ title, subtitle, eyebrow, actions, children }: PageHeaderProps) {
  return (
    <div className={styles.root}>
      <div className={styles.top}>
        <div className={styles.text}>
          {eyebrow && <p className={styles.eyebrow}>{eyebrow}</p>}
          <h1 className={styles.title}>{title}</h1>
          {subtitle && <p className={styles.subtitle}>{subtitle}</p>}
        </div>
        {actions && <div className={styles.actions}>{actions}</div>}
      </div>
      {children && <div className={styles.below}>{children}</div>}
    </div>
  );
}
