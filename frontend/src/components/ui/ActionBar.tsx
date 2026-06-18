import React from 'react';
import styles from './ActionBar.module.css';

type ActionBarProps = {
  children: React.ReactNode;
  align?: 'left' | 'right' | 'between';
  sticky?: boolean;
  className?: string;
};

const alignClass: Record<NonNullable<ActionBarProps['align']>, string> = {
  left: styles.alignLeft,
  right: styles.alignRight,
  between: styles.alignBetween,
};

export function ActionBar({ children, align = 'right', sticky = false, className }: ActionBarProps) {
  return (
    <div
      className={[
        styles.root,
        alignClass[align],
        sticky ? styles.sticky : '',
        className,
      ].filter(Boolean).join(' ')}
    >
      {children}
    </div>
  );
}
