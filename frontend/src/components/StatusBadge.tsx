import styles from './StatusBadge.module.css';

type Color = 'green' | 'blue' | 'amber' | 'teal' | 'red' | 'purple' | 'gray' | 'orange';

interface Props {
  label: string;
  color: Color;
}

export function StatusBadge({ label, color }: Props) {
  return (
    <span className={`${styles.badge} ${styles[color]}`}>
      <span className={styles.dot} />
      {label}
    </span>
  );
}

// ── Convenience badge components ─────────────────────────────────────────────

export function CompanyStatusBadge({ status, isSuspended }: { status: string; isSuspended: boolean }) {
  if (isSuspended)           return <StatusBadge label="Suspended" color="orange" />;
  if (status === 'Active')   return <StatusBadge label="Active"   color="green"  />;
  if (status === 'Inactive') return <StatusBadge label="Inactive" color="gray"   />;
  return <StatusBadge label={status} color="gray" />;
}

export function BranchStatusBadge({ status }: { status: string }) {
  if (status === 'Active')   return <StatusBadge label="Active"   color="green" />;
  if (status === 'Inactive') return <StatusBadge label="Inactive" color="gray"  />;
  if (status === 'Closed')   return <StatusBadge label="Closed"   color="red"   />;
  return <StatusBadge label={status} color="gray" />;
}

// Payroll period status → badge
const PERIOD_STATUS_COLOR: Record<string, Color> = {
  Draft:     'gray',
  Open:      'blue',
  InReview:  'amber',
  Returned:  'orange',
  Approved:  'green',
  Locked:    'teal',
  Cancelled: 'red',
  Archived:  'purple',
};

const PERIOD_STATUS_LABEL: Record<string, string> = {
  Draft: 'Prepared',
  InReview: 'In Review',
  Returned: 'Returned for Correction',
};

export function PeriodStatusBadge({ status }: { status: string }) {
  const color = PERIOD_STATUS_COLOR[status] ?? 'gray';
  return <StatusBadge label={PERIOD_STATUS_LABEL[status] ?? status} color={color} />;
}

export function LineStatusBadge({ status, needsReview }: { status: string; needsReview: boolean }) {
  if (status === 'Void')     return <StatusBadge label="Void"         color="gray"  />;
  if (needsReview)           return <StatusBadge label="Needs Review" color="amber" />;
  if (status === 'Active')   return <StatusBadge label="Active"       color="green" />;
  if (status === 'Rejected') return <StatusBadge label="Rejected"     color="red"   />;
  return <StatusBadge label={status} color="gray" />;
}
