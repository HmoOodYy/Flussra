import type { ReactNode } from 'react';
import type { FinalizedAvailabilityState, FinalizedSectionAvailability } from '../../../types/payroll';
import { availabilityMessage } from './formatting';
import styles from '../FinalizedPayrollLibraryDialog.module.css';

const AVAILABILITY_LABELS: Record<FinalizedAvailabilityState, string> = {
  AVAILABLE: 'Available',
  EMPTY: 'Empty',
  PARTIAL: 'Partial',
  UNAVAILABLE: 'Unavailable',
};

export function AvailabilityBadge({ availability }: { availability: FinalizedSectionAvailability | undefined }) {
  const state = availability?.state ?? 'UNAVAILABLE';
  return (
    <span className={`${styles.availability} ${styles[state.toLowerCase()]}`}>
      {AVAILABILITY_LABELS[state]}
    </span>
  );
}

export function StateMessage({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <p className={error ? styles.error : styles.state}>{children}</p>;
}

export function EvidenceSectionMessage({
  availability,
  emptyText,
}: {
  availability: FinalizedSectionAvailability | undefined;
  emptyText: string;
}) {
  if (!availability || availability.state === 'UNAVAILABLE') {
    return <StateMessage>{availabilityMessage(availability, emptyText)}</StateMessage>;
  }
  if (availability.state === 'EMPTY') return <StateMessage>{emptyText}</StateMessage>;
  if (availability.state === 'PARTIAL') {
    return <StateMessage>{availabilityMessage(availability, emptyText)}</StateMessage>;
  }
  return null;
}
