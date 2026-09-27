import { useState } from 'react';
import styles from './PayrollBoundaryDateInput.module.css';
import { DateInput, type DateInputStatus } from '../ui/DateInput';
import { formatIsoLong } from '../../lib/isoDate';
import {
  boundaryStatus,
  describeBoundaryChoice,
  isChoicesCurrent,
  stepTarget,
  type BoundaryContext,
  type BoundaryStatus,
} from '../../lib/payrollBoundaryView';
import { PayrollErrorNotice } from './PayrollErrorNotice';
import type { ApiErrorInfo } from '../../lib/payrollSetupErrors';
import type { BoundaryChoicesResponse } from '../../types/payrollSetup';

export type { BoundaryContext };

type PayrollBoundaryDateInputProps = {
  id: string;
  label: string;
  value: string;
  onChange: (iso: string) => void;
  choices: BoundaryChoicesResponse | null;
  loading: boolean;
  error?: ApiErrorInfo | null;
  disabled?: boolean;
  hint?: string;
  showTechnicalDetails?: boolean;
  context: BoundaryContext;
};

/**
 * A DateInput wired to backend-supplied BoundaryChoicesResponse: stepper
 * buttons that move the whole date to the next/previous valid boundary, and
 * a status line explaining the current date (or the nearest valid ones).
 * Never computes chronology itself — every judgment about which dates are
 * valid comes from `choices`, as supplied by the backend.
 */
export function PayrollBoundaryDateInput({
  id,
  label,
  value,
  onChange,
  choices,
  loading,
  error,
  disabled = false,
  hint,
  showTechnicalDetails = true,
  context,
}: PayrollBoundaryDateInputProps) {
  // Tracks the DateInput's own calendar-level status (separately from
  // `value`, which collapses to '' for anything that isn't a valid ISO
  // date) so we know when to suppress backend boundary status: an
  // incomplete/invalid calendar entry has nothing to do with chronology yet.
  const [typedStatus, setTypedStatus] = useState<DateInputStatus>(value === '' ? 'empty' : 'valid');

  function handleDateInputChange(iso: string, status: DateInputStatus) {
    setTypedStatus(status);
    onChange(iso);
  }

  function handleStep(direction: 'next' | 'previous') {
    const target = stepTarget(choices, value, direction);
    if (target == null) return;
    setTypedStatus('valid');
    onChange(target);
  }

  const isStale = !isChoicesCurrent(choices, value);
  const nextTarget = stepTarget(choices, value, 'next');
  const previousTarget = stepTarget(choices, value, 'previous');
  const nextDisabled = disabled || loading || choices == null || isStale || nextTarget == null;
  const previousDisabled = disabled || loading || choices == null || isStale || previousTarget == null;

  const showBoundaryStatus = typedStatus === 'valid' || typedStatus === 'empty';
  const status: BoundaryStatus = showBoundaryStatus
    ? boundaryStatus(choices, value, loading)
    : { kind: 'idle' };

  const showEarliestHint =
    (context === 'assignment' || context === 'onboarding') &&
    showBoundaryStatus &&
    !isStale &&
    choices?.earliest_allowed_date != null;

  const statusMessageId = `${id}-boundary-status`;

  return (
    <div className={styles.root}>
      <div className={styles.row}>
        <div className={styles.dateInput}>
          <DateInput
            id={id}
            label={label}
            value={value}
            onChange={handleDateInputChange}
            hint={hint}
            disabled={disabled}
            describedBy={showBoundaryStatus ? statusMessageId : undefined}
          />
        </div>
        <div className={styles.steppers}>
          <button
            type="button"
            className={styles.stepperButton}
            aria-label="Next valid date"
            title="Next valid date"
            disabled={nextDisabled}
            onClick={() => handleStep('next')}
          >
            &uarr;
          </button>
          <button
            type="button"
            className={styles.stepperButton}
            aria-label="Previous valid date"
            title="Previous valid date"
            disabled={previousDisabled}
            onClick={() => handleStep('previous')}
          >
            &darr;
          </button>
        </div>
      </div>

      {error && <PayrollErrorNotice info={error} />}

      {!error && showBoundaryStatus && (
        <div id={statusMessageId} aria-live="polite">
          {status.kind === 'valid' && choices?.requested && (
            <p className={styles.status}>{describeBoundaryChoice(choices.requested, context)}</p>
          )}

          {status.kind === 'invalid' && (
            <>
              <p className={[styles.status, styles.statusInvalid].join(' ')}>{status.text}</p>
              {(status.previous != null || status.next != null) && (
                <div className={styles.nearestList}>
                  <span className={styles.nearestLabel}>Nearest valid dates:</span>
                  {status.previous != null && (
                    <button
                      type="button"
                      className={styles.nearestButton}
                      onClick={() => onChange(status.previous!)}
                    >
                      {formatIsoLong(status.previous)}
                    </button>
                  )}
                  {status.next != null && (
                    <button
                      type="button"
                      className={styles.nearestButton}
                      onClick={() => onChange(status.next!)}
                    >
                      {formatIsoLong(status.next)}
                    </button>
                  )}
                </div>
              )}
              {showTechnicalDetails && status.codes.length > 0 && (
                <details className={styles.details}>
                  <summary className={styles.detailsSummary}>Technical details</summary>
                  <div className={styles.detailsBody}>
                    {choices?.conflicts.map((conflict, index) => (
                      <p key={`${conflict.code}-${index}`}>
                        {conflict.code}: {conflict.reason}
                      </p>
                    ))}
                  </div>
                </details>
              )}
            </>
          )}

          {status.kind === 'none-found' && (
            <p className={[styles.status, styles.statusNoneFound].join(' ')}>{status.text}</p>
          )}
        </div>
      )}

      {showEarliestHint && (
        <p className={styles.earliestHint}>
          Payroll can start as early as {formatIsoLong(choices!.earliest_allowed_date!)}.
        </p>
      )}
    </div>
  );
}
